"""Backend-signature regressions; no yfinance or curl installation/network required."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock-data/scripts"))
spec = importlib.util.spec_from_file_location("yahoo_backend_contract", ROOT / "stock-data/scripts/yfinance_provider.py")
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)


class SignatureFaithfulSession:
    """The curl Session.request boundary has retry on constructor, not request."""
    def __init__(self, *, retry=0, **kwargs):
        self.retry = retry

    def request(self, method, url, *, timeout=None, allow_redirects=None,
                content_callback=None, params=None, headers=None):
        content_callback(b'{"ok":true}')
        return SimpleNamespace(status_code=200, headers={}, content=b'')


def test_transport_does_not_pass_constructor_retry_to_request():
    policy = provider.TransportPolicy()
    session = provider.bounded_session_class(SignatureFaithfulSession)(policy)
    response = session.request('GET', 'https://query1.finance.yahoo.com/v8/finance/chart/AAPL')
    assert response.content == b'{"ok":true}'
    assert policy.count == 1


class ColumnLabels:
    nlevels = 2
    def __init__(self, symbols):
        self.symbols = symbols
    def get_level_values(self, level):
        return self.symbols if level == 0 else ['Open', 'Close']


class BatchFrame:
    def __init__(self, parts):
        self.parts = parts
        self.columns = ColumnLabels(list(parts))
        self.empty = not parts
    def xs(self, label, *, axis, level):
        return self.parts[label]


class OfflineTicker:
    fast_info = {'lastPrice': 0, 'currency': 'USD'}
    options = ['2026-10-16']
    calendar = {'Earnings Date': ['2026-10-20']}
    funds_data = SimpleNamespace(description='ETF', fund_overview={}, fund_operations={'expenseRatio': 0},
                               asset_classes={}, top_holdings=[], sector_weightings={})
    def get_info(self):
        return {'financialCurrency': 'USD', 'quoteType': 'EQUITY'}
    def get_income_stmt(self, **kwargs):
        class FinancialFrame:
            empty = False
            columns = ['2026-06-30']
            def iterrows(self):
                return iter([('TotalRevenue', {'2026-06-30': 0})])
        return FinancialFrame()
    def get_cash_flow(self, **kwargs):
        return []
    def get_balance_sheet(self, **kwargs):
        return []
    def get_earnings_estimate(self):
        return {'0q': {'avg': 0}}
    def get_major_holders(self):
        return {}
    def option_chain(self, expiration):
        return SimpleNamespace(calls=[{'contractSymbol': 'TEST', 'lastTradeDate': 1790000000,
                                       'bid': 0, 'impliedVolatility': .3}], puts=[], underlying={'symbol': 'TEST'})


def configured_provider():
    value = provider.YahooProvider(cache_dir='/tmp/unused-offline-yahoo-cache')
    value._ready = True
    value._session = SimpleNamespace(close=lambda: None)
    value._yf = SimpleNamespace(__version__='1.7.0', Ticker=lambda symbol, session=None: OfflineTicker())
    return value


def test_registered_p1_module_calls_do_not_eagerly_require_unrequested_methods():
    value = configured_provider()
    assert value.call('yahoo_analysis', symbol='TEST', modules=['earnings_estimate'])['status'] == 'success'
    with patch.object(provider, '_legacy_yahoo_fallback', return_value={
            'status': 'empty', 'data': None, 'fallback_attempted': True, 'fallback_used': True,
            'fallback_route': 'institutional_holders'}) as fallback:
        result = value.call('yahoo_holders', symbol='TEST', modules=['major'])
    assert result['status'] == 'empty'
    fallback.assert_called_once()
    assert fallback.call_args.args[0] == 'yahoo_holders'
    assert fallback.call_args.args[2]['category'] == 'DataUnavailable'
    assert value.call('yahoo_earnings', symbol='TEST', modules=['calendar'])['status'] == 'success'
    result = value.call('yahoo_funds', symbol='TEST', modules=['operations'])
    assert result['data']['operations']['expenseRatio'] == 0
    value.close()


def test_financials_select_statements_and_preserve_partial_empty_tables():
    value = configured_provider()
    result = value.call('yahoo_financials', symbol='TEST', frequency='yearly', statements=['income', 'cashflow'])
    assert result['status'] == 'partial'
    assert result['data']['statements']['income'] == 'success'
    assert result['data']['records'][0]['value'] == 0
    assert result['module_status']['cashflow'] == 'empty'


def test_options_use_normalizer_not_raw_dataframe_and_keep_utc():
    value = configured_provider()
    result = value.call('yahoo_options', symbol='TEST', expiration='2026-10-16')
    assert result['data']['calls'][0]['lastTradeDate'].endswith(('Z', '+00:00'))
    assert result['data']['calls'][0]['bid'] == 0


def test_batch_multiindex_has_per_symbol_windows_and_unknown_empty_axis_evidence():
    value = configured_provider()
    bars = [{'date': '2026-10-07T00:00:00-04:00', 'Open': 10, 'High': 11, 'Low': 9,
             'Close': 10, 'Adj Close': 10, 'Volume': 0},
            {'date': '2026-10-08T00:00:00-04:00', 'Open': None, 'Close': None, 'Adj Close': None}]
    value._yf.download = lambda **kwargs: BatchFrame({'TEST': bars, 'EMPTY': []})
    value.policy.chart_metadata['TEST'] = {'symbol': 'TEST', 'exchangeTimezoneName': 'America/New_York', 'currency': 'USD'}
    result = value.call('yahoo_history_batch', symbols=['TEST', 'EMPTY'], start='2026-10-07', end='2026-10-09')
    assert result['status'] == 'partial'
    test = result['data']['symbols']['TEST']
    assert test['data']['history_window']['start'] == '2026-10-07'
    assert test['source_timestamp'] is not None
    assert test['data']['bars'][0]['volume'] == 0
    assert test['alignment']['null_origin'] == 'source_or_batch_alignment_unconfirmed'
    assert test['data']['bars'][1]['close'] is None
    assert result['data']['symbols']['EMPTY']['status'] == 'empty'


def test_gateway_public_search_count_and_lookup_signatures():
    value = configured_provider()
    calls = []
    def search(query, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(quotes=[{'symbol': 'TEST'}])
    value._yf.Search = search
    assert value.call('yahoo_search', query='TEST', count=3)['status'] == 'success'
    assert calls[0]['max_results'] == 3
    lookups = []
    value._yf.Lookup = lambda *args, **kwargs: lookups.append((args, kwargs)) or SimpleNamespace(get_etf=lambda count: [{'symbol': 'SPY'}])
    assert value.call('yahoo_search', query='SPY', count=2, lookup_type='etf')['data']['lookup_type'] == 'etf'
    assert len(lookups) == 1
