from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from recorder.windows import SYDNEY_TZ, window_end, window_label, window_start

PRAGUE_TZ = ZoneInfo("Europe/Prague")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_floors_to_ten_minutes_in_sydney_time():
    # 01:19:59.999Z = 12:19:59.999 AEDT
    start = window_start(utc(2026, 10, 16, 1, 19, 59, 999_999), SYDNEY_TZ)
    assert start == datetime(2026, 10, 16, 12, 10, tzinfo=SYDNEY_TZ)
    assert window_label(start) == "2026-10-16T12-10+1100"


def test_boundary_belongs_to_new_window():
    start = window_start(utc(2026, 10, 16, 1, 20), SYDNEY_TZ)
    assert window_label(start) == "2026-10-16T12-20+1100"


def test_window_end_is_ten_minutes_later():
    start = window_start(utc(2026, 10, 16, 1, 15), SYDNEY_TZ)
    assert window_label(window_end(start)) == "2026-10-16T12-20+1100"


def test_repeated_hour_at_end_of_daylight_saving_has_distinct_labels():
    # 2026-04-05 03:00 AEDT -> 02:00 AEST; 02:10 happens twice.
    first = window_start(utc(2026, 4, 4, 15, 12), SYDNEY_TZ)
    second = window_start(utc(2026, 4, 4, 16, 12), SYDNEY_TZ)
    assert window_label(first) == "2026-04-05T02-10+1100"
    assert window_label(second) == "2026-04-05T02-10+1000"


def test_window_end_across_end_of_daylight_saving():
    last_aedt = window_start(utc(2026, 4, 4, 15, 55), SYDNEY_TZ)  # 02:50 AEDT
    assert window_label(window_end(last_aedt)) == "2026-04-05T02-00+1000"


def test_window_end_across_start_of_daylight_saving():
    # 2026-10-04 02:00 AEST -> 03:00 AEDT
    last_aest = window_start(utc(2026, 10, 3, 15, 55), SYDNEY_TZ)  # 01:50 AEST
    assert window_label(last_aest) == "2026-10-04T01-50+1000"
    assert window_label(window_end(last_aest)) == "2026-10-04T03-00+1100"


def test_feed_versions():
    from recorder.feeds import KNOWN_FEEDS, V2_FEEDS, feed_version

    assert V2_FEEDS == {"sydneytrains", "metro", "lightrail/innerwest"}
    assert V2_FEEDS <= set(KNOWN_FEEDS)
    assert feed_version("metro") == "v2" and feed_version("buses") == "v1"


def test_prague_floors_and_labels_in_prague_time():
    # 10:19:59Z = 12:19:59 CEST
    start = window_start(utc(2026, 10, 16, 10, 19, 59), PRAGUE_TZ)
    assert start == datetime(2026, 10, 16, 12, 10, tzinfo=PRAGUE_TZ)
    assert window_label(start) == "2026-10-16T12-10+0200"
    assert window_label(window_end(start)) == "2026-10-16T12-20+0200"


def test_prague_repeated_hour_at_end_of_daylight_saving_has_distinct_labels():
    # 2026-10-25 03:00 CEST -> 02:00 CET (01:00Z); 02:10 happens twice.
    first = window_start(utc(2026, 10, 25, 0, 12), PRAGUE_TZ)
    second = window_start(utc(2026, 10, 25, 1, 12), PRAGUE_TZ)
    assert window_label(first) == "2026-10-25T02-10+0200"
    assert window_label(second) == "2026-10-25T02-10+0100"
    last_cest = window_start(utc(2026, 10, 25, 0, 55), PRAGUE_TZ)
    assert window_label(window_end(last_cest)) == "2026-10-25T02-00+0100"


def test_prague_start_of_daylight_saving_skips_the_missing_hour():
    # 2026-03-29 02:00 CET -> 03:00 CEST (01:00Z).
    last_cet = window_start(utc(2026, 3, 29, 0, 55), PRAGUE_TZ)
    assert window_label(last_cet) == "2026-03-29T01-50+0100"
    assert window_label(window_end(last_cet)) == "2026-03-29T03-00+0200"
