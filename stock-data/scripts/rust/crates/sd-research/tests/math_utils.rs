//! Differential parity for the RNG and the math helpers.
//!
//! Reference values are produced by CPython's `random.Random` and
//! `quant_research/math_utils.py`. The bootstrap section only agrees if the whole
//! chain (MT19937 seeding, `randrange` rejection sampling, rank averaging, quantile
//! interpolation) matches, so it is the strongest signal in this file.

use std::path::{Path, PathBuf};

use sd_core::random::PythonRandom;
use sd_research::math_utils::{
    average_ranks, benjamini_yekutieli, circular_block_bootstrap_spearman,
    circular_shift_spearman_p_value, logistic, mean, pearson, quantile, sample_std, spearman,
    wilson_interval,
};
use serde_json::{json, Value};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/math")
}

fn read(name: &str) -> Value {
    serde_json::from_str(&std::fs::read_to_string(fixtures().join(name)).expect("fixture"))
        .expect("json")
}

fn numbers(value: &Value) -> Vec<f64> {
    value
        .as_array()
        .expect("array")
        .iter()
        .map(|item| item.as_f64().expect("number"))
        .collect()
}

fn json_float_option(value: Option<f64>) -> Value {
    value.map(Value::from).unwrap_or(Value::Null)
}

fn assert_gauss_close(actual: &[Value], expected: &Value, label: &str) {
    let expected = expected.as_array().expect("Gaussian reference array");
    assert_eq!(actual.len(), expected.len(), "{label}: length differs");
    for (index, (actual, expected)) in actual.iter().zip(expected).enumerate() {
        let actual = actual.as_f64().expect("Gaussian number");
        let expected = expected.as_f64().expect("Gaussian reference number");
        // Box-Muller uses platform libm sin/cos/log/sqrt; the Windows-generated
        // fixture differs by one ULP on Debian glibc. Only this transcendental
        // transform uses a tiny floating tolerance. MT19937 bits, random() and
        // randrange above remain exact; no probability gate tolerance changes.
        let tolerance = 8.0 * f64::EPSILON * actual.abs().max(expected.abs()).max(1.0);
        assert!(
            actual.is_finite() && (actual - expected).abs() <= tolerance,
            "{label}[{index}]: {actual} != {expected} (tolerance {tolerance})"
        );
    }
}

#[test]
fn python_rng_streams_match() {
    let streams = read("rng.json");
    for stream in streams.as_array().expect("streams") {
        let Some(random_rows) = stream.get("random") else {
            continue;
        };
        let seed = stream["seed"].as_u64().expect("seed");
        let mut rng = PythonRandom::new(seed);
        let random: Vec<Value> = (0..random_rows.as_array().unwrap().len())
            .map(|_| Value::from(rng.random()))
            .collect();
        assert_eq!(
            Value::Array(random),
            stream["random"],
            "random() differs for seed {seed}"
        );
        let bits: Vec<Value> = stream["getrandbits"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| Value::from(rng.getrandbits(item["k"].as_u64().unwrap() as u32)))
            .collect();
        let expected_bits: Vec<Value> = stream["getrandbits"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| item["value"].clone())
            .collect();
        assert_eq!(
            Value::Array(bits),
            Value::Array(expected_bits),
            "getrandbits differs for seed {seed}"
        );
        let ranges: Vec<Value> = stream["randrange"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| Value::from(rng.randrange(item["limit"].as_u64().unwrap())))
            .collect();
        let expected_ranges: Vec<Value> = stream["randrange"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| item["value"].clone())
            .collect();
        assert_eq!(
            Value::Array(ranges),
            Value::Array(expected_ranges),
            "randrange differs for seed {seed}"
        );
        if let Some(expected) = stream.get("gauss") {
            let gauss: Vec<Value> = (0..expected.as_array().unwrap().len())
                .map(|_| Value::from(rng.gauss(0.0, 1.0)))
                .collect();
            assert_gauss_close(&gauss, expected, &format!("gauss seed {seed}"));
        }
    }
    // A second stream with non-default mu/sigma shares the documented cache semantics.
    let scaled = streams
        .as_array()
        .unwrap()
        .iter()
        .find(|stream| stream.get("gauss_scaled").is_some())
        .expect("scaled stream");
    let mut rng = PythonRandom::new(scaled["seed"].as_u64().unwrap());
    let actual: Vec<Value> = (0..scaled["gauss_scaled"].as_array().unwrap().len())
        .map(|_| Value::from(rng.gauss(0.5, 2.0)))
        .collect();
    assert_gauss_close(&actual, &scaled["gauss_scaled"], "scaled gauss");
}

#[test]
fn scalar_helpers_match_python() {
    let documents = read("math-utils.json");
    let cases = json!([[1.0, 2.0, 3.0], [], [1.5, -2.5, 4.0]]);
    let expected_mean = documents["mean"].as_array().unwrap();
    for (index, values) in cases.as_array().unwrap().iter().enumerate() {
        assert_eq!(
            json_float_option(mean(&numbers(values))),
            expected_mean[index],
            "mean differs for case {index}"
        );
    }
    let std_cases = json!([[1.0, 2.0, 3.0, 4.0], [1.0], []]);
    let expected_std = documents["sample_std"].as_array().unwrap();
    for (index, values) in std_cases.as_array().unwrap().iter().enumerate() {
        assert_eq!(
            json_float_option(sample_std(&numbers(values))),
            expected_std[index],
            "sample_std differs for case {index}"
        );
    }
    let rank_cases = json!([[3.0, 1.0, 1.0, 2.0, 2.0, 2.0], [5.0], []]);
    let expected_ranks = documents["average_ranks"].as_array().unwrap();
    for (index, values) in rank_cases.as_array().unwrap().iter().enumerate() {
        let computed: Vec<Value> = average_ranks(&numbers(values))
            .into_iter()
            .map(Value::from)
            .collect();
        assert_eq!(
            Value::Array(computed),
            expected_ranks[index],
            "average_ranks differs for case {index}"
        );
    }
    let quantile_values: Vec<f64> = (1..=10).map(|value| value as f64).collect();
    let expected_quantiles = documents["quantile"].as_array().unwrap();
    for (index, probability) in [0.0, 0.25, 0.5, 0.9, 1.0].iter().enumerate() {
        assert_eq!(
            json_float_option(quantile(&quantile_values, *probability)),
            expected_quantiles[index],
            "quantile differs for p={probability}"
        );
    }
    let expected_wilson = documents["wilson"].as_array().unwrap();
    for (index, pair) in [(30u64, 50u64), (0, 10), (10, 10), (0, 0)]
        .iter()
        .enumerate()
    {
        let expected = &expected_wilson[index];
        match wilson_interval(pair.0, pair.1, 1.96) {
            Some((low, high)) => {
                assert_eq!(Value::from(low), expected[0], "wilson low differs");
                assert_eq!(Value::from(high), expected[1], "wilson high differs");
            }
            None => assert!(expected.is_null(), "wilson must be null for zero count"),
        }
    }
    let expected_logistic = documents["logistic"].as_array().unwrap();
    for (index, value) in [-100.0, -1.0, 0.0, 1.0, 100.0].iter().enumerate() {
        assert_eq!(
            Value::from(logistic(*value)),
            expected_logistic[index],
            "logistic differs"
        );
    }
    let p_values: Vec<(String, Option<f64>)> = vec![
        ("a".into(), Some(0.01)),
        ("b".into(), Some(0.04)),
        ("c".into(), Some(0.03)),
        ("d".into(), None),
        ("e".into(), Some(1.5)),
        ("f".into(), Some(0.5)),
    ];
    let expected_bh = &documents["benjamini_yekutieli"];
    for (label, trials) in [("no_trials", None), ("trials", Some(10usize))] {
        let adjusted = benjamini_yekutieli(&p_values, trials);
        let map: serde_json::Map<String, Value> = adjusted
            .into_iter()
            .map(|(name, value)| (name, json_float_option(value)))
            .collect();
        assert_eq!(
            Value::Object(map),
            expected_bh[label],
            "benjamini_yekutieli differs ({label})"
        );
    }
}

#[test]
fn rank_and_correlation_helpers_match_python() {
    let documents = read("math-utils.json");
    let x = numbers(&documents["series"]["x"]);
    let y = numbers(&documents["series"]["y"]);
    assert_eq!(json_float_option(pearson(&x, &y)), documents["pearson"][0]);
    assert_eq!(
        json_float_option(pearson(&x[..5], &y[..5])),
        documents["pearson"][1]
    );
    assert_eq!(
        json_float_option(spearman(&x, &y)),
        documents["spearman"][0]
    );
    assert_eq!(
        json_float_option(spearman(&x[..5], &y[..5])),
        documents["spearman"][1]
    );
}

#[test]
fn bootstrap_and_circular_shift_match_python() {
    let documents = read("math-utils.json");
    let x = numbers(&documents["series"]["x"]);
    let y = numbers(&documents["series"]["y"]);
    let bootstrap = &documents["bootstrap"];
    let expected_full = &bootstrap["full"];
    assert_eq!(
        circular_block_bootstrap_spearman(&x, &y, 5, 50, 7),
        *expected_full,
        "bootstrap provenance/interval differs"
    );
    assert_eq!(
        circular_block_bootstrap_spearman(&x[..10], &y[..10], 5, 50, 7),
        bootstrap["insufficient"],
        "insufficient-sample bootstrap differs"
    );
    assert_eq!(
        circular_block_bootstrap_spearman(&x, &y, 5, 5, 7),
        bootstrap["unstable"],
        "unstable bootstrap differs"
    );
    let shift = &documents["circular_shift"];
    assert_eq!(
        circular_shift_spearman_p_value(&x, &y, 5),
        shift["full"],
        "circular-shift p-value differs"
    );
    assert_eq!(
        circular_shift_spearman_p_value(&x[..30], &y[..30], 40),
        shift["insufficient"],
        "insufficient circular-shift case differs"
    );
}

#[test]
fn optional_values_are_none_not_zero() {
    // Guard against silently filling missing statistics with 0.0.
    assert!(mean(&[]).is_none());
    assert!(sample_std(&[1.0]).is_none());
    assert!(pearson(&[1.0, 2.0], &[1.0, 2.0]).is_none());
    assert!(quantile(&[], 0.5).is_none());
    assert!(wilson_interval(0, 0, 1.96).is_none());
}
