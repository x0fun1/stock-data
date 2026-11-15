import json
from pathlib import Path

import pytest

from stock_research.acquisition.news_collector import collect_news_evidence, TinyFishSearchFetch, prepare_plugin_title_plan, _publication_metadata, _public_url
from stock_research.common.jsonio import read_json, write_json


def workflow(tmp_path):
    root=tmp_path/'runtime'
    folder=root/'news-workflows/news'
    write_json(folder/'tasks.json',[{'task_id':str(i),'title':f'Nvidia event {i}','ticker':'NVDA',
        'published_at':'2026-11-01T12:00:00Z','review':{'grade':'DO_NOT_USE_ANALYSIS'}} for i in range(3)])
    return root,folder


class Provider:
    name='fake_provider'
    def __init__(self):self.queries=[];self.fetches=[]
    def search(self,task,query):
        self.queries.append((task['task_id'],query))
        if task['task_id']=='1' and query==task['title']:return {'sources':[]}
        return {'sources':[{'url':f"https://example.com/{task['task_id']}",'title':task['title']}]}
    def fetch(self,task,urls):
        self.fetches.extend(urls)
        if task['task_id']=='2':return [{'url':url,'status':'unavailable','reason':'http_403'} for url in urls]
        return [{'url':url,'title':task['title'],'status':'fetched','markdown':'Body evidence for the company. '*100} for url in urls]


def test_program_collects_all_titles_before_analysis_and_can_resume(tmp_path):
    root,source=workflow(tmp_path);provider=Provider()
    result=collect_news_evidence(root,'news','collection',client=provider)
    assert result['total_titles']==3 and result['fetched_for_review']==2 and result['fetch_unresolved']==1
    assert result['llm_called'] is False and result['cleanup_performed'] is False
    assert {task for task,query in provider.queries}=={'0','1','2'}
    assert len(provider.queries)==5  # Fallback for missing search results and unavailable body.
    records=read_json(source/'collections/collection/collection.json')
    assert all(not row['sentiment_assessed'] for row in records)
    assert records[2]['documents'][0]['reason']=='http_403'
    markdown=Path(result['markdown']).read_text()
    assert 'DO_NOT_USE_ANALYSIS' not in markdown
    assert records[0]['documents'][0]['excerpt_truncated'] is True
    prior=len(provider.queries)
    assert collect_news_evidence(root,'news','collection',client=provider)==result
    assert len(provider.queries)==prior


def test_changed_title_cannot_overwrite_collected_evidence(tmp_path):
    root,source=workflow(tmp_path)
    collect_news_evidence(root,'news','collection',client=Provider())
    tasks=read_json(source/'tasks.json');tasks[0]['title']='Different'
    write_json(source/'tasks.json',tasks)
    with pytest.raises(ValueError,match='protocol changed'):collect_news_evidence(root,'news','collection',client=Provider())


def test_completed_collection_detects_modified_evidence(tmp_path):
    root,source=workflow(tmp_path)
    collect_news_evidence(root,'news','collection',client=Provider())
    path=source/'collections/collection/items/0.json'
    item=read_json(path);item['documents'][0]['excerpt']='Changed evidence'
    write_json(path,item)
    with pytest.raises(ValueError,match='evidence changed'):collect_news_evidence(root,'news','collection',client=Provider())


def test_provider_api_uses_search_then_markdown_fetch_and_retains_per_url_errors(monkeypatch):
    calls=[]
    class Response:
        status_code=200
        def __init__(self,data):self.data=data
        def json(self):return self.data
    class Session:
        def request(self,method,url,**kwargs):
            calls.append((method,url,kwargs))
            if method=='GET':return Response({'results':[{'url':'https://example.com/a','title':'Title','date':'2026-11-01'}]})
            return Response({'results':[{'url':'https://example.com/a','title':'Title','text':'Actual article body'}],
                'errors':[{'url':'https://example.com/b','code':'access_denied'}]})
    client=TinyFishSearchFetch(api_key='test-secret',session=Session())
    assert len(client.search({},'Original title')['sources'])==1
    documents=client.fetch({},['https://example.com/a','https://example.com/b'])
    assert documents[1]['reason']=='access_denied'
    assert calls[0][2]['params']['query']=='Original title'
    assert calls[1][2]['json']['format']=='markdown'
    assert all(call[2]['verify'] for call in calls)
    assert 'test-secret' not in json.dumps(documents)


def test_authentication_or_allowance_failure_stops_without_retry():
    class Session:
        count=0
        def request(self,*args,**kwargs):
            self.count+=1
            return type('Response',(),{'status_code':402})()
    session=Session();client=TinyFishSearchFetch(api_key='test-secret',session=session)
    with pytest.raises(RuntimeError,match='batch stopped'):client.search({},'Title')
    assert session.count==1


def plugin_batch(root,source):
    folder=source/'plugin-batches/native'
    write_json(folder/'manifest.json',{'provider':'tinyfish_connected_plugin','connected_authorization':True})
    write_json(folder/'driver-result.json',{'all_collected':True,'total':3,'fatal_error':None})
    for task in read_json(source/'tasks.json'):
        url=f"https://example.com/{task['task_id']}"
        write_json(folder/'items'/f"{task['task_id']}.json",{'task_id':task['task_id'],'title':task['title'],
            'searches':[{'query':task['title'],'searched_at':'2026-11-08T10:00:00Z','response_sha256':'search-hash',
                'result':{'status':'ok','sources':[{'url':url,'title':task['title'],'published_at':'Fri, 06 Nov 2026 21:00:00 GMT'}]}}],
            'documents':[{'url':url,'status':'fetched','title':task['title'],'excerpt':'Native fetched evidence.',
                'content_sha256':'original-content-hash','semantic_review_complete':False}]})
    return folder


def test_connected_plugin_batch_does_not_require_an_independent_api_key(tmp_path,monkeypatch):
    root,source=workflow(tmp_path);folder=plugin_batch(root,source)
    monkeypatch.delenv('TINYFISH_API_KEY',raising=False)
    result=collect_news_evidence(root,'news','native-collection',provider='tinyfish-plugin',plugin_batch_id='native')
    assert result['total_titles']==3 and result['fetched_for_review']==3
    assert result['is_live_provider_run'] is True and result['python_network_calls'] is False
    item=read_json(source/'collections/native-collection/items/0.json')
    assert item['documents'][0]['content_sha256']=='original-content-hash'
    assert item['documents'][0]['published_at']=='2026-11-06T21:00:00+00:00'
    assert item['documents'][0]['publication_time_basis']=='search_metadata'


def test_incomplete_or_invalid_plugin_batch_cannot_be_promoted(tmp_path):
    root,source=workflow(tmp_path);folder=plugin_batch(root,source)
    write_json(folder/'driver-result.json',{'all_collected':False,'total':3})
    with pytest.raises(ValueError,match='incomplete'):
        collect_news_evidence(root,'news','native-collection',provider='tinyfish-plugin',plugin_batch_id='native')
    write_json(folder/'driver-result.json',{'all_collected':True,'total':3})
    write_json(folder/'invalid-driver.json',{'use_for_evidence':False})
    with pytest.raises(ValueError,match='invalid driver'):
        collect_news_evidence(root,'news','native-collection',provider='tinyfish-plugin',plugin_batch_id='native')


def test_title_plan_excludes_prior_analysis_and_freezes_original_titles(tmp_path):
    root,source=workflow(tmp_path)
    plan=prepare_plugin_title_plan(root,'news','new')
    assert len(plan['tasks'])==3 and all('review' not in task for task in plan['tasks'])
    assert plan['llm_in_collection'] is False
    tasks=read_json(source/'tasks.json');tasks[0]['title']='Changed title';write_json(source/'tasks.json',tasks)
    with pytest.raises(ValueError,match='title plan changed'):prepare_plugin_title_plan(root,'news','new')


def test_publication_dates_require_an_explicit_timezone():
    assert _publication_metadata('Fri, 06 Nov 2026 21:00:00 GMT')=='2026-11-06T21:00:00+00:00'
    assert _publication_metadata('2026-11-06T16:00:00-05:00')=='2026-11-06T21:00:00+00:00'
    assert _publication_metadata('2026-11-06') is None


def test_quote_hubs_are_not_accepted_as_source_articles():
    assert not _public_url('https://finance.yahoo.com/quote/NVDA/news/')
    assert not _public_url('https://seekingalpha.com/symbol/NVDA')
    assert _public_url('https://finance.yahoo.com/markets/stocks/articles/nvidia-report-123.html')


def test_access_denial_body_triggers_search_fallback(tmp_path):
    root, source = workflow(tmp_path)
    tasks = read_json(source / 'tasks.json')[:1]
    write_json(source / 'tasks.json', tasks)
    class AccessProvider(Provider):
        def fetch(self, task, urls):
            if len(self.queries) == 1:
                return [{'url': url, 'status': 'fetched', 'markdown': 'Verify you are human. Access denied.'} for url in urls]
            return [{'url': url, 'status': 'fetched', 'markdown': 'Official announcement and relevant details.'} for url in urls]
        def search(self, task, query):
            self.queries.append((task['task_id'], query))
            return {'sources': [{'url': f'https://example.com/{len(self.queries)}'}]}
    provider = AccessProvider()
    result = collect_news_evidence(root, 'news', 'fallback', client=provider)
    assert len(provider.queries) == 2 and result['fetched_for_review'] == 1
    record = read_json(source / 'collections/fallback/items/0.json')
    assert record['documents'][0]['reason'] == 'access_restricted'
