// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! Newznab RSS item parser shared by every provider client.
//!
//! `hydra.py`, `prowlarr.py`, and `direct_indexers.py` all implement the
//! same Newznab item-extraction logic with tiny per-provider tweaks
//! (Hydra reads `<source url=...>` for the fallback indexer name; direct
//! indexers carry a `fallback_indexer` label; Prowlarr does the same as
//! direct). One function, three callers, three [`IndexerNameMode`]s.

use std::collections::BTreeMap;

use quick_xml::events::Event;
use quick_xml::Reader;

use crate::caps::local_name;
use crate::http::age_days_from_pubdate;
use crate::types::{Candidate, ProviderError};

/// How the parser computes the per-row `indexer` value when the Newznab
/// `attr name="indexer"` isn't present. The three Python clients all
/// shake out to this enum.
#[derive(Debug, Clone)]
pub(crate) enum IndexerNameMode {
    /// Use the static label passed in (direct indexers, Prowlarr). The
    /// label is the user-visible name of the indexer the row came from.
    Static(String),
    /// Hydra mode — fall back to `<source>` text, then `<source url>`'s
    /// hostname. Has no static label.
    HydraSource,
    /// Prowlarr mode — same as direct, but with `indexer` populated
    /// first when present. We use `Static` for the fallback case.
    PrefixedStatic { prefix: String, fallback: String },
}

impl IndexerNameMode {
    fn resolve(&self, attr_indexer: &str, source_text: &str, source_url: &str) -> String {
        // 1. Newznab attr `indexer` always wins when present.
        if !attr_indexer.is_empty() {
            return match self {
                IndexerNameMode::PrefixedStatic { prefix, .. } => {
                    format!("{prefix}:{attr_indexer}")
                }
                _ => attr_indexer.to_string(),
            };
        }
        // 2. `<source>$text</source>` next, then `<source url=...>` hostname.
        let source_fallback = if !source_text.is_empty() {
            source_text.to_string()
        } else if !source_url.is_empty() {
            hostname_from_url(source_url)
        } else {
            String::new()
        };
        match self {
            IndexerNameMode::HydraSource => source_fallback,
            IndexerNameMode::Static(label) => {
                if !source_fallback.is_empty() {
                    source_fallback
                } else {
                    label.clone()
                }
            }
            IndexerNameMode::PrefixedStatic { prefix, fallback } => {
                let name = if !source_fallback.is_empty() {
                    source_fallback
                } else {
                    fallback.clone()
                };
                if name.is_empty() {
                    prefix.clone()
                } else {
                    format!("{prefix}:{name}")
                }
            }
        }
    }
}

fn hostname_from_url(url: &str) -> String {
    if !url.contains('/') {
        return url.to_string();
    }
    match url::Url::parse(url) {
        Ok(parsed) => parsed.host_str().unwrap_or("").to_string(),
        Err(_) => String::new(),
    }
}

/// Parse a Newznab/RSS XML response into a flat list of [`Candidate`]s.
///
/// `provider` is the label used in errors only. `indexer_mode` controls
/// the per-row `Candidate.indexer` value.
pub(crate) fn parse_newznab_items(
    provider: &str,
    xml_text: &str,
    indexer_mode: &IndexerNameMode,
) -> Result<Vec<Candidate>, ProviderError> {
    let mut reader = Reader::from_str(xml_text);
    reader.config_mut().trim_text(true);

    // We need to assert the root tag is `<rss>` (Python parsers do).
    // Stream until we see the first Start event and reject anything else.
    let mut buf = Vec::new();
    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(e)) => {
                let raw = e.name();
                let name = local_name(raw.as_ref()).to_string();
                if name != "rss" {
                    return Err(ProviderError::InvalidResponse {
                        provider: provider.to_string(),
                        message: format!(
                            "expected RSS feed, got <{}>",
                            std::str::from_utf8(raw.as_ref()).unwrap_or("?")
                        ),
                    });
                }
                break;
            }
            Ok(Event::Decl(_)) | Ok(Event::Comment(_)) | Ok(Event::Text(_)) => {}
            Ok(Event::Eof) => {
                return Err(ProviderError::InvalidResponse {
                    provider: provider.to_string(),
                    message: "empty XML".into(),
                });
            }
            Ok(_) => {}
            Err(e) => {
                return Err(ProviderError::InvalidResponse {
                    provider: provider.to_string(),
                    message: format!("XML parse error: {e}"),
                });
            }
        }
        buf.clear();
    }

    let mut candidates: Vec<Candidate> = Vec::new();
    buf.clear();

    // Walk top-level. We only collect <item> children of <channel>.
    // quick-xml event stream gives us depth implicitly via Start/End pairs.
    let mut path: Vec<String> = Vec::new();
    let mut current_item: Option<ItemInProgress> = None;

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Eof) => break,
            Ok(Event::Start(e)) => {
                let name = local_name(e.name().as_ref()).to_string();
                path.push(name.clone());

                if name == "item"
                    && path.len() >= 2
                    && path[path.len() - 2] == "channel"
                    && current_item.is_none()
                {
                    current_item = Some(ItemInProgress::default());
                }
                if let Some(item) = current_item.as_mut() {
                    match name.as_str() {
                        "title" => item.next_text_target = Some(TextTarget::Title),
                        "link" => item.next_text_target = Some(TextTarget::Link),
                        "pubDate" => item.next_text_target = Some(TextTarget::PubDate),
                        "guid" => item.next_text_target = Some(TextTarget::Guid),
                        "source" => {
                            item.next_text_target = Some(TextTarget::SourceText);
                            for attr in e.attributes().with_checks(false).flatten() {
                                if attr.key.as_ref().eq_ignore_ascii_case(b"url") {
                                    let v = attr.unescape_value().unwrap_or_default();
                                    item.source_url = v.to_string();
                                }
                            }
                        }
                        _ => {
                            item.next_text_target = None;
                        }
                    }
                }
            }
            Ok(Event::Empty(e)) => {
                let name = local_name(e.name().as_ref()).to_string();
                if let Some(item) = current_item.as_mut() {
                    match name.as_str() {
                        "enclosure" => {
                            for attr in e.attributes().with_checks(false).flatten() {
                                let key = attr.key.as_ref().to_ascii_lowercase();
                                let value = attr.unescape_value().unwrap_or_default().to_string();
                                if key == b"length" {
                                    item.enclosure_length = value;
                                } else if key == b"url" {
                                    item.enclosure_url = value;
                                }
                            }
                        }
                        "attr" => parse_newznab_attr(item, &e.attributes().collect_attrs()),
                        "source" => {
                            for attr in e.attributes().with_checks(false).flatten() {
                                if attr.key.as_ref().eq_ignore_ascii_case(b"url") {
                                    let v = attr.unescape_value().unwrap_or_default();
                                    item.source_url = v.to_string();
                                }
                            }
                        }
                        _ => {}
                    }
                }
            }
            Ok(Event::Text(t)) => {
                if let Some(item) = current_item.as_mut() {
                    if let Some(target) = item.next_text_target.clone() {
                        let value = t.unescape().unwrap_or_default().into_owned();
                        match target {
                            TextTarget::Title => item.title.push_str(&value),
                            TextTarget::Link => item.link.push_str(&value),
                            TextTarget::PubDate => item.pubdate.push_str(&value),
                            TextTarget::Guid => item.guid.push_str(&value),
                            TextTarget::SourceText => item.source_text.push_str(&value),
                        }
                    }
                }
            }
            Ok(Event::CData(t)) => {
                if let Some(item) = current_item.as_mut() {
                    if let Some(target) = item.next_text_target.clone() {
                        let value = std::str::from_utf8(t.as_ref()).unwrap_or("").to_string();
                        match target {
                            TextTarget::Title => item.title.push_str(&value),
                            TextTarget::Link => item.link.push_str(&value),
                            TextTarget::PubDate => item.pubdate.push_str(&value),
                            TextTarget::Guid => item.guid.push_str(&value),
                            TextTarget::SourceText => item.source_text.push_str(&value),
                        }
                    }
                }
            }
            Ok(Event::End(_)) => {
                let popped = path.pop();
                if let Some(name) = popped {
                    if name == "item" && current_item.is_some() {
                        let item = current_item.take().expect("just checked is_some");
                        candidates.push(finalise(item, indexer_mode));
                    } else if let Some(item) = current_item.as_mut() {
                        item.next_text_target = None;
                    }
                }
            }
            Ok(_) => {}
            Err(e) => {
                return Err(ProviderError::InvalidResponse {
                    provider: provider.to_string(),
                    message: format!("XML parse error: {e}"),
                });
            }
        }
        buf.clear();
    }

    Ok(candidates)
}

#[derive(Default)]
struct ItemInProgress {
    title: String,
    link: String,
    pubdate: String,
    guid: String,
    source_text: String,
    source_url: String,
    enclosure_url: String,
    enclosure_length: String,
    attr_size: String,
    attr_indexer: String,
    attr_categories: Vec<u32>,
    attrs: BTreeMap<String, String>,
    next_text_target: Option<TextTarget>,
}

#[derive(Debug, Clone)]
enum TextTarget {
    Title,
    Link,
    PubDate,
    Guid,
    SourceText,
}

fn parse_newznab_attr(item: &mut ItemInProgress, attrs: &[(Vec<u8>, String)]) {
    let mut name = String::new();
    let mut value = String::new();
    for (k, v) in attrs {
        let key = std::str::from_utf8(k).unwrap_or("").to_ascii_lowercase();
        if key == "name" {
            name = v.clone();
        } else if key == "value" {
            value = v.clone();
        }
    }
    if name.is_empty() {
        return;
    }
    match name.as_str() {
        "size" => item.attr_size = value.clone(),
        "indexer" | "source" | "hydraIndexerName" if item.attr_indexer.is_empty() => {
            item.attr_indexer = value.clone();
        }
        "category" => {
            if let Ok(id) = value.parse::<u32>() {
                item.attr_categories.push(id);
            }
        }
        _ => {}
    }
    item.attrs.insert(name, value);
}

fn finalise(item: ItemInProgress, indexer_mode: &IndexerNameMode) -> Candidate {
    // Size precedence: attr.size > enclosure.length > 0.
    let size_str = if !item.attr_size.is_empty() {
        item.attr_size
    } else {
        item.enclosure_length
    };
    let size: u64 = size_str.parse().unwrap_or(0);

    // Link precedence: <link> > enclosure.url.
    let nzb_url = if !item.link.is_empty() {
        item.link
    } else {
        item.enclosure_url
    };

    let age_days = if !item.pubdate.is_empty() {
        age_days_from_pubdate(&item.pubdate)
    } else {
        None
    };

    let indexer = indexer_mode.resolve(&item.attr_indexer, &item.source_text, &item.source_url);

    let extra = serde_json::Value::Object(
        item.attrs
            .into_iter()
            .map(|(k, v)| (k, serde_json::Value::String(v)))
            .collect(),
    );

    Candidate {
        nzb_url,
        indexer,
        title: item.title,
        size,
        age_days,
        pubdate: if item.pubdate.is_empty() {
            None
        } else {
            Some(item.pubdate)
        },
        guid: if item.guid.is_empty() {
            None
        } else {
            Some(item.guid)
        },
        categories: item.attr_categories,
        extra,
    }
}

// quick-xml's Attributes iterator doesn't expose a collect-as-Vec helper
// shape that's convenient for parse_newznab_attr above (we need both
// owned key and value); this trait fills that gap.
trait CollectAttrs<'a> {
    fn collect_attrs(self) -> Vec<(Vec<u8>, String)>;
}

impl<'a> CollectAttrs<'a> for quick_xml::events::attributes::Attributes<'a> {
    fn collect_attrs(mut self) -> Vec<(Vec<u8>, String)> {
        self.with_checks(false)
            .flatten()
            .map(|a| {
                let key = a.key.as_ref().to_vec();
                let value = a.unescape_value().unwrap_or_default().to_string();
                (key, value)
            })
            .collect()
    }
}
