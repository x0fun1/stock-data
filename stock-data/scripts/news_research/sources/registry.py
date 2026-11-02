"""Source declarations are not evidence of an account entitlement or a live probe."""

from copy import deepcopy


def _entry(source_id, markets, languages, domain, data_types, documented, credentials=()):
    return {
        "source_id": source_id,
        "markets": markets,
        "languages": languages,
        "domain": domain,
        "entity_scope": "configured entity or topic only",
        "data_types": data_types,
        "documented": documented,
        "account_test": {"status": "not_tested", "tested_at": None, "reason": "No account probe has been performed."},
        "current_request": {"status": "not_requested", "receipt_ids": []},
        "credentials": list(credentials),
        "request_policy": {"https_only": True, "redirects": False, "timeout_seconds": 10, "max_response_bytes": 16777216, "retry_policy": "No automatic retry; Retry-After defers the host for this run."},
    }


_REGISTRY = [
    _entry("sec_submissions", ["US"], ["en"], "company", ["filing_metadata"], {
        "history": "Recent submissions plus every overlapping historical submissions file listed by SEC.",
        "date_filter": "Local acceptance timestamp / filing-date filtering after archive traversal.",
        "pagination": "filings.files; no latest-50 truncation",
        "text_scope": "headline_only",
        "latency": "SEC dissemination; no contractual latency guarantee",
        "permissions": {"store_text": False, "retention_days": 120, "note": "Adapter collects filing metadata and links, not filing bodies."},
        "forms": ["10-K", "10-Q", "8-K", "20-F", "6-K", "all other submission forms"],
        "financial_support": "Submissions are not financial facts; use SEC companyfacts or authorized host facts separately.",
        "references": ["https://www.sec.gov/search-filings/edgar-application-programming-interfaces", "https://www.sec.gov/about/developer-resources"],
    }, ["SEC_USER_AGENT or SEC_CONTACT (user-supplied identification; never invented)"]),
    _entry("sec_companyfacts", ["US"], ["en"], "financial", ["financial_facts"], {
        "history": "Companyfacts includes as-filed XBRL facts; retain accession and filed date for as-of checks.",
        "date_filter": "As-filed date must be fully past in America/New_York; financial history is independent of the news window.",
        "pagination": "one bounded companyfacts response; no recent-50 or analysis-batch truncation",
        "text_scope": "official_excerpt of exact structured XBRL JSON, not issuer report prose",
        "latency": "SEC dissemination; taxonomy availability varies",
        "permissions": {"store_text": True, "retention_days": 120, "note": "Structured factual XBRL excerpts only; no filing-body reproduction."},
        "taxonomies": ["us-gaap", "ifrs-full (only when reported)"],
        "forms": ["10-K", "10-Q", "20-F", "6-K (only tagged facts actually present)"],
        "adapter_status": "Core revenue/net income/CFO/capex/cash/debt/EPS/shares facts and evidence records; only tags actually present, no issuer-extension or complete foreign-issuer/6-K claim.",
        "references": ["https://www.sec.gov/search-filings/edgar-application-programming-interfaces"],
    }, ["SEC_USER_AGENT or SEC_CONTACT (user-supplied identification; never invented)"]),
    _entry("finnhub", ["US", "HK (account/coverage dependent)"], ["provider supplied"], "company", ["company_news"], {
        "history": "company-news accepts from/to dates; 60-day entitlement and HK availability require account verification.",
        "date_filter": "7-day disjoint query intervals with boundary filtering",
        "pagination": "No documented pagination/completeness receipt in company-news responses; coverage remains partial even on HTTP 200.",
        "text_scope": "headline_only by default; abstract only with explicit authorized store_text/retention_days configuration",
        "latency": "account dependent / unknown",
        "permissions": {"store_text": False, "retention_days": 0, "note": "An API key alone does not establish content storage rights."},
        "references": ["https://finnhub.io/docs/api/company-news", "https://github.com/Finnhub-Stock-API/finnhub-python"],
    }, ["FINNHUB_API_KEY"]),
    _entry("fed", ["US", "HK (macro context only)"], ["en"], "macro", ["official_monetary_releases"], {
        "history": "Annual official monetary-policy press-release archive (2006 onward); RSS supplements updates, never proves history coverage.",
        "date_filter": "Release date from the official archive link; precision remains date.",
        "pagination": "One archive per calendar year, followed by each in-window official release.",
        "text_scope": "official_excerpt; release body only, not archive/nav text",
        "latency": "publication-driven",
        "permissions": {"store_text": True, "retention_days": 120, "note": "Federal Reserve authored public releases only; no linked third-party bodies."},
        "topics": ["monetary"],
        "references": ["https://www.federalreserve.gov/newsevents/pressreleases.htm", "https://www.federalreserve.gov/feeds/feeds.htm"],
    }),
    _entry("fred", ["US", "HK (macro context only)"], ["en"], "macro", ["as_of_vintage_observations"], {
        "history": "Paginated series vintage-date index followed by observations queried with realtime_start=realtime_end for each in-window vintage.",
        "date_filter": "Vintage availability is date-precision; observations retain their own period dates, never substituted for release dates.",
        "pagination": "count/offset/limit with a frozen real-time date",
        "text_scope": "headline_only unless explicit series-specific redistribution/storage permission is configured",
        "latency": "Series specific; real-time precision is a calendar day, not an intraday timestamp.",
        "permissions": {"store_text": False, "retention_days": 0, "note": "FRED includes third-party series; API access is not blanket redistribution permission."},
        "references": ["https://fred.stlouisfed.org/docs/api/fred/realtime_period.html", "https://fred.stlouisfed.org/docs/api/fred/series_observations.html"],
    }, ["FRED_API_KEY"]),
]

for _source_id, _markets, _languages, _domain, _note in [
    ("hkex", ["HK"], ["zh", "en"], "company", "HKEX public search is not assumed to authorize automation; use authorized host announcements/IIS."),
    ("company_ir", ["US", "HK"], ["en", "zh"], "company", "No arbitrary IR URL fetching; import lawfully obtained issuer releases."),
    ("china_macro", ["HK"], ["zh", "en"], "macro", "PBOC/NBS and mainland exposure data require verified endpoints or authorized host import."),
    ("hkma", ["HK"], ["zh", "en"], "macro", "Import authorized HKMA releases with original official links and query receipts."),
    ("eia", ["US", "HK"], ["en"], "macro", "Energy prices/inventory/production/forecasts must retain unit, observation period and release date; host import only."),
    ("host_search", ["US", "HK"], ["provider supplied"], "company", "Discovery only; search results do not establish exhaustive news coverage."),
]:
    _REGISTRY.append(_entry(_source_id, _markets, _languages, _domain, ["authorized_host_import"], {
        "history": "Only the ranges substantiated by authorized host query receipts.",
        "date_filter": "Host must provide explicit query intervals, not first/last article dates.",
        "pagination": "Host receipts must declare cursor/exhaustion/cap status.",
        "text_scope": "Per-record explicit permission and scope",
        "latency": "unknown",
        "permissions": {"store_text": False, "retention_days": 0, "note": _note},
        "adapter_status": "host_import_only",
    }))


def source_registry() -> list[dict]:
    """Return independent declarations; this call never makes network requests."""
    return deepcopy(_REGISTRY)
