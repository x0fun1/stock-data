"""Offline tests: actual bounded Session.request path, no Yahoo or installation."""
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'stock-data' / 'scripts'))
from yfinance_provider import GatewayError, TransportPolicy, YahooProvider, bounded_session_class


class FakeBase:
    def __init__(self, **kwargs):
        self.constructor_options = kwargs
        self.calls = []
        self.queue = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        status, headers, chunks = self.queue.pop(0)
        for chunk in chunks:
            if kwargs['content_callback'](chunk) != len(chunk):
                raise RuntimeError('callback aborted')
        return SimpleNamespace(status_code=status, headers=headers, content=b'')

    # Mirrors base Session.get/post: dispatch through overridden request.
    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)


class TransportTests(unittest.TestCase):
    def session(self, **kwargs):
        return bounded_session_class(FakeBase)(TransportPolicy(**kwargs))

    def test_untrusted_hosts_are_blocked_before_transport(self):
        for url in ('http://query1.finance.yahoo.com/a', 'https://evil.com',
                    'https://user:pass@query1.finance.yahoo.com/a',
                    'https://query1.finance.yahoo.com:444/a',
                    'https://query1.finance.yahoo.com.evil.com/a'):
            s = self.session()
            with self.assertRaises(GatewayError):
                s.get(url)
            self.assertEqual(s.calls, [])

    def test_body_restored_and_retry_disabled(self):
        s = self.session()
        s.queue = [(200, {}, [b'{"ok":', b'true}'])]
        r = s.get('https://query1.finance.yahoo.com/test', timeout=50)
        self.assertEqual(json.loads(r.content), {'ok': True})
        self.assertFalse(s.calls[0][2]['allow_redirects'])
        self.assertEqual(s.constructor_options['retry'], 0)
        self.assertNotIn('retry', s.calls[0][2])
        self.assertLessEqual(s.calls[0][2]['timeout'], 15)

    def test_oversize_stops_during_callback_and_opens_circuit(self):
        s = self.session(max_bytes=4)
        s.queue = [(200, {}, [b'abcd', b'e', b'must not be buffered'])]
        with self.assertRaises(GatewayError) as cm:
            s.get('https://query1.finance.yahoo.com/test')
        self.assertEqual(cm.exception.code, 'InvalidResponse')
        with self.assertRaises(GatewayError):
            s.get('https://query1.finance.yahoo.com/another')
        self.assertEqual(len(s.calls), 1)

    def test_429_blocks_subsequent_library_requests(self):
        s = self.session()
        s.queue = [(429, {}, [b'limited'])]
        with self.assertRaises(GatewayError) as cm:
            s.post('https://finance.yahoo.com/xhr/ncp', data='x')
        self.assertEqual(cm.exception.code, 'RateLimited')
        with self.assertRaises(GatewayError):
            s.get('https://query2.finance.yahoo.com/test')
        self.assertEqual(len(s.calls), 1)

    def test_403_no_retry(self):
        s = self.session()
        s.queue = [(403, {}, [])]
        with self.assertRaises(GatewayError):
            s.get('https://query1.finance.yahoo.com/test')
        self.assertEqual(s.policy.blocked[0], 'AccessDenied')

    def test_401_only_one_library_recovery(self):
        s = self.session()
        s.queue = [(401, {}, []), (401, {}, [])]
        self.assertEqual(s.get('https://query1.finance.yahoo.com/test').status_code, 401)
        with self.assertRaises(GatewayError):
            s.get('https://query1.finance.yahoo.com/test')
        with self.assertRaises(GatewayError):
            s.get('https://query1.finance.yahoo.com/test')
        self.assertEqual(len(s.calls), 2)

    def test_redirect_rejected_before_destination_request(self):
        s = self.session()
        s.queue = [(302, {'Location': 'https://evil.com/'}, [])]
        with self.assertRaises(GatewayError):
            s.get('https://query1.finance.yahoo.com/test')
        self.assertEqual(len(s.calls), 1)
        s = self.session()
        s.queue = [(302, {'Location': 'https://query2.finance.yahoo.com/test'}, [])]
        with self.assertRaises(GatewayError):
            s.get('https://query1.finance.yahoo.com/test')
        self.assertEqual(len(s.calls), 1)

    def test_registered_handshake_and_three_hop_limit(self):
        s = self.session()
        s.queue = [(302, {'Location': 'https://guce.yahoo.com/consent'}, []), (200, {}, [b'ok'])]
        self.assertEqual(s.get('https://fc.yahoo.com/').content, b'ok')
        s = self.session()
        s.queue = [(302, {'Location': '/next'}, [])] * 4
        with self.assertRaises(GatewayError):
            s.get('https://query1.finance.yahoo.com/start')
        self.assertEqual(len(s.calls), 4)

    def test_budget_covers_redirects_and_post(self):
        s = self.session(max_requests=1)
        s.queue = [(302, {'Location': '/next'}, [])]
        with self.assertRaises(GatewayError) as cm:
            s.post('https://query1.finance.yahoo.com/a', data='x')
        self.assertEqual(cm.exception.code, 'BudgetExceeded')
        self.assertEqual(len(s.calls), 1)

    def test_deadline_pretransport_and_body(self):
        now = [0]
        s = self.session(clock=lambda: now[0], deadline=2)
        now[0] = 3
        with self.assertRaises(GatewayError):
            s.get('https://query1.finance.yahoo.com/a')
        self.assertEqual(s.calls, [])
        s = self.session(clock=lambda: now[0], deadline=2)
        callback = s.policy.clock
        ticks = iter([3, 6])
        s.policy.clock = lambda: next(ticks)
        s.queue = [(200, {}, [b'body'])]
        with self.assertRaises(GatewayError) as cm:
            s.get('https://query1.finance.yahoo.com/a')
        self.assertEqual(cm.exception.code, 'BudgetExceeded')


class ProviderTests(unittest.TestCase):
    def test_import_and_construction_are_lazy(self):
        with patch('yfinance_provider.importlib.import_module', side_effect=AssertionError('import called')):
            YahooProvider('/path/not/created')

    def test_missing_dependency_is_explicit(self):
        with patch('yfinance_provider.importlib.import_module', side_effect=ImportError()):
            with self.assertRaises(GatewayError) as cm:
                YahooProvider('/tmp/unused').call('yahoo_quote', symbol='AAPL')
            self.assertEqual(cm.exception.code, 'DependencyUnavailable')

    def test_unknown_capability_never_initializes(self):
        p = YahooProvider()
        with patch.object(p, '_initialize', side_effect=AssertionError('called')):
            with self.assertRaises(GatewayError) as cm:
                p.call('__import__', symbol='AAPL')
            self.assertEqual(cm.exception.code, 'UnsupportedCapability')

    def test_window_validation(self):
        for params in [('2024-01-01', '2023-01-01', None, '1d'),
                       ('2024-01-01', '2024-02-01', '1mo', '1d'),
                       ('2024-01-01', None, None, '1d'),
                       (None, None, 'max', '1m')]:
            with self.assertRaises(GatewayError):
                YahooProvider._window(*params)

    def test_profile_reuse_and_archive_redaction(self):
        calls = []
        class Ticker:
            def __init__(self, symbol, session):
                calls.append(symbol)
            def get_info(self):
                return {'symbol': 'AAPL', 'cookie': 'SECRET', 'nested': {'crumb': 'SECRET', 'value': 0}}
        yf = SimpleNamespace(Ticker=Ticker, __version__='1.7.0',
                             set_tz_cache_location=lambda p: None,
                             config=SimpleNamespace(debug=SimpleNamespace(), network=SimpleNamespace()))
        session = bounded_session_class(FakeBase)(TransportPolicy())
        with tempfile.TemporaryDirectory() as tmp:
            p = YahooProvider(tmp, yf_module=yf, session=session)
            a = p.call('yahoo_profile', symbol='AAPL')
            b = p.call('yahoo_profile', symbol='AAPL')
        self.assertEqual(calls, ['AAPL'])
        self.assertEqual(a['data']['nested']['value'], 0)
        self.assertNotIn('SECRET', json.dumps(a))
        self.assertEqual(yf.config.network.retries, 0)


if __name__ == '__main__':
    unittest.main()
