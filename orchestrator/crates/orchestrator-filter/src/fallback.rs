//! Regex-only fallback parser — 1:1 port of `_fallback_parse`.
//!
//! Activated when PTT-equivalent parsing returns nothing useful for
//! a title. Deliberately conservative; just enough to populate
//! `resolution` / `codec` / `audio` / `hdr` / `quality` / `edition`
//! / `channels` / `year` / `upscaled` / `group` so the filter
//! pipeline can still make a defensible decision.

use once_cell::sync::Lazy;
use regex::Regex;

use crate::metadata::ParsedMeta;

static RE_RESOLUTION: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)\b(2160p|1080p|1080i|720p|480p|4K)\b").unwrap());

static RE_CODEC: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)\b(x265|h\.?265|hevc|x264|h\.?264|avc|av1|vp9)\b").unwrap());

static RE_AUDIO_ATMOS: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)\batmos\b").unwrap());
static RE_AUDIO_TRUEHD: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)\btruehd\b").unwrap());
static RE_AUDIO_DTSHDMA: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)\bdts[-. ]?hd[-. ]?ma\b").unwrap());
static RE_AUDIO_DDPLUS: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)\bddp?5[. ]1|eac3|dd\+|dolby.digital.plus\b").unwrap());
static RE_AUDIO_DD: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)\bac3|dd[. ]?5[. ]1|dolby.digital\b").unwrap());
static RE_AUDIO_AAC: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)\baac\b").unwrap());
static RE_AUDIO_DTS: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)\bdts\b").unwrap());

static RE_HDR_DV: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)\b(dv|dovi|dolby[. ]?vision)\b").unwrap());
static RE_HDR_PLUS: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)\b(hdr10\+|hdr10plus)\b").unwrap());
static RE_HDR_10: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)\bhdr10\b").unwrap());
static RE_HDR_HLG: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)\bhlg\b").unwrap());

static RE_QUALITY: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(remux|blu[-. ]?ray|bdrip|web[-. ]?dl|webrip|hdtv|dvdrip|hdrip)\b").unwrap()
});

static RE_EDITION: Lazy<Regex> = Lazy::new(|| {
    Regex::new(
        r"(?i)\b(uncut|unrated|director'?s[. ]?cut|extended[. ]?cut|recut|theatrical|imax|special[. ]?edition)\b",
    )
    .unwrap()
});

static RE_CHANNELS: Lazy<Regex> = Lazy::new(|| Regex::new(r"\b(7\.1|5\.1|2\.0)\b").unwrap());
static RE_YEAR: Lazy<Regex> = Lazy::new(|| Regex::new(r"[. (](\d{4})[. )]").unwrap());
static RE_UPSCALED: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)\bupscale[d]?\b").unwrap());
static RE_GROUP: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"-([A-Za-z0-9][A-Za-z0-9_-]*)(?:\.[a-z]{2,4})?$").unwrap());

/// Produce a parsed-meta dict from a raw release title using only
/// regexes. Mirrors `_fallback_parse(title)` exactly.
pub fn fallback_parse(title: &str) -> ParsedMeta {
    let mut meta = ParsedMeta::default();

    // Bracket → dot: aligns single-line patterns that expect `.`
    // separators with bracketed scene titles.
    let t = title.replace(['[', ']', '(', ')'], ".");

    if let Some(c) = RE_RESOLUTION.captures(&t) {
        meta.resolution = c.get(1).unwrap().as_str().to_string();
    }
    if let Some(c) = RE_CODEC.captures(&t) {
        meta.codec = c.get(1).unwrap().as_str().to_lowercase();
    }

    let mut audio: Vec<String> = Vec::new();
    if RE_AUDIO_ATMOS.is_match(&t) {
        audio.push("Atmos".to_string());
    }
    if RE_AUDIO_TRUEHD.is_match(&t) {
        audio.push("TrueHD".to_string());
    }
    if RE_AUDIO_DTSHDMA.is_match(&t) {
        audio.push("DTS-HD MA".to_string());
    }
    if RE_AUDIO_DDPLUS.is_match(&t) {
        audio.push("DD+".to_string());
    }
    if RE_AUDIO_DD.is_match(&t) {
        audio.push("DD".to_string());
    }
    if RE_AUDIO_AAC.is_match(&t) {
        audio.push("AAC".to_string());
    }
    if audio.is_empty() && RE_AUDIO_DTS.is_match(&t) {
        audio.push("DTS".to_string());
    }
    meta.audio = audio;

    let mut hdr: Vec<String> = Vec::new();
    if RE_HDR_DV.is_match(&t) {
        hdr.push("DV".to_string());
    }
    if RE_HDR_PLUS.is_match(&t) {
        hdr.push("HDR10+".to_string());
    } else if RE_HDR_10.is_match(&t) {
        hdr.push("HDR10".to_string());
    }
    if RE_HDR_HLG.is_match(&t) {
        hdr.push("HLG".to_string());
    }
    meta.hdr = hdr;

    if let Some(c) = RE_QUALITY.captures(&t) {
        let raw = c.get(1).unwrap().as_str().to_uppercase();
        let raw: String = raw
            .chars()
            .filter(|ch| !matches!(ch, ' ' | '.' | '-'))
            .collect();
        meta.quality = if raw.contains("REMUX") {
            "BluRay REMUX".to_string()
        } else if raw.contains("BLURAY") || raw.contains("BDRIP") {
            "BluRay".to_string()
        } else if raw.contains("WEBDL") {
            "WEB-DL".to_string()
        } else if raw.contains("WEBRIP") {
            "WEBRip".to_string()
        } else if raw.contains("HDTV") {
            "HDTV".to_string()
        } else {
            raw
        };
    }

    if let Some(c) = RE_EDITION.captures(&t) {
        meta.edition = c.get(1).unwrap().as_str().replace('.', " ");
    }

    if let Some(c) = RE_CHANNELS.captures(&t) {
        meta.channels = c.get(1).unwrap().as_str().to_string();
    }

    if let Some(c) = RE_YEAR.captures(&t) {
        if let Ok(yr) = c.get(1).unwrap().as_str().parse::<i32>() {
            if (1920..=2100).contains(&yr) {
                meta.year = yr;
            }
        }
    }

    if RE_UPSCALED.is_match(&t) {
        meta.upscaled = true;
    }

    // Note: group regex runs against the *original* title (with
    // hyphens preserved), not the bracket-stripped variant — matches
    // `_fallback_parse` literally.
    if let Some(c) = RE_GROUP.captures(title) {
        meta.group = c.get(1).unwrap().as_str().to_string();
    }

    meta
}
