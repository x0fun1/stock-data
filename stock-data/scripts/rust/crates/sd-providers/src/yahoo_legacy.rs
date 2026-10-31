//! Seven legacy Yahoo gateway functions (`global_stock_data.py`).
//!
//! These are the pre-yfinance routes and the compatibility fallback targets. Requests and
//! parsing mirror the legacy code exactly: chart JSONP values keep their precision, the
//! quoteSummary projections preserve key order, and the options chain keeps Unix expiries.

use serde_json::{Map, Value};

use crate::common::{assert_us_ticker, int_value, nonempty_text, num_text, num_value, symbol_text};
use crate::http::{HttpRequest, HttpTransport};
use crate::transport::{JobContext, ProviderError};
use crate::yahoo_session::{get_yahoo_session, YahooSession};

const UA: &str = crate::transport::USER_AGENT;
const MAX_UNIX_TIMESTAMP: i64 = 4_102_444_800;

fn val(data: &Value, key: &str) -> Value {
    match data.get(key) {
        Some(Value::Object(inner)) if inner.contains_key("raw") => {
            inner.get("raw").cloned().unwrap_or(Value::Null)
        }
        Some(value) => value.clone(),
        None => Value::Null,
    }
}

fn text_value(value: Option<&Value>) -> Value {
    value.cloned().unwrap_or(Value::Null)
}

/// yfinance action column for one Yahoo chart event block name.
fn action_column(wire_key: &str) -> Option<&'static str> {
    match wire_key {
        "dividends" => Some("Dividends"),
        "splits" => Some("Stock Splits"),
        "capitalGains" | "capital_gains" => Some("Capital Gains"),
        _ => None,
    }
}

fn epoch_seconds(value: &Value) -> Option<i64> {
    if let Some(seconds) = num_value(value) {
        return Some(seconds.trunc() as i64);
    }
    if let Some(text) = value.as_str() {
        if let Ok(seconds) = text.parse::<i64>() {
            return Some(seconds);
        }
        let stamp = chrono::DateTime::parse_from_rfc3339(text).ok()?;
        return Some(stamp.timestamp());
    }
    None
}

fn local_session_date(zone: Option<chrono_tz::Tz>, seconds: i64) -> Option<chrono::NaiveDate> {
    let stamp = chrono::DateTime::<chrono::Utc>::from_timestamp(seconds, 0)?;
    Some(match zone {
        Some(zone) => stamp.with_timezone(&zone).date_naive(),
        None => stamp.date_naive(),
    })
}

fn row_row_date(
    row: &Map<String, Value>,
    zone: Option<chrono_tz::Tz>,
) -> Option<chrono::NaiveDate> {
    if let Some(epoch) = row.get("timestamp_epoch").and_then(epoch_seconds) {
        return local_session_date(zone, epoch);
    }
    if let Some(epoch) = row.get("timestamp").and_then(epoch_seconds) {
        return local_session_date(zone, epoch);
    }
    if let Some(text) = row
        .get("timestamp_utc")
        .or_else(|| row.get("date"))
        .and_then(Value::as_str)
    {
        if let Some(epoch) = epoch_seconds(&Value::String(text.to_string())) {
            return local_session_date(zone, epoch);
        }
        return chrono::NaiveDate::parse_from_str(text, "%Y-%m-%d").ok();
    }
    None
}

/// Map the chart `events` block (`dividends`/`splits`/`capitalGains`) onto per-row yfinance
/// action columns (`Dividends`/`Stock Splits`/`Capital Gains`).
///
/// Yahoo labels each event with epoch seconds at the event session's local market time, so an
/// event belongs to the row whose exchange-timezone session date matches the event's local
/// date. Rows without an event get no column (the normalizer keeps `null`; nothing is
/// zero-filled and `missing_fields`/status are unaffected). Duplicate events for one date sum
/// for cash-like actions and keep the last split ratio.
pub fn attach_chart_events(rows: &mut [Value], meta: &Value, events: &Value) {
    let zone = meta
        .get("exchangeTimezoneName")
        .and_then(Value::as_str)
        .and_then(|name| name.parse::<chrono_tz::Tz>().ok());
    let mut by_date: std::collections::BTreeMap<chrono::NaiveDate, Vec<(&'static str, f64)>> =
        std::collections::BTreeMap::new();
    for wire_key in ["dividends", "splits", "capitalGains", "capital_gains"] {
        let Some(column) = action_column(wire_key) else {
            continue;
        };
        let Some(kind) = events.get(wire_key).and_then(Value::as_object) else {
            continue;
        };
        for (key, event) in kind {
            let Some(seconds) = event
                .get("date")
                .and_then(epoch_seconds)
                .or_else(|| key.parse::<i64>().ok())
            else {
                continue;
            };
            let Some(date) = local_session_date(zone, seconds) else {
                continue;
            };
            // `splits` events carry `numerator`/`denominator`; the yfinance column is the ratio.
            let Some(amount) = event.get("amount").and_then(num_value).or_else(|| {
                let numerator = event.get("numerator").and_then(num_value)?;
                let denominator = event.get("denominator").and_then(num_value)?;
                (denominator != 0.0).then(|| numerator / denominator)
            }) else {
                continue;
            };
            by_date.entry(date).or_default().push((column, amount));
        }
    }
    if by_date.is_empty() {
        return;
    }
    for row in rows.iter_mut() {
        let Some(object) = row.as_object_mut() else {
            continue;
        };
        let Some(date) = row_row_date(object, zone) else {
            continue;
        };
        let Some(entries) = by_date.get(&date) else {
            continue;
        };
        let mut totals: std::collections::BTreeMap<&'static str, f64> =
            std::collections::BTreeMap::new();
        for (column, amount) in entries {
            let updated = match totals.get(column) {
                Some(existing) if *column != "Stock Splits" => existing + amount,
                _ => *amount,
            };
            totals.insert(column, updated);
        }
        for (column, amount) in totals {
            object.insert(column.to_string(), Value::from(amount));
        }
    }
}

/// `stock_kline_yahoo`: chart OHLCV with epoch and UTC timestamps.
pub fn stock_kline_yahoo(
    symbol: Option<&Value>,
    interval: Option<&Value>,
    range_: Option<&Value>,
    include_metadata: bool,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    stock_kline_yahoo_with_prepost(
        symbol,
        interval,
        range_,
        include_metadata,
        false,
        transport,
        context,
    )
}

/// Chart OHLCV with the yfinance `prepost` switch forwarded to the wire
/// (`includePrePost=true` for intraday extended-hours bars). The plain
/// `stock_kline_yahoo` compatibility function keeps `prepost=false`.
pub fn stock_kline_yahoo_with_prepost(
    symbol: Option<&Value>,
    interval: Option<&Value>,
    range_: Option<&Value>,
    include_metadata: bool,
    prepost: bool,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let symbol = match symbol {
        Some(Value::String(text)) => symbol_text(text, "symbol")?,
        _ => return Err(ProviderError::value("Parameter symbol must have type str.")),
    };
    let interval = match interval {
        None => "1d".to_string(),
        Some(Value::String(text)) => text.clone(),
        Some(_) => {
            return Err(ProviderError::value(
                "Parameter interval must have type str.",
            ))
        }
    };
    let range_ = match range_ {
        None => "6mo".to_string(),
        Some(Value::String(text)) => text.clone(),
        Some(_) => return Err(ProviderError::value("Parameter range_ must have type str.")),
    };
    if ![
        "1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo",
    ]
    .contains(&interval.as_str())
    {
        return Err(ProviderError::value("Unsupported Yahoo candle interval."));
    }
    if ![
        "1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max",
    ]
    .contains(&range_.as_str())
    {
        return Err(ProviderError::value("Unsupported Yahoo candle range."));
    }
    let mut query: Vec<(&str, String)> = vec![
        ("interval", interval.clone()),
        ("range", range_),
        ("events", "div,splits".to_string()),
        ("includeAdjustedClose", "true".to_string()),
    ];
    if prepost {
        query.push(("includePrePost", "true".to_string()));
    }
    let request = HttpRequest::new(
        format!("https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"),
        &query,
        15.0,
    )
    .with_header("User-Agent", UA);
    let response = transport.get(&request, context)?;
    response.raise_for_status()?;
    let document = response.json()?;
    let payload = document
        .get("chart")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    if payload
        .get("error")
        .map(|error| !error.is_null())
        .unwrap_or(false)
    {
        return Err(ProviderError::provider(
            "Yahoo chart returned a provider error.",
        ));
    }
    let results = payload
        .get("result")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if results.is_empty() {
        return Ok(Value::Array(Vec::new()));
    }
    let chart = results.first().cloned().unwrap_or(Value::Null);
    let quote = chart
        .get("indicators")
        .and_then(|indicators| indicators.get("quote"))
        .and_then(Value::as_array)
        .and_then(|quotes| quotes.first())
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let timestamps = chart
        .get("timestamp")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut bars: Vec<Value> = Vec::with_capacity(timestamps.len());
    for (index, epoch) in timestamps.iter().enumerate() {
        let Some(seconds) = num_value(epoch) else {
            return Err(ProviderError::provider(
                "Yahoo chart returned an invalid candle timestamp.",
            ));
        };
        let stamp = chrono::DateTime::from_timestamp(seconds.trunc() as i64, 0)
            .ok_or_else(|| ProviderError::provider("Yahoo chart timestamp is out of range."))?;
        let mut candle = Map::new();
        candle.insert(
            "date".into(),
            Value::String(stamp.format("%Y-%m-%d").to_string()),
        );
        candle.insert("timestamp_epoch".into(), epoch.clone());
        candle.insert(
            "timestamp_utc".into(),
            Value::String(stamp.format("%Y-%m-%dT%H:%M:%SZ").to_string()),
        );
        for field in ["open", "high", "low", "close", "volume"] {
            let values = quote
                .get(field)
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            candle.insert(
                field.to_string(),
                values
                    .get(index)
                    .and_then(num_value)
                    .map(Value::from)
                    .unwrap_or(Value::Null),
            );
        }
        bars.push(Value::Object(candle));
    }
    if include_metadata {
        let mut result = Map::new();
        result.insert("bars".into(), Value::Array(bars));
        result.insert(
            "meta".into(),
            chart
                .get("meta")
                .cloned()
                .unwrap_or_else(|| Value::Object(Map::new())),
        );
        result.insert(
            "adjclose".into(),
            chart
                .get("indicators")
                .and_then(|indicators| indicators.get("adjclose"))
                .cloned()
                .unwrap_or(Value::Null),
        );
        result.insert(
            "events".into(),
            chart
                .get("events")
                .cloned()
                .unwrap_or_else(|| Value::Object(Map::new())),
        );
        result.insert("frequency".into(), Value::String(interval));
        result.insert("symbol".into(), Value::String(symbol));
        result.insert(
            "price_basis".into(),
            Value::String("provider_quote_OHLCV_adjustment_unverified".into()),
        );
        result.insert(
            "calendar_status".into(),
            Value::String("not_a_complete_exchange_calendar".into()),
        );
        return Ok(Value::Object(result));
    }
    Ok(Value::Array(bars))
}

/// `yahoo_quote_summary`: one quoteSummary module query.
pub fn yahoo_quote_summary(
    symbol: Option<&Value>,
    modules: Option<&Value>,
    session: &YahooSession,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let symbol = match symbol {
        Some(Value::String(text)) => symbol_text(text, "symbol")?,
        _ => return Err(ProviderError::value("Parameter symbol must have type str.")),
    };
    let modules = match modules {
        Some(Value::Array(items)) if !items.is_empty() => items
            .iter()
            .map(|item| match item.as_str() {
                Some(text) if !text.trim().is_empty() => Ok(text.to_string()),
                _ => Err(ProviderError::value(
                    "modules must be a nonempty list of module names.",
                )),
            })
            .collect::<Result<Vec<String>, ProviderError>>()?,
        _ => {
            return Err(ProviderError::value(
                "modules must be a nonempty list of module names.",
            ))
        }
    };
    let response = session.get(
        transport,
        context,
        &format!("https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"),
        &[
            ("modules", modules.join(",")),
            ("crumb", session.crumb.clone()),
        ],
        15.0,
    )?;
    response.raise_for_status()?;
    let document = response.json()?;
    let payload = document
        .get("quoteSummary")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    if payload
        .get("error")
        .map(|error| !error.is_null())
        .unwrap_or(false)
    {
        return Err(ProviderError::provider(
            "Yahoo quoteSummary returned a provider error.",
        ));
    }
    let results = payload
        .get("result")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    Ok(results
        .first()
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new())))
}

/// `key_statistics`: selected quoteSummary financial fields.
pub fn key_statistics(
    symbol: Option<&Value>,
    session: &YahooSession,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let modules = serde_json::json!(["financialData", "defaultKeyStatistics", "summaryDetail"]);
    let data = yahoo_quote_summary(symbol, Some(&modules), session, transport, context)?;
    let empty = Value::Object(Map::new());
    let fd = data.get("financialData").unwrap_or(&empty);
    let ks = data.get("defaultKeyStatistics").unwrap_or(&empty);
    let sd = data.get("summaryDetail").unwrap_or(&empty);
    let mut result = Map::new();
    result.insert("current_price".into(), val(fd, "currentPrice"));
    result.insert("target_high".into(), val(fd, "targetHighPrice"));
    result.insert("target_low".into(), val(fd, "targetLowPrice"));
    result.insert("target_mean".into(), val(fd, "targetMeanPrice"));
    result.insert(
        "recommendation".into(),
        text_value(fd.get("recommendationKey")),
    );
    result.insert("trailing_pe".into(), val(sd, "trailingPE"));
    result.insert("forward_pe".into(), val(ks, "forwardPE"));
    result.insert("peg_ratio".into(), val(ks, "pegRatio"));
    result.insert("price_to_book".into(), val(ks, "priceToBook"));
    result.insert("enterprise_value".into(), val(ks, "enterpriseValue"));
    result.insert("ev_to_ebitda".into(), val(ks, "enterpriseToEbitda"));
    result.insert("ev_to_revenue".into(), val(ks, "enterpriseToRevenue"));
    result.insert("profit_margin".into(), val(ks, "profitMargins"));
    result.insert("operating_margin".into(), val(fd, "operatingMargins"));
    result.insert("gross_margin".into(), val(fd, "grossMargins"));
    result.insert("return_on_equity".into(), val(fd, "returnOnEquity"));
    result.insert("return_on_assets".into(), val(fd, "returnOnAssets"));
    result.insert("earnings_growth".into(), val(fd, "earningsGrowth"));
    result.insert("revenue_growth".into(), val(fd, "revenueGrowth"));
    result.insert("beta".into(), val(ks, "beta"));
    result.insert("short_ratio".into(), val(ks, "shortRatio"));
    result.insert("dividend_yield".into(), val(sd, "dividendYield"));
    result.insert("payout_ratio".into(), val(ks, "payoutRatio"));
    result.insert("market_cap".into(), val(sd, "marketCap"));
    result.insert("total_revenue".into(), val(fd, "totalRevenue"));
    result.insert("total_cash".into(), val(fd, "totalCash"));
    result.insert("total_debt".into(), val(fd, "totalDebt"));
    Ok(Value::Object(result))
}

/// `analyst_estimates`: EPS trend, rating trend and upgrade/downgrade history.
pub fn analyst_estimates(
    symbol: Option<&Value>,
    session: &YahooSession,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let modules = serde_json::json!([
        "earningsTrend",
        "recommendationTrend",
        "upgradeDowngradeHistory",
        "earnings",
        "earningsHistory",
    ]);
    let data = yahoo_quote_summary(symbol, Some(&modules), session, transport, context)?;
    let empty = Value::Object(Map::new());
    let trend = data
        .get("earningsTrend")
        .and_then(|value| value.get("trend"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let eps_trend: Vec<Value> = trend
        .iter()
        .map(|item| {
            let estimate = item.get("earningsEstimate").unwrap_or(&empty);
            let revenue = item.get("revenueEstimate").unwrap_or(&empty);
            serde_json::json!({
                "period": item.get("period").cloned().unwrap_or(Value::Null),
                "end_date": item.get("endDate").cloned().unwrap_or(Value::Null),
                "eps_estimate": val(estimate, "avg"),
                "eps_high": val(estimate, "high"),
                "eps_low": val(estimate, "low"),
                "revenue_estimate": val(revenue, "avg"),
                "num_analysts": val(estimate, "numberOfAnalysts"),
            })
        })
        .collect();
    let rating = data
        .get("recommendationTrend")
        .and_then(|value| value.get("trend"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let rating_trend: Vec<Value> = rating
        .iter()
        .map(|item| {
            serde_json::json!({
                "period": item.get("period").cloned().unwrap_or(Value::Null),
                "strong_buy": item.get("strongBuy").cloned().unwrap_or(Value::Null),
                "buy": item.get("buy").cloned().unwrap_or(Value::Null),
                "hold": item.get("hold").cloned().unwrap_or(Value::Null),
                "sell": item.get("sell").cloned().unwrap_or(Value::Null),
                "strong_sell": item.get("strongSell").cloned().unwrap_or(Value::Null),
            })
        })
        .collect();
    let history = data
        .get("upgradeDowngradeHistory")
        .and_then(|value| value.get("history"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let upgrades: Vec<Value> = history
        .iter()
        .take(20)
        .map(|item| {
            serde_json::json!({
                "date": item.get("epochGradeDate").cloned().unwrap_or(Value::Null),
                "firm": item.get("firm").cloned().unwrap_or(Value::Null),
                "to_grade": item.get("toGrade").cloned().unwrap_or(Value::Null),
                "from_grade": item.get("fromGrade").cloned().unwrap_or(Value::Null),
                "action": item.get("action").cloned().unwrap_or(Value::Null),
            })
        })
        .collect();
    Ok(serde_json::json!({
        "eps_trend": eps_trend,
        "rating_trend": rating_trend,
        "upgrade_downgrade": upgrades,
    }))
}

/// `institutional_holders`: major holder percentages and top institutions.
pub fn institutional_holders(
    symbol: Option<&Value>,
    session: &YahooSession,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let modules = serde_json::json!(["institutionOwnership", "majorHoldersBreakdown"]);
    let data = yahoo_quote_summary(symbol, Some(&modules), session, transport, context)?;
    let empty = Value::Object(Map::new());
    let breakdown = data.get("majorHoldersBreakdown").unwrap_or(&empty);
    let overview = serde_json::json!({
        "insiders_pct": val(breakdown, "insidersPercentHeld"),
        "institutions_pct": val(breakdown, "institutionsPercentHeld"),
        "institutions_float_pct": val(breakdown, "institutionsFloatPercentHeld"),
        "institutions_count": val(breakdown, "institutionsCount"),
    });
    let ownership = data
        .get("institutionOwnership")
        .and_then(|value| value.get("ownershipList"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let top_holders: Vec<Value> = ownership
        .iter()
        .take(10)
        .map(|holder| {
            let report_date = holder
                .get("reportDate")
                .and_then(|value| {
                    if value.is_object() {
                        value.get("fmt").cloned()
                    } else {
                        None
                    }
                })
                .unwrap_or(Value::Null);
            serde_json::json!({
                "name": holder.get("organization").cloned().unwrap_or(Value::Null),
                "shares": val(holder, "position"),
                "value": val(holder, "value"),
                "pct_held": val(holder, "pctHeld"),
                "report_date": report_date,
            })
        })
        .collect();
    Ok(serde_json::json!({"overview": overview, "top_holders": top_holders}))
}

/// `financial_statements_yahoo`: three statements with raw/fmt unwrapping.
pub fn financial_statements_yahoo(
    symbol: Option<&Value>,
    quarterly: Option<&Value>,
    session: &YahooSession,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let quarterly = match quarterly {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return Err(ProviderError::value("quarterly must have type bool.")),
    };
    let suffix = if quarterly { "Quarterly" } else { "" };
    let modules = serde_json::json!([
        format!("incomeStatementHistory{suffix}"),
        format!("balanceSheetHistory{suffix}"),
        format!("cashflowStatementHistory{suffix}"),
    ]);
    let data = yahoo_quote_summary(symbol, Some(&modules), session, transport, context)?;
    let empty = Value::Object(Map::new());
    let extract = |key: &str, list_key: &str| -> Value {
        let statements = data
            .get(key)
            .unwrap_or(&empty)
            .get(list_key)
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let rows: Vec<Value> = statements
            .iter()
            .map(|statement| {
                let Some(map) = statement.as_object() else {
                    return Value::Object(Map::new());
                };
                let mut row = Map::new();
                for (name, value) in map {
                    let unwrapped = match value {
                        Value::Object(inner) if inner.contains_key("raw") => {
                            inner.get("raw").cloned().unwrap_or(Value::Null)
                        }
                        Value::Object(inner) if inner.contains_key("fmt") => {
                            inner.get("fmt").cloned().unwrap_or(Value::Null)
                        }
                        other => other.clone(),
                    };
                    row.insert(name.clone(), unwrapped);
                }
                Value::Object(row)
            })
            .collect();
        Value::Array(rows)
    };
    Ok(serde_json::json!({
        "income": extract(&format!("incomeStatementHistory{suffix}"), "incomeStatementHistory"),
        "balance": extract(&format!("balanceSheetHistory{suffix}"), "balanceSheetStatements"),
        "cashflow": extract(&format!("cashflowStatementHistory{suffix}"), "cashflowStatements"),
    }))
}

/// `options_chain`: Yahoo option chain with Unix expiries.
pub fn options_chain(
    symbol: Option<&Value>,
    expiration: Option<&Value>,
    session: &YahooSession,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let symbol = assert_us_ticker(symbol)?;
    let expiration = match expiration {
        None | Some(Value::Null) => None,
        Some(value) => Some(positive_int_max(
            Some(value),
            "expiration",
            MAX_UNIX_TIMESTAMP,
        )?),
    };
    let mut params: Vec<(&str, String)> = vec![("crumb", session.crumb.clone())];
    if let Some(expiration) = expiration {
        params.push(("date", expiration.to_string()));
    }
    let response = session.get(
        transport,
        context,
        &format!("https://query2.finance.yahoo.com/v7/finance/options/{symbol}"),
        &params,
        15.0,
    )?;
    response.raise_for_status()?;
    let document = response.json()?;
    let payload = document
        .get("optionChain")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    if payload
        .get("error")
        .map(|error| !error.is_null())
        .unwrap_or(false)
    {
        return Err(ProviderError::provider(
            "Yahoo options returned a provider error.",
        ));
    }
    let results = payload
        .get("result")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let chain = results
        .first()
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let expiration_dates = chain
        .get("expirationDates")
        .cloned()
        .unwrap_or_else(|| Value::Array(Vec::new()));
    let options = chain
        .get("options")
        .and_then(Value::as_array)
        .and_then(|items| items.first())
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let parse_side = |side: &str| -> Vec<Value> {
        let items = options
            .get(side)
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        items
            .iter()
            .map(|option| {
                let expiration_value = option
                    .get("expiration")
                    .and_then(|value| {
                        if value.is_object() {
                            value.get("fmt").cloned()
                        } else {
                            None
                        }
                    })
                    .unwrap_or(Value::Null);
                serde_json::json!({
                    "strike": val(option, "strike"),
                    "last_price": val(option, "lastPrice"),
                    "bid": val(option, "bid"),
                    "ask": val(option, "ask"),
                    "volume": val(option, "volume"),
                    "open_interest": val(option, "openInterest"),
                    "implied_volatility": val(option, "impliedVolatility"),
                    "in_the_money": option.get("inTheMoney").cloned().unwrap_or(Value::Null),
                    "expiration": expiration_value,
                    "contract_symbol": option.get("contractSymbol").cloned().unwrap_or(Value::Null),
                })
            })
            .collect()
    };
    Ok(serde_json::json!({
        "expiration_dates": expiration_dates,
        "calls": parse_side("calls"),
        "puts": parse_side("puts"),
        "underlying_price": chain
            .get("quote")
            .and_then(|quote| quote.get("regularMarketPrice"))
            .cloned()
            .unwrap_or(Value::Null),
    }))
}

fn positive_int_max(value: Option<&Value>, name: &str, maximum: i64) -> Result<i64, ProviderError> {
    match value {
        Some(Value::Number(number)) => {
            if let Some(integer) = number.as_i64() {
                if integer > 0 && integer <= maximum {
                    return Ok(integer);
                }
            }
            Err(ProviderError::value(format!(
                "{name} must be a positive integer no larger than {maximum}."
            )))
        }
        _ => Err(ProviderError::value(format!(
            "{name} must be a positive integer no larger than {maximum}."
        ))),
    }
}

/// Build the session used by the session-backed legacy functions.
pub fn session_for(
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<YahooSession, ProviderError> {
    get_yahoo_session(transport, context)
}

#[allow(dead_code)]
fn _keep_helpers(value: &Value) -> Option<i64> {
    let _ = nonempty_text(Some(value), "x");
    let _ = num_text("1");
    let _ = int_value(value);
    None
}
