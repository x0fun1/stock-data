"""Federal Reserve annual release archives plus monetary-policy RSS discovery."""

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .base import in_window, interval, plain, record, result
from .transport import SourceError

_RELEASE = re.compile(r"/newsevents/pressreleases/monetary(\d{8})[a-z]\d?\.htm")


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = {}
        self._anchor = None
        self._anchor_text = []
        self._divs = []
        self._body_depth = None
        self._title = False
        self._update = False
        self._ignore = 0
        self.title = []
        self.body = []
        self.updated = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "style"}:
            self._ignore += 1
        if tag == "div":
            self._divs.append(attributes.get("id", ""))
            classes = set(attributes.get("class", "").split())
            if "article" in self._divs and classes == {"col-xs-12", "col-sm-8", "col-md-8"}:
                self._body_depth = len(self._divs)
            if attributes.get("id") == "lastUpdate":
                self._update = True
        if tag == "h3" and "title" in attributes.get("class", "").split():
            self._title = True
        if tag == "a":
            self._anchor = attributes.get("href")
            self._anchor_text = []
        if tag in {"p", "br", "li"} and self._body_depth:
            self.body.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._ignore = max(0, self._ignore - 1)
        if tag == "div" and self._divs:
            if self._body_depth == len(self._divs):
                self._body_depth = None
            if self._divs[-1] == "lastUpdate":
                self._update = False
            self._divs.pop()
        if tag == "h3":
            self._title = False
        if tag == "a" and self._anchor:
            self.links[self._anchor] = plain(" ".join(self._anchor_text))
            self._anchor = None

    def handle_data(self, data):
        if self._ignore:
            return
        if self._anchor:
            self._anchor_text.append(data)
        if self._title:
            self.title.append(data)
        if self._body_depth:
            self.body.append(data)
        if self._update:
            self.updated.append(data)


def _path(value):
    parts = urlsplit(value)
    if parts.scheme and (parts.scheme != "https" or parts.netloc != "www.federalreserve.gov"):
        return None
    if parts.query or parts.fragment:
        return None
    return parts.path if _RELEASE.fullmatch(parts.path) else None


def _date(path):
    return datetime.strptime(_RELEASE.fullmatch(path).group(1), "%Y%m%d").date().isoformat()


def _page(client, path):
    parser = _Page()
    parser.feed(client.get("www.federalreserve.gov", path).decode("utf-8-sig"))
    return parser


def collect(client, item, start, end):
    if item.get("topics", ["monetary"]) not in (["monetary"], "monetary"):
        raise SourceError("fed_only_monetary_archive_supported", unavailable=True)
    if start.year < 2006:
        raise SourceError("fed_archive_before_2006_not_supported", unavailable=True)
    paths, failures, limitations = {}, [], []
    capped = False
    for year in range(start.year, end.year + 1):
        # The main annual archive excludes FOMC decisions; both indexes are necessary.
        for suffix in ("press", "press-fomc"):
            try:
                page = _page(client, f"/newsevents/pressreleases/{year}-{suffix}.htm")
                releases = {_path(link): title for link, title in page.links.items() if _path(link)}
                if not releases:
                    raise SourceError("fed_archive_layout_or_empty_index_unverified")
                paths.update(releases)
            except (SourceError, UnicodeError, ValueError) as exc:  # noqa: PERF203 -- one failed archive must not discard another
                reason = exc.reason if isinstance(exc, SourceError) else "fed_invalid_archive"
                failures.append(interval(start, end, reason))
                capped |= getattr(exc, "cap_reached", False)
    # RSS is supplementary. A rolling feed can never fill a missing archive interval.
    try:
        xml = client.get("www.federalreserve.gov", "/feeds/press_monetary.xml")
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            raise SourceError("fed_unsafe_rss_declaration")
        root = ET.fromstring(xml)  # noqa: S314 -- bounded bytes; entity/DTD declarations rejected
        if root.tag != "rss":
            raise SourceError("fed_invalid_rss")
        for entry in root.findall("./channel/item"):
            path = _path(entry.findtext("link", ""))
            if path:
                paths.setdefault(path, plain(entry.findtext("title", "")))
    except (SourceError, ET.ParseError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, SourceError) else "fed_invalid_rss"
        limitations.append(f"Supplemental Fed RSS unavailable: {reason}; RSS is not the archive coverage receipt.")
    rows = []
    for path, title in sorted(paths.items()):
        publication = _date(path)
        if not in_window(publication, start, end):
            continue
        row = record("fed", path.rsplit("/", 1)[-1], title=title,
                     url="https://www.federalreserve.gov" + path, published_at=publication,
                     category="macro", event_key="fed:" + path, origin_id="fed:" + path,
                     permissions={"store_text": True, "retention_days": 120})
        try:
            page = _page(client, path)
            body, heading = plain(" ".join(page.body)), plain(" ".join(page.title))
            if not body or not heading:
                raise SourceError("fed_release_body_unrecognized")
            row.update(title=heading, text=body, text_scope="official_excerpt", evidence_locator="#article")
            updated = plain(" ".join(page.updated)).removeprefix("Last Update:").strip()
            if updated:
                try:
                    row["updated_at"] = datetime.strptime(updated, "%B %d, %Y").date().isoformat()
                except ValueError:
                    raise SourceError("fed_update_date_unrecognized") from None
            else:
                raise SourceError("fed_update_date_missing")
            if row["updated_at"] > end.date().isoformat():
                # Archive title remains discoverable, but post-cutoff body cannot be replayed.
                row.update(text="", text_scope="headline_only")
                limitations.append("Fed release body was updated after as-of; only metadata retained.")
        except (SourceError, UnicodeError, ValueError) as exc:
            reason = exc.reason if isinstance(exc, SourceError) else "fed_invalid_release"
            row.update(text="", text_scope="headline_only")
            failures.append(interval(start, end, reason))
            capped |= getattr(exc, "cap_reached", False)
        rows.append(row)
    return result(rows, successful=[] if failures else [interval(start, end)], failed=failures,
                  exhausted=not failures, cap_reached=capped, limitations=limitations)
