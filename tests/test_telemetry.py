from unittest.mock import MagicMock

from resources.lib import telemetry


def test_timed_block_logs_elapsed_milliseconds(monkeypatch):
    clock_values = iter([10.0, 10.125])
    log = MagicMock()

    monkeypatch.setattr(telemetry.time, "monotonic", lambda: next(clock_values))
    monkeypatch.setattr(telemetry.xbmc, "log", log)

    with telemetry.timed_block("search hydra", count=3, error=False):
        pass

    log.assert_called_once_with(
        "NZB-DAV: timing search hydra elapsed_ms=125.0 count=3 error=False",
        telemetry.xbmc.LOGDEBUG,
    )


def test_log_timing_tolerates_logger_failure(monkeypatch):
    monkeypatch.setattr(
        telemetry.xbmc,
        "log",
        MagicMock(side_effect=RuntimeError("Kodi shutting down")),
    )

    telemetry.log_timing("filter", 1.25, results=10)
