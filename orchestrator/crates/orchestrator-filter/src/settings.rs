//! Filter settings — the Rust analogue of `_get_filter_settings`.
//!
//! The Python helper reads a flat key/value map (every value a
//! string) coming from either `xbmcaddon.Addon().getSetting()` or a
//! script-side `settings_getter(key, default="")` callable. This
//! crate is host-agnostic: callers can either:
//!
//!   1. Build a [`FilterSettings`] directly (orchestrator-server,
//!      which receives a JSON body — preferred).
//!   2. Call [`FilterSettings::from_kodi_map`] with a raw
//!      `HashMap<String, String>` that mirrors the legacy
//!      `getSetting` map. This is the parity path used by the
//!      ported `test_get_filter_settings_*` tests.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

/// Strongly-typed filter knobs. Lists hold the *normalised* label
/// strings PTT-equivalent normalisation produces (e.g. `"x265/HEVC"`,
/// `"DD+"`) so a direct membership check is sufficient at filter
/// time.
#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct FilterSettings {
    #[serde(default)]
    pub resolutions: Vec<String>,
    #[serde(default)]
    pub hdr: Vec<String>,
    #[serde(default)]
    pub audio: Vec<String>,
    #[serde(default)]
    pub codecs: Vec<String>,
    /// ISO 639-1 lowercase 2-letter codes (`"en"`, `"es"`, ...) to
    /// match PTT's output. See TODO.md §H.2-H11.
    #[serde(default)]
    pub languages: Vec<String>,
    /// Substrings (lowercased) — title must NOT contain any.
    #[serde(default)]
    pub exclude_keywords: Vec<String>,
    /// Substrings (lowercased) — title must contain every one.
    #[serde(default)]
    pub require_keywords: Vec<String>,
    /// Preferred release groups (lowercased). Boost in relevance
    /// sort but never reject.
    #[serde(default)]
    pub release_group: Vec<String>,
    /// Excluded release groups (lowercased). Reject hard.
    #[serde(default)]
    pub exclude_release_group: Vec<String>,
    /// MiB. `0` disables the bound.
    #[serde(default)]
    pub min_size: u64,
    /// MiB. `0` disables the bound.
    #[serde(default)]
    pub max_size: u64,
    /// 0 relevance, 1 size-desc, 2 size-asc, 3 pubdate-desc, 4
    /// pubdate-asc.
    #[serde(default)]
    pub sort_order: u8,
    /// `0` = unlimited, otherwise hard cap after sort.
    #[serde(default = "default_max_results")]
    pub max_results: u32,
}

fn default_max_results() -> u32 {
    25
}

/// Master list — the Kodi UI multiselect dialog uses this. Kept
/// purely as a reference so callers building settings JSON can
/// surface the same list to end users.
pub const ALL_RELEASE_GROUPS: &[&str] = &[
    "4KDVS",
    "Amen",
    "AOC",
    "APEX",
    "B0MBARDiERS",
    "Ben The Men",
    "BHDstudio",
    "BiTOR",
    "BYNDR",
    "c0kE",
    "CiNEPHiLES",
    "CM",
    "CMRG",
    "DDR",
    "DEFLATE",
    "DirtyHippie",
    "DiscoD",
    "DON",
    "DreamHD",
    "DVSUX",
    "EDITH",
    "ENDSTATiON",
    "ETHEL",
    "EVO",
    "FETiSH",
    "FGT",
    "FLUX",
    "FraMeSToR",
    "FrameStor",
    "FW",
    "GalaxyRG",
    "GLHF",
    "Gungnir",
    "hallowed",
    "HDS",
    "HDT",
    "HHWEB",
    "HiDt",
    "HONE",
    "HSaber",
    "IAMABLE",
    "j3rico",
    "KC",
    "Kira",
    "Kitsune",
    "KOGi",
    "KTR",
    "LEGi0N",
    "MainFrame",
    "MgB",
    "MIXED",
    "mkv",
    "mp4",
    "MZABI",
    "NAHOM",
    "Narcos",
    "NBQ",
    "NHTFS",
    "NOGRP",
    "NTb",
    "NUXWIO",
    "P2P",
    "playWEB",
    "PSA",
    "R3MiX",
    "Ralphy",
    "RARBG",
    "SDH",
    "Sensei",
    "SESKAPiLE",
    "SEV",
    "SiC",
    "SMURF",
    "SPHD",
    "SPx",
    "STRiKES",
    "SuccessfulCrab",
    "SUPPLY",
    "SURCODE",
    "SWTYBLZ",
    "TERMiNAL",
    "TEPES",
    "TheBiscuitMan",
    "ToonsHub",
    "TrollUHD",
    "TW",
    "VSEX",
    "W4NK3R",
    "WADU",
    "WiKi",
    "WRB",
    "XEBEC",
    "XXX",
    "ZAX",
];

pub const DEFAULT_PREFERRED_GROUPS: &[&str] = &[
    "CiNEPHiLES",
    "DiscoD",
    "DON",
    "FrameStor",
    "hallowed",
    "HiDt",
    "HONE",
    "j3rico",
    "Kira",
    "MainFrame",
    "SEV",
    "SPHD",
    "W4NK3R",
];

pub const DEFAULT_EXCLUDED_GROUPS: &[&str] = &[
    "4KDVS",
    "B0MBARDiERS",
    "Ben The Men",
    "BHDstudio",
    "BiTOR",
    "c0kE",
    "ENDSTATiON",
    "Gungnir",
    "HDS",
    "HSaber",
    "NUXWIO",
    "Ralphy",
    "SESKAPiLE",
    "SPx",
    "STRiKES",
    "SURCODE",
    "TW",
    "WiKi",
    "ZAX",
];

/// `(setting_id, normalised_label)` pairs for boolean filter toggles.
const RESOLUTION_PAIRS: &[(&str, &str)] = &[
    ("filter_2160p", "2160p"),
    ("filter_1080p", "1080p"),
    ("filter_720p", "720p"),
    ("filter_480p", "480p"),
];

const HDR_PAIRS: &[(&str, &str)] = &[
    ("filter_hdr10", "HDR10"),
    ("filter_hdr10plus", "HDR10+"),
    ("filter_dolby_vision", "Dolby Vision"),
    ("filter_hlg", "HLG"),
    ("filter_sdr", "SDR"),
];

const AUDIO_PAIRS: &[(&str, &str)] = &[
    ("filter_atmos", "Atmos"),
    ("filter_truehd", "TrueHD"),
    ("filter_dtshd_ma", "DTS-HD MA"),
    ("filter_dtsx", "DTS:X"),
    ("filter_ddplus", "DD+"),
    ("filter_dd", "DD"),
    ("filter_aac", "AAC"),
];

const CODEC_PAIRS: &[(&str, &str)] = &[
    ("filter_hevc", "x265/HEVC"),
    ("filter_avc", "x264/AVC"),
    ("filter_av1", "AV1"),
    ("filter_vp9", "VP9"),
    ("filter_mpeg2", "MPEG-2"),
];

const LANGUAGE_PAIRS: &[(&str, &str)] = &[
    ("filter_english", "en"),
    ("filter_spanish", "es"),
    ("filter_french", "fr"),
    ("filter_german", "de"),
    ("filter_italian", "it"),
    ("filter_portuguese", "pt"),
    ("filter_dutch", "nl"),
    ("filter_russian", "ru"),
    ("filter_japanese", "ja"),
    ("filter_korean", "ko"),
    ("filter_chinese", "zh"),
    ("filter_arabic", "ar"),
    ("filter_hindi", "hi"),
];

fn collect_enabled(map: &HashMap<String, String>, pairs: &[(&str, &str)]) -> Vec<String> {
    pairs
        .iter()
        .filter_map(|(setting_id, label)| {
            let raw = map.get(*setting_id).map(String::as_str).unwrap_or("");
            if raw.eq_ignore_ascii_case("true") {
                Some((*label).to_string())
            } else {
                None
            }
        })
        .collect()
}

fn csv_setting(map: &HashMap<String, String>, key: &str) -> Vec<String> {
    let raw = map.get(key).map(String::as_str).unwrap_or("").trim();
    if raw.is_empty() {
        return Vec::new();
    }
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect()
}

/// Read an integer setting with the same fallback semantics as
/// Python's `_int_setting`: try `int(raw)`, then `int(float(raw))`,
/// then `default`. Non-finite floats (`inf`/`nan`) → default.
fn int_setting(map: &HashMap<String, String>, key: &str, default: i64) -> i64 {
    let raw = match map.get(key) {
        Some(s) if !s.is_empty() => s,
        _ => return default,
    };
    if let Ok(n) = raw.parse::<i64>() {
        return n;
    }
    if let Ok(f) = raw.parse::<f64>() {
        if f.is_finite() {
            // Truncate toward zero like Python's `int(float(...))`.
            return f.trunc() as i64;
        }
    }
    default
}

impl FilterSettings {
    /// Build settings from a raw flat key/value map — the parity
    /// path that mirrors `_get_filter_settings`.
    ///
    /// `log_warning` is invoked with a human-readable message when
    /// the inverted-range clamp fires (`min_size > max_size`). It
    /// substitutes for `xbmc.log(...)` in the Python source. Pass
    /// `|_| ()` if you don't care.
    pub fn from_kodi_map<F: FnMut(&str)>(
        map: &HashMap<String, String>,
        mut log_warning: F,
    ) -> Self {
        let resolutions = collect_enabled(map, RESOLUTION_PAIRS);
        let hdr = collect_enabled(map, HDR_PAIRS);
        let audio = collect_enabled(map, AUDIO_PAIRS);
        let codecs = collect_enabled(map, CODEC_PAIRS);
        let languages = collect_enabled(map, LANGUAGE_PAIRS);

        let exclude_keywords = csv_setting(map, "filter_exclude_keywords")
            .into_iter()
            .map(|s| s.to_lowercase())
            .collect();
        let require_keywords = csv_setting(map, "filter_require_keywords")
            .into_iter()
            .map(|s| s.to_lowercase())
            .collect();
        let release_group = csv_setting(map, "filter_release_group")
            .into_iter()
            .map(|s| s.to_lowercase())
            .collect();
        let exclude_release_group = csv_setting(map, "filter_exclude_release_group")
            .into_iter()
            .map(|s| s.to_lowercase())
            .collect();

        let mut min_size = int_setting(map, "filter_min_size", 0).max(0) as u64;
        let mut max_size = int_setting(map, "filter_max_size", 0).max(0) as u64;
        if max_size > 0 && max_size < min_size {
            log_warning(&format!(
                "NZB-DAV: filter_min_size={} exceeds filter_max_size={}; disabling size filter",
                min_size, max_size
            ));
            min_size = 0;
            max_size = 0;
        }

        let sort_order_raw = int_setting(map, "sort_order", 0);
        let sort_order = if (0..=255).contains(&sort_order_raw) {
            sort_order_raw as u8
        } else {
            0
        };
        let max_results_raw = int_setting(map, "max_results", 25);
        let max_results = if max_results_raw < 0 {
            0
        } else if max_results_raw > u32::MAX as i64 {
            u32::MAX
        } else {
            max_results_raw as u32
        };

        FilterSettings {
            resolutions,
            hdr,
            audio,
            codecs,
            languages,
            exclude_keywords,
            require_keywords,
            release_group,
            exclude_release_group,
            min_size,
            max_size,
            sort_order,
            max_results,
        }
    }
}
