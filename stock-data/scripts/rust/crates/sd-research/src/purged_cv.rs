//! Closed-interval purging, embargo and causal walk-forward folds
//! (`validation/purged_cv.py`).
//!
//! The interval is `[information_start, label_end]`: touching an endpoint is an overlap
//! and is purged, because a label that matures exactly when the test window begins still
//! shares the boundary session. Walk-forward folds are causal: every training label leaves
//! the book before the test block's information window starts.

use serde_json::{Map, Value};

/// One sample's causal footprint.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SampleInterval {
    /// Session whose close the decision uses.
    pub decision_session: i64,
    /// First session the features read.
    pub information_start: i64,
    /// Session whose close resolves the label.
    pub label_end: i64,
}

/// One purged or walk-forward fold.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fold {
    /// Training-row indices.
    pub train: Vec<i64>,
    /// Held-out row indices.
    pub test: Vec<i64>,
    /// Rows removed because their label overlaps the test window.
    pub purged: i64,
    /// Rows removed by the fixed embargo gap.
    pub embargoed: i64,
}

impl Fold {
    /// JSON view with the legacy key order.
    pub fn to_value(&self) -> Value {
        let mut map = Map::new();
        map.insert(
            "train".into(),
            Value::Array(self.train.iter().map(|index| Value::from(*index)).collect()),
        );
        map.insert(
            "test".into(),
            Value::Array(self.test.iter().map(|index| Value::from(*index)).collect()),
        );
        map.insert("purged".into(), Value::from(self.purged));
        map.insert("embargoed".into(), Value::from(self.embargoed));
        Value::Object(map)
    }
}

/// Python `round()`: half-to-even, not half-away-from-zero.
fn python_round(value: f64) -> i64 {
    let floor = value.floor();
    let fraction = value - floor;
    if fraction > 0.5 || (fraction == 0.5 && (floor as i64) % 2 != 0) {
        floor as i64 + 1
    } else {
        floor as i64
    }
}

fn overlap(a_start: i64, a_end: i64, b_start: i64, b_end: i64) -> bool {
    // Closed intervals: touching an endpoint is an overlap and must be purged.
    a_start <= b_end && b_start <= a_end
}

/// Generate contiguous test blocks and purge any intersecting training interval.
pub fn purged_kfold(
    intervals: &[SampleInterval],
    n_splits: usize,
    embargo_sessions: i64,
) -> Result<Vec<Fold>, String> {
    let n = intervals.len() as i64;
    if n_splits < 2 || n < n_splits as i64 {
        return Ok(Vec::new());
    }
    if embargo_sessions < 0 {
        return Err("embargo_sessions must be nonnegative".into());
    }
    let boundaries: Vec<i64> = (0..=n_splits)
        .map(|index| python_round(index as f64 * n as f64 / n_splits as f64))
        .collect();
    let mut folds: Vec<Fold> = Vec::new();
    for fold_number in 0..n_splits {
        let test: Vec<i64> = (boundaries[fold_number]..boundaries[fold_number + 1]).collect();
        if test.is_empty() {
            continue;
        }
        let test_start = test
            .iter()
            .map(|index| intervals[*index as usize].information_start)
            .min()
            .unwrap_or(0);
        let test_end = test
            .iter()
            .map(|index| intervals[*index as usize].label_end)
            .max()
            .unwrap_or(0);
        let test_decision_end = test
            .iter()
            .map(|index| intervals[*index as usize].decision_session)
            .max()
            .unwrap_or(0);
        let candidate: Vec<i64> = (0..n).filter(|index| !test.contains(index)).collect();
        let mut keep: Vec<i64> = Vec::new();
        let mut purged = 0i64;
        let mut embargoed = 0i64;
        for index in candidate {
            let interval = intervals[index as usize];
            if overlap(
                interval.information_start,
                interval.label_end,
                test_start,
                test_end,
            ) {
                purged += 1;
                continue;
            }
            if test_decision_end < interval.decision_session
                && interval.decision_session <= test_decision_end + embargo_sessions
            {
                embargoed += 1;
                continue;
            }
            keep.push(index);
        }
        folds.push(Fold {
            train: keep,
            test,
            purged,
            embargoed,
        });
    }
    Ok(folds)
}

/// Options of [`causal_walk_forward`].
pub struct WalkForwardOptions {
    /// Number of expanding-window test blocks.
    pub n_splits: usize,
    /// Minimum training rows before the first test block.
    pub minimum_train: i64,
    /// Fixed gap between the last training decision and the first test decision.
    pub pre_test_gap_sessions: i64,
    /// Fraction of the book used as warm-up when `block_sessions` is `None`.
    pub first_test_floor_fraction: f64,
    /// Absolute test block length; `None` derives it from the history length.
    pub block_sessions: Option<i64>,
}

impl Default for WalkForwardOptions {
    fn default() -> Self {
        Self {
            n_splits: 5,
            minimum_train: 100,
            pre_test_gap_sessions: 0,
            first_test_floor_fraction: 0.5,
            block_sessions: None,
        }
    }
}

/// Expanding-window validation. Every training label ends before the test starts.
pub fn causal_walk_forward(
    intervals: &[SampleInterval],
    options: &WalkForwardOptions,
) -> Result<Vec<Fold>, String> {
    if options.n_splits < 1 || options.pre_test_gap_sessions < 0 {
        return Err("invalid walk-forward options".into());
    }
    if !options.first_test_floor_fraction.is_finite()
        || !(0.0..=1.0).contains(&options.first_test_floor_fraction)
    {
        return Err("first_test_floor_fraction must be finite and within [0, 1]".into());
    }
    if let Some(block) = options.block_sessions {
        if block < 1 {
            return Err("block_sessions must be a positive integer when provided".into());
        }
    }
    let n = intervals.len() as i64;
    let (first_test, block) = match options.block_sessions {
        None => {
            let derived = (options.minimum_train + options.pre_test_gap_sessions)
                .max((n as f64 * options.first_test_floor_fraction) as i64);
            (
                derived,
                1.max((n - derived) / options.n_splits.max(1) as i64),
            )
        }
        Some(block) => (options.minimum_train + options.pre_test_gap_sessions, block),
    };
    if first_test >= n {
        return Ok(Vec::new());
    }
    let mut folds: Vec<Fold> = Vec::new();
    let mut start = first_test;
    while start < n {
        let stop = n.min(start + block);
        let test: Vec<i64> = (start..stop).collect();
        let first_decision = test
            .iter()
            .map(|index| intervals[*index as usize].decision_session)
            .min()
            .unwrap_or(0);
        let test_information_start = test
            .iter()
            .map(|index| intervals[*index as usize].information_start)
            .min()
            .unwrap_or(0);
        let mut train: Vec<i64> = Vec::new();
        let mut purged = 0i64;
        let mut embargoed = 0i64;
        for index in 0..start {
            let interval = intervals[index as usize];
            if first_decision - options.pre_test_gap_sessions <= interval.decision_session
                && interval.decision_session < first_decision
            {
                embargoed += 1;
                continue;
            }
            if interval.label_end >= test_information_start {
                purged += 1;
                continue;
            }
            train.push(index);
        }
        if train.len() as i64 >= options.minimum_train {
            folds.push(Fold {
                train,
                test,
                purged,
                embargoed,
            });
        }
        start = stop;
    }
    Ok(folds)
}

/// Number of training intervals whose label overlaps any test interval.
pub fn count_interval_overlaps(intervals: &[SampleInterval], train: &[i64], test: &[i64]) -> i64 {
    let mut count = 0i64;
    for index in train {
        let left = intervals[*index as usize];
        let intersects = test.iter().any(|other| {
            let right = intervals[*other as usize];
            overlap(
                left.information_start,
                left.label_end,
                right.information_start,
                right.label_end,
            )
        });
        if intersects {
            count += 1;
        }
    }
    count
}

/// JSON view of a fold list.
pub fn folds_to_value(folds: &[Fold]) -> Value {
    Value::Array(folds.iter().map(Fold::to_value).collect())
}
