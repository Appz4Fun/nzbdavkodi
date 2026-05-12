use orchestrator_providers::{__test_helpers, caps::parse_caps};
use std::fs;

fn main() {
    let base = "crates/orchestrator-providers/tests/fixtures";
    let hydra = __test_helpers::parse_newznab_items_hydra(
        &fs::read_to_string(format!("{base}/hydra_search_sample.xml")).unwrap(),
    );
    fs::write(
        format!("{base}/hydra_search_sample.golden.json"),
        serde_json::to_string_pretty(&hydra).unwrap() + "\n",
    )
    .unwrap();

    let prowlarr = __test_helpers::parse_newznab_items_prowlarr(
        &fs::read_to_string(format!("{base}/prowlarr_search_sample.xml")).unwrap(),
    );
    fs::write(
        format!("{base}/prowlarr_search_sample.golden.json"),
        serde_json::to_string_pretty(&prowlarr).unwrap() + "\n",
    )
    .unwrap();

    let direct = __test_helpers::parse_newznab_items_direct(
        &fs::read_to_string(format!("{base}/direct_newznab_sample.xml")).unwrap(),
        "NZBgeek",
    );
    fs::write(
        format!("{base}/direct_newznab_sample.golden.json"),
        serde_json::to_string_pretty(&direct).unwrap() + "\n",
    )
    .unwrap();

    let caps = parse_caps(&fs::read_to_string(format!("{base}/caps_sample.xml")).unwrap());
    fs::write(
        format!("{base}/caps_sample.golden.json"),
        serde_json::to_string_pretty(&caps).unwrap() + "\n",
    )
    .unwrap();
    println!("wrote goldens");
}
