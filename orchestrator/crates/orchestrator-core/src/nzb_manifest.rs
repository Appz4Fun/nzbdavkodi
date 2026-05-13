// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! NZB article-ID extraction and overlap scoring for peer validation.

use std::collections::BTreeSet;

use quick_xml::encoding::Decoder;
use quick_xml::events::Event;
use quick_xml::Reader;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NzbArticleManifest {
    pub files: Vec<NzbArticleFile>,
    pub article_ids: BTreeSet<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NzbArticleFile {
    pub subject: String,
    pub article_ids: BTreeSet<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ArticleOverlap {
    pub candidate_index: usize,
    pub shared_articles: usize,
    pub union_articles: usize,
    pub jaccard: f64,
}

#[derive(Debug, thiserror::Error)]
pub enum NzbManifestError {
    #[error("NZB payload exceeds {0} byte limit")]
    TooLarge(usize),
    #[error("NZB XML parse error: {0}")]
    Xml(String),
}

pub fn extract_article_manifest(xml: &[u8]) -> Result<NzbArticleManifest, NzbManifestError> {
    extract_article_manifest_unchecked(xml)
}

pub fn extract_article_manifest_limited(
    xml: &[u8],
    max_bytes: usize,
) -> Result<NzbArticleManifest, NzbManifestError> {
    if xml.len() > max_bytes {
        return Err(NzbManifestError::TooLarge(max_bytes));
    }
    extract_article_manifest_unchecked(xml)
}

fn extract_article_manifest_unchecked(xml: &[u8]) -> Result<NzbArticleManifest, NzbManifestError> {
    let mut reader = Reader::from_reader(xml);
    reader.config_mut().trim_text(true);
    let mut buf = Vec::new();
    let mut files = Vec::new();
    let mut all_article_ids = BTreeSet::new();
    let mut in_file = false;
    let mut in_segment = false;
    let mut subject = String::new();
    let mut article_ids = BTreeSet::new();

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Eof) => break,
            Ok(Event::Start(event)) => match local_name(event.name().as_ref()) {
                "file" => {
                    in_file = true;
                    in_segment = false;
                    subject = subject_attr(&event, reader.decoder())?;
                    article_ids.clear();
                }
                "segment" if in_file => in_segment = true,
                _ => {}
            },
            Ok(Event::Text(text)) => {
                if in_segment {
                    let decoded = text
                        .unescape()
                        .map_err(|e| NzbManifestError::Xml(e.to_string()))?;
                    if let Some(article_id) = normalize_article_id(&decoded) {
                        article_ids.insert(article_id);
                    }
                }
            }
            Ok(Event::End(event)) => match local_name(event.name().as_ref()) {
                "segment" => in_segment = false,
                "file" if in_file => {
                    if !article_ids.is_empty() {
                        all_article_ids.extend(article_ids.iter().cloned());
                        files.push(NzbArticleFile {
                            subject: subject.clone(),
                            article_ids: article_ids.clone(),
                        });
                    }
                    in_file = false;
                    in_segment = false;
                    subject.clear();
                    article_ids.clear();
                }
                _ => {}
            },
            Ok(_) => {}
            Err(e) => return Err(NzbManifestError::Xml(e.to_string())),
        }
        buf.clear();
    }

    Ok(NzbArticleManifest {
        files,
        article_ids: all_article_ids,
    })
}

pub fn rank_article_overlap_candidates(
    primary: &NzbArticleManifest,
    candidates: &[NzbArticleManifest],
    min_jaccard: f64,
    top_k: usize,
) -> Vec<ArticleOverlap> {
    if top_k == 0 || primary.article_ids.is_empty() {
        return Vec::new();
    }
    let min_jaccard = if min_jaccard.is_finite() {
        min_jaccard.clamp(0.0, 1.0)
    } else {
        0.0
    };
    let mut ranked = candidates
        .iter()
        .enumerate()
        .filter_map(|(candidate_index, candidate)| {
            score_article_overlap(
                candidate_index,
                &primary.article_ids,
                &candidate.article_ids,
            )
        })
        .filter(|overlap| overlap.shared_articles > 0 && overlap.jaccard >= min_jaccard)
        .collect::<Vec<_>>();
    ranked.sort_by(|left, right| {
        right
            .jaccard
            .total_cmp(&left.jaccard)
            .then_with(|| right.shared_articles.cmp(&left.shared_articles))
            .then_with(|| left.candidate_index.cmp(&right.candidate_index))
    });
    ranked.truncate(top_k);
    ranked
}

fn score_article_overlap(
    candidate_index: usize,
    primary_ids: &BTreeSet<String>,
    candidate_ids: &BTreeSet<String>,
) -> Option<ArticleOverlap> {
    if candidate_ids.is_empty() {
        return None;
    }
    let shared_articles = primary_ids.intersection(candidate_ids).count();
    let union_articles = primary_ids.len() + candidate_ids.len() - shared_articles;
    if union_articles == 0 {
        return None;
    }
    Some(ArticleOverlap {
        candidate_index,
        shared_articles,
        union_articles,
        jaccard: shared_articles as f64 / union_articles as f64,
    })
}

fn subject_attr(
    event: &quick_xml::events::BytesStart<'_>,
    decoder: Decoder,
) -> Result<String, NzbManifestError> {
    let Some(attr) = event
        .attributes()
        .filter_map(Result::ok)
        .find(|attr| local_name(attr.key.as_ref()) == "subject")
    else {
        return Ok(String::new());
    };
    attr.decode_and_unescape_value(decoder)
        .map(|value| value.into_owned())
        .map_err(|e| NzbManifestError::Xml(e.to_string()))
}

fn normalize_article_id(raw: &str) -> Option<String> {
    let normalized = raw
        .trim()
        .trim_matches('<')
        .trim_matches('>')
        .to_ascii_lowercase();
    if normalized.is_empty() {
        None
    } else {
        Some(normalized)
    }
}

fn local_name(qualified: &[u8]) -> &str {
    let raw = std::str::from_utf8(qualified).unwrap_or("");
    raw.rsplit_once(':').map(|(_, local)| local).unwrap_or(raw)
}
