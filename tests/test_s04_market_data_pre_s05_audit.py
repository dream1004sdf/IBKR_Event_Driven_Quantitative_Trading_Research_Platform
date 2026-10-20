from datetime import UTC, date, datetime

from recorder.s04_market_data_pre_s05_audit import (
    expected_eth_for_window,
    stable_id,
)


def test_expected_eth_normal_session_excludes_rth() -> None:
    session_date = date(2025, 7, 9)
    schedule = {
        session_date: (
            datetime(2025, 7, 9, 13, 30, tzinfo=UTC),
            datetime(2025, 7, 9, 20, 0, tzinfo=UTC),
        )
    }

    expected, market_closed = expected_eth_for_window(
        datetime(2025, 7, 9, 8, 0, tzinfo=UTC),
        datetime(2025, 7, 10, 0, 0, tzinfo=UTC),
        schedule,
    )

    assert len(expected) == 570
    assert not market_closed
    assert datetime(2025, 7, 9, 13, 29, tzinfo=UTC) in expected
    assert datetime(2025, 7, 9, 13, 30, tzinfo=UTC) not in expected
    assert datetime(2025, 7, 9, 20, 0, tzinfo=UTC) in expected


def test_expected_eth_early_close_stops_at_1700_eastern() -> None:
    session_date = date(2025, 11, 28)
    schedule = {
        session_date: (
            datetime(2025, 11, 28, 14, 30, tzinfo=UTC),
            datetime(2025, 11, 28, 18, 0, tzinfo=UTC),
        )
    }

    expected, market_closed = expected_eth_for_window(
        datetime(2025, 11, 28, 9, 0, tzinfo=UTC),
        datetime(2025, 11, 29, 1, 0, tzinfo=UTC),
        schedule,
    )

    assert len(expected) == 570
    assert len(market_closed) == 180
    assert datetime(2025, 11, 28, 21, 59, tzinfo=UTC) in expected
    assert datetime(2025, 11, 28, 22, 0, tzinfo=UTC) in market_closed


def test_stable_id_is_deterministic_and_domain_separated() -> None:
    first = stable_id("release-bundle", "fomc", "2025-07-09")
    second = stable_id("release-bundle", "fomc", "2025-07-09")
    other = stable_id("overlap-group", "fomc", "2025-07-09")

    assert first == second
    assert first != other
    assert first.startswith("release-bundle:")
