use anyhow::Result;
use rusqlite::{params, Connection};

pub fn upsert_language(conn: &Connection, iso: &str, name: &str, english_name: &str) -> Result<()> {
    conn.prepare_cached(
        "INSERT OR IGNORE INTO languages (iso_language, name, english_name) VALUES (?1, ?2, ?3)",
    )?.execute(params![iso, name, english_name])?;
    Ok(())
}

pub fn upsert_country(conn: &Connection, iso: &str, name: &str) -> Result<()> {
    conn.prepare_cached(
        "INSERT OR IGNORE INTO countries (iso_country, name) VALUES (?1, ?2)",
    )?.execute(params![iso, name])?;
    Ok(())
}

pub fn upsert_company(conn: &Connection, tmdb_id: i64, name: &str, logo: Option<&str>, country: Option<&str>) -> Result<()> {
    conn.prepare_cached(
        "INSERT OR IGNORE INTO company (tmdb_id, name, logo, country) VALUES (?1, ?2, ?3, ?4)",
    )?.execute(params![tmdb_id, name, logo, country])?;
    Ok(())
}

pub fn upsert_broadcaster(conn: &Connection, tmdb_id: i64, name: &str, logo: Option<&str>, country: Option<&str>) -> Result<()> {
    conn.prepare_cached(
        "INSERT OR IGNORE INTO broadcaster (tmdb_id, name, logo, country) VALUES (?1, ?2, ?3, ?4)",
    )?.execute(params![tmdb_id, name, logo, country])?;
    Ok(())
}

pub fn upsert_service(conn: &Connection, provider_id: i64, name: &str, logo: Option<&str>, display_priority: Option<i64>) -> Result<()> {
    conn.prepare_cached(
        "INSERT OR IGNORE INTO service (tmdb_id, name, logo, display_priority) VALUES (?1, ?2, ?3, ?4)",
    )?.execute(params![provider_id, name, logo, display_priority])?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("
            CREATE TABLE languages(name TEXT, english_name TEXT, iso_language TEXT PRIMARY KEY);
            CREATE TABLE countries(name TEXT, iso_country TEXT PRIMARY KEY);
            CREATE TABLE company(tmdb_id INTEGER PRIMARY KEY, name TEXT, logo TEXT, country TEXT);
        ").unwrap();
        c
    }

    #[test]
    fn upsert_language_idempotent() {
        let mut c = temp_conn();
        let tx = c.transaction().unwrap();
        upsert_language(&tx, "en", "English", "English").unwrap();
        upsert_language(&tx, "en", "English (changed)", "English (changed)").unwrap();
        tx.commit().unwrap();
        let name: String = c.query_row("SELECT name FROM languages WHERE iso_language='en'", [], |r| r.get(0)).unwrap();
        assert_eq!(name, "English", "INSERT OR IGNORE must not overwrite");
    }
}
