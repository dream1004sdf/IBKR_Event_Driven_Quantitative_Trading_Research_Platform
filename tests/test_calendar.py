from datetime import datetime, timezone

from recorder.calendar_utils import due_time

UTC = timezone.utc


def test_close_and_trading_day_horizons_skip_weekend():
    # Friday during the US regular session.
    event = datetime(2026, 6, 26, 15, 0, tzinfo=UTC)
    close = due_time(event, "close")
    one_day = due_time(event, "1d")
    assert close.weekday() == 4
    assert one_day.weekday() == 0
    assert one_day > close


def test_event_timestamp_with_seconds_is_accepted():
    event = datetime(2026, 6, 25, 15, 23, 47, tzinfo=UTC)
    assert due_time(event, "close") > event
