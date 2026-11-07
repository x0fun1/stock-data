"""Price, trend, volatility and liquidity factors from raw prices and actions."""

from __future__ import annotations

from datetime import date
from math import log, sqrt
from typing import Iterable

import numpy as np

from ..common.timeutil import parse_date
from ..contracts import FeatureObservation, MissingReason, PriceBar
from ..corporate_actions.ledger import ActionLedger
from ..corporate_actions.returns import (
    PriceSeries,
    adjusted_history,
    build_total_return_series,
)
from .registry import DISPLAY_FACTORS, PRICE_FACTORS


def _ok(name: str, value: float, cutoff: date, inputs: dict | None = None, display: bool = False) -> FeatureObservation:
    return FeatureObservation(name=name, value=float(value), as_of=cutoff, inputs=inputs or {}, display_only=display)


def _miss(name: str, cutoff: date, reason: MissingReason | str, inputs: dict | None = None, display: bool = False) -> FeatureObservation:
    return FeatureObservation(
        name=name,
        value=None,
        as_of=cutoff,
        status="missing",
        reason=reason.value if isinstance(reason, MissingReason) else reason,
        inputs=inputs or {},
        display_only=display,
    )


def _ema(values: Iterable[float], n: int) -> list[float]:
    seq = list(values)
    if not seq:
        return []
    alpha = 2.0 / (n + 1.0)
    out = [seq[0]]
    for value in seq[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _wilders(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    avg = sum(values[:n]) / n
    for value in values[n:]:
        avg = (avg * (n - 1) + value) / n
    return avg


def _align_returns(
    stock_dates: list[date],
    stock_returns: list[float],
    bench_dates: list[date],
    bench_returns: list[float],
) -> tuple[list[date], np.ndarray, np.ndarray]:
    bench_map = dict(zip(bench_dates, bench_returns))
    dates = [d for d in stock_dates if d in bench_map]
    s = np.array([stock_returns[stock_dates.index(d)] for d in dates], dtype=float)
    b = np.array([bench_map[d] for d in dates], dtype=float)
    return dates, s, b


def _unsupported_in_window(ts, start: date | None) -> bool:
    if start is None:
        return bool(ts.unsupported_actions)
    return any(action.ex_date >= start for action in ts.unsupported_actions)


def compute_price_features(
    series: PriceSeries,
    ledger: ActionLedger,
    cutoff: date | str,
    benchmark: PriceSeries | None = None,
    benchmark_ledger: ActionLedger | None = None,
    price_config: dict | None = None,
    known_by=None,
    include_display: bool = True,
    market: str = "US",
) -> dict[str, FeatureObservation]:
    cutoff = parse_date(cutoff)
    series = series.slice(cutoff, known_by=known_by)
    if benchmark is not None:
        benchmark = benchmark.slice(cutoff, known_by=known_by)
    cfg = price_config or {}
    annual_cfg = cfg.get("annualization_days", 252)
    annualization = int(annual_cfg.get(market, 252)) if isinstance(annual_cfg, dict) else int(annual_cfg)
    beta_min_days = int(cfg.get("beta_min_days", 120))
    min_window = int(cfg.get("return_window_days", 60))
    ts = build_total_return_series(series, ledger, cutoff, known_by=known_by)
    out: dict[str, FeatureObservation] = {}
    names = list(PRICE_FACTORS)
    if not ts.dates or ts.quality == "break":
        reason = MissingReason.INCOMPLETE_PATH if ts.quality == "break" else MissingReason.NO_DATA
        for name in names:
            out[name] = _miss(name, cutoff, reason)
        if include_display:
            for name in DISPLAY_FACTORS:
                out[name] = _miss(name, cutoff, reason, display=True)
        return out

    J = np.asarray(ts.index, dtype=float)
    r = np.asarray(ts.log_returns, dtype=float)
    simple = np.asarray(ts.simple_returns, dtype=float)
    ret_dates = ts.dates[1:]
    action_note = "unknown" if any(a.available_at is None for a in ledger.actions(series.security_id, end=cutoff)) else "known"

    def base_inputs(window: int, n: int, **extra) -> dict:
        return {"window": window, "valid_observations": n, "annualization_days": annualization, "action_availability": action_note, **extra}

    def window_start(n: int, use_returns: bool = False) -> date | None:
        seq = ret_dates if use_returns else ts.dates
        if len(seq) < n:
            return None
        return seq[-n]

    def guard(name: str, n: int, use_returns: bool = False, **extra) -> FeatureObservation | None:
        seq = ret_dates if use_returns else ts.dates
        if len(seq) < n:
            return _miss(name, cutoff, MissingReason.WINDOW_INSUFFICIENT, base_inputs(n, len(seq), **extra))
        start = window_start(n, use_returns=use_returns)
        if _unsupported_in_window(ts, start):
            return _miss(name, cutoff, MissingReason.UNSUPPORTED_ACTION, base_inputs(n, len(seq), **extra))
        return None

    # Momentum
    miss = guard("mom_60", 61)
    if miss:
        out["mom_60"] = miss
    elif J[-61] <= 0:
        out["mom_60"] = _miss("mom_60", cutoff, MissingReason.NONPOSITIVE_BASE, base_inputs(60, len(J)))
    else:
        out["mom_60"] = _ok("mom_60", log(J[-1] / J[-61]), cutoff, base_inputs(60, len(J)))

    miss = guard("mom_12_1", 253)
    if miss:
        out["mom_12_1"] = miss
    elif J[-253] <= 0:
        out["mom_12_1"] = _miss("mom_12_1", cutoff, MissingReason.NONPOSITIVE_BASE, base_inputs(252, len(J)))
    else:
        out["mom_12_1"] = _ok("mom_12_1", log(J[-22] / J[-253]), cutoff, base_inputs(252, len(J)))

    sigma60 = None
    if len(r) >= 60:
        sigma60 = float(np.std(r[-60:], ddof=1))
    sigma20 = None
    if len(r) >= 20:
        sigma20 = float(np.std(r[-20:], ddof=1))

    miss = guard("trend_diff", 61)
    if miss:
        out["trend_diff"] = miss
    elif sigma60 is None or sigma60 <= 0:
        out["trend_diff"] = _miss("trend_diff", cutoff, MissingReason.ZERO_DENOMINATOR, base_inputs(60, len(r)))
    else:
        ema20 = _ema(J, 20)[-1]
        ema60 = _ema(J, 60)[-1]
        if ema20 <= 0 or ema60 <= 0:
            out["trend_diff"] = _miss("trend_diff", cutoff, MissingReason.NONPOSITIVE_BASE, base_inputs(60, len(r)))
        else:
            out["trend_diff"] = _ok("trend_diff", log(ema20 / ema60) / sigma60, cutoff, base_inputs(60, len(r), sigma60=sigma60))

    # Residual reversal and relative strength
    if benchmark is None:
        out["residual_reversal"] = _miss("residual_reversal", cutoff, MissingReason.NO_DATA, {"benchmark": "missing"})
        out["relative_strength"] = _miss("relative_strength", cutoff, MissingReason.NO_DATA, {"benchmark": "missing"})
        out["beta_252"] = _miss("beta_252", cutoff, MissingReason.NO_DATA, {"benchmark": "missing"})
        out["idio_vol_252"] = _miss("idio_vol_252", cutoff, MissingReason.NO_DATA, {"benchmark": "missing"})
    else:
        bts = build_total_return_series(benchmark, benchmark_ledger or ActionLedger(), cutoff, known_by=known_by)
        if not bts.dates or len(bts.log_returns) < min_window:
            out["residual_reversal"] = _miss("residual_reversal", cutoff, MissingReason.NO_DATA, {"benchmark": "insufficient"})
            out["relative_strength"] = _miss("relative_strength", cutoff, MissingReason.NO_DATA, {"benchmark": "insufficient"})
            out["beta_252"] = _miss("beta_252", cutoff, MissingReason.NO_DATA, {"benchmark": "insufficient"})
            out["idio_vol_252"] = _miss("idio_vol_252", cutoff, MissingReason.NO_DATA, {"benchmark": "insufficient"})
        else:
            aligned_dates, s_ret, b_ret = _align_returns(ret_dates, ts.log_returns, bts.dates[1:], bts.log_returns)
            if len(aligned_dates) < min_window:
                for name in ("residual_reversal", "beta_252", "idio_vol_252"):
                    out[name] = _miss(name, cutoff, MissingReason.WINDOW_INSUFFICIENT, {"aligned_days": len(aligned_dates)})
            else:
                # Residual reversal: fit only on observations before the final five days.
                n_use = min(252, len(s_ret))
                x = b_ret[-n_use:]
                y = s_ret[-n_use:]
                if len(y) < 60 or len(y) <= 5:
                    out["residual_reversal"] = _miss("residual_reversal", cutoff, MissingReason.WINDOW_INSUFFICIENT, {"aligned_days": len(y)})
                else:
                    fit_x, fit_y = x[:-5], y[:-5]
                    design = np.column_stack([np.ones_like(fit_x), fit_x])
                    try:
                        coef, *_ = np.linalg.lstsq(design, fit_y, rcond=None)
                    except np.linalg.LinAlgError:
                        coef = None
                    if coef is None:
                        out["residual_reversal"] = _miss("residual_reversal", cutoff, MissingReason.ZERO_DENOMINATOR)
                    else:
                        eps_all = y - (coef[0] + coef[1] * x)
                        eps_last = y[-5:] - (coef[0] + coef[1] * x[-5:])
                        denom = float(np.std(eps_all[-60:], ddof=1)) * sqrt(5.0)
                        if denom <= 0 or not np.isfinite(denom):
                            out["residual_reversal"] = _miss("residual_reversal", cutoff, MissingReason.ZERO_DENOMINATOR, {"residual_std": denom})
                        else:
                            out["residual_reversal"] = _ok(
                                "residual_reversal", float(-np.sum(eps_last) / denom), cutoff,
                                {"fit_days": len(fit_y), "predicted_days": 5, "residual_std60": float(np.std(eps_all[-60:], ddof=1))},
                            )
                # Beta and idiosyncratic volatility
                n_beta = min(252, len(s_ret))
                xb, yb = b_ret[-n_beta:], s_ret[-n_beta:]
                if len(yb) < beta_min_days:
                    for name in ("beta_252", "idio_vol_252"):
                        out[name] = _miss(name, cutoff, MissingReason.WINDOW_INSUFFICIENT, {"aligned_days": len(yb), "minimum": beta_min_days})
                elif float(np.var(xb, ddof=1)) <= 0:
                    for name in ("beta_252", "idio_vol_252"):
                        out[name] = _miss(name, cutoff, MissingReason.ZERO_DENOMINATOR, {"benchmark_variance": 0.0})
                else:
                    design = np.column_stack([np.ones_like(xb), xb])
                    coef, *_ = np.linalg.lstsq(design, yb, rcond=None)
                    resid = yb - design @ coef
                    beta = float(coef[1])
                    idio = float(np.std(resid, ddof=1) * sqrt(annualization))
                    out["beta_252"] = _ok("beta_252", beta, cutoff, {"fit_days": len(yb), "window": "up_to_252"})
                    out["idio_vol_252"] = _ok("idio_vol_252", idio, cutoff, {"fit_days": len(yb), "annualization_days": annualization})
            # Relative strength uses common total-return index dates.
            b_index = dict(zip(bts.dates, bts.index))
            common = [d for d in ts.dates if d in b_index]
            if len(common) < 61:
                out["relative_strength"] = _miss("relative_strength", cutoff, MissingReason.WINDOW_INSUFFICIENT, {"common_days": len(common)})
            else:
                last = common[-1]
                first = common[-61]
                s_ratio = ts.index[ts.dates.index(last)] / ts.index[ts.dates.index(first)]
                b_ratio = b_index[last] / b_index[first]
                out["relative_strength"] = _ok(
                    "relative_strength", float((s_ratio - 1.0) - (b_ratio - 1.0)), cutoff,
                    {"common_days": len(common), "window_start": first.isoformat()},
                )

    # Realised volatility and risk
    miss = guard("rv_20", 20, use_returns=True)
    if miss:
        out["rv_20"] = miss
    elif sigma20 is None:
        out["rv_20"] = _miss("rv_20", cutoff, MissingReason.WINDOW_INSUFFICIENT)
    else:
        out["rv_20"] = _ok("rv_20", float(sigma20 * sqrt(annualization)), cutoff, base_inputs(20, len(r), annualization_days=annualization))

    miss = guard("vol_change", 60, use_returns=True)
    if miss:
        out["vol_change"] = miss
    elif sigma20 is None or sigma60 is None or sigma60 <= 0:
        out["vol_change"] = _miss("vol_change", cutoff, MissingReason.ZERO_DENOMINATOR, base_inputs(60, len(r)))
    else:
        out["vol_change"] = _ok("vol_change", float(sigma20 / sigma60 - 1.0), cutoff, base_inputs(60, len(r), sigma20=sigma20, sigma60=sigma60))

    miss = guard("downside_vol_20", 20, use_returns=True)
    if miss:
        out["downside_vol_20"] = miss
    elif len(simple) < 20:
        out["downside_vol_20"] = _miss("downside_vol_20", cutoff, MissingReason.WINDOW_INSUFFICIENT)
    else:
        downside = sqrt(annualization * float(np.mean(np.minimum(simple[-20:], 0.0) ** 2)))
        out["downside_vol_20"] = _ok("downside_vol_20", downside, cutoff, base_inputs(20, len(simple)))

    miss = guard("dd_60", 60)
    if miss:
        out["dd_60"] = miss
    else:
        peak = float(np.max(J[-60:]))
        out["dd_60"] = _ok("dd_60", float(J[-1] / peak - 1.0) if peak > 0 else None, cutoff, base_inputs(60, len(J))) if peak > 0 else _miss("dd_60", cutoff, MissingReason.NONPOSITIVE_BASE)

    miss = guard("max_dd_60", 60)
    if miss:
        out["max_dd_60"] = miss
    else:
        window = J[-60:]
        running = np.maximum.accumulate(window)
        if float(np.min(running)) <= 0:
            out["max_dd_60"] = _miss("max_dd_60", cutoff, MissingReason.NONPOSITIVE_BASE)
        else:
            out["max_dd_60"] = _ok("max_dd_60", float(np.min(window / running - 1.0)), cutoff, base_inputs(60, len(J)))

    # Volume and liquidity
    adjusted = adjusted_history(series, ledger, cutoff, known_by=known_by)
    if len(adjusted) >= 21:
        elems = [row for row in adjusted[-21:] if row.get("volume") is not None and row["volume"] > 0]
        if len(elems) < 21:
            out["abnormal_volume"] = _miss("abnormal_volume", cutoff, MissingReason.NO_DATA, {"valid_volume_days": len(elems)})
        else:
            vols = np.array([row["volume"] for row in elems], dtype=float)
            baseline = float(np.median(vols[:-1]))
            if baseline <= 0:
                out["abnormal_volume"] = _miss("abnormal_volume", cutoff, MissingReason.ZERO_DENOMINATOR)
            else:
                out["abnormal_volume"] = _ok("abnormal_volume", float(log(vols[-1] / baseline)), cutoff, base_inputs(20, len(vols)))
    else:
        out["abnormal_volume"] = _miss("abnormal_volume", cutoff, MissingReason.WINDOW_INSUFFICIENT, {"bars": len(adjusted)})

    if len(ret_dates) >= 20:
        recent_dates = ret_dates[-20:]
        vals = []
        for d in recent_dates:
            amt = series.amount(d)
            idx = ret_dates.index(d)
            if amt is not None and amt > 0 and np.isfinite(simple[idx]):
                vals.append(abs(float(simple[idx])) / amt)
        if len(vals) < 20:
            out["amihud_20"] = _miss("amihud_20", cutoff, MissingReason.NO_DATA, {"valid_amount_days": len(vals)})
        else:
            out["amihud_20"] = _ok("amihud_20", float(np.mean(vals)), cutoff, base_inputs(20, len(vals)))
    else:
        out["amihud_20"] = _miss("amihud_20", cutoff, MissingReason.WINDOW_INSUFFICIENT)

    if include_display:
        _compute_display(series, ledger, cutoff, adjusted, J, out, known_by=known_by)
    return out


def _compute_display(
    series: PriceSeries,
    ledger: ActionLedger,
    cutoff: date,
    adjusted: list[dict],
    J: np.ndarray,
    out: dict[str, FeatureObservation],
    known_by=None,
) -> None:
    closes = np.array([row["close"] for row in adjusted if row["close"] is not None], dtype=float)
    if len(closes) >= 15:
        diffs = np.diff(closes)
        gains = np.maximum(diffs, 0.0)
        losses = np.maximum(-diffs, 0.0)
        avg_gain = _wilders(list(gains), 14)
        avg_loss = _wilders(list(losses), 14)
        if avg_gain is None or avg_loss is None:
            out["rsi_14"] = _miss("rsi_14", cutoff, MissingReason.WINDOW_INSUFFICIENT, display=True)
        elif avg_loss == 0:
            out["rsi_14"] = _ok("rsi_14", 100.0 if avg_gain > 0 else 50.0, cutoff, {"window": 14}, display=True)
        else:
            rs = avg_gain / avg_loss
            out["rsi_14"] = _ok("rsi_14", 100.0 - 100.0 / (1.0 + rs), cutoff, {"window": 14}, display=True)
    else:
        out["rsi_14"] = _miss("rsi_14", cutoff, MissingReason.WINDOW_INSUFFICIENT, display=True)

    if len(closes) >= 35:
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        macd = [a - b for a, b in zip(ema12, ema26)]
        signal = _ema(macd, 9)
        hist = macd[-1] - signal[-1]
        out["macd_12_26_9"] = _ok(
            "macd_12_26_9", hist, cutoff,
            {"macd": macd[-1], "signal": signal[-1], "histogram": hist},
            display=True,
        )
    else:
        out["macd_12_26_9"] = _miss("macd_12_26_9", cutoff, MissingReason.WINDOW_INSUFFICIENT, display=True)

    if len(J) >= 20:
        window = J[-20:]
        sd = float(np.std(window, ddof=1))
        if sd > 0:
            out["bollinger_position_20"] = _ok(
                "bollinger_position_20", float((J[-1] - np.mean(window)) / sd), cutoff, {"window": 20}, display=True
            )
        else:
            out["bollinger_position_20"] = _miss("bollinger_position_20", cutoff, MissingReason.ZERO_DENOMINATOR, display=True)
    else:
        out["bollinger_position_20"] = _miss("bollinger_position_20", cutoff, MissingReason.WINDOW_INSUFFICIENT, display=True)

    rows = [row for row in adjusted if None not in (row.get("high"), row.get("low"), row.get("close"))]
    if len(rows) >= 15 and rows[-1]["close"] > 0:
        trs = []
        prev_close = rows[0]["close"]
        for row in rows[1:]:
            high, low, close = row["high"], row["low"], row["close"]
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
            prev_close = close
        atr = _wilders(trs, 14)
        if atr is None:
            out["atr_14_pct"] = _miss("atr_14_pct", cutoff, MissingReason.WINDOW_INSUFFICIENT, display=True)
        else:
            out["atr_14_pct"] = _ok("atr_14_pct", atr / rows[-1]["close"], cutoff, {"window": 14}, display=True)
    else:
        out["atr_14_pct"] = _miss("atr_14_pct", cutoff, MissingReason.NO_DATA, display=True)


def market_breadth(
    universe: dict[str, PriceSeries],
    ledgers: dict[str, ActionLedger],
    cutoff: date | str,
    known_by=None,
) -> FeatureObservation:
    cutoff = parse_date(cutoff)
    if len(universe) < 2:
        return _miss("market_breadth_60", cutoff, MissingReason.UNIVERSE_REQUIRED)
    above = 0
    valid = 0
    for security_id, series in universe.items():
        ts = build_total_return_series(series, ledgers.get(security_id, ActionLedger()), cutoff, known_by=known_by)
        if len(ts.index) < 60:
            continue
        sma = float(np.mean(ts.index[-60:]))
        if sma <= 0:
            continue
        valid += 1
        if ts.index[-1] > sma:
            above += 1
    if valid < 2:
        return _miss("market_breadth_60", cutoff, MissingReason.UNIVERSE_REQUIRED, {"valid_securities": valid})
    return _ok("market_breadth_60", above / valid, cutoff, {"securities": valid, "above": above})
