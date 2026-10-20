from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

UTC = timezone.utc
_RELATIVE = re.compile(r"^(\d+)([mh])$")
_TRADING_DAYS = re.compile(r"^(\d+)d$")


@dataclass(frozen=True)
class SessionContext:
    phase: str  # premarket | regular | afterhours | closed
    first_reaction_session: pd.Timestamp
    session_open: datetime
    session_close: datetime


@lru_cache(maxsize=1)
def nyse_calendar():
    import exchange_calendars as xcals

    return xcals.get_calendar("XNYS")


def _as_utc_timestamp(value: datetime) -> "pd.Timestamp":
    import pandas as pd

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return pd.Timestamp(value.astimezone(UTC)).floor("min")


def session_context(event_time: datetime) -> SessionContext:
    cal = nyse_calendar()
    ts = _as_utc_timestamp(event_time)
    if cal.is_open_on_minute(ts):
        session = cal.minute_to_session(ts, direction="none")
        return SessionContext(
            "regular",
            session,
            cal.session_open(session).to_pydatetime(),
            cal.session_close(session).to_pydatetime(),
        )

    next_session = cal.minute_to_session(ts, direction="next")
    next_open = cal.session_open(next_session)
    previous_session = cal.minute_to_session(ts, direction="previous")
    previous_close = cal.session_close(previous_session)

    if ts < next_open and ts.date() == next_session.date():
        phase = "premarket"
        session = next_session
    elif ts > previous_close and ts.date() == previous_session.date():
        phase = "afterhours"
        session = next_session
    else:
        phase = "closed"
        session = next_session

    return SessionContext(
        phase,
        session,
        cal.session_open(session).to_pydatetime(),
        cal.session_close(session).to_pydatetime(),
    )


def _shift_session(session: pd.Timestamp, count: int) -> pd.Timestamp:
    cal = nyse_calendar()
    current = session
    for _ in range(max(0, count)):
        current = cal.next_session(current)
    return current


def evaluation_label(event_time: datetime, horizon: str) -> str:
    horizon = horizon.strip().lower()
    context = session_context(event_time)
    if horizon == "close":
        return "same_session_close" if context.phase in {"premarket", "regular"} else "next_regular_close"
    if horizon == "next_open":
        return "next_regular_open"
    if horizon == "same_session_close":
        return "same_session_close"
    if horizon == "next_regular_close":
        return "next_regular_close"
    days = _TRADING_DAYS.match(horizon)
    if days:
        return f"trading_day_{int(days.group(1))}_close"
    return horizon


def due_time(event_time: datetime, horizon: str) -> datetime:
    horizon = horizon.strip().lower()
    relative = _RELATIVE.match(horizon)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        delta = timedelta(minutes=amount) if unit == "m" else timedelta(hours=amount)
        return event_time.astimezone(UTC) + delta

    cal = nyse_calendar()
    context = session_context(event_time)
    if horizon in {"close", "next_regular_close"}:
        return cal.session_close(context.first_reaction_session).to_pydatetime()
    if horizon == "same_session_close":
        # For an after-hours event a future same-session close does not exist. Returning
        # the next regular close is explicit and is labeled next_regular_close in reports.
        return cal.session_close(context.first_reaction_session).to_pydatetime()
    if horizon == "next_open":
        session = (
            cal.next_session(context.first_reaction_session)
            if context.phase == "regular"
            else context.first_reaction_session
        )
        return cal.session_open(session).to_pydatetime()

    days = _TRADING_DAYS.match(horizon)
    if days:
        n = int(days.group(1))
        # For a premarket/regular event, day 1 is the next session close because "close"
        # already captures the current session. For after-hours/weekend events, the first
        # upcoming session is trading day 1.
        shift = n if context.phase in {"premarket", "regular"} else max(0, n - 1)
        target_session = _shift_session(context.first_reaction_session, shift)
        return cal.session_close(target_session).to_pydatetime()

    raise ValueError(f"Unsupported reaction horizon: {horizon}")


def build_schedule(event_time: datetime, horizons: tuple[str, ...]) -> dict[str, datetime]:
    return {horizon: due_time(event_time, horizon) for horizon in horizons}
