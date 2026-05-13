// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! WebDAV discovery for completed nzbdav jobs.

use std::collections::BTreeMap;
use std::time::Duration;

use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use quick_xml::events::Event;
use quick_xml::Reader;
use reqwest::{Client, Method, StatusCode};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct WebdavConfig {
    pub base_url: String,
    #[serde(default)]
    pub username: String,
    #[serde(default)]
    pub password: String,
    #[serde(default = "default_content_root")]
    pub content_root: String,
}

fn default_content_root() -> String {
    "content".to_string()
}

#[derive(Debug, Clone, Serialize)]
pub struct VideoStream {
    pub path: String,
    pub url: String,
    pub headers: BTreeMap<String, String>,
    pub content_length: Option<u64>,
}

#[derive(Debug, Clone)]
struct VideoFile {
    path: String,
    size: Option<u64>,
}

#[derive(Debug, thiserror::Error)]
pub enum WebdavError {
    #[error("invalid WebDAV URL {url:?}: {message}")]
    InvalidUrl { url: String, message: String },
    #[error("WebDAV request failed: {0}")]
    Http(String),
    #[error("WebDAV returned HTTP {0}")]
    HttpStatus(u16),
    #[error("WebDAV XML parse error: {0}")]
    Xml(String),
    #[error("no video file found under {0}")]
    NoVideo(String),
}

pub async fn find_video_stream_for_storage(
    cfg: &WebdavConfig,
    storage_path: &str,
) -> Result<VideoStream, WebdavError> {
    let folder = storage_to_webdav_path(storage_path, &cfg.content_root);
    find_video_stream_for_folder(cfg, &folder).await
}

pub async fn find_video_stream_for_folder(
    cfg: &WebdavConfig,
    folder_path: &str,
) -> Result<VideoStream, WebdavError> {
    let client = Client::builder()
        .user_agent("NZB-DAV Kodi Addon")
        .build()
        .map_err(|e| WebdavError::Http(e.to_string()))?;
    let Some(file) = find_video_file(&client, cfg, folder_path, 0, &mut Vec::new()).await? else {
        return Err(WebdavError::NoVideo(folder_path.to_string()));
    };
    let url = webdav_url(cfg, &file.path, true)?;
    Ok(VideoStream {
        path: file.path,
        url,
        headers: auth_headers(cfg),
        content_length: file.size,
    })
}

fn find_video_file<'a>(
    client: &'a Client,
    cfg: &'a WebdavConfig,
    folder_path: &'a str,
    depth: usize,
    visited: &'a mut Vec<String>,
) -> std::pin::Pin<
    Box<dyn std::future::Future<Output = Result<Option<VideoFile>, WebdavError>> + Send + 'a>,
> {
    Box::pin(async move {
        if depth > 2 {
            return Ok(None);
        }
        let normalized = folder_path.trim_end_matches('/').to_string();
        if visited.iter().any(|path| path == &normalized) {
            return Ok(None);
        }
        visited.push(normalized);

        let listing = propfind(client, cfg, folder_path).await?;
        if listing.best_file.is_some() {
            return Ok(listing.best_file);
        }
        for subdir in listing.subdirs {
            if let Some(found) = find_video_file(client, cfg, &subdir, depth + 1, visited).await? {
                return Ok(Some(found));
            }
        }
        Ok(None)
    })
}

#[derive(Default)]
struct FolderListing {
    best_file: Option<VideoFile>,
    subdirs: Vec<String>,
}

async fn propfind(
    client: &Client,
    cfg: &WebdavConfig,
    folder_path: &str,
) -> Result<FolderListing, WebdavError> {
    let mut url = webdav_url(cfg, folder_path, true)?;
    if !url.ends_with('/') {
        url.push('/');
    }
    let propfind = Method::from_bytes(b"PROPFIND").expect("valid method");
    let mut req = client
        .request(propfind, &url)
        .header("Depth", "1")
        .timeout(Duration::from_secs(10));
    if !cfg.username.is_empty() {
        req = req.basic_auth(cfg.username.clone(), Some(cfg.password.clone()));
    }
    let response = req
        .send()
        .await
        .map_err(|e| WebdavError::Http(e.to_string()))?;
    let status = response.status();
    if !(status.is_success() || status == StatusCode::MULTI_STATUS) {
        return Err(WebdavError::HttpStatus(status.as_u16()));
    }
    let body = response
        .text()
        .await
        .map_err(|e| WebdavError::Http(e.to_string()))?;
    parse_propfind(&body, request_path_from_url(&url).as_str())
}

fn parse_propfind(xml: &str, request_path: &str) -> Result<FolderListing, WebdavError> {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(true);
    let mut buf = Vec::new();
    let mut response = ResponseInProgress::default();
    let mut in_response = false;
    let mut text_target = TextTarget::None;
    let mut out = FolderListing::default();

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Eof) => break,
            Ok(Event::Start(e)) => {
                let name = local_name(e.name().as_ref()).to_string();
                match name.as_str() {
                    "response" => {
                        in_response = true;
                        response = ResponseInProgress::default();
                    }
                    "href" if in_response => text_target = TextTarget::Href,
                    "getcontentlength" if in_response => text_target = TextTarget::ContentLength,
                    "collection" if in_response => response.is_collection = true,
                    _ => {}
                }
            }
            Ok(Event::Empty(e)) => {
                if in_response && local_name(e.name().as_ref()) == "collection" {
                    response.is_collection = true;
                }
            }
            Ok(Event::Text(t)) => {
                if in_response {
                    let value = t.unescape().unwrap_or_default().into_owned();
                    match text_target {
                        TextTarget::Href => response.href.push_str(&value),
                        TextTarget::ContentLength => response.content_length.push_str(&value),
                        TextTarget::None => {}
                    }
                }
            }
            Ok(Event::End(e)) => {
                let name = local_name(e.name().as_ref()).to_string();
                if name == "response" {
                    consume_response(&mut out, &response, request_path);
                    in_response = false;
                    text_target = TextTarget::None;
                } else if matches!(name.as_str(), "href" | "getcontentlength") {
                    text_target = TextTarget::None;
                }
            }
            Ok(_) => {}
            Err(e) => return Err(WebdavError::Xml(e.to_string())),
        }
        buf.clear();
    }

    Ok(out)
}

#[derive(Default)]
struct ResponseInProgress {
    href: String,
    is_collection: bool,
    content_length: String,
}

#[derive(Clone, Copy)]
enum TextTarget {
    None,
    Href,
    ContentLength,
}

fn consume_response(out: &mut FolderListing, response: &ResponseInProgress, request_path: &str) {
    let href = response.href.trim();
    if href.is_empty() {
        return;
    }
    let path = href_path(href);
    if response.is_collection {
        if path.trim_end_matches('/') != request_path.trim_end_matches('/') {
            out.subdirs.push(format!("{}/", path.trim_end_matches('/')));
        }
        return;
    }
    if !is_video_path(&path) {
        return;
    }
    let size = response.content_length.trim().parse::<u64>().ok();
    let current = out.best_file.as_ref().and_then(|f| f.size).unwrap_or(0);
    if size.unwrap_or(0) >= current {
        out.best_file = Some(VideoFile { path, size });
    }
}

pub fn storage_to_webdav_path(storage_path: &str, content_root: &str) -> String {
    let root = content_root.trim_matches('/').trim();
    let root = if root.is_empty() { "content" } else { root };
    let storage = storage_path.trim();
    if storage.starts_with(&format!("/{root}/")) || storage == format!("/{root}") {
        return ensure_trailing_slash(storage.to_string());
    }
    if let Some((_, tail)) = storage.split_once("/completed-symlinks/") {
        return ensure_trailing_slash(format!("/{root}/{}", tail.trim_start_matches('/')));
    }
    let parts: Vec<&str> = storage
        .trim_matches('/')
        .split('/')
        .filter(|part| !part.is_empty())
        .collect();
    let suffix = if parts.len() >= 2 {
        format!("{}/{}", parts[parts.len() - 2], parts[parts.len() - 1])
    } else {
        parts.last().copied().unwrap_or("").to_string()
    };
    ensure_trailing_slash(format!("/{root}/{suffix}"))
}

fn ensure_trailing_slash(mut path: String) -> String {
    if !path.ends_with('/') {
        path.push('/');
    }
    path
}

fn webdav_url(
    cfg: &WebdavConfig,
    path: &str,
    preserve_percent: bool,
) -> Result<String, WebdavError> {
    let parsed = url::Url::parse(cfg.base_url.trim_end_matches('/')).map_err(|e| {
        WebdavError::InvalidUrl {
            url: cfg.base_url.clone(),
            message: e.to_string(),
        }
    })?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(WebdavError::InvalidUrl {
            url: cfg.base_url.clone(),
            message: format!("unsupported scheme {}", parsed.scheme()),
        });
    }
    let encoded_path = encode_path(path, preserve_percent);
    Ok(format!(
        "{}/{}",
        cfg.base_url.trim_end_matches('/'),
        encoded_path.trim_start_matches('/')
    ))
}

fn encode_path(path: &str, preserve_percent: bool) -> String {
    let mut out = String::with_capacity(path.len());
    for byte in path.as_bytes() {
        let b = *byte;
        if b == b'/'
            || (preserve_percent && b == b'%')
            || b.is_ascii_alphanumeric()
            || matches!(b, b'-' | b'_' | b'.' | b'~')
        {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

fn auth_headers(cfg: &WebdavConfig) -> BTreeMap<String, String> {
    let mut headers = BTreeMap::new();
    if !cfg.username.is_empty() {
        let user = cfg.username.replace(['\r', '\n'], "");
        let pass = cfg.password.replace(['\r', '\n'], "");
        headers.insert(
            "Authorization".to_string(),
            format!("Basic {}", BASE64_STANDARD.encode(format!("{user}:{pass}"))),
        );
    }
    headers
}

fn href_path(href: &str) -> String {
    if href.starts_with("http://") || href.starts_with("https://") {
        if let Ok(parsed) = url::Url::parse(href) {
            return parsed.path().to_string();
        }
    }
    if href.starts_with("//") {
        if let Ok(parsed) = url::Url::parse(&format!("http:{href}")) {
            return parsed.path().to_string();
        }
    }
    href.to_string()
}

fn request_path_from_url(url: &str) -> String {
    url::Url::parse(url)
        .map(|parsed| parsed.path().trim_end_matches('/').to_string())
        .unwrap_or_default()
}

fn is_video_path(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    [".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov"]
        .iter()
        .any(|ext| lower.ends_with(ext))
}

fn local_name(qualified: &[u8]) -> &str {
    let raw = std::str::from_utf8(qualified).unwrap_or("");
    raw.rsplit_once(':').map(|(_, local)| local).unwrap_or(raw)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn storage_paths_match_python_resolver_shape() {
        assert_eq!(
            storage_to_webdav_path(
                "/mnt/nzbdav/completed-symlinks/uncategorized/Send Help 2026 1080p",
                "content"
            ),
            "/content/uncategorized/Send Help 2026 1080p/"
        );
        assert_eq!(
            storage_to_webdav_path("/content/uncategorized/Movie Name", "content"),
            "/content/uncategorized/Movie Name/"
        );
    }
}
