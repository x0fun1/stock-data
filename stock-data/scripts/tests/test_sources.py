"""Isolated source/collection regression tests; all automated network calls are mocked."""

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from news_research.collection.collect import collect_sources
from news_research.common.time import parse_time
from news_research.context.facts import build_context
from news_research.events.normalize import build_events, normalize_records
from news_research.sources import companyfacts, fed, finnhub, fred, sec
from news_research.sources.base import interval, record, result
from news_research.sources.registry import source_registry
from news_research.sources.transport import MAX_RESPONSE_BYTES, Client, SourceError, _NoRedirect, retry_after_seconds

START = "2025-09-02T23:59:59Z"
END = "2025-11-01T23:59:59Z"


def request(source_id="hkex", **overrides):
    value = {"symbol": "00700.HK", "market": "HK", "currency": "HKD", "asset_type": "stock", "as_of": END,
             "lookback_days": 60, "recent_days": 7, "sources": [{"source_id": source_id}],
             "budget": {"max_requests": 120, "batch_size": 100, "max_analysis_events": 1000}, "collection_mode": "backfill"}
    value.update(overrides)
    return value


def receipt(source_id="hkex", **overrides):
    value = {"receipt_id": "host-1", "source_id": source_id, "entity": "00700.HK", "domain": "company",
             "requested_start": START, "requested_end": END, "successful_intervals": [{"start": START, "end": END}],
             "failed_intervals": [], "status": "success", "cursor": None, "exhausted": True, "cap_reached": False,
             "fetched_count": 0, "deduplicated_count": 0, "last_success_at": END, "reason": None}
    value.update(overrides)
    return value


def article(source_id="hkex", item_id="1", **overrides):
    value = record(source_id, item_id, title="Issuer official disclosure", url=f"https://www1.hkexnews.hk/listedco/{item_id}.pdf",
                   published_at="2025-10-20T06:00:00Z", entities=["00700.HK"], text="Document evidence.", text_scope="official_excerpt",
                   permissions={"store_text": True, "retention_days": 120}, receipt_id="host-1")
    value.update(overrides)
    return value


def sec_arrays(count=1, *, day="2025-10-20", offset=0):
    return {"accessionNumber": [f"0000320193-25-{index + offset:06d}" for index in range(count)],
            "filingDate": [day] * count, "form": ["6-K"] * count, "primaryDocument": ["doc.htm"] * count,
            "acceptanceDateTime": [day + "T13:00:00Z"] * count}


class CollectionTests(unittest.TestCase):
    def test_registry_is_declarative_and_independent(self):
        first = source_registry()
        self.assertTrue(all(row["account_test"]["status"] == "not_tested" for row in first))
        self.assertTrue(all(row["current_request"]["status"] == "not_requested" for row in first))
        first[0]["documented"]["history"] = "mutated"
        self.assertNotEqual(first[0]["documented"]["history"], source_registry()[0]["documented"]["history"])
        facts = next(row for row in first if row["source_id"] == "sec_companyfacts")
        self.assertIn("ifrs-full (only when reported)", facts["documented"]["taxonomies"])
        self.assertIn("Core revenue", facts["documented"]["adapter_status"])

    def test_empty_success_is_not_api_failure(self):
        output = collect_sources(request(), {"coverage": [receipt()]})
        self.assertEqual(output["records"], [])
        self.assertEqual(output["coverage"][0]["status"], "success")
        self.assertEqual(output["coverage"][0]["last_success_at"], END)
        self.assertTrue(output["coverage"][0]["exhausted"])

    def test_sparse_articles_do_not_reduce_receipt_coverage(self):
        imports = {"records": [article(item_id="a"), article(item_id="b", published_at="2025-10-21T06:00:00Z")],
                   "coverage": [receipt(fetched_count=2, deduplicated_count=2)]}
        output = collect_sources(request(), imports)
        self.assertEqual(output["coverage"][0]["successful_intervals"], [{"start": START, "end": END}])
        self.assertEqual(len(output["records"]), 2)
        self.assertEqual(output["records"][0]["source_kind"], "official")

    def test_seven_day_receipt_cannot_become_sixty_day_complete(self):
        recent = "2025-10-25T23:59:59Z"
        output = collect_sources(request(), {"coverage": [receipt(requested_start=recent, successful_intervals=[{"start": recent, "end": END}])]})
        coverage = output["coverage"][0]
        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(coverage["requested_start"], START)
        self.assertEqual(coverage["failed_intervals"][0]["end"], recent)

    def test_article_span_without_receipt_never_proves_coverage(self):
        output = collect_sources(request(), {"records": [article(published_at="2025-09-03T00:00:00Z"), article(item_id="2", published_at=END)]})
        self.assertEqual(len(output["records"]), 2)
        self.assertEqual(output["coverage"][0]["successful_intervals"], [])
        self.assertEqual(output["coverage"][0]["status"], "partial")

    def test_reject_inconsistent_receipt_semantics(self):
        cases = [
            {"exhausted": False}, {"cap_reached": True}, {"cursor": "next"}, {"fetched_count": -1},
            {"deduplicated_count": 1}, {"successful_intervals": []}, {"entity": "OTHER"},
            {"requested_start": "2025-09-01T00:00:00Z"}, {"requested_end": "2025-11-02T00:00:00Z"},
            {"successful_intervals": [{"start": "2025-09-01T00:00:00Z", "end": END}]},
            {"failed_intervals": [{"start": START, "end": END, "reason": "overlaps success"}]},
            {"requested_start": "2025-09-02T23:59:59"}, {"status": "failed"},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                output = collect_sources(request(), {"coverage": [receipt(**fields)]})
                self.assertNotEqual(output["coverage"][0]["status"], "success")
                self.assertEqual(output["coverage"][0]["successful_intervals"], [])

    def test_host_counts_and_duplicate_conflicts_rejected(self):
        output = collect_sources(request(), {"records": [article()], "coverage": [receipt()]})
        self.assertNotEqual(output["coverage"][0]["status"], "success")
        output = collect_sources(request(), {"coverage": [receipt(), receipt(fetched_count=9)]})
        self.assertNotEqual(output["coverage"][0]["status"], "success")

    def test_date_only_host_record_without_timezone_keeps_uncertainty(self):
        row = article(published_at="2025-10-20", time_precision="date")
        row.pop("timezone")
        output = collect_sources(request(), {"records": [row]})
        self.assertIsNone(output["records"][0]["timezone"])
        self.assertEqual(output["records"][0]["published_at"], "2025-10-20")

    def test_permissions_are_explicit_and_enforced(self):
        for permission in (None, {"store_text": True, "retention_days": 0}, {"store_text": "yes", "retention_days": 120}):
            with self.subTest(permission=permission):
                output = collect_sources(request(), {"records": [article(permissions=permission)]})
                self.assertEqual(output["records"], [])
        output = collect_sources(request(), {"records": [article(permissions={"store_text": False, "retention_days": 30})]})
        self.assertEqual(output["records"][0]["text"], "")
        self.assertEqual(output["records"][0]["text_scope"], "headline_only")

    def test_official_source_bound_to_official_citation(self):
        output = collect_sources(request(), {"records": [article(url="https://lookalike.example/official.pdf")]})
        self.assertEqual(output["records"], [])
        self.assertIn("domain_mismatch", " ".join(output["limitations"]))

    def test_financial_exposure_and_ongoing_records_outside_news_window_survive(self):
        old = "2024-10-20T12:00:00Z"
        rows = [article(item_id="1", data_type="financial", published_at=old),
                article(item_id="2", data_type="exposure", published_at=old, evidence_locator="p.20",
                        confirmed_numbers=[{"value": 30, "unit": "%", "subject": "Mainland revenue"}]),
                article(item_id="3", ongoing=True, published_at=old, effective_at=old),
                article(item_id="4", published_at=old), article(item_id="5", published_at="2025-11-03T12:00:00Z")]
        output = collect_sources(request(), {"records": rows})
        self.assertEqual({row["source_item_id"] for row in output["records"]}, {"1", "2", "3"})
        self.assertEqual(output["records"][1]["evidence_locator"], "p.20")
        self.assertEqual(output["records"][1]["confirmed_numbers"][0]["value"], 30)

    def test_post_cutoff_host_revision_cannot_supply_old_body(self):
        output = collect_sources(request(), {"records": [article(updated_at="2025-11-02T10:00:00Z",
            confirmed_numbers=[{"value": 9, "unit": "USD", "subject": "revised amount"}])]})
        self.assertEqual(output["records"][0]["text"], "")
        self.assertEqual(output["records"][0]["confirmed_numbers"], [])

    def test_failures_are_receipted_and_other_sources_continue(self):
        for error in (SourceError("http_403", unavailable=True), SourceError("http_429"), SourceError("network_or_timeout")):
            with self.subTest(reason=error.reason), patch("news_research.collection.collect._ADAPTERS", {"fed": Mock(side_effect=error)}):
                output = collect_sources(request(sources=[{"source_id": "fed"}, {"source_id": "hkex"}]), {"coverage": [receipt()]})
                self.assertIn(error.reason, output["coverage"][0]["reason"])
                self.assertEqual(output["coverage"][1]["status"], "success")

    def test_fetched_record_citation_keeps_essential_query_without_secrets(self):
        output = collect_sources(request(), {"records": [article(url="https://www1.hkexnews.hk/filing?id=123&token=fixture-secret")]})
        row = output["records"][0]
        self.assertIn("id=123", row["url"])
        self.assertNotIn("fixture-secret", row["url"])

    def test_acquisition_not_truncated_by_analysis_batch_or_event_budget(self):
        rows = [article(item_id=str(index)) for index in range(750)]
        output = collect_sources(request(budget={"max_requests": 0, "batch_size": 1, "max_analysis_events": 1}),
                                 {"records": rows, "coverage": [receipt(fetched_count=750, deduplicated_count=750)]})
        self.assertEqual(len(output["records"]), 750)
        self.assertEqual(output["coverage"][0]["status"], "success")
        self.assertEqual(output["coverage"][0]["requests_used"], 0)

    def test_exact_versions_deduplicate_but_correction_survives(self):
        original = article()
        correction = article(status="corrected", substantive_update=True, updated_at="2025-10-21T12:00:00Z", text="Corrected evidence.")
        imports = {"records": [original, original, correction], "coverage": [receipt(fetched_count=3, deduplicated_count=2)]}
        first, second = collect_sources(request(), imports), collect_sources(request(), imports)
        self.assertEqual(len(first["records"]), 2)
        self.assertEqual(first["coverage"][0]["receipt_id"], second["coverage"][0]["receipt_id"])
        self.assertEqual(first["coverage"][0]["deduplicated_count"], 2)

    def test_missing_credentials_do_not_block_other_sources(self):
        req = request(sources=[{"source_id": "finnhub"}, {"source_id": "hkex"}])
        with patch.dict("os.environ", {}, clear=True):
            output = collect_sources(req, {"coverage": [receipt()]})
        self.assertEqual([value["status"] for value in output["coverage"]], ["unavailable", "success"])
        self.assertEqual(output["coverage"][0]["requests_used"], 0)

    def test_environment_secrets_not_present_in_output(self):
        secret = "fixture-key-192871938"  # noqa: S105 -- synthetic test credential, never sent
        with patch.dict("os.environ", {"FINNHUB_API_KEY": secret}):
            output = collect_sources(request(), {"records": [article(text=f"External text echoes {secret}")]})
        self.assertNotIn(secret, json.dumps(output))
        self.assertIn("[redacted]", output["records"][0]["text"])

    def test_request_bounds_and_budget_validation(self):
        for fields in ({"as_of": "2025-11-01"}, {"lookback_days": 29}, {"lookback_days": 61}, {"budget": {"max_requests": 121}},
                       {"budget": {"batch_size": 0}}, {"collection_mode": "unknown"}):
            with self.subTest(fields=fields), self.assertRaises((ValueError, TypeError)):
                collect_sources(request(**fields))

    def test_incremental_reuses_verified_history_and_refreshes_recent_days(self):
        def acquire(_client, _item, left, right):
            return result(successful=[interval(left, right)], exhausted=True)

        with patch("news_research.collection.collect._ADAPTERS", {"sec_submissions": acquire}):
            first = collect_sources(request("sec_submissions"))
        mock = Mock(side_effect=acquire)
        with patch("news_research.collection.collect._ADAPTERS", {"sec_submissions": mock}):
            second = collect_sources(request("sec_submissions", collection_mode="incremental"), previous_coverage=first["coverage"])
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(mock.call_args.args[2], parse_time(END) - timedelta(days=7))
        self.assertEqual(second["coverage"][0]["status"], "success")
        previous = copy.deepcopy(first["coverage"])
        previous[0]["query_key"] = "wrong-cik"
        with patch("news_research.collection.collect._ADAPTERS", {"sec_submissions": mock}):
            collect_sources(request("sec_submissions", collection_mode="incremental"), previous_coverage=previous)
        self.assertEqual(mock.call_args.args[2], parse_time(START))

    def test_discovery_receipt_not_promoted_to_exhaustive_news(self):
        output = collect_sources(request("host_search"), {
            "records": [article("host_search", source_kind="official", text_scope="full_text")],
            "coverage": [receipt("host_search", fetched_count=1, deduplicated_count=1)],
        })
        self.assertNotEqual(output["coverage"][0]["status"], "success")
        self.assertEqual(output["records"][0]["source_kind"], "unknown")

    def test_multiple_scopes_require_matching_query_key(self):
        req = request(sources=[{"source_id": "sec_submissions", "cik": "1"}, {"source_id": "sec_submissions", "cik": "2"}])
        output = collect_sources(req, {"coverage": [receipt("sec_submissions")]})
        self.assertEqual(len(output["coverage"]), 2)
        self.assertNotEqual(output["coverage"][0]["query_key"], output["coverage"][1]["query_key"])
        self.assertTrue(all(row["status"] != "success" for row in output["coverage"]))

    def test_distinct_same_entity_scopes_use_only_their_own_host_receipts(self):
        req = request(sources=[{"source_id": "sec_submissions", "cik": "1"}, {"source_id": "sec_submissions", "cik": "2"}])
        with patch("news_research.collection.collect._ADAPTERS", {}):
            planned = collect_sources(req)["coverage"]
        receipts = [receipt("sec_submissions", receipt_id=f"host-{index}", query_key=row["query_key"], fetched_count=1, deduplicated_count=1)
                    for index, row in enumerate(planned)]
        rows = [article("sec_submissions", str(index), url=f"https://www.sec.gov/Archives/{index}.htm", receipt_id=f"host-{index}")
                for index in range(2)]
        output = collect_sources(req, {"records": rows, "coverage": receipts})
        self.assertTrue(all(row["status"] == "success" for row in output["coverage"]))
        self.assertEqual(len(output["records"]), 2)
        self.assertNotEqual(output["records"][0]["receipt_id"], output["records"][1]["receipt_id"])

    def test_malformed_untrusted_host_records_do_not_crash_run(self):
        output = collect_sources(request(), {"records": [None, {"source_id": []}, article(entities=7), article(receipt_id=[])],
                                            "coverage": [{"source_id": "hkex", "entity": "00700.HK", "receipt_id": []}]})
        self.assertNotEqual(output["coverage"][0]["status"], "success")


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.start, self.end = parse_time(START), parse_time(END)

    def test_sec_reads_historical_files_without_latest_50_limit(self):
        client = Mock()
        client.json.side_effect = [
            {"filings": {"recent": sec_arrays(600), "files": [{"name": "CIK0000320193-submissions-001.json", "filingFrom": "2025-09-01", "filingTo": "2025-09-20"}]}},
            sec_arrays(80, day="2025-09-10", offset=600),
        ]
        with patch.dict("os.environ", {"SEC_USER_AGENT": "fixture fixture@example.invalid"}, clear=True):
            value = sec.collect(client, {"cik": "320193", "symbol": "AAPL"}, self.start, self.end)
        self.assertEqual(len(value["records"]), 680)
        self.assertTrue(value["exhausted"])
        self.assertEqual(client.json.call_count, 2)
        self.assertTrue(all(row["event_key"].startswith("sec:") for row in value["records"]))
        self.assertTrue(all(row["text_scope"] == "headline_only" for row in value["records"]))

    def test_sec_accepts_official_xsl_document_subdirectories(self):
        data = sec_arrays()
        data["primaryDocument"] = ["xslF345X05/form4.xml"]
        client = Mock()
        client.json.return_value = {"filings": {"recent": data, "files": []}}
        with patch.dict("os.environ", {"SEC_CONTACT": "fixture@example.invalid"}, clear=True):
            value = sec.collect(client, {"cik": "320193", "symbol": "AAPL"}, self.start, self.end)
        self.assertTrue(value["exhausted"])
        self.assertTrue(value["records"][0]["url"].endswith("/xslF345X05/form4.xml"))

    def test_sec_historical_failure_preserves_recent_metadata(self):
        client = Mock()
        client.json.side_effect = [
            {"filings": {"recent": sec_arrays(), "files": [{"name": "CIK0000320193-submissions-001.json", "filingFrom": "2025-09-01", "filingTo": "2025-09-20"}]}},
            SourceError("request_budget_exhausted", cap_reached=True),
        ]
        with patch.dict("os.environ", {"SEC_CONTACT": "fixture@example.invalid"}, clear=True):
            value = sec.collect(client, {"cik": "320193", "symbol": "AAPL"}, self.start, self.end)
        self.assertEqual(len(value["records"]), 1)
        self.assertTrue(value["cap_reached"])
        self.assertFalse(value["exhausted"])
        self.assertEqual(value["successful_intervals"], [])

    def test_sec_refuses_untrusted_archive_path_and_requires_identity(self):
        client = Mock()
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(SourceError):
            sec.collect(client, {"cik": "320193", "symbol": "AAPL"}, self.start, self.end)
        client.json.assert_not_called()
        client.json.return_value = {"filings": {"recent": sec_arrays(), "files": [{"name": "https://attacker.invalid/a.json"}]}}
        with patch.dict("os.environ", {"SEC_CONTACT": "fixture@example.invalid"}, clear=True), self.assertRaises(SourceError):
            sec.collect(client, {"cik": "320193", "symbol": "AAPL"}, self.start, self.end)
        self.assertEqual(client.json.call_count, 1)

    def test_companyfacts_preserves_history_excludes_future_and_links_exact_json(self):
        historical = {"start": "2019-01-01", "end": "2019-12-31", "val": 1250, "accn": "0000320193-20-000001",
                      "fy": 2019, "fp": "FY", "form": "10-K", "filed": "2020-02-01", "frame": "CY2019"}
        future = {**historical, "accn": "0000320193-25-000002", "filed": "2025-11-02"}
        unknown_intraday = {**historical, "accn": "0000320193-25-000003", "filed": "2025-11-01"}
        payload = {"cik": 320193, "facts": {"us-gaap": {"NetIncomeLoss": {"label": "Net Income", "units": {"USD": [historical, future, unknown_intraday]}}}}}
        req = request("sec_companyfacts", sources=[{"source_id": "sec_companyfacts", "cik": "320193"}], symbol="AAPL", market="US")
        with patch.dict("os.environ", {"SEC_CONTACT": "fixture@example.invalid"}, clear=True), patch.object(Client, "json", return_value=payload):
            output = collect_sources(req)
        self.assertEqual(len(output["records"]), 1)
        self.assertEqual(len(output["financial_facts"]), 1)
        row, fact = output["records"][0], output["financial_facts"][0]
        self.assertEqual(row["data_type"], "financial")
        self.assertEqual(row["published_at"], "2020-02-01")
        self.assertEqual(row["timezone"], "America/New_York")
        self.assertEqual(json.loads(row["text"])["fact"], historical)
        self.assertEqual(fact["period_kind"], "annual")
        self.assertEqual(fact["accounting_basis"], "US GAAP")
        self.assertEqual(fact["record_refs"], [{"source_id": "sec_companyfacts", "source_item_id": row["source_item_id"]}])
        self.assertEqual(fact["disclosed_at"], "2020-02-01")
        self.assertEqual(output["coverage"][0]["status"], "success")
        normalized, rejected = normalize_records(output["records"])
        self.assertEqual(rejected, [])
        built = build_events(normalized, req)
        context = build_context(output, normalized, built["evidence"], req)
        self.assertEqual(len(context["financial_facts"]), 1)
        self.assertEqual(built["events"], [])

    def test_companyfacts_ifrs_six_k_cumulative_is_not_invented_quarter(self):
        disclosed = {"start": "2025-01-01", "end": "2025-06-30", "val": 500, "accn": "0000320193-25-000001",
                     "fy": 2025, "fp": "Q2", "form": "6-K", "filed": "2025-08-01"}
        payload = {"cik": 320193, "facts": {"ifrs-full": {"Revenue": {"label": "Revenue", "units": {"HKD": [disclosed]}}}}}
        client = Mock()
        client.json.return_value = payload
        with patch.dict("os.environ", {"SEC_CONTACT": "fixture@example.invalid"}, clear=True):
            output = companyfacts.collect(client, {"cik": "320193", "symbol": "HK-issuer"}, self.start, self.end)
        fact = output["financial_facts"][0]
        self.assertEqual(fact["period_kind"], "cumulative")
        self.assertEqual(fact["currency"], "HKD")
        self.assertEqual(fact["accounting_basis"], "IFRS")
        self.assertIn("6-K", fact["basis"])
        self.assertEqual(output["records"][0]["text_scope"], "official_excerpt")
        self.assertNotIn("free_cash_flow", {item["metric"] for item in output["financial_facts"]})

    def test_companyfacts_taxonomy_gap_and_invalid_units_are_explicit(self):
        client = Mock()
        client.json.return_value = {"cik": 320193, "facts": {"issuer-extension": {}}}
        with patch.dict("os.environ", {"SEC_CONTACT": "fixture@example.invalid"}, clear=True), self.assertRaisesRegex(SourceError, "supported_taxonomies_unavailable"):
            companyfacts.collect(client, {"cik": "320193", "symbol": "AAPL"}, self.start, self.end)
        disclosed = {"end": "2025-06-30", "val": 1, "accn": "0000320193-25-000001", "form": "10-Q", "filed": "2025-08-01"}
        client.json.return_value = {"cik": 320193, "facts": {"us-gaap": {"NetIncomeLoss": {"label": "Net Income", "units": {"shares": [disclosed]}}}}}
        with patch.dict("os.environ", {"SEC_CONTACT": "fixture@example.invalid"}, clear=True):
            output = companyfacts.collect(client, {"cik": "320193", "symbol": "AAPL"}, self.start, self.end)
        self.assertFalse(output["exhausted"])
        self.assertEqual(output["financial_facts"], [])
        self.assertIn("unit_not_supported", output["failed_intervals"][0]["reason"])

    def test_companyfacts_only_explicit_frame_supports_standalone_quarter(self):
        base = {"start": "2025-04-01", "end": "2025-06-30", "fp": "Q2"}
        self.assertEqual(companyfacts._period(base), "cumulative")
        self.assertEqual(companyfacts._period({**base, "frame": "CY2025Q2"}), "quarter")
        self.assertEqual(companyfacts._period({"end": "2025-06-30"}), "instant")

    def test_finnhub_segments_dates_without_inventing_exhaustion_or_rights(self):
        stamp = int(datetime(2025, 10, 20, 6, tzinfo=timezone.utc).timestamp())
        client = Mock()
        client.json.return_value = [{"id": 1, "datetime": stamp, "headline": "Company news", "url": "https://example.com/news", "summary": "Licensed body."}]
        with patch.dict("os.environ", {"FINNHUB_API_KEY": "fixture-token"}, clear=True):
            value = finnhub.collect(client, {"symbol": "AAPL"}, self.start, self.end)
        self.assertEqual(client.json.call_count, 9)
        self.assertEqual(len(value["records"]), 1)
        self.assertEqual(value["records"][0]["text"], "")
        self.assertEqual(value["records"][0]["source_kind"], "unknown")
        self.assertFalse(value["exhausted"])
        self.assertEqual(value["successful_intervals"], [])
        self.assertEqual(len(value["queried_intervals"]), 9)
        for call in client.json.call_args_list:
            self.assertNotIn("token", call.kwargs["params"])

    def test_finnhub_explicit_permission_allows_abstract_not_full_text(self):
        client = Mock()
        client.json.return_value = [{"id": 1, "datetime": int(self.end.timestamp()), "headline": "News", "url": "https://example.com/news", "summary": "Summary."}]
        with patch.dict("os.environ", {"FINNHUB_API_KEY": "fixture-token"}, clear=True):
            value = finnhub.collect(client, {"symbol": "AAPL", "permissions": {"store_text": True, "retention_days": 10}}, self.end - timedelta(days=1), self.end)
        self.assertEqual(value["records"][0]["text_scope"], "abstract")

    def test_fed_uses_both_archives_and_extracts_body_not_navigation(self):
        path = "/newsevents/pressreleases/monetary20251020a.htm"
        archive = f'<html><a href="{path}">Official title</a></html>'.encode()
        body = b'<nav>Not evidence</nav><div id="article"><div class="heading"><h3 class="title">Official title</h3></div><div class="col-xs-12 col-sm-8 col-md-8"><p>Actual policy evidence.</p></div></div><div id="lastUpdate">Last Update: October 20, 2025</div>'
        client = Mock()
        client.get.side_effect = [archive, archive, b"<rss><channel/></rss>", body]
        value = fed.collect(client, {}, self.start, self.end)
        self.assertTrue(value["exhausted"])
        self.assertEqual(value["records"][0]["text"], "Actual policy evidence.")
        self.assertEqual(value["records"][0]["time_precision"], "date")
        paths = [call.args[1] for call in client.get.call_args_list]
        self.assertIn("/newsevents/pressreleases/2025-press.htm", paths)
        self.assertIn("/newsevents/pressreleases/2025-press-fomc.htm", paths)

    def test_fed_post_cutoff_revision_does_not_become_historical_body(self):
        path = "/newsevents/pressreleases/monetary20251020a.htm"
        archive = f'<a href="{path}">Title</a>'.encode()
        body = b'<div id="article"><h3 class="title">Title</h3><div class="col-xs-12 col-sm-8 col-md-8">Revised evidence</div></div><div id="lastUpdate">Last Update: November 03, 2025</div>'
        client = Mock()
        client.get.side_effect = [archive, archive, b"<rss><channel/></rss>", body]
        value = fed.collect(client, {}, self.start, self.end)
        self.assertEqual(value["records"][0]["text"], "")
        self.assertEqual(value["records"][0]["updated_at"], "2025-11-03")

    def test_fred_paginates_vintage_index_and_freezes_each_observation_snapshot(self):
        client = Mock()
        client.json.side_effect = [
            {"count": 2, "offset": 0, "vintage_dates": ["2025-10-01"]},
            {"count": 2, "offset": 1, "vintage_dates": ["2025-10-15"]},
            {"seriess": [{"id": "DGS10", "units": "Percent", "frequency": "Daily"}]},
            {"count": 2, "offset": 0, "observations": [{"date": "2025-09-29", "value": "4.1"}]},
            {"count": 2, "offset": 1, "observations": [{"date": "2025-09-30", "value": "."}]},
            {"count": 1, "offset": 0, "observations": [{"date": "2025-10-14", "value": "4.2"}]},
        ]
        with patch.dict("os.environ", {"FRED_API_KEY": "fixture-token"}, clear=True):
            value = fred.collect(client, {"series_id": "DGS10", "permissions": {"store_text": True, "retention_days": 120}}, self.start, self.end)
        self.assertTrue(value["exhausted"])
        self.assertEqual([row["published_at"] for row in value["records"]], ["2025-10-01", "2025-10-15"])
        self.assertIn("2025-09-30: .", value["records"][0]["text"])
        for call in client.json.call_args_list:
            if call.args[1] == "/fred/series/observations":
                self.assertEqual(call.kwargs["params"]["realtime_start"], call.kwargs["params"]["realtime_end"])

    def test_fred_empty_page_before_count_is_failure(self):
        client = Mock()
        client.json.return_value = {"count": 10, "offset": 0, "vintage_dates": []}
        with patch.dict("os.environ", {"FRED_API_KEY": "fixture-token"}, clear=True), self.assertRaises(SourceError):
            fred.collect(client, {"series_id": "DGS10"}, self.start, self.end)


class TransportTests(unittest.TestCase):
    def test_allowlist_and_budget_reject_before_network(self):
        client = Client(0)
        client._opener = Mock()
        for host, path in (("attacker.invalid", "/data"), ("data.sec.gov", "//attacker.invalid/x"),
                           ("data.sec.gov", "/../secret"), ("data.sec.gov", "/submissions/a.json")):
            with self.subTest(host=host, path=path), self.assertRaises(SourceError):
                client.get(host, path)
        client._opener.open.assert_not_called()

    def test_http_errors_are_safe_and_retry_after_defers(self):
        client = Client(3)
        client._opener = Mock()
        client._opener.open.side_effect = HTTPError("https://finnhub.io/?token=secret", 429, "secret error", {"Retry-After": "120"}, BytesIO(b"secret response"))
        with self.assertRaisesRegex(SourceError, "http_429"):
            client.get("finnhub.io", "/api/v1/company-news")
        with self.assertRaisesRegex(SourceError, "retry_after_deferred"):
            client.get("finnhub.io", "/api/v1/company-news")
        self.assertEqual(client._opener.open.call_count, 1)
        self.assertEqual(client.requests, 1)
        self.assertGreaterEqual(retry_after_seconds("invalid"), 1)

    def test_response_size_cap_and_timeout_are_finite(self):
        client = Client(1)
        response = Mock()
        response.headers = {"Content-Length": str(MAX_RESPONSE_BYTES + 1)}
        manager = Mock()
        manager.__enter__ = Mock(return_value=response)
        manager.__exit__ = Mock(return_value=False)
        client._opener = Mock()
        client._opener.open.return_value = manager
        with self.assertRaisesRegex(SourceError, "response_size_limit"):
            client.get("data.sec.gov", "/submissions/a.json")
        self.assertEqual(client._opener.open.call_args.kwargs["timeout"], 10)
        response.read.assert_not_called()

    def test_redirect_handler_never_constructs_followup_request(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://attacker.invalid"))


if __name__ == "__main__":
    unittest.main()
