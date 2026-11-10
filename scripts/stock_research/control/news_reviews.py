"""Import source-grounded analyst reviews without turning headlines into facts."""
from pathlib import Path
from urllib.parse import urlsplit
import json
import pandas as pd

from ..common.jsonio import read_json
from ..common.timeutil import parse_dt, now_utc
from ..context.news_impact import evaluate_event_impact, validate_grade_sign
from ..config import load_policies
from ..contracts import ContextEvent, EvidenceScope, Grade, SourceKind, to_dict
from ..storage.curated_store import CuratedStore


def import_news_reviews(path, runtime_root):
    reviews = read_json(path)
    if not isinstance(reviews, list) or not reviews:
        raise ValueError('news reviews must be a nonempty list')
    assert load_policies().source_allowed('user_import', 'US', 'news')
    events = []
    for review in reviews:
        sources = review.get('sources', [])
        if not sources or not review.get('fact_summary') or not review.get('grade_reason'):
            raise ValueError('a review requires source citations, facts, and an explained grade')
        scope = EvidenceScope(review.get('evidence_scope', 'none'))
        if scope not in {EvidenceScope.FULL_TEXT, EvidenceScope.OFFICIAL_EXCERPT, EvidenceScope.SOURCE_EXCERPT, EvidenceScope.NUMERIC}:
            raise ValueError('headline/search-snippet evidence cannot qualify a news review')
        reviewed_at = parse_dt(review['reviewed_at'])
        if review.get('impact_assessment'):
            evaluate_event_impact(review['impact_assessment'],reviewed_at)
            validate_grade_sign({**review['impact_assessment'],'grade':review['grade']})
        times = []
        for source in sources:
            parts = urlsplit(source['url'])
            if parts.scheme != 'https' or not parts.netloc or not source.get('evidence_locator'):
                raise ValueError('source requires HTTPS original URL and an evidence locator')
            published = parse_dt(source['published_at'])
            if published is None or published > reviewed_at:
                raise ValueError('source publication cannot follow review time')
            times.append(published)
        event = ContextEvent(event_id='', market=review.get('market', 'US'),
            security_ids=review['security_ids'], title=review['title'],
            published_at=max(times), available_at=reviewed_at, received_at=reviewed_at,
            source_id='user_import', source_kind=SourceKind(review.get('source_kind', 'licensed')),
            evidence_scope=scope, url=sources[0]['url'], body='',
            evidence_locator='; '.join(source['evidence_locator'] for source in sources),
            event_cluster=review['event_cluster'], grade=Grade(review['grade']),
            grade_method='source_review', grade_version='source-review-v1',
            grade_reason=review['grade_reason'], fact_summary=review['fact_summary'],
            permissions={'review_sources':sources,'reviewed_at':reviewed_at.isoformat(),
                'conditions':review.get('conditions', ''), 'discovered_titles':review.get('discovered_titles', []),
                'impact_assessment':review.get('impact_assessment'), 'impact_reason':review.get('impact_reason',''),
                'workflow_task_id':review.get('workflow_task_id'),
                'review_engine':review.get('review_engine','host_agent'),
                'review_prompt_version':review.get('review_prompt_version','news-impact-v1'),
                'historical_training_ready':False,'store_full_text':False})
        events.append(to_dict(event))
    store = CuratedStore(runtime_root)
    frame = pd.DataFrame(events)
    for column in ('security_ids', 'permissions', 'price_reaction'):
        frame[column] = frame[column].map(lambda value: json.dumps(value, ensure_ascii=False))
    old = store.catalog.get_dataset('news')
    if old:
        frame = pd.concat([store.load('news'), frame], ignore_index=True)
    version = store.save('news', frame, {'source_id':'user_import','review_method':'source-review-v1',
        'evidence_file':str(Path(path).resolve()),'new_reviews':len(events),
        'historical_training_ready':False}, source_manifests=[str(Path(path).resolve())])
    return {'version':version,'reviews_imported':len(events),'rows':len(frame)}


def retract_news_reviews(runtime_root, task_reasons):
    """Append a dated retraction; older reports and source reviews stay intact."""
    store = CuratedStore(runtime_root)
    frame = store.load('news')
    rows = frame.to_dict('records')
    additions = []
    stamp = now_utc().isoformat()
    for task_id, reason in task_reasons:
        matches = []
        for row in rows:
            permissions = row.get('permissions',{})
            if isinstance(permissions,str):
                permissions = json.loads(permissions)
            if permissions.get('workflow_task_id') == task_id:
                matches.append((row,permissions))
        if not matches:
            continue
        row, permissions = max(matches,key=lambda pair:parse_dt(pair[0]['available_at']))
        additions.append({**row,'event_id':'','available_at':stamp,'received_at':stamp,
            'grade':Grade.INSUFFICIENT.value,'grade_method':'review_retracted','grade_reason':reason,
            'evidence_scope':EvidenceScope.NONE.value,
            'permissions':json.dumps({**permissions,'impact_assessment':None,'review_status':'retracted'},ensure_ascii=False)})
    if additions:
        store.save('news',pd.concat([frame,pd.DataFrame(additions)],ignore_index=True),
            {'source_id':'user_import','review_method':'dated-retraction-v1','new_retractions':len(additions)})
