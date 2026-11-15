"""Loopback Search/Fetch adapter without a TinyFish account or fixed search delay."""
from __future__ import annotations

import os
import threading
from urllib.parse import urlsplit

import requests


def local_endpoint(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme not in {'http', 'https'} or parts.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError('Search/Fetch endpoint must be a loopback HTTP(S) URL without credentials or query')
    return value.rstrip('/')


class LocalSearchFetch:
    name = 'local_search_fetch_http'
    supports_concurrency = True
    prefer_source_url = True

    def __init__(self, search_url=None, fetch_url=None, timeout=45, session=None):
        self.search_url = local_endpoint(search_url or os.environ.get('STOCK_SEARCH_URL', 'http://127.0.0.1:8765/search'))
        self.fetch_url = local_endpoint(fetch_url or os.environ.get('STOCK_FETCH_URL', 'http://127.0.0.1:8765/fetch'))
        if not 1 <= timeout <= 120:
            raise ValueError('local timeout must be between 1 and 120 seconds')
        self.timeout = timeout
        self._sessions = threading.local()
        self._injected_session = session

    @property
    def session(self):
        if self._injected_session is not None:
            return self._injected_session
        if not hasattr(self._sessions, 'session'):
            self._sessions.session = requests.Session()
            # Loopback calls must not go through an HTTP proxy.
            self._sessions.session.trust_env = False
        return self._sessions.session

    @property
    def collection_config(self):
        return {'search_url': self.search_url, 'fetch_url': self.fetch_url,
                'timeout_seconds': self.timeout, 'source_url_first': True}

    def _request(self, url, payload):
        try:
            response = self.session.post(url, json=payload, timeout=self.timeout, verify=True,
                                         allow_redirects=False)
        except requests.RequestException as error:
            return {'status': 'unavailable', 'reason': 'local_network:' + type(error).__name__}
        if response.status_code != 200:
            return {'status': 'unavailable', 'reason': f'local_http_{response.status_code}'}
        try:
            result = response.json()
        except ValueError:
            return {'status': 'unavailable', 'reason': 'local_response_not_json'}
        if not isinstance(result, dict):
            return {'status': 'unavailable', 'reason': 'local_response_not_object'}
        return result

    def search(self, task, query):
        result = self._request(self.search_url, {'query': query})
        sources = result.get('sources', [])
        if not isinstance(sources, list):
            return {'status': 'unavailable', 'sources': [], 'reason': 'local_sources_not_list'}
        return {**result, 'sources': [row for row in sources if isinstance(row, dict) and isinstance(row.get('url'), str)]}

    def check_available(self):
        for endpoint in {self.search_url, self.fetch_url}:
            parts = urlsplit(endpoint)
            health_url = f'{parts.scheme}://{parts.netloc}/health'
            try:
                response = self.session.get(health_url, timeout=5, allow_redirects=False)
                if response.status_code != 200 or response.json().get('status') != 'ok':
                    raise ValueError('invalid health response')
            except (requests.RequestException, ValueError, AttributeError) as error:
                raise RuntimeError('Local Search/Fetch service unavailable; start news-local-serve before collecting') from error

    def fetch(self, task, urls):
        result = self._request(self.fetch_url, {'urls': urls, 'expected_title': task['title']})
        rows = result.get('documents', [])
        if not isinstance(rows, list):
            rows = []
        by_url = {row['url']: row for row in rows if isinstance(row, dict) and row.get('url') in urls}
        return [by_url.get(url, {'url': url, 'status': 'unavailable',
                                'reason': result.get('reason', 'missing_local_fetch_result')}) for url in urls]
