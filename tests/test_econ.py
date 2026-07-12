from datetime import datetime, timezone

from app import econ


def test_build_events_dedupes_release_families_and_uses_kst():
    now = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
    cpi_ts = int(datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc).timestamp())
    jobs_ts = int(datetime(2026, 8, 7, 12, 30, tzinfo=timezone.utc).timestamp())
    rows = [
        {"announcement_datetime": cpi_ts, "release": "core_inflation", "event_importance": "high"},
        {"announcement_datetime": cpi_ts, "release": "inflation", "event_importance": "high",
         "release_date_confirmed": True, "source": "BLS"},
        {"announcement_datetime": jobs_ts, "release": "employment", "event_importance": "high"},
        {"announcement_datetime": jobs_ts, "release": "non_farm_payrolls", "event_importance": "high"},
        {"announcement_datetime": cpi_ts, "release": "minor_unknown", "event_importance": "high"},
    ]
    events = econ.build_events(rows, now=now)
    assert [e["title"] for e in events] == ["미국 CPI", "미국 고용보고서"]
    assert events[0]["time"] == "21:30"
    assert events[0]["dd"] == 1
    assert events[0]["confirmed"] is True


def test_build_events_excludes_old_and_far_future_rows():
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    rows = [
        {"announcement_datetime": int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()),
         "release": "policy_rate"},
        {"announcement_datetime": int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()),
         "release": "policy_rate"},
    ]
    assert econ.build_events(rows, now=now) == []
