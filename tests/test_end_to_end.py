from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import security_rows
from stock_research.contracts import ResearchRequest
from stock_research.control.news_reviews import import_news_reviews
from stock_research.control.orchestration import run_research
from stock_research.storage import CuratedStore

UTC = timezone.utc
CUTOFF = datetime(2026, 11, 8, tzinfo=UTC)


def headline(ticker, when='2026-11-06T12:00:00+00:00', security_ids=None):
    ids = [f'US:XNAS:{ticker}'] if security_ids is None else security_ids
    return {'title': ticker + ' event', 'security_ids': json.dumps(ids), 'market': 'US',
            'source_id': 'user_import', 'published_at': when, 'available_at': when}


def review(tmp_path, root, grade='中度利好', sign=1):
    entry = {'title': 'Confirmed partnership', 'security_ids': ['US:XNAS:X'], 'event_cluster': 'partner',
        'fact_summary': 'A signed partnership is announced.', 'grade': grade,
        'grade_reason': 'Adoption expands; revenue impact undisclosed.',
        'reviewed_at': '2026-11-07T10:00:00Z', 'evidence_scope': 'official_excerpt',
        'impact_assessment': {'direction_sign': sign, 'impact_level': 'medium', 'event_kind': 'partnership',
            'event_time': '2026-11-05T15:00:00Z', 'stock_relevance': 1.0, 'evidence_confidence': 0.8},
        'sources': [{'url': 'https://example.com/official', 'published_at': '2026-11-05T15:00:00Z',
            'evidence_locator': 'official announcement, paragraph 2'}]}
    path = tmp_path / 'review.json'
    path.write_text(json.dumps([entry]), encoding='utf-8')
    import_news_reviews(path, root)


def test_empty_runtime_reports_gaps_without_calling_them_neutral(tmp_path):
    bundle, markdown = run_research(ResearchRequest(market='US', tickers=['X'], cutoff=CUTOFF), runtime_root=tmp_path)
    assert bundle.context_events == [] and bundle.news_impact_summary['status'] == 'unavailable'
    assert '缺少证据不等于中性' in markdown
    assert sum('缺少数据集' in item for item in bundle.limitations) == 3
    assert '量化' not in markdown and '概率参考' not in markdown


def test_news_context_is_limited_to_requested_securities_and_market_wide_events(tmp_path):
    store = CuratedStore(tmp_path)
    store.save('securities', security_rows('X', 'Y'))
    store.save('news', [headline('X'), headline('Y'), headline('Macro', security_ids=[])])
    bundle, _ = run_research(ResearchRequest(market='US', tickers=['X'], cutoff=CUTOFF), runtime_root=tmp_path)
    assert sorted(event.title for event in bundle.context_events) == ['Macro event', 'X event']


def test_events_after_the_knowledge_cutoff_or_outside_the_window_are_excluded(tmp_path):
    store = CuratedStore(tmp_path)
    store.save('securities', security_rows('X'))
    late = (CUTOFF + timedelta(hours=1)).isoformat()
    old = (CUTOFF - timedelta(days=40)).isoformat()
    store.save('news', [headline('X'), headline('X', late), headline('X', old)])
    bundle, _ = run_research(ResearchRequest(market='US', tickers=['X'], cutoff=CUTOFF), runtime_root=tmp_path)
    assert len(bundle.context_events) == 1


def test_reviewed_event_reaches_the_graded_report_only_after_its_review_time(tmp_path):
    root = tmp_path / 'data'
    CuratedStore(root).save('securities', security_rows('X'))
    review(tmp_path, root)
    bundle, markdown = run_research(ResearchRequest(market='US', tickers=['X'], cutoff=CUTOFF), runtime_root=root)
    impact = bundle.news_impact_summary
    assert impact['status'] == 'computed' and impact['classification'] == '利多' and impact['event_count'] == 1
    assert '**中度利好**' in markdown and 'https://example.com/official' in markdown
    assert '不是上涨概率' in markdown
    before, _ = run_research(ResearchRequest(market='US', tickers=['X'],
        cutoff=datetime(2026, 11, 6, tzinfo=UTC)), runtime_root=root)
    assert before.context_events == [] and before.news_impact_summary['status'] == 'unavailable'


def test_headline_only_records_are_listed_as_pending_and_do_not_count(tmp_path):
    store = CuratedStore(tmp_path)
    store.save('securities', security_rows('X'))
    store.save('news', [{**headline('X'), 'title': 'X beats estimates', 'evidence_scope': 'title'}])
    bundle, markdown = run_research(ResearchRequest(market='US', tickers=['X'], cutoff=CUTOFF), runtime_root=tmp_path)
    assert bundle.context_events[0].grade.value == '证据不足'
    assert '待核实' in markdown and '尚无已核实的影响评估' in markdown


def test_macro_background_uses_only_observations_public_by_the_cutoff(tmp_path):
    store = CuratedStore(tmp_path)
    known = '2026-11-06T21:00:00+00:00'
    rows = [{'series_id': 'DGS10', 'observation_date': '2026-11-05', 'value': 4.1, 'market': 'US', 'available_at': known},
            {'series_id': 'DGS2', 'observation_date': '2026-11-05', 'value': 3.6, 'market': 'US', 'available_at': known},
            {'series_id': 'DGS10', 'observation_date': '2026-11-07', 'value': 9.0, 'market': 'US',
             'available_at': (CUTOFF + timedelta(days=1)).isoformat()},
            {'series_id': 'DGS2', 'observation_date': '2026-11-06', 'value': 0.0, 'market': 'US', 'available_at': None}]
    store.save('macro', rows)
    bundle, markdown = run_research(ResearchRequest(market='US', tickers=['X'], cutoff=CUTOFF), runtime_root=tmp_path)
    readings = {item['name']: item for item in bundle.macro_background}
    assert readings['curve_10y_2y']['value'] == pytest.approx(0.5)
    assert readings['cpi_yoy']['status'] == 'missing' and readings['cpi_yoy']['reason'] == 'window_insufficient'
    assert '10年−2年国债利差' in markdown and '0.5000' in markdown


def test_unknown_ticker_keeps_running_with_a_stated_default_identity(tmp_path):
    bundle, _ = run_research(ResearchRequest(market='HK', tickers=['0700'], cutoff=CUTOFF), runtime_root=tmp_path)
    assert bundle.securities[0].security_id == 'HK:XHKG:0700'
    assert any('默认身份' in item for item in bundle.limitations)


def test_old_request_files_with_retired_quantitative_keys_still_load():
    request = ResearchRequest.from_dict({'market': 'US', 'tickers': ['aapl'], 'benchmark': 'SPY',
        'horizons': [1, 5], 'targets': ['absolute'], 'quant_baseline': True, 'multifactor': True})
    assert request.tickers == ['AAPL']


def test_cli_doctor_research_and_reproduce(tmp_path):
    root = tmp_path / 'data'
    CuratedStore(root).save('securities', security_rows('X'))
    review(tmp_path, root)
    base = Path(__file__).resolve().parents[1]
    script = base / 'scripts' / 'research.py'
    env = {'PYTHONPATH': str(base / 'scripts'), 'STOCK_RESEARCH_RUNTIME': str(root)}

    def cli(*args):
        result = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, encoding='utf-8', env=env)
        assert result.returncode == 0, result.stderr
        return result.stdout

    doctor = json.loads(cli('doctor'))
    assert doctor['policy_hash'] and doctor['datasets']['news']['rows'] == 1
    assert 'quant_baseline' not in doctor and 'multifactor' not in doctor
    output = json.loads(cli('research', '--ticker', 'X', '--cutoff', '2026-11-08T00:00:00Z', '--json'))
    assert output['report'].startswith('# 消息研究报告：X')
    assert output['summary']['news_impact_summary']['classification'] == '利多'
    assert cli('reproduce', '--run-id', output['summary']['run_id']).strip() == output['report'].strip()
    help_text = cli('--help')
    assert 'kronos' not in help_text and 'multifactor' not in help_text and 'quant-baseline' not in help_text
