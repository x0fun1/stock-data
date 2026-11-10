"""Durable per-headline search/LLM workflow, supplied by a tool-capable host agent."""
from pathlib import Path
import json
import pandas as pd

from ..common.hashing import canonical_hash
from ..common.jsonio import read_json,write_json
from ..common.timeutil import now_utc,parse_dt
from ..context.news import event_id_for
from ..context.news_impact import evaluate_event_impact, validate_grade_sign
from ..storage.curated_store import CuratedStore
from .ingest import news_from_frame
from .news_reviews import import_news_reviews, retract_news_reviews

TERMINAL = {'verified','unrelated','unresolved'}


def prepare_news_workflow(root,ticker='NVDA',workflow_id='news-nvda-20261108'):
    root=Path(root)
    store=CuratedStore(root)
    entry=store.metadata('news')
    events=news_from_frame(store.load('news'))
    security_ids={identity['security_id'] for identity in store.load('securities').to_dict('records') if identity['ticker'].upper()==ticker.upper()}
    tasks=[]
    for event in sorted(events,key=lambda item:item.published_at or now_utc(),reverse=True):
        if event.source_id!='finnhub' or not security_ids.intersection(event.security_ids):
            continue
        tasks.append({'task_id':event.event_id or event_id_for(event),'title':event.title,
            'source_url':event.url,'published_at':event.published_at.isoformat() if event.published_at else None,
            'security_ids':event.security_ids,'ticker':ticker,'search_query':event.title,
            'status':'pending','search_attempts':[],'review':None})
    folder=root/'news-workflows'/workflow_id
    manifest={'workflow_id':workflow_id,'ticker':ticker,'source_version':entry['version'],
        'task_ids':[task['task_id'] for task in tasks],'all_source_headlines_retained':True,
        'host_route':'one title search via available free search MCP, read original/official evidence, per-item LLM assessment, apply, finalize',
        'cleanup_enabled':False,'reviews_are_not_trading_probabilities':True}
    if (folder/'manifest.json').exists():
        old=read_json(folder/'manifest.json')
        if old['task_ids']!=manifest['task_ids']:
            raise ValueError('workflow source headlines changed; use a new workflow ID')
    else:
        write_json(folder/'manifest.json',manifest)
        write_json(folder/'tasks.json',tasks)
    write_json(root/'news-workflows'/f'{ticker.upper()}-latest.json',{'folder':str(folder.resolve()),'workflow_id':workflow_id})
    return workflow_status(folder)


def workflow_status(folder):
    tasks=read_json(Path(folder)/'tasks.json')
    counts={status:sum(task['status']==status for task in tasks) for status in ['pending',*sorted(TERMINAL)]}
    return {'workflow_id':Path(folder).name,'total':len(tasks),'counts':counts,
        'completed':sum(counts[status] for status in TERMINAL),'all_processed':all(task['status'] in TERMINAL for task in tasks),
        'task_state_sha256':canonical_hash(tasks)}


def next_news_tasks(folder,limit=8):
    return [task for task in read_json(Path(folder)/'tasks.json') if task['status']=='pending'][:limit]


def apply_news_assessments(root,folder,path):
    folder=Path(folder)
    tasks=read_json(folder/'tasks.json')
    by_id={task['task_id']:task for task in tasks}
    incoming=read_json(path)
    accepted=[]
    retracted=[]
    seen=set()
    for assessment in incoming:
        if assessment['task_id'] in seen:
            raise ValueError('duplicate task assessment in a batch')
        seen.add(assessment['task_id'])
        task=by_id.get(assessment['task_id'])
        if task is None:
            raise ValueError('unknown headline task')
        status=assessment['status']
        if status not in TERMINAL or not assessment.get('reason') or not assessment.get('search_attempts'):
            raise ValueError('every completed item requires status, specific reason, and search trace')
        if status=='verified':
            review=assessment['review']
            if not review.get('impact_reason'):
                raise ValueError('LLM must explain impact magnitude separately from sentiment')
            evaluated=evaluate_event_impact(review,review['reviewed_at'])
            validate_grade_sign(review)
            if not any(attempt.get('kind') in {'article_read','original_read','official_read'} for attempt in assessment['search_attempts']):
                raise ValueError('verified review requires a read evidence trace')
            if not review.get('event_cluster'):
                raise ValueError('semantic event cluster required to avoid counting reprints')
            review['security_ids']=task['security_ids']
            review['discovered_titles']=[task['title']]
            review['workflow_task_id']=task['task_id']
            review['impact_assessment']={key:review[key] for key in ['direction_sign','impact_level','event_kind','event_time','stock_relevance','evidence_confidence']}
            accepted.append(review)
        elif status=='unrelated' and assessment.get('stock_relevance',0)!=0:
            raise ValueError('unrelated items must have zero stock relevance')
        if status!='verified' and task['status']=='verified':
            retracted.append((task['task_id'],assessment['reason']))
        if task['status']!='pending':
            task.setdefault('assessment_history',[]).append({key:task.get(key) for key in ('status','search_attempts','reason','review','reviewed_at')})
        task.update(status=status,search_attempts=assessment['search_attempts'],reason=assessment['reason'],
            review=assessment.get('review'),reviewed_at=now_utc().isoformat())
    # Validate the entire batch before writing task state.
    if accepted:
        review_path=folder/'review-batches'/f"{canonical_hash(accepted)[:16]}.json"
        write_json(review_path,accepted)
        import_news_reviews(review_path,root)
    if retracted:
        retract_news_reviews(root,retracted)
    write_json(folder/'tasks.json',tasks)
    write_json(folder/'coverage.json',workflow_status(folder))
    return workflow_status(folder)


def finalize_news_workflow(folder):
    folder=Path(folder)
    status=workflow_status(folder)
    if not status['all_processed']:
        raise ValueError('cannot finalize while headline tasks remain pending')
    write_json(folder/'completion.json',{**status,'completed_at':now_utc().isoformat(),
        'manifest_sha256':canonical_hash(read_json(folder/'manifest.json')),'cleanup_performed':False})
    return status


def latest_workflow_coverage(root,ticker):
    pointer=Path(root)/'news-workflows'/f'{ticker.upper()}-latest.json'
    if not pointer.exists():
        return {}
    folder=Path(read_json(pointer)['folder'])
    if not folder.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('news workflow pointer leaves runtime root')
    status=workflow_status(folder)
    completion=read_json(folder/'completion.json') if (folder/'completion.json').exists() else {}
    return {**status,'unresolved_items':[{'task_id':task['task_id'],'title':task['title'],
        'reason':task.get('reason'),'source_url':task.get('source_url')} for task in read_json(folder/'tasks.json') if task['status']=='unresolved'],
        'finalized':bool(completion) and completion.get('task_state_sha256')==status['task_state_sha256'],
        'manifest_sha256':canonical_hash(read_json(folder/'manifest.json'))}


def apply_workflow_dispositions(events,root,tickers,asof):
    """Attach completed headline decisions only after their actual review time."""
    dispositions={}
    for ticker in tickers:
        pointer=Path(root)/'news-workflows'/f'{ticker.upper()}-latest.json'
        if not pointer.exists():continue
        folder=Path(read_json(pointer)['folder'])
        if not folder.resolve().is_relative_to(Path(root).resolve()):
            raise ValueError('news workflow pointer leaves runtime root')
        for task in read_json(folder/'tasks.json'):
            states=[*task.get('assessment_history',[]),task]
            eligible=[state for state in states if state.get('reviewed_at') and parse_dt(state['reviewed_at'])<=parse_dt(asof)]
            if eligible:dispositions[task['task_id']]=max(eligible,key=lambda state:parse_dt(state['reviewed_at']))
    selected=[]
    for event in events:
        disposition=dispositions.get(event.event_id or event_id_for(event)) if event.source_id=='finnhub' else None
        if disposition:
            if disposition['status']=='unrelated':continue
            event.permissions={**event.permissions,'workflow_status':disposition['status'],
                'workflow_reason':disposition.get('reason')}
            if disposition.get('review'):event.event_cluster=disposition['review']['event_cluster']
        selected.append(event)
    return selected
