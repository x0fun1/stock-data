from datetime import date, datetime, timezone
import json

import pandas as pd

from conftest import make_days, price_rows, security_rows, write_runtime
from stock_research.contracts import ResearchRequest
from stock_research.control.kronos_baseline import hourly_history, next_regular_hour
from stock_research.control.orchestration import run_research
from stock_research.identity_calendar.calendar import build_us_calendar


def hour(start, close=101, **extra):
    t = pd.Timestamp(start)
    return {'ticker': 'NVDA', 'bar_start_utc': t.isoformat(),
        'bar_end_utc': (t + pd.Timedelta(hours=1)).isoformat(),
        'open': 100, 'high': max(102, close), 'low': min(99, close),
        'close': close, 'volume': 1000, **extra}


def test_hourly_future_quotes_and_late_availability_never_enter_history():
    cutoff = datetime(2026, 11, 5, 20, tzinfo=timezone.utc)
    cal = build_us_calendar(2025, 2027)
    rows = [hour('2026-11-05T18:00Z'), hour('2026-11-05T19:00Z'),
            hour('2026-11-05T17:00Z', available_at='2026-11-05T21:00Z'),
            hour('2026-11-05T20:00Z')]
    before, excluded = hourly_history(pd.DataFrame(rows), 'NVDA', cutoff, cal)
    rows[-1].update({'open': 999, 'close': -999, 'volume': 0})
    after, _ = hourly_history(pd.DataFrame(rows), 'NVDA', cutoff, cal)
    assert before == after
    assert len(before) == 2
    assert excluded[0]['reason'] == 'not_available_at_cutoff'


def test_regular_hour_calendar_carter_and_early_close():
    cal = build_us_calendar(2025, 2026)
    assert not cal.is_session(date(2025, 1, 9))
    assert cal.next_session(date(2025, 1, 8)) == date(2025, 1, 10)
    assert cal.session_close_utc(date(2025, 7, 3)) == datetime(2025, 7, 3, 17, tzinfo=timezone.utc)
    start, end = next_regular_hour(datetime(2025, 7, 3, 17, tzinfo=timezone.utc), cal)
    assert start == datetime(2025, 7, 7, 14, tzinfo=timezone.utc)
    assert (end - start).total_seconds() == 3600
    assert cal.session_close_utc(date(2026, 7, 2)) == datetime(2026, 7, 2, 20, tzinfo=timezone.utc)


def test_default_research_executes_baseline_and_preserves_probability_gate(tmp_path, monkeypatch):
    days = make_days(count=40)
    write_runtime(tmp_path, price_rows('US:XNAS:NVDA', days), security_rows('NVDA'))
    calls = []

    def predictor(history, **kwargs):
        calls.append((history, kwargs))
        return {'status': 'ok', 'model_id': 'kronos-small', 'direction': 'up',
            'n_history': len(history), 'up_paths': 9, 'valid_paths': 16, 'path_count': 16,
            'cutoff': kwargs['cutoff'], 'target_start': kwargs['target_start'],
            'target_end': kwargs['target_end'], 'input_sha256': 'frozen-input', 'limitations': []}

    monkeypatch.setattr('stock_research.control.kronos_baseline.forecast_kronos', predictor)
    request = ResearchRequest(market='US', tickers=['NVDA'], horizons=(1,))
    bundle, report = run_research(request, runtime_root=tmp_path)
    assert len(calls) == 1 and len(calls[0][0]) == 40
    assert bundle.baseline_forecasts['US:XNAS:NVDA']['status'] == 'ok'
    assert all(r.probability is None for r in bundle.results)
    assert '9/16' in report
    saved = json.loads((tmp_path / 'reports' / (bundle.run_manifest.run_id + '.json')).read_text(encoding='utf-8'))
    assert saved['run_manifest']['outputs']['markdown'].endswith('.md')
    assert saved['baseline_forecasts']['US:XNAS:NVDA']['probability_verified'] is False
    input_path = saved['baseline_forecasts']['US:XNAS:NVDA']['input_file']
    assert json.loads(__import__('pathlib').Path(input_path).read_text())['future_actual_quotes_present'] is False


def test_user_can_disable_baseline(tmp_path, monkeypatch):
    days = make_days(count=30)
    write_runtime(tmp_path, price_rows('US:XNAS:NVDA', days), security_rows('NVDA'))

    def must_not_run(*args, **kwargs):
        raise AssertionError('disabled model was called')

    monkeypatch.setattr('stock_research.control.kronos_baseline.forecast_kronos', must_not_run)
    bundle, _ = run_research(ResearchRequest(market='US', tickers=['NVDA'], quant_baseline=False), runtime_root=tmp_path)
    assert not bundle.baseline_forecasts
