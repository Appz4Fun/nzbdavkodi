use orchestrator_core::nzb_manifest::{extract_article_manifest, rank_article_overlap_candidates};

fn nzb_xml(files: &[&str]) -> Vec<u8> {
    format!(
        r#"<?xml version="1.0" encoding="utf-8"?>
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
{}
</nzb>"#,
        files.join("\n")
    )
    .into_bytes()
}

fn file(subject: &str, segments: &[(&str, u64)]) -> String {
    let segment_xml = segments
        .iter()
        .enumerate()
        .map(|(index, (msgid, bytes))| {
            format!(
                r#"<segment number="{}" bytes="{}">{}</segment>"#,
                index + 1,
                bytes,
                xml_escape(msgid)
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        r#"<file poster="poster" date="1777937305" subject="{}">
  <groups><group>alt.binaries.test</group></groups>
  <segments>{}</segments>
</file>"#,
        xml_escape(subject),
        segment_xml
    )
}

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

#[test]
fn extracts_sorted_deduplicated_article_ids_per_file() {
    let first = file(
        "Movie.Name.2026.2160p-GROUP.mkv yEnc",
        &[
            ("<B@Example>", 2000),
            ("a@example", 1000),
            ("b@example", 2000),
            ("", 3000),
        ],
    );
    let second = file(
        "Movie.Name.2026.2160p-GROUP.nfo yEnc",
        &[("meta@example", 100)],
    );
    let xml = nzb_xml(&[&first, &second]);

    let manifest = extract_article_manifest(&xml).expect("valid nzb");

    assert_eq!(manifest.files.len(), 2);
    assert_eq!(
        manifest.files[0].subject,
        "Movie.Name.2026.2160p-GROUP.mkv yEnc"
    );
    assert_eq!(
        manifest.files[0]
            .article_ids
            .iter()
            .cloned()
            .collect::<Vec<_>>(),
        vec!["a@example", "b@example"]
    );
    assert_eq!(
        manifest.article_ids.iter().cloned().collect::<Vec<_>>(),
        vec!["a@example", "b@example", "meta@example"]
    );
}

#[test]
fn ranks_candidate_article_overlap_by_jaccard_then_shared_count() {
    let primary = extract_article_manifest(&nzb_xml(&[&file(
        "Primary.mkv yEnc",
        &[("a@id", 1), ("b@id", 1), ("c@id", 1), ("d@id", 1)],
    )]))
    .unwrap();
    let exact = extract_article_manifest(&nzb_xml(&[&file(
        "Exact.mkv yEnc",
        &[("a@id", 1), ("b@id", 1), ("c@id", 1), ("d@id", 1)],
    )]))
    .unwrap();
    let partial = extract_article_manifest(&nzb_xml(&[&file(
        "Partial.mkv yEnc",
        &[("a@id", 1), ("b@id", 1), ("x@id", 1), ("y@id", 1)],
    )]))
    .unwrap();
    let disjoint = extract_article_manifest(&nzb_xml(&[&file(
        "Disjoint.mkv yEnc",
        &[("w@id", 1), ("x@id", 1)],
    )]))
    .unwrap();

    let ranked = rank_article_overlap_candidates(&primary, &[partial, disjoint, exact], 0.30, 3);

    assert_eq!(
        ranked
            .iter()
            .map(|candidate| candidate.candidate_index)
            .collect::<Vec<_>>(),
        vec![2, 0]
    );
    assert_eq!(ranked[0].shared_articles, 4);
    assert_eq!(ranked[0].union_articles, 4);
    assert_eq!(ranked[0].jaccard, 1.0);
    assert_eq!(ranked[1].shared_articles, 2);
    assert_eq!(ranked[1].union_articles, 6);
}

#[test]
fn manifest_extraction_rejects_payloads_over_limit() {
    let xml = nzb_xml(&[&file("Movie.mkv yEnc", &[("a@id", 1)])]);

    let error = orchestrator_core::nzb_manifest::extract_article_manifest_limited(&xml, 8)
        .expect_err("manifest should be rejected before parsing");

    assert_eq!(error.to_string(), "NZB payload exceeds 8 byte limit");
}
