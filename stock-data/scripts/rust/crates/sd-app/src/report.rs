//! Render the fixed-policy report and the mandatory response lines (`report.py`).
//!
//! One definition of "may this run's probability be delivered": the report reads the
//! frozen payload, not a stale eligibility flag. The three News states are disclosed
//! separately, and every required line is a delivery contract.

use serde_json::{Map, Value};

use sd_core::contracts::python_str;
use sd_core::policy::{
    dividend_label_disclosure, DRIFT_SHRINKAGE_K, FORECAST_HORIZON_SESSIONS, HISTORY_CALENDAR_DAYS,
    MIN_PROSPECTIVE_NONOVERLAP, MIN_REPLAY_EFFECTIVE_SAMPLES,
};
use sd_core::security::{markdown_text, safe_url};
use sd_news::status::{news_assessed, news_status_label};

/// Byte budget of the compact agent summary.
pub const AGENT_SUMMARY_BUDGET: usize = 9000;

fn lean_labels() -> Map<String, Value> {
    let mut map = Map::new();
    for (key, value) in [
        ("up", "点估计偏上涨"),
        ("down", "点估计偏下跌"),
        ("flat", "点估计持平"),
        ("unavailable", "不可用"),
    ] {
        map.insert(key.into(), Value::String(value.into()));
    }
    map
}

fn lean_label(value: Option<&str>) -> String {
    if let Some(value) = value {
        if let Some(label) = lean_labels().get(value).and_then(Value::as_str) {
            return label.to_string();
        }
    }
    "不可用".to_string()
}

fn pct(value: Option<f64>, digits: usize) -> String {
    match value {
        Some(value) => format!("{:.*}%", digits, value * 100.0),
        None => "不可估计".to_string(),
    }
}

fn num(value: Option<f64>, digits: usize) -> String {
    match value {
        Some(value) => format!("{:.*}", digits, value),
        None => "不可估计".to_string(),
    }
}

pub fn reporting_status_for(result: &Value) -> &'static str {
    let consensus = result.get("consensus").cloned().unwrap_or(Value::Null);
    let audit = result
        .get("adversarial_audit")
        .cloned()
        .unwrap_or(Value::Null);
    let probability = consensus.get("prob_up").and_then(Value::as_f64);
    let reportable = probability.is_some()
        && audit.get("may_report_probability").and_then(Value::as_bool) == Some(true);
    if reportable {
        "reportable"
    } else {
        "unavailable"
    }
}

/// The exact fixed text block; every line is a delivery contract.
pub fn required_response_lines(result: &Value) -> Map<String, Value> {
    let consensus = result.get("consensus").cloned().unwrap_or(Value::Null);
    let snapshot = result.get("snapshot").cloned().unwrap_or(Value::Null);
    let window = window_of(result);
    let decision = window
        .get("decision_session")
        .and_then(Value::as_str)
        .map(ToString::to_string)
        .or_else(|| {
            snapshot
                .get("last_bar_date")
                .and_then(Value::as_str)
                .map(ToString::to_string)
        })
        .unwrap_or_else(|| "未知".into());
    let end = window
        .get("window_end_inclusive")
        .and_then(Value::as_str)
        .map(ToString::to_string)
        .unwrap_or_else(|| decision.clone());
    let target = snapshot
        .get("target_session")
        .and_then(Value::as_str)
        .map(ToString::to_string)
        .unwrap_or_else(|| "待定".into());
    let probability = consensus.get("prob_up").and_then(Value::as_f64);
    let evidence = evidence_of(result);
    let production = production_of(result);
    let transform = production
        .get("probability_transform")
        .cloned()
        .unwrap_or(Value::Null);
    let k = transform
        .get("k")
        .and_then(Value::as_i64)
        .unwrap_or(i64::from(DRIFT_SHRINKAGE_K));
    let probability_line = if let Some(probability) = probability {
        let n_eff = transform.get("n_eff").and_then(Value::as_f64);
        let prior_weight = n_eff
            .filter(|value| *value + k as f64 > 0.0)
            .map(|value| k as f64 / (value + k as f64));
        let prior_text = match prior_weight {
            Some(value) => format!("；先验权重≈{}", pct(Some(value), 1)),
            None => String::new(),
        };
        format!(
            "上涨概率：{}（漂移收缩估计 k={k}{prior_text}；未校准，不可作为真实频率）",
            pct(Some(probability), 1)
        )
    } else {
        format!("上涨概率：不可用（{}）", unavailable_reason(result))
    };
    let prospective = evidence
        .get("prospective_nonoverlap")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let intervals = evidence
        .get("relative_baseline_skill")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let skill = intervals
        .iter()
        .find(|row| row.get("primary").and_then(Value::as_bool) == Some(true))
        .or_else(|| intervals.iter().find(|row| row.is_object()));
    let comparison = evidence
        .get("baseline_comparison")
        .cloned()
        .unwrap_or(Value::Null);
    let skill_line = match skill {
        Some(skill) if skill.get("status").and_then(Value::as_str) == Some("estimated") => {
            format!(
                "相对因果基率技能：{}；区间：[{}, {}]；Brier 模型{} vs 基率{} vs 常数0.5 {}；AUC {}；有效样本≈{}",
                num(skill.get("mean").and_then(Value::as_f64), 4),
                num(skill.get("lower").and_then(Value::as_f64), 4),
                num(skill.get("upper").and_then(Value::as_f64), 4),
                num(comparison.get("brier_model").and_then(Value::as_f64), 4),
                num(
                    comparison.get("brier_causal_base_rate").and_then(Value::as_f64),
                    4
                ),
                num(
                    comparison.get("brier_constant_half").and_then(Value::as_f64),
                    4
                ),
                num(comparison.get("auc").and_then(Value::as_f64), 4),
                comparison
                    .get("effective_sample_size")
                    .and_then(Value::as_i64)
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "不可估计".into())
            )
        }
        _ => "相对因果基率技能：不可估计；区间：不可估计".to_string(),
    };
    let replay = evidence
        .get("replay_nonoverlap")
        .cloned()
        .unwrap_or(Value::Null);
    let coverage_text = match evidence
        .get("replay_coverage_ratio")
        .and_then(Value::as_f64)
    {
        Some(coverage) => pct(Some(coverage), 1),
        None => {
            if evidence.get("oos_sampling_status").and_then(Value::as_str) == Some("unanchored") {
                "不适用（相位未锚定，未节流）".to_string()
            } else {
                "不可估计".to_string()
            }
        }
    };
    let effective = evidence
        .get("oos_effective_samples")
        .cloned()
        .unwrap_or(Value::Null);
    let coverage_ok = window.get("coverage_status").and_then(Value::as_str) == Some("complete")
        && window.get("coverage_recomputed").and_then(Value::as_bool) == Some(true);
    let window_note = if coverage_ok {
        "覆盖已按交易日历逐日重算".to_string()
    } else {
        format!(
            "覆盖未通过独立重算（{}）",
            window
                .get("coverage_status")
                .and_then(Value::as_str)
                .unwrap_or("未知")
        )
    };
    let mut lines = Map::new();
    lines.insert(
        "research_window".into(),
        Value::String(format!(
            "研究窗口：截至{end}的52周（{HISTORY_CALENDAR_DAYS}个自然日；{window_note}）"
        )),
    );
    lines.insert(
        "forecast_target".into(),
        Value::String(format!(
            "预测目标：{target}收盘相对{decision}收盘的拆股调整价格涨跌（{FORECAST_HORIZON_SESSIONS}个交易日）"
        )),
    );
    lines.insert("probability".into(), Value::String(probability_line));
    lines.insert(
        "model_lean".into(),
        Value::String(format!(
            "模型倾向：{}（仅描述点估计）",
            lean_label(consensus.get("model_lean").and_then(Value::as_str))
        )),
    );
    lines.insert(
        "directional_edge".into(),
        Value::String(edge_line(result, prospective)),
    );
    lines.insert(
        "production_path".into(),
        Value::String("生产路径：A；B/C为影子研究，D为描述性诊断".into()),
    );
    lines.insert(
        "validation".into(),
        Value::String(format!(
            "验证：前缀回放{}条（其中非重叠{}条；有效样本≈{}）；真实前向{prospective}条；覆盖率{coverage_text}；抽样状态{}",
            evidence.get("replay_valid").and_then(Value::as_i64).unwrap_or(0),
            replay.as_i64().unwrap_or(0),
            effective
                .as_i64()
                .map(|value| value.to_string())
                .unwrap_or_else(|| "不可估计".into()),
            evidence.get("oos_sampling_status").and_then(Value::as_str).unwrap_or("未知")
        )),
    );
    lines.insert("relative_skill".into(), Value::String(skill_line));
    lines.insert("news".into(), Value::String(news_line(result)));
    lines
}

fn window_of(result: &Value) -> Value {
    let snapshot = result.get("snapshot").cloned().unwrap_or(Value::Null);
    snapshot
        .get("policy")
        .filter(|value| !value.is_null())
        .or_else(|| snapshot.get("manifest_policy"))
        .cloned()
        .unwrap_or(Value::Object(Map::new()))
}

fn production_of(result: &Value) -> Value {
    result
        .get("consensus")
        .and_then(|consensus| consensus.get("production"))
        .cloned()
        .unwrap_or(Value::Object(Map::new()))
}

fn evidence_of(result: &Value) -> Value {
    result
        .get("consensus")
        .and_then(|consensus| consensus.get("evidence"))
        .cloned()
        .unwrap_or(Value::Object(Map::new()))
}

fn as_string(value: Option<&Value>) -> String {
    value.map(python_str).unwrap_or_default()
}

fn unavailable_reason(result: &Value) -> String {
    let consensus = result.get("consensus").cloned().unwrap_or(Value::Null);
    let codes: Vec<String> = consensus
        .get("reason_codes")
        .and_then(Value::as_array)
        .map(|items| items.iter().map(|value| as_string(Some(value))).collect())
        .unwrap_or_default();
    let evidence = evidence_of(result);
    let mut gaps: Vec<String> = Vec::new();
    if codes.iter().any(|code| code == "OOS_SUPPORT_INSUFFICIENT") {
        gaps.push(format!(
            "有效回放样本 {}/{}",
            evidence
                .get("oos_effective_samples")
                .and_then(Value::as_i64)
                .unwrap_or(0),
            MIN_REPLAY_EFFECTIVE_SAMPLES
        ));
    }
    if codes.iter().any(|code| code == "NO_VALID_OOS_PROBABILITY") {
        gaps.push("有效OOS概率 0".into());
    }
    if codes.iter().any(|code| code == "TARGET_SESSION_UNRESOLVED") {
        gaps.push("预测目标交易日未解析".into());
    }
    if codes
        .iter()
        .any(|code| code == "INSUFFICIENT_HISTORY_COVERAGE")
    {
        gaps.push("52周覆盖不完整".into());
    }
    if codes.iter().any(|code| code == "CALENDAR_UNVERIFIED") {
        gaps.push("交易日历不可验证".into());
    }
    if codes.iter().any(|code| code == "PRICE_BASIS_UNVERIFIED") {
        gaps.push("价格基础未验证".into());
    }
    if codes
        .iter()
        .any(|code| code == "PREDICTION_INTEGRITY_FAILED")
    {
        gaps.push("当前概率无法重算".into());
    }
    if gaps.is_empty() {
        gaps.push("生产路径A没有可用概率".into());
    }
    format!(
        "原因码{}；实际缺口：{}",
        if codes.is_empty() {
            "UNKNOWN".to_string()
        } else {
            codes.join("/")
        },
        gaps.join("，")
    )
}

fn news_line(result: &Value) -> String {
    let news = result.get("news_result").cloned().unwrap_or(Value::Null);
    let status = news.get("status").and_then(Value::as_str);
    if !news_assessed(status) {
        return "新闻：未提供".into();
    }
    let state = news_status_label(status);
    let warnings = news
        .get("warnings")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    let ai = news.get("ai_lean").cloned().unwrap_or(Value::Null);
    let coverage = ai.get("coverage").cloned().unwrap_or(Value::Null);
    let counts = coverage
        .get("direct_label_counts")
        .cloned()
        .unwrap_or(Value::Null);
    let ai_status = ai.get("status").and_then(Value::as_str);
    let judgement =
        if matches!(ai_status, Some("assessed" | "partial")) {
            let label = if ai_status == Some("assessed") {
                "已完成"
            } else {
                "部分完成"
            };
            format!(
            "AI逐条{label}，覆盖{}/{}；直接相关利多{}条、利空{}条、中性{}条、混合{}条、不明{}条",
            coverage.get("assessed_count").and_then(Value::as_i64).unwrap_or(0),
            coverage.get("candidate_count").and_then(Value::as_i64).unwrap_or(0),
            counts.get("bullish").and_then(Value::as_i64).unwrap_or(0),
            counts.get("bearish").and_then(Value::as_i64).unwrap_or(0),
            counts.get("neutral").and_then(Value::as_i64).unwrap_or(0),
            counts.get("mixed").and_then(Value::as_i64).unwrap_or(0),
            counts.get("unclear").and_then(Value::as_i64).unwrap_or(0)
        )
        } else {
            format!(
                "AI逐条未评估，候选{}条",
                coverage
                    .get("candidate_count")
                    .and_then(Value::as_i64)
                    .unwrap_or(0)
            )
        };
    let regime = news
        .get("expected_volatility_regime")
        .and_then(|block| block.get("regime"))
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    format!("新闻：已采集（{state}；{warnings} 条警告；{judgement}；波动状态 {regime}）")
}

fn news_link(item: &Value) -> String {
    let title = markdown_text(
        item.get("headline")
            .unwrap_or(&Value::String("原文".into())),
        200,
    );
    match safe_url(item.get("url").unwrap_or(&Value::Null)) {
        Some(url) => format!(
            "[{title}](<{}>)",
            url.replace('<', "%3C").replace('>', "%3E")
        ),
        None => format!("{title}（来源链接缺失）"),
    }
}

fn major_news_lines(news: &Value, limit: usize) -> Vec<String> {
    let ai = news.get("ai_lean").cloned().unwrap_or(Value::Null);
    let ai_status = ai.get("status").and_then(Value::as_str);
    if !matches!(ai_status, Some("assessed" | "partial")) {
        return vec!["- 重大利多/利空：AI 逐条判断尚未完成".to_string()];
    }
    let rows = ai
        .get("major_direct_items")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut lines: Vec<String> = Vec::new();
    for (lean, label) in [("bullish", "重大利多"), ("bearish", "重大利空")] {
        let selected: Vec<&Value> = rows
            .iter()
            .filter(|row| row.get("lean").and_then(Value::as_str) == Some(lean))
            .collect();
        if selected.is_empty() {
            lines.push(format!("- {label}：本次未识别到有原文链接的条目"));
            continue;
        }
        for row in selected.into_iter().take(limit) {
            let published = row
                .get("published_at")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .unwrap_or("时间未提供");
            lines.push(format!(
                "- {label}：{}；{published}；理由：{}",
                news_link(row),
                markdown_text(row.get("reason").unwrap_or(&Value::Null), 400)
            ));
        }
    }
    lines
}

fn edge_line(result: &Value, prospective: i64) -> String {
    let consensus = result.get("consensus").cloned().unwrap_or(Value::Null);
    let evidence = evidence_of(result);
    let state = evidence
        .get("directional_edge_state")
        .and_then(Value::as_str)
        .unwrap_or("");
    let replay = evidence
        .get("replay_valid")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let independent = evidence
        .get("directional_edge_independent_supported")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let scope = "窗口内因果前缀回放，非独立留出";
    if consensus.get("prob_up").map(Value::is_null).unwrap_or(true) {
        return "方向优势：不可用（没有可发布的生产概率）".into();
    }
    match state {
        "positive_skill_with_estimated_interval_excluding_zero" => {
            if independent {
                format!(
                    "方向优势：独立前向证据支持（{prospective}/{MIN_PROSPECTIVE_NONOVERLAP}条成熟前向记录，改善区间下界大于0）；窗口内回放 {replay} 条为佐证"
                )
            } else {
                format!(
                    "方向优势：仅回放区间下界大于0（{scope}，{replay} 条）；独立前向证据 {prospective}/{MIN_PROSPECTIVE_NONOVERLAP} 条，尚不足以支持方向优势"
                )
            }
        }
        "negative_skill_with_estimated_interval_excluding_zero" => format!(
            "方向优势：回放劣于纯基率（{scope}，{replay} 条，改善区间上界小于0）；独立前向 {prospective}/{MIN_PROSPECTIVE_NONOVERLAP} 条"
        ),
        _ => format!(
            "方向优势：证据不足（{scope}，{replay} 条，改善区间含0或不可估计；独立前向 {prospective}/{MIN_PROSPECTIVE_NONOVERLAP} 条）"
        ),
    }
}

fn contract_lines(lines: &Map<String, Value>, keys: &[&str]) -> Map<String, Value> {
    let mut map = Map::new();
    for key in keys {
        map.insert(
            (*key).to_string(),
            lines.get(*key).cloned().unwrap_or(Value::Null),
        );
    }
    map
}

/// Compact agent summary (`agent-summary-3.0`).
pub fn build_agent_summary(result: &Value) -> Value {
    let consensus = result.get("consensus").cloned().unwrap_or(Value::Null);
    let snapshot = result.get("snapshot").cloned().unwrap_or(Value::Null);
    let production = production_of(result);
    let evidence = evidence_of(result);
    let lines = required_response_lines(result);
    let window = window_of(result);
    let news = result.get("news_result").cloned().unwrap_or(Value::Null);
    let mut header = Map::new();
    header.insert(
        "ticker".into(),
        snapshot.get("ticker").cloned().unwrap_or(Value::Null),
    );
    header.insert(
        "market".into(),
        snapshot.get("market").cloned().unwrap_or(Value::Null),
    );
    header.insert(
        "horizon".into(),
        snapshot.get("horizon").cloned().unwrap_or(Value::Null),
    );
    header.insert(
        "asof".into(),
        snapshot
            .get("asof_timestamp")
            .cloned()
            .unwrap_or(Value::Null),
    );
    header.insert(
        "decision_session".into(),
        window
            .get("decision_session")
            .cloned()
            .unwrap_or(Value::Null),
    );
    header.insert(
        "policy_version".into(),
        window.get("policy_version").cloned().unwrap_or(Value::Null),
    );
    header.insert(
        "headline".into(),
        Value::Object(contract_lines(
            &lines,
            &[
                "research_window",
                "forecast_target",
                "probability",
                "model_lean",
            ],
        )),
    );

    let mut quant = Map::new();
    for key in [
        "production_probability_eligible",
        "current_prediction_integrity_pass",
        "oos_integrity_pass",
        "directional_edge_supported",
    ] {
        quant.insert(
            key.into(),
            consensus.get(key).cloned().unwrap_or(Value::Null),
        );
    }
    quant.insert("trading_signal_eligible".into(), Value::Bool(false));
    quant.insert(
        "probability_label".into(),
        Value::String("uncalibrated_model_estimate".into()),
    );
    quant.insert(
        "reason_codes".into(),
        consensus
            .get("reason_codes")
            .cloned()
            .unwrap_or_else(|| Value::Array(Vec::new())),
    );
    quant.insert(
        "matching_observations".into(),
        production
            .get("matching_observations")
            .cloned()
            .unwrap_or(Value::Null),
    );
    quant.insert(
        "causal_base_rate".into(),
        production
            .get("causal_base_rate")
            .cloned()
            .unwrap_or(Value::Null),
    );

    let mut replay = Map::new();
    for key in [
        "replay_type",
        "replay_nonoverlap",
        "replay_total",
        "replay_abstained",
        "replay_coverage_ratio",
        "oos_effective_samples",
        "prospective_nonoverlap",
        "directional_edge_state",
        "primary_evidence",
    ] {
        let evidence_key = if key == "primary_evidence" {
            "directional_edge_primary_evidence"
        } else {
            key
        };
        replay.insert(
            key.into(),
            evidence.get(evidence_key).cloned().unwrap_or(Value::Null),
        );
    }
    let mut comparison = Map::new();
    for key in [
        "observations",
        "effective_sample_size",
        "brier_model",
        "brier_causal_base_rate",
        "brier_constant_half",
        "skill",
        "auc",
        "verdict",
    ] {
        comparison.insert(
            key.into(),
            evidence
                .get("baseline_comparison")
                .and_then(|block| block.get(key))
                .cloned()
                .unwrap_or(Value::Null),
        );
    }
    replay.insert("baseline_comparison".into(), Value::Object(comparison));
    replay.insert(
        "calibration_status".into(),
        Value::String("disabled_by_policy".into()),
    );

    let shadow: Vec<Value> = consensus
        .get("shadow_predictions")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .map(|row| {
                    let mut item = Map::new();
                    for key in ["path", "probability", "status", "model_version"] {
                        item.insert(key.into(), row.get(key).cloned().unwrap_or(Value::Null));
                    }
                    Value::Object(item)
                })
                .collect()
        })
        .unwrap_or_default();

    let mut news_block = Map::new();
    news_block.insert(
        "status".into(),
        news.get("status").cloned().unwrap_or(Value::Null),
    );
    news_block.insert(
        "state".into(),
        Value::String(news_status_label(news.get("status").and_then(Value::as_str)).into()),
    );
    news_block.insert(
        "assessed".into(),
        Value::Bool(news_assessed(news.get("status").and_then(Value::as_str))),
    );
    news_block.insert(
        "warning_count".into(),
        news.get("warning_count").cloned().unwrap_or(Value::Null),
    );
    news_block.insert(
        "ai_assessment_status".into(),
        news.get("ai_lean")
            .and_then(|block| block.get("status"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    news_block.insert(
        "direct_label_counts".into(),
        news.get("ai_lean")
            .and_then(|block| block.get("coverage"))
            .and_then(|block| block.get("direct_label_counts"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    news_block.insert(
        "volatility_regime".into(),
        news.get("expected_volatility_regime")
            .and_then(|block| block.get("regime"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    news_block.insert("independent_of_quant".into(), Value::Bool(true));

    let mut sections = Map::new();
    sections.insert("quant".into(), Value::Object(quant));
    sections.insert("replay_and_prospective".into(), Value::Object(replay));
    sections.insert("shadow_predictions".into(), Value::Array(shadow));
    sections.insert(
        "diagnostics".into(),
        consensus
            .get("diagnostics")
            .cloned()
            .unwrap_or_else(|| Value::Object(Map::new())),
    );
    sections.insert("news".into(), Value::Object(news_block));

    let mut summary = Map::new();
    summary.insert("schema".into(), Value::String("agent-summary-3.0".into()));
    for (key, value) in header {
        summary.insert(key, value);
    }
    summary.insert("sections".into(), Value::Object(sections));
    summary.insert(
        "contract".into(),
        Value::Object(contract_lines(
            &lines,
            &[
                "production_path",
                "validation",
                "relative_skill",
                "news",
                "directional_edge",
            ],
        )),
    );
    Value::Object(summary)
}

fn encoded(summary: &Value) -> String {
    serde_json::to_string(summary).unwrap_or_default()
}

/// Trim the summary to the byte budget, dropping optional contract lines from the end.
pub fn enforce_agent_summary_budget(summary: Value, budget: usize) -> Value {
    if encoded(&summary).len() <= budget {
        return summary;
    }
    let mut trimmed = summary;
    if let Some(map) = trimmed.as_object_mut() {
        map.insert("truncated".into(), Value::Bool(true));
        if let Some(sections) = map.get_mut("sections").and_then(Value::as_object_mut) {
            if let Some(shadow) = sections
                .get_mut("shadow_predictions")
                .and_then(Value::as_array_mut)
            {
                shadow.truncate(3);
            }
            sections.insert("diagnostics".into(), Value::Object(Map::new()));
        }
    }
    let mut text = encoded(&trimmed);
    while text.len() > budget {
        let Some(contract) = trimmed
            .as_object_mut()
            .and_then(|map| map.get_mut("contract"))
            .and_then(Value::as_object_mut)
        else {
            break;
        };
        if contract.is_empty() {
            break;
        }
        let first = contract.keys().next().cloned();
        if let Some(first) = first {
            let mut ordered = Map::new();
            for (key, value) in contract.iter() {
                if key != &first {
                    ordered.insert(key.clone(), value.clone());
                }
            }
            let parent = trimmed.as_object_mut().expect("object");
            parent.insert("contract".into(), Value::Object(ordered));
        } else {
            break;
        }
        text = encoded(&trimmed);
    }
    trimmed
}

/// Render the fixed final response block.
pub fn render_final_response(result: &Value) -> String {
    let lines = required_response_lines(result);
    let consensus = result.get("consensus").cloned().unwrap_or(Value::Null);
    let evidence = evidence_of(result);
    let snapshot = result.get("snapshot").cloned().unwrap_or(Value::Null);
    let mut blocks: Vec<String> = Vec::new();
    for key in [
        "research_window",
        "forecast_target",
        "probability",
        "model_lean",
        "directional_edge",
        "production_path",
        "validation",
        "relative_skill",
        "news",
    ] {
        blocks.push(as_string(lines.get(key)));
    }
    blocks.push(String::new());
    blocks.push("证据边界：".into());
    blocks.push("- 概率语义：漂移收缩的因果基率估计；未校准，不可解释为真实涨跌频率".into());
    blocks.push(format!(
        "- 资格状态：computable={}；current_integrity={}；oos_integrity={}；production_eligible={}；directional_edge={}；trading_signal=false",
        as_string(consensus.get("computable")),
        as_string(consensus.get("current_prediction_integrity_pass")),
        as_string(consensus.get("oos_integrity_pass")),
        as_string(consensus.get("production_probability_eligible")),
        as_string(consensus.get("directional_edge_supported"))
    ));
    blocks.push(format!(
        "- 优势证据状态：{}；回放范围={}（{}条，有效样本≈{}）；独立方向优势={}；成熟前向（{}条）",
        evidence
            .get("directional_edge_state")
            .and_then(Value::as_str)
            .unwrap_or("insufficient_evidence"),
        evidence
            .get("directional_edge_evidence_scope")
            .and_then(Value::as_str)
            .unwrap_or("in_window_causal_prefix_replay_not_independent_holdout"),
        evidence
            .get("replay_valid")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        evidence
            .get("oos_effective_samples")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        as_string(evidence.get("directional_edge_independent_supported")),
        evidence
            .get("prospective_nonoverlap")
            .and_then(Value::as_i64)
            .unwrap_or(0)
    ));
    blocks.push(format!(
        "- 回放类型：{}；前缀回放成熟{}条，其中非重叠{}条；真实前向{}条；抽样状态{}",
        as_string(evidence.get("replay_type")),
        evidence
            .get("replay_valid")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        evidence
            .get("replay_nonoverlap")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        evidence
            .get("prospective_nonoverlap")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        evidence
            .get("oos_sampling_status")
            .and_then(Value::as_str)
            .unwrap_or("未知")
    ));
    let shadow_paths: Vec<String> = consensus
        .get("shadow_predictions")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|row| {
                    row.get("path")
                        .and_then(Value::as_str)
                        .map(ToString::to_string)
                })
                .collect()
        })
        .unwrap_or_default();
    blocks.push(format!(
        "- 校准状态：disabled_by_policy；影子路径：{}",
        if shadow_paths.is_empty() {
            "无".to_string()
        } else {
            shadow_paths.join(", ")
        }
    ));
    let disclosure = dividend_label_disclosure();
    blocks.push(format!(
        "- 标签口径：拆股调整=True；分红调整={}；股息翻转率未量化（快照无分红历史）",
        disclosure
            .get("dividend_adjusted")
            .map(python_str)
            .unwrap_or_else(|| "False".into())
    ));
    blocks.push(format!(
        "- 标的：{} / {} / {}；快照 {}",
        as_string(snapshot.get("ticker")),
        as_string(snapshot.get("market")),
        as_string(snapshot.get("horizon")),
        as_string(snapshot.get("snapshot_id"))
    ));
    if let Some(codes) = consensus.get("reason_codes").and_then(Value::as_array) {
        if !codes.is_empty() {
            let rendered: Vec<String> = codes.iter().map(|value| as_string(Some(value))).collect();
            blocks.push(format!("- 原因码：{}", rendered.join(", ")));
        }
    }
    if let Some(vetoes) = result
        .get("adversarial_audit")
        .and_then(|audit| audit.get("vetoes"))
        .and_then(Value::as_array)
    {
        if !vetoes.is_empty() {
            let rendered: Vec<String> = vetoes.iter().map(|value| as_string(Some(value))).collect();
            blocks.push(format!("- 审计否决：{}", rendered.join("；")));
        }
    }
    blocks.push(String::new());
    blocks.push("消息面（AI 标题判断，未核验正文）：".into());
    blocks.extend(major_news_lines(
        &result.get("news_result").cloned().unwrap_or(Value::Null),
        5,
    ));
    blocks.join("\n")
}

/// Render the full markdown report.
pub fn render_markdown(result: &Value) -> String {
    let lines = required_response_lines(result);
    let consensus = result.get("consensus").cloned().unwrap_or(Value::Null);
    let evidence = evidence_of(result);
    let snapshot = result.get("snapshot").cloned().unwrap_or(Value::Null);
    let production = production_of(result);
    let window = window_of(result);
    let news = result.get("news_result").cloned().unwrap_or(Value::Null);
    let audit = result
        .get("adversarial_audit")
        .cloned()
        .unwrap_or(Value::Null);
    let mut out: Vec<String> = vec!["# Stock Data 研究报告".into(), String::new()];
    for value in lines.values() {
        out.push(format!("- {}", as_string(Some(value))));
    }
    out.push(String::new());
    out.push("## 数据与目标".into());
    out.push(String::new());
    out.push(format!(
        "- 决策日：{}",
        as_string(window.get("decision_session"))
    ));
    out.push(format!(
        "- 窗口：({}, {}]",
        as_string(window.get("window_start_exclusive")),
        as_string(window.get("window_end_inclusive"))
    ));
    out.push(format!(
        "- 日历验证：{}；来源 {}；预期交易日 {}；观测 {}；缺失 {}",
        as_string(window.get("calendar_verified")),
        window
            .get("calendar_source")
            .and_then(Value::as_str)
            .unwrap_or("unverified"),
        as_string(window.get("expected_sessions")),
        as_string(window.get("observed_sessions")),
        as_string(window.get("missing_sessions"))
    ));
    out.push(format!(
        "- 价格基础：{}（拆股调整，现金分红不再投资）",
        as_string(window.get("price_basis"))
    ));
    out.push(String::new());
    out.push("## 概率与证据".into());
    out.push(String::new());
    out.push(format!(
        "- 模型倾向：{}",
        lean_label(consensus.get("model_lean").and_then(Value::as_str))
    ));
    out.push(format!(
        "- 当前匹配样本：{}；因果基率：{}",
        as_string(production.get("matching_observations")),
        num(
            production.get("causal_base_rate").and_then(Value::as_f64),
            4
        )
    ));
    out.push(format!(
        "- 前缀回放：类型 {}；有效 {}；非重叠 {}；弃权 {}",
        as_string(evidence.get("replay_type")),
        as_string(evidence.get("replay_valid")),
        as_string(evidence.get("replay_nonoverlap")),
        as_string(evidence.get("replay_abstained"))
    ));
    let momentum = result
        .get("researchers")
        .and_then(|researchers| researchers.get("quant"))
        .and_then(|quant| quant.get("validation"))
        .and_then(|validation| validation.get("momentum_20d"))
        .cloned()
        .unwrap_or(Value::Null);
    if !momentum.is_null()
        && momentum
            .as_object()
            .map(|map| !map.is_empty())
            .unwrap_or(false)
    {
        out.push(format!(
            "- 动量诊断：Score20={}；方向={}；return_20d={}；仅描述当期价格路径，不进入概率，也不与概率作一致性判定",
            num(momentum.get("momentum_score_20d").and_then(Value::as_f64), 2),
            momentum
                .get("momentum_direction")
                .and_then(Value::as_str)
                .unwrap_or("unavailable"),
            pct(momentum.get("return_20d").and_then(Value::as_f64), 2)
        ));
    }
    out.push(format!(
        "- 有效回放样本：{}（=有效条数/5）；对照基线：因果基率与常数0.5",
        as_string(evidence.get("oos_effective_samples"))
    ));
    out.push(format!(
        "- 抽样相位：{}（角色：{}）",
        as_string(evidence.get("oos_sampling_status")),
        as_string(evidence.get("oos_sampling_role"))
    ));
    out.push(format!(
        "- 方向优势状态：{}；主要证据：{}",
        as_string(evidence.get("directional_edge_state")),
        as_string(evidence.get("directional_edge_primary_evidence"))
    ));
    out.push(format!(
        "- 真实前向：非重叠 {}；校准状态 disabled_by_policy",
        as_string(evidence.get("prospective_nonoverlap"))
    ));
    let edge_state = evidence
        .get("directional_edge_state")
        .and_then(Value::as_str)
        .unwrap_or("insufficient_evidence");
    let edge_text = match edge_state {
        "positive_skill_with_estimated_interval_excluding_zero" => {
            "回放区间下界>0（窗口内回放，非独立留出）"
        }
        "negative_skill_with_estimated_interval_excluding_zero" => "回放区间上界<0，劣于纯基率",
        _ => "证据不足（区间含0或不可估计）",
    };
    out.push(format!("- 方向优势（回放）：{edge_text}"));
    out.push(format!(
        "- 独立方向优势：{}（成熟前向 {}/{} 条；抽样状态 {}）",
        if evidence
            .get("directional_edge_independent_supported")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            "有"
        } else {
            "证据不足"
        },
        evidence
            .get("prospective_nonoverlap")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        MIN_PROSPECTIVE_NONOVERLAP,
        evidence
            .get("oos_sampling_status")
            .and_then(Value::as_str)
            .unwrap_or("未知")
    ));
    if consensus
        .get("shadow_predictions")
        .and_then(Value::as_array)
        .map(|rows| !rows.is_empty())
        .unwrap_or(false)
    {
        out.push(String::new());
        out.push("## 影子与诊断（非生产、不可引用、不进入生产概率）".into());
        out.push(String::new());
        out.push(
            "- 本节仅是影子研究/描述性诊断输出：不得作为生产概率、方向结论或交付文本引用；对外只使用 `final_response.md` 的固定九行。".into(),
        );
        if let Some(rows) = consensus
            .get("shadow_predictions")
            .and_then(Value::as_array)
        {
            for row in rows {
                out.push(format!(
                    "- {}：{}；状态 {}",
                    as_string(row.get("path")),
                    pct(row.get("probability").and_then(Value::as_f64), 1),
                    as_string(row.get("status"))
                ));
            }
        }
        let diagnostic = consensus.get("diagnostics").cloned().unwrap_or(Value::Null);
        if diagnostic
            .as_object()
            .map(|map| !map.is_empty())
            .unwrap_or(false)
        {
            out.push(format!(
                "- D 描述性诊断（5D）：IC={}；留出样本={}；descriptive_only=true",
                num(
                    diagnostic
                        .get("information_coefficient")
                        .and_then(Value::as_f64),
                    4
                ),
                as_string(diagnostic.get("holdout_sample_count"))
            ));
        }
    }
    out.push(String::new());
    out.push("## 消息面".into());
    out.push(String::new());
    let warning_count = news
        .get("warning_count")
        .and_then(Value::as_i64)
        .or_else(|| {
            news.get("warnings")
                .and_then(Value::as_array)
                .map(|items| items.len() as i64)
        })
        .unwrap_or(0);
    out.push(format!(
        "- 状态：{}（{}）；已评估：{}；警告：{} 条",
        news.get("status")
            .and_then(Value::as_str)
            .unwrap_or("not_assessed"),
        news_status_label(news.get("status").and_then(Value::as_str)),
        if news_assessed(news.get("status").and_then(Value::as_str)) {
            "True"
        } else {
            "False"
        },
        warning_count
    ));
    out.push(format!("- {}", as_string(lines.get("news"))));
    out.push(format!(
        "- 预期波动状态：{}",
        news.get("expected_volatility_regime")
            .and_then(|block| block.get("regime"))
            .and_then(Value::as_str)
            .unwrap_or("unknown")
    ));
    out.push(format!(
        "- 发布时段分布：{}",
        as_string(news.get("event_time_bucket_counts"))
    ));
    out.push(format!(
        "- 来源一致度：观点 {}；事件 {}",
        as_string(
            news.get("opinion_source_agreement")
                .and_then(|block| block.get("state"))
        ),
        as_string(
            news.get("event_source_agreement")
                .and_then(|block| block.get("state"))
        )
    ));
    out.extend(major_news_lines(&news, 5));
    let ai = news.get("ai_lean").cloned().unwrap_or(Value::Null);
    if matches!(
        ai.get("status").and_then(Value::as_str),
        Some("assessed" | "partial")
    ) {
        out.push(String::new());
        out.push("### 逐条 AI 新闻判断".into());
        out.push(String::new());
        for item in ai
            .get("items")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
        {
            out.push(format!(
                "- {}；{}；{}；{}；{}；理由：{}",
                item.get("published_at")
                    .and_then(Value::as_str)
                    .unwrap_or("时间未提供"),
                item.get("target_relevance")
                    .and_then(Value::as_str)
                    .unwrap_or("unclear"),
                item.get("lean")
                    .and_then(Value::as_str)
                    .unwrap_or("unclear"),
                item.get("importance")
                    .and_then(Value::as_str)
                    .unwrap_or("routine"),
                news_link(&item),
                markdown_text(item.get("reason").unwrap_or(&Value::Null), 400)
            ));
        }
    }
    out.push(String::new());
    out.push("## 审计与限制".into());
    out.push(String::new());
    out.push(format!(
        "- 审计状态：{}；may_report_probability={}",
        as_string(audit.get("status")),
        as_string(audit.get("may_report_probability"))
    ));
    out.push(format!("- 否决：{}", join_or_none(audit.get("vetoes"))));
    out.push(format!("- 警告：{}", join_or_none(audit.get("warnings"))));
    out.push(
        "- 收益为拆股调整收盘价的 5 交易日方向，不含分红再投资；未建模执行、价差、滑点与费用。"
            .into(),
    );
    let label_basis = dividend_label_disclosure();
    out.push(format!(
        "- 标签口径：拆股调整={}；分红调整={}；股息翻转率={}（{}）",
        as_string(label_basis.get("price_basis")),
        as_string(label_basis.get("dividend_adjusted")),
        as_string(label_basis.get("flip_rate")),
        as_string(label_basis.get("status"))
    ));
    out.push(format!(
        "- 快照：{}；政策：{}",
        as_string(snapshot.get("snapshot_id")),
        as_string(window.get("policy_version"))
    ));
    out.join("\n")
}

fn join_or_none(value: Option<&Value>) -> String {
    match value.and_then(Value::as_array) {
        Some(items) if !items.is_empty() => items
            .iter()
            .map(|item| as_string(Some(item)))
            .collect::<Vec<_>>()
            .join("；"),
        _ => "无".to_string(),
    }
}
