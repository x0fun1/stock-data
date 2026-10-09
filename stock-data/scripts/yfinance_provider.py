"""Lazy, bounded Yahoo gateway. No network or cache activity at module import.

The injected transport is a curl_cffi Session subclass, not a monkey patch.
One provider owns process-wide yfinance configuration for its CLI job; callers
must not run providers with different configurations concurrently.
"""
from __future__ import annotations

import datetime as dt
import importlib
import math
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urljoin

BASELINE = '1.7.0'
COMMIT = '5cae563642b59f49adf6a04a5ad6744f8b0e084d'
HOSTS = frozenset({'query1.finance.yahoo.com', 'query2.finance.yahoo.com',
                  'finance.yahoo.com', 'fc.yahoo.com', 'guce.yahoo.com', 'consent.yahoo.com'})
# Only these documented authentication endpoints may cross origins.
HANDSHAKES = {'fc.yahoo.com': ('/',), 'guce.yahoo.com': ('/consent', '/copyConsent'),
              'consent.yahoo.com': ('/v2/collectConsent',), 'finance.yahoo.com': ('/',)}


class GatewayError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _has_data(value):
    """Nested empty modules are not successful data; genuine zero remains data."""
    if hasattr(value, 'empty'):
        return not value.empty
    if isinstance(value, dict):
        return any(_has_data(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_data(item) for item in value)
    return value is not None and value != ''


def _safe(value):
    """Sanitize archives without preserving credentials, URLs with query tokens."""
    from yfinance_normalize import json_safe
    value = json_safe(value)
    def clean(v):
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()
                    if not re.search(r'cookie|crumb|token|password|proxy|authorization|sessionid', str(k), re.I)}
        if isinstance(v, list):
            return [clean(x) for x in v]
        if isinstance(v, str) and v.startswith(('http://', 'https://')):
            p = urlsplit(v)
            if p.username or p.password:
                return '[redacted URL]'
            if re.search(r'crumb|token|sessionid', p.query, re.I):
                return p._replace(query='', fragment='').geturl()
        return v
    return clean(value)


class TransportPolicy:
    """Shared pretransport budget and streaming decoded-body limits."""
    def __init__(self, *, timeout=15, deadline=120, max_requests=30,
                 max_bytes=20 * 1024 * 1024, clock=time.monotonic):
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0
               for v in (timeout, deadline, max_requests, max_bytes)):
            raise GatewayError('InvalidParameters', 'Transport limits must be positive')
        self.timeout, self.deadline, self.max_requests = timeout, deadline, max_requests
        self.max_bytes, self.clock = max_bytes, clock
        self.started = clock()
        self.count = 0
        self.blocked = None
        self.records = []
        self.chart_metadata = {}
        self.failures = []
        self.auth_failures = {}
        self.lock = threading.Lock()

    def validate_url(self, url):
        p = urlsplit(url)
        try:
            allowed = (p.scheme == 'https' and p.hostname in HOSTS and
                       p.port in (None, 443) and not p.username and not p.password)
        except ValueError:
            allowed = False
        if not allowed:
            raise GatewayError('InvalidParameters', 'URL outside approved HTTPS Yahoo hosts')
        return p

    def before(self, url):
        p = self.validate_url(url)
        with self.lock:
            if self.blocked:
                raise GatewayError(*self.blocked)
            remaining = self.deadline - (self.clock() - self.started)
            if remaining <= 0 or self.count >= self.max_requests:
                self.blocked = ('BudgetExceeded', 'Yahoo job deadline or request budget exhausted')
                raise GatewayError(*self.blocked)
            key = (p.hostname, p.path)
            if self.auth_failures.get(key, 0) >= 2:
                raise GatewayError('AccessDenied', 'Only one authentication recovery is allowed')
            self.count += 1
        return min(self.timeout, remaining)

    def redirect(self, old, new):
        a, b = self.validate_url(old), self.validate_url(new)
        if a.netloc == b.netloc:
            return
        # Cross-origin redirects must start AND finish on registered handshake paths.
        def handshake(p):
            return p.path in HANDSHAKES.get(p.hostname, ()) or (p.path == '' and p.hostname in HANDSHAKES)
        if not (handshake(a) and handshake(b)):
            raise GatewayError('AccessDenied', 'Unregistered cross-origin redirect')


def bounded_session_class(base):
    """Build subclass lazily; fake base sessions also support offline tests."""
    class BoundedSession(base):
        def __init__(self, policy, **kwargs):
            kwargs['retry'] = 0
            super().__init__(**kwargs)
            self.policy = policy

        def request(self, method, url, **kwargs):
            if method.upper() not in ('GET', 'POST'):
                raise GatewayError('InvalidParameters', 'Only GET and POST are permitted')
            if kwargs.get('stream') or kwargs.get('content_callback'):
                raise GatewayError('InvalidParameters', 'External streaming callbacks are not supported')
            kwargs.pop('allow_redirects', None)
            kwargs.pop('max_redirects', None)
            for hop in range(4):
                timeout = self.policy.before(url)
                kwargs['timeout'] = min(float(kwargs.get('timeout') or timeout), timeout)
                chunks, size, violation = [], 0, []
                def receive(chunk):
                    nonlocal size
                    size += len(chunk)
                    if size > self.policy.max_bytes:
                        violation.append(('InvalidResponse', 'Decoded response exceeds configured size boundary'))
                        self.policy.blocked = violation[-1]
                        return 0
                    if self.policy.clock() - self.policy.started >= self.policy.deadline:
                        violation.append(('BudgetExceeded', 'Deadline reached while receiving response'))
                        self.policy.blocked = violation[-1]
                        return 0
                    chunks.append(chunk)
                    return len(chunk)
                try:
                    response = super().request(method, url, allow_redirects=False,
                                               content_callback=receive, **kwargs)
                except Exception:
                    if violation:
                        raise GatewayError(*violation[0]) from None
                    self.policy.failures.append(('NetworkError', 'Yahoo transport failed (details redacted)'))
                    raise GatewayError(*self.policy.failures[-1]) from None
                if violation:
                    raise GatewayError(*violation[0])
                # curl callback bypasses its normal response buffer; restore bounded bytes.
                response.content = b''.join(chunks)
                status = response.status_code
                p = urlsplit(url)
                self.policy.records.append({'host': p.hostname, 'path': p.path,
                                            'status': status, 'decoded_bytes': size})
                if status == 200 and p.path.startswith('/v8/finance/chart/'):
                    # Capture business metadata only, never cookie/consent wire payloads.
                    import json
                    try:
                        wire = json.loads(response.content)['chart']['result'][0]['meta']
                        if isinstance(wire.get('symbol'), str):
                            self.policy.chart_metadata[wire['symbol']] = {
                                k: wire.get(k) for k in ('symbol','currency','exchangeTimezoneName',
                                                        'quoteType','regularMarketTime','currentTradingPeriod')}
                    except (ValueError, KeyError, TypeError, IndexError):
                        pass
                if status == 429:
                    self.policy.blocked = ('RateLimited', 'Yahoo rate limit; job circuit open')
                    raise GatewayError(*self.policy.blocked)
                if status == 403:
                    self.policy.blocked = ('AccessDenied', 'Yahoo access denied; no further job requests')
                    raise GatewayError(*self.policy.blocked)
                if status == 401:
                    key = (p.hostname, p.path)
                    self.policy.auth_failures[key] = self.policy.auth_failures.get(key, 0) + 1
                    if self.policy.auth_failures[key] >= 2:
                        self.policy.blocked = ('AccessDenied', 'Authentication recovery failed; job stopped')
                        raise GatewayError(*self.policy.blocked)
                if status >= 500:
                    self.policy.failures.append(('NetworkError', 'Yahoo returned a server error'))
                if status not in (301, 302, 303, 307, 308):
                    return response
                if hop == 3:
                    raise GatewayError('AccessDenied', 'Redirect limit exceeded')
                location = response.headers.get('Location') or response.headers.get('location')
                if not location:
                    raise GatewayError('InvalidResponse', 'Redirect without Location')
                new = urljoin(url, location)
                self.policy.redirect(url, new)
                if status == 303 or (status in (301, 302) and method.upper() == 'POST'):
                    method = 'GET'
                    kwargs.pop('data', None)
                    kwargs.pop('json', None)
                # Do not replay original query parameters into redirect destinations.
                kwargs.pop('params', None)
                url = new
    return BoundedSession


def _legacy_yahoo_fallback(function, params, reason):
    from yahoo_compat_fallback import fallback
    return fallback(function, params, reason)


class YahooProvider:
    NAMES = frozenset('yahoo_history yahoo_history_batch yahoo_quote yahoo_profile yahoo_financials yahoo_news yahoo_search yahoo_statistics yahoo_analysis yahoo_holders yahoo_options yahoo_earnings yahoo_funds yahoo_screen'.split())

    def __init__(self, cache_dir=None, *, timeout=15, deadline=None, max_requests=None,
                 budget=None, yf_module=None, session=None, proxy=None, **kwargs):
        if budget is not None:
            max_requests = budget
        if kwargs:
            raise GatewayError('InvalidParameters', 'Unknown provider configuration')
        self.cache_dir = cache_dir or os.environ.get('STOCK_DATA_YAHOO_CACHE_DIR')
        self._default_deadline, self._default_budget = deadline is None, max_requests is None
        self.policy = TransportPolicy(timeout=timeout, deadline=120 if deadline is None else deadline,
                                      max_requests=30 if max_requests is None else max_requests)
        self._yf, self._session = yf_module, session
        self.proxy = proxy
        self._ready = False
        self._tickers = {}

    def _initialize(self):
        if self._ready:
            return
        if self._yf is None:
            try:
                self._yf = importlib.import_module('yfinance')
                curl = importlib.import_module('curl_cffi.requests')
            except ImportError:
                raise GatewayError('DependencyUnavailable', 'Install requirements-yahoo.txt in the executing Python environment') from None
            if self._yf.__version__ != BASELINE:
                raise GatewayError('UnsupportedCapability', 'This gateway requires fixed yfinance 1.7.0 baseline')
            if not getattr(importlib.import_module('yfinance._http'), 'HAS_CURL_CFFI', False):
                raise GatewayError('UnsupportedCapability', 'curl_cffi backend required; disabled fallback is not equivalent')
            self._session = bounded_session_class(curl.Session)(self.policy, impersonate='chrome')
        if self._session is None:
            raise GatewayError('DependencyUnavailable', 'A bounded session is required')
        if not hasattr(self._session, 'policy'):
            raise GatewayError('InvalidParameters', 'Session must enforce TransportPolicy')
        self._session.policy = self.policy
        if not self.cache_dir:
            raise GatewayError('InvalidParameters', 'Explicit writable cache_dir required for Yahoo calls')
        cache = Path(self.cache_dir)
        cache.mkdir(parents=True, exist_ok=True)
        self._yf.set_tz_cache_location(str(cache))
        self._yf.config.debug.hide_exceptions = False
        # Disable nested yfinance retry loops; transport does not retry either.
        self._yf.config.network.retries = 0
        self._yf.config.network.proxy = self.proxy
        self._ready = True

    def _ticker(self, symbol):
        if not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z0-9^][A-Za-z0-9.^=\-]{0,30}', symbol):
            raise GatewayError('InvalidParameters', 'Invalid Yahoo symbol')
        if symbol not in self._tickers:
            self._tickers[symbol] = self._yf.Ticker(symbol, session=self._session)
        return self._tickers[symbol]

    def call(self, name, **params):
        """Use yfinance first, then automatically try the registered legacy route."""
        if name not in self.NAMES:
            raise GatewayError('UnsupportedCapability', 'Unregistered Yahoo capability')
        try:
            result = self._call_yfinance(name, **params)
        except GatewayError as exc:
            if exc.code == 'InvalidParameters' and exc.message != 'Unsupported parameters or upstream signature':
                raise
            reason = {'category': exc.code, 'exception_type': type(exc).__name__}
            return _legacy_yahoo_fallback(name, params, reason)
        except Exception as exc:
            return _legacy_yahoo_fallback(name, params, {
                'category': 'DataUnavailable', 'exception_type': type(exc).__name__})
        if isinstance(result, dict) and result.get('status') in {'empty', 'unavailable', 'error', 'failed'}:
            error = result.get('error')
            reason = {'category': error.get('category', 'DataUnavailable') if isinstance(error, dict) else 'DataUnavailable',
                      'exception_type': error.get('exception_type', 'EmptyYahooResult') if isinstance(error, dict) else 'EmptyYahooResult'}
            return _legacy_yahoo_fallback(name, params, reason)
        return result

    def _call_yfinance(self, name, **params):
        if name not in self.NAMES:
            raise GatewayError('UnsupportedCapability', 'Unregistered Yahoo capability')
        if not self._ready:
            # Job limits are selected once, never reset for lazy or repair calls.
            self.policy.started = self.policy.clock()
            if name == 'yahoo_history_batch':
                if self._default_deadline:
                    self.policy.deadline = 180
                if self._default_budget:
                    self.policy.max_requests = 100
        self._initialize()
        fetched = dt.datetime.now(dt.timezone.utc).isoformat()
        failure_start = len(self.policy.failures)
        try:
            # Fixed dispatch: user strings are never attribute or method names.
            dispatch = {'yahoo_history': self._history, 'yahoo_history_batch': self._batch,
                        'yahoo_quote': self._quote, 'yahoo_profile': self._profile,
                        'yahoo_financials': self._financials, 'yahoo_news': self._news,
                        'yahoo_search': self._search, 'yahoo_statistics': self._statistics,
                        'yahoo_analysis': self._analysis, 'yahoo_holders': self._holders,
                        'yahoo_options': self._options, 'yahoo_earnings': self._earnings,
                        'yahoo_funds': self._funds, 'yahoo_screen': self._screen}
            result = dispatch[name](**params)
        except GatewayError:
            raise
        except TypeError:
            raise GatewayError('InvalidParameters', 'Unsupported parameters or upstream signature') from None
        except AttributeError:
            raise GatewayError('UnsupportedCapability', 'Required public API unavailable') from None
        except Exception:
            if self.policy.blocked:
                raise GatewayError(*self.policy.blocked) from None
            raise GatewayError('DataUnavailable', 'Yahoo capability failed') from None
        data, extra = result if isinstance(result, tuple) else (result, {})
        # Canonical normalizers may themselves return a gateway-like envelope.
        if isinstance(data, dict) and 'data' in data and 'status' in data:
            extra = {**{k: v for k, v in data.items() if k != 'data'}, **extra}
            data = data['data']
        status = extra.pop('status', 'success' if _has_data(data) else 'empty')
        failures = self.policy.failures[failure_start:]
        if failures:
            if status == 'empty' or not data:
                raise GatewayError(*failures[-1])
            status = 'partial'
            extra.setdefault('warnings', []).extend(f[1] for f in failures)
        if self.policy.blocked:
            if status == 'empty' or not data:
                raise GatewayError(*self.policy.blocked)
            status = 'partial'
            extra.setdefault('warnings', []).append(self.policy.blocked[1])
        return _safe({'status': status, 'data': data, 'actual_source': 'Yahoo Finance',
                      'fetched_at_utc': fetched, 'fallback_used': False,
                      'provenance': {'library': 'yfinance', 'version': getattr(self._yf, '__version__', None),
                                     'baseline_commit': COMMIT, 'backend': 'curl_cffi',
                                     'parameters': params, 'transport': self.policy.records,
                                     'archive_kind': 'normalized_library_result_not_wire_payload'}, **extra})

    def close(self):
        """Release the job's session; never export its cookies or cache files."""
        if self._session is not None and hasattr(self._session, 'close'):
            self._session.close()
        self._session = None
        self._tickers.clear()
        self._ready = False

    @staticmethod
    def _window(start, end, period, interval):
        if period and (start or end):
            raise GatewayError('InvalidParameters', 'period and start/end are mutually exclusive')
        if bool(start) != bool(end):
            raise GatewayError('InvalidParameters', 'start and end must be provided together')
        if interval not in ('1m','2m','5m','15m','30m','60m','90m','1h','1d','5d','1wk','1mo','3mo'):
            raise GatewayError('InvalidParameters', 'Unsupported interval')
        if start:
            try:
                a, b = dt.datetime.fromisoformat(start), dt.datetime.fromisoformat(end)
                if b <= a:
                    raise ValueError()
                limit = 8 if interval == '1m' else 730 if interval in ('60m','1h') else 60 if interval.endswith('m') and interval not in ('1mo','3mo') else None
                if limit and (b-a).total_seconds() > limit * 86400:
                    raise GatewayError('InsufficientCoverage', 'Requested intraday window exceeds provider range')
                if limit and (dt.datetime.now(dt.timezone.utc)-a.replace(tzinfo=a.tzinfo or dt.timezone.utc)).days > limit:
                    raise GatewayError('InsufficientCoverage', 'Intraday start outside current provider retention')
            except ValueError:
                raise GatewayError('InvalidParameters', 'Invalid ISO window') from None
        elif period not in (None,'1d','5d','1mo','3mo','6mo','1y','2y','5y','10y','ytd','max'):
            raise GatewayError('InvalidParameters', 'Unsupported period')
        if not start and interval not in ('1d','5d','1wk','1mo','3mo') and period not in ('1d','5d','1mo'):
            raise GatewayError('InvalidParameters', 'Intraday queries require an explicit bounded window')

    def _history(self, symbol, start=None, end=None, period=None, interval='1d',
                 price_basis='provider', prepost=False, repair=False, timeout=None):
        from yfinance_normalize import normalize_history
        self._window(start, end, period, interval)
        if price_basis not in ('provider','adjusted'):
            raise GatewayError('InvalidParameters', 'price_basis must be provider or adjusted')
        t = self._ticker(symbol)
        args = dict(interval=interval, auto_adjust=False, back_adjust=False, actions=True,
                    keepna=True, rounding=False, prepost=prepost, repair=False,
                    timeout=timeout or self.policy.timeout)
        args.update({'start': start, 'end': end} if start else {'period': period or '1mo'})
        frame = t.history(**args)
        # Avoid dict(metadata), which eagerly reads tradingPeriods.
        meta_obj = t.history_metadata
        meta = {k: meta_obj.get(k) for k in ('symbol','currency','exchangeTimezoneName','quoteType','regularMarketTime')}
        original_meta = dict(meta)
        original = None
        if repair:
            from yfinance_normalize import repair_diff
            original_frame = frame.copy()
            original = normalize_history(frame, meta, price_basis='provider', symbol=symbol, interval=interval)
            args['repair'] = True
            frame = t.history(**args)
            repaired_meta = t.history_metadata
            meta = {k: repaired_meta.get(k) for k in original_meta}
        data = normalize_history(frame, meta, price_basis=price_basis, symbol=symbol, interval=interval)
        # Normalizer returns an envelope; expose its bars directly under gateway data.
        normalized = data
        data = normalized.get('data', normalized)
        extra = {k: v for k, v in normalized.items() if k != 'data'}
        source_meta = self.policy.chart_metadata.get(symbol.upper(), {})
        extra.update({'currency': meta.get('currency'), 'frequency': interval,
                 'source_symbol': source_meta.get('symbol'), 'metadata': meta,
                 'adjustment': price_basis, 'source_timestamp_kind': 'bar_label_not_trade_time',
                 'warnings': normalized.get('warnings', []) + ['Bar closure and action coverage require independent calendar evidence'],
                 'last_bar_closed': None, 'gateway_envelope': {'provider_metadata': meta, 'wire_chart_metadata': source_meta, 'native_history': _safe(frame)}})
        bars = data.get('bars', [])
        if bars:
            extra['source_timestamp'] = bars[-1].get('timestamp_utc') or bars[-1].get('timestamp')
        else:
            extra['status'] = 'empty'
        if repair:
            extra['gateway_envelope']['repair_before'] = original
            extra['gateway_envelope']['repair_after'] = data
            extra['gateway_envelope']['repair_diff'] = repair_diff(original_frame, frame, original_meta, meta)
            extra['gateway_envelope']['repair_comparison'] = 'Compared OHLC/Adj Close/actions/currency; not inferred from Repaired flag'
        return data, extra

    def _batch(self, symbols, start=None, end=None, period=None, interval='1d',
               price_basis='provider', prepost=False, repair=False, timeout=None,
               concurrency=1, market=None):
        from yfinance_normalize import normalize_history_batch, repair_diff
        self._window(start, end, period, interval)
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= 10 or len(set(symbols)) != len(symbols):
            raise GatewayError('InvalidParameters', 'Batch needs 1 to 10 distinct symbols')
        if concurrency not in (1,2) or price_basis not in ('provider','adjusted'):
            raise GatewayError('InvalidParameters', 'Invalid concurrency or price basis')
        if len({s.upper().endswith('.HK') for s in symbols}) > 1:
            raise GatewayError('InvalidParameters', 'US and HK batches must be separate')
        for s in symbols:
            self._ticker(s)
        args = dict(tickers=symbols, interval=interval, group_by='ticker', multi_level_index=True,
                    auto_adjust=False, back_adjust=False, actions=True, prepost=prepost,
                    ignore_tz=False, progress=False, threads=False if concurrency == 1 else 2,
                    session=self._session, timeout=timeout or self.policy.timeout,
                    keepna=True, repair=False, rounding=False)
        args.update({'start': start, 'end': end} if start else {'period': period or '1mo'})
        frame = self._yf.download(**args)
        original_metadata = {key: dict(value) for key, value in self.policy.chart_metadata.items()}
        repaired = None
        if repair and not self.policy.blocked:
            repaired = self._yf.download(**{**args, 'repair': True})
        def split(value, symbol):
            columns = value.columns
            if getattr(columns, 'nlevels', 1) > 1:
                for level in range(columns.nlevels):
                    labels = columns.get_level_values(level)
                    matched = next((label for label in labels if str(label).upper() == symbol.upper()), None)
                    if matched is not None:
                        return value.xs(matched, axis=1, level=level)
                raise KeyError(symbol)
            if len(symbols) == 1:
                return value
            raise KeyError(symbol)
        results = {}
        for s in symbols:
            try:
                original = split(frame, s)
                part = split(repaired, s) if repaired is not None else original
                meta = self.policy.chart_metadata.get(s.upper(), {})
                result = normalize_history_batch({s: part}, {s: meta}, price_basis=price_basis, interval=interval)['symbols'][s]
                results[s] = result
                if repaired is not None:
                    results[s]['repair'] = {'before': _safe(original), 'after': _safe(part),
                                            'diff': repair_diff(original, part,
                                                                original_metadata.get(s.upper(), {}), meta)}
                if not meta.get('exchangeTimezoneName'):
                    if results[s]['status'] != 'empty':
                        results[s]['status'] = 'partial'
                    results[s]['warnings'].append('Exchange timezone not confirmed; batch date axis is not session evidence')
            except (KeyError, ValueError):
                results[s] = {'status': 'empty', 'reason': 'No price-bearing result; failure cause unconfirmed'}
        ok = sum(r['status'] in ('success', 'partial') for r in results.values())
        complete = all(r['status'] == 'success' for r in results.values())
        if self.policy.blocked and not ok:
            raise GatewayError(*self.policy.blocked)
        return {'symbols': results}, {'status': 'success' if complete else 'partial' if ok else 'empty'}

    def _quote(self, symbol, fields=None):
        t = self._ticker(symbol)
        allowed = {'last_price','currency','exchange','timezone','previous_close','open','day_high','day_low','last_volume','market_cap','shares'}
        fields = fields or ['last_price','currency']
        if not isinstance(fields, list) or not set(fields) <= allowed:
            raise GatewayError('InvalidParameters', 'Unregistered quote fields')
        data = {f: t.fast_info[f] for f in fields}
        meta = self.policy.chart_metadata.get(symbol.upper(), {})
        stamp = meta.get('regularMarketTime')
        return data, {'warnings': ['fast_info price is not guaranteed realtime; chart market time may not correspond to last_price'],
                      'source_timestamp': None, 'price_type': 'fast_info_last_price',
                      'currency': data.get('currency'), 'source_symbol': meta.get('symbol'),
                      'metadata': meta, 'chart_regular_market_time': stamp}

    def _profile(self, symbol):
        return self._ticker(symbol).get_info(), {'warnings': ['get_info symbol may be library-synthesized; not independent identity proof']}

    def _financials(self, symbol, frequency='yearly', statements=None):
        from yfinance_normalize import normalize_financials
        frequency = {'annual': 'yearly', 'ttm': 'trailing'}.get(frequency, frequency)
        if frequency not in ('yearly','quarterly','trailing'):
            raise GatewayError('InvalidParameters', 'frequency must be yearly, quarterly or trailing')
        t = self._ticker(symbol)
        selected = statements if statements is not None else ['income', 'cashflow'] + ([] if frequency == 'trailing' else ['balance_sheet'])
        methods = {'income': lambda: t.get_income_stmt(freq=frequency, pretty=False),
                   'cashflow': lambda: t.get_cash_flow(freq=frequency, pretty=False),
                   'balance_sheet': lambda: t.get_balance_sheet(freq=frequency, pretty=False)}
        if not isinstance(selected, list) or not selected or not set(selected) <= set(methods):
            raise GatewayError('InvalidParameters', 'Unregistered financial statements')
        if frequency == 'trailing' and 'balance_sheet' in selected:
            raise GatewayError('UnsupportedCapability', 'No TTM balance sheet is provided')
        raw, detail = self._selected(methods, selected, serialize=False)
        result = normalize_financials(raw, frequency, currency=None)
        return result, {**detail, 'warnings': detail.get('warnings', []) + ['Financial currency and disclosure dates not independently confirmed']}

    def _news(self, symbol=None, query=None, count=10, tab='news'):
        from yfinance_normalize import normalize_news
        if bool(symbol) == bool(query) or not isinstance(count, int) or not 1 <= count <= 100 or tab not in ('news','all','press releases'):
            raise GatewayError('InvalidParameters', 'Specify symbol OR query and bounded count/tab')
        if symbol:
            # Dedicated ticker avoids upstream parameter-insensitive news cache.
            self._ticker(symbol)
            t = self._yf.Ticker(symbol, session=self._session)
            items = t.get_news(count=count, tab=tab)
        else:
            items = self._yf.Search(query, news_count=count, session=self._session).news
        return normalize_news(items, kind='ticker' if symbol else 'search', query_symbol=symbol), {'warnings': ['Short current feed; no historical PIT coverage or full-text guarantee']}

    def _search(self, query, count=8, lookup_type=None):
        if not isinstance(query, str) or not 1 <= len(query) <= 200 or not 1 <= count <= 100:
            raise GatewayError('InvalidParameters', 'Invalid bounded query')
        if lookup_type is not None:
            if not hasattr(self._yf, 'Lookup'):
                raise GatewayError('UnsupportedCapability', 'This version has no Lookup')
            lookup = self._yf.Lookup(query, session=self._session, timeout=self.policy.timeout)
            methods = {'all': lambda: lookup.get_all(count=count),
                       'equity': lambda: lookup.get_stock(count=count),
                       'etf': lambda: lookup.get_etf(count=count)}
            if lookup_type not in methods:
                raise GatewayError('InvalidParameters', 'Lookup supports all/equity/etf only')
            return {'query': query, 'lookup_type': lookup_type, 'quotes': methods[lookup_type]()}
        result = self._yf.Search(query, max_results=count, news_count=0, session=self._session)
        return {'quotes': result.quotes}

    def _statistics(self, symbol, valuation=False):
        t = self._ticker(symbol)
        data = {'info': t.get_info()}
        if valuation:
            data['valuation_measures'] = t.get_valuation_measures()
        return data

    def _analysis(self, symbol, modules=None):
        t = self._ticker(symbol)
        methods = {'earnings_estimate': lambda: t.get_earnings_estimate(), 'revenue_estimate': lambda: t.get_revenue_estimate(),
                   'eps_trend': lambda: t.get_eps_trend(), 'eps_revisions': lambda: t.get_eps_revisions(),
                   'growth_estimates': lambda: t.get_growth_estimates(), 'target_prices': lambda: t.get_analyst_price_targets(),
                   'recommendations': lambda: t.get_recommendations(), 'upgrades_downgrades': lambda: t.get_upgrades_downgrades()}
        return self._selected(methods, modules or ['earnings_estimate','target_prices'])

    def _holders(self, symbol, modules=None):
        t = self._ticker(symbol)
        methods = {'major': lambda: t.get_major_holders(), 'institutional': lambda: t.get_institutional_holders(),
                   'mutualfund': lambda: t.get_mutualfund_holders(), 'insider_transactions': lambda: t.get_insider_transactions(),
                   'insider_purchases': lambda: t.get_insider_purchases(), 'insider_roster': lambda: t.get_insider_roster_holders()}
        return self._selected(methods, modules or ['major'])

    @staticmethod
    def _selected(methods, selected, serialize=True):
        if not isinstance(selected, list) or not selected or not set(selected) <= set(methods):
            raise GatewayError('InvalidParameters', 'Unregistered module')
        data, states, warnings = {}, {}, []
        for name in selected:
            try:
                data[name] = methods[name]()
                states[name] = 'success' if _has_data(data[name]) else 'empty'
            except Exception as exc:
                data[name] = None
                states[name] = 'error'
                warnings.append({'module': name, 'error_type': getattr(exc, 'code', type(exc).__name__),
                                 'message': 'Module unavailable; exception details redacted'})
        normalized = _safe(data) if serialize else data
        successes = sum(s == 'success' for s in states.values())
        if not successes and 'error' in states.values():
            raise GatewayError('DataUnavailable', 'All requested modules failed')
        return normalized, {'status': 'success' if successes == len(states) else 'partial' if successes else 'empty',
                            'module_status': states, 'warnings': warnings}

    def _options(self, symbol, expiration=None):
        t = self._ticker(symbol)
        dates = list(t.options)
        if expiration is None:
            return {'expirations': dates}
        if expiration not in dates:
            raise GatewayError('DataUnavailable', 'Requested expiry not listed by source')
        chain = t.option_chain(expiration)
        from yfinance_normalize import normalize_options
        return normalize_options(chain, expiration=expiration, underlying=chain.underlying)

    def _earnings(self, symbol, modules=None, limit=12):
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise GatewayError('InvalidParameters', 'Invalid earnings count')
        t = self._ticker(symbol)
        return self._selected({'calendar': lambda: t.calendar,
                               'dates': lambda: t.get_earnings_dates(limit=limit),
                               'history': lambda: t.get_earnings_history()}, modules or ['calendar'])

    def _funds(self, symbol, modules=None):
        f = self._ticker(symbol).funds_data
        return self._selected({'description': lambda: f.description, 'overview': lambda: f.fund_overview,
                               'operations': lambda: f.fund_operations, 'asset_classes': lambda: f.asset_classes,
                               'top_holdings': lambda: f.top_holdings, 'sector_weightings': lambda: f.sector_weightings},
                              modules or ['description','overview'])

    def _screen(self, query, query_type='equity', size=25, max_pages=1, max_results=250, offset=0):
        kind = query_type
        if kind not in ('equity','etf') or not 1 <= size <= 250 or not 1 <= max_pages <= 10 or not 1 <= max_results <= 1000 or offset < 0:
            raise GatewayError('InvalidParameters', 'Invalid screen bounds')
        query_class = self._yf.EquityQuery if kind == 'equity' else self._yf.ETFQuery
        def build(node, depth=0):
            if depth > 8 or not isinstance(node, dict) or set(node) != {'operator','operands'}:
                raise GatewayError('InvalidParameters', 'Invalid structured query')
            op, values = node['operator'], node['operands']
            if op not in ('AND','OR','EQ','IS-IN','BTWN','GT','GTE','LT','LTE') or not isinstance(values, list) or len(values) > 30:
                raise GatewayError('InvalidParameters', 'Unregistered query operator')
            try:
                return query_class(op, [build(v, depth+1) if isinstance(v, dict) else v for v in values])
            except GatewayError:
                raise
            except (ValueError, TypeError):
                raise GatewayError('InvalidParameters', 'Unsupported query field or operands') from None
        q = build(query)
        pages, collected = [], 0
        for _ in range(max_pages):
            n = min(size, max_results-collected)
            if n <= 0:
                break
            page = self._yf.screen(q, offset=offset, size=n, session=self._session)
            pages.append({'offset': offset, 'result': page})
            got = len(page.get('quotes', []))
            collected += got
            offset += got
            if got < n:
                break
        return {'pages': pages, 'count': collected, 'historical_universe': False}
