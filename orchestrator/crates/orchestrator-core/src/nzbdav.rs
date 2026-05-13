// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! SABnzbd-compatible nzbdav API client.

use std::time::Duration;

use reqwest::Client;
use serde::{Deserialize, Serialize};

use crate::webdav::WebdavConfig;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct NzbdavConfig {
    pub base_url: String,
    #[serde(default)]
    pub api_key: String,
    #[serde(default)]
    pub webdav_url: String,
    #[serde(default)]
    pub webdav_username: String,
    #[serde(default)]
    pub webdav_password: String,
    #[serde(default)]
    pub webdav_content_root: Option<String>,
}

impl NzbdavConfig {
    pub fn webdav_config(&self) -> WebdavConfig {
        WebdavConfig {
            base_url: if self.webdav_url.trim().is_empty() {
                self.base_url.clone()
            } else {
                self.webdav_url.clone()
            },
            username: self.webdav_username.clone(),
            password: self.webdav_password.clone(),
            content_root: self
                .webdav_content_root
                .clone()
                .unwrap_or_else(|| "content".to_string()),
        }
    }
}

#[derive(Clone)]
pub struct NzbdavClient {
    cfg: NzbdavConfig,
    http: Client,
}

#[derive(Debug, Clone)]
pub struct QueueStatus {
    pub status: String,
    pub percentage: Option<String>,
}

#[derive(Debug, Clone)]
pub struct HistoryEntry {
    pub status: String,
    pub storage: String,
    pub name: String,
    pub nzo_id: String,
    pub fail_message: Option<String>,
    pub completed: Option<i64>,
}

#[derive(Debug, thiserror::Error)]
pub enum NzbdavError {
    #[error("invalid nzbdav URL {url:?}: {message}")]
    InvalidUrl { url: String, message: String },
    #[error("HTTP request failed: {0}")]
    Http(String),
    #[error("nzbdav returned HTTP {0}")]
    HttpStatus(u16),
    #[error("nzbdav returned invalid JSON: {0}")]
    InvalidJson(String),
    #[error("nzbdav rejected the request: {0}")]
    Rejected(String),
    #[error("nzbdav response did not contain an nzo_id")]
    MissingNzoId,
}

impl NzbdavClient {
    pub fn new(cfg: NzbdavConfig) -> Result<Self, NzbdavError> {
        parse_api_url(&cfg.base_url)?;
        Ok(Self {
            cfg,
            http: Client::builder()
                .user_agent("NZB-DAV Kodi Addon")
                .build()
                .map_err(|e| NzbdavError::Http(e.to_string()))?,
        })
    }

    pub async fn submit_nzb(&self, nzb_url: &str, title: &str) -> Result<String, NzbdavError> {
        let url = parse_api_url(&self.cfg.base_url)?;
        let response = self
            .http
            .get(url)
            .query(&[
                ("mode", "addurl"),
                ("name", nzb_url),
                ("nzbname", title),
                ("apikey", self.cfg.api_key.as_str()),
                ("output", "json"),
            ])
            .timeout(Duration::from_secs(300))
            .send()
            .await
            .map_err(|e| NzbdavError::Http(e.to_string()))?;
        json_response(response).await.and_then(parse_submit)
    }

    pub async fn get_job_status(&self, nzo_id: &str) -> Result<Option<QueueStatus>, NzbdavError> {
        let url = parse_api_url(&self.cfg.base_url)?;
        let response = self
            .http
            .get(url)
            .query(&[
                ("mode", "queue"),
                ("nzo_ids", nzo_id),
                ("apikey", self.cfg.api_key.as_str()),
                ("output", "json"),
            ])
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .map_err(|e| NzbdavError::Http(e.to_string()))?;
        let json = json_response(response).await?;
        Ok(queue_slots(&json).into_iter().find_map(|slot| {
            if slot.get("nzo_id").and_then(|v| v.as_str()) == Some(nzo_id) {
                Some(QueueStatus {
                    status: string_field(slot, "status").unwrap_or_else(|| "Unknown".into()),
                    percentage: string_field(slot, "percentage"),
                })
            } else {
                None
            }
        }))
    }

    pub async fn get_job_history(&self, nzo_id: &str) -> Result<Option<HistoryEntry>, NzbdavError> {
        let url = parse_api_url(&self.cfg.base_url)?;
        let response = self
            .http
            .get(url)
            .query(&[
                ("mode", "history"),
                ("nzo_ids", nzo_id),
                ("apikey", self.cfg.api_key.as_str()),
                ("output", "json"),
            ])
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .map_err(|e| NzbdavError::Http(e.to_string()))?;
        let json = json_response(response).await?;
        Ok(history_slots(&json).into_iter().find_map(|slot| {
            if slot.get("nzo_id").and_then(|v| v.as_str()) == Some(nzo_id) {
                Some(history_entry(slot))
            } else {
                None
            }
        }))
    }

    pub async fn find_completed_by_name(
        &self,
        title: &str,
    ) -> Result<Option<HistoryEntry>, NzbdavError> {
        let Some(entry) = self.find_terminal_by_name(title).await? else {
            return Ok(None);
        };
        if entry.status == "Completed" {
            Ok(Some(entry))
        } else {
            Ok(None)
        }
    }

    pub async fn find_terminal_by_name(
        &self,
        title: &str,
    ) -> Result<Option<HistoryEntry>, NzbdavError> {
        if title.trim().is_empty() {
            return Ok(None);
        }
        let search = history_search_term(title);
        let url = parse_api_url(&self.cfg.base_url)?;
        let response = self
            .http
            .get(url)
            .query(&[
                ("mode", "history"),
                ("apikey", self.cfg.api_key.as_str()),
                ("output", "json"),
                ("limit", "200"),
                ("search", search.as_str()),
            ])
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .map_err(|e| NzbdavError::Http(e.to_string()))?;
        let json = json_response(response).await?;
        Ok(history_slots(&json)
            .into_iter()
            .filter(|slot| {
                slot.get("name").and_then(|v| v.as_str()) == Some(title)
                    && matches!(
                        slot.get("status").and_then(|v| v.as_str()),
                        Some("Completed" | "Failed")
                    )
            })
            .max_by_key(|slot| slot.get("completed").and_then(|v| v.as_i64()).unwrap_or(-1))
            .map(history_entry))
    }
}

fn parse_api_url(base_url: &str) -> Result<url::Url, NzbdavError> {
    let trimmed = base_url.trim().trim_end_matches('/');
    let candidate = format!("{trimmed}/api");
    let parsed = url::Url::parse(&candidate).map_err(|e| NzbdavError::InvalidUrl {
        url: base_url.to_string(),
        message: e.to_string(),
    })?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(NzbdavError::InvalidUrl {
            url: base_url.to_string(),
            message: format!("unsupported scheme {}", parsed.scheme()),
        });
    }
    Ok(parsed)
}

async fn json_response(response: reqwest::Response) -> Result<serde_json::Value, NzbdavError> {
    let status = response.status();
    if !status.is_success() {
        return Err(NzbdavError::HttpStatus(status.as_u16()));
    }
    response
        .json::<serde_json::Value>()
        .await
        .map_err(|e| NzbdavError::InvalidJson(e.to_string()))
}

fn parse_submit(json: serde_json::Value) -> Result<String, NzbdavError> {
    if !truthy(json.get("status")) {
        let message = string_field(&json, "error").unwrap_or_else(|| "status=false".into());
        return Err(NzbdavError::Rejected(message));
    }
    if let Some(id) = json.get("nzo_id").and_then(|v| v.as_str()) {
        if !id.is_empty() {
            return Ok(id.to_string());
        }
    }
    if let Some(id) = json
        .get("nzo_ids")
        .and_then(|v| v.as_array())
        .and_then(|a| a.first())
        .and_then(|v| v.as_str())
    {
        if !id.is_empty() {
            return Ok(id.to_string());
        }
    }
    Err(NzbdavError::MissingNzoId)
}

fn truthy(value: Option<&serde_json::Value>) -> bool {
    match value {
        Some(serde_json::Value::Bool(v)) => *v,
        Some(serde_json::Value::String(v)) => {
            matches!(v.to_ascii_lowercase().as_str(), "true" | "ok" | "1")
        }
        Some(serde_json::Value::Number(v)) => v.as_i64().unwrap_or(0) != 0,
        _ => false,
    }
}

fn queue_slots(json: &serde_json::Value) -> Vec<&serde_json::Value> {
    json.get("queue")
        .and_then(|v| v.get("slots"))
        .and_then(|v| v.as_array())
        .map(|slots| slots.iter().collect())
        .unwrap_or_default()
}

fn history_slots(json: &serde_json::Value) -> Vec<&serde_json::Value> {
    json.get("history")
        .and_then(|v| v.get("slots"))
        .and_then(|v| v.as_array())
        .map(|slots| slots.iter().collect())
        .unwrap_or_default()
}

fn history_entry(slot: &serde_json::Value) -> HistoryEntry {
    HistoryEntry {
        status: string_field(slot, "status").unwrap_or_default(),
        storage: string_field(slot, "storage").unwrap_or_default(),
        name: string_field(slot, "name").unwrap_or_default(),
        nzo_id: string_field(slot, "nzo_id").unwrap_or_default(),
        fail_message: string_field(slot, "fail_message"),
        completed: slot.get("completed").and_then(|v| v.as_i64()),
    }
}

fn string_field(value: &serde_json::Value, key: &str) -> Option<String> {
    value
        .get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

fn history_search_term(name: &str) -> String {
    name.split_once('.')
        .map(|(prefix, _)| prefix.to_string())
        .unwrap_or_else(|| name.to_string())
}
