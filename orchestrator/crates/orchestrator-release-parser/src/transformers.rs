//! 1:1 port of `plugin.video.nzbdav/resources/lib/ptt/transformers.py`.
//!
//! Each transformer takes the matched substring and (for two-arg
//! transformers) the existing result value for the field, and
//! returns the typed value that goes into the parsed dict.

use once_cell::sync::Lazy;
use regex::Regex;

use crate::parser::Value;

/// Enum-shaped transformer dispatch.
///
/// Python PTT lets you pass `value(X)` or `uniq_concat(int)` as the
/// transformer arg of `add_handler`; the Rust port keeps the same
/// expressive set without forcing every handler registration to
/// reach for `Box<dyn Fn>` (which kills the static-init story).
#[derive(Clone)]
pub enum Transformer {
    /// `none` — identity (return the matched string unchanged).
    None_,
    /// `integer` — strip non-digit chars, parse as int. None on
    /// failure.
    Integer,
    /// `first_integer` — first run of digits in the input, parsed.
    FirstInteger,
    /// `boolean` — always true (used for flag handlers).
    Boolean,
    /// `lowercase`.
    Lowercase,
    /// `uppercase`.
    Uppercase,
    /// `value(literal)` — replace with a fixed string. `$1` in the
    /// literal is substituted with the matched substring.
    Value(String),
    /// `array(none)` — wrap the matched string in a single-element
    /// list.
    Array,
    /// `array(transform_resolution)` — list of one resolution.
    ArrayTransformResolution,
    /// `array(lowercase)`.
    ArrayLowercase,
    /// `uniq_concat(none)` — append to list, dedup.
    UniqConcat,
    /// `uniq_concat(lowercase)`.
    UniqConcatLowercase,
    /// `uniq_concat(uppercase)`.
    UniqConcatUppercase,
    /// `uniq_concat(value("x"))` — append a fixed value.
    UniqConcatValue(String),
    /// `transform_resolution` — normalise to 2160p/1440p/1080p/...
    TransformResolution,
    /// `year_range`.
    YearRange,
    /// `range_func`.
    RangeFunc,
    /// `range_x_of_y_func`.
    RangeXOfYFunc,
}

impl Transformer {
    /// Apply the transformer. `matched` is the substring (raw or the
    /// first capture group). `existing` is the current value of the
    /// result key, for the two-arg uniq_concat variants.
    pub fn apply(&self, matched: &str, existing: Option<&Value>) -> Option<Value> {
        match self {
            Transformer::None_ => Some(Value::Str(matched.to_string())),
            Transformer::Integer => integer(matched).map(Value::Int),
            Transformer::FirstInteger => first_integer(matched).map(Value::Int),
            Transformer::Boolean => Some(Value::Bool(true)),
            Transformer::Lowercase => Some(Value::Str(matched.to_lowercase())),
            Transformer::Uppercase => Some(Value::Str(matched.to_uppercase())),
            Transformer::Value(v) => Some(Value::Str(v.replace("$1", matched))),
            Transformer::Array => Some(Value::List(vec![Value::Str(matched.to_string())])),
            Transformer::ArrayTransformResolution => {
                Some(Value::List(vec![Value::Str(transform_resolution(matched))]))
            }
            Transformer::ArrayLowercase => {
                Some(Value::List(vec![Value::Str(matched.to_lowercase())]))
            }
            Transformer::UniqConcat => uniq_append(existing, Value::Str(matched.to_string())),
            Transformer::UniqConcatLowercase => {
                uniq_append(existing, Value::Str(matched.to_lowercase()))
            }
            Transformer::UniqConcatUppercase => {
                uniq_append(existing, Value::Str(matched.to_uppercase()))
            }
            Transformer::UniqConcatValue(v) => {
                uniq_append(existing, Value::Str(v.replace("$1", matched)))
            }
            Transformer::TransformResolution => Some(Value::Str(transform_resolution(matched))),
            Transformer::YearRange => year_range(matched).map(Value::Str),
            Transformer::RangeFunc => {
                range_func(matched).map(|xs| Value::List(xs.into_iter().map(Value::Int).collect()))
            }
            Transformer::RangeXOfYFunc => range_x_of_y_func(matched)
                .map(|xs| Value::List(xs.into_iter().map(Value::Int).collect())),
        }
    }
}

fn uniq_append(existing: Option<&Value>, candidate: Value) -> Option<Value> {
    let mut items: Vec<Value> = match existing {
        Some(Value::List(xs)) => xs.clone(),
        _ => Vec::new(),
    };
    if !items.iter().any(|v| v == &candidate) {
        items.push(candidate);
    }
    Some(Value::List(items))
}

static INTEGER_NON_DIGIT: Lazy<Regex> = Lazy::new(|| Regex::new(r"\D").unwrap());
static DIGITS_RUN: Lazy<Regex> = Lazy::new(|| Regex::new(r"\d+").unwrap());

fn integer(input: &str) -> Option<i64> {
    let cleaned = INTEGER_NON_DIGIT.replace_all(input, "");
    if cleaned.is_empty() {
        return None;
    }
    cleaned.parse::<i64>().ok()
}

fn first_integer(input: &str) -> Option<i64> {
    DIGITS_RUN
        .find(input)
        .and_then(|m| m.as_str().parse::<i64>().ok())
}

/// 1:1 port of `transform_resolution(input_value)`.
pub fn transform_resolution(input: &str) -> String {
    let lower = input.to_lowercase();
    if lower.contains("2160") || lower.contains("4k") {
        return "2160p".to_string();
    }
    if lower.contains("1440") || lower.contains("2k") {
        return "1440p".to_string();
    }
    if lower.contains("1080") {
        return "1080p".to_string();
    }
    if lower.contains("720") {
        return "720p".to_string();
    }
    if lower.contains("480") {
        return "480p".to_string();
    }
    if lower.contains("360") {
        return "360p".to_string();
    }
    if lower.contains("240") {
        return "240p".to_string();
    }
    lower
}

fn year_range(input: &str) -> Option<String> {
    let parts: Vec<&str> = DIGITS_RUN.find_iter(input).map(|m| m.as_str()).collect();
    if parts.is_empty() {
        return None;
    }
    let start: i64 = parts[0].parse().ok()?;
    let mut end: Option<i64> = if parts.len() > 1 {
        parts[1].parse().ok()
    } else {
        None
    };
    if let Some(e) = end {
        if e < 100 {
            end = Some(e + (start - start % 100));
        }
    }
    match end {
        None => Some(start.to_string()),
        Some(e) if e <= start => None,
        Some(e) => Some(format!("{start}-{e}")),
    }
}

fn range_func(input: &str) -> Option<Vec<i64>> {
    let numbers: Vec<i64> = DIGITS_RUN
        .find_iter(input)
        .filter_map(|m| m.as_str().parse::<i64>().ok())
        .collect();
    if numbers.len() == 2 && numbers[0] < numbers[1] {
        return Some((numbers[0]..=numbers[1]).collect());
    }
    if numbers.len() > 2 && numbers.windows(2).all(|w| w[0] + 1 == w[1]) {
        return Some(numbers);
    }
    if numbers.len() == 1 {
        return Some(numbers);
    }
    None
}

fn range_x_of_y_func(input: &str) -> Option<Vec<i64>> {
    let numbers: Vec<i64> = DIGITS_RUN
        .find_iter(input)
        .filter_map(|m| m.as_str().parse::<i64>().ok())
        .collect();
    if numbers.len() != 1 {
        return None;
    }
    Some((1..=numbers[0]).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn integer_strips_non_digits() {
        assert_eq!(integer("S05"), Some(5));
        assert_eq!(integer("foo"), None);
        assert_eq!(integer("1080p"), Some(1080));
    }

    #[test]
    fn first_integer_returns_only_first_run() {
        assert_eq!(first_integer("1999-2003"), Some(1999));
        assert_eq!(first_integer("noop"), None);
    }

    #[test]
    fn transform_resolution_normalises_known_buckets() {
        assert_eq!(transform_resolution("2160p"), "2160p");
        assert_eq!(transform_resolution("4K"), "2160p");
        assert_eq!(transform_resolution("1080P"), "1080p");
        assert_eq!(transform_resolution("720"), "720p");
    }

    #[test]
    fn year_range_handles_pair_singleton_and_invalid() {
        // Python year_range: two-digit end gets adjusted by start
        // century — "1999-03" computes 3 + (1999 - 99) = 1903 which
        // is <= start, so the function returns None. Closed pairs
        // ending later than start (e.g. "1999-2003") format as-is.
        assert_eq!(year_range("1999-2003"), Some("1999-2003".into()));
        assert_eq!(year_range("1999"), Some("1999".into()));
        assert_eq!(year_range("2010-2010"), None);
        assert_eq!(year_range("1999-03"), None);
    }

    #[test]
    fn range_func_handles_pair_and_singleton() {
        assert_eq!(range_func("16-18"), Some(vec![16, 17, 18]));
        assert_eq!(range_func("16"), Some(vec![16]));
        assert_eq!(range_func("16 17 19"), None);
    }
}
