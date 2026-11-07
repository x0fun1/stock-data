"""Observable time-boundary, packaged-assets and lock ownership checks."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from stock_research.models import kronos


def hour_bar(**overrides):
    bar = {'date': '2026-11-05T14:00:00', 'start_utc': '2026-11-05T19:00:00Z',
        'end_utc': '2026-11-05T20:00:00Z', 'market_date': '2026-11-05',
        'open': 180., 'high': 182., 'low': 179., 'close': 181.,
        'volume': 100000., 'amount': 18100000., 'available_at': None}
    return {**bar, **overrides}


def forecast(history, tmp_path, **config):
    return kronos.forecast_kronos(history, runtime_root=tmp_path,
        config={'package_root': str(tmp_path / 'empty-package'), 'lock_wait_seconds': 0, **config},
        ticker='NVDA', frequency='1h', cutoff='2026-11-05T20:00:00Z',
        target_start='2026-11-05T20:00:00Z', target_end='2026-11-05T21:00:00Z')


@pytest.mark.parametrize('change', [
    {'end_utc': '2026-11-05T20:01:00Z'},
    {'available_at': '2026-11-05T20:01:00Z'},
    {'start_utc': '2026-11-05T19:30:00Z'},
])
def test_future_and_partial_bars_stop_before_loading_models(tmp_path, monkeypatch, change):
    def unexpected(*args):
        pytest.fail('time-invalid input reached model loader')
    monkeypatch.setattr(kronos, '_audit_assets', unexpected)
    result = forecast([hour_bar(**change)], tmp_path)
    assert result['status'] == 'failed'
    assert result['direction'] is None
    assert result['paths'] == []


def test_missing_packaged_assets_does_not_fall_back_to_runtime(tmp_path):
    (tmp_path / 'model-comparison/kronos').mkdir(parents=True)
    result = forecast([hour_bar()], tmp_path)
    assert result['status'] == 'unavailable'
    assert result['reason'] == 'missing_local_kronos_assets'
    assert result['direction'] is None


def test_foreign_inference_lock_is_never_removed(tmp_path):
    lock = tmp_path / 'inference.lock'
    contents = '{"pid": 987654321, "model": "other"}'
    lock.write_text(contents)
    with pytest.raises(kronos.BaselineUnavailable, match='inference_busy'):
        with kronos._inference_lock(lock, 0):
            pytest.fail('acquired an already-owned lock')
    assert lock.read_text() == contents


def test_own_lock_released_when_inference_raises(tmp_path):
    lock = tmp_path / 'inference.lock'
    with pytest.raises(RuntimeError, match='simulated inference error'):
        with kronos._inference_lock(lock, 0):
            assert lock.exists()
            raise RuntimeError('simulated inference error')
    assert not lock.exists()


def test_raw_invalid_ohlc_paths_are_retained_for_direction_score(tmp_path, monkeypatch):
    raw = np.array([[180., 170., 190., 181., 1000., 181000.]] * 8 +
                   [[180., 182., 179., 179., 1000., 179000.]] * 8, dtype=np.float32)
    predictor = SimpleNamespace(forecast=lambda values, times, seed: (raw, {'pre': [], 'post': []}))
    fake_torch = SimpleNamespace(random=SimpleNamespace(fork_rng=lambda devices: nullcontext()),
        get_num_threads=lambda: 8)
    monkeypatch.setattr(kronos, '_audit_assets', lambda root: (
        tmp_path, tmp_path, {'checkpoint_revision': 'fixture'}))
    monkeypatch.setattr(kronos, '_load_local', lambda *args: (fake_torch, predictor, {}))
    result = forecast([hour_bar()], tmp_path)
    assert result['status'] == 'ok'
    assert result['direction'] == 'up'  # Registered tie rule.
    assert result['up_paths'] == 8 and result['valid_paths'] == 16
    assert result['ohlc_order_violations'] == 8
    assert result['paths'][0]['high'] == 170.
    assert result['paths'][0]['low'] == 190.
    assert 'probability' not in result
