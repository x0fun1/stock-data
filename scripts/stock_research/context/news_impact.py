"""Explicit event impact, time decay and deduplicated news synthesis."""
from __future__ import annotations
from collections import defaultdict
import math

from ..common.timeutil import parse_dt
from ..common.hashing import canonical_hash
from ..contracts import Grade

IMPACT_WEIGHTS = {'light':1., 'medium':2., 'major':4.}
HALF_LIVES = {'earnings':3., 'price_move':2., 'institutional_flow':3., 'opinion':2.,
    'regulation':14., 'partnership':14., 'capacity':45., 'calendar':7., 'other':7.}


def impact_policy(policy=None):
    policy = policy or {}
    return {'version':'news-impact-v1',
        'impact_weights':{**IMPACT_WEIGHTS, **policy.get('impact_weights',{})},
        'half_life_days':{**HALF_LIVES, **policy.get('half_life_days',{})},
        'neutral_band':float(policy.get('neutral_band',.15))}


def validate_grade_sign(review):
    grade = Grade(review['grade'])
    expected = {Grade.VERY_BULLISH:1, Grade.BULLISH:1, Grade.MODERATELY_BULLISH:1,
        Grade.NEUTRAL:0, Grade.MODERATELY_BEARISH:-1, Grade.BEARISH:-1, Grade.VERY_BEARISH:-1}
    if grade not in expected or review['direction_sign'] != expected[grade]:
        raise ValueError('direction sign contradicts grade')


def evaluate_event_impact(review, asof, policy=None):
    policy = policy or {}
    sign = review.get('direction_sign')
    level = review.get('impact_level')
    if sign not in (-1,0,1) or isinstance(sign,bool) or level not in IMPACT_WEIGHTS:
        raise ValueError('review requires explicit direction_sign=-1/0/1 and light/medium/major impact')
    relevance = float(review.get('stock_relevance',1.))
    confidence = float(review.get('evidence_confidence',1.))
    if not all(math.isfinite(value) and 0<=value<=1 for value in (relevance,confidence)):
        raise ValueError('relevance and evidence confidence must be in [0,1]')
    event_time = parse_dt(review['event_time'])
    asof = parse_dt(asof)
    if event_time > asof:
        raise ValueError('event_time is the announcement/first material information time, not a future effective date')
    kind = review.get('event_kind','other')
    half_life = float(policy.get('half_life_days',{}).get(kind,HALF_LIVES.get(kind,7.)))
    if not math.isfinite(half_life) or half_life<=0:
        raise ValueError('half-life must be positive')
    age = (asof-event_time).total_seconds()/86400
    decay = math.exp(-math.log(2)*age/half_life)
    weight = float(policy.get('impact_weights',{}).get(level,IMPACT_WEIGHTS[level]))
    if not math.isfinite(weight) or weight <= 0:
        raise ValueError('impact weight must be finite and positive')
    contribution = sign*weight*confidence*relevance*decay
    return {'direction_sign':sign,'impact_level':level,'base_weight':weight,
        'stock_relevance':relevance,'evidence_confidence':confidence,'event_kind':kind,
        'event_time':event_time.isoformat(),'age_days':age,'half_life_days':half_life,
        'time_decay':decay,'signed_contribution':contribution,'asof':asof.isoformat(),
        'is_calibrated_probability':False}


def synthesize_news_impacts(events, asof, policy=None, coverage=None):
    policy = impact_policy(policy)
    clusters = defaultdict(list)
    # A later review/retraction supersedes the same task, but distinct source
    # reviews remain available for choosing the best evidence in a cluster.
    latest = {}
    for event in events:
        if not event.available_at or event.available_at > parse_dt(asof):
            continue
        key = event.permissions.get('workflow_task_id') or event.event_id or event.url
        if key not in latest or latest[key].available_at < event.available_at:
            latest[key] = event
    for event in latest.values():
        assessment = event.permissions.get('impact_assessment')
        if assessment and event.grade is not Grade.INSUFFICIENT:
            validate_grade_sign({**assessment,'grade':event.grade.value})
            clusters[event.event_cluster or event.event_id].append((event,assessment))
    contributions, duplicates = [],0
    for cluster, members in clusters.items():
        # Reprints do not reset event_time or multiply an event's impact.
        event, assessment = max(members,key=lambda item:(float(item[1].get('evidence_confidence',0))*float(item[1].get('stock_relevance',0)),item[0].available_at))
        duplicates += len(members)-1
        # Reprint publication timestamps do not refresh a known event.
        assessment = {**assessment,'event_time':min(parse_dt(item[1]['event_time']) for item in members)}
        contribution = evaluate_event_impact(assessment,asof,policy)
        contributions.append({'event_cluster':cluster,'title':event.title,'event_id':event.event_id,
            'grade':event.grade.value,'reason':event.grade_reason,'sources':event.permissions.get('review_sources',[]),
            **contribution})
    positive = sum(max(0,item['signed_contribution']) for item in contributions)
    negative = sum(max(0,-item['signed_contribution']) for item in contributions)
    gross = positive+negative
    net = positive-negative
    normalized = net/gross if gross else 0.
    threshold = float((policy or {}).get('neutral_band',.15))
    if not math.isfinite(threshold) or not 0<=threshold<=1:
        raise ValueError('neutral band must be in [0,1]')
    classification = ('利多' if normalized>threshold else '利空' if normalized<-threshold else '中性/多空抵消') if contributions else '尚无已核实影响评估'
    return {'status':'computed' if contributions else 'unavailable','classification':classification,
        'positive_contribution':positive,'negative_contribution':negative,'net_contribution':net,
        'normalized_balance':normalized,'neutral_band':threshold,'event_count':len(contributions),
        'deduplicated_reprints':duplicates,'events':sorted(contributions,key=lambda item:abs(item['signed_contribution']),reverse=True),
        'coverage':coverage or {},'asof':parse_dt(asof).isoformat(),
        'method':'signed importance * evidence confidence * relevance * 2^(-age/half_life), unique event clusters',
        'policy':policy,'policy_sha256':canonical_hash(policy),
        'is_probability':False,'policy_is_validated_market_impact_model':False}
