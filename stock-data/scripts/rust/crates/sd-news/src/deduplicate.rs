//! Conservatively merge repeated reports without counting copies as new events
//! (`news/deduplicate.py`).
//!
//! Exact IDs/URLs are removed first (with tracking parameters stripped). Remaining
//! same-class headlines are merged only when the wording is close and the reports fall
//! inside a 72-hour window, so a copied wire story is not counted as independent evidence.

use std::collections::{BTreeSet, HashMap};

use chrono::{DateTime, Duration, Utc};
use regex::Regex;
use serde_json::{Map, Value};

use sd_core::contracts::parse_timestamp;

use crate::normalize::{isoformat_z, record_role};

/// Percent-decode a URL query component (Python `parse_qsl` gives decoded strings).
fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut index = 0usize;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => {
                out.push(b' ');
                index += 1;
            }
            b'%' if index + 2 < bytes.len() => {
                let high = (bytes[index + 1] as char).to_digit(16);
                let low = (bytes[index + 2] as char).to_digit(16);
                match (high, low) {
                    (Some(high), Some(low)) => {
                        out.push((high * 16 + low) as u8);
                        index += 3;
                    }
                    _ => {
                        out.push(bytes[index]);
                        index += 1;
                    }
                }
            }
            byte => {
                out.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Python `urllib.parse.quote_plus` restricted to the safe set used by `urlencode`.
fn quote_plus(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-' | b'~') {
            out.push(byte as char);
        } else if byte == b' ' {
            out.push('+');
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

fn decode_component(text: &str) -> String {
    percent_decode(text)
}

/// Strip tracking parameters and normalize a URL for identity comparison.
pub fn clean_url(value: Option<&str>) -> Option<String> {
    let value = value?;
    if value.is_empty() {
        return None;
    }
    let trimmed = value.trim();
    let (scheme, rest) = match trimmed.split_once("://") {
        Some((scheme, rest)) => (scheme.to_lowercase(), rest),
        None => (String::new(), trimmed),
    };
    let (rest, fragment) = match rest.split_once('#') {
        Some((rest, fragment)) => (rest, Some(fragment)),
        None => (rest, None),
    };
    let _ = fragment;
    let (rest, query) = match rest.split_once('?') {
        Some((rest, query)) => (rest, Some(query)),
        None => (rest, None),
    };
    let (netloc, path) = match rest.split_once('/') {
        Some((netloc, path)) => (netloc.to_lowercase(), format!("/{path}")),
        None => (rest.to_lowercase(), String::new()),
    };
    let path = path.trim_end_matches('/').to_string();
    let mut pairs: Vec<(String, String)> = Vec::new();
    if let Some(query) = query {
        for pair in query.split('&') {
            if pair.is_empty() {
                continue;
            }
            let (key, value) = match pair.split_once('=') {
                Some((key, value)) => (decode_component(key), decode_component(value)),
                None => (decode_component(pair), String::new()),
            };
            let lowered = key.to_lowercase();
            if lowered.starts_with("utm_") || ["fbclid", "gclid"].contains(&lowered.as_str()) {
                continue;
            }
            pairs.push((key, value));
        }
    }
    let encoded: Vec<String> = pairs
        .iter()
        .map(|(key, value)| format!("{}={}", quote_plus(key), quote_plus(value)))
        .collect();
    let mut result = String::new();
    if !scheme.is_empty() {
        result.push_str(&scheme);
        result.push_str("://");
    }
    result.push_str(&netloc);
    result.push_str(&path);
    if !encoded.is_empty() {
        result.push('?');
        result.push_str(&encoded.join("&"));
    }
    if result.is_empty() {
        None
    } else {
        Some(result)
    }
}

pub fn title_key(value: &str) -> String {
    let pattern = Regex::new(r"[\w]+").expect("word regex");
    pattern
        .find_iter(&value.to_lowercase())
        .map(|found| found.as_str().to_string())
        .collect::<Vec<_>>()
        .join(" ")
}

fn exact_keys(article: &Value) -> BTreeSet<String> {
    let mut keys: BTreeSet<String> = BTreeSet::new();
    let source = article
        .get("source")
        .map(|value| sd_core::contracts::py_str(value).trim().to_lowercase())
        .unwrap_or_else(|| "unknown".into());
    if let Some(article_id) = article.get("article_id") {
        if !matches!(article_id, Value::Null | Value::Bool(false))
            && !sd_core::contracts::py_str(article_id).trim().is_empty()
        {
            keys.insert(format!(
                "id:{source}:{}",
                sd_core::contracts::py_str(article_id).trim().to_lowercase()
            ));
        }
    }
    if let Some(url) = clean_url(article.get("url").and_then(Value::as_str)) {
        keys.insert(format!("url:{url}"));
    }
    keys
}

/// Sequence matcher ratio matching Python's `difflib.SequenceMatcher.ratio`.
fn sequence_ratio(left: &[char], right: &[char]) -> f64 {
    let matches = matching_blocks(left, right);
    let total = left.len() + right.len();
    if total == 0 {
        return 1.0;
    }
    let matched: usize = matches.iter().map(|(size, _, _)| *size).sum();
    2.0 * matched as f64 / total as f64
}

/// Python `SequenceMatcher(b_autojunk=False)` matching blocks for short titles.
fn matching_blocks(left: &[char], right: &[char]) -> Vec<(usize, usize, usize)> {
    let mut blocks: Vec<(usize, usize, usize)> = Vec::new();
    let mut queue: Vec<(usize, usize, usize, usize)> = vec![(0, left.len(), 0, right.len())];
    let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
    for (index, character) in right.iter().enumerate() {
        b2j.entry(*character).or_default().push(index);
    }
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (best_i, best_j, best_size) = longest_match(left, &b2j, alo, ahi, blo, bhi);
        if best_size > 0 {
            blocks.push((best_size, best_i, best_j));
            if alo < best_i && blo < best_j {
                queue.push((alo, best_i, blo, best_j));
            }
            if best_i + best_size < ahi && best_j + best_size < bhi {
                queue.push((best_i + best_size, ahi, best_j + best_size, bhi));
            }
        }
    }
    blocks.sort_by_key(|left| (left.1, left.2));
    // Merge adjacent blocks exactly like `get_matching_blocks`.
    let mut merged: Vec<(usize, usize, usize)> = Vec::new();
    for (size, i, j) in blocks {
        if let Some(last) = merged.last_mut() {
            if last.1 + last.0 == i && last.2 + last.0 == j {
                last.0 += size;
                last.1 = i;
                last.2 = j;
                continue;
            }
        }
        merged.push((size, i, j));
    }
    merged
}

fn longest_match(
    left: &[char],
    b2j: &HashMap<char, Vec<usize>>,
    alo: usize,
    ahi: usize,
    blo: usize,
    bhi: usize,
) -> (usize, usize, usize) {
    let mut best_i = alo;
    let mut best_j = blo;
    let mut best_size = 0usize;
    let mut previous: HashMap<usize, usize> = HashMap::new();
    for (i, character) in left.iter().enumerate().take(ahi).skip(alo) {
        let mut current: HashMap<usize, usize> = HashMap::new();
        if let Some(positions) = b2j.get(character) {
            for j in positions {
                if *j < blo {
                    continue;
                }
                if *j >= bhi {
                    break;
                }
                let size = previous.get(&(j.saturating_sub(1))).copied().unwrap_or(0) + 1;
                current.insert(*j, size);
                if size > best_size {
                    best_size = size;
                    best_i = i + 1 - size;
                    best_j = j + 1 - size;
                }
            }
        }
        previous = current;
    }
    (best_i, best_j, best_size)
}

fn canonical(value: &Value) -> String {
    serde_json::to_string(value).unwrap_or_default()
}

/// Result of deduplication: clusters plus the number of absorbed duplicates.
pub struct Deduplication {
    /// Clusters in reverse chronological order.
    pub clusters: Vec<Value>,
    /// Number of records absorbed as duplicates.
    pub duplicate_count: i64,
}

/// Cluster the normalized articles.
pub fn deduplicate_articles(articles: &[Value]) -> Deduplication {
    let mut sorted: Vec<Value> = articles.to_vec();
    // Python sorts by the raw published_at string; stable order is preserved on ties.
    sorted.sort_by_key(|article| {
        article
            .get("published_at")
            .map(sd_core::contracts::py_str)
            .unwrap_or_default()
    });
    let mut exact_seen: HashMap<String, BTreeSet<String>> = HashMap::new();
    let mut unique: Vec<Value> = Vec::new();
    let mut duplicate_count = 0i64;
    for article in sorted {
        let role = record_role(&article);
        let identities: BTreeSet<String> = exact_keys(&article)
            .into_iter()
            .map(|identity| format!("{role}:{identity}"))
            .collect();
        let tickers = article
            .get("tickers")
            .cloned()
            .unwrap_or(Value::Array(Vec::new()));
        let fingerprint = format!(
            "{}|{}|{}|{}|{}",
            sd_core::contracts::py_str(article.get("title").unwrap_or(&Value::Null)).to_lowercase(),
            sd_core::contracts::py_str(article.get("summary").unwrap_or(&Value::Null))
                .to_lowercase(),
            canonical(&tickers),
            canonical(article.get("occurred_at").unwrap_or(&Value::Null)),
            canonical(article.get("published_at").unwrap_or(&Value::Null)),
        );
        if identities.iter().any(|identity| {
            exact_seen
                .get(identity)
                .map(|seen| seen.contains(&fingerprint))
                .unwrap_or(false)
        }) {
            duplicate_count += 1;
            continue;
        }
        for identity in &identities {
            exact_seen
                .entry(identity.clone())
                .or_default()
                .insert(fingerprint.clone());
        }
        unique.push(article);
    }

    struct Cluster {
        role: &'static str,
        first_published: DateTime<Utc>,
        title_key: String,
        articles: Vec<Value>,
    }
    let mut clusters: Vec<Cluster> = Vec::new();
    for article in unique {
        let key = title_key(&sd_core::contracts::py_str(
            article.get("title").unwrap_or(&Value::Null),
        ));
        let published = parse_timestamp(article.get("published_at").unwrap_or(&Value::Null))
            .unwrap_or_else(|_| DateTime::<Utc>::from_timestamp(0, 0).expect("epoch"));
        let role = record_role(&article);
        let article_tickers: BTreeSet<String> = article
            .get("tickers")
            .and_then(Value::as_array)
            .map(|items| items.iter().map(sd_core::contracts::py_str).collect())
            .unwrap_or_default();
        let mut matched: Option<usize> = None;
        for index in (0..clusters.len()).rev() {
            let cluster = &clusters[index];
            if cluster.role != role {
                continue;
            }
            let previous = &cluster.articles[0];
            let previous_tickers: BTreeSet<String> = previous
                .get("tickers")
                .and_then(Value::as_array)
                .map(|items| items.iter().map(sd_core::contracts::py_str).collect())
                .unwrap_or_default();
            if article_tickers != previous_tickers {
                continue;
            }
            let article_occurred = article.get("occurred_at").and_then(Value::as_str);
            let previous_occurred = previous.get("occurred_at").and_then(Value::as_str);
            if let (Some(article_occurred), Some(previous_occurred)) =
                (article_occurred, previous_occurred)
            {
                let article_day = article_occurred.get(..10).unwrap_or(article_occurred);
                let previous_day = previous_occurred.get(..10).unwrap_or(previous_occurred);
                if article_day != previous_day {
                    continue;
                }
            }
            let canonical_article = article.get("canonical_event_id").and_then(Value::as_str);
            let canonical_previous = previous.get("canonical_event_id").and_then(Value::as_str);
            if let (Some(article_id), Some(previous_id)) = (canonical_article, canonical_previous) {
                if article_id == previous_id {
                    matched = Some(index);
                    break;
                }
            }
            let delta = (published - cluster.first_published).num_seconds().abs();
            if delta > Duration::hours(72).num_seconds() {
                continue;
            }
            if key.is_empty() || cluster.title_key.is_empty() {
                continue;
            }
            let similarity = sequence_ratio(
                &key.chars().collect::<Vec<_>>(),
                &cluster.title_key.chars().collect::<Vec<_>>(),
            );
            if key == cluster.title_key || similarity >= 0.88 {
                matched = Some(index);
                break;
            }
        }
        match matched {
            None => clusters.push(Cluster {
                role,
                first_published: published,
                title_key: key,
                articles: vec![article],
            }),
            Some(index) => {
                let cluster = &mut clusters[index];
                cluster.articles.push(article);
                cluster.first_published = cluster.first_published.min(published);
                duplicate_count += 1;
            }
        }
    }

    let mut result: Vec<Value> = Vec::new();
    for cluster in &clusters {
        let rows = &cluster.articles;
        let first_published = rows
            .iter()
            .filter_map(|row| parse_timestamp(row.get("published_at").unwrap_or(&Value::Null)).ok())
            .min()
            .unwrap_or(cluster.first_published);
        // Python `max` keeps the first maximum; the comparison is title+summary length.
        let mut representative = &rows[0];
        let mut best_length = usize::MIN;
        for row in rows {
            let length = sd_core::contracts::py_str(row.get("title").unwrap_or(&Value::Null))
                .chars()
                .count()
                + sd_core::contracts::py_str(row.get("summary").unwrap_or(&Value::Null))
                    .chars()
                    .count();
            if length > best_length {
                best_length = length;
                representative = row;
            }
        }
        // Python's `datetime.isoformat()` prints +00:00 (and six digits when micros
        // are nonzero); the cluster id must hash that exact string.
        let stamp = if first_published.timestamp_subsec_micros() == 0 {
            first_published
                .format("%Y-%m-%dT%H:%M:%S+00:00")
                .to_string()
        } else {
            first_published
                .format("%Y-%m-%dT%H:%M:%S%.6f+00:00")
                .to_string()
        };
        let identity_seed = format!("{stamp}|{}|{}", cluster.title_key, cluster.role);
        let digest = sha256_hex(identity_seed.as_bytes());
        let cluster_id = format!("cluster_{}", &digest[..12]);
        let mut sources: BTreeSet<String> = BTreeSet::new();
        let mut independent: BTreeSet<String> = BTreeSet::new();
        for row in rows {
            sources.insert(
                row.get("source")
                    .map(sd_core::contracts::py_str)
                    .unwrap_or_else(|| "unknown".into()),
            );
            if let Some(value) = row.get("original_reporting_id") {
                if !matches!(value, Value::Null) && !sd_core::contracts::py_str(value).is_empty() {
                    independent.insert(sd_core::contracts::py_str(value));
                }
            }
        }
        let mut map = Map::new();
        map.insert("cluster_id".into(), Value::String(cluster_id));
        map.insert(
            "first_published_at".into(),
            Value::String(isoformat_z(first_published)),
        );
        map.insert("representative".into(), representative.clone());
        map.insert("articles".into(), Value::Array(rows.clone()));
        map.insert(
            "sources".into(),
            Value::Array(sources.into_iter().map(Value::String).collect()),
        );
        map.insert(
            "independent_reporting_ids".into(),
            Value::Array(independent.into_iter().map(Value::String).collect()),
        );
        map.insert("article_count".into(), Value::from(rows.len()));
        map.insert("role".into(), Value::String(cluster.role.into()));
        map.insert("is_opinion".into(), Value::Bool(cluster.role == "opinion"));
        result.push(Value::Object(map));
    }
    result.sort_by(|left, right| {
        right
            .get("first_published_at")
            .map(sd_core::contracts::py_str)
            .cmp(
                &left
                    .get("first_published_at")
                    .map(sd_core::contracts::py_str),
            )
    });
    Deduplication {
        clusters: result,
        duplicate_count,
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    let digest = hasher.finalize();
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}
