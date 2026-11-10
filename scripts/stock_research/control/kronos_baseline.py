"""Skill-owned daily/hourly input preparation and sealed Kronos baseline runs."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import json
import math

import pandas as pd

from ..common.hashing import canonical_hash, sha256_file
from ..common.jsonio import write_json
from ..common.timeutil import ensure_utc, now_utc
from ..config import default_runtime_root, load_policies
from ..identity_calendar.calendar import build_calendar
from ..models.kronos import forecast_kronos
from ..storage.catalog import Catalog

NY = ZoneInfo('America/New_York')
FIELDS = ('open', 'high', 'low', 'close', 'volume')


def utc(value) -> datetime:
    t = pd.Timestamp(value)
    if t.tzinfo is None:
        raise ValueError('cutoff and hourly interval timestamps require an explicit timezone')
    return t.tz_convert('UTC').to_pydatetime()


def _missing(value) -> bool:
    return value is None or value == '' or bool(pd.isna(value))


def _quotes(row: dict) -> tuple[dict | None, str]:
    try:
        values = {name: float(row[name]) for name in FIELDS}
    except (KeyError, TypeError, ValueError):
        return None, 'missing_ohlcv'
    if not all(math.isfinite(x) for x in values.values()):
        return None, 'nonfinite_ohlcv'
    if any(values[name] <= 0 for name in ('open', 'high', 'low', 'close')) or values['volume'] <= 0:
        return None, 'nonpositive_price_or_volume'
    if values['high'] < max(values['open'], values['close']) or values['low'] > min(values['open'], values['close']):
        return None, 'invalid_observed_ohlc_order'
    amount = row.get('amount', row.get('amount_proxy_usd'))
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        amount = float('nan')
    if math.isfinite(amount) and amount > 0:
        values['amount'] = amount
        values['amount_basis'] = ('estimated_volume_times_mean_ohlc' if row.get('amount_is_estimate') is True
                                 or 'amount_proxy_usd' in row else 'vendor_reported_amount')
    else:
        values['amount'] = values['volume'] * sum(values[k] for k in ('open', 'high', 'low', 'close')) / 4
        values['amount_basis'] = 'estimated_volume_times_mean_ohlc'
    return values, ''


def daily_history(series, calendar, cutoff: datetime) -> tuple[list[dict], list[dict]]:
    history, excluded = [], []
    for bar in series.bars:
        if bar.date > cutoff.astimezone(NY).date():
            continue
        if not calendar.is_session(bar.date):
            excluded.append({'date': str(bar.date), 'reason': 'not_a_session'})
            continue
        end = calendar.session_close_utc(bar.date)
        if end > cutoff:
            continue
        available = ensure_utc(bar.available_at) if bar.available_at else None
        if available is not None and available > cutoff:
            excluded.append({'date': str(bar.date), 'reason': 'not_available_at_cutoff'})
            continue
        if bar.quality not in ('ok', '', 'raw_fqt0_vendor_history_unverified'):
            excluded.append({'date': str(bar.date), 'reason': 'quality:' + bar.quality})
            continue
        quotes, reason = _quotes({name: getattr(bar, name) for name in (*FIELDS, 'amount')})
        if quotes is None:
            excluded.append({'date': str(bar.date), 'reason': reason})
            continue
        history.append({'date': bar.date.isoformat() + 'T00:00:00', 'market_date': str(bar.date),
            'start_utc': calendar.session_open_utc(bar.date).isoformat(), 'end_utc': end.isoformat(),
            'available_at': available.isoformat() if available else None, **quotes})
    return history, excluded


def hourly_history(frame: pd.DataFrame, ticker: str, cutoff: datetime, calendar) -> tuple[list[dict], list[dict]]:
    if 'ticker' in frame:
        frame = frame[frame['ticker'].astype(str).str.upper() == ticker.upper()]
    if frame.empty:
        raise ValueError('ticker has no rows in the hourly file')
    start_col = 'bar_start_utc' if 'bar_start_utc' in frame else 'start_utc'
    end_col = 'bar_end_utc' if 'bar_end_utc' in frame else 'end_utc'
    if start_col not in frame or end_col not in frame:
        raise ValueError('hourly CSV requires explicit bar_start_utc/bar_end_utc intervals')
    history, excluded, seen = [], [], set()
    for row in sorted(frame.to_dict('records'), key=lambda r: utc(r[start_col])):
        start, end = utc(row[start_col]), utc(row[end_col])
        # No target or later quote is read by the predictor's input preparation.
        if end > cutoff:
            continue
        available_raw = row.get('available_at')
        available = None if _missing(available_raw) else utc(available_raw)
        if available and available > cutoff:
            excluded.append({'start_utc': start.isoformat(), 'reason': 'not_available_at_cutoff'})
            continue
        local, day = start.astimezone(NY), start.astimezone(NY).date()
        reason = ''
        if end - start != timedelta(hours=1):
            reason = 'not_60_minutes'
        elif not calendar.is_session(day) or local.minute or local.second or not 10 <= local.hour <= 15:
            reason = 'not_regular_full_hour'
        elif end > calendar.session_close_utc(day):
            reason = 'after_session_close'
        quality = row.get('quality', 'ok')
        if not _missing(quality) and quality not in ('ok', 'raw_fqt0_vendor_history_unverified'):
            reason = 'quality:' + str(quality)
        if reason:
            excluded.append({'start_utc': start.isoformat(), 'reason': reason})
            continue
        if start in seen:
            raise ValueError('duplicate completed hourly interval')
        quotes, reason = _quotes(row)
        if quotes is None:
            excluded.append({'start_utc': start.isoformat(), 'reason': reason})
            continue
        seen.add(start)
        history.append({'date': local.replace(tzinfo=None).isoformat(), 'market_date': str(day),
            'start_utc': start.isoformat(), 'end_utc': end.isoformat(),
            'available_at': available.isoformat() if available else None, **quotes})
    return history, excluded


def next_regular_hour(cutoff: datetime, calendar) -> tuple[datetime, datetime]:
    day = cutoff.astimezone(NY).date()
    for _ in range(370):
        if calendar.is_session(day):
            for hour in range(10, 16):
                start = datetime(day.year, day.month, day.day, hour, tzinfo=NY).astimezone(timezone.utc)
                end = start + timedelta(hours=1)
                if start >= cutoff and end <= calendar.session_close_utc(day):
                    return start, end
        day += timedelta(days=1)
    raise ValueError('no next regular full hour inside calendar coverage')


def execute_baseline(history: list[dict], *, runtime_root, config, ticker, frequency,
                     cutoff: datetime, target_start: datetime, target_end: datetime,
                     excluded: list[dict] | None = None, provenance: dict | None = None) -> dict:
    runtime = Path(runtime_root)
    run_id = 'kronos-' + now_utc().strftime('%Y%m%dT%H%M%S%fZ')
    folder = runtime / 'baseline-runs' / run_id
    input_file = folder / 'input.json'
    payload = {'ticker': ticker, 'frequency': frequency, 'cutoff': cutoff.isoformat(),
               'target_start': target_start.isoformat(), 'target_end': target_end.isoformat(),
               'history': history, 'future_actual_quotes_present': False}
    write_json(input_file, payload)
    result = forecast_kronos(history, runtime_root=runtime, config=config, ticker=ticker,
        frequency=frequency, cutoff=cutoff.isoformat(), target_start=target_start.isoformat(), target_end=target_end.isoformat())
    result.update({'run_id': run_id, 'role': 'frozen_research_baseline', 'frequency': frequency,
        'ticker': ticker, 'probability_verified': False, 'network_weights_updated': False,
        'vote_calibration_applied':False,'vote_calibration_mode':'off',
        'input_file': str(input_file), 'input_file_sha256': sha256_file(input_file),
        'excluded_history': excluded or [], 'provenance': provenance or {},
        'history_availability_verified': bool(history) and all(b.get('available_at') for b in history)})
    if history:
        age = (target_start - utc(history[-1]['end_utc'])).total_seconds() / 3600
        result['lag_age_hours'] = age
        result['last_history_end'] = history[-1]['end_utc']
        result['target_group'] = ('cross_session' if history[-1]['market_date'] != str(target_start.astimezone(NY).date())
                                  else 'contiguous_same_session' if age == 0 else 'same_session_gap')
    if not result['history_availability_verified']:
        result.setdefault('limitations', []).append('历史供应商首次可得时间未完整核验；本次为历史研究基准。')
    result.setdefault('limitations', []).append('模型生成分数未经校准；本次只研究下一根报价开收盘方向。')
    diagnostic_path = runtime / 'validation/kronos-calibration-diagnostic.json'
    if diagnostic_path.is_file():
        diagnostic = json.loads(diagnostic_path.read_text(encoding='utf-8'))
        direction_path=runtime/'validation/kronos-direction-diagnostic.json'
        if direction_path.is_file():
            direction_audit=json.loads(direction_path.read_text(encoding='utf-8'))
            if direction_audit['study_id']==diagnostic.get('study_id'):
                diagnostic['direction_audit']=direction_audit
        if (diagnostic.get('ticker') == ticker and diagnostic.get('frequency') == frequency
                and diagnostic.get('model_version') == result.get('model_version')
                and date.fromisoformat(diagnostic['test_end']) <= cutoff.date()):
            if diagnostic.get('context_matches_default',True):
                result['calibration_diagnostic'] = diagnostic
            else:
                result['context_calibration_study'] = diagnostic
    forecast_file = folder / 'forecast.json'
    result['forecast_file'] = str(forecast_file)
    write_json(forecast_file, result)
    return result


def daily_baseline(series, calendar, cutoff_day: date, *, runtime_root, config, ticker, market='US') -> dict:
    if str(getattr(market, 'value', market)) != 'US':
        return {'status': 'unavailable', 'model_id': 'kronos-small', 'reason': 'US_baseline_not_validated_for_HK'}
    if calendar is None or series is None:
        return {'status': 'unavailable', 'model_id': 'kronos-small', 'reason': 'no_daily_ohlcv_or_calendar'}
    cutoff = calendar.session_close_utc(cutoff_day)
    history, excluded = daily_history(series, calendar, cutoff)
    target_day = calendar.next_session(cutoff_day)
    return execute_baseline(history, runtime_root=runtime_root, config=config, ticker=ticker,
        frequency='1d', cutoff=cutoff, target_start=calendar.session_open_utc(target_day),
        target_end=calendar.session_close_utc(target_day), excluded=excluded,
        provenance={'source': 'registered_daily_prices', 'return_basis': 'raw_quote_open_to_close'})


def run_file_baseline(file: str | Path, *, ticker: str, frequency: str, cutoff: str | None = None,
                      target_start: str | None = None, runtime_root=None, skill_root=None) -> tuple[dict, str]:
    policies = load_policies(skill_root)
    runtime = Path(runtime_root or default_runtime_root(policies.skill_root))
    path = Path(file)
    frame = pd.read_csv(path, encoding='utf-8-sig', float_precision='round_trip')
    cutoff_dt = utc(cutoff) if cutoff else now_utc()
    frequency = {'daily': '1d', 'hourly': '1h'}.get(frequency, frequency)
    calendar = build_calendar('US', policies.calendars.get('US', {}),
        start_year=1980, end_year=max(cutoff_dt.year + 1, date.today().year + 1))
    if frequency == '1h':
        local = cutoff_dt.astimezone(NY)
        if local.minute or local.second or local.microsecond:
            raise ValueError('hourly cutoff must be a completed whole-hour boundary')
        target, end = next_regular_hour(cutoff_dt, calendar)
        if target_start and utc(target_start) != target:
            raise ValueError('target_start must be the next regular full hour at or after cutoff')
        history, excluded = hourly_history(frame, ticker, cutoff_dt, calendar)
    elif frequency == '1d':
        from .ingest import prices_from_frame
        if 'ticker' in frame:
            frame = frame[frame['ticker'].astype(str).str.upper() == ticker.upper()].copy()
        if 'security_id' in frame and frame['security_id'].nunique() > 1:
            raise ValueError('daily file must identify a single ticker')
        if 'security_id' not in frame:
            frame['security_id'] = 'US:XNAS:' + ticker.upper()
        if 'market' not in frame:
            frame['market'] = 'US'
        series_map = prices_from_frame(frame, 'US')
        if len(series_map) != 1:
            raise ValueError('daily CSV has no unique valid ticker series')
        series = next(iter(series_map.values()))
        completed = [b.date for b in series.bars if calendar.is_session(b.date)
                     and calendar.session_close_utc(b.date) <= cutoff_dt]
        if not completed:
            raise ValueError('no completed daily bars before cutoff')
        day = max(completed)
        cutoff_dt = calendar.session_close_utc(day)
        history, excluded = daily_history(series, calendar, cutoff_dt)
        day = calendar.next_session(day)
        target, end = calendar.session_open_utc(day), calendar.session_close_utc(day)
        if target_start and utc(target_start) != target:
            raise ValueError('target_start must be the next trading-day open')
    else:
        raise ValueError('frequency must be 1d or 1h')
    result = execute_baseline(history, runtime_root=runtime,
        config={**policies.defaults.get('quant_baseline', {}), 'package_root': str(policies.skill_root)},
        ticker=ticker.upper(), frequency=frequency, cutoff=cutoff_dt, target_start=target, target_end=end,
        excluded=excluded, provenance={'file': str(path.resolve()), 'source_sha256': sha256_file(path)})
    report = render_baseline(result)
    report_file = runtime / 'reports' / (result['run_id'] + '.md')
    write_json(runtime / 'reports' / (result['run_id'] + '.json'), {'run_manifest': {
        'run_id': result['run_id'], 'outputs': {'markdown': str(report_file)},
        'created_at': now_utc().isoformat()}, 'baseline_forecast': result})
    report_file.write_text(report, encoding='utf-8')
    Catalog(runtime).register_run(result['run_id'], runtime / 'reports' / (result['run_id'] + '.json'),
                                  {'kind': 'kronos_baseline', 'ticker': ticker, 'frequency': frequency})
    return result, report


def render_baseline(result: dict) -> str:
    lines = ['### Kronos-small 量价基准', '']
    if result.get('status') != 'ok':
        lines.append('基准未执行：' + str(result.get('reason', result.get('status', 'unknown'))))
        return '\n'.join(lines) + '\n'
    direction = '上涨' if result.get('direction') == 'up' else '不上涨'
    score = result.get('vote_score', (result['up_paths'] + 1) / (result['valid_paths'] + 2))
    lines += [f'- 研究方向：**{direction}**；目标自身收盘是否高于开盘。',
              f'- 目标：{result["target_start"]} → {result["target_end"]}；数据截止：{result["cutoff"]}。',
              f'- 全部历史：{result["n_history"]} 根；上涨路径 {result["up_paths"]}/{result["valid_paths"]}。',
              '- 每条路径是一次随机采样的下一根K线；预测收盘高于预测开盘记1票，否则记0票。',
              f'- 原始路径比例：{result["up_paths"]}/{result["valid_paths"]}；平滑研究分数=(上涨票数+1)/(有效路径数+2)={score:.4f}。',
              '- 16次生成共享模型与输入，并非16个独立历史样本；路径票数不能直接当作真实上涨概率。']
    if result.get('context_exceeds_training_reference'):
        lines.append('- 输入超过512根训练上下文参考，属于长上下文研究压力测试。')
    violations = result.get('ohlc_order_violations', 0)
    if violations:
        if result.get('constrained_paths'):
            lines.append(f'- 原始解码有 {violations} 条OHLC顺序异常，已生成严格满足高低价约束的独立派生K线；开盘/收盘及方向票数保持原样。原始路径保留供审计。')
            lines.append('- 约束后结构有效不代表价格区间已准确；价格区间与路径风险仍需独立样本外检验。')
        else:
            lines.append(f'- 原始解码有 {violations} 条OHLC顺序异常；不能作为价格极值或风险预测。')
    if result.get('lag_age_hours', 0) > 0:
        lines.append(f'- 历史距目标开始 {result["lag_age_hours"]:g} 小时；分组：{result.get("target_group")}。')
    if not result.get('history_availability_verified'):
        lines.append('- 数据时点：已核对历史交易日与行情内容，尚无逐根历史首次供应记录；下载时间不能倒填成过去的首次可得时间。')
    diagnostic = result.get('calibration_diagnostic')
    candidate_study = result.get('context_calibration_study')
    if candidate_study:
        effective = candidate_study['calibrator']['calibration_rows']
        lines.append(f"- 输入与方向研究已完成：{candidate_study['sample_count']}个预测时点，开发{candidate_study['development_samples']}、诊断块{candidate_study['calibration_samples']}（有效{effective}）、留出{candidate_study['test_samples']}；开发段选中{candidate_study['selected_variant']}。该候选与本栏全部历史基准分别保存。")
        metrics=candidate_study['test_metrics']
        lines.append(f"- 原始方向留出诊断：命中率{candidate_study['direction_accuracy']:.1%}，前日方向基线{candidate_study['previous_direction_accuracy']:.1%}；Brier {metrics['brier']:.4f}，ECE {metrics['ece']:.3f}。尚未证实预测优势，未获得可靠概率资格。历史回放不等同于新进行的前瞻盲测。")
        if candidate_study.get('direction_audit'):
            from .kronos_diagnostics import render_direction_comparison
            audit=candidate_study['direction_audit']
            parameters=audit['calibrator']['parameters']
            if audit.get('calibration_mode')=='off':
                lines.append('- 外加投票校准默认关闭，未拟合、未改变原始分数或方向；诊断块用于检查可靠性。旧校准器及退化结果保留。')
            else:
                lines.append(f"- 投票校准诊断= sigmoid({parameters['a']:.4f} + ({parameters['b']:.4f}) × logit(原始分数))；它不是神经训练，不得替代原始方向。")
            lines+=['',render_direction_comparison(audit),'']
    if diagnostic:
        reason = '独立样本外样本/日期不足' if diagnostic['reason'].startswith('insufficient_out_of_sample_evidence') else diagnostic['reason']
        effective=diagnostic.get('calibrator',{}).get('calibration_rows',diagnostic['calibration_samples'])
        lines.append(f"- 校准诊断：已用{effective}个历史样本拟合Platt，并在后续{diagnostic['test_samples']}个样本检验；{reason}。概率资格按该版本的独立证据判定。")
    elif candidate_study:
        lines.append('- 本栏全历史基准保留原始票数；512根上下文候选另行研究，二者均未发布真实上涨概率。')
    else:
        lines.append('- 校准状态：须用历史遮蔽生成分数，在独立校准段拟合后，在留出段验证Brier、校准误差和简单基线优势。')
    lines.append('- 输入哈希：' + str(result.get('input_sha256', '')))
    return '\n'.join(lines) + '\n'
