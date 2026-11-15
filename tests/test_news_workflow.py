import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from stock_research.common.jsonio import read_json, write_json
from stock_research.context.news_impact import evaluate_event_impact, synthesize_news_impacts
from stock_research.contracts import ContextEvent
from stock_research.control.ingest import news_from_frame
from stock_research.control.news_workflow import (prepare_news_workflow, next_news_tasks,
    apply_news_assessments, finalize_news_workflow, apply_workflow_dispositions)
from stock_research.storage import CuratedStore
from stock_research.acquisition.article_reader import read_public_article


def assessment(sign=1, event_time='2026-11-01T12:00:00Z', confidence=.8):
    return {'direction_sign':sign,'impact_level':'medium','event_kind':'opinion',
        'event_time':event_time,'stock_relevance':1.,'evidence_confidence':confidence}


def reviewed_event(task='a', cluster='same', at='2026-11-02T12:00:00Z', **impact):
    return ContextEvent(event_id=task,market='US',security_ids=['US:XNAS:NVDA'],title=task,
        available_at=at,published_at=at,received_at=at,source_id='user_import',source_kind='user_import',evidence_scope='source_excerpt',
        grade='中度利好',event_cluster=cluster,
        permissions={'workflow_task_id':task,'impact_assessment':assessment(**impact)})


def test_time_decay_reprints_and_reliable_source_selection():
    old=reviewed_event(confidence=.9)
    reprint=reviewed_event(task='b',at='2026-11-03T12:00:00Z',confidence=.4,
        event_time='2026-11-03T12:00:00Z')
    result=synthesize_news_impacts([old,reprint],'2026-11-03T12:00:00Z')
    assert result['event_count']==1 and result['deduplicated_reprints']==1
    assert result['events'][0]['event_id']=='a'
    assert result['events'][0]['time_decay']==pytest.approx(.5)
    assert result['net_contribution']==pytest.approx(.9)
    assert result['policy_sha256'] and result['is_probability'] is False


def test_future_and_retracted_assessments_do_not_contribute():
    original=reviewed_event()
    retracted=reviewed_event(at='2026-11-03T12:00:00Z')
    retracted.permissions['impact_assessment']=None
    assert synthesize_news_impacts([original,retracted],'2026-11-02T18:00:00Z')['event_count']==1
    assert synthesize_news_impacts([original,retracted],'2026-11-04T12:00:00Z')['event_count']==0


@pytest.mark.parametrize('change',[
    {'event_time':'2027-01-01T00:00:00Z'},{'evidence_confidence':float('nan')},
    {'stock_relevance':1.5},{'direction_sign':True}])
def test_invalid_impact_is_rejected(change):
    with pytest.raises(ValueError):evaluate_event_impact({**assessment(),**change},'2026-11-03T00:00:00Z')


def make_workflow(tmp_path):
    root=tmp_path/'runtime'
    store=CuratedStore(root)
    store.save('securities',[{'security_id':'US:XNAS:NVDA','ticker':'NVDA'}])
    store.save('news',[{'event_id':'title-a','market':'US',
        'security_ids':json.dumps(['US:XNAS:NVDA']),'title':'Partnership',
        'published_at':'2026-11-01T12:00:00Z','available_at':'2026-11-01T12:00:00Z',
        'source_id':'finnhub','source_kind':'licensed','evidence_scope':'title',
        'url':'https://finnhub.io/a','permissions':'{}'}])
    prepare_news_workflow(root,workflow_id='test')
    return root,root/'news-workflows/test'


def review():
    return {**assessment(),'title':'Partnership','event_cluster':'partner',
        'fact_summary':'Company announced a partnership; revenue impact undisclosed.',
        'grade':'中度利好','grade_reason':'Potential adoption improves.',
        'impact_reason':'Moderate potential scale; revenue remains uncertain.',
        'reviewed_at':'2026-11-03T12:00:00Z','evidence_scope':'source_excerpt',
        'sources':[{'url':'https://example.com/announcement','published_at':'2026-11-01T12:00:00Z',
            'evidence_locator':'original announcement paragraph 2'}]}


def test_workflow_covers_every_title_and_preserves_reassessment(tmp_path):
    root,folder=make_workflow(tmp_path)
    assert len(next_news_tasks(folder))==1
    with pytest.raises(ValueError,match='pending'):finalize_news_workflow(folder)
    path=tmp_path/'assessment.json'
    batch=[{'task_id':'title-a','status':'verified','reason':'Read announcement.',
        'search_attempts':[{'kind':'search_MCP','query':'Partnership'}, {'kind':'article_read','url':'https://example.com/announcement'}],
        'review':review()}]
    write_json(path,batch)
    assert apply_news_assessments(root,folder,path)['all_processed'] is True
    finalize_news_workflow(folder)
    assert read_json(folder/'completion.json')['cleanup_performed'] is False
    batch[0].update(status='unresolved',reason='Source later retracted the announcement.',review=None)
    write_json(path,batch);apply_news_assessments(root,folder,path)
    tasks=read_json(folder/'tasks.json')
    assert tasks[0]['assessment_history'][0]['status']=='verified'
    events=news_from_frame(CuratedStore(root).load('news'))
    assert synthesize_news_impacts(events,datetime.now(timezone.utc))['event_count']==0
    assert len(events)==3  # Original discovery, review and dated retraction all retained.


@pytest.mark.parametrize('grade,sign',[('中度利空',1),('中度利好',0),('中性',-1)])
def test_inconsistent_grade_is_rejected_without_writing_state(tmp_path,grade,sign):
    root,folder=make_workflow(tmp_path)
    entry=review();entry.update(grade=grade,direction_sign=sign)
    path=tmp_path/'bad.json'
    write_json(path,[{'task_id':'title-a','status':'verified','reason':'reason',
        'search_attempts':[{'kind':'article_read'}],'review':entry}])
    with pytest.raises(ValueError,match='contradicts'):apply_news_assessments(root,folder,path)
    assert len(next_news_tasks(folder))==1
    assert len(CuratedStore(root).load('news'))==1


def test_article_reader_selects_main_jsonld_not_related_promotional_story():
    main={'headline':'Main results','articleBody':'Main company reported results and guidance. '*20,
        'datePublished':'2026-11-01T12:00:00Z'}
    related={'headline':'Next Nvidia','articleBody':'Unrelated promotional story '*30,
        'datePublished':'2026-11-06T12:00:00Z'}
    html='<title>Main results | Publisher</title><nav>NVDA marketing</nav><script type="application/ld+json">'+json.dumps([related,main])+'</script>'
    transport=SimpleNamespace(get=lambda *args,**kwargs:SimpleNamespace(ok=True,content=html.encode(),fetched_at=datetime.now(timezone.utc)))
    result=read_public_article('https://example.com/article',transport,expected_title='Main results')
    assert result['status']=='read' and 'promotional' not in result['body']
    assert result['published_at']=='2026-11-01T12:00:00+00:00'


def test_reader_keeps_access_failure_unavailable():
    html='<title>Article</title><article>Verify you are human '+('placeholder '*100)+'</article>'
    transport=SimpleNamespace(get=lambda *args,**kwargs:SimpleNamespace(ok=True,content=html.encode(),fetched_at=datetime.now(timezone.utc)))
    assert read_public_article('https://example.com/article',transport)['status']=='unavailable'


def test_unrelated_discovery_is_filtered_only_after_actual_review(tmp_path):
    root,folder=make_workflow(tmp_path)
    path=tmp_path/'unrelated.json'
    write_json(path,[{'task_id':'title-a','status':'unrelated','reason':'Other company; Nvidia only in advertisement.',
        'stock_relevance':0,'search_attempts':[{'kind':'search_MCP','query':'Partnership'}]}])
    apply_news_assessments(root,folder,path)
    events=news_from_frame(CuratedStore(root).load('news'))
    assert len(apply_workflow_dispositions(events,root,['NVDA'],'2026-11-01T12:00:00Z'))==1
    assert apply_workflow_dispositions(events,root,['NVDA'],datetime.now(timezone.utc))==[]


def test_replacement_workflow_can_withdraw_a_previously_verified_review(tmp_path):
    root, folder = make_workflow(tmp_path)
    path = tmp_path / 'assessment.json'
    write_json(path, [{'task_id': 'title-a', 'status': 'verified', 'reason': 'Read announcement.',
        'search_attempts': [{'kind': 'article_read'}], 'review': review()}])
    apply_news_assessments(root, folder, path)
    prepare_news_workflow(root, workflow_id='replacement')
    replacement = root / 'news-workflows/replacement'
    assert next_news_tasks(replacement)[0]['status'] == 'pending'
    write_json(path, [{'task_id': 'title-a', 'status': 'unrelated', 'reason': 'It concerns another company.',
        'stock_relevance': 0, 'search_attempts': [{'kind': 'article_read'}]}])
    apply_news_assessments(root, replacement, path)
    events = news_from_frame(CuratedStore(root).load('news'))
    assert synthesize_news_impacts(events, datetime.now(timezone.utc))['event_count'] == 0


def test_workflow_coverage_does_not_leak_post_cutoff_decisions(tmp_path):
    from stock_research.control.news_workflow import latest_workflow_coverage
    root, folder = make_workflow(tmp_path)
    manifest = read_json(folder / 'manifest.json')
    manifest['created_at'] = '2026-11-01T12:00:00Z'
    write_json(folder / 'manifest.json', manifest)
    tasks = read_json(folder / 'tasks.json')
    tasks[0].update(status='unresolved', reason='A later investigation.', reviewed_at='2026-11-08T12:00:00Z')
    write_json(folder / 'tasks.json', tasks)
    status = latest_workflow_coverage(root, 'NVDA', '2026-11-07T12:00:00Z')
    assert status['counts']['pending'] == 1 and status['counts']['unresolved'] == 0
    assert status['unresolved_items'] == [] and not status['finalized']
    assert latest_workflow_coverage(root, 'NVDA', '2026-10-01T12:00:00Z') == {}


def test_hk_imported_headlines_can_be_reviewed_without_a_security_table(tmp_path):
    root = tmp_path / 'hk'
    store = CuratedStore(root)
    store.save('news', [{'title': '公司公告', 'market': 'HK', 'source_id': 'user_import',
        'security_ids': ['HK:XHKG:0700'], 'published_at': '2026-11-01T12:00:00Z', 'evidence_scope': 'title'}])
    prepare_news_workflow(root, ticker='0700', workflow_id='hk', market='HK')
    folder = root / 'news-workflows/hk'
    task = next_news_tasks(folder)[0]
    assert task['market'] == 'HK' and task['security_ids'] == ['HK:XHKG:0700']
    entry = review()
    entry['title'] = '公司公告'
    path = tmp_path / 'hk-review.json'
    write_json(path, [{'task_id': task['task_id'], 'status': 'verified', 'reason': '阅读原公告。',
        'search_attempts': [{'kind': 'official_read'}], 'review': entry}])
    apply_news_assessments(root, folder, path)
    assert news_from_frame(store.load('news'))[-1].market.value == 'HK'
