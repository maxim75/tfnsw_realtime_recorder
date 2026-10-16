from datetime import datetime, timezone

from recorder.windows import SYDNEY_TZ, window_end, window_label, window_start


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_floors_to_ten_minutes_in_sydney_time():
    # 01:19:59.999Z = 12:19:59.999 AEDT
    start = window_start(utc(2026, 10, 16, 1, 19, 59, 999_999))
    assert start == datetime(2026, 10, 16, 12, 10, tzinfo=SYDNEY_TZ)
    assert window_label(start) == "2026-10-16T12-10+1100"


def test_boundary_belongs_to_new_window():
    start = window_start(utc(2026, 10, 16, 1, 20))
    assert window_label(start) == "2026-10-16T12-20+1100"


def test_window_end_is_ten_minutes_later():
    start = window_start(utc(2026, 10, 16, 1, 15))
    assert window_label(window_end(start)) == "2026-10-16T12-20+1100"


def test_repeated_hour_at_end_of_daylight_saving_has_distinct_labels():
    # 2026-04-05 03:00 AEDT -> 02:00 AEST; 02:10 happens twice.
    first = window_start(utc(2026, 4, 4, 15, 12))
    second = window_start(utc(2026, 4, 4, 16, 12))
    assert window_label(first) == "2026-04-05T02-10+1100"
    assert window_label(second) == "2026-04-05T02-10+1000"


def test_window_end_across_end_of_daylight_saving():
    last_aedt = window_start(utc(2026, 4, 4, 15, 55))  # 02:50 AEDT
    assert window_label(window_end(last_aedt)) == "2026-04-05T02-00+1000"


def test_window_end_across_start_of_daylight_saving():
    # 2026-10-04 02:00 AEST -> 03:00 AEDT
    last_aest = window_start(utc(2026, 10, 3, 15, 55))  # 01:50 AEST
    assert window_label(last_aest) == "2026-10-04T01-50+1000"
    assert window_label(window_end(last_aest)) == "2026-10-04T03-00+1100"
