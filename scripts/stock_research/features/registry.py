"""Factor catalog: economic groups, windows, and display/model separation."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FactorSpec:
    name: str
    group: str
    description: str
    window: int
    min_history: int
    direction: str = "none"
    display_only: bool = False
    inputs: tuple[str, ...] = ()
    version: str = "f1"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "group": self.group,
            "description": self.description,
            "window": self.window,
            "min_history": self.min_history,
            "direction": self.direction,
            "display_only": self.display_only,
            "inputs": list(self.inputs),
            "version": self.version,
        }


PRICE_FACTORS: dict[str, FactorSpec] = {
    "mom_60": FactorSpec("mom_60", "trend_reversal", "60-day log total-return momentum", 60, 61, "trend", inputs=("J",)),
    "mom_12_1": FactorSpec("mom_12_1", "trend_reversal", "12-month momentum skipping the last month", 252, 253, "trend", inputs=("J",)),
    "trend_diff": FactorSpec("trend_diff", "trend_reversal", "log(EMA20/EMA60) divided by 60-day volatility", 60, 61, "trend", inputs=("J", "r")),
    "residual_reversal": FactorSpec("residual_reversal", "trend_reversal", "Negative 5-day residual reversal after a past-only benchmark regression", 60, 60, "reversal", inputs=("r", "benchmark_r")),
    "relative_strength": FactorSpec("relative_strength", "trend_reversal", "60-day stock total return minus benchmark total return", 60, 61, "relative", inputs=("J", "benchmark_J")),
    "rv_20": FactorSpec("rv_20", "volatility_liquidity", "20-day realized volatility, annualised for display", 20, 21, "risk", inputs=("r",)),
    "vol_change": FactorSpec("vol_change", "volatility_liquidity", "20-day volatility divided by 60-day volatility minus one", 60, 61, "risk", inputs=("r",)),
    "downside_vol_20": FactorSpec("downside_vol_20", "volatility_liquidity", "20-day downside deviation of simple returns", 20, 21, "risk", inputs=("simple_returns",)),
    "dd_60": FactorSpec("dd_60", "volatility_liquidity", "Current drawdown from the 60-day running high", 60, 60, "risk", inputs=("J",)),
    "max_dd_60": FactorSpec("max_dd_60", "volatility_liquidity", "Worst drawdown within the 60-day window", 60, 60, "risk", inputs=("J",)),
    "abnormal_volume": FactorSpec("abnormal_volume", "volatility_liquidity", "Log volume versus the prior 20-day median, split-adjusted", 20, 21, "risk", inputs=("volume", "q")),
    "amihud_20": FactorSpec("amihud_20", "volatility_liquidity", "Mean 20-day absolute return per unit of traded amount", 20, 21, "risk", inputs=("simple_returns", "amount")),
    "beta_252": FactorSpec("beta_252", "volatility_liquidity", "Historical slope of stock returns on benchmark returns", 252, 120, "risk", inputs=("simple_returns", "benchmark_simple_returns")),
    "idio_vol_252": FactorSpec("idio_vol_252", "volatility_liquidity", "Annualised residual volatility from the benchmark regression", 252, 120, "risk", inputs=("simple_returns", "benchmark_simple_returns")),
}

DISPLAY_FACTORS: dict[str, FactorSpec] = {
    "rsi_14": FactorSpec("rsi_14", "display", "Wilder RSI(14)", 14, 15, "none", display_only=True, inputs=("J",)),
    "macd_12_26_9": FactorSpec("macd_12_26_9", "display", "MACD histogram (12,26,9)", 26, 35, "none", display_only=True, inputs=("J",)),
    "bollinger_position_20": FactorSpec("bollinger_position_20", "display", "Position within a 20-day Bollinger band", 20, 21, "none", display_only=True, inputs=("J",)),
    "atr_14_pct": FactorSpec("atr_14_pct", "display", "Wilder ATR(14) divided by close", 14, 15, "none", display_only=True, inputs=("adjusted_ohlc",)),
}

FINANCIAL_FACTORS: dict[str, FactorSpec] = {
    "size_log": FactorSpec("size_log", "size", "Log market cap from cutoff price and known reported shares", 0, 1, "size", inputs=("shares", "raw_close", "q"), version="f2"),
    "bp": FactorSpec("bp", "value", "Known shareholder book equity divided by cutoff market capitalisation", 0, 1, "value", inputs=("equity", "market_cap"), version="f2"),
    "ep_ttm": FactorSpec("ep_ttm", "value", "Trailing net income divided by market capitalisation at cutoff", 0, 4, "value", inputs=("net_income_ttm", "market_cap")),
    "fcf_yield_ttm": FactorSpec("fcf_yield_ttm", "value", "TTM free cash flow divided by market capitalisation", 0, 4, "value", inputs=("cfo_ttm", "capex_ttm", "market_cap")),
    "roa_ttm": FactorSpec("roa_ttm", "quality_growth", "TTM net income over average assets", 0, 4, "quality", inputs=("net_income_ttm", "assets")),
    "roe_ttm": FactorSpec("roe_ttm", "quality_growth", "TTM net income over positive average shareholder equity", 0, 4, "quality", inputs=("net_income_ttm", "equity"), version="f2"),
    "asset_growth_yoy": FactorSpec("asset_growth_yoy", "quality_growth", "Known asset growth versus the same fiscal period one year ago", 0, 2, "growth", inputs=("assets", "assets_1y"), version="f2"),
    "accruals_ttm": FactorSpec("accruals_ttm", "quality_growth", "Negative TTM accruals over average assets", 0, 4, "quality", inputs=("net_income_ttm", "cfo_ttm", "assets")),
    "gross_margin_ttm": FactorSpec("gross_margin_ttm", "quality_growth", "TTM gross profit over revenue", 0, 4, "quality", inputs=("gross_profit_ttm", "revenue_ttm")),
    "revenue_growth_yoy": FactorSpec("revenue_growth_yoy", "quality_growth", "TTM revenue growth versus one year ago", 0, 8, "growth", inputs=("revenue_ttm", "revenue_ttm_1y")),
    "profit_improvement_yoy": FactorSpec("profit_improvement_yoy", "quality_growth", "TTM net-income change over average assets", 0, 8, "growth", inputs=("net_income_ttm", "net_income_ttm_1y", "assets")),
    "leverage": FactorSpec("leverage", "quality_growth", "Total debt over assets", 0, 1, "risk", inputs=("total_debt", "assets")),
    "net_debt_ratio": FactorSpec("net_debt_ratio", "quality_growth", "Net debt over assets", 0, 1, "risk", inputs=("total_debt", "cash", "assets")),
    "interest_coverage": FactorSpec("interest_coverage", "quality_growth", "Operating income over interest expense", 0, 4, "quality", inputs=("operating_income_ttm", "interest_expense_ttm")),
    "share_change_yoy": FactorSpec("share_change_yoy", "quality_growth", "Split-adjusted share count change versus one year ago", 0, 2, "dilution", inputs=("shares",)),
}

MACRO_FACTORS: dict[str, FactorSpec] = {
    "curve_10y_2y": FactorSpec("curve_10y_2y", "market_macro", "10-year minus 2-year yield", 0, 1, "macro", inputs=("DGS10", "DGS2")),
    "cpi_yoy": FactorSpec("cpi_yoy", "market_macro", "CPIAUCSL year-over-year inflation", 12, 13, "macro", inputs=("CPIAUCSL",)),
    "fx_20d": FactorSpec("fx_20d", "market_macro", "20-day log change in a configured FX series", 20, 21, "macro", inputs=("FX",)),
    "policy_change_20d": FactorSpec("policy_change_20d", "market_macro", "Change in a policy or funding rate over 20 observations", 20, 21, "macro", inputs=("RATE",)),
}

ALL_FACTORS: dict[str, FactorSpec] = {
    **PRICE_FACTORS,
    **DISPLAY_FACTORS,
    **FINANCIAL_FACTORS,
    **MACRO_FACTORS,
}

INITIAL_MODEL_CANDIDATES: tuple[str, ...] = (
    "mom_60",
    "mom_12_1",
    "trend_diff",
    "residual_reversal",
    "rv_20",
    "vol_change",
    "dd_60",
    "abnormal_volume",
    "amihud_20",
    "beta_252",
    "idio_vol_252",
    "ep_ttm",
    "fcf_yield_ttm",
    "roa_ttm",
    "accruals_ttm",
    "revenue_growth_yoy",
    "net_debt_ratio",
)

FACTOR_GROUPS: dict[str, tuple[str, ...]] = {
    "trend_reversal": ("mom_60", "mom_12_1", "trend_diff", "residual_reversal", "relative_strength"),
    "volatility_liquidity": ("rv_20", "vol_change", "downside_vol_20", "dd_60", "max_dd_60", "abnormal_volume", "amihud_20", "beta_252", "idio_vol_252"),
    "size": ("size_log",),
    "value": ("ep_ttm", "fcf_yield_ttm", "bp"),
    "quality_growth": ("roa_ttm", "roe_ttm", "asset_growth_yoy", "accruals_ttm", "gross_margin_ttm", "revenue_growth_yoy", "profit_improvement_yoy", "leverage", "net_debt_ratio", "interest_coverage", "share_change_yoy"),
    "market_macro": ("curve_10y_2y", "cpi_yoy", "fx_20d", "policy_change_20d"),
}


def factor_specs(names: tuple[str, ...] | list[str] | None = None) -> list[FactorSpec]:
    if names is None:
        return list(PRICE_FACTORS.values())
    out = []
    for name in names:
        if name not in ALL_FACTORS:
            raise KeyError(f"unknown factor {name}")
        out.append(ALL_FACTORS[name])
    return out
