"""Local, frozen Kronos-small direction baseline; no calibrated probabilities.

The neural checkpoint and tokenizer are loaded only from audited local files.
Every supplied completed historical bar is used. Forecasts retain raw generated
paths, including OHLC ordering defects, so research scores remain reproducible.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import types
from typing import Any

import numpy as np
import pandas as pd

from ..config import default_skill_root

FIELDS = ('open', 'high', 'low', 'close', 'volume', 'amount')
PATH_COUNT = 16
TRAINING_CONTEXT_REFERENCE = 512
SOURCE_COMMIT = '67b630e67f6a18c9e9be918d9b4337c960db1e9a'
_PINNED = {
    'small/config.json': '5e0f6a605d5f81b5c9b559fe5cf716a1acb041c744e6f41bd05b097b7a685396',
    'small/model.safetensors': 'b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020',
    'tokenizer/config.json': '2366e7ccfec76cbc19cf3c4c1b9c5d901be336ca1e83f2d2292c9bff381b77a2',
    'tokenizer/model.safetensors': '59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee',
}
_PINNED_SOURCE = {
    'kronos.py': 'bf075627ad412ce195b985b47296eae3571a4cc4c33e43b8b0ef4f194d165242',
    'module.py': '6bc2f4594e33be391071ea236ad28886bd59667562d38f24db3adb06bd079092',
    '__init__.py': '1bf70432e43a45f0205c1e092eb785ec05ac2e69589a9d7d3608b46f891e36a2',
}
_PROCESS_MUTEX = threading.RLock()
_LOADED: dict[str, tuple] = {}


class BaselineUnavailable(Exception):
    pass


class BaselineInputError(ValueError):
    pass


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def _utc(value: Any, name: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise BaselineInputError(f'invalid_{name}') from exc
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise BaselineInputError(f'{name}_requires_timezone')
    return timestamp.tz_convert('UTC')


def _prepare(history, frequency, cutoff, target_start, target_end):
    frequency = {'daily': '1d', 'hourly': '1h'}.get(frequency, frequency)
    if frequency not in ('1d', '1h'):
        raise BaselineInputError('unsupported_frequency')
    cutoff = _utc(cutoff, 'cutoff')
    target_start, target_end = _utc(target_start, 'target_start'), _utc(target_end, 'target_end')
    if target_start < cutoff or target_end <= target_start:
        raise BaselineInputError('invalid_target_interval')
    if frequency == '1h' and target_end - target_start != pd.Timedelta(hours=1):
        raise BaselineInputError('target_must_be_complete_hour')
    if not history:
        raise BaselineUnavailable('no_completed_history')
    normalized, previous_end = [], None
    for bar in history:
        if not isinstance(bar, dict):
            raise BaselineInputError('history_bar_must_be_object')
        start = _utc(bar.get('start_utc'), 'history_start')
        end = _utc(bar.get('end_utc'), 'history_end')
        if start >= end or end > cutoff:
            raise BaselineInputError('future_or_invalid_history_interval')
        if previous_end is not None and start < previous_end:
            raise BaselineInputError('overlapping_or_nonchronological_history')
        if frequency == '1h' and end - start != pd.Timedelta(hours=1):
            raise BaselineInputError('history_must_be_complete_hours')
        previous_end = end
        available = bar.get('available_at')
        if available is not None and available != '' and _utc(available, 'available_at') > cutoff:
            raise BaselineInputError('future_availability_in_history')
        try:
            values = {key: float(bar[key]) for key in FIELDS}
        except (KeyError, TypeError, ValueError) as exc:
            raise BaselineInputError('incomplete_ohlcva_history') from exc
        if not all(math.isfinite(v) and v > 0 for v in values.values()):
            raise BaselineInputError('nonpositive_or_nonfinite_ohlcva_history')
        if values['high'] < max(values['open'], values['close']) or values['low'] > min(values['open'], values['close']):
            raise BaselineInputError('historical_ohlc_order_violation')
        try:
            local = pd.Timestamp(bar['date'])
            if pd.isna(local):
                raise ValueError
            if local.tzinfo is not None:
                local = local.tz_convert('America/New_York').tz_localize(None)
        except (KeyError, TypeError, ValueError) as exc:
            raise BaselineInputError('invalid_history_ny_date') from exc
        ny_start = start.tz_convert('America/New_York')
        if str(local.date()) != str(ny_start.date()):
            raise BaselineInputError('history_date_timezone_mismatch')
        if frequency == '1h' and local != ny_start.tz_localize(None):
            raise BaselineInputError('history_hour_timestamp_mismatch')
        canonical = {'date': local.isoformat(), 'start_utc': start.isoformat(),
            'end_utc': end.isoformat(), 'market_date': str(ny_start.date()), **values,
            'available_at': _utc(available, 'available_at').isoformat() if available is not None and available != '' else None}
        if bar.get('amount_basis'):
            canonical['amount_basis'] = str(bar['amount_basis'])
        normalized.append(canonical)
    return normalized, frequency, cutoff.isoformat(), target_start.isoformat(), target_end.isoformat()


def _audit_assets(package_root: Path):
    models = package_root / 'model-assets/kronos-small'
    source = package_root / 'vendor/kronos/model'
    manifest_path = models / 'manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('source_commit') != SOURCE_COMMIT:
            raise ValueError('source_commit_mismatch')
        checkpoints = {c['name']: c for c in manifest['checkpoints']}
        hashes = {}
        for key, expected in _PINNED.items():
            path = models / key
            actual = _sha(path)
            name, filename = key.split('/')
            declared = next(f for f in checkpoints[name]['files'] if Path(f['path']).name == filename)
            if actual != expected or declared['sha256'] != expected:
                raise ValueError('checkpoint_hash_mismatch:' + key)
            hashes[key] = actual
        source_hashes = {}
        for filename, expected in _PINNED_SOURCE.items():
            actual = _sha(source / filename)
            if actual != expected:
                raise ValueError('vendor_source_hash_mismatch:' + filename)
            source_hashes[filename] = actual
    except FileNotFoundError as exc:
        raise BaselineUnavailable('missing_local_kronos_assets') from exc
    return models, source, {
        'manifest_sha256': _sha(manifest_path), 'source_commit': SOURCE_COMMIT,
        'checkpoint_sha256': hashes, 'vendor_source_sha256': source_hashes,
        'checkpoint_revision': checkpoints['small']['revision'],
        'checkpoint_published_at': checkpoints['small']['published_at'],
        'tokenizer_revision': checkpoints['tokenizer']['revision'],
        'backend_sha256': _sha(Path(__file__)),
        'one_step_source_sha256': _sha(Path(__file__).with_name('kronos_one_step.py')),
    }


@contextmanager
def _inference_lock(lock: Path, wait_seconds: float):
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    owned = False
    try:
        while True:
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise BaselineUnavailable('inference_busy')
                time.sleep(min(.25, max(0, deadline - time.monotonic())))
                continue
            try:
                os.write(descriptor, json.dumps({'pid': os.getpid(), 'model': 'skill-kronos-small',
                    'started_at': datetime.now(timezone.utc).isoformat()}).encode('utf-8'))
            finally:
                os.close(descriptor)
            owned = True
            break
        yield
    finally:
        if owned and lock.exists():
            try:
                if json.loads(lock.read_text(encoding='utf-8')).get('pid') == os.getpid():
                    lock.unlink()
            except (OSError, ValueError):
                pass


def _load_vendor(source: Path):
    """Honor pinned absolute imports temporarily, preserving unrelated modules."""
    private = 'stock_research.models._kronos_vendor_' + SOURCE_COMMIT[:12]
    if private + '.kronos' in sys.modules:
        return sys.modules[private + '.kronos']
    package = types.ModuleType(private)
    package.__path__ = [str(source)]
    sys.modules[private] = package
    missing = object()
    previous = {key: sys.modules.get(key, missing) for key in ('model', 'model.module', 'model.kronos')}
    old_paths = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(private + '.module', source / 'module.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        sys.modules['model'] = package
        sys.modules['model.module'] = module
        spec = importlib.util.spec_from_file_location(private + '.kronos', source / 'kronos.py')
        native = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = native
        sys.modules['model.kronos'] = native
        spec.loader.exec_module(native)
        return native
    except Exception:
        for key in (private + '.kronos', private + '.module', private):
            sys.modules.pop(key, None)
        raise
    finally:
        sys.path[:] = old_paths
        for key, value in previous.items():
            if value is missing:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


def _load_local(models: Path, source: Path, threads: int):
    old_paths = list(sys.path)
    try:
        import torch
        from safetensors.torch import load_model
        from .kronos_one_step import SharedOneStep
        torch.set_num_threads(threads)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            # A host which already used torch owns the initialized interop pool.
            pass
        key = str(models.resolve())
        if key in _LOADED:
            return _LOADED[key]
        native = _load_vendor(source)
        network = native.Kronos(**json.loads((models / 'small/config.json').read_text()))
        tokenizer = native.KronosTokenizer(**json.loads((models / 'tokenizer/config.json').read_text()))
        load_model(network, str(models / 'small/model.safetensors'), strict=True, device='cpu')
        load_model(tokenizer, str(models / 'tokenizer/model.safetensors'), strict=True, device='cpu')
        network.eval().requires_grad_(False)
        tokenizer.eval().requires_grad_(False)
        predictor = SharedOneStep(network, tokenizer, paths=PATH_COUNT,
            sample_from_logits=native.sample_from_logits, calc_time_stamps=native.calc_time_stamps)
        result = (torch, predictor, {
            'torch_version': torch.__version__, 'dtype': str(next(network.parameters()).dtype),
            'device': 'cpu', 'threads': torch.get_num_threads(),
            'interop_threads': torch.get_num_interop_threads(),
            'network_parameters': sum(p.numel() for p in network.parameters()),
            'network_weights_updated': False, 'tokenizer_weights_updated': False,
            'loader': 'local_safetensors_strict_no_hub_or_pickle',
        })
        _LOADED[key] = result
        return result
    except ImportError as exc:
        raise BaselineUnavailable('missing_local_inference_dependency:' + str(exc.name or 'unknown')) from exc
    finally:
        sys.path[:] = old_paths


def forecast_kronos(history: list[dict], *, runtime_root, config: dict,
                    ticker: str, frequency: str, cutoff: str,
                    target_start: str, target_end: str) -> dict:
    """Forecast a target's own open-to-close direction using the full prefix.

    ``vote_score`` is a smoothed generated-path score, never a calibrated stock
    probability. Raw prices are retained solely as reproducibility diagnostics.
    """
    started = time.monotonic()
    result = {'model_id': 'kronos-small', 'model_name': 'NeoQuasar/Kronos-small',
        'baseline_role': 'frozen_pretrained_reference', 'status': 'unavailable',
        'ticker': str(ticker).upper(), 'frequency': frequency, 'cutoff': cutoff,
        'target_start': target_start, 'target_end': target_end, 'direction': None,
        'up_paths': None, 'valid_paths': 0, 'path_count': PATH_COUNT,
        'vote_score': None, 'raw_up_fraction': None, 'n_history': len(history),
        'history_truncated': False, 'context_exceeds_training_reference': len(history) > TRAINING_CONTEXT_REFERENCE,
        'training_context_reference': TRAINING_CONTEXT_REFERENCE,
        'network_weights_updated': False, 'calibrated_probability': False,
        'target_definition': 'target own close>open; prediction vote ties up; realized zero not-up',
        'paths': [], 'sampled_tokens': {}, 'ohlc_order_violations': 0,
        'limitations': ['generated-path direction score is not a calibrated investment probability',
            'raw generated paths are not validated target prices or path-risk forecasts',
            'macro, news, fundamentals and benchmark joint dynamics are not inputs'],
    }
    if result['context_exceeds_training_reference']:
        result['limitations'].append('full history exceeds the published512 training-context reference; research stress test')
    if history and any(not bar.get('available_at') for bar in history if isinstance(bar, dict)):
        result['limitations'].append('not every historical bar has a verified first-availability timestamp')
    try:
        records, frequency, cutoff, target_start, target_end = _prepare(history, frequency, cutoff, target_start, target_end)
        result.update({'frequency': frequency, 'cutoff': cutoff, 'target_start': target_start, 'target_end': target_end})
        identity = {'ticker': result['ticker'], 'frequency': frequency, 'cutoff': cutoff,
            'target_start': target_start, 'target_end': target_end, 'history': records,
            'model_id': 'kronos-small', 'path_count': PATH_COUNT}
        result['input_sha256'] = _digest(identity)
        seed = int(_digest({'input_sha256': result['input_sha256'], 'seed_policy': 'kronos-skill-v1'})[:8], 16)
        result['seed'] = seed
        if config.get('device', 'cpu') != 'cpu':
            raise BaselineUnavailable('this_frozen_baseline_supports_cpu_only')
        threads = int(config.get('threads', 8))
        if not 1 <= threads <= 64:
            raise BaselineInputError('threads_must_be_between1_and64')
        wait = float(config.get('lock_wait_seconds', 60))
        if not math.isfinite(wait) or not 0 <= wait <= 60:
            raise BaselineInputError('lock_wait_seconds_must_be_between0_and60')
        package_root = Path(config.get('package_root') or default_skill_root()).resolve()
        models, source, artifacts = _audit_assets(package_root)
        result['artifacts'] = artifacts
        result['model_version'] = artifacts['checkpoint_revision']
        values = np.asarray([[bar[k] for k in FIELDS] for bar in records], dtype=np.float32)
        if not np.isfinite(values).all():
            raise BaselineInputError('float32_input_overflow')
        timestamps = pd.Series(pd.to_datetime([bar['date'] for bar in records]))
        with _PROCESS_MUTEX:
            with _inference_lock(models / 'inference.lock', wait):
                torch, predictor, execution = _load_local(models, source, threads)
                result['execution'] = {**execution, 'threads': torch.get_num_threads()}
                with torch.random.fork_rng(devices=[]):
                    output, tokens = predictor.forecast(values, timestamps, seed)
        paths = [dict(zip(FIELDS, map(float, row))) for row in output]
        if len(paths) != PATH_COUNT or not all(all(math.isfinite(v) for v in p.values()) for p in paths):
            raise RuntimeError('nonfinite_or_incomplete_generated_paths')
        valid = [p for p in paths if p['open'] > 0 and p['close'] > 0]
        result.update({'paths': paths, 'sampled_tokens': tokens, 'valid_paths': len(valid)})
        if len(valid) < 8:
            raise RuntimeError('too_few_positive_open_close_paths')
        up = sum(p['close'] > p['open'] for p in valid)
        score = (up + 1) / (len(valid) + 2)
        violations = sum(p['high'] < max(p['open'], p['close']) or p['low'] > min(p['open'], p['close']) for p in valid)
        result.update({'status': 'ok', 'direction': 'up' if score >= .5 else 'not_up',
            'up_paths': up, 'raw_up_fraction': up / len(valid), 'vote_score': score,
            'ohlc_order_violations': violations, 'reason': ''})
        if violations:
            result['limitations'].append(f'{violations}/{len(valid)} retained generated paths violate OHLC ordering')
    except BaselineUnavailable as exc:
        result.update({'status': 'unavailable', 'reason': str(exc)})
    except (BaselineInputError, KeyError, ValueError, TypeError) as exc:
        result.update({'status': 'failed', 'reason': str(exc), 'error_type': type(exc).__name__})
    except Exception as exc:
        result.update({'status': 'failed', 'reason': 'inference_failure', 'error_type': type(exc).__name__})
    result['elapsed_seconds'] = time.monotonic() - started
    return result
