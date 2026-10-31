//! Present frozen Quant and News results as independent research sections
//! (`synthesis.py`).
//!
//! News never edits Quant fields: the synthesis copies Quant's production fields and only
//! reports the News lean as an independent, display-only section.

use serde_json::{Map, Value};

use sd_core::security::bounded_text;

fn bounded(message: &Value) -> String {
    bounded_text(message, 1800)
}

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

/// Copy Quant's production fields; News never edits them.
pub fn reportable_quant_fields(quant_result: &Value) -> Value {
    let quant = quant_result
        .get("consensus")
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    let probability = quant.get("prob_up").and_then(Value::as_f64);
    let direction = quant
        .get("direction")
        .map(sd_core::contracts::py_str)
        .unwrap_or_else(|| "unavailable".into());
    let allowed = matches!(direction.as_str(), "bullish" | "bearish" | "neutral")
        && probability
            .map(|value| value.is_finite() && (0.0..=1.0).contains(&value))
            .unwrap_or(false)
        && quant_result
            .get("adversarial_audit")
            .and_then(|audit| audit.get("may_report_direction"))
            .and_then(Value::as_bool)
            == Some(true);
    let mut fields = Map::new();
    fields.insert(
        "prob_up".into(),
        quant.get("prob_up").cloned().unwrap_or(Value::Null),
    );
    fields.insert(
        "raw_probability".into(),
        quant.get("raw_probability").cloned().unwrap_or(Value::Null),
    );
    fields.insert(
        "direction".into(),
        quant.get("direction").cloned().unwrap_or(Value::Null),
    );
    fields.insert(
        "model_lean".into(),
        quant.get("model_lean").cloned().unwrap_or(Value::Null),
    );
    fields.insert(
        "probability_label".into(),
        Value::String("uncalibrated_model_estimate".into()),
    );
    fields.insert("calibrated_probability".into(), Value::Null);
    fields.insert(
        "production_probability_eligible".into(),
        Value::Bool(python_truthy(quant.get("production_probability_eligible"))),
    );
    fields.insert(
        "directional_edge_supported".into(),
        Value::Bool(python_truthy(quant.get("directional_edge_supported"))),
    );
    fields.insert(
        "evidence".into(),
        quant
            .get("evidence")
            .filter(|value| python_truthy(Some(value)))
            .cloned()
            .unwrap_or_else(|| Value::Object(Map::new())),
    );
    fields.insert(
        "reason_codes".into(),
        quant
            .get("reason_codes")
            .filter(|value| python_truthy(Some(value)))
            .cloned()
            .unwrap_or_else(|| Value::Array(Vec::new())),
    );
    fields.insert("confidence_score".into(), Value::Null);
    fields.insert("confidence_score_deprecated".into(), Value::Bool(true));
    if allowed {
        fields.insert(
            "reporting_status".into(),
            Value::String("reportable_uncalibrated".into()),
        );
    } else {
        fields.insert("prob_up".into(), Value::Null);
        fields.insert("raw_probability".into(), Value::Null);
        fields.insert(
            "direction".into(),
            Value::String("research_unavailable".into()),
        );
        fields.insert(
            "reporting_status".into(),
            Value::String("unavailable".into()),
        );
    }
    Value::Object(fields)
}

/// Build the independent Quant/News synthesis block.
pub fn build_synthesis(quant_result: &Value, news_result: &Value) -> Result<Value, String> {
    let quant = quant_result
        .get("consensus")
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    let snapshot = quant_result
        .get("snapshot")
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    if python_truthy(Some(&snapshot)) {
        for key in [
            "ticker",
            "market",
            "horizon",
            "snapshot_id",
            "asof_timestamp",
        ] {
            let left = news_result.get(key).cloned().unwrap_or(Value::Null);
            let right = snapshot.get(key).cloned().unwrap_or(Value::Null);
            if left != right {
                return Err("Quant/News identity or as-of mismatch".into());
            }
        }
    }
    let fields = reportable_quant_fields(quant_result);
    let mut viability = quant
        .get("economic_viability")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if !viability.contains_key("status") {
        viability.insert("status".into(), Value::String("not_assessed".into()));
    }
    viability.insert(
        "source".into(),
        Value::String("quant_result.json.consensus.economic_viability".into()),
    );
    viability.insert("single_source".into(), Value::Bool(true));
    let quant_probability = fields.get("prob_up").cloned().unwrap_or(Value::Null);
    let ai = news_result
        .get("ai_lean")
        .cloned()
        .unwrap_or(Value::Object(Map::new()));

    let mut risk_rows: Vec<Value> = Vec::new();
    let mut seen: Vec<(String, String)> = Vec::new();
    let push_risk = |source: &str,
                     message: &Value,
                     severity: &str,
                     rows: &mut Vec<Value>,
                     seen: &mut Vec<(String, String)>| {
        let detail = bounded(message);
        let key = (source.to_string(), detail.clone());
        if seen.contains(&key) {
            return;
        }
        seen.push(key);
        rows.push(serde_json::json!({
            "source": source,
            "detail": detail,
            "severity": severity,
        }));
    };
    for message in quant_result
        .get("snapshot")
        .and_then(|snapshot| snapshot.get("data_validation"))
        .and_then(|validation| validation.get("blockers"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        push_risk("data_or_audit", &message, "high", &mut risk_rows, &mut seen);
    }
    for message in quant_result
        .get("adversarial_audit")
        .and_then(|audit| audit.get("vetoes"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        push_risk("data_or_audit", &message, "high", &mut risk_rows, &mut seen);
    }
    let mut coverage_messages: Vec<Value> = quant_result
        .get("adversarial_audit")
        .and_then(|audit| audit.get("warnings"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    coverage_messages.extend(
        snapshot
            .get("warnings")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default(),
    );
    for message in coverage_messages {
        push_risk(
            "coverage_or_validation",
            &message,
            "medium",
            &mut risk_rows,
            &mut seen,
        );
    }
    let unique_risks = Value::Array(risk_rows);

    let news_major_bearish: Vec<Value> = ai
        .get("major_direct_items")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter(|row| row.get("lean").and_then(Value::as_str) == Some("bearish"))
                .map(|row| {
                    serde_json::json!({
                        "headline": row.get("headline").cloned().unwrap_or(Value::Null),
                        "url": row.get("url").cloned().unwrap_or(Value::Null),
                        "reason": row.get("reason").cloned().unwrap_or(Value::Null),
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    let news_sentiment = if ai.get("status").and_then(Value::as_str) == Some("assessed") {
        "headline_ai"
    } else {
        "not_assessed"
    };
    let news_event_bias = Value::Null;
    let mut result = Map::new();
    result.insert("quant".into(), fields.clone());
    result.insert(
        "quant_field_source".into(),
        Value::String("quant_result.json.consensus".into()),
    );
    result.insert("economic_viability".into(), Value::Object(viability));
    result.insert("alignment".into(), Value::String("NOT_COMPUTED".into()));
    result.insert(
        "quantitative_bias".into(),
        fields.get("direction").cloned().unwrap_or(Value::Null),
    );
    result.insert("quant_prob_up".into(), quant_probability.clone());
    result.insert("news_event_bias".into(), news_event_bias);
    result.insert("news_evidence_direction".into(), Value::Null);
    result.insert(
        "alignment_basis".into(),
        Value::String("independent_sections".into()),
    );
    result.insert(
        "news_ai_status".into(),
        ai.get("status").cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "news_direct_label_counts".into(),
        ai.get("coverage")
            .and_then(|coverage| coverage.get("direct_label_counts"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    result.insert(
        "news_sentiment".into(),
        Value::String(news_sentiment.into()),
    );
    result.insert(
        "market_confirmation".into(),
        Value::String(
            news_result
                .get("market_confirmation")
                .map(sd_core::contracts::py_str)
                .unwrap_or_else(|| "not_assessed".into()),
        ),
    );
    result.insert("overall_confidence".into(), Value::Null);
    result.insert("overall_confidence_deprecated".into(), Value::Bool(true));
    result.insert(
        "quant_probability_preserved".into(),
        Value::Bool(quant_probability == *quant.get("prob_up").unwrap_or(&Value::Null)),
    );
    result.insert(
        "probability_blending".into(),
        Value::String("prohibited_in_v1".into()),
    );
    result.insert("key_risks".into(), unique_risks);
    result.insert(
        "news_major_bearish".into(),
        Value::Array(news_major_bearish),
    );
    result.insert(
        "news_warnings".into(),
        news_result
            .get("warnings")
            .filter(|value| python_truthy(Some(value)))
            .cloned()
            .unwrap_or_else(|| Value::Array(Vec::new())),
    );
    let mut links = Map::new();
    links.insert(
        "quant_paths".into(),
        Value::Array(
            quant
                .get("shadow_predictions")
                .and_then(Value::as_array)
                .map(|rows| {
                    rows.iter()
                        .map(|row| row.get("path").cloned().unwrap_or(Value::Null))
                        .collect()
                })
                .unwrap_or_default(),
        ),
    );
    links.insert(
        "news_urls".into(),
        Value::Array(
            ai.get("major_direct_items")
                .and_then(Value::as_array)
                .map(|rows| {
                    rows.iter()
                        .map(|row| row.get("url").cloned().unwrap_or(Value::Null))
                        .collect()
                })
                .unwrap_or_default(),
        ),
    );
    result.insert("evidence_links".into(), Value::Object(links));
    result.insert("warnings".into(), Value::Array(Vec::new()));
    Ok(Value::Object(result))
}
