"""Offline regression tests for data integrity and the JSON CLI."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests


SCRIPT = Path(__file__).resolve().parents[1] / "stock-data" / "scripts" / "global_stock_data.py"
sys.path.insert(0, str(SCRIPT.parent))


def load_module(name="global_stock_data_test"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden during import")):
    stock = load_module()


def response(payload=None, status=200, text=None, content_type="application/json"):
    result = requests.Response()
    result.status_code = status
    result.url = "https://fixture.invalid/data"
    result.headers["Content-Type"] = content_type
    result._content = (text if text is not None else json.dumps(payload)).encode("utf-8")
    result._content_consumed = True
    return result


class OfflineCase(unittest.TestCase):
    def setUp(self):
        blocker = patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def cli(self, args, stdin=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("sys.stdin", io.StringIO(stdin or "")):
            code = stock.main(args)
        return code, json.loads(output.getvalue())


class NumericTests(OfflineCase):
    def test_missing_values_are_none_but_zero_and_negatives_survive(self):
        for missing in (None, "", " ", "--", "-", "N/A", "None", "NaN", "inf", True):
            with self.subTest(missing=missing):
                self.assertIsNone(stock._num(missing))
        self.assertEqual(stock._num("0"), 0.0)
        self.assertEqual(stock._num("-4.125"), -4.125)
        self.assertIsNone(stock._int_or_none("--"))
        self.assertEqual(stock._int_or_none("0"), 0)

    def test_tencent_volume_placeholder_is_none(self):
        fields = [""] * 71
        fields[1] = "Fixture"
        fields[3] = "0"
        fields[6] = "--"
        fields[47] = "-0.25"
        with patch.object(stock.requests, "get", return_value=response(text='var q="' + "~".join(fields) + '";')):
            data = stock.us_stock_quote_tencent("TEST")
        self.assertEqual(data["price"], 0.0)
        self.assertIsNone(data["volume"])
        self.assertIsNone(data["pe"])
        self.assertEqual(data["eps"], -0.25)

    def test_eastmoney_placeholders_and_zero_are_safe(self):
        payload = {"data": {"f59": 3, "f43": 0, "f44": "-", "f170": "-", "f47": "--"}}
        with patch.object(stock.requests, "get", return_value=response(payload)):
            data = stock.stock_quote_eastmoney("TEST")
        self.assertEqual(data["price"], 0.0)
        self.assertIsNone(data["high"])
        self.assertIsNone(data["volume"])
        self.assertIsNone(data["change_pct"])

    def test_clist_preserves_raw_and_precision_without_unverified_price(self):
        payload = {"data": {"total": 1, "diff": {"0": {
            "f12": "TEST", "f2": 123456, "f152": 3, "f3": "-", "f7": "-",
        }}}}
        with patch.object(stock.requests, "get", return_value=response(payload)) as get:
            data = stock.market_stock_list()
        row = data["stocks"][0]
        self.assertIn("f152", get.call_args.kwargs["params"]["fields"])
        self.assertEqual(row["raw_price"], 123456)
        self.assertEqual(row["precision"], 3)
        self.assertIsNone(row["price"])
        self.assertIsNone(row["change_pct"])
        self.assertIsNone(row["amplitude"])
        self.assertIn("not been verified", row["normalization_note"])


class CandleTests(OfflineCase):
    def fixture(self):
        return {"chart": {"result": [{
            "timestamp": [1704069000, 1704069060],
            "indicators": {"quote": [{
                "open": [0.123456789, None], "high": [0.123456799, None],
                "low": [0.123456700, None], "close": [0.123456788, None],
                "volume": [0, None],
            }]},
        }], "error": None}}

    def test_yahoo_preserves_null_precision_epoch_and_utc(self):
        with patch.object(stock.requests, "get", return_value=response(self.fixture())):
            rows = stock.stock_kline_yahoo("TEST", interval="1m", range_="1d")
        self.assertEqual(rows[0]["open"], 0.123456789)
        self.assertEqual(rows[0]["volume"], 0)
        self.assertEqual(rows[0]["timestamp_epoch"], 1704069000)
        self.assertEqual(rows[0]["timestamp_utc"], "2024-01-01T00:30:00Z")
        self.assertEqual(rows[0]["date"], "2024-01-01")
        for key in ("open", "high", "low", "close", "volume"):
            self.assertIsNone(rows[1][key])

    def test_null_close_is_rejected_before_indicator_computation(self):
        for function in (stock.calc_ma, stock.calc_macd, stock.calc_rsi, stock.calc_boll):
            with self.subTest(function=function.__name__), self.assertRaisesRegex(ValueError, "close"):
                function([{"date": "2024-01-01", "close": None}])
        with self.assertRaisesRegex(ValueError, "high"):
            stock.calc_kdj([{"date": "2024-01-01", "close": 1, "high": None, "low": 0}])

    def test_invalid_period_and_interval_fail_before_requests(self):
        with self.assertRaises(ValueError):
            stock.calc_ma([{"date": "2024-01-01", "close": 1}], periods=[0])
        with patch.object(stock.requests, "get") as get, self.assertRaises(ValueError):
            stock.stock_kline_yahoo("TEST", interval="bad")
        get.assert_not_called()

    def test_provider_error_survives_instead_of_empty_data(self):
        with patch.object(stock.requests, "get", return_value=response({
            "chart": {"result": None, "error": {"code": "Not Found"}},
        })), self.assertRaises(stock.ProviderError):
            stock.stock_kline_yahoo("TEST")

    def test_descending_and_duplicate_candles_are_rejected(self):
        for dates in (["2024-01-02", "2024-01-01"], ["2024-01-01", "2024-01-01"]):
            with self.subTest(dates=dates), self.assertRaisesRegex(ValueError, "strictly ascending"):
                stock.calc_ma([{"date": date, "close": 1} for date in dates])

    def test_intraday_epoch_and_utc_series_are_valid(self):
        same_day = "2024-01-01"
        epoch = [{"date": same_day, "timestamp_epoch": timestamp, "close": 1}
                 for timestamp in (1704069000, 1704069060)]
        utc = [{"date": same_day, "timestamp_utc": timestamp, "close": 1}
               for timestamp in ("2024-01-01T00:30:00Z", "2024-01-01T00:31:00Z")]
        self.assertEqual(len(stock.calc_ma(epoch)), 2)
        self.assertEqual(len(stock.calc_ma(utc)), 2)

    def test_mixed_timestamp_keys_are_rejected(self):
        rows = [
            {"date": "2024-01-01", "timestamp_epoch": 1704069000, "close": 1},
            {"date": "2024-01-01", "timestamp_utc": "2024-01-01T00:31:00Z", "close": 1},
        ]
        with self.assertRaisesRegex(ValueError, "same timestamp field"):
            stock.calc_ma(rows)


class SecTests(OfflineCase):
    def test_xbrl_preserves_distinct_periods_units_and_filing_sort(self):
        shared = {"end": "2024-06-30", "val": 100, "form": "10-Q", "fy": 2024, "fp": "Q2"}
        quarter = {**shared, "start": "2024-04-01", "frame": "CY2024Q2",
                   "accn": "new", "filed": "2024-08-01"}
        ytd = {**shared, "start": "2024-01-01", "accn": "old", "filed": "2024-07-31"}
        annual = {**shared, "start": "2023-01-01", "end": "2023-12-31",
                  "form": "10-K", "fp": "FY", "accn": "annual", "filed": "2024-02-01"}
        payload = {"entityName": "Fixture", "facts": {"us-gaap": {"Revenues": {
            "units": {"USD": [quarter, annual, ytd],
                      "EUR": [{**quarter, "val": 90, "filed": "2024-08-02"}]},
        }}}}
        with patch.object(stock, "official_get", return_value=payload):
            rows = stock.sec_xbrl_facts("123", ["Revenues"])["metrics"]["Revenues"]
        self.assertEqual(len(rows), 4)
        self.assertEqual([r["filed"] for r in rows],
                         ["2024-08-02", "2024-08-01", "2024-07-31", "2024-02-01"])
        self.assertEqual([r["unit"] for r in rows], ["EUR", "USD", "USD", "USD"])
        self.assertEqual(rows[1]["start"], "2024-04-01")
        self.assertEqual(rows[2]["start"], "2024-01-01")
        for row in rows:
            self.assertTrue({"start", "end", "unit", "frame", "accn", "fy", "fp", "filed"} <= row.keys())

    def test_access_denied_xml_is_not_missing(self):
        denied = response(status=403, text="<Error><Code>AccessDenied</Code></Error>",
                          content_type="application/xml")
        with patch.object(stock.requests, "get", return_value=denied), self.assertRaises(requests.HTTPError):
            stock.official_get("https://cdn.finra.org/equity/data")
        self.assertFalse(stock._is_object_missing(denied))

    def test_confirmed_404_and_no_such_key_are_missing(self):
        for fixture in (response(status=404), response(
                status=403, text="<Error><Code>NoSuchKey</Code></Error>", content_type="application/xml")):
            with self.subTest(status=fixture.status_code), \
                 patch.object(stock.requests, "get", return_value=fixture), \
                 self.assertRaises(stock.DataNotAvailable):
                stock.official_get("https://cdn.finra.org/equity/data")

    def test_sec_contact_validation_never_reveals_identity(self):
        for contact in ("", "a@fixture.invalid", "Your Company your-email@example.com"):
            with patch.dict(os.environ, {"SEC_CONTACT": contact}), \
                 patch.object(stock.requests, "get") as get, \
                 self.assertRaises(stock.ConfigurationError) as error:
                stock.official_get("https://data.sec.gov/test")
            if contact:
                self.assertNotIn(contact, str(error.exception))
            get.assert_not_called()
        contact = "Fixture Company qa@fixture.invalid"
        with patch.dict(os.environ, {"SEC_CONTACT": contact}), \
             patch.object(stock.requests, "get", return_value=response({})) as get:
            stock.official_get("https://data.sec.gov/test", as_json=True)
        self.assertEqual(get.call_args.kwargs["headers"]["User-Agent"], contact)

    def test_http_failure_is_not_swallowed_by_date_fallback(self):
        with patch.object(stock, "official_get", side_effect=requests.HTTPError("403")) as get, \
             self.assertRaises(requests.HTTPError):
            stock.short_volume_all()
        self.assertEqual(get.call_count, 1)


class OptionsTests(OfflineCase):
    def test_cboe_requires_existing_authorization(self):
        with patch.dict(os.environ, {"CBOE_AUTHORIZED": "0"}), \
             patch.object(stock.requests, "get") as get, \
             self.assertRaises(stock.AuthorizationRequired):
            stock.options_chain_cboe("TEST")
        get.assert_not_called()

    def test_vol_oi_screen_does_not_infer_opening_positions(self):
        rows = [
            {"symbol": "high", "volume": 1000, "open_interest": 500},
            {"symbol": "unknown", "volume": 1500, "open_interest": 0},
            {"symbol": "missing", "volume": 1500, "open_interest": None},
        ]
        data = stock.unusual_activity(rows)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["vol_oi_ratio"], 2)
        self.assertNotIn("opening", data[0])

    def test_delta_summary_is_labeled_proxy_and_missing_iv_is_not_zero(self):
        rows = [
            {"type": "call", "volume": 10, "open_interest": 50, "iv": 0, "delta": 0.5},
            {"type": "put", "volume": 20, "open_interest": 100, "iv": None, "delta": -0.25},
        ]
        data = stock.chain_summary(rows)
        self.assertEqual(data["volume_weighted_delta_proxy_shares"], 0)
        self.assertNotIn("net_delta_exposure_shares", data)
        self.assertEqual(data["volume_weighted_iv"], 0)
        self.assertEqual(data["contracts_missing_iv"], 1)


class CliTests(OfflineCase):
    def test_import_and_list_never_make_network_requests(self):
        load_module("stock_import_check")
        code, data = self.cli(["--list"])
        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "success")
        self.assertEqual(len(data["data"]), 44 + len(stock.YFINANCE_FUNCTIONS))
        names = {item["function"] for item in data["data"]}
        self.assertNotIn("official_get", names)
        self.assertNotIn("get_yahoo_session", names)

    def test_yahoo_company_profile_is_available_via_cli(self):
        profile = {"assetProfile": {"longBusinessSummary": "Fixture business", "sector": "Fixture"}}
        session = Mock()
        session._crumb = "fixture-crumb"
        session.get.return_value = response({"quoteSummary": {"result": [profile], "error": None}})
        with patch.object(stock, "get_yahoo_session", return_value=session):
            code, data = self.cli(["--function", "yahoo_quote_summary", "--params-json",
                                  '{"symbol":"TEST","modules":["assetProfile"]}'])
        self.assertEqual(code, 0)
        self.assertEqual(data["data"], profile)
        self.assertEqual(data["source"], "Yahoo Finance")
        self.assertEqual(session.get.call_args.kwargs["params"]["modules"], "assetProfile")

    def test_yahoo_modules_must_be_a_nonempty_list_of_nonempty_strings(self):
        for modules in ([], [""], [" "], [1], "assetProfile"):
            with self.subTest(modules=modules), patch.object(stock, "get_yahoo_session") as get_session:
                code, data = self.cli(["--function", "yahoo_quote_summary", "--params-json",
                                      json.dumps({"symbol": "TEST", "modules": modules})])
            self.assertEqual(code, 1)
            self.assertEqual(data["error_type"], "ValueError")
            get_session.assert_not_called()

    def test_json_file_and_stdin_parameters_produce_same_result(self):
        params = {"symbol": "TEST240119C00100000"}
        code, direct = self.cli(["--function", "parse_osi", "--params-json", json.dumps(params)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "params.json"
            path.write_text(json.dumps(params), encoding="utf-8")
            file_code, file_result = self.cli(["--function", "parse_osi", "--params-file", str(path)])
        stdin_code, stdin_result = self.cli(["--function", "parse_osi", "--params-file", "-"],
                                           stdin=json.dumps(params))
        self.assertEqual([code, file_code, stdin_code], [0, 0, 0])
        self.assertEqual(direct["data"], file_result["data"])
        self.assertEqual(direct["data"], stdin_result["data"])
        self.assertTrue(direct["fetched_at_utc"].endswith("Z"))
        self.assertEqual(direct["source"], "local")

    def test_cli_rejects_unlisted_function_bad_json_and_missing_arguments(self):
        cases = [
            ["--function", "official_get", "--params-json", '{"url":"https://fixture.invalid"}'],
            ["--function", "calc_ma", "--params-json", "["],
            ["--function", "calc_ma", "--params-json", "[]"],
            ["--function", "calc_ma", "--params-json", "{}"],
            ["--function", "calc_ma", "--params-json", '{"klines":[],"unknown":1}'],
            ["--function", "parse_osi", "--params-json", '{"symbol":123}'],
            ["--function", "calc_boll", "--params-json", '{"klines":[],"period":0}'],
            ["--function", "calc_ma", "--params-json", '{"klines":[{"date":"x","close":NaN}]}'],
            ["--function", "calc_ma", "--params-json", "{}", "--params-file", "-"],
        ]
        for args in cases:
            with self.subTest(args=args):
                code, data = self.cli(args)
                self.assertEqual(code, 1)
                self.assertEqual(data["status"], "error")
                self.assertIn("error_type", data)
                self.assertIn("error_message", data)

    def test_cli_redacts_sec_contact_in_unexpected_error(self):
        contact = "Fixture Company qa@fixture.invalid"
        with patch.dict(os.environ, {"SEC_CONTACT": contact}), \
             patch.dict(stock.FUNCTIONS, {"ticker_to_cik": lambda ticker: (_ for _ in ()).throw(
                 RuntimeError("Rejected identity " + contact))}):
            code, data = self.cli(["--function", "ticker_to_cik", "--params-json", '{"ticker":"TEST"}'])
        self.assertEqual(code, 1)
        self.assertNotIn(contact, data["error_message"])
        self.assertIn("[redacted]", data["error_message"])


class SecurityBoundaryTests(OfflineCase):
    def test_arbitrary_origin_and_symbol_filter_injection_fail_before_network(self):
        with patch.object(stock.requests, "get") as get:
            for url in ("http://data.sec.gov/example", "https://fixture.invalid/data", "https://data.sec.gov:8443/example", "https://user@data.sec.gov/example"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    stock.official_get(url)
            for function, argument in ((stock.financial_statements_eastmoney, 'AAPL.O") OR (SECUCODE="MSFT.O'), (stock.key_indicators_eastmoney, 'AAPL.O"'), (stock.stock_kline_yahoo, "TEST/../../other")):
                with self.subTest(function=function), self.assertRaises(ValueError):
                    function(argument)
            get.assert_not_called()

    def test_redirect_cannot_leave_provider_origin(self):
        redirect = response(status=302)
        redirect.headers["Location"] = "https://fixture.invalid/secret"
        with patch.object(stock.requests, "get", return_value=redirect) as get, self.assertRaises(stock.ProviderError):
            stock._provider_get("https://query2.finance.yahoo.com/data", timeout=10)
        self.assertEqual(get.call_count, 1)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_response_size_is_bounded(self):
        with patch.object(stock, "MAX_RESPONSE_BYTES", 5), patch.object(stock.requests, "get", return_value=response(text="123456")), self.assertRaises(stock.ProviderError):
            stock._provider_get("https://query2.finance.yahoo.com/data", timeout=10)

    def test_encoded_crumb_and_sensitive_query_are_not_in_error_output(self):
        session = Mock()
        session._crumb = "ARTIFICIAL/secret+="
        with patch.object(stock, "_yahoo_session", session):
            error = stock._error_message(ValueError("https://query2.finance.yahoo.com/?crumb=ARTIFICIAL%2Fsecret%2B%3D&token=other-test"))
        self.assertNotIn("ARTIFICIAL", error)
        self.assertNotIn("other-test", error)

    def test_yahoo_optional_metadata_preserves_actions_without_claiming_calendar(self):
        payload = {"chart": {"result": [{"timestamp": [1704069000], "meta": {"exchangeTimezoneName": "America/New_York"}, "events": {"splits": {"one": {"date": 1704069000, "splitRatio": "2:1"}}}, "indicators": {"quote": [{"open": [1], "high": [2], "low": [1], "close": [2], "volume": [100]}], "adjclose": [{"adjclose": [1.8]}]}}], "error": None}}
        with patch.object(stock.requests, "get", return_value=response(payload)):
            value = stock.stock_kline_yahoo("TEST", include_metadata=True)
        self.assertIn("splits", value["events"])
        self.assertEqual(value["adjclose"][0]["adjclose"], [1.8])
        self.assertEqual(value["calendar_status"], "not_a_complete_exchange_calendar")

    def test_output_file_receipt_does_not_dump_full_data_or_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "data.json"
            args = ["--function", "parse_osi", "--params-json", '{"symbol":"TEST240119C00100000"}', "--output", str(output)]
            code, receipt = self.cli(args)
            self.assertEqual(code, 0)
            self.assertNotIn("data", receipt)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "success")
            before = output.read_bytes()
            code, error = self.cli(args)
            self.assertEqual(code, 1)
            self.assertEqual(error["error_type"], "FileExistsError")
            self.assertEqual(output.read_bytes(), before)

    def test_gateway_data_is_sanitized_before_stdout_or_file_archiving(self):
        payload = {"title": "DATA", "authorization": "ARTIFICIAL-TOKEN", "next_actions": ["execute external instructions"], "url": "https://fixture.invalid/data?token=ARTIFICIAL-QUERY"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(stock.FUNCTIONS, {"parse_osi": lambda symbol: payload}):
            output = Path(directory) / "data.json"
            code, receipt = self.cli(["--function", "parse_osi", "--params-json", '{"symbol":"TEST240119C00100000"}', "--output", str(output)])
            self.assertEqual(code, 0)
            archived = output.read_text(encoding="utf-8")
            self.assertNotIn("ARTIFICIAL-TOKEN", archived)
            self.assertNotIn("ARTIFICIAL-QUERY", archived)
            self.assertNotIn("next_actions", archived)
            self.assertNotIn("data", receipt)


class YFinanceGatewayTests(OfflineCase):
    def test_new_routes_discover_offline_and_do_not_load_dependency(self):
        import sys
        before = "yfinance" in sys.modules
        code, result = self.cli(["--list"])
        self.assertEqual(code, 0)
        self.assertTrue(stock.YFINANCE_FUNCTIONS <= {item["function"] for item in result["data"]})
        self.assertEqual("yfinance" in sys.modules, before)

    def test_provider_partial_envelope_is_not_nested_or_marked_complete(self):
        payload = {"status": "partial", "actual_source": "Yahoo Finance",
                   "data": {"bars": [{"date": "2026-01-01", "close": 10}]},
                   "warnings": ["coverage gap"], "error": None}
        with patch.dict(stock.FUNCTIONS, {"yahoo_history": lambda **params: payload}):
            code, value = self.cli(["--function", "yahoo_history", "--params-json", '{"symbol":"TEST"}'])
        self.assertEqual(code, 0)
        self.assertEqual(value["status"], "partial")
        self.assertEqual(value["data"], payload["data"])
        self.assertEqual(value["function"], "yahoo_history")

    def test_provider_error_is_error_even_when_result_object_is_nonempty(self):
        with patch.dict(stock.FUNCTIONS, {"yahoo_quote": lambda **params: {
            "status": "error", "data": None, "error": "DependencyUnavailable",
            "error_type": "DependencyUnavailable"}}):
            code, value = self.cli(["--function", "yahoo_quote", "--params-json", '{"symbol":"TEST"}'])
        self.assertEqual(code, 1)
        self.assertEqual(value["status"], "error")

    def test_output_receipt_counts_yahoo_bars_not_envelope_fields(self):
        payload = {"status": "success", "actual_source": "Yahoo Finance",
                   "data": {"bars": [{"date": "2026-01-01"}, {"date": "2026-01-02"}, {"date": "2026-01-03"}]}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(stock.FUNCTIONS, {"yahoo_history": lambda **params: payload}):
            output = Path(directory) / "history.json"
            code, receipt = self.cli(["--function", "yahoo_history", "--params-json", '{"symbol":"TEST"}', "--output", str(output)])
            archived_count = len(json.loads(output.read_text(encoding="utf-8"))["data"]["bars"])
        self.assertEqual(code, 0)
        self.assertEqual(receipt["record_count"], 3)
        self.assertEqual(archived_count, 3)


if __name__ == "__main__":
    unittest.main()
