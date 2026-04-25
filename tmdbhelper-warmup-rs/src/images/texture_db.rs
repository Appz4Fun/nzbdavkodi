use anyhow::{Context, Result};
use rusqlite::{params, Connection, OpenFlags};
use std::path::Path;

pub struct TextureDb {
    conn: Connection,
}

impl TextureDb {
    pub fn open(path: &Path) -> Result<Self> {
        let conn = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
        )
        .with_context(|| format!("open textures db {}", path.display()))?;
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA synchronous=OFF;
             PRAGMA busy_timeout=30000;",
        )?;
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS texture (
                id INTEGER PRIMARY KEY,
                url TEXT,
                cachedurl TEXT,
                imagehash TEXT,
                lasthashcheck TEXT
             );
             CREATE TABLE IF NOT EXISTS sizes (
                idtexture INTEGER,
                size INTEGER,
                width INTEGER,
                height INTEGER,
                usecount INTEGER,
                lastusetime TEXT
             );
             CREATE UNIQUE INDEX IF NOT EXISTS idxTexture ON texture(url);
             CREATE INDEX IF NOT EXISTS idxSize ON sizes(idtexture, size);",
        )?;
        Ok(Self { conn })
    }

    pub fn is_cached(&self, url: &str) -> bool {
        self.conn
            .prepare_cached("SELECT 1 FROM texture WHERE url=?1 LIMIT 1")
            .and_then(|mut s| s.query_row(params![url], |_| Ok(())))
            .is_ok()
    }

    pub fn insert_texture(
        &self,
        url: &str,
        cached_url: &str,
        width: u32,
        height: u32,
    ) -> Result<i64> {
        let now = format_utc_now();
        self.conn.prepare_cached(
            "INSERT OR IGNORE INTO texture (url, cachedurl, imagehash, lasthashcheck) VALUES (?1, ?2, '', ?3)",
        )?.execute(params![url, cached_url, &now])?;

        let id: i64 = self.conn.prepare_cached(
            "SELECT id FROM texture WHERE url=?1",
        )?.query_row(params![url], |r| r.get(0))?;

        self.conn.prepare_cached(
            "INSERT OR IGNORE INTO sizes (idtexture, size, width, height, usecount, lastusetime) VALUES (?1, 1, ?2, ?3, 1, ?4)",
        )?.execute(params![id, width, height, &now])?;

        Ok(id)
    }

    pub fn insert_texture_batch(
        &self,
        items: &[(String, String, u32, u32)],
    ) -> Result<usize> {
        let now = format_utc_now();
        let tx = self.conn.unchecked_transaction()?;
        let mut count = 0usize;

        {
            let mut ins_tex = tx.prepare_cached(
                "INSERT OR IGNORE INTO texture (url, cachedurl, imagehash, lasthashcheck) VALUES (?1, ?2, '', ?3)",
            )?;
            let mut sel_id = tx.prepare_cached(
                "SELECT id FROM texture WHERE url=?1",
            )?;
            let mut ins_size = tx.prepare_cached(
                "INSERT OR IGNORE INTO sizes (idtexture, size, width, height, usecount, lastusetime) VALUES (?1, 1, ?2, ?3, 1, ?4)",
            )?;

            for (url, cached_url, w, h) in items {
                if ins_tex.execute(params![url, cached_url, &now])? > 0 {
                    count += 1;
                }
                let id: i64 = sel_id.query_row(params![url], |r| r.get(0))?;
                ins_size.execute(params![id, w, h, &now])?;
            }
        }

        tx.commit()?;
        Ok(count)
    }
}

fn format_utc_now() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    format_utc(secs)
}

fn format_utc(secs: u64) -> String {
    let days = (secs / 86400) as i64;
    let time_of_day = secs % 86400;
    let h = time_of_day / 3600;
    let m = (time_of_day % 3600) / 60;
    let s = time_of_day % 60;

    // Civil date from days since 1970-01-01 (algorithm from Howard Hinnant)
    let z = days + 719468;
    let era = (if z >= 0 { z } else { z - 146096 }) / 146097;
    let doe = (z - era * 146097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mo = if mp < 10 { mp + 3 } else { mp - 9 };
    let yr = if mo <= 2 { y + 1 } else { y };

    format!("{:04}-{:02}-{:02} {:02}:{:02}:{:02}", yr, mo, d, h, m, s)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::NamedTempFile;

    #[test]
    fn format_utc_known_dates() {
        assert_eq!(format_utc(0), "1970-01-01 00:00:00");
        assert_eq!(format_utc(1745570400), "2025-04-25 08:40:00");
        assert_eq!(format_utc(951782400), "2000-02-29 00:00:00");
        assert_eq!(format_utc(1609459199), "2020-12-31 23:59:59");
    }

    #[test]
    fn open_creates_tables() {
        let f = NamedTempFile::new().unwrap();
        let db = TextureDb::open(f.path()).unwrap();
        assert!(!db.is_cached("https://example.com/test.jpg"));
    }

    #[test]
    fn insert_and_query() {
        let f = NamedTempFile::new().unwrap();
        let db = TextureDb::open(f.path()).unwrap();
        let id = db.insert_texture(
            "https://image.tmdb.org/t/p/w780/test.jpg",
            "f/fa000000.jpg",
            780,
            1170,
        ).unwrap();
        assert!(id > 0);
        assert!(db.is_cached("https://image.tmdb.org/t/p/w780/test.jpg"));
        assert!(!db.is_cached("https://image.tmdb.org/t/p/w500/test.jpg"));
    }

    #[test]
    fn insert_batch() {
        let f = NamedTempFile::new().unwrap();
        let db = TextureDb::open(f.path()).unwrap();
        let items = vec![
            ("https://a.com/1.jpg".into(), "a/a0000001.jpg".into(), 100, 200),
            ("https://a.com/2.jpg".into(), "b/b0000002.jpg".into(), 300, 400),
        ];
        let count = db.insert_texture_batch(&items).unwrap();
        assert_eq!(count, 2);
        assert!(db.is_cached("https://a.com/1.jpg"));
        assert!(db.is_cached("https://a.com/2.jpg"));
    }

    #[test]
    fn insert_ignore_duplicate() {
        let f = NamedTempFile::new().unwrap();
        let db = TextureDb::open(f.path()).unwrap();
        db.insert_texture("https://a.com/x.jpg", "a/a0000001.jpg", 100, 200).unwrap();
        db.insert_texture("https://a.com/x.jpg", "a/a0000001.jpg", 100, 200).unwrap();
        let count: i64 = db.conn.query_row(
            "SELECT COUNT(*) FROM texture WHERE url='https://a.com/x.jpg'", [], |r| r.get(0),
        ).unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn multiple_variants_share_cached_url() {
        let f = NamedTempFile::new().unwrap();
        let db = TextureDb::open(f.path()).unwrap();
        let cached = "f/fa000000.jpg";
        db.insert_texture("https://image.tmdb.org/t/p/original/test.jpg", cached, 1920, 1080).unwrap();
        db.insert_texture("https://image.tmdb.org/t/p/w1280/test.jpg", cached, 1920, 1080).unwrap();
        db.insert_texture("https://image.tmdb.org/t/p/w780/test.jpg", cached, 1920, 1080).unwrap();

        let mut stmt = db.conn.prepare("SELECT cachedurl FROM texture WHERE cachedurl=?1").unwrap();
        let rows: Vec<String> = stmt.query_map(params![cached], |r| r.get(0)).unwrap()
            .filter_map(|r| r.ok()).collect();
        assert_eq!(rows.len(), 3);
    }
}
