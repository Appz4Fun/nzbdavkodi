//! The pipeline entry point — `filter_results`-equivalent.

use serde::{Deserialize, Serialize};

use crate::metadata::{parse_metadata, ParsedMeta};
use crate::rank::sort_candidates;
use crate::settings::FilterSettings;

/// Lean input row. Captures the only fields the filter needs from a
/// release-search hit. Distinct from the eventual provider
/// [`Candidate`](crate::types::Candidate) so the filter crate stays
/// pure-logic / I/O-free.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FilterInput {
    pub title: String,
    /// Size in bytes. Use `0` for unknown — the Python `_size_sort_key`
    /// already collapses unparseable/`None` to `0`.
    #[serde(default)]
    pub size_bytes: u64,
    /// Original indexer-supplied pubdate (RFC-822). Only used by the
    /// age sort orders.
    #[serde(default)]
    pub pubdate: Option<String>,
}

/// The pipeline output for a single candidate.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FilteredCandidate {
    pub input: FilterInput,
    /// Normalised metadata extracted from the title. Equivalent to
    /// the `_meta` key Python attached.
    pub meta: ParsedMeta,
}

/// Reasons a candidate was rejected. Mirrors the Layer-2 reason
/// enum in plan §11.2.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RejectionReason {
    ResolutionMismatch,
    HdrMismatch,
    AudioMismatch,
    CodecExcluded,
    LanguageMismatch,
    ReleaseGroupExcluded,
    KeywordRequiredMissing,
    KeywordExcludedPresent,
    SizeBelowMin,
    SizeAboveMax,
    PttParseFailed,
}

impl RejectionReason {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::ResolutionMismatch => "resolution_mismatch",
            Self::HdrMismatch => "hdr_mismatch",
            Self::AudioMismatch => "audio_mismatch",
            Self::CodecExcluded => "codec_excluded",
            Self::LanguageMismatch => "language_mismatch",
            Self::ReleaseGroupExcluded => "release_group_excluded",
            Self::KeywordRequiredMissing => "keyword_required_missing",
            Self::KeywordExcludedPresent => "keyword_excluded_present",
            Self::SizeBelowMin => "size_below_min",
            Self::SizeAboveMax => "size_above_max",
            Self::PttParseFailed => "ptt_parse_failed",
        }
    }
}

/// True iff every configured filter accepts this candidate. Returns
/// the first matching rejection reason on failure for observability.
///
/// Pure function — does not touch input.
pub fn matches_filters(
    input: &FilterInput,
    meta: &ParsedMeta,
    settings: &FilterSettings,
) -> Result<(), (RejectionReason, String)> {
    let title_lower = input.title.to_lowercase();

    if !settings.resolutions.is_empty()
        && !meta.resolution.is_empty()
        && !settings.resolutions.iter().any(|r| r == &meta.resolution)
    {
        return Err((
            RejectionReason::ResolutionMismatch,
            format!("resolution={}", meta.resolution),
        ));
    }

    if !settings.hdr.is_empty()
        && !meta.hdr.is_empty()
        && !meta.hdr.iter().any(|h| settings.hdr.iter().any(|s| s == h))
    {
        return Err((RejectionReason::HdrMismatch, format!("hdr={:?}", meta.hdr)));
    }
    if !settings.hdr.is_empty() && meta.hdr.is_empty() && !settings.hdr.iter().any(|s| s == "SDR") {
        return Err((
            RejectionReason::HdrMismatch,
            "hdr=<none> and SDR not allowed".into(),
        ));
    }

    if !settings.audio.is_empty()
        && !meta.audio.is_empty()
        && !meta
            .audio
            .iter()
            .any(|a| settings.audio.iter().any(|s| s == a))
    {
        return Err((
            RejectionReason::AudioMismatch,
            format!("audio={:?}", meta.audio),
        ));
    }

    if !settings.codecs.is_empty()
        && !meta.codec.is_empty()
        && !settings.codecs.iter().any(|c| c == &meta.codec)
    {
        return Err((
            RejectionReason::CodecExcluded,
            format!("codec={}", meta.codec),
        ));
    }

    if !settings.languages.is_empty()
        && !meta.languages.is_empty()
        && !meta
            .languages
            .iter()
            .any(|l| settings.languages.iter().any(|s| s == l))
    {
        return Err((
            RejectionReason::LanguageMismatch,
            format!("languages={:?}", meta.languages),
        ));
    }

    for kw in &settings.exclude_keywords {
        if title_lower.contains(kw) {
            return Err((
                RejectionReason::KeywordExcludedPresent,
                format!("keyword={}", kw),
            ));
        }
    }

    for kw in &settings.require_keywords {
        if !title_lower.contains(kw) {
            return Err((
                RejectionReason::KeywordRequiredMissing,
                format!("keyword={}", kw),
            ));
        }
    }

    if !meta.group.is_empty() {
        let group_lower = meta.group.to_lowercase();
        if settings
            .exclude_release_group
            .iter()
            .any(|g| g == &group_lower)
        {
            return Err((
                RejectionReason::ReleaseGroupExcluded,
                format!("group={}", meta.group),
            ));
        }
    }

    // Size — Python computes `size_mb = int(raw)/1048576` (or 0 if
    // unparseable). We feed bytes directly and divide by 1024*1024
    // with integer math, which matches Python's `int(...)/1048576`
    // for any whole-byte value the indexer would emit.
    let size_mb = input.size_bytes / 1_048_576;
    if settings.min_size > 0 && size_mb < settings.min_size {
        return Err((
            RejectionReason::SizeBelowMin,
            format!("size_mb={}, min={}", size_mb, settings.min_size),
        ));
    }
    if settings.max_size > 0 && size_mb > settings.max_size {
        return Err((
            RejectionReason::SizeAboveMax,
            format!("size_mb={}, max={}", size_mb, settings.max_size),
        ));
    }

    Ok(())
}

/// The pipeline output.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FilterOutput {
    /// Subset that passed every rule, sorted by `sort_order`, then
    /// truncated to `max_results` (when non-zero).
    pub filtered: Vec<FilteredCandidate>,
    /// Every input, with metadata attached, sorted by the same key.
    pub all_parsed: Vec<FilteredCandidate>,
}

/// Parse, filter, sort, truncate. Pure function. Emits `tracing`
/// events per plan §11.2.
pub fn filter_results(inputs: Vec<FilterInput>, settings: &FilterSettings) -> FilterOutput {
    let before = inputs.len();

    let mut all_parsed: Vec<FilteredCandidate> = Vec::with_capacity(before);
    let mut filtered: Vec<FilteredCandidate> = Vec::new();

    for input in inputs.into_iter() {
        let meta = parse_metadata(&input.title);
        let candidate = FilteredCandidate {
            input: input.clone(),
            meta: meta.clone(),
        };

        match matches_filters(&input, &meta, settings) {
            Ok(()) => filtered.push(candidate.clone()),
            Err((reason, detail)) => {
                tracing::info!(
                    event = "filter.candidate_rejected",
                    nzb_title = %input.title,
                    reason = reason.as_str(),
                    matched_rule = %detail,
                    "candidate rejected"
                );
            }
        }
        all_parsed.push(candidate);
    }

    // Stable sort on both lists so insertion order is preserved on
    // ties — matches Python's `sorted(...)`.
    sort_candidates(&mut filtered, settings, |c| {
        (c.meta.clone(), c.input.size_bytes, c.input.pubdate.clone())
    });
    sort_candidates(&mut all_parsed, settings, |c| {
        (c.meta.clone(), c.input.size_bytes, c.input.pubdate.clone())
    });

    let matched_count = filtered.len();
    let max_results = settings.max_results as usize;
    if max_results > 0 && filtered.len() > max_results {
        filtered.truncate(max_results);
    }

    let after = filtered.len();
    tracing::info!(
        event = "filter.applied",
        before = before,
        matched = matched_count,
        after = after,
        rules_applied = describe_rules(settings),
        "filter pipeline applied"
    );

    FilterOutput {
        filtered,
        all_parsed,
    }
}

fn describe_rules(s: &FilterSettings) -> String {
    let mut bits: Vec<String> = Vec::new();
    if !s.resolutions.is_empty() {
        bits.push(format!("resolutions={:?}", s.resolutions));
    }
    if !s.hdr.is_empty() {
        bits.push(format!("hdr={:?}", s.hdr));
    }
    if !s.audio.is_empty() {
        bits.push(format!("audio={:?}", s.audio));
    }
    if !s.codecs.is_empty() {
        bits.push(format!("codecs={:?}", s.codecs));
    }
    if !s.languages.is_empty() {
        bits.push(format!("languages={:?}", s.languages));
    }
    if !s.exclude_keywords.is_empty() {
        bits.push(format!("exclude_keywords={:?}", s.exclude_keywords));
    }
    if !s.require_keywords.is_empty() {
        bits.push(format!("require_keywords={:?}", s.require_keywords));
    }
    if !s.release_group.is_empty() {
        bits.push(format!("preferred_groups={:?}", s.release_group));
    }
    if !s.exclude_release_group.is_empty() {
        bits.push(format!("excluded_groups={:?}", s.exclude_release_group));
    }
    if s.min_size > 0 {
        bits.push(format!("min_size_mb={}", s.min_size));
    }
    if s.max_size > 0 {
        bits.push(format!("max_size_mb={}", s.max_size));
    }
    bits.push(format!("sort_order={}", s.sort_order));
    bits.push(format!("max_results={}", s.max_results));
    bits.join("; ")
}
