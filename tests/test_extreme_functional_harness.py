import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
TESTS_DIR = REPO_ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import test_extreme_functional as extreme  # noqa: E402


def test_extreme_fault_schedule_is_five_resets_in_first_six_playback_minutes():
    schedule = extreme._generate_fault_schedule(random.Random(1234))

    assert len(schedule) == 5
    assert all(event["fault_type"] == "connection_reset" for event in schedule)

    times = [event["at_seconds"] for event in schedule]
    assert times == [90.0, 150.0, 210.0, 270.0, 330.0]
    assert all(60 <= event["at_seconds"] <= 600 for event in schedule)
    assert all(b - a >= 60 for a, b in zip(times, times[1:]))


def test_extreme_harness_requires_a_standby_backup_stream():
    expected = extreme.EXTREME_REQUIRED_FALLBACKS

    assert extreme.os.environ["FUNCTIONAL_MIN_FALLBACK_CANDIDATES"] == expected
    assert extreme.EXTREME_FILTER_SETTINGS["fallback_streams_max"] == expected


def test_extreme_observe_window_covers_last_fault_plus_recovery_margin(monkeypatch):
    monkeypatch.delenv("EXTREME_OBSERVE_SECONDS", raising=False)
    schedule = [
        {"at_seconds": 60.0, "fault_type": "connection_reset"},
        {"at_seconds": 600.0, "fault_type": "connection_reset"},
    ]

    assert extreme._observe_seconds(schedule) == 690.0


def test_extreme_harness_counts_proxy_fallback_switches(tmp_path):
    kodi_log = tmp_path / "kodi.log"
    kodi_log.write_text(
        "\n".join(
            [
                "NZB-DAV: Switched pass-through source at byte 1 "
                "to fallback nzo_id=one (switch_count=1)",
                "NZB-DAV: Switched pass-through source at byte 2 "
                "to fallback nzo_id=one (switch_count=5)",
            ]
        )
    )

    assert extreme._read_fallback_switch_count(kodi_log) == 5
