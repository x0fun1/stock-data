"""Native local Search/Fetch service: DDGS or SearXNG, and public HTML extraction."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import ipaddress
import json
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests

from ..common.timeutil import now_utc
from .local_search_fetch import local_endpoint


class PublicationParser(HTMLParser):
    """Retain publisher-provided dates; never infer a timezone from a date."""
    def __init__(self):
        super().__init__()
        self.published_at = None
        self.script = None
        self.json_scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        key = attrs.get('property', attrs.get('name', '')).lower()
        if tag == 'meta' and key in {'article:published_time', 'datepublished', 'pubdate', 'parsely-pub-date'}:
            self.published_at = self.published_at or attrs.get('content')
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.script = []

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.script is not None:
            self.json_scripts.append(''.join(self.script))
            self.script = None


def _article_dates(value):
    if isinstance(value, dict):
        kind = value.get('@type', [])
        kinds = [kind] if isinstance(kind, str) else kind
        if isinstance(kinds, list) and any('article' in str(item).lower() for item in kinds):
            if isinstance(value.get('datePublished'), str):
                yield value['datePublished']
        for child in value.values():
            yield from _article_dates(child)
    elif isinstance(value, list):
        for child in value:
            yield from _article_dates(child)


def extract_document(html, url, extractor=None):
    if extractor is None:
        from trafilatura import extract
        extractor = extract
    packed = extractor(html, url=url, output_format='json', with_metadata=True,
                       include_comments=False, include_tables=False, favor_precision=True)
    data = json.loads(packed) if packed else {}
    text = data.get('text') or ''
    restricted = any(term in text[:4000].lower() for term in (
        'access denied', 'verify you are human', 'enable javascript and cookies',
        'subscribe to continue', 'sign in to read', 'we do not host the original article'))
    if restricted or len(text) < 300 or len(text.split()) < 40:
        return {'url': url, 'status': 'unavailable',
                'reason': 'access_restricted' if restricted else 'body_unavailable_or_javascript_required'}
    parser = PublicationParser()
    parser.feed(html)
    dates = []
    for block in parser.json_scripts:
        try:
            dates.extend(_article_dates(json.loads(block)))
        except (ValueError, TypeError):
            pass
    publication = parser.published_at or (dates[0] if len(set(dates)) == 1 else None) or data.get('date')
    return {'url': url, 'status': 'fetched', 'title': data.get('title'), 'markdown': text,
            'published_at': publication, 'fetched_at': now_utc().isoformat(),
            'content_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'extraction_method': 'trafilatura_public_html', 'semantic_review_complete': False}


def public_article_url(url, allow_vpn_fake_ip=False):
    """Check initial URLs and every redirect before sending an outbound request."""
    parts = urlsplit(url)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.port not in {None, 443}):
        raise ValueError('public HTTPS article URL required')
    host = parts.hostname.lower().rstrip('.')
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home')):
        raise ValueError('local article hostname rejected')
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError('non-public literal article address rejected')
    addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
    def allowed(address):
        ip = ipaddress.ip_address(address[4][0])
        # Tun proxies use this benchmark range as synthetic DNS addresses.
        # This opt-in does not permit loopback, RFC1918, link-local, or literal synthetic IP URLs.
        return ip.is_global or (allow_vpn_fake_ip and literal is None and '.' in host
                                and ip in ipaddress.ip_network('198.18.0.0/15'))
    if not addresses or not all(allowed(address) for address in addresses):
        raise ValueError('private or non-public article address rejected')
    return url


class LocalService:
    def __init__(self, search_backend='ddgs', searxng_url=None, engines='auto',
                 search_workers=32, fetch_workers=32, timeout=15, max_results=6, allow_vpn_fake_ip=False):
        if search_backend not in {'ddgs', 'searxng'}:
            raise ValueError('unknown local search backend')
        if not (1 <= search_workers <= 32 and 1 <= fetch_workers <= 32 and 1 <= timeout <= 60
                and 1 <= max_results <= 20):
            raise ValueError('invalid local service concurrency, timeout, or result limit')
        if search_backend == 'searxng' and not searxng_url:
            raise ValueError('SearXNG backend requires --searxng-url')
        self.search_backend = search_backend
        self.searxng_url = local_endpoint(searxng_url) if searxng_url else None
        self.engines = engines
        self.timeout = timeout
        self.max_results = max_results
        self.allow_vpn_fake_ip = allow_vpn_fake_ip
        self.search_slots = threading.BoundedSemaphore(search_workers)
        self.fetch_pool = ThreadPoolExecutor(max_workers=fetch_workers, thread_name_prefix='local-fetch')
        self.search_workers = search_workers
        self.fetch_workers = fetch_workers

    def health(self):
        return {'status': 'ok', 'search_backend': self.search_backend, 'engines': self.engines,
                'search_workers': self.search_workers, 'fetch_workers': self.fetch_workers,
                'fixed_search_interval_seconds': 0, 'javascript_rendering': False,
                'vpn_fake_ip_allowed': self.allow_vpn_fake_ip}

    def search(self, query):
        start = time.monotonic()
        with self.search_slots:
            try:
                if self.search_backend == 'ddgs':
                    from ddgs import DDGS
                    rows = DDGS(timeout=self.timeout).text(query, region='us-en',
                        max_results=self.max_results, backend=self.engines)
                else:
                    response = requests.get(self.searxng_url + '/search', params={
                        'q': query, 'format': 'json', 'language': 'en'}, timeout=self.timeout,
                        allow_redirects=False)
                    response.raise_for_status()
                    data = response.json()
                    rows = data.get('results', [])
                    if data.get('unresponsive_engines') and not rows:
                        return {'status': 'unavailable', 'sources': [], 'reason': 'searxng_engines_unavailable'}
                sources = [{'url': row.get('url') or row.get('href'), 'title': row.get('title', ''),
                            'snippet': row.get('body') or row.get('content') or '',
                            'published_at': row.get('date') or row.get('publishedDate'),
                            'publisher': row.get('source'), 'engine': row.get('engine')}
                           for row in rows if row.get('url') or row.get('href')]
                return {'status': 'ok', 'sources': sources[:self.max_results],
                        'searched_at': now_utc().isoformat(), 'backend': self.search_backend,
                        'elapsed_seconds': round(time.monotonic() - start, 3)}
            except Exception as error:
                # Keep upstream errors observable, without leaking proxy credentials.
                return {'status': 'unavailable', 'sources': [], 'backend': self.search_backend,
                        'reason': 'search_backend:' + type(error).__name__,
                        'elapsed_seconds': round(time.monotonic() - start, 3)}

    def fetch_one(self, url):
        current = url
        start = time.monotonic()
        try:
            for _ in range(6):
                public_article_url(current, self.allow_vpn_fake_ip)
                with requests.get(current, timeout=self.timeout, stream=True, allow_redirects=False,
                                  headers={'User-Agent': 'stock-research-local/0.1',
                                           'Accept': 'text/html,application/xhtml+xml'}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        current = urljoin(current, response.headers.get('Location', ''))
                        continue
                    if response.status_code >= 400:
                        return {'url': url, 'status': 'unavailable', 'reason': f'http_{response.status_code}'}
                    if not any(kind in response.headers.get('Content-Type', '').lower()
                               for kind in ('text/html', 'application/xhtml+xml')):
                        return {'url': url, 'status': 'unavailable', 'reason': 'unsupported_content_type'}
                    chunks = []
                    size = 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 5_000_000:
                            return {'url': url, 'status': 'unavailable', 'reason': 'page_too_large'}
                        if time.monotonic() - start > self.timeout * 2:
                            return {'url': url, 'status': 'unavailable', 'reason': 'page_download_timeout'}
                        chunks.append(chunk)
                    html = b''.join(chunks).decode(response.encoding or 'utf-8', errors='replace')
                document = extract_document(html, current)
                return {**document, 'url': url, 'resolved_url': current,
                        'elapsed_seconds': round(time.monotonic() - start, 3)}
            return {'url': url, 'status': 'unavailable', 'reason': 'too_many_redirects'}
        except Exception as error:
            return {'url': url, 'status': 'unavailable', 'reason': 'public_fetch:' + type(error).__name__}

    def fetch(self, urls):
        return {'status': 'ok', 'documents': list(self.fetch_pool.map(self.fetch_one, urls))}

    def close(self):
        self.fetch_pool.shutdown(wait=True, cancel_futures=True)


def create_server(service, host='127.0.0.1', port=8765):
    if host not in {'127.0.0.1', 'localhost'}:
        raise ValueError('local Search/Fetch server must bind to loopback')

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # No titles, query strings, or full article bodies in HTTP logs.

        def reply(self, status, data):
            packed = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(packed)))
            self.end_headers()
            try:
                self.wfile.write(packed)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == '/health':
                self.reply(200, service.health())
            else:
                self.reply(404, {'status': 'unavailable', 'reason': 'unknown_endpoint'})

        def do_POST(self):
            if self.headers.get('Origin') or 'application/json' not in self.headers.get('Content-Type', ''):
                self.reply(415, {'status': 'unavailable', 'reason': 'local_json_client_required'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    raise ValueError('invalid request size')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('object required')
                if self.path == '/search':
                    query = payload.get('query')
                    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
                        raise ValueError('query required')
                    self.reply(200, service.search(query))
                elif self.path == '/fetch':
                    urls = payload.get('urls')
                    if (not isinstance(urls, list) or not 1 <= len(urls) <= 5
                            or any(not isinstance(url, str) or len(url) > 8192 for url in urls)):
                        raise ValueError('one to five URLs required')
                    self.reply(200, service.fetch(urls))
                else:
                    self.reply(404, {'status': 'unavailable', 'reason': 'unknown_endpoint'})
            except (ValueError, TypeError):
                self.reply(400, {'status': 'unavailable', 'reason': 'invalid_request'})
            except Exception as error:
                self.reply(500, {'status': 'unavailable', 'reason': 'local_service:' + type(error).__name__})

    return ThreadingHTTPServer((host, port), Handler)


def serve(search_backend='ddgs', searxng_url=None, engines='auto', search_workers=32,
          fetch_workers=32, timeout=15, max_results=6, port=8765, allow_vpn_fake_ip=False):
    try:
        import trafilatura  # noqa: F401
        if search_backend == 'ddgs':
            import ddgs  # noqa: F401
    except ImportError as error:
        raise SystemExit('Install skill dependencies in its .venv: python -m pip install -r requirements.txt') from error
    service = LocalService(search_backend, searxng_url, engines, search_workers, fetch_workers, timeout, max_results,
                           allow_vpn_fake_ip)
    server = create_server(service, port=port)
    print(json.dumps({'listening': f'http://127.0.0.1:{port}', **service.health()}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
