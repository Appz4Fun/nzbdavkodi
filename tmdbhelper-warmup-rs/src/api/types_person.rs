use serde::Deserialize;

#[derive(Debug, Deserialize)]
pub struct PersonResponse { pub id: i64, pub name: Option<String> }
