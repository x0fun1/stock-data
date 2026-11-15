import json
import socket
import sys
import threading
from types import SimpleNamespace

import pytest
import requests

from stock_research.acquisition.local_search_fetch import LocalSearchFetch, local_endpoint
from stock_research.acquisition.local_service import LocalService, extract_document, public_article_url
from stock_research.acquisition.news_collector import collect_news_evidence
from stock_research.common.jsonio import read_json, write_json


def test_local_contract_preserves_per_url_errors_and_excludes_analysis():
    calls = []
    class Session:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            data = {'sources': [{'url': 'https://example.com/a', 'title': 'Company announcement'}]}
            if url.endswith('/fetch'):
                data = {'documents': [{'url': 'https://example.com/a', 'status': 'fetched', 'markdown': 'Evidence'},
                                      {'url': 'https://example.com/b', 'status': 'unavailable', 'reason': 'http_403'}]}
            return SimpleNamespace(status_code=200, json=lambda: data)
    client = LocalSearchFetch(session=Session())
    task = {'title': 'Company announcement', 'review': 'DO_NOT_SEND'}
    assert client.search(task, task['title'])['sources'][0]['url'] == 'https://example.com/a'
    rows = client.fetch(task, ['https://example.com/a', 'https://example.com/b', 'https://example.com/c'])
    assert rows[1]['reason'] == 'http_403'
    assert rows[2]['reason'] == 'missing_local_fetch_result'
    assert 'DO_NOT_SEND' not in json.dumps(calls)
    assert all(kwargs['allow_redirects'] is False for _, kwargs in calls)


@pytest.mark.parametrize('url', ['https://remote.example/search', 'http://user:password@localhost/search',
                                'http://localhost/search?token=secret'])
def test_local_endpoints_reject_remote_or_credential_urls(url):
    with pytest.raises(ValueError, match='loopback'):
        local_endpoint(url)


def test_provider_unavailable_does_not_seal_a_failed_batch(tmp_path):
    write_json(tmp_path / 'news-workflows/news/tasks.json', [
        {'task_id': 'one', 'title': 'Company news', 'ticker': 'NVDA'}])
    class Session:
        def get(self, *args, **kwargs):
            raise requests.ConnectionError('offline')
    with pytest.raises(RuntimeError, match='start news-local-serve'):
        collect_news_evidence(tmp_path, 'news', 'local', client=LocalSearchFetch(session=Session()))
    assert not (tmp_path / 'news-workflows/news/collections/local/completion.json').exists()


def test_parallel_source_url_fetch_retains_order_and_resumes_without_network(tmp_path):
    tasks = [{'task_id': str(i), 'title': f'Company event {i}', 'ticker': 'NVDA',
              'source_url': f'https://example.com/{i}', 'published_at': '2026-11-01T12:00:00Z',
              'review': {'grade': 'DO_NOT_SEND'}} for i in range(2)]
    write_json(tmp_path / 'news-workflows/news/tasks.json', tasks)
    barrier = threading.Barrier(2)
    class Provider:
        name = 'parallel_local_test'
        supports_concurrency = True
        prefer_source_url = True
        fetches = 0
        def search(self, *args):
            raise AssertionError('A readable provided article must not need search')
        def fetch(self, task, urls):
            assert 'review' not in task
            self.fetches += 1
            barrier.wait(timeout=3)  # A sequential regression fails, without a timing benchmark.
            return [{'url': urls[0], 'status': 'fetched', 'markdown': 'Publisher evidence. ' * 100}]
    client = Provider()
    result = collect_news_evidence(tmp_path, 'news', 'local', client=client, workers=2)
    assert result['fetched_for_review'] == 2 and result['search_requests'] == 0
    records = read_json(tmp_path / 'news-workflows/news/collections/local/collection.json')
    assert [row['task_id'] for row in records] == ['0', '1']
    assert all(row['documents'][0]['published_at'] is None for row in records)
    assert all(row['searches'][0]['result']['status'] == 'not_searched' for row in records)
    assert collect_news_evidence(tmp_path, 'news', 'local', client=client, workers=2) == result
    assert client.fetches == 2


def test_direct_fetch_failure_falls_back_to_title_search(tmp_path):
    write_json(tmp_path / 'news-workflows/news/tasks.json', [
        {'task_id': 'one', 'title': 'Company event', 'ticker': 'NVDA', 'source_url': 'https://example.com/blocked'}])
    class Provider:
        name = 'local_fallback_test'
        prefer_source_url = True
        def search(self, task, query):
            return {'sources': [{'url': 'https://example.com/original'}]}
        def fetch(self, task, urls):
            return [{'url': url, 'status': 'unavailable', 'reason': 'http_403'} if url.endswith('blocked')
                    else {'url': url, 'status': 'fetched', 'markdown': 'Official evidence.'} for url in urls]
    result = collect_news_evidence(tmp_path, 'news', 'local', client=Provider())
    assert result['fetched_for_review'] == 1 and result['search_requests'] == 1
    record = read_json(tmp_path / 'news-workflows/news/collections/local/items/one.json')
    assert record['documents'][0]['reason'] == 'http_403'
    assert record['searches'][1]['query'] == 'Company event'


def test_ddgs_searches_have_no_fixed_wait(monkeypatch):
    queries = []
    class DDGS:
        def __init__(self, **kwargs):
            pass
        def text(self, query, **kwargs):
            queries.append(query)
            return [{'href': 'https://example.com/news', 'title': 'News', 'body': 'Snippet'}]
    monkeypatch.setitem(sys.modules, 'ddgs', SimpleNamespace(DDGS=DDGS))
    monkeypatch.setattr('time.sleep', lambda *_: pytest.fail('fixed search sleep'))
    service = LocalService()
    try:
        assert service.search('one')['sources'][0]['url'] == 'https://example.com/news'
        assert service.search('two')['status'] == 'ok'
        assert queries == ['one', 'two']
    finally:
        service.close()


def test_html_extraction_preserves_offset_and_rejects_short_or_blocked_text():
    def extractor(text):
        return lambda *args, **kwargs: json.dumps({'text': text, 'title': 'News', 'date': '2026-11-01'})
    html = '<meta property="article:published_time" content="2026-11-01T16:00:00-05:00">'
    result = extract_document(html, 'https://example.com/news', extractor('Relevant publisher facts. ' * 50))
    assert result['published_at'] == '2026-11-01T16:00:00-05:00'
    assert result['semantic_review_complete'] is False
    assert len(result['content_sha256']) == 64
    assert extract_document('', 'https://example.com', extractor('Snippet'))['status'] == 'unavailable'
    assert extract_document('', 'https://example.com', extractor('Verify you are human. ' * 50))['reason'] == 'access_restricted'


def test_redirect_to_private_address_is_rejected_before_request(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ValueError, match='non-public'):
        public_article_url('https://example.com/private')
    with pytest.raises(ValueError, match='HTTPS'):
        public_article_url('http://example.com')


def test_base_cli_defaults_to_local_with_optional_service_dependencies():
    from research import build_parser
    args = build_parser().parse_args(['news-collect', '--workflow-id', 'news', '--collection-id', 'local'])
    assert args.provider == 'local'
    args = build_parser().parse_args(['news-local-serve'])
    assert args.search_workers == args.fetch_workers == 32


def test_vpn_fake_ip_opt_in_does_not_allow_private_or_literal_urls(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.42', 443))])
    with pytest.raises(ValueError, match='non-public'):
        public_article_url('https://nvidianews.nvidia.com/news')
    assert public_article_url('https://nvidianews.nvidia.com/news', True).startswith('https://')
    for url in ['https://198.18.0.42/news', 'https://localhost/news', 'https://router.local/news']:
        with pytest.raises(ValueError):
            public_article_url(url, True)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.1', 443))])
    with pytest.raises(ValueError):
        public_article_url('https://example.com/news', True)


def test_public_fetch_stops_before_following_a_private_redirect(monkeypatch):
    calls = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])
    class Response:
        status_code = 302
        headers = {'Location': 'https://127.0.0.1/private'}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def get(url, **kwargs):
        calls.append(url)
        assert kwargs['allow_redirects'] is False
        return Response()
    monkeypatch.setattr(requests, 'get', get)
    service = LocalService()
    try:
        result = service.fetch_one('https://example.com/news')
        assert result['status'] == 'unavailable'
        assert calls == ['https://example.com/news']
    finally:
        service.close()


def test_fetch_service_processes_urls_concurrently_but_keeps_request_order():
    service = LocalService(fetch_workers=2)
    barrier = threading.Barrier(2)
    def fetch(url):
        barrier.wait(timeout=3)
        return {'url': url, 'status': 'fetched', 'markdown': 'Publisher body'}
    service.fetch_one = fetch
    try:
        urls = ['https://example.com/one', 'https://example.com/two']
        assert [row['url'] for row in service.fetch(urls)['documents']] == urls
    finally:
        service.close()


def test_32_title_workers_can_run_without_reordering_frozen_tasks(tmp_path):
    tasks = [{'task_id': str(i), 'title': f'Company event {i}', 'ticker': 'NVDA',
              'source_url': f'https://example.com/{i}'} for i in range(32)]
    write_json(tmp_path / 'news-workflows/news/tasks.json', tasks)
    barrier = threading.Barrier(32)
    class Provider:
        name = 'parallel_32_test'
        supports_concurrency = True
        prefer_source_url = True
        def fetch(self, task, urls):
            barrier.wait(timeout=5)
            return [{'url': urls[0], 'status': 'fetched', 'markdown': 'Publisher evidence.'}]
        def search(self, *args):
            raise AssertionError('Readable source URLs do not need search')
    service = LocalService()
    try:
        assert service.health()['search_workers'] == 32
        assert service.health()['fetch_workers'] == 32
        result = collect_news_evidence(tmp_path, 'news', 'workers32', client=Provider())
        assert result['fetched_for_review'] == 32
        assert result['workers'] == 32
        records = read_json(tmp_path / 'news-workflows/news/collections/workers32/collection.json')
        assert [row['task_id'] for row in records] == [str(i) for i in range(32)]
    finally:
        service.close()
