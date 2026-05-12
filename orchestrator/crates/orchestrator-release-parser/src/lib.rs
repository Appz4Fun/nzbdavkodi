//! Rust port of the vendored Python `parse-torrent-title` library that
//! lives at `plugin.video.nzbdav/resources/lib/ptt/`.
//!
//! Phase 1 of `docs/rust-migration-plan.md` (§10 decision 3 amended:
//! the upfront audit found `torrent-name-parser` 0.12 missing
//! `hdr`/`channels`/`edition`/`upscaled`/`container` and treating
//! `audio`/`language` as single options instead of lists, so the user
//! elected to hand-port the full PTT handler chain).
//!
//! The architecture mirrors the Python source one-to-one:
//!
//!   - [`Parser`] holds an ordered list of [`Handler`]s.
//!   - [`Handler`]s are produced from a `(field, regex, transformer,
//!     options)` quadruple — same shape as
//!     `Parser.add_handler(name, pattern, transformer, options)` in
//!     `parse.py`.
//!   - [`Transformer`]s are the value normalisers from
//!     `transformers.py` (none, integer, first_integer, boolean,
//!     lowercase, uppercase, value, array, uniq_concat, range,
//!     range_x_of_y, year_range, transform_resolution).
//!   - The handler chain runs against the input title and accumulates
//!     a [`Parsed`] dict mirroring the JSON shape the Python parser
//!     emits — pinned by the parity corpus at
//!     `orchestrator/tests/harness/fixtures/ptt_parity_corpus.json`.
//!
//! Phase 1 ships the engine + transformers + the subset of handlers
//! the filter actually consumes (resolution, codec, audio, channels,
//! hdr, languages, quality, edition, proper, repack, year, upscaled,
//! container, group, plus title cleanup and season/episode for the
//! search planner). Anime + adult + niche cleanup handlers from
//! `handlers.py` are deferred — they don't feed filter.py decisions
//! and the parity corpus doesn't exercise them.

pub mod parser;
pub mod transformers;

pub mod handlers;

pub use parser::{Parsed, Parser, Value};
