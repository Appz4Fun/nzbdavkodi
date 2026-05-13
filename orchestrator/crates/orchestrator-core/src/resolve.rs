// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! Resolve pipeline for migration Phases 2-3.

use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use futures::future::join_all;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tracing::info;
use ulid::Ulid;

use crate::nzb_manifest::{
    extract_article_manifest_limited, rank_article_overlap_candidates, NzbArticleManifest,
};
use crate::nzbdav::{HistoryEntry, NzbdavClient, NzbdavConfig, NzbdavError};
use crate::webdav::{VideoStream, WebdavConfig, WebdavError};

const MAX_NZB_MANIFEST_BYTES: usize = 100 * 1024 * 1024;
const CONTENT_LENGTH_EPSILON_BYTES: u64 = 0;
const BYTE_SAMPLE_HEAD_LENGTHS: [u64; 3] = [2, 100, 4096];

#[derive(Debug, Clone, Deserialize)]
pub struct ResolveRequest {
    #[serde(default)]
    pub resolve_id: Option<String>,
    pub nzb_url: String,
    pub title: String,
    #[serde(default)]
    pub peer_pool_cache_key: Option<String>,
    #[serde(default)]
    pub fallback_count: Option<u32>,
    #[serde(default)]
    pub candidate_peers: Vec<ResolvePeerCandidate>,
    #[serde(default)]
    pub poll_interval_secs: Option<u64>,
    #[serde(default)]
    pub download_timeout_secs: Option<u64>,
    pub nzbdav: NzbdavConfig,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ResolvePeerCandidate {
    pub nzb_url: String,
    pub title: String,
    #[serde(default)]
    pub size: Option<u64>,
    #[serde(default)]
    pub indexer: String,
    #[serde(default)]
    pub extra: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ResolveResponse {
    pub resolve_id: String,
    pub primary_peer_id: String,
    pub nzo_id: String,
    pub stream_url: String,
    pub stream_headers: BTreeMap<String, String>,
    pub peer_cohort: Vec<PeerCohortResponse>,
    pub peers: Vec<PeerResponse>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PeerCohortResponse {
    pub peer_id: String,
    pub title: String,
    pub nzb_url: String,
    pub state: &'static str,
    pub validation_state: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub nzo_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub submit_error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub validation_error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stream_url: Option<String>,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub stream_headers: BTreeMap<String, String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content_length: Option<u64>,
    pub shared_articles: usize,
    pub union_articles: usize,
    pub jaccard: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct PeerResponse {
    pub peer_id: String,
    pub state: &'static str,
    pub validation_state: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub nzo_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stream_url: Option<String>,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub stream_headers: BTreeMap<String, String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content_length: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub submit_error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub validation_error: Option<String>,
}

#[derive(Debug, Clone)]
pub struct ResolveProgressEvent {
    pub resolve_id: String,
    pub event: &'static str,
    pub peer_id: Option<String>,
    pub state: &'static str,
    pub reason: Option<String>,
    pub payload: serde_json::Value,
}

type ProgressSink = std::sync::Arc<dyn Fn(ResolveProgressEvent) + Send + Sync>;

#[derive(Debug, thiserror::Error)]
pub enum ResolveError {
    #[error("invalid resolve request: {0}")]
    InvalidRequest(String),
    #[error("nzbdav submit failed: {0}")]
    Submit(String),
    #[error("nzbdav polling timed out after {0}s")]
    PollTimeout(u64),
    #[error("nzbdav job failed: {0}")]
    JobFailed(String),
    #[error("WebDAV probe failed: {0}")]
    Webdav(String),
    #[error(transparent)]
    Nzbdav(#[from] NzbdavError),
    #[error(transparent)]
    WebdavSource(#[from] WebdavError),
}

impl ResolveError {
    pub fn reason(&self) -> &'static str {
        match self {
            ResolveError::InvalidRequest(_) => "invalid_request",
            ResolveError::Submit(_) | ResolveError::Nzbdav(_) => "nzbdav_submit_failed",
            ResolveError::PollTimeout(_) => "poll_timeout",
            ResolveError::JobFailed(_) => "nzbdav_job_failed",
            ResolveError::Webdav(_) | ResolveError::WebdavSource(_) => "webdav_unreachable",
        }
    }
}

pub async fn resolve_single_peer(req: ResolveRequest) -> Result<ResolveResponse, ResolveError> {
    resolve_single_peer_with_progress(req, |_| {}).await
}

pub async fn resolve_single_peer_with_progress<F>(
    req: ResolveRequest,
    on_progress: F,
) -> Result<ResolveResponse, ResolveError>
where
    F: Fn(ResolveProgressEvent) + Send + Sync + 'static,
{
    let resolve_id = req
        .resolve_id
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| Ulid::new().to_string());
    let peer_id = Ulid::new().to_string();
    let started = Instant::now();
    let progress: ProgressSink = std::sync::Arc::new(on_progress);
    validate_request(&req)?;

    info!(
        event = "resolve.started",
        resolve_id = %resolve_id,
        peer_id = %peer_id,
        outcome = "started",
        selected_nzb_url = %redact_url(&req.nzb_url),
        fallback_count_requested = req
            .fallback_count
            .unwrap_or(req.candidate_peers.len() as u32) as u64,
        candidate_peer_count = req.candidate_peers.len() as u64,
        "resolve started"
    );

    let client = NzbdavClient::new(req.nzbdav.clone())?;
    if let Some(completed) = client.find_completed_by_name(&req.title).await? {
        let stream = stream_for_history(&req.nzbdav.webdav_config(), &completed).await?;
        info!(
            event = "poll.terminal",
            resolve_id = %resolve_id,
            peer_id = %peer_id,
            outcome = "ok",
            final_status = "Completed",
            duration_ms = started.elapsed().as_millis() as u64,
            "existing completed job resolved"
        );
        return Ok(response(
            resolve_id,
            peer_id,
            completed.nzo_id,
            stream,
            Vec::new(),
            &progress,
        ));
    }

    info!(
        event = "submit.attempted",
        resolve_id = %resolve_id,
        peer_id = %peer_id,
        outcome = "started",
        nzb_url_redacted = %redact_url(&req.nzb_url),
        "submitting nzb"
    );
    let nzo_id = client
        .submit_nzb(&req.nzb_url, &req.title)
        .await
        .map_err(|e| ResolveError::Submit(e.to_string()))?;
    info!(
        event = "submit.accepted",
        resolve_id = %resolve_id,
        peer_id = %peer_id,
        outcome = "ok",
        nzbdav_job_id = %nzo_id,
        "nzbdav accepted submit"
    );
    emit_progress(
        &progress,
        ResolveProgressEvent {
            resolve_id: resolve_id.clone(),
            event: "submit.accepted",
            peer_id: Some(peer_id.clone()),
            state: "submitted",
            reason: None,
            payload: serde_json::json!({
                "nzo_id": nzo_id.clone(),
                "validation_state": "single_peer_phase_2",
            }),
        },
    );

    let peer_cohort = select_peer_cohort(&resolve_id, &req).await;
    let timeout = Duration::from_secs(req.download_timeout_secs.unwrap_or(3600).clamp(1, 86_400));
    let poll_interval = Duration::from_secs(req.poll_interval_secs.unwrap_or(1).clamp(1, 60));
    let (peer_cohort, terminal) = tokio::join!(
        submit_peer_cohort(
            resolve_id.clone(),
            client.clone(),
            peer_cohort,
            progress.clone()
        ),
        poll_until_terminal(&client, &nzo_id, &req.title, timeout, poll_interval)
    );
    let terminal = terminal?;
    let stream = stream_for_history(&req.nzbdav.webdav_config(), &terminal).await?;
    let peer_cohort = validate_peer_cohort_content_lengths_once(
        resolve_id.clone(),
        client.clone(),
        req.nzbdav.webdav_config(),
        stream.content_length,
        peer_cohort,
    )
    .await;
    let peer_cohort =
        validate_peer_cohort_byte_samples_once(resolve_id.clone(), &stream, peer_cohort).await;

    info!(
        event = "webdav.probe",
        resolve_id = %resolve_id,
        peer_id = %peer_id,
        outcome = "ok",
        content_length = stream.content_length.unwrap_or(0),
        duration_ms = started.elapsed().as_millis() as u64,
        "WebDAV stream ready"
    );
    emit_progress(
        &progress,
        ResolveProgressEvent {
            resolve_id: resolve_id.clone(),
            event: "webdav.probe",
            peer_id: Some(peer_id.clone()),
            state: "ready",
            reason: None,
            payload: serde_json::json!({
                "content_length": stream.content_length,
                "stream_url": stream.url.clone(),
                "validation_state": "single_peer_phase_2",
            }),
        },
    );

    Ok(response(
        resolve_id,
        peer_id,
        nzo_id,
        stream,
        peer_cohort,
        &progress,
    ))
}

fn validate_request(req: &ResolveRequest) -> Result<(), ResolveError> {
    if req.nzb_url.trim().is_empty() {
        return Err(ResolveError::InvalidRequest("nzb_url is required".into()));
    }
    if req.title.trim().is_empty() {
        return Err(ResolveError::InvalidRequest("title is required".into()));
    }
    Ok(())
}

async fn poll_until_terminal(
    client: &NzbdavClient,
    nzo_id: &str,
    title: &str,
    timeout: Duration,
    poll_interval: Duration,
) -> Result<HistoryEntry, ResolveError> {
    let started = Instant::now();
    loop {
        if let Some(history) = client.get_job_history(nzo_id).await? {
            if history.status == "Completed" {
                return Ok(history);
            }
            if history.status == "Failed" {
                return Err(ResolveError::JobFailed(
                    history
                        .fail_message
                        .unwrap_or_else(|| "download failed".into()),
                ));
            }
        } else if let Some(history) = client.find_terminal_by_name(title).await? {
            if history.status == "Completed" {
                return Ok(history);
            }
            if history.status == "Failed" {
                return Err(ResolveError::JobFailed(
                    history
                        .fail_message
                        .unwrap_or_else(|| "download failed".into()),
                ));
            }
        }

        if let Some(status) = client.get_job_status(nzo_id).await? {
            info!(
                event = "poll.tick",
                peer_id = %nzo_id,
                outcome = "ok",
                nzbdav_status = %status.status,
                percent_complete = %status.percentage.unwrap_or_default(),
                "poll tick"
            );
            let normalized = status.status.to_ascii_lowercase();
            if normalized == "failed" || normalized == "deleted" {
                return Err(ResolveError::JobFailed(status.status));
            }
        }

        if started.elapsed() >= timeout {
            return Err(ResolveError::PollTimeout(timeout.as_secs()));
        }
        tokio::time::sleep(poll_interval).await;
    }
}

async fn stream_for_history(
    webdav: &WebdavConfig,
    history: &HistoryEntry,
) -> Result<VideoStream, ResolveError> {
    if history.storage.trim().is_empty() {
        return Err(ResolveError::Webdav(
            "completed job has no storage path".into(),
        ));
    }
    crate::webdav::find_video_stream_for_storage(webdav, &history.storage)
        .await
        .map_err(ResolveError::from)
}

async fn select_peer_cohort(resolve_id: &str, req: &ResolveRequest) -> Vec<PeerCohortResponse> {
    if req.candidate_peers.is_empty() {
        return Vec::new();
    }

    let client = match Client::builder().user_agent("NZB-DAV Kodi Addon").build() {
        Ok(client) => client,
        Err(error) => {
            info!(
                event = "peer.cohort_build_failed",
                resolve_id = %resolve_id,
                outcome = "error",
                error = %error,
                "candidate cohort lookup disabled"
            );
            return Vec::new();
        }
    };

    let Some(primary_manifest) =
        fetch_article_manifest(&client, resolve_id, "primary", &req.nzb_url).await
    else {
        return Vec::new();
    };

    let candidate_manifests = join_all(req.candidate_peers.iter().enumerate().map(
        |(index, candidate)| {
            let client = client.clone();
            let resolve_id = resolve_id.to_string();
            let nzb_url = candidate.nzb_url.clone();
            async move {
                let manifest = fetch_article_manifest(
                    &client,
                    &resolve_id,
                    &format!("candidate-{index}"),
                    &nzb_url,
                )
                .await;
                (index, manifest)
            }
        },
    ))
    .await;

    let mut manifests = Vec::new();
    let mut manifest_candidates = Vec::new();
    for (index, manifest) in candidate_manifests {
        if let Some(manifest) = manifest {
            manifests.push(manifest);
            manifest_candidates.push(index);
        }
    }
    if manifests.is_empty() {
        info!(
            event = "peer.cohort_selected",
            resolve_id = %resolve_id,
            peer_cohort_size = 0u64,
            admitted_candidate_count = 0u64,
            candidate_peer_count = req.candidate_peers.len() as u64,
            outcome = "ok",
            "candidate cohort selected"
        );
        return Vec::new();
    }

    let limit = req
        .fallback_count
        .unwrap_or(req.candidate_peers.len() as u32) as usize;
    let ranked = rank_article_overlap_candidates(&primary_manifest, &manifests, 0.25, limit);
    let mut cohort = Vec::new();
    for overlap in ranked {
        let candidate_index = manifest_candidates[overlap.candidate_index];
        let candidate = &req.candidate_peers[candidate_index];
        let peer_id = Ulid::new().to_string();
        info!(
            event = "peer.cohort_candidate",
            resolve_id = %resolve_id,
            peer_id = %peer_id,
            outcome = "ok",
            title = %candidate.title,
            nzb_url = %redact_url(&candidate.nzb_url),
            shared_articles = overlap.shared_articles as u64,
            union_articles = overlap.union_articles as u64,
            jaccard = overlap.jaccard,
            state = "admitted",
            validation_state = "article_overlap_phase_3",
            "candidate admitted into peer cohort"
        );
        cohort.push(PeerCohortResponse {
            peer_id,
            title: candidate.title.clone(),
            nzb_url: candidate.nzb_url.clone(),
            state: "admitted",
            validation_state: "article_overlap_phase_3",
            nzo_id: None,
            submit_error: None,
            validation_error: None,
            stream_url: None,
            stream_headers: BTreeMap::new(),
            content_length: None,
            shared_articles: overlap.shared_articles,
            union_articles: overlap.union_articles,
            jaccard: overlap.jaccard,
        });
    }

    info!(
        event = "peer.cohort_selected",
        resolve_id = %resolve_id,
        peer_cohort_size = cohort.len() as u64,
        admitted_candidate_count = cohort.len() as u64,
        candidate_peer_count = req.candidate_peers.len() as u64,
        outcome = "ok",
        "candidate cohort selected"
    );
    cohort
}

async fn submit_peer_cohort(
    resolve_id: String,
    client: NzbdavClient,
    cohort: Vec<PeerCohortResponse>,
    progress: ProgressSink,
) -> Vec<PeerCohortResponse> {
    if cohort.is_empty() {
        return cohort;
    }

    join_all(cohort.into_iter().map(|mut peer| {
        let client = client.clone();
        let resolve_id = resolve_id.clone();
        let progress = progress.clone();
        async move {
            info!(
                event = "peer.submit_attempted",
                resolve_id = %resolve_id,
                peer_id = %peer.peer_id,
                outcome = "started",
                title = %peer.title,
                nzb_url = %redact_url(&peer.nzb_url),
                "submitting candidate peer"
            );
            match client.submit_nzb(&peer.nzb_url, &peer.title).await {
                Ok(nzo_id) => {
                    info!(
                        event = "peer.submit_accepted",
                        resolve_id = %resolve_id,
                        peer_id = %peer.peer_id,
                        outcome = "ok",
                        nzbdav_job_id = %nzo_id,
                        "candidate peer submitted"
                    );
                    peer.state = "submitted";
                    peer.validation_state = "submitted_phase_3";
                    peer.nzo_id = Some(nzo_id);
                    peer.submit_error = None;
                    emit_progress(
                        &progress,
                        ResolveProgressEvent {
                            resolve_id: resolve_id.clone(),
                            event: "submit.accepted",
                            peer_id: Some(peer.peer_id.clone()),
                            state: "submitted",
                            reason: None,
                            payload: serde_json::json!({
                                "nzo_id": peer.nzo_id.clone(),
                                "validation_state": peer.validation_state,
                            }),
                        },
                    );
                }
                Err(error) => {
                    let submit_error = error.to_string();
                    info!(
                        event = "peer.submit_failed",
                        resolve_id = %resolve_id,
                        peer_id = %peer.peer_id,
                        outcome = "error",
                        error = %error,
                        "candidate peer submit failed"
                    );
                    peer.state = "submit_failed";
                    peer.validation_state = "submit_failed_phase_3";
                    peer.submit_error = Some(submit_error.clone());
                    emit_progress(
                        &progress,
                        ResolveProgressEvent {
                            resolve_id: resolve_id.clone(),
                            event: "submit.rejected",
                            peer_id: Some(peer.peer_id.clone()),
                            state: "submit_failed",
                            reason: Some(submit_error),
                            payload: serde_json::json!({
                                "submit_error": peer.submit_error.clone(),
                                "validation_state": peer.validation_state,
                            }),
                        },
                    );
                }
            }
            peer
        }
    }))
    .await
}

async fn validate_peer_cohort_content_lengths_once(
    resolve_id: String,
    client: NzbdavClient,
    webdav: WebdavConfig,
    primary_content_length: Option<u64>,
    cohort: Vec<PeerCohortResponse>,
) -> Vec<PeerCohortResponse> {
    let Some(primary_content_length) = primary_content_length else {
        return cohort
            .into_iter()
            .map(|mut peer| {
                if peer.state == "submitted" {
                    peer.validation_state = "content_length_primary_unknown_phase_3";
                    peer.validation_error = Some("primary content length unavailable".to_string());
                }
                peer
            })
            .collect();
    };

    join_all(cohort.into_iter().map(|mut peer| {
        let client = client.clone();
        let webdav = webdav.clone();
        let resolve_id = resolve_id.clone();
        async move {
            if peer.state != "submitted" {
                return peer;
            }
            let Some(nzo_id) = peer.nzo_id.clone() else {
                return peer;
            };

            info!(
                event = "peer.content_length_probe",
                resolve_id = %resolve_id,
                peer_id = %peer.peer_id,
                outcome = "started",
                nzbdav_job_id = %nzo_id,
                "probing candidate peer content length"
            );

            let history = match client.get_job_history(&nzo_id).await {
                Ok(Some(history)) => history,
                Ok(None) => {
                    peer.validation_state = "download_pending_phase_3";
                    return peer;
                }
                Err(error) => {
                    peer.state = "validation_failed";
                    peer.validation_state = "content_length_probe_failed_phase_3";
                    peer.validation_error = Some(error.to_string());
                    return peer;
                }
            };

            if history.status == "Failed" {
                peer.state = "validation_failed";
                peer.validation_state = "download_failed_phase_3";
                peer.validation_error = Some(
                    history
                        .fail_message
                        .unwrap_or_else(|| "candidate download failed".to_string()),
                );
                return peer;
            }
            if history.status != "Completed" {
                peer.validation_state = "download_pending_phase_3";
                return peer;
            }

            let stream = match stream_for_history(&webdav, &history).await {
                Ok(stream) => stream,
                Err(error) => {
                    peer.state = "validation_failed";
                    peer.validation_state = "content_length_probe_failed_phase_3";
                    peer.validation_error = Some(error.to_string());
                    return peer;
                }
            };

            let Some(candidate_content_length) = stream.content_length else {
                peer.state = "validation_failed";
                peer.validation_state = "content_length_unknown_phase_3";
                peer.validation_error = Some("candidate content length unavailable".to_string());
                return peer;
            };
            peer.content_length = Some(candidate_content_length);

            let delta = candidate_content_length.abs_diff(primary_content_length);
            if delta == CONTENT_LENGTH_EPSILON_BYTES {
                info!(
                    event = "peer.content_length_validated",
                    resolve_id = %resolve_id,
                    peer_id = %peer.peer_id,
                    outcome = "ok",
                    content_length = candidate_content_length,
                    "candidate peer content length matches primary"
                );
                peer.validation_state = "content_length_validated_phase_3";
                peer.stream_url = Some(stream.url);
                peer.stream_headers = stream.headers;
                peer.validation_error = None;
                return peer;
            }

            info!(
                event = "peer.content_length_rejected",
                resolve_id = %resolve_id,
                peer_id = %peer.peer_id,
                outcome = "error",
                primary_content_length = primary_content_length,
                content_length = candidate_content_length,
                delta = delta,
                max_delta = CONTENT_LENGTH_EPSILON_BYTES,
                "candidate peer content length differs from primary"
            );
            peer.state = "rejected";
            peer.validation_state = "content_length_mismatch_phase_3";
            peer.validation_error = Some(format!(
                "content length {} differs from primary {}",
                candidate_content_length, primary_content_length
            ));
            peer
        }
    }))
    .await
}

async fn validate_peer_cohort_byte_samples_once(
    resolve_id: String,
    primary_stream: &VideoStream,
    cohort: Vec<PeerCohortResponse>,
) -> Vec<PeerCohortResponse> {
    let ranges = byte_sample_ranges(primary_stream.content_length.unwrap_or(0));
    if ranges.is_empty() {
        return cohort
            .into_iter()
            .map(|mut peer| {
                if peer.validation_state == "content_length_validated_phase_3" {
                    peer.state = "ready";
                    peer.validation_state = "byte_sample_validated_phase_3";
                }
                peer
            })
            .collect();
    }

    let http = match Client::builder().user_agent("NZB-DAV Kodi Addon").build() {
        Ok(http) => http,
        Err(error) => {
            return cohort
                .into_iter()
                .map(|mut peer| {
                    if peer.validation_state == "content_length_validated_phase_3" {
                        peer.validation_state = "byte_sample_probe_failed_phase_3";
                        peer.validation_error = Some(error.to_string());
                    }
                    peer
                })
                .collect();
        }
    };
    let primary_digests = match fetch_sample_digests(&http, primary_stream, &ranges).await {
        Ok(digests) => digests,
        Err(error) => {
            info!(
                event = "peer.byte_sample_primary_failed",
                resolve_id = %resolve_id,
                outcome = "error",
                error = %error,
                "primary byte-sample probe failed"
            );
            return cohort
                .into_iter()
                .map(|mut peer| {
                    if peer.validation_state == "content_length_validated_phase_3" {
                        peer.validation_state = "byte_sample_primary_probe_failed_phase_3";
                        peer.validation_error = Some(error.to_string());
                    }
                    peer
                })
                .collect();
        }
    };

    join_all(cohort.into_iter().map(|mut peer| {
        let http = http.clone();
        let primary_digests = primary_digests.clone();
        let ranges = ranges.clone();
        let resolve_id = resolve_id.clone();
        async move {
            if peer.validation_state != "content_length_validated_phase_3" {
                return peer;
            }
            let Some(stream_url) = peer.stream_url.clone() else {
                peer.state = "validation_failed";
                peer.validation_state = "byte_sample_probe_failed_phase_3";
                peer.validation_error = Some("candidate stream URL unavailable".to_string());
                return peer;
            };
            let stream = VideoStream {
                path: String::new(),
                url: stream_url,
                headers: peer.stream_headers.clone(),
                content_length: peer.content_length,
            };
            let candidate_digests = match fetch_sample_digests(&http, &stream, &ranges).await {
                Ok(digests) => digests,
                Err(error) => {
                    peer.state = "validation_failed";
                    peer.validation_state = "byte_sample_probe_failed_phase_3";
                    peer.validation_error = Some(error.to_string());
                    return peer;
                }
            };

            if candidate_digests == primary_digests {
                info!(
                    event = "peer.byte_sample_validated",
                    resolve_id = %resolve_id,
                    peer_id = %peer.peer_id,
                    outcome = "ok",
                    sample_count = ranges.len() as u64,
                    "candidate peer byte samples match primary"
                );
                peer.state = "ready";
                peer.validation_state = "byte_sample_validated_phase_3";
                peer.validation_error = None;
                return peer;
            }

            info!(
                event = "peer.byte_sample_rejected",
                resolve_id = %resolve_id,
                peer_id = %peer.peer_id,
                outcome = "error",
                sample_count = ranges.len() as u64,
                "candidate peer byte samples differ from primary"
            );
            peer.state = "rejected";
            peer.validation_state = "byte_sample_mismatch_phase_3";
            peer.validation_error = Some("byte samples differ from primary".to_string());
            peer
        }
    }))
    .await
}

fn byte_sample_ranges(content_length: u64) -> Vec<(u64, u64)> {
    if content_length == 0 {
        return Vec::new();
    }
    let mut ranges = Vec::new();
    for sample_len in BYTE_SAMPLE_HEAD_LENGTHS {
        let end = content_length.min(sample_len).saturating_sub(1);
        let range = (0, end);
        if !ranges.contains(&range) {
            ranges.push(range);
        }
    }
    ranges
}

async fn fetch_sample_digests(
    client: &Client,
    stream: &VideoStream,
    ranges: &[(u64, u64)],
) -> Result<BTreeMap<(u64, u64), Vec<u8>>, ResolveError> {
    let mut digests = BTreeMap::new();
    for &(start, end) in ranges {
        let digest = fetch_range_digest(client, stream, start, end).await?;
        digests.insert((start, end), digest);
    }
    Ok(digests)
}

async fn fetch_range_digest(
    client: &Client,
    stream: &VideoStream,
    start: u64,
    end: u64,
) -> Result<Vec<u8>, ResolveError> {
    let mut request = client
        .get(stream.url.as_str())
        .header("Range", format!("bytes={start}-{end}"))
        .timeout(Duration::from_secs(10));
    for (key, value) in &stream.headers {
        request = request.header(key.as_str(), value.as_str());
    }
    let response = request
        .send()
        .await
        .map_err(|error| ResolveError::Webdav(error.to_string()))?;
    if !response.status().is_success() {
        return Err(ResolveError::Webdav(format!(
            "range probe returned HTTP {}",
            response.status().as_u16()
        )));
    }
    let bytes = response
        .bytes()
        .await
        .map_err(|error| ResolveError::Webdav(error.to_string()))?;
    Ok(Sha256::digest(&bytes).to_vec())
}

async fn fetch_article_manifest(
    client: &Client,
    resolve_id: &str,
    label: &str,
    url: &str,
) -> Option<NzbArticleManifest> {
    let trimmed = url.trim();
    if trimmed.is_empty() {
        return None;
    }

    let response = match client
        .get(trimmed)
        .timeout(Duration::from_secs(15))
        .send()
        .await
    {
        Ok(response) => response,
        Err(error) => {
            info!(
                event = "peer.cohort_fetch_failed",
                resolve_id = %resolve_id,
                outcome = "error",
                label = label,
                nzb_url = %redact_url(trimmed),
                error = %error,
                "candidate NZB fetch failed"
            );
            return None;
        }
    };
    if !response.status().is_success() {
        info!(
            event = "peer.cohort_fetch_failed",
            resolve_id = %resolve_id,
            outcome = "error",
            label = label,
            nzb_url = %redact_url(trimmed),
            status = response.status().as_u16() as u64,
            "candidate NZB fetch returned HTTP error"
        );
        return None;
    }
    if let Some(content_length) = response.content_length() {
        if content_length > MAX_NZB_MANIFEST_BYTES as u64 {
            info!(
                event = "peer.cohort_fetch_failed",
                resolve_id = %resolve_id,
                outcome = "error",
                label = label,
                nzb_url = %redact_url(trimmed),
                content_length = content_length,
                max_bytes = MAX_NZB_MANIFEST_BYTES as u64,
                "candidate NZB exceeds manifest byte limit"
            );
            return None;
        }
    }
    let bytes = match response.bytes().await {
        Ok(bytes) => bytes,
        Err(error) => {
            info!(
                event = "peer.cohort_fetch_failed",
                resolve_id = %resolve_id,
                outcome = "error",
                label = label,
                nzb_url = %redact_url(trimmed),
                error = %error,
                "candidate NZB fetch body read failed"
            );
            return None;
        }
    };
    match extract_article_manifest_limited(&bytes, MAX_NZB_MANIFEST_BYTES) {
        Ok(manifest) => Some(manifest),
        Err(error) => {
            info!(
                event = "peer.cohort_fetch_failed",
                resolve_id = %resolve_id,
                outcome = "error",
                label = label,
                nzb_url = %redact_url(trimmed),
                error = %error,
                "candidate NZB parse failed"
            );
            None
        }
    }
}

fn response(
    resolve_id: String,
    peer_id: String,
    nzo_id: String,
    stream: VideoStream,
    peer_cohort: Vec<PeerCohortResponse>,
    progress: &ProgressSink,
) -> ResolveResponse {
    let stream_url = stream.url;
    let stream_headers = stream.headers;
    let mut peers = vec![PeerResponse {
        peer_id: peer_id.clone(),
        state: "ready",
        validation_state: "single_peer_phase_2",
        nzo_id: Some(nzo_id.clone()),
        stream_url: Some(stream_url.clone()),
        stream_headers: stream_headers.clone(),
        content_length: stream.content_length,
        submit_error: None,
        validation_error: None,
    }];
    peers.extend(peer_cohort.iter().map(|peer| PeerResponse {
        peer_id: peer.peer_id.clone(),
        state: peer.state,
        validation_state: peer.validation_state,
        nzo_id: peer.nzo_id.clone(),
        stream_url: peer.stream_url.clone(),
        stream_headers: peer.stream_headers.clone(),
        content_length: peer.content_length,
        submit_error: peer.submit_error.clone(),
        validation_error: peer.validation_error.clone(),
    }));

    let response = ResolveResponse {
        resolve_id,
        primary_peer_id: peer_id.clone(),
        nzo_id: nzo_id.clone(),
        stream_url,
        stream_headers,
        peer_cohort,
        peers,
    };
    emit_response_progress(progress, &response);
    response
}

fn emit_response_progress(progress: &ProgressSink, response: &ResolveResponse) {
    for peer in response.peers.iter().skip(1) {
        match peer.state {
            "ready" => {
                emit_progress(
                    progress,
                    ResolveProgressEvent {
                        resolve_id: response.resolve_id.clone(),
                        event: "webdav.probe",
                        peer_id: Some(peer.peer_id.clone()),
                        state: "ready",
                        reason: None,
                        payload: serde_json::json!({
                            "content_length": peer.content_length,
                            "stream_url": peer.stream_url.clone(),
                            "validation_state": peer.validation_state,
                        }),
                    },
                );
                if peer.validation_state == "byte_sample_validated_phase_3" {
                    emit_progress(
                        progress,
                        ResolveProgressEvent {
                            resolve_id: response.resolve_id.clone(),
                            event: "peer.admitted",
                            peer_id: Some(peer.peer_id.clone()),
                            state: "ready",
                            reason: None,
                            payload: serde_json::json!({
                                "content_length": peer.content_length,
                                "validation_state": peer.validation_state,
                            }),
                        },
                    );
                }
            }
            "rejected" => {
                emit_progress(
                    progress,
                    ResolveProgressEvent {
                        resolve_id: response.resolve_id.clone(),
                        event: "peer.rejected",
                        peer_id: Some(peer.peer_id.clone()),
                        state: "rejected",
                        reason: Some(peer.validation_state.to_string()),
                        payload: serde_json::json!({
                            "validation_error": peer.validation_error.clone(),
                            "validation_state": peer.validation_state,
                        }),
                    },
                );
            }
            "validation_failed" => {
                emit_progress(
                    progress,
                    ResolveProgressEvent {
                        resolve_id: response.resolve_id.clone(),
                        event: "peer.rejected",
                        peer_id: Some(peer.peer_id.clone()),
                        state: "validation_failed",
                        reason: peer.validation_error.clone(),
                        payload: serde_json::json!({
                            "validation_error": peer.validation_error.clone(),
                            "validation_state": peer.validation_state,
                        }),
                    },
                );
            }
            _ => {}
        }
    }

    emit_progress(
        progress,
        ResolveProgressEvent {
            resolve_id: response.resolve_id.clone(),
            event: "resolve.completed",
            peer_id: Some(response.primary_peer_id.clone()),
            state: "completed",
            reason: None,
            payload: serde_json::json!({
                "peer_count": response.peers.len(),
                "primary_peer_id": response.primary_peer_id.clone(),
            }),
        },
    );
}

fn emit_progress(progress: &ProgressSink, event: ResolveProgressEvent) {
    progress(event);
}

fn redact_url(input: &str) -> String {
    let Ok(mut parsed) = url::Url::parse(input) else {
        return input.to_string();
    };
    let pairs: Vec<(String, String)> = parsed.query_pairs().into_owned().collect();
    if pairs.is_empty() {
        return parsed.to_string();
    }
    parsed.query_pairs_mut().clear();
    {
        let mut qp = parsed.query_pairs_mut();
        for (key, value) in pairs {
            if matches!(
                key.to_ascii_lowercase().as_str(),
                "apikey" | "api_key" | "token" | "password" | "key"
            ) {
                qp.append_pair(&key, "REDACTED");
            } else {
                qp.append_pair(&key, &value);
            }
        }
    }
    parsed.to_string()
}
