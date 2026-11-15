from datetime import datetime, timezone
import json

import pytest

from stock_research.acquisition.sources.fred import parse_observations
from stock_research.context.macro import MacroStore

UTC=timezone.utc


def test_alfred_vintage_dates_are_conservative_and_revision_safe():
    data={'observations':[{'date':'2026-01-01','value':'4','realtime_start':'2026-01-02','realtime_end':'2026-01-31'},
                          {'date':'2026-01-01','value':'9','realtime_start':'2026-02-01','realtime_end':'9999-12-31'}]}
    assert parse_observations(data,'DGS10')[0].available_at is None
    observations=parse_observations(data,'DGS10',use_realtime_boundaries=True)
    store=MacroStore(observations)
    assert store.latest('DGS10',datetime(2026,1,2,21,tzinfo=UTC)) is None
    assert store.latest('DGS10',datetime(2026,1,3,21,tzinfo=UTC)).value==4
    assert store.latest('DGS10',datetime(2026,2,2,21,tzinfo=UTC)).value==9
    assert observations[0].quality=='alfred_vintage_date_conservative'


def test_news_review_requires_content_and_respects_actual_review_time(tmp_path):
    from stock_research.control.news_reviews import import_news_reviews
    from stock_research.control.ingest import news_from_frame
    from stock_research.context.news import build_context
    from stock_research.storage import CuratedStore
    review={'title':'Confirmed partnership','security_ids':['US:XNAS:NVDA'],'event_cluster':'partner',
        'fact_summary':'A signed partnership is announced.','grade':'中度利好','grade_reason':'Adoption expands; revenue impact undisclosed.',
        'reviewed_at':'2026-11-07T10:00:00Z','evidence_scope':'official_excerpt',
        'sources':[{'url':'https://example.com/official','published_at':'2026-11-05T15:00:00Z','evidence_locator':'official announcement, paragraph 2'}]}
    path=tmp_path/'review.json';path.write_text(json.dumps([review]))
    import_news_reviews(path,tmp_path/'data')
    events=news_from_frame(CuratedStore(tmp_path/'data').load('news'))
    assert build_context(events,'2026-11-06T21:00:00Z',30)==[]
    assert build_context(events,'2026-11-07T11:00:00Z',30)[0].grade.value=='中度利好'
    review['evidence_scope']='title';path.write_text(json.dumps([review]))
    with pytest.raises(ValueError,match='headline'):
        import_news_reviews(path,tmp_path/'data')


def test_repeated_payload_receipt_keeps_first_local_observation(tmp_path):
    from stock_research.storage import RawStore
    from stock_research.contracts import AcquisitionReceipt
    store=RawStore(tmp_path)
    first=AcquisitionReceipt('finnhub','news','one',received_at=datetime(2026,11,7,10,tzinfo=UTC))
    last=AcquisitionReceipt('finnhub','news','two',received_at=datetime(2026,11,8,10,tzinfo=UTC))
    path=store.save_payload('finnhub','news',b'[{"headline":"a"}]',first)
    assert store.save_payload('finnhub','news',b'[{"headline":"a"}]',last)==path
    record=store.read_manifest(path)
    assert record['first_observed_at']=='2026-11-07T10:00:00+00:00'
    assert record['last_observed_at']=='2026-11-08T10:00:00+00:00'
    assert 'not_provider_first_publication' in record['observation_time_basis']
