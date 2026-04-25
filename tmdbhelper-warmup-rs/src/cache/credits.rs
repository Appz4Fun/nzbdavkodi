use anyhow::Result;
use rusqlite::{params, Transaction};
use crate::api::types_movie::{CastMember, CrewMember};

pub fn write_castmember(tx: &Transaction, parent_id: &str, c: &CastMember) -> Result<()> {
    tx.execute(
        "INSERT OR IGNORE INTO castmember (tmdb_id, role, ordering, appearances, guest, parent_id)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![c.id, c.character.as_deref(), c.order, 1_i64, None::<i64>, parent_id],
    )?;
    Ok(())
}

pub fn write_crewmember(tx: &Transaction, parent_id: &str, c: &CrewMember) -> Result<()> {
    tx.execute(
        "INSERT OR IGNORE INTO crewmember (tmdb_id, role, department, appearances, parent_id)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![c.id, c.job.as_deref(), c.department.as_deref(), 1_i64, parent_id],
    )?;
    Ok(())
}
