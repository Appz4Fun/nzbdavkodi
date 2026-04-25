use anyhow::Result;
use rusqlite::{params, Connection};
use crate::api::types_movie::ImageRef;

/// art(aspect_ratio INTEGER, quality INTEGER, iso_language TEXT, iso_country TEXT,
///     icon TEXT, type TEXT, extension TEXT, rating INTEGER, votes INTEGER, parent_id TEXT,
///     UNIQUE (icon, type, parent_id))
///
/// `art_type` is one of: posters, backdrops, logos, profiles, stills.
///
/// Three semantic conversions match TMDBHelper's `mappings.py`:
/// - `aspect_ratio` is bucketed 1-5 (poster/square/thumb/landscape/wide), not raw
/// - `quality` is megapixel rank `(width * height) / 200000`, not raw width
/// - `rating` is `vote_average * 100` integer, not raw 0-10 float
pub fn write_image(conn: &Connection, parent_id: &str, art_type: &str, img: &ImageRef) -> Result<()> {
    let extension = img.file_path.rsplit('.').next().unwrap_or("jpg");
    let aspect_bucket = aspect_ratio_bucket(img.aspect_ratio);
    let quality = match (img.width, img.height) {
        (Some(w), Some(h)) => Some((w * h) / 200_000),
        _ => None,
    };
    let rating_scaled = img.vote_average.map(|r| (r * 100.0).round() as i64);
    conn.execute(
        "INSERT OR IGNORE INTO art (aspect_ratio, quality, iso_language, iso_country, icon, type, extension, rating, votes, parent_id)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        params![
            aspect_bucket,
            quality,
            img.iso_639_1.as_deref(),
            img.iso_3166_1.as_deref(),  // C6: TMDB returns iso_3166_1 on poster/logo images
            &img.file_path,
            art_type,
            extension,
            rating_scaled,
            img.vote_count,
            parent_id,
        ],
    )?;
    Ok(())
}

/// Bucket TMDB's raw aspect ratio float into TMDBHelper's 1-5 enum.
/// Source: tmdbhelper/lib/items/database/mappings.py — buckets are
/// 1=poster, 2=square, 3=thumb, 4=landscape, 5=wide.
fn aspect_ratio_bucket(ar: Option<f64>) -> Option<i64> {
    let r = ar?;
    Some(if r < 1.0 { 1 }
        else if (r - 1.0).abs() < f64::EPSILON { 2 }
        else if r < 1.7 { 3 }
        else if r <= 1.8 { 4 }
        else { 5 })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn aspect_ratio_buckets_match_tmdbhelper() {
        assert_eq!(aspect_ratio_bucket(None), None);
        assert_eq!(aspect_ratio_bucket(Some(0.667)), Some(1));   // poster
        assert_eq!(aspect_ratio_bucket(Some(1.0)), Some(2));     // square
        assert_eq!(aspect_ratio_bucket(Some(1.5)), Some(3));     // thumb
        assert_eq!(aspect_ratio_bucket(Some(1.778)), Some(4));   // 16:9 landscape
        assert_eq!(aspect_ratio_bucket(Some(2.39)), Some(5));    // anamorphic wide
    }
}
