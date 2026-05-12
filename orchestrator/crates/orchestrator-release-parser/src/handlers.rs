//! 1:1 port of `add_defaults(parser)` from `handlers.py`.
//!
//! Each call to [`Parser::add_regex`] / [`Parser::add_regex_with_value`]
//! / [`Parser::add_custom`] in [`add_defaults`] mirrors one
//! `parser.add_handler(...)` line in the Python source. Order MUST
//! match — PTT's behaviour depends on handler ordering because each
//! handler can shrink the running title and the `end_of_title`
//! watermark.
//!
//! Phase 1 lands the handler subset that filter.py consumes:
//! resolution, codec, audio, channels, hdr, languages, quality,
//! edition, proper, repack, year, upscaled, container, group, plus
//! season/episode for the search planner. Anime/adult/cleanup
//! handlers from the Python source that don't feed filter decisions
//! are left for a follow-up commit. Every release name in
//! `orchestrator/tests/harness/fixtures/ptt_parity_corpus.json` is a
//! parity oracle for the subset shipped here.

use once_cell::sync::Lazy;
use regex::Regex;

use crate::parser::{HandlerOptions, Parser, Value};
use crate::transformers::Transformer;

/// Construct a [`Parser`] preloaded with the filter-critical handler
/// set.
pub fn default_parser() -> Parser {
    let mut p = Parser::new();
    add_defaults(&mut p);
    p
}

/// Convenience: build, then parse.
pub fn parse_title(raw: &str) -> crate::Parsed {
    DEFAULT_PARSER.parse(raw)
}

static DEFAULT_PARSER: Lazy<Parser> = Lazy::new(default_parser);

// Convenience option presets for readability.
fn defaults() -> HandlerOptions {
    HandlerOptions::defaults()
}
fn keep() -> HandlerOptions {
    HandlerOptions {
        remove: false,
        ..defaults()
    }
}

#[allow(clippy::too_many_lines)]
fn add_defaults(p: &mut Parser) {
    // The handler order must mirror handlers.py. The subset below
    // covers every field filter.py reads. Each block corresponds to
    // the same-named block in the Python source so a future reader
    // can grep one in the other.

    add_year(p);
    add_resolution(p);
    add_quality(p);
    add_container(p);
    add_codec(p);
    add_audio(p);
    add_channels(p);
    add_hdr(p);
    add_languages(p);
    add_edition(p);
    add_flags(p);
    add_season_episode(p);
    add_group(p);
    add_upscaled(p);
}

fn add_year(p: &mut Parser) {
    // Year range first (e.g. 1999-2003) so the pair-year handler
    // doesn't pick the second year as the canonical one. Mirrors
    // handlers.py "Year Pre-check".
    p.add_regex(
        "year",
        Regex::new(r"\b19\d{2}\s?-\s?20\d{2}\b").unwrap(),
        Transformer::FirstInteger,
        keep(),
    );
    p.add_regex(
        "year",
        Regex::new(r"\b(19[0-9]{2}|20[0-9]{2})\b").unwrap(),
        Transformer::Integer,
        keep(),
    );
}

fn add_resolution(p: &mut Parser) {
    // Mirrors handlers.py "Resolution" block.
    p.add_regex(
        "resolution",
        Regex::new(r"(?i)\b(?:4k|2160p|UHD)\b").unwrap(),
        Transformer::TransformResolution,
        keep(),
    );
    p.add_regex(
        "resolution",
        Regex::new(r"(?i)\b(?:1440p|2k)\b").unwrap(),
        Transformer::TransformResolution,
        keep(),
    );
    p.add_regex(
        "resolution",
        Regex::new(r"(?i)\b1080p\b").unwrap(),
        Transformer::TransformResolution,
        keep(),
    );
    p.add_regex(
        "resolution",
        Regex::new(r"(?i)\b720p\b").unwrap(),
        Transformer::TransformResolution,
        keep(),
    );
    p.add_regex(
        "resolution",
        Regex::new(r"(?i)\b480p\b").unwrap(),
        Transformer::TransformResolution,
        keep(),
    );
    p.add_regex(
        "resolution",
        Regex::new(r"(?i)\b360p\b").unwrap(),
        Transformer::TransformResolution,
        keep(),
    );
    p.add_regex(
        "resolution",
        Regex::new(r"(?i)\b240p\b").unwrap(),
        Transformer::TransformResolution,
        keep(),
    );
}

fn add_quality(p: &mut Parser) {
    // Order matters — BluRay REMUX is checked before plain BluRay so
    // the REMUX qualifier is captured.
    p.add_regex_with_value(
        "quality",
        Regex::new(r"(?i)\bBluRay\b.*\bREMUX\b|\bREMUX\b.*\bBluRay\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("BluRay REMUX".into()),
    );
    p.add_regex_with_value(
        "quality",
        Regex::new(r"(?i)\b(?:REMUX|REMASTERED?)\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("BluRay REMUX".into()),
    );
    p.add_regex_with_value(
        "quality",
        Regex::new(r"(?i)\bBluRay\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("BluRay".into()),
    );
    p.add_regex_with_value(
        "quality",
        Regex::new(r"(?i)\bWEB[ ._-]?DL\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("WEB-DL".into()),
    );
    p.add_regex_with_value(
        "quality",
        Regex::new(r"(?i)\bWEBRip\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("WEBRip".into()),
    );
    p.add_regex_with_value(
        "quality",
        Regex::new(r"(?i)\bHDTV\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("HDTV".into()),
    );
    p.add_regex_with_value(
        "quality",
        Regex::new(r"(?i)\bDVDRip\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("DVDRip".into()),
    );
    p.add_regex_with_value(
        "quality",
        Regex::new(r"(?i)\bHDTC\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("HDTC".into()),
    );
    p.add_regex_with_value(
        "quality",
        Regex::new(r"(?i)\bCAM(?:Rip)?\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("CAM".into()),
    );
    p.add_regex_with_value(
        "quality",
        Regex::new(r"(?i)\b(?:TS|TELESYNC)\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("TELESYNC".into()),
    );
}

fn add_container(p: &mut Parser) {
    p.add_regex(
        "container",
        Regex::new(r"(?i)\b(MKV|AVI|MP4|WMV|MPG|MPEG)\b").unwrap(),
        Transformer::Lowercase,
        keep(),
    );
}

fn add_codec(p: &mut Parser) {
    // Order: HEVC variants first so x265 → hevc precedes x264 → avc.
    p.add_regex_with_value(
        "codec",
        Regex::new(r"(?i)\b(?:HEVC|x[._ -]?265|h[._ -]?265)\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("hevc".into()),
    );
    p.add_regex_with_value(
        "codec",
        Regex::new(r"(?i)\b(?:x[._ -]?264|h[._ -]?264|AVC)\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("avc".into()),
    );
    p.add_regex_with_value(
        "codec",
        Regex::new(r"(?i)\bAV1\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("av1".into()),
    );
    p.add_regex_with_value(
        "codec",
        Regex::new(r"(?i)\bXviD\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("xvid".into()),
    );
}

fn add_audio(p: &mut Parser) {
    // PTT returns the audio field as a list (uniq_concat). Order:
    // Atmos + DTS-X first (specific), then lossless families, then
    // generic.
    p.add_regex(
        "audio",
        Regex::new(r"(?i)\bAtmos\b").unwrap(),
        Transformer::UniqConcatValue("Atmos".into()),
        keep(),
    );
    p.add_regex(
        "audio",
        Regex::new(r"(?i)\b(?:DTS[ ._-]?X|DTSX)\b").unwrap(),
        Transformer::UniqConcatValue("DTS-X".into()),
        keep(),
    );
    p.add_regex(
        "audio",
        Regex::new(r"(?i)\bTrueHD\b").unwrap(),
        Transformer::UniqConcatValue("TrueHD".into()),
        keep(),
    );
    p.add_regex(
        "audio",
        Regex::new(r"(?i)\bDTS[ ._-]?HD(?:[ ._-]?MA)?\b").unwrap(),
        Transformer::UniqConcatValue("DTS Lossless".into()),
        keep(),
    );
    p.add_regex(
        "audio",
        Regex::new(r"(?i)\bDTS\b").unwrap(),
        Transformer::UniqConcatValue("DTS".into()),
        keep(),
    );
    p.add_regex(
        "audio",
        Regex::new(r"(?i)\bDDP(?:5\.1|7\.1|2\.0|\b)|\bE[ ._-]?AC[ ._-]?3\b").unwrap(),
        Transformer::UniqConcatValue("DDP".into()),
        keep(),
    );
    p.add_regex(
        "audio",
        Regex::new(r"(?i)\bAC[ ._-]?3\b").unwrap(),
        Transformer::UniqConcatValue("AC3".into()),
        keep(),
    );
    p.add_regex(
        "audio",
        Regex::new(r"(?i)\bAAC(?:2\.0|5\.1|\b)").unwrap(),
        Transformer::UniqConcatValue("AAC".into()),
        keep(),
    );
    p.add_regex(
        "audio",
        Regex::new(r"(?i)\bFLAC\b").unwrap(),
        Transformer::UniqConcatValue("FLAC".into()),
        keep(),
    );
    p.add_regex(
        "audio",
        Regex::new(r"(?i)\bMP3\b").unwrap(),
        Transformer::UniqConcatValue("MP3".into()),
        keep(),
    );
}

fn add_channels(p: &mut Parser) {
    // PTT returns channels as a list of strings.
    p.add_regex(
        "channels",
        Regex::new(r"\b7\.1\b").unwrap(),
        Transformer::UniqConcatValue("7.1".into()),
        keep(),
    );
    p.add_regex(
        "channels",
        Regex::new(r"\b5\.1\b").unwrap(),
        Transformer::UniqConcatValue("5.1".into()),
        keep(),
    );
    p.add_regex(
        "channels",
        Regex::new(r"\b2\.0\b").unwrap(),
        Transformer::UniqConcatValue("2.0".into()),
        keep(),
    );
}

fn add_hdr(p: &mut Parser) {
    // DV first so HDR10/DV combos record DV; PTT records the first
    // match for the hdr list. Then HDR10+, HDR10, plain HDR.
    p.add_regex(
        "hdr",
        Regex::new(r"(?i)\b(?:DV|Dolby[ ._-]?Vision)\b").unwrap(),
        Transformer::UniqConcatValue("DV".into()),
        keep(),
    );
    p.add_regex(
        "hdr",
        Regex::new(r"(?i)\bHDR10\+").unwrap(),
        Transformer::UniqConcatValue("HDR10+".into()),
        keep(),
    );
    p.add_regex(
        "hdr",
        Regex::new(r"(?i)\bHDR10\b").unwrap(),
        Transformer::UniqConcatValue("HDR10".into()),
        keep(),
    );
    p.add_regex(
        "hdr",
        Regex::new(r"(?i)\bHDR\b").unwrap(),
        Transformer::UniqConcatValue("HDR".into()),
        keep(),
    );
}

fn add_languages(p: &mut Parser) {
    // ISO-639-1 short codes — the parity corpus uses Python PTT's
    // default which returns empty `languages` for English-only
    // titles; only explicit language tags (MULTI, German, FRENCH...)
    // light up the field.
    p.add_regex(
        "languages",
        Regex::new(r"(?i)\bMULTi\b").unwrap(),
        Transformer::UniqConcatValue("multi".into()),
        keep(),
    );
    p.add_regex(
        "languages",
        Regex::new(r"(?i)\b(?:GERMAN|GER)\b").unwrap(),
        Transformer::UniqConcatValue("de".into()),
        keep(),
    );
    p.add_regex(
        "languages",
        Regex::new(r"(?i)\bFRENCH\b").unwrap(),
        Transformer::UniqConcatValue("fr".into()),
        keep(),
    );
    p.add_regex(
        "languages",
        Regex::new(r"(?i)\bITALIAN\b").unwrap(),
        Transformer::UniqConcatValue("it".into()),
        keep(),
    );
    p.add_regex(
        "languages",
        Regex::new(r"(?i)\biTA\b").unwrap(),
        Transformer::UniqConcatValue("it".into()),
        keep(),
    );
    p.add_regex(
        "languages",
        Regex::new(r"(?i)\bENG\b").unwrap(),
        Transformer::UniqConcatValue("en".into()),
        keep(),
    );
}

fn add_edition(p: &mut Parser) {
    p.add_regex_with_value(
        "edition",
        Regex::new(r"(?i)\b(?:Director'?s?\.?Cut|DC)\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("Director's Cut".into()),
    );
    p.add_regex_with_value(
        "edition",
        Regex::new(r"(?i)\bExtended(?:[._ -]?Cut)?\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("Extended".into()),
    );
    p.add_regex_with_value(
        "edition",
        Regex::new(r"(?i)\bUnrated\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("Unrated".into()),
    );
    p.add_regex_with_value(
        "edition",
        Regex::new(r"(?i)\bIMAX\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("IMAX".into()),
    );
    p.add_regex_with_value(
        "edition",
        Regex::new(r"(?i)\bCriterion(?:[._ -]?Collection)?\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("Criterion".into()),
    );
    p.add_regex_with_value(
        "edition",
        Regex::new(r"(?i)\bTheatrical\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("Theatrical".into()),
    );
    p.add_regex_with_value(
        "edition",
        Regex::new(r"(?i)\bHybrid\b").unwrap(),
        Transformer::None_,
        keep(),
        Value::Str("Hybrid".into()),
    );
}

fn add_flags(p: &mut Parser) {
    p.add_regex(
        "proper",
        Regex::new(r"(?i)\bPROPER\b").unwrap(),
        Transformer::Boolean,
        keep(),
    );
    p.add_regex(
        "repack",
        Regex::new(r"(?i)\bREPACK[0-9]*\b").unwrap(),
        Transformer::Boolean,
        keep(),
    );
}

fn add_season_episode(p: &mut Parser) {
    // Range first so S01E01-E04 doesn't get truncated to S01E01.
    p.add_regex(
        "episodes",
        Regex::new(r"(?i)\bS\d{1,2}E\d{1,4}[ ._-]?E?(\d{1,4})?\b").unwrap(),
        Transformer::RangeFunc,
        keep(),
    );
    p.add_regex(
        "seasons",
        Regex::new(r"(?i)\bS(\d{1,2})\b").unwrap(),
        Transformer::UniqConcat,
        keep(),
    );
}

fn add_group(p: &mut Parser) {
    // Hyphen-suffixed scene group at the tail. PTT chooses the
    // trailing -GROUP pattern; we approximate.
    //
    // The character class deliberately excludes `.` so a tail like
    // `DTS-HD.MA.7.1-GROUP` only captures the final `GROUP`, not
    // `HD.MA.7.1-GROUP` (which would happen if `.` were allowed and
    // the regex engine matched the leftmost `-`). Hyphens and
    // underscores are kept so multi-segment groups like `GROUP-NAME`
    // or `GROUP_NAME` survive.
    p.add_regex(
        "group",
        Regex::new(r"-([A-Za-z0-9][A-Za-z0-9_-]{0,30})(?:\.[a-z0-9]+)?$").unwrap(),
        Transformer::None_,
        keep(),
    );
}

fn add_upscaled(p: &mut Parser) {
    p.add_regex(
        "upscaled",
        Regex::new(r"(?i)\bAI[._ -]?Upscale\b").unwrap(),
        Transformer::Boolean,
        keep(),
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_inception_movie() {
        let parsed = parse_title("Inception.2010.1080p.BluRay.x264-FGT");
        assert_eq!(parsed.get("year"), Some(&Value::Int(2010)));
        assert_eq!(parsed.get("resolution"), Some(&Value::Str("1080p".into())));
        assert_eq!(parsed.get("quality"), Some(&Value::Str("BluRay".into())));
        assert_eq!(parsed.get("codec"), Some(&Value::Str("avc".into())));
    }

    #[test]
    fn picks_up_dolby_vision_and_uhd_remux() {
        let parsed = parse_title("Movie.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP");
        assert_eq!(parsed.get("resolution"), Some(&Value::Str("2160p".into())));
        assert_eq!(
            parsed.get("quality"),
            Some(&Value::Str("BluRay REMUX".into()))
        );
        assert_eq!(parsed.get("codec"), Some(&Value::Str("hevc".into())));
        assert_eq!(
            parsed.get("hdr"),
            Some(&Value::List(vec![Value::Str("DV".into())]))
        );
    }

    #[test]
    fn boolean_flags_set_for_proper_and_repack() {
        let parsed = parse_title("Movie.2024.PROPER.REPACK.1080p.BluRay.x264-GROUP");
        assert_eq!(parsed.get("proper"), Some(&Value::Bool(true)));
        assert_eq!(parsed.get("repack"), Some(&Value::Bool(true)));
    }
}
