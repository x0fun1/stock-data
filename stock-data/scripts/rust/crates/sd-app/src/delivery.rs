//! Validate a prepared reply against the frozen analysis bundle and its stages
//! (`delivery.py`).
//!
//! The delivery check is the last gate before a reply leaves the runtime: the response
//! must carry every fixed line literally, must not invent conflicting labeled values, the
//! stage receipts must be the unique required sequence bound to this run/snapshot/policy,
//! the frozen News result must match its digest receipt, and a partial AI review may be
//! delivered only when its counts reconcile and the gaps are disclosed.

use std::collections::HashSet;
use std::path::Path;

use regex::Regex;
use serde_json::{Map, Value};

use sd_core::policy::{POLICY_VERSION, REQUIRED_STAGES};
use sd_news::pipeline::verify_frozen_result;

use crate::report::{reporting_status_for, required_response_lines};

fn invisible_pattern() -> Regex {
    Regex::new(r"[\u{200b}-\u{200f}\u{2060}\u{feff}]").expect("compiled invisible pattern")
}

fn leading_marker_pattern() -> Regex {
    Regex::new(r"^(?:(?:[-•*]|\(?\d{1,3}[.)、]|[①-⑳])\s*)+").expect("compiled marker pattern")
}

fn whitespace_pattern() -> Regex {
    Regex::new(r"\s+").expect("compiled whitespace pattern")
}

fn comment_pattern() -> Regex {
    Regex::new(r"(?s)<!--.*?-->").expect("compiled comment pattern")
}

/// Labels whose values are contract-bound; a conflicting value is rejected.
pub const LABELS: [&str; 9] = [
    "研究窗口",
    "预测目标",
    "上涨概率",
    "模型倾向",
    "方向优势",
    "生产路径",
    "验证",
    "相对因果基率技能",
    "新闻",
];

/// Quant-facing vocabulary that must not appear in a delivered reply.
pub const FORBIDDEN: [&str; 9] = [
    "Quant Confidence",
    "置信度评分",
    "置信度支持度评分",
    "校准概率",
    "联合概率校准",
    "报告状态",
    "模型一致度",
    "概率区间",
    "OOS 预测证据等级",
];

const STAGE_STATUSES: [&str; 7] = [
    "complete",
    "partial",
    "skipped",
    "not_applicable",
    "not_assessed",
    "blocked",
    "insufficient_data",
];

fn python_truthy(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Null) | None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().map(|value| value != 0.0).unwrap_or(false),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

/// Normalize one rendered line for contract comparison.
pub fn normalized_line(line: &str) -> String {
    let mut folded = invisible_pattern()
        .replace_all(&line.replace("\\_", "_").replace("\\*", "*"), "")
        .to_string();
    for _ in 0..3 {
        folded = folded
            .trim()
            .trim_matches(|character: char| "*_`#~|\"'“”‘’>".contains(character))
            .trim()
            .to_string();
        folded = folded.replace("**", "").replace('`', "");
        folded = leading_marker_pattern().replace(&folded, "").to_string();
        folded = folded.trim().to_string();
    }
    folded = folded
        .replace('：', ":")
        .replace('（', "(")
        .replace('）', ")")
        .replace('，', ",")
        .replace('；', ";");
    whitespace_pattern()
        .replace_all(&folded, " ")
        .trim_end_matches([':', ';', '。', '.'])
        .trim()
        .to_string()
}

/// Require literal bound fields and reject conflicting labeled values.
pub fn validate_response_text(result: &Value, text: &str) -> Result<(), String> {
    let visible = comment_pattern().replace_all(text, " ").to_string();
    let expected = required_response_lines(result);
    let raw_lines: Vec<String> = visible
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(ToString::to_string)
        .collect();
    let normalized: Vec<String> = raw_lines.iter().map(|line| normalized_line(line)).collect();
    let expected_normalized: HashSet<String> = expected
        .values()
        .map(|value| normalized_line(&as_text(value)))
        .collect();
    let mut missing: Vec<String> = Vec::new();
    for (name, value) in &expected {
        if !normalized.contains(&normalized_line(&as_text(value))) {
            missing.push(name.clone());
        }
    }
    let mut conflicting = false;
    'outer: for line in &raw_lines {
        let folded = normalized_line(line);
        for label in LABELS {
            if folded.starts_with(&normalized_line(label)) && !expected_normalized.contains(&folded)
            {
                conflicting = true;
                break 'outer;
            }
        }
        for token in FORBIDDEN {
            if line.contains(token) {
                conflicting = true;
                break 'outer;
            }
        }
    }
    if !missing.is_empty() || conflicting {
        let mut parts = missing;
        if conflicting {
            parts.push("conflicting/forbidden field".into());
        }
        return Err(format!(
            "Final response failed Quant delivery contract: {}",
            parts.join(", ")
        ));
    }
    Ok(())
}

fn as_text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => sd_core::contracts::python_str(other),
    }
}

pub fn validate_stages(result: &Value) -> Result<Value, String> {
    let Some(stages) = result.get("stages").and_then(Value::as_array) else {
        return Err(
            "Final audit rejected: stage receipts must be unique and exactly the required sequence"
                .into(),
        );
    };
    let names: Vec<String> = stages
        .iter()
        .map(|row| {
            row.get("stage")
                .map(sd_core::contracts::python_str)
                .unwrap_or_default()
        })
        .collect();
    let required: Vec<String> = REQUIRED_STAGES
        .iter()
        .map(|name| (*name).to_string())
        .collect();
    if names != required {
        return Err(
            "Final audit rejected: stage receipts must be unique and exactly the required sequence"
                .into(),
        );
    }
    let run_id = result.get("run_id").cloned().unwrap_or(Value::Null);
    let snapshot_id = result.get("snapshot_id").cloned().unwrap_or(Value::Null);
    for row in stages {
        let status = row
            .get("status")
            .map(sd_core::contracts::python_str)
            .unwrap_or_default();
        if !STAGE_STATUSES.contains(&status.as_str()) {
            return Err(format!(
                "Final audit rejected: invalid stage status '{}'",
                status
            ));
        }
        if status != "complete" && !python_truthy(row.get("reason")) {
            return Err(format!(
                "Final audit rejected: stage {} has no reason",
                row.get("stage")
                    .map(sd_core::contracts::python_str)
                    .unwrap_or_default()
            ));
        }
        if row.get("run_id") != Some(&run_id) || row.get("snapshot_id") != Some(&snapshot_id) {
            return Err(format!(
                "Final audit rejected: stage {} is bound to another run or snapshot",
                row.get("stage")
                    .map(sd_core::contracts::python_str)
                    .unwrap_or_default()
            ));
        }
        if row.get("policy_version").and_then(Value::as_str) != Some(POLICY_VERSION) {
            return Err(format!(
                "Final audit rejected: stage {} uses another policy version",
                row.get("stage")
                    .map(sd_core::contracts::python_str)
                    .unwrap_or_default()
            ));
        }
    }
    let mut summary = Map::new();
    summary.insert("stage_count".into(), Value::from(stages.len()));
    summary.insert(
        "order".into(),
        Value::Array(names.into_iter().map(Value::String).collect()),
    );
    Ok(Value::Object(summary))
}

/// Require a consistent per-headline AI review, and disclose partial coverage.
pub fn require_ai_news_coverage(news: &Value) -> Result<(), String> {
    let ai = news.get("ai_lean").cloned().unwrap_or(Value::Null);
    let coverage = ai.get("coverage").cloned().unwrap_or(Value::Null);
    let candidates = news
        .get("ai_lean_candidates")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let candidate_count = candidates.len();
    if candidate_count == 0 {
        return Ok(());
    }
    let status = ai.get("status").and_then(Value::as_str).unwrap_or("");
    if !matches!(status, "assessed" | "partial") {
        return Err(
            "Final audit rejected: every canonical news headline needs a host AI assessment".into(),
        );
    }
    let coverage_count = coverage.get("candidate_count").and_then(Value::as_i64);
    if coverage_count != Some(candidate_count as i64) {
        return Err(
            "Final audit rejected: AI coverage counts do not match the canonical candidates".into(),
        );
    }
    let Some(assessed_count) = coverage.get("assessed_count").and_then(Value::as_i64) else {
        return Err("Final audit rejected: AI coverage has no assessed count".into());
    };
    let item_count = ai
        .get("items")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0) as i64;
    if item_count != assessed_count {
        return Err("Final audit rejected: AI items do not match the assessed count".into());
    }
    if assessed_count > candidate_count as i64 {
        return Err("Final audit rejected: AI assessed count exceeds the candidate count".into());
    }
    if status == "assessed" && assessed_count != candidate_count as i64 {
        return Err(
            "Final audit rejected: an assessed AI review must cover every canonical headline"
                .into(),
        );
    }
    if status == "partial" && !python_truthy(news.get("warnings")) {
        return Err(
            "Final audit rejected: a partial AI review must disclose why it is partial".into(),
        );
    }
    Ok(())
}

fn read_json(path: &Path) -> Result<Value, String> {
    let text = std::fs::read_to_string(path).map_err(|error| error.to_string())?;
    serde_json::from_str(&text).map_err(|error| error.to_string())
}

/// Validate the final response against the frozen analysis bundle and its stages.
pub fn validate_final_response(analysis_dir: &Path, response_file: &Path) -> Result<Value, String> {
    let result = read_json(&analysis_dir.join("analysis.json"))?;
    if !result.is_object() {
        return Err("Final audit rejected: analysis.json is not an object".into());
    }
    let stage_summary = validate_stages(&result)?;
    let news_path = analysis_dir.join("news_result.json");
    let freeze_path = analysis_dir.join("news_result.freeze.json");
    if !news_path.is_file() || !freeze_path.is_file() {
        return Err(format!(
            "frozen result and receipt are required: {}",
            news_path
                .file_name()
                .map(|name| name.to_string_lossy().to_string())
                .unwrap_or_default()
        ));
    }
    let frozen_news = read_json(&news_path)?;
    let freeze = read_json(&freeze_path)?;
    let result_name = news_path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default();
    verify_frozen_result(&result_name, &frozen_news, &freeze)?;
    if frozen_news != result.get("news_result").cloned().unwrap_or(Value::Null) {
        return Err(
            "Final audit rejected: news_result.json does not match the frozen News stage".into(),
        );
    }
    require_ai_news_coverage(&frozen_news)?;
    let consensus = result
        .get("consensus")
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    let production = consensus
        .get("production")
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    if consensus.get("prob_up") != production.get("prob_up") {
        return Err(
            "Final audit rejected: consensus probability differs from the production path".into(),
        );
    }
    if consensus
        .get("trading_signal_eligible")
        .and_then(Value::as_bool)
        != Some(false)
    {
        return Err("Final audit rejected: trading_signal_eligible must remain false".into());
    }
    let production_paths = consensus
        .get("production_path_count")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if consensus.get("aggregation_method").and_then(Value::as_str) != Some("single_path")
        || production_paths != 1
    {
        return Err("Final audit rejected: production must use exactly one path".into());
    }
    if !consensus.get("prob_up").map(Value::is_null).unwrap_or(true)
        && !python_truthy(consensus.get("production_probability_eligible"))
    {
        return Err("Final audit rejected: ineligible production probability is present".into());
    }
    let response_text = std::fs::read_to_string(response_file)
        .map(|text| text.strip_prefix('\u{feff}').unwrap_or(&text).to_string())
        .map_err(|error| error.to_string())?;
    validate_response_text(&result, &response_text)?;
    let snapshot = result
        .get("snapshot")
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    let mut output = Map::new();
    output.insert("status".into(), Value::String("success".into()));
    output.insert("delivery_status".into(), Value::String("validated".into()));
    output.insert(
        "analysis_id".into(),
        result
            .get("analysis_id")
            .filter(|value| !value.is_null())
            .or_else(|| result.get("run_id"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    output.insert(
        "analysis_status".into(),
        result
            .get("analysis_status")
            .filter(|value| !value.is_null())
            .or_else(|| consensus.get("status"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    output.insert(
        "reporting_status".into(),
        Value::String(reporting_status_for(&result).to_string()),
    );
    output.insert(
        "run_id".into(),
        result.get("run_id").cloned().unwrap_or(Value::Null),
    );
    output.insert(
        "snapshot_id".into(),
        result.get("snapshot_id").cloned().unwrap_or(Value::Null),
    );
    output.insert(
        "policy_version".into(),
        result.get("policy_version").cloned().unwrap_or(Value::Null),
    );
    output.insert(
        "ticker".into(),
        snapshot.get("ticker").cloned().unwrap_or(Value::Null),
    );
    output.insert(
        "horizon".into(),
        snapshot.get("horizon").cloned().unwrap_or(Value::Null),
    );
    output.insert(
        "asof_timestamp".into(),
        snapshot
            .get("asof_timestamp")
            .cloned()
            .unwrap_or(Value::Null),
    );
    output.insert(
        "response_file".into(),
        Value::String(response_file.to_string_lossy().to_string()),
    );
    output.insert(
        "prob_up".into(),
        consensus.get("prob_up").cloned().unwrap_or(Value::Null),
    );
    output.insert(
        "production_probability_eligible".into(),
        Value::Bool(python_truthy(
            consensus.get("production_probability_eligible"),
        )),
    );
    output.insert(
        "directional_edge_supported".into(),
        Value::Bool(python_truthy(consensus.get("directional_edge_supported"))),
    );
    output.insert("stages".into(), stage_summary);
    Ok(Value::Object(output))
}
