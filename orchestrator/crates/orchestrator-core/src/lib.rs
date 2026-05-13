// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! Core service logic for the Rust migration.
//!
//! Phase 2 owns the nzbdav submit/poll/WebDAV probe path and returns a
//! single ready peer. Phase 3 starts the peer-validation foundation with
//! NZB article-ID extraction and candidate overlap scoring.

pub mod nzb_manifest;
pub mod nzbdav;
pub mod resolve;
pub mod webdav;
