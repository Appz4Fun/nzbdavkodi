//! Normalised parsed metadata + the `parse_metadata` entry point.
//!
//! Mirrors `parse_title_metadata` in `filter.py`. Strategy:
//!
//!   1. Run the PTT-equivalent parser. If it produced *neither*
//!      `resolution` nor `codec`, try the regex fallback and prefer
//!      whichever yielded fields.
//!   2. Normalise PTT's raw labels through the resolution / hdr /
//!      audio / codec maps to canonical strings.
//!
//! If anything blows up during normalisation we drop down to a pure
//! regex fallback so a single bad release doesn't kill the entire
//! search.

use std::collections::HashMap;

use once_cell::sync::Lazy;
use orchestrator_release_parser::{handlers::parse_title, Parsed, Value};
use serde::{Deserialize, Serialize};

use crate::fallback::fallback_parse;

/// Canonical, deduplicated view of release-title metadata.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ParsedMeta {
    pub resolution: String,
    pub hdr: Vec<String>,
    pub audio: Vec<String>,
    pub codec: String,
    pub languages: Vec<String>,
    pub group: String,
    pub quality: String,
    pub edition: String,
    pub proper: bool,
    pub repack: bool,
    pub channels: String,
    pub year: i32,
    pub upscaled: bool,
    pub container: String,
}

static RESOLUTION_MAP: Lazy<HashMap<&'static str, &'static str>> = Lazy::new(|| {
    HashMap::from([
        ("2160p", "2160p"),
        ("4K", "2160p"),
        ("1080p", "1080p"),
        ("1080i", "1080p"),
        ("720p", "720p"),
        ("480p", "480p"),
        ("480i", "480p"),
        ("SD", "480p"),
    ])
});

static HDR_MAP: Lazy<HashMap<&'static str, &'static str>> = Lazy::new(|| {
    HashMap::from([
        ("HDR", "HDR10"),
        ("HDR10", "HDR10"),
        ("HDR10+", "HDR10+"),
        ("HDR10Plus", "HDR10+"),
        ("DV", "Dolby Vision"),
        ("Dolby Vision", "Dolby Vision"),
        ("DoVi", "Dolby Vision"),
        ("HLG", "HLG"),
    ])
});

static AUDIO_MAP: Lazy<HashMap<&'static str, &'static str>> = Lazy::new(|| {
    HashMap::from([
        ("Atmos", "Atmos"),
        ("TrueHD", "TrueHD"),
        ("DTS-HD MA", "DTS-HD MA"),
        ("DTS-HD", "DTS-HD MA"),
        ("DTS Lossless", "DTS-HD MA"),
        ("DTS:X", "DTS:X"),
        ("DTS-X", "DTS:X"),
        ("DD+", "DD+"),
        ("EAC3", "DD+"),
        ("E-AC-3", "DD+"),
        ("Dolby Digital Plus", "DD+"),
        ("DD", "DD"),
        ("AC3", "DD"),
        ("AC-3", "DD"),
        ("Dolby Digital", "DD"),
        ("DTS Lossy", "DD"),
        // The Rust release-parser emits a bare `"DTS"` token where
        // Python PTT emits `"DTS Lossy"` (which then maps to `DD`).
        // Treating bare `DTS` as `DD` collapses both paths to the
        // same canonical output. See parity test
        // `filter_pipeline_realistic_titles`.
        ("DTS", "DD"),
        ("AAC", "AAC"),
    ])
});

static CODEC_MAP: Lazy<HashMap<&'static str, &'static str>> = Lazy::new(|| {
    HashMap::from([
        ("x265", "x265/HEVC"),
        ("HEVC", "x265/HEVC"),
        ("H.265", "x265/HEVC"),
        ("h265", "x265/HEVC"),
        ("hevc", "x265/HEVC"),
        ("x264", "x264/AVC"),
        ("AVC", "x264/AVC"),
        ("H.264", "x264/AVC"),
        ("h264", "x264/AVC"),
        ("avc", "x264/AVC"),
        ("AV1", "AV1"),
        ("av1", "AV1"),
        ("VP9", "VP9"),
        ("vp9", "VP9"),
        ("MPEG2", "MPEG-2"),
        ("MPEG-2", "MPEG-2"),
        ("mpeg2", "MPEG-2"),
    ])
});

fn map_value<'a>(map: &'a HashMap<&'static str, &'static str>, raw: &'a str) -> String {
    map.get(raw)
        .map(|s| (*s).to_string())
        .unwrap_or_else(|| raw.to_string())
}

/// Pull a string out of a [`Parsed`] dict at `key`. `""` if missing
/// or non-string.
fn get_str(parsed: &Parsed, key: &str) -> String {
    match parsed.get(key) {
        Some(Value::Str(s)) => s.clone(),
        _ => String::new(),
    }
}

fn get_bool(parsed: &Parsed, key: &str) -> bool {
    matches!(parsed.get(key), Some(Value::Bool(true)))
}

fn get_int(parsed: &Parsed, key: &str) -> i64 {
    match parsed.get(key) {
        Some(Value::Int(n)) => *n,
        _ => 0,
    }
}

/// Coerce `parsed[key]` into `Vec<String>`. PTT sometimes returns a
/// bare string instead of a one-element list — both flow through.
fn get_str_list(parsed: &Parsed, key: &str) -> Vec<String> {
    match parsed.get(key) {
        Some(Value::List(items)) => items
            .iter()
            .filter_map(|v| match v {
                Value::Str(s) if !s.is_empty() => Some(s.clone()),
                _ => None,
            })
            .collect(),
        Some(Value::Str(s)) if !s.is_empty() => vec![s.clone()],
        _ => Vec::new(),
    }
}

/// Dedup while preserving first-occurrence order. Matches Python's
/// `list(dict.fromkeys(...))` idiom.
fn dedup_preserve_order(items: Vec<String>) -> Vec<String> {
    let mut seen: Vec<String> = Vec::with_capacity(items.len());
    for item in items {
        if !seen.contains(&item) {
            seen.push(item);
        }
    }
    seen
}

/// Did PTT return anything we'd consider useful?
fn ptt_was_empty(parsed: &Parsed) -> bool {
    get_str(parsed, "resolution").is_empty() && get_str(parsed, "codec").is_empty()
}

/// Build a [`ParsedMeta`] from a [`Parsed`] dict produced by either
/// PTT or the fallback. Encapsulates the normalisation block in
/// `parse_title_metadata`.
fn normalise(parsed: &Parsed) -> ParsedMeta {
    let raw_res = get_str(parsed, "resolution");
    let resolution = map_value(&RESOLUTION_MAP, &raw_res);

    let raw_hdr = get_str_list(parsed, "hdr");
    let hdr = dedup_preserve_order(raw_hdr.iter().map(|h| map_value(&HDR_MAP, h)).collect());

    let raw_audio = get_str_list(parsed, "audio");
    let audio = dedup_preserve_order(raw_audio.iter().map(|a| map_value(&AUDIO_MAP, a)).collect());

    let raw_codec = get_str(parsed, "codec");
    let codec = map_value(&CODEC_MAP, &raw_codec);

    let languages = dedup_preserve_order(get_str_list(parsed, "languages"));

    let group = get_str(parsed, "group");
    let quality = get_str(parsed, "quality");
    let edition = get_str(parsed, "edition");
    let proper = get_bool(parsed, "proper");
    let repack = get_bool(parsed, "repack");
    let year_i64 = get_int(parsed, "year");
    let year = year_i64.clamp(i32::MIN as i64, i32::MAX as i64) as i32;
    let upscaled = get_bool(parsed, "upscaled");
    let container = get_str(parsed, "container");

    let raw_channels = get_str_list(parsed, "channels");
    let channels = raw_channels.into_iter().next().unwrap_or_default();

    ParsedMeta {
        resolution,
        hdr,
        audio,
        codec,
        languages,
        group,
        quality,
        edition,
        proper,
        repack,
        channels,
        year,
        upscaled,
        container,
    }
}

/// Parse a release title into normalised metadata. Always succeeds —
/// worst case the returned struct is mostly default.
pub fn parse_metadata(title: &str) -> ParsedMeta {
    // 1. Try the PTT-equivalent parser. Wrap in `catch_unwind` so a
    //    bug in the parser can't kill the whole search — same intent
    //    as the outer `except Exception` in `parse_title_metadata`.
    let parsed_initial = std::panic::catch_unwind(|| parse_title(title)).unwrap_or_else(|_| {
        tracing::warn!(
            event = "filter.candidate_rejected",
            reason = "ptt_parse_failed",
            title = %title,
            "PTT parse panicked — falling back to regex"
        );
        parsed_from_fallback(title)
    });

    // 2. If PTT got nothing, *prefer* fallback (only if fallback got
    //    something).
    let parsed = if ptt_was_empty(&parsed_initial) {
        let fb = parsed_from_fallback(title);
        if !ptt_was_empty(&fb) {
            fb
        } else {
            parsed_initial
        }
    } else {
        parsed_initial
    };

    // 3. Normalise. Hand-port doesn't expose the inner-typed crash
    //    mode that Python's `try/except` block guarded against
    //    (Rust's static typing would have already failed at compile
    //    time). Keep the structure clean.
    normalise(&parsed)
}

/// Build a [`Parsed`] dict from a fallback [`ParsedMeta`] so the
/// downstream pipeline only deals with one shape.
fn parsed_from_fallback(title: &str) -> Parsed {
    let fb = fallback_parse(title);
    let mut p = Parsed::new();
    p.insert("resolution".into(), Value::Str(fb.resolution));
    p.insert("codec".into(), Value::Str(fb.codec));
    p.insert(
        "audio".into(),
        Value::List(fb.audio.into_iter().map(Value::Str).collect()),
    );
    p.insert(
        "hdr".into(),
        Value::List(fb.hdr.into_iter().map(Value::Str).collect()),
    );
    p.insert("languages".into(), Value::List(vec![]));
    p.insert("group".into(), Value::Str(fb.group));
    p.insert("quality".into(), Value::Str(fb.quality));
    p.insert("edition".into(), Value::Str(fb.edition));
    p.insert("year".into(), Value::Int(fb.year as i64));
    p.insert("upscaled".into(), Value::Bool(fb.upscaled));
    if !fb.channels.is_empty() {
        p.insert(
            "channels".into(),
            Value::List(vec![Value::Str(fb.channels)]),
        );
    }
    p
}
