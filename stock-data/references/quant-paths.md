# Quant Research Paths and Artifacts

Policy: `fixed_52w_5d_v1`. The production path is A only; B/C are shadow
candidates and D is a descriptive diagnostic. All paths read one frozen
snapshot and the same split-adjusted close series.

## Execution map

| Stage | Code | Role / artifact |
|---|---|---|
| A — Quant | `researchers/quant.py` | production; `researchers/quant.json` |
| B — Factor | `researchers/factor.py` | shadow; `researchers/factor.json` |
| C — ML | `researchers/ml.py` | shadow; `researchers/ml.json` |
| D — Factor diagnostic | `researchers/factor_backtest.py` | diagnostic; `researchers/factor_backtest.json` |
| Consensus | `consensus.py` | single-path production + shadow isolation in `analysis.json.consensus` |
| E — Audit | `audit.py` | production eligibility, transform replay, stage/claims checks |
| News | `news/` | independent frozen result; never changes Quant |
| Synthesis / delivery | `synthesis.py`, `report.py`, `delivery.py` | fixed report block and literal reply contract |

Fatal policy/data failures block A. A shadow failure is isolated and never
blocks A. No path may widen the window, change the horizon or use data outside
`(decision - 364d, decision]`.

## A — causal empirical shrinkage (production)

Version `quant_causal_shrinkage_v1`, feature `roc20_v1`, target
`split_adjusted_price_direction_5d_v1`.

- Signal: sign of the 20-session return; zero is non-negative.
- Matching sample: same-sign origins whose 5-session label matured by the
  decision close, accepted chronologically with at least 6 sessions between
  starts.
- Base rate: all daily 5-session labels matured by the decision close.
- Probability: `(wins + 12 * base_rate) / (matches + 12)`.
- Report only with at least 12 matches; otherwise abstain with a reason code.
- Current and every replay row are produced by `predict_at_origin`; the complete
  transform record is frozen with each row.

The prefix replay is retrospective and overlapping. The fixed 6-session phase is
a sampling policy, not a claim that the records are independent.

## B — restricted / two-bucket shadow candidate

Version `factor_restricted_two_honest_v1`. Selection uses the restricted
profile; the bucket mode is fixed to two buckets. The factor set is fixed on the
chronological selection segment and never re-selected from the final snapshot
backwards. The reported bucket probability may use only labels that matured
**after** the selection segment. If either bucket has fewer than 6 post-selection
observations, the candidate abstains; it does not fall back to the selection
labels. All output is shadow-only.

## C — daily compact ML shadow candidate

Version `ml_compact_daily_v1`. Features are `return_5`, `return_20` and
`volatility_20`; warm-up is 20 sessions and the minimum training set is 60 daily
labels. The model is refitted at every origin (`refit_sessions = 1`), using all
rows whose labels matured by that origin inside the 52-week snapshot. There is
no monthly anchor and no 252-row proxy. Training rows are daily overlapping
labels and are disclosed as such. Platt/ensemble calibration is disabled; the
reported number is the raw logistic output and the path is shadow-only.

## D — descriptive 5-session diagnostic

D uses only the 5-session horizon, a fixed 70/30 chronological split and at
least 20 non-overlapping holdout observations for a confirmatory IC. With fewer,
the IC is `descriptive_only` and exploratory. D never emits a probability and
never enters consensus, eligibility or promotion.

## E — audit

E verifies the snapshot identity, policy version, window coverage, price basis,
current and OOS transform reproducibility, path roles, stage receipts and the
absence of a confidence score. Vetoes clear the reportable probability; warnings
bound the claim. See [reliability-gates.md](reliability-gates.md).

## Artifacts

`analysis.json` contains `run_id`, `snapshot_id`, `policy_version`, stages,
researchers, consensus, audit, news and synthesis. `report.md` is the retained
human report; `final_response.md` is the literal delivery text; `agent_summary.json`
is the bounded host projection. Legacy `quant_result.json` / `report.json` are no
longer written.