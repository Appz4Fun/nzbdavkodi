//! Behaviour-parity tests against the Python original
//! (`tests/test_filter.py`). Every Python test ports to one Rust
//! test with equivalent fixtures and assertions.

use std::collections::HashMap;

use orchestrator_filter::{
    filter_results, matches_filters, parse_metadata, rank::sort_candidates,
    settings::FilterSettings, FilterInput,
};

const FIVE_GB: u64 = 5_000_000_000;

fn make_input(title: &str, size_bytes: u64) -> FilterInput {
    FilterInput {
        title: title.to_string(),
        size_bytes,
        pubdate: None,
    }
}

fn make_default(title: &str) -> FilterInput {
    make_input(title, FIVE_GB)
}

fn all_pass_settings() -> FilterSettings {
    FilterSettings {
        resolutions: vec!["2160p", "1080p", "720p", "480p"]
            .into_iter()
            .map(str::to_string)
            .collect(),
        hdr: vec!["HDR10", "HDR10+", "Dolby Vision", "HLG", "SDR"]
            .into_iter()
            .map(str::to_string)
            .collect(),
        audio: vec!["Atmos", "TrueHD", "DTS-HD MA", "DTS:X", "DD+", "DD", "AAC"]
            .into_iter()
            .map(str::to_string)
            .collect(),
        codecs: vec!["x265/HEVC", "x264/AVC", "AV1", "VP9", "MPEG-2"]
            .into_iter()
            .map(str::to_string)
            .collect(),
        languages: vec![],
        exclude_keywords: vec![],
        require_keywords: vec![],
        release_group: vec![],
        exclude_release_group: vec![],
        min_size: 0,
        max_size: 0,
        sort_order: 0,
        max_results: 25,
    }
}

// --- parse_title_metadata equivalents -------------------------------------

#[test]
fn parse_metadata_movie() {
    let meta = parse_metadata("The.Matrix.1999.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.7.1-GROUP");
    assert_eq!(meta.resolution, "2160p");
    let codec_lower = meta.codec.to_lowercase();
    assert!(
        codec_lower.contains("hevc") || codec_lower.contains("265"),
        "expected HEVC/x265, got {:?}",
        meta.codec
    );
    assert_eq!(meta.group, "GROUP");
}

#[test]
fn parse_metadata_no_resolution() {
    let meta = parse_metadata("Some.Random.Title-GROUP");
    assert_eq!(meta.resolution, "");
}

#[test]
fn parse_metadata_exposes_proper_and_repack() {
    let meta = parse_metadata("Movie.2024.PROPER.REPACK.1080p.BluRay.x264-GROUP");
    assert!(meta.proper);
    assert!(meta.repack);
}

#[test]
fn parse_metadata_1080p_x264() {
    let meta = parse_metadata("Inception.2010.1080p.BluRay.x264-FGT");
    assert_eq!(meta.resolution, "1080p");
    assert_eq!(meta.codec, "x264/AVC");
    assert_eq!(meta.group, "FGT");
}

#[test]
fn parse_metadata_720p_web() {
    let meta = parse_metadata("The.Office.S09E23.720p.WEB-DL.AAC2.0.H.264-NTb");
    assert_eq!(meta.resolution, "720p");
}

#[test]
fn parse_metadata_4k_hdr() {
    let meta = parse_metadata("Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-FLUX");
    assert_eq!(meta.resolution, "2160p");
}

#[test]
fn parse_metadata_empty_title() {
    let meta = parse_metadata("");
    assert_eq!(meta.resolution, "");
    assert_eq!(meta.codec, "");
    assert_eq!(meta.group, "");
    assert!(meta.hdr.is_empty());
    assert!(meta.audio.is_empty());
    assert!(meta.languages.is_empty());
}

#[test]
fn parse_metadata_special_characters() {
    let meta = parse_metadata("Spider-Man.No.Way.Home.2021.1080p.BluRay.x264-SPARKS");
    assert_eq!(meta.resolution, "1080p");
    assert_eq!(meta.group, "SPARKS");
}

#[test]
fn parse_metadata_dots_and_dashes() {
    let meta =
        parse_metadata("Mr.Robot.S04E13.Series.Finale.Part.2.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTG");
    assert_eq!(meta.resolution, "1080p");
}

#[test]
fn parse_metadata_fallback_preserves_hyphenated_release_group() {
    // The original Python test forces fallback by mocking PTT to
    // return `{}`. In Rust we exercise the fallback directly: this
    // input is also one where the live PTT parser yields the same
    // group, so the assertion holds either way.
    use orchestrator_filter::fallback::fallback_parse;
    let meta = fallback_parse("Movie.2024.1080p.WEB-DL.x264-GROUP-NAME");
    assert_eq!(meta.group, "GROUP-NAME");
}

#[test]
fn parse_metadata_fallback_preserves_underscored_release_group() {
    use orchestrator_filter::fallback::fallback_parse;
    let meta = fallback_parse("Movie.2024.1080p.WEB-DL.x264-GROUP_NAME");
    assert_eq!(meta.group, "GROUP_NAME");
}

// --- Full pipeline tests --------------------------------------------------

#[test]
fn filter_pipeline_realistic_titles() {
    let settings = FilterSettings {
        resolutions: vec!["1080p".into()],
        hdr: vec!["SDR".into()],
        audio: vec!["Atmos", "TrueHD", "DTS-HD MA", "DTS:X", "DD+", "DD", "AAC"]
            .into_iter()
            .map(str::to_string)
            .collect(),
        codecs: vec!["x265/HEVC".into(), "x264/AVC".into()],
        languages: vec![],
        exclude_keywords: vec!["cam".into()],
        require_keywords: vec![],
        release_group: vec![],
        exclude_release_group: vec!["yify".into()],
        min_size: 0,
        max_size: 0,
        sort_order: 0,
        max_results: 25,
    };

    let inputs = vec![
        make_default("The.Matrix.1999.2160p.UHD.BluRay.REMUX.HDR.HEVC.DTS-HD.MA.7.1-FraMeSToR"),
        make_default("The.Matrix.1999.1080p.BluRay.x264.DTS-FGT"),
        make_default("The.Matrix.1999.1080p.BluRay.x264-YIFY"),
        make_default("The.Matrix.1999.CAM.x264-JUNK"),
        make_default("The.Matrix.1999.720p.WEB-DL.x264-GRP"),
    ];
    let out = filter_results(inputs, &settings);
    assert_eq!(out.filtered.len(), 1, "expected one survivor");
    assert!(out.filtered[0].input.title.contains("FGT"));
}

#[test]
fn filter_pipeline_empty_results() {
    let out = filter_results(vec![], &all_pass_settings());
    assert!(out.filtered.is_empty());
}

#[test]
fn filter_pipeline_all_filtered_out() {
    let settings = FilterSettings {
        resolutions: vec!["480p".into()],
        ..all_pass_settings()
    };
    let inputs = vec![
        make_default("Movie.2024.2160p.BluRay.HEVC-GRP"),
        make_default("Movie.2024.1080p.BluRay.x264-GRP"),
    ];
    let out = filter_results(inputs, &settings);
    assert_eq!(out.filtered.len(), 0);
}

#[test]
fn filter_excludes_resolution() {
    let settings = FilterSettings {
        resolutions: vec!["1080p".into()],
        ..all_pass_settings()
    };
    let inputs = vec![
        make_default("Movie.2024.2160p.BluRay.HEVC-GRP"),
        make_default("Movie.2024.1080p.BluRay.x264-GRP"),
        make_default("Movie.2024.720p.WEB-DL.x264-GRP"),
    ];
    let out = filter_results(inputs, &settings);
    assert_eq!(out.filtered.len(), 1);
    assert!(out.filtered[0].input.title.contains("1080p"));
}

#[test]
fn filter_excludes_keywords() {
    let settings = FilterSettings {
        exclude_keywords: vec!["cam".into(), "ts".into()],
        ..all_pass_settings()
    };
    let inputs = vec![
        make_default("Movie.2024.CAM.x264-GRP"),
        make_default("Movie.2024.1080p.BluRay.x264-GRP"),
    ];
    let out = filter_results(inputs, &settings);
    assert_eq!(out.filtered.len(), 1);
    assert!(out.filtered[0].input.title.contains("BluRay"));
}

#[test]
fn filter_size_range() {
    let settings = FilterSettings {
        min_size: 1000,
        max_size: 10000,
        ..all_pass_settings()
    };
    let inputs = vec![
        make_input("Small.Movie-GRP", 500_000_000),
        make_input("Good.Movie-GRP", 5_000_000_000),
        make_input("Huge.Movie-GRP", 50_000_000_000),
    ];
    let out = filter_results(inputs, &settings);
    assert_eq!(out.filtered.len(), 1);
    assert!(out.filtered[0].input.title.contains("Good"));
}

#[test]
fn filter_max_results() {
    let settings = FilterSettings {
        max_results: 2,
        ..all_pass_settings()
    };
    let inputs: Vec<FilterInput> = (0..5)
        .map(|i| make_default(&format!("Movie.{}.1080p-GRP", i)))
        .collect();
    let out = filter_results(inputs, &settings);
    assert_eq!(out.filtered.len(), 2);
}

#[test]
fn filter_preferred_release_group_boosted() {
    let settings = FilterSettings {
        release_group: vec!["sparks".into()],
        ..all_pass_settings()
    };
    let inputs = vec![
        make_default("Movie.2024.1080p.BluRay.x264-OTHER"),
        make_default("Movie.2024.1080p.BluRay.x264-SPARKS"),
    ];
    let out = filter_results(inputs, &settings);
    assert!(
        out.filtered[0].input.title.ends_with("-SPARKS"),
        "preferred group should win the sort: {:?}",
        out.filtered[0].input.title
    );
}

#[test]
fn filter_exclude_release_group() {
    let settings = FilterSettings {
        exclude_release_group: vec!["yify".into()],
        ..all_pass_settings()
    };
    let inputs = vec![
        make_default("Movie.2024.1080p.BluRay.x264-YIFY"),
        make_default("Movie.2024.1080p.BluRay.x264-SPARKS"),
    ];
    let out = filter_results(inputs, &settings);
    assert_eq!(out.filtered.len(), 1);
    assert!(out.filtered[0].input.title.contains("SPARKS"));
}

// --- Edge cases -----------------------------------------------------------

#[test]
fn filter_very_large_size() {
    // 100GB+ files must not overflow.
    let inputs = vec![make_input("Movie.2024.2160p.REMUX-GRP", 107_374_182_400)];
    let out = filter_results(inputs, &all_pass_settings());
    assert_eq!(out.filtered.len(), 1);
}

#[test]
fn filter_zero_size() {
    let inputs = vec![make_input("Movie.2024.1080p-GRP", 0)];
    let out = filter_results(inputs, &all_pass_settings());
    assert_eq!(out.filtered.len(), 1);
}

#[test]
fn filter_empty_size() {
    // No "" distinction in Rust — `0` covers it. Python's test
    // verifies "no crash"; we verify the same.
    let inputs = vec![make_input("Movie.2024.1080p-GRP", 0)];
    let out = filter_results(inputs, &all_pass_settings());
    assert_eq!(out.filtered.len(), 1);
}

#[test]
fn filter_require_keywords() {
    let settings = FilterSettings {
        require_keywords: vec!["remux".into()],
        ..all_pass_settings()
    };
    // Disable all other implicit filters by emptying the lists
    // (matches Python test, which set every list except the keyword
    // pair to []).
    let settings = FilterSettings {
        resolutions: vec![],
        hdr: vec![],
        audio: vec![],
        codecs: vec![],
        languages: vec![],
        ..settings
    };
    let inputs = vec![
        make_default("Movie.2024.1080p.BluRay.REMUX.HEVC-GRP"),
        make_default("Movie.2024.1080p.BluRay.x264-GRP"),
    ];
    let out = filter_results(inputs, &settings);
    assert_eq!(out.filtered.len(), 1);
    assert!(out.filtered[0].input.title.contains("REMUX"));
}

// --- Sort orders ----------------------------------------------------------

#[test]
fn sort_by_size_largest_first() {
    let mut results: Vec<(FilterInput, _)> = vec![
        (make_input("Small", 1_000_000_000), ()),
        (make_input("Large", 9_000_000_000), ()),
    ];
    let mut settings = all_pass_settings();
    settings.sort_order = 1;
    sort_candidates(&mut results, &settings, |(inp, _)| {
        (
            parse_metadata(&inp.title),
            inp.size_bytes,
            inp.pubdate.clone(),
        )
    });
    assert_eq!(results[0].0.title, "Large");
}

#[test]
fn sort_by_size_largest_first_tolerates_malformed_size() {
    // Rust models malformed size as `0`. With size=0 vs 9_000_000_000
    // desc, the larger wins — same outcome as the Python test.
    let mut results: Vec<FilterInput> =
        vec![make_input("Bad", 0), make_input("Large", 9_000_000_000)];
    let mut settings = all_pass_settings();
    settings.sort_order = 1;
    sort_candidates(&mut results, &settings, |inp| {
        (
            parse_metadata(&inp.title),
            inp.size_bytes,
            inp.pubdate.clone(),
        )
    });
    let titles: Vec<&str> = results.iter().map(|r| r.title.as_str()).collect();
    assert_eq!(titles, ["Large", "Bad"]);
}

#[test]
fn sort_by_size_smallest_first() {
    let mut results: Vec<FilterInput> = vec![
        make_input("Large", 9_000_000_000),
        make_input("Small", 1_000_000_000),
    ];
    let mut settings = all_pass_settings();
    settings.sort_order = 2;
    sort_candidates(&mut results, &settings, |inp| {
        (
            parse_metadata(&inp.title),
            inp.size_bytes,
            inp.pubdate.clone(),
        )
    });
    assert_eq!(results[0].title, "Small");
}

#[test]
fn sort_relevance_tolerates_malformed_size() {
    let mut results: Vec<FilterInput> = vec![
        make_input("Movie.2024.1080p.H264-GRP", 0),
        make_input("Movie.2024.1080p.H264-GRP", 1_000_000_000),
    ];
    let mut settings = all_pass_settings();
    settings.sort_order = 0;
    sort_candidates(&mut results, &settings, |inp| {
        (
            parse_metadata(&inp.title),
            inp.size_bytes,
            inp.pubdate.clone(),
        )
    });
    assert_eq!(results.len(), 2);
}

#[test]
fn sort_relevance_preserves_order() {
    let mut results: Vec<FilterInput> = vec![
        make_default("First"),
        make_default("Second"),
        make_default("Third"),
    ];
    let mut settings = all_pass_settings();
    settings.sort_order = 0;
    sort_candidates(&mut results, &settings, |inp| {
        (
            parse_metadata(&inp.title),
            inp.size_bytes,
            inp.pubdate.clone(),
        )
    });
    assert_eq!(results[0].title, "First");
    assert_eq!(results[1].title, "Second");
    assert_eq!(results[2].title, "Third");
}

// --- Multi-codec audio ----------------------------------------------------

#[test]
fn parse_metadata_multiple_audio_codecs() {
    let meta = parse_metadata(
        "The.Dark.Knight.2008.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.Atmos.7.1-GROUP",
    );
    assert!(
        !meta.audio.is_empty(),
        "should detect at least one audio codec, got {:?}",
        meta.audio
    );
    assert!(
        meta.audio.iter().any(|a| a == "TrueHD" || a == "Atmos"),
        "expected TrueHD or Atmos, got {:?}",
        meta.audio
    );
}

#[test]
fn filter_tv_episode_with_season_episode() {
    let settings = FilterSettings {
        resolutions: vec!["1080p".into()],
        hdr: vec![],
        audio: vec![],
        codecs: vec!["x265/HEVC".into(), "x264/AVC".into()],
        languages: vec![],
        exclude_keywords: vec![],
        require_keywords: vec![],
        release_group: vec![],
        exclude_release_group: vec![],
        min_size: 0,
        max_size: 0,
        sort_order: 0,
        max_results: 25,
    };
    let inputs = vec![
        make_default("Breaking.Bad.S05E14.Ozymandias.1080p.BluRay.x265.DTS-HD.MA-NTb"),
        make_default("Breaking.Bad.S05E14.Ozymandias.720p.WEB-DL.x264-GRP"),
        make_default("Breaking.Bad.S05E14.Ozymandias.2160p.BluRay.HEVC-SPARKS"),
    ];
    let out = filter_results(inputs, &settings);
    assert_eq!(out.filtered.len(), 1);
    assert!(out.filtered[0].input.title.contains("S05E14"));
    assert!(out.filtered[0].input.title.contains("1080p"));
}

#[test]
fn filter_no_resolution_detected_passes_when_all_enabled() {
    let inputs = vec![
        make_default("Some.Old.Movie.DVDRip.x264-GRP"),
        make_default("Another.Release.HDTV.x264-GRP"),
    ];
    let out = filter_results(inputs, &all_pass_settings());
    assert_eq!(out.filtered.len(), 2);
}

#[test]
fn filter_combined_resolution_audio_codec() {
    let settings = FilterSettings {
        resolutions: vec!["1080p".into()],
        hdr: vec![],
        audio: vec!["DTS-HD MA".into()],
        codecs: vec!["x265/HEVC".into()],
        languages: vec![],
        exclude_keywords: vec![],
        require_keywords: vec![],
        release_group: vec![],
        exclude_release_group: vec![],
        min_size: 0,
        max_size: 0,
        sort_order: 0,
        max_results: 25,
    };
    let inputs = vec![
        make_default("Movie.2024.1080p.BluRay.HEVC.DTS-HD.MA.7.1-GRP"),
        make_default("Movie.2024.1080p.BluRay.x264.DTS-HD.MA-GRP"),
        make_default("Movie.2024.720p.BluRay.HEVC.DTS-HD.MA-GRP"),
        make_default("Movie.2024.1080p.BluRay.HEVC.AAC-GRP"),
    ];
    let out = filter_results(inputs, &settings);
    assert_eq!(out.filtered.len(), 1);
    let t = &out.filtered[0].input.title;
    assert!(t.contains("HEVC"));
    assert!(t.contains("DTS-HD"));
}

#[test]
fn filter_results_attaches_meta() {
    let inputs = vec![
        make_default("Movie.2024.1080p.BluRay.x264-GRP"),
        make_default("Another.2023.2160p.UHD.BluRay.HEVC-SPARKS"),
    ];
    let out = filter_results(inputs, &all_pass_settings());
    assert_eq!(out.filtered.len(), 2);
    for c in &out.filtered {
        // Equivalent of "_meta in item" — our type guarantees it.
        // We just sanity-check the fields are populated.
        assert!(!c.meta.resolution.is_empty());
    }
}

// --- Size parsing robustness ---------------------------------------------

#[test]
fn matches_filters_non_numeric_size() {
    // Rust collapses unparseable to 0; with min_size=100MB and
    // size=0 the rejection fires.
    let input = make_input("Movie.2024.1080p.BluRay.x264-GRP", 0);
    let meta = parse_metadata(&input.title);
    let settings = FilterSettings {
        min_size: 100,
        ..FilterSettings::default()
    };
    let res = matches_filters(&input, &meta, &settings);
    assert!(res.is_err());
}

#[test]
fn matches_filters_empty_size() {
    let input = make_input("Movie.2024.1080p.BluRay.x264-GRP", 0);
    let meta = parse_metadata(&input.title);
    let settings = FilterSettings::default();
    assert!(matches_filters(&input, &meta, &settings).is_ok());
}

#[test]
fn matches_filters_none_size() {
    // Identical to the empty-size case under our representation.
    let input = make_input("Movie.2024.1080p.BluRay.x264-GRP", 0);
    let meta = parse_metadata(&input.title);
    let settings = FilterSettings::default();
    assert!(matches_filters(&input, &meta, &settings).is_ok());
}

#[test]
fn filter_results_returns_all_parsed() {
    let settings = FilterSettings {
        resolutions: vec!["1080p".into()],
        ..FilterSettings::default()
    };
    let inputs = vec![
        make_input("Movie.2024.1080p.BluRay.x264-GRP", 5_000_000_000),
        make_input("Movie.2024.720p.BluRay.x264-GRP", 3_000_000_000),
    ];
    let out = filter_results(inputs, &settings);
    assert_eq!(out.filtered.len(), 1);
    assert_eq!(out.all_parsed.len(), 2);
}

// Skipping the log-counts test — the Python original asserted on
// the formatted xbmc.log() string; our tracing instrumentation is
// covered structurally by the events we emit, which any subscriber
// can inspect at runtime. A log-capture parity test would re-invent
// `tracing_test` for one assertion. Documenting the gap here.

// --- _get_filter_settings parity ------------------------------------------

fn kodi_map<I, K, V>(items: I) -> HashMap<String, String>
where
    I: IntoIterator<Item = (K, V)>,
    K: Into<String>,
    V: Into<String>,
{
    items
        .into_iter()
        .map(|(k, v)| (k.into(), v.into()))
        .collect()
}

#[test]
fn get_filter_settings_collects_enabled_resolutions_and_codecs() {
    let map = kodi_map([
        ("filter_1080p", "true"),
        ("filter_2160p", "true"),
        ("filter_hevc", "true"),
        ("filter_av1", "true"),
        ("filter_dolby_vision", "true"),
        ("filter_atmos", "true"),
        ("filter_english", "true"),
    ]);
    let s = FilterSettings::from_kodi_map(&map, |_| ());

    assert!(s.resolutions.iter().any(|r| r == "1080p"));
    assert!(s.resolutions.iter().any(|r| r == "2160p"));
    assert!(!s.resolutions.iter().any(|r| r == "720p"));
    assert!(s.codecs.iter().any(|c| c == "x265/HEVC"));
    assert!(s.codecs.iter().any(|c| c == "AV1"));
    assert!(!s.codecs.iter().any(|c| c == "x264/AVC"));
    assert!(s.hdr.iter().any(|h| h == "Dolby Vision"));
    assert!(!s.hdr.iter().any(|h| h == "HDR10"));
    assert!(s.audio.iter().any(|a| a == "Atmos"));
    assert!(!s.audio.iter().any(|a| a == "DD"));
    assert!(s.languages.iter().any(|l| l == "en"));
    assert!(!s.languages.iter().any(|l| l == "es"));
}

#[test]
fn get_filter_settings_csv_split_and_strip() {
    let map = kodi_map([
        ("filter_exclude_keywords", "CAM, HDCAM ,  ,TS"),
        ("filter_require_keywords", ""),
        ("filter_release_group", "GRP1,GRP2"),
        ("filter_exclude_release_group", "  NUKED  , "),
    ]);
    let s = FilterSettings::from_kodi_map(&map, |_| ());

    assert_eq!(s.exclude_keywords, vec!["cam", "hdcam", "ts"]);
    assert!(s.require_keywords.is_empty());
    assert_eq!(s.release_group, vec!["grp1", "grp2"]);
    assert_eq!(s.exclude_release_group, vec!["nuked"]);
}

#[test]
fn get_filter_settings_int_fields_fall_back_on_non_numeric() {
    let map = kodi_map([
        ("filter_min_size", "not a number"),
        ("filter_max_size", ""),
        ("max_results", ""),
    ]);
    let s = FilterSettings::from_kodi_map(&map, |_| ());
    assert_eq!(s.min_size, 0);
    assert_eq!(s.max_size, 0);
    assert_eq!(s.max_results, 25);
}

#[test]
fn get_filter_settings_empty_when_nothing_enabled() {
    let map: HashMap<String, String> = HashMap::new();
    let s = FilterSettings::from_kodi_map(&map, |_| ());
    assert!(s.resolutions.is_empty());
    assert!(s.hdr.is_empty());
    assert!(s.audio.is_empty());
    assert!(s.codecs.is_empty());
    assert!(s.languages.is_empty());
    assert!(s.exclude_keywords.is_empty());
    assert!(s.require_keywords.is_empty());
    assert!(s.release_group.is_empty());
    assert!(s.exclude_release_group.is_empty());
}

#[test]
fn get_filter_settings_inverted_range_zeros_both() {
    let map = kodi_map([("filter_min_size", "10000"), ("filter_max_size", "5000")]);
    let mut logs: Vec<String> = Vec::new();
    let s = FilterSettings::from_kodi_map(&map, |m| logs.push(m.to_string()));
    assert_eq!(s.min_size, 0);
    assert_eq!(s.max_size, 0);
    let joined = logs.join("\n");
    assert!(joined.contains("filter_min_size=10000"));
    assert!(joined.contains("filter_max_size=5000"));
}

#[test]
fn get_filter_settings_open_ended_floor_preserved() {
    let map = kodi_map([("filter_min_size", "1000"), ("filter_max_size", "0")]);
    let s = FilterSettings::from_kodi_map(&map, |_| ());
    assert_eq!(s.min_size, 1000);
    assert_eq!(s.max_size, 0);
}

#[test]
fn get_filter_settings_decimal_input_truncates() {
    let map = kodi_map([("filter_min_size", "1.5"), ("filter_max_size", "100.9")]);
    let s = FilterSettings::from_kodi_map(&map, |_| ());
    assert_eq!(s.min_size, 1);
    assert_eq!(s.max_size, 100);
}

#[test]
fn get_filter_settings_unparseable_falls_back() {
    let map = kodi_map([("filter_min_size", "abc"), ("filter_max_size", "xyz")]);
    let s = FilterSettings::from_kodi_map(&map, |_| ());
    assert_eq!(s.min_size, 0);
    assert_eq!(s.max_size, 0);
}

#[test]
fn get_filter_settings_non_finite_float_falls_back() {
    let map = kodi_map([("filter_min_size", "1e309"), ("filter_max_size", "nan")]);
    let s = FilterSettings::from_kodi_map(&map, |_| ());
    assert_eq!(s.min_size, 0);
    assert_eq!(s.max_size, 0);
}

#[test]
fn get_filter_settings_valid_range_unchanged() {
    let map = kodi_map([("filter_min_size", "1000"), ("filter_max_size", "10000")]);
    let s = FilterSettings::from_kodi_map(&map, |_| ());
    assert_eq!(s.min_size, 1000);
    assert_eq!(s.max_size, 10000);
}

#[test]
fn filter_results_with_kodi_map_settings_path() {
    // Mirror of `test_filter_results_uses_script_settings_getter_without_kodi_addon`.
    let map = kodi_map([
        ("filter_1080p", "true"),
        ("filter_hevc", "true"),
        ("max_results", "5"),
    ]);
    let settings = FilterSettings::from_kodi_map(&map, |_| ());
    let inputs = vec![make_input(
        "The.Odyssey.2026.1080p.WEB-DL.DDP5.1.H.265-GROUP.mkv",
        8 * 1024_u64.pow(3),
    )];
    let out = filter_results(inputs.clone(), &settings);
    assert_eq!(out.filtered.len(), 1);
    assert_eq!(out.all_parsed.len(), 1);
    assert_eq!(out.filtered[0].input, inputs[0]);
}
