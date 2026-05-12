//! Ranking + sorting — 1:1 port of `_sort_results` and its helpers.

use crate::metadata::ParsedMeta;
use crate::settings::FilterSettings;

/// Resolution rank (lower is better).
fn resolution_rank(res: &str) -> u32 {
    match res {
        "2160p" => 0,
        "1080p" => 1,
        "720p" => 2,
        "480p" => 3,
        _ => 4,
    }
}

/// HDR rank, lower is better. Per-tier list — best tier of any
/// present wins.
fn hdr_rank(label: &str) -> u32 {
    match label {
        "Dolby Vision" => 0,
        "HDR10+" => 1,
        "HDR10" => 2,
        "HLG" => 3,
        _ => 4,
    }
}

/// Audio rank. `Atmos` is 0 because the combo logic in
/// [`relevance_audio_rank`] uses `0 in ranks && 1 in ranks` (Atmos +
/// TrueHD) to fast-path to `-1`.
fn audio_rank(label: &str) -> i32 {
    match label {
        "Atmos" => 0,
        "TrueHD" => 1,
        "DTS:X" => 3,
        "DTS-HD MA" => 4,
        "DTS" => 5,
        "DD+" => 6,
        "DD" => 7,
        "AAC" => 8,
        _ => 9,
    }
}

fn relevance_audio_rank(audio: &[String]) -> i32 {
    if audio.is_empty() {
        return 10;
    }
    let ranks: Vec<i32> = audio.iter().map(|s| audio_rank(s)).collect();
    if ranks.contains(&0) && ranks.contains(&1) {
        -1
    } else {
        *ranks.iter().min().unwrap()
    }
}

/// Composite key returned by the relevance sort. Sorts ascending —
/// every component is "lower is better".
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct RelevanceKey {
    pub resolution: u32,
    pub hdr: u32,
    /// 0 if preferred group, 1 otherwise.
    pub preferred: u32,
    pub audio: i32,
    /// Negated size so larger sorts first.
    pub neg_size: i64,
}

/// Build a relevance key for a (meta, size) pair given the filter
/// settings (used only for the preferred-group list).
pub fn relevance_key(
    meta: &ParsedMeta,
    size_bytes: u64,
    settings: &FilterSettings,
) -> RelevanceKey {
    let preferred = if settings
        .release_group
        .iter()
        .any(|g| g.eq_ignore_ascii_case(&meta.group))
    {
        0
    } else {
        1
    };

    let hdr = if meta.hdr.is_empty() {
        5
    } else {
        meta.hdr.iter().map(|h| hdr_rank(h)).min().unwrap()
    };

    let neg_size = -(size_bytes.min(i64::MAX as u64) as i64);

    RelevanceKey {
        resolution: resolution_rank(&meta.resolution),
        hdr,
        preferred,
        audio: relevance_audio_rank(&meta.audio),
        neg_size,
    }
}

/// Sort orders (mirroring `_sort_results`):
///
/// * `0` — relevance (resolution → HDR → preferred → audio → size)
/// * `1` — size desc
/// * `2` — size asc
/// * `3` — pubdate desc
/// * `4` — pubdate asc
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SortOrder {
    Relevance,
    SizeDesc,
    SizeAsc,
    PubdateDesc,
    PubdateAsc,
}

impl SortOrder {
    pub fn from_u8(n: u8) -> Self {
        match n {
            1 => SortOrder::SizeDesc,
            2 => SortOrder::SizeAsc,
            3 => SortOrder::PubdateDesc,
            4 => SortOrder::PubdateAsc,
            _ => SortOrder::Relevance,
        }
    }
}

/// Parse an RFC-822 pubdate string into a unix timestamp. Used by
/// the age sort orders. `None` (or an unparseable string) sorts at
/// the epoch (`0`).
pub fn pubdate_sort_key(pubdate: Option<&str>) -> i64 {
    let s = match pubdate {
        Some(s) if !s.is_empty() => s,
        _ => return 0,
    };
    parse_rfc822(s).unwrap_or(0)
}

/// Minimal RFC-822 parser. Used only for ordering, so we don't need
/// to be locale-tolerant — Hydra2 emits the canonical
/// `Mon, 02 Jan 2006 15:04:05 GMT` shape.
///
/// Returns `None` for any input that doesn't match the expected
/// shape; callers fall back to `0` (matches the Python behaviour of
/// returning `0.0` on parse failure).
fn parse_rfc822(s: &str) -> Option<i64> {
    // "Mon, 02 Jan 2006 15:04:05 GMT"
    let s = s.trim();
    let parts: Vec<&str> = s.split_whitespace().collect();
    // After optional weekday, we need at least: day month year time tz
    // (5 tokens). With weekday: 6 tokens.
    let offset = if !parts.is_empty() && parts[0].ends_with(',') {
        1
    } else {
        0
    };
    if parts.len() < offset + 5 {
        return None;
    }
    let day: u32 = parts[offset].parse().ok()?;
    let month = match parts[offset + 1] {
        "Jan" => 1,
        "Feb" => 2,
        "Mar" => 3,
        "Apr" => 4,
        "May" => 5,
        "Jun" => 6,
        "Jul" => 7,
        "Aug" => 8,
        "Sep" => 9,
        "Oct" => 10,
        "Nov" => 11,
        "Dec" => 12,
        _ => return None,
    };
    let year: i32 = parts[offset + 2].parse().ok()?;
    let time_parts: Vec<&str> = parts[offset + 3].split(':').collect();
    if time_parts.len() < 3 {
        return None;
    }
    let h: u32 = time_parts[0].parse().ok()?;
    let m: u32 = time_parts[1].parse().ok()?;
    let sec: u32 = time_parts[2].parse().ok()?;

    // Days since civil epoch (1970-01-01) — Howard Hinnant's
    // algorithm. Kept inline to avoid a chrono/time dep purely for
    // sort-order generation.
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as u32;
    let doy = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days_since_epoch = era as i64 * 146097 + doe as i64 - 719468;

    Some(days_since_epoch * 86400 + h as i64 * 3600 + m as i64 * 60 + sec as i64)
}

/// Sort a slice of `(meta, size, pubdate)` triples in place.
///
/// Mirrors `_sort_results`: chooses the comparator by
/// `settings.sort_order` and uses a stable sort (`slice::sort_by_*`)
/// to preserve insertion order on ties — matches Python's
/// `sorted(..., key=...)`.
pub fn sort_candidates<T, F>(items: &mut [T], settings: &FilterSettings, mut access: F)
where
    F: FnMut(&T) -> (ParsedMeta, u64, Option<String>),
{
    let order = SortOrder::from_u8(settings.sort_order);
    match order {
        SortOrder::SizeDesc => {
            items.sort_by_cached_key(|c| {
                let (_, size, _) = access(c);
                -(size.min(i64::MAX as u64) as i64)
            });
        }
        SortOrder::SizeAsc => {
            items.sort_by_cached_key(|c| {
                let (_, size, _) = access(c);
                size.min(i64::MAX as u64) as i64
            });
        }
        SortOrder::PubdateDesc => {
            items.sort_by_cached_key(|c| {
                let (_, _, pd) = access(c);
                -pubdate_sort_key(pd.as_deref())
            });
        }
        SortOrder::PubdateAsc => {
            items.sort_by_cached_key(|c| {
                let (_, _, pd) = access(c);
                pubdate_sort_key(pd.as_deref())
            });
        }
        SortOrder::Relevance => {
            items.sort_by_cached_key(|c| {
                let (meta, size, _) = access(c);
                relevance_key(&meta, size, settings)
            });
        }
    }
}
