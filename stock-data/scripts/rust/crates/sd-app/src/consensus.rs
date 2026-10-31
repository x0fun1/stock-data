//! Single-path production probability with isolated shadow candidates
//! (`quant_research/consensus.py`).
//!
//! Exactly one production path (A/quant). B/C are shadow candidates that are recorded
//! but never weighted, never promoted and never carry a confidence vote; D is a
//! descriptive diagnostic. A veto redaction removes every publishable probability and
//! direction from the payload, including the nested `prediction` block.

use chrono::NaiveDate;
use serde_json::{Map, Value};

use sd_audit::probability_quality::{
    audit_current_prediction, audit_oos_result, directional_edge_assessment,
};
use sd_core::contracts::py_str;
use sd_core::policy;

/// Round-trip cost scenarios (basis points) for the economic sensitivity block.
pub const ROUND_TRIP_COST_SCENARIOS_BPS: [i64; 3] = [5, 10, 20];

fn object(value: Option<&Value>) -> Map<String, Value> {
    value
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn as_object(value: &Value) -> Map<String, Value> {
    value.as_object().cloned().unwrap_or_default()
}

fn truthy(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Null) | None => false,
        Some(Value::Number(number)) => number.as_f64().map(|value| value != 0.0).unwrap_or(false),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

/// `_economic_viability`: cost-scenario sensitivity, never an actual cost estimate.
pub fn economic_viability(researchers: &[Value]) -> Value {
    let returns: Vec<f64> = researchers
        .iter()
        .filter_map(|result| match result.get("expected_return") {
            Some(Value::Number(number)) => number.as_f64().filter(|value| value.is_finite()),
            _ => None,
        })
        .collect();
    let gross = if returns.is_empty() {
        None
    } else {
        Some(returns.iter().sum::<f64>() / returns.len() as f64)
    };
    let Some(gross) = gross else {
        let mut result = Map::new();
        result.insert("status".into(), Value::String("not_assessed".into()));
        result.insert("gross_expected_return".into(), Value::Null);
        result.insert("scenarios".into(), Value::Array(Vec::new()));
        result.insert(
            "cost_model_status".into(),
            Value::String("assumed_scenario_sensitivity_not_actual_cost_estimate".into()),
        );
        result.insert(
            "execution_validation".into(),
            Value::String("not_assessed".into()),
        );
        return Value::Object(result);
    };
    let scenarios: Vec<Value> = ROUND_TRIP_COST_SCENARIOS_BPS
        .iter()
        .map(|cost| {
            let net = gross - *cost as f64 / 10_000.0;
            let mut row = Map::new();
            row.insert("round_trip_cost_bps".into(), Value::from(*cost));
            row.insert("net_expected_return".into(), Value::from(net));
            row.insert(
                "expected_return_below_cost".into(),
                Value::Bool(gross < *cost as f64 / 10_000.0),
            );
            Value::Object(row)
        })
        .collect();
    let mut result = Map::new();
    result.insert(
        "status".into(),
        Value::String("scenario_sensitivity_only".into()),
    );
    result.insert("gross_expected_return".into(), Value::from(gross));
    result.insert(
        "expected_return_definition".into(),
        Value::String(
            "mean realised forward return of the matching-sample origins; not the probability-weighted expected return of the production estimator and not a tradable forecast"
                .into(),
        ),
    );
    result.insert("scenarios".into(), Value::Array(scenarios));
    result.insert(
        "cost_model_status".into(),
        Value::String("assumed_scenario_sensitivity_not_actual_cost_estimate".into()),
    );
    result.insert(
        "execution_validation".into(),
        Value::String("not_assessed".into()),
    );
    Value::Object(result)
}

/// Human-readable text for the reason codes, in the legacy wording.
pub fn reason_text(codes: &[String]) -> Vec<String> {
    codes
        .iter()
        .map(|code| match code.as_str() {
            "POLICY_VERSION_MISMATCH" => "冻结快照的政策版本不是当前固定窗口政策。".to_string(),
            "INSUFFICIENT_HISTORY_COVERAGE" => "52周窗口覆盖不完整，无法发布正式概率。".to_string(),
            "CALENDAR_UNVERIFIED" => "交易所日历无法验证，只能输出诊断。".to_string(),
            "PRICE_BASIS_UNVERIFIED" => "价格基础不是拆股调整收盘价，目标语义不一致。".to_string(),
            "NO_CURRENT_PROBABILITY" => "当前decision session没有可重算的概率。".to_string(),
            "CURRENT_MATCHING_SAMPLES_INSUFFICIENT" => {
                format!("当前匹配样本不足{}条。", policy::SHRINKAGE_MIN_MATCHES)
            }
            "OOS_SUPPORT_INSUFFICIENT" => format!(
                "同版本有效OOS回放样本不足{}个非重叠等价样本。",
                policy::MIN_REPLAY_EFFECTIVE_SAMPLES
            ),
            "NO_VALID_OOS_PROBABILITY" => "没有有效的非空OOS概率。".to_string(),
            "OOS_INTEGRITY_FAILED" => "OOS概率存在完整性错误。".to_string(),
            "PREDICTION_INTEGRITY_FAILED" => "当前概率无法从冻结行情和固定参数重算。".to_string(),
            "PRODUCTION_PATH_UNAVAILABLE" => "生产路径A没有产出可用结果。".to_string(),
            "PROSPECTIVE_SUPPORT_INSUFFICIENT" => format!(
                "真实前向非重叠记录不足{}条。",
                policy::MIN_PROSPECTIVE_NONOVERLAP
            ),
            "DIRECTIONAL_EDGE_UNPROVEN" => "相对因果基率的方向优势尚无充分证据。".to_string(),
            "INTERVAL_BELOW_ZERO" => {
                "相对因果基率的成对损失改善区间完全在零以下（劣于纯基率）。".to_string()
            }
            "INTERVAL_CROSSES_ZERO" => "成对损失改善的区间跨零。".to_string(),
            "INTERVAL_UNESTIMABLE" => "区间无法可靠估计。".to_string(),
            "TARGET_SESSION_UNRESOLVED" => "预测目标交易日未解析，不能发布正式概率。".to_string(),
            other => other.to_string(),
        })
        .collect()
}

/// Policy reasons that a manifest itself fails, before any researcher is graded.
pub fn manifest_policy_reasons(manifest: &Value) -> Vec<String> {
    let policy_block = object(manifest.get("policy"));
    let mut reasons: Vec<String> = Vec::new();
    if policy_block.get("policy_version").and_then(Value::as_str) != Some(policy::POLICY_VERSION) {
        reasons.push("POLICY_VERSION_MISMATCH".into());
    }
    if policy_block.get("coverage_status").and_then(Value::as_str) != Some("complete") {
        reasons.push("INSUFFICIENT_HISTORY_COVERAGE".into());
    }
    if !truthy(policy_block.get("calendar_verified")) {
        reasons.push("CALENDAR_UNVERIFIED".into());
    }
    let validation = manifest
        .get("data_validation")
        .cloned()
        .unwrap_or(Value::Null);
    let declared_basis = validation
        .get("policy")
        .and_then(|value| value.get("price_basis"))
        .cloned()
        .filter(|value| !value.is_null())
        .or_else(|| policy_block.get("price_basis").cloned())
        .unwrap_or(Value::Null);
    if declared_basis.as_str() != Some(policy::PRICE_BASIS) {
        reasons.push("PRICE_BASIS_UNVERIFIED".into());
    }
    if !truthy(policy_block.get("target_session")) {
        reasons.push("TARGET_SESSION_UNRESOLVED".into());
    }
    reasons
}

/// Compatibility shim: support is an effective-sample floor, not a score.
pub fn oos_support_sessions() -> Value {
    let mut result = Map::new();
    result.insert(
        "minimum_effective_samples".into(),
        Value::from(policy::MIN_REPLAY_EFFECTIVE_SAMPLES),
    );
    result.insert(
        "policy_version".into(),
        Value::String(policy::POLICY_VERSION.into()),
    );
    result.insert("scaling".into(), Value::String("none".into()));
    result.insert(
        "primary_evidence".into(),
        Value::String("full_prefix_replay".into()),
    );
    Value::Object(result)
}

fn int_or_zero(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(number)) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|value| value as i64))
            .unwrap_or(0),
        Some(Value::String(text)) => text.trim().parse::<i64>().unwrap_or(0),
        _ => 0,
    }
}

/// Options of [`forecast_assessment`].
pub struct AssessmentInput<'a> {
    /// Researcher result being graded.
    pub result: &'a Value,
    /// Frozen snapshot manifest.
    pub manifest: &'a Value,
    /// Pre-computed OOS evidence audit (skips the recomputation when supplied).
    pub oos_evidence_audit: Option<&'a Value>,
    /// Frozen market bars used for the independent replay.
    pub bars: Option<&'a [Value]>,
    /// Frozen session calendar (anchored sampling).
    pub sessions: Option<&'a [NaiveDate]>,
    /// Prospective forward records.
    pub prospective_records: &'a [Value],
}

/// Grade one researcher path against the fixed-policy publication rules.
pub fn forecast_assessment(input: &AssessmentInput<'_>) -> Value {
    let result = input.result;
    let path = py_str(result.get("researcher_id").unwrap_or(&Value::Null));
    let validation = object(result.get("validation"));
    let current = audit_current_prediction(result, input.bars);
    let oos = match input.oos_evidence_audit {
        Some(audit) => audit.clone(),
        None => audit_oos_result(result, input.bars, input.sessions),
    };
    let mut reasons: Vec<String> = manifest_policy_reasons(input.manifest);
    if result.get("status").and_then(Value::as_str) != Some("success") {
        reasons.push("PRODUCTION_PATH_UNAVAILABLE".into());
    }
    if result.get("prob_up").map(Value::is_null).unwrap_or(true) {
        reasons.push("NO_CURRENT_PROBABILITY".into());
    }
    if !truthy(current.get("pass")) {
        let current_reasons: Vec<String> = current
            .get("reasons")
            .and_then(Value::as_array)
            .map(|rows| {
                rows.iter()
                    .filter_map(|row| row.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        if current_reasons.is_empty() {
            reasons.push("PREDICTION_INTEGRITY_FAILED".into());
        } else {
            reasons.extend(current_reasons);
        }
    }
    if !truthy(oos.get("integrity_pass")) {
        let oos_reasons: Vec<String> = oos
            .get("reasons")
            .and_then(Value::as_array)
            .map(|rows| {
                rows.iter()
                    .filter_map(|row| row.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        if oos_reasons.is_empty() {
            reasons.push("OOS_INTEGRITY_FAILED".into());
        } else {
            reasons.extend(oos_reasons);
        }
    }
    let effective = int_or_zero(oos.get("effective_valid_samples"));
    if effective < i64::from(policy::MIN_REPLAY_EFFECTIVE_SAMPLES) {
        for code in oos
            .get("reasons")
            .and_then(Value::as_array)
            .map(|rows| {
                rows.iter()
                    .filter_map(|row| row.as_str().map(str::to_string))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default()
        {
            if !reasons.contains(&code) {
                reasons.push(code);
            }
        }
    }
    let replay_rows: Vec<Value> = object(result.get("validation"))
        .get("oos_forecasts")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter(|row| {
                    row.is_object()
                        && !row.get("probability").map(Value::is_null).unwrap_or(true)
                        && !row.get("target").map(Value::is_null).unwrap_or(true)
                })
                .cloned()
                .collect()
        })
        .unwrap_or_default();
    let edge = directional_edge_assessment(&replay_rows, input.prospective_records);
    let eligible = reasons.is_empty();
    let mut sorted: Vec<String> = reasons.clone();
    sorted.sort();
    sorted.dedup();
    let mut result_map = Map::new();
    result_map.insert(
        "computable".into(),
        Value::Bool(!result.get("prob_up").map(Value::is_null).unwrap_or(true)),
    );
    result_map.insert(
        "current_prediction_integrity_pass".into(),
        Value::Bool(truthy(current.get("pass"))),
    );
    result_map.insert(
        "oos_integrity_pass".into(),
        Value::Bool(truthy(oos.get("integrity_pass"))),
    );
    result_map.insert(
        "production_probability_eligible".into(),
        Value::Bool(eligible),
    );
    result_map.insert(
        "directional_edge_supported".into(),
        Value::Bool(truthy(edge.get("supported"))),
    );
    result_map.insert(
        "directional_edge_state".into(),
        edge.get("state").cloned().unwrap_or(Value::Null),
    );
    result_map.insert("trading_signal_eligible".into(), Value::Bool(false));
    result_map.insert(
        "reason_codes".into(),
        Value::Array(
            sorted
                .iter()
                .map(|code| Value::String(code.clone()))
                .collect(),
        ),
    );
    result_map.insert(
        "reasons".into(),
        Value::Array(
            reason_text(&sorted)
                .into_iter()
                .map(Value::String)
                .collect(),
        ),
    );
    result_map.insert(
        "warnings".into(),
        Value::Array(if eligible {
            Vec::new()
        } else {
            reason_text(&sorted)
                .into_iter()
                .map(Value::String)
                .collect()
        }),
    );
    result_map.insert("oos_evidence_audit".into(), oos);
    result_map.insert("current_prediction_audit".into(), current);
    result_map.insert("directional_edge".into(), edge);
    result_map.insert(
        "matching_floor_role".into(),
        Value::String("disclosure_only_not_an_eligibility_gate".into()),
    );
    result_map.insert(
        "minimum_matching_samples".into(),
        Value::from(policy::SHRINKAGE_MIN_MATCHES),
    );
    result_map.insert("label_basis".into(), policy::dividend_label_disclosure());
    result_map.insert("path".into(), Value::String(path.clone()));
    let identity = validation
        .get("model_identity")
        .cloned()
        .filter(|value| !value.is_null())
        .unwrap_or_else(|| {
            let known = policy::model_version(&path).is_some();
            let fallback = if known {
                path.clone()
            } else {
                "quant".to_string()
            };
            policy::model_identity(&fallback).unwrap_or(Value::Null)
        });
    result_map.insert("model_identity".into(), identity);
    Value::Object(result_map)
}

fn model_lean(probability: Option<f64>) -> &'static str {
    match probability {
        None => "unavailable",
        Some(value) if value > 0.5 => "up",
        Some(value) if value < 0.5 => "down",
        _ => "flat",
    }
}

fn model_version(path: &str) -> Value {
    policy::model_version(path)
        .map(|version| Value::String(version.to_string()))
        .unwrap_or(Value::Null)
}

fn lean_direction(lean: &str) -> &'static str {
    match lean {
        "up" => "bullish",
        "down" => "bearish",
        "flat" => "neutral",
        _ => "unavailable",
    }
}

fn by_id(researchers: &[Value]) -> Map<String, Value> {
    let mut map = Map::new();
    for researcher in researchers {
        let key = py_str(researcher.get("researcher_id").unwrap_or(&Value::Null));
        map.insert(key, researcher.clone());
    }
    map
}

/// Options of [`build_consensus`].
pub struct ConsensusInput<'a> {
    /// Researcher results (A plus optional B/C/D).
    pub researchers: &'a [Value],
    /// Frozen snapshot manifest.
    pub manifest: &'a Value,
    /// Pre-computed per-path assessments (only `quant` is consulted).
    pub assessments: Option<&'a Map<String, Value>>,
    /// Frozen market bars.
    pub bars: Option<&'a [Value]>,
    /// Frozen session calendar.
    pub sessions: Option<&'a [NaiveDate]>,
    /// Prospective forward records.
    pub prospective_records: &'a [Value],
}

/// Build the single-path consensus payload.
pub fn build_consensus(input: &ConsensusInput<'_>) -> Value {
    let researchers = input.researchers;
    let map = by_id(researchers);
    let production = map.get("quant").cloned();
    let assessment = match input
        .assessments
        .and_then(|assessments| assessments.get("quant").cloned())
    {
        Some(assessment) => assessment,
        None => forecast_assessment(&AssessmentInput {
            result: production.as_ref().unwrap_or(&Value::Null),
            manifest: input.manifest,
            oos_evidence_audit: None,
            bars: input.bars,
            sessions: input.sessions,
            prospective_records: input.prospective_records,
        }),
    };
    let eligible = truthy(assessment.get("production_probability_eligible"));
    let probability = if production.is_some() && eligible {
        production
            .as_ref()
            .and_then(|production| production.get("prob_up"))
            .cloned()
            .unwrap_or(Value::Null)
    } else {
        Value::Null
    };
    let raw = if production.is_some() && eligible {
        production
            .as_ref()
            .and_then(|production| production.get("raw_probability"))
            .cloned()
            .unwrap_or(Value::Null)
    } else {
        Value::Null
    };
    let validation = object(
        production
            .as_ref()
            .and_then(|production| production.get("validation")),
    );
    let transform = object(validation.get("probability_transform"));
    let probability_value = probability.as_f64();
    let lean = model_lean(probability_value);
    let direction = lean_direction(lean);

    let mut shadow: Vec<Value> = Vec::new();
    for path in policy::SHADOW_PATHS {
        let Some(item) = map.get(*path) else {
            continue;
        };
        let mut entry = Map::new();
        entry.insert("path".into(), Value::String((*path).into()));
        entry.insert("role".into(), Value::String("shadow".into()));
        entry.insert("model_version".into(), model_version(path));
        entry.insert(
            "status".into(),
            item.get("status").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "probability".into(),
            item.get("prob_up").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "raw_probability".into(),
            item.get("raw_probability").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "note".into(),
            Value::String(
                "shadow research candidate; not part of the production probability and not a confidence vote"
                    .into(),
            ),
        );
        entry.insert("production_probability".into(), Value::Bool(false));
        shadow.push(Value::Object(entry));
    }
    let diagnostic = map.get("factor_backtest").cloned();
    let diagnostics = match diagnostic.as_ref() {
        Some(diagnostic) => {
            let validation = object(diagnostic.get("validation"));
            let mut entries = Map::new();
            entries.insert("path".into(), Value::String("factor_backtest".into()));
            entries.insert("role".into(), Value::String("diagnostic".into()));
            entries.insert(
                "status".into(),
                diagnostic.get("status").cloned().unwrap_or(Value::Null),
            );
            entries.insert(
                "horizon".into(),
                diagnostic.get("horizon").cloned().unwrap_or(Value::Null),
            );
            entries.insert("descriptive_only".into(), Value::Bool(true));
            entries.insert(
                "information_coefficient".into(),
                validation
                    .get("information_coefficient")
                    .cloned()
                    .unwrap_or(Value::Null),
            );
            entries.insert(
                "holdout_sample_count".into(),
                validation
                    .get("holdout_sample_count")
                    .cloned()
                    .unwrap_or(Value::Null),
            );
            entries.insert(
                "holdout_minimum".into(),
                Value::from(policy::FACTOR_BACKTEST_HOLDOUT_MIN),
            );
            entries.insert(
                "note".into(),
                Value::String(
                    "descriptive 5D diagnostic; never participates in probability, eligibility or promotion".into(),
                ),
            );
            Value::Object(entries)
        }
        None => Value::Object(Map::new()),
    };

    let oos = object(assessment.get("oos_evidence_audit"));
    let prospective: Vec<&Value> = input
        .prospective_records
        .iter()
        .filter(|row| {
            row.get("sampled") == Some(&Value::Bool(true))
                && row.get("status").and_then(Value::as_str) == Some("matured")
        })
        .collect();
    let prospective_unanchored = input
        .prospective_records
        .iter()
        .filter(|row| {
            row.get("status").and_then(Value::as_str) == Some("matured")
                && row.get("sampling_status").and_then(Value::as_str) == Some("unanchored_calendar")
        })
        .count();
    let edge = assessment
        .get("directional_edge")
        .cloned()
        .unwrap_or(Value::Null);
    let edge_object = object(Some(&edge));
    let mut evidence = Map::new();
    evidence.insert(
        "replay_type".into(),
        Value::String("prefix_walk_forward".into()),
    );
    for (key, value) in [
        ("replay_nonoverlap", oos.get("nonoverlap_valid_count")),
        ("replay_valid", oos.get("valid_count")),
        ("replay_total", oos.get("record_count")),
        ("replay_abstained", oos.get("abstained_count")),
        ("replay_coverage_ratio", oos.get("coverage_ratio")),
    ] {
        evidence.insert(key.into(), value.cloned().unwrap_or(Value::Null));
    }
    evidence.insert(
        "oos_support_required_effective_samples".into(),
        Value::from(policy::MIN_REPLAY_EFFECTIVE_SAMPLES),
    );
    evidence.insert(
        "oos_effective_samples".into(),
        oos.get("effective_valid_samples")
            .cloned()
            .unwrap_or(Value::Null),
    );
    evidence.insert(
        "oos_nonoverlap".into(),
        oos.get("nonoverlap_valid_count")
            .cloned()
            .unwrap_or(Value::Null),
    );
    evidence.insert(
        "oos_sampling_status".into(),
        oos.get("sampling")
            .and_then(|sampling| sampling.get("status"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    evidence.insert(
        "oos_sampling_role".into(),
        oos.get("sampling_role").cloned().unwrap_or(Value::Null),
    );
    evidence.insert(
        "prospective_nonoverlap".into(),
        Value::from(prospective.len()),
    );
    evidence.insert(
        "prospective_unanchored".into(),
        Value::from(prospective_unanchored),
    );
    evidence.insert(
        "prospective_counting_rule".into(),
        Value::String(
            "anchored phase + prompt recording only; unanchored or late records are descriptive"
                .into(),
        ),
    );
    evidence.insert(
        "prospective_required".into(),
        Value::from(policy::MIN_PROSPECTIVE_NONOVERLAP),
    );
    evidence.insert(
        "directional_edge_supported".into(),
        Value::Bool(truthy(assessment.get("directional_edge_supported"))),
    );
    for (key, source) in [
        ("directional_edge_evidence_scope", "evidence_scope"),
        (
            "directional_edge_replay_interval_excludes_zero",
            "replay_interval_excludes_zero",
        ),
        (
            "directional_edge_independent_supported",
            "independent_supported",
        ),
        ("directional_edge_reasons", "reasons"),
        ("directional_edge_state", "state"),
        ("directional_edge_primary_evidence", "primary_evidence"),
    ] {
        evidence.insert(
            key.into(),
            edge_object.get(source).cloned().unwrap_or(Value::Null),
        );
    }
    evidence.insert(
        "calibration_status".into(),
        Value::String("disabled_by_policy".into()),
    );
    evidence.insert(
        "probability_label".into(),
        Value::String("uncalibrated_model_estimate".into()),
    );
    evidence.insert(
        "relative_baseline_skill".into(),
        edge_object.get("intervals").cloned().unwrap_or(Value::Null),
    );
    evidence.insert(
        "baseline_comparison".into(),
        oos.get("effectiveness").cloned().unwrap_or(Value::Null),
    );
    evidence.insert("label_basis".into(), policy::dividend_label_disclosure());

    let mut reason_codes: Vec<String> = assessment
        .get("reason_codes")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|row| row.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    if !eligible && probability.is_null() {
        reason_codes.push("NO_CURRENT_PROBABILITY".into());
        reason_codes.sort();
        reason_codes.dedup();
    }
    let mut prediction = Map::new();
    prediction.insert(
        "status".into(),
        Value::String(
            if probability.is_null() {
                "unavailable"
            } else {
                "available"
            }
            .into(),
        ),
    );
    prediction.insert("prob_up".into(), probability.clone());
    prediction.insert("reportable_probability".into(), probability.clone());
    prediction.insert("raw_probability".into(), raw.clone());
    prediction.insert("calibrated_probability".into(), Value::Null);
    prediction.insert("model_lean".into(), Value::String(lean.into()));
    prediction.insert(
        "probability_label".into(),
        Value::String("uncalibrated_model_estimate".into()),
    );

    let mut production_block = Map::new();
    production_block.insert("path".into(), Value::String("quant".into()));
    production_block.insert("model_version".into(), model_version("quant"));
    production_block.insert("prob_up".into(), probability.clone());
    production_block.insert("raw_probability".into(), raw.clone());
    production_block.insert("model_lean".into(), Value::String(lean.into()));
    production_block.insert(
        "probability_label".into(),
        Value::String("uncalibrated_model_estimate".into()),
    );
    production_block.insert(
        "current_prediction_integrity_pass".into(),
        Value::Bool(truthy(assessment.get("current_prediction_integrity_pass"))),
    );
    production_block.insert(
        "oos_integrity_pass".into(),
        Value::Bool(truthy(assessment.get("oos_integrity_pass"))),
    );
    production_block.insert(
        "production_probability_eligible".into(),
        Value::Bool(eligible),
    );
    production_block.insert(
        "directional_edge_supported".into(),
        Value::Bool(truthy(assessment.get("directional_edge_supported"))),
    );
    production_block.insert("trading_signal_eligible".into(), Value::Bool(false));
    production_block.insert(
        "matching_observations".into(),
        validation.get("n_raw").cloned().unwrap_or(Value::Null),
    );
    production_block.insert(
        "causal_base_rate".into(),
        transform
            .get("base_rate_at_origin")
            .cloned()
            .unwrap_or(Value::Null),
    );
    production_block.insert(
        "probability_transform".into(),
        Value::Object(transform.clone()),
    );

    let production_status = production
        .as_ref()
        .and_then(|production| production.get("status"))
        .cloned();
    let mut paths: Vec<Value> = Vec::new();
    let mut production_path = Map::new();
    production_path.insert("path".into(), Value::String("quant".into()));
    production_path.insert("role".into(), Value::String("production".into()));
    production_path.insert("model_version".into(), model_version("quant"));
    production_path.insert("status".into(), production_status.unwrap_or(Value::Null));
    paths.push(Value::Object(production_path));
    for path in policy::SHADOW_PATHS {
        let mut entry = Map::new();
        entry.insert("path".into(), Value::String((*path).into()));
        entry.insert("role".into(), Value::String("shadow".into()));
        entry.insert("model_version".into(), model_version(path));
        entry.insert(
            "status".into(),
            map.get(*path)
                .and_then(|item| item.get("status"))
                .cloned()
                .unwrap_or(Value::Null),
        );
        paths.push(Value::Object(entry));
    }
    let mut diagnostic_path = Map::new();
    diagnostic_path.insert("path".into(), Value::String("factor_backtest".into()));
    diagnostic_path.insert("role".into(), Value::String("diagnostic".into()));
    diagnostic_path.insert("model_version".into(), model_version("factor_backtest"));
    diagnostic_path.insert(
        "status".into(),
        diagnostic
            .as_ref()
            .and_then(|diagnostic| diagnostic.get("status"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    paths.push(Value::Object(diagnostic_path));

    let mut result = Map::new();
    result.insert(
        "status".into(),
        Value::String(
            if probability.is_null() {
                "unavailable"
            } else {
                "available"
            }
            .into(),
        ),
    );
    result.insert("policy".into(), policy::policy_metadata());
    result.insert("production".into(), Value::Object(production_block));
    result.insert(
        "computable".into(),
        Value::Bool(truthy(assessment.get("computable"))),
    );
    result.insert(
        "current_prediction_integrity_pass".into(),
        Value::Bool(truthy(assessment.get("current_prediction_integrity_pass"))),
    );
    result.insert(
        "oos_integrity_pass".into(),
        Value::Bool(truthy(assessment.get("oos_integrity_pass"))),
    );
    result.insert(
        "production_probability_eligible".into(),
        Value::Bool(eligible),
    );
    result.insert(
        "directional_edge_supported".into(),
        Value::Bool(truthy(assessment.get("directional_edge_supported"))),
    );
    result.insert("trading_signal_eligible".into(), Value::Bool(false));
    result.insert("prob_up".into(), probability.clone());
    result.insert("raw_probability".into(), raw.clone());
    result.insert("reportable_probability".into(), probability.clone());
    result.insert("calibrated_probability".into(), Value::Null);
    result.insert(
        "probability_basis".into(),
        Value::String("drift_shrunk_causal_base_rate".into()),
    );
    result.insert(
        "probability_basis_version".into(),
        transform
            .get("version")
            .cloned()
            .filter(|value| !value.is_null())
            .unwrap_or(Value::String(policy::DRIFT_SHRINKAGE_VERSION.into())),
    );
    result.insert(
        "calibration_status".into(),
        Value::String("disabled_by_policy".into()),
    );
    result.insert("confidence".into(), Value::Null);
    result.insert(
        "available_paths".into(),
        Value::from(if probability.is_null() { 0 } else { 1 }),
    );
    result.insert(
        "path_ids".into(),
        Value::Array(vec![Value::String("quant".into())]),
    );
    result.insert("probability_range".into(), Value::Null);
    result.insert("direction".into(), Value::String(direction.into()));
    result.insert("model_lean".into(), Value::String(lean.into()));
    result.insert("confidence_score".into(), Value::Null);
    result.insert("confidence_score_deprecated".into(), Value::Bool(true));
    result.insert(
        "aggregation_method".into(),
        Value::String("single_path".into()),
    );
    result.insert("production_path_count".into(), Value::from(1));
    result.insert("shadow_predictions".into(), Value::Array(shadow));
    result.insert("diagnostics".into(), diagnostics);
    result.insert("paths".into(), Value::Array(paths));
    result.insert("evidence".into(), Value::Object(evidence));
    result.insert(
        "reason_codes".into(),
        Value::Array(reason_codes.into_iter().map(Value::String).collect()),
    );
    result.insert(
        "reasons".into(),
        assessment
            .get("reasons")
            .cloned()
            .unwrap_or(Value::Array(Vec::new())),
    );
    result.insert(
        "warnings".into(),
        assessment
            .get("warnings")
            .cloned()
            .unwrap_or(Value::Array(Vec::new())),
    );
    result.insert("assessment".into(), assessment);
    result.insert("prediction".into(), Value::Object(prediction));
    result.insert(
        "economic_viability".into(),
        economic_viability(production.as_slice()),
    );
    Value::Object(result)
}

const PUBLISHABLE_KEYS: [&str; 7] = [
    "prob_up",
    "reportable_probability",
    "raw_probability",
    "calibrated_probability",
    "probability_range",
    "model_lean",
    "direction",
];

/// Remove every publishable probability and direction after an audit veto.
///
/// Clearing only the top-level fields leaves `prediction.prob_up`, `model_lean` and
/// `production.probability_transform` readable; the transform is preserved under
/// `audit_only` with an explicit non-publishable marker.
pub fn redact_publishable_prediction(consensus: &mut Value, audit: &Value) {
    let root = match consensus.as_object_mut() {
        Some(root) => root,
        None => return,
    };
    let production = match root
        .get("production")
        .filter(|value| value.is_object())
        .cloned()
    {
        Some(production) => production,
        None => Value::Null,
    };
    let mut production_map = as_object(&production);
    if let Some(transform) = production_map.get("probability_transform").cloned() {
        let audit_only = root
            .entry("audit_only")
            .or_insert_with(|| Value::Object(Map::new()));
        if let Value::Object(audit_only) = audit_only {
            audit_only.insert("probability_transform".into(), transform);
            audit_only.insert("publishable".into(), Value::Bool(false));
            audit_only.insert(
                "note".into(),
                Value::String(
                    "frozen estimator inputs retained for audit only; the audit vetoed publication, so no probability derived from them may be shown or reused"
                        .into(),
                ),
            );
        }
    }
    production_map.remove("probability_transform");
    let prediction = root
        .get("prediction")
        .filter(|value| value.is_object())
        .cloned();
    let mut prediction_map = prediction.as_ref().map(as_object).unwrap_or_default();
    // Clear the publishable keys at every level a renderer may read.
    for key in PUBLISHABLE_KEYS {
        if root.contains_key(key) {
            root.insert(key.to_string(), Value::Null);
        }
        if production_map.contains_key(key) {
            production_map.insert(key.to_string(), Value::Null);
        }
        if prediction_map.contains_key(key) {
            prediction_map.insert(key.to_string(), Value::Null);
        }
    }
    root.insert("model_lean".into(), Value::String("unavailable".into()));
    root.insert("direction".into(), Value::String("unavailable".into()));
    root.insert("status".into(), Value::String("unavailable".into()));
    root.insert("available_paths".into(), Value::from(0));
    if prediction.is_some() {
        prediction_map.insert("status".into(), Value::String("unavailable".into()));
        prediction_map.insert(
            "probability_label".into(),
            Value::String("unavailable_after_audit_veto".into()),
        );
        prediction_map.insert("model_lean".into(), Value::String("unavailable".into()));
    }
    production_map.insert("prob_up".into(), Value::Null);
    production_map.insert("raw_probability".into(), Value::Null);
    production_map.insert("model_lean".into(), Value::String("unavailable".into()));
    root.insert("production".into(), Value::Object(production_map));
    if prediction.is_some() {
        root.insert("prediction".into(), Value::Object(prediction_map));
    }
    let reason_codes = audit
        .get("reason_codes")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut redaction = Map::new();
    redaction.insert("publishable".into(), Value::Bool(false));
    redaction.insert(
        "cleared_fields".into(),
        Value::Array(
            PUBLISHABLE_KEYS
                .iter()
                .map(|key| Value::String((*key).into()))
                .collect(),
        ),
    );
    redaction.insert("reason_codes".into(), Value::Array(reason_codes));
    redaction.insert(
        "rule".into(),
        Value::String(
            "an audit veto leaves no publishable probability or direction anywhere in the payload"
                .into(),
        ),
    );
    root.insert("redaction".into(), Value::Object(redaction));
}
