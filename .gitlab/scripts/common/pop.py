"""
The JWCC Period of Performance boundary.

Every total labeled "JWCC POP" is scoped by this, so getting it wrong shifts
figures people read. It used to be derived inline as `date(today.year - 1, 12, 1)`
in four places, which is correct for eleven months of the year and wrong for
December: on December 1 a new period starts, but that expression keeps returning
the previous one until January 1. The total silently covers thirteen months, and
then December's spend moves from one period to the other overnight.

The period is assumed to run for one year from its start date. Set
JWCC_POP_START (YYYY-MM-DD) to state the start explicitly rather than inferring
it from the calendar — inference assumes the period always begins on December 1,
and a contract that starts on a different date would shift every total with
nothing saying so.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone


def _add_years(d: date, n: int) -> date:
    """Shift a date by whole years, stepping February 29 back to the 28th."""
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)


def _boundaries() -> list:
    """Explicit period starts from JWCC_POP_START, oldest first.

    Accepts a comma-separated list, because a contract boundary can move. A
    single value behaves exactly as before: it rolls forward a year at a time.
    With several, each names the day a period began, and the last one keeps
    rolling annually so the list does not need extending every year.

    Deriving earlier periods by subtracting years from the current anchor — what
    this did before — silently rewrites history when a boundary moves. Changing
    the anchor to 2026-08-30 would have reported a period of 2025-08-30 to
    2026-08-29 that never existed, and frozen it into the archive.
    """
    raw = os.environ.get("JWCC_POP_START", "").strip()
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(date.fromisoformat(part))
        except ValueError:
            raise SystemExit(f"[ERROR] JWCC_POP_START contains '{part}', which is "
                             f"not a valid YYYY-MM-DD date")
    return sorted(set(out))


def pop_periods(today: date | None = None) -> list:
    """Every (start, end) up to and including the period containing `today`."""
    today = today or datetime.now(timezone.utc).date()
    bounds = _boundaries()

    if not bounds:
        # Default: December 1 to November 30. Being in December means being in
        # the new period, which the original inline expression got wrong.
        start = date(today.year if today.month == 12 else today.year - 1, 12, 1)
        return [(start, _add_years(start, 1) - timedelta(days=1))]

    past = [b for b in bounds if b <= today] or [bounds[0]]
    periods = [(past[i], past[i + 1] - timedelta(days=1))
               for i in range(len(past) - 1)]

    # The most recent stated boundary rolls forward annually from there.
    last = past[-1]
    while _add_years(last, 1) <= today:
        periods.append((last, _add_years(last, 1) - timedelta(days=1)))
        last = _add_years(last, 1)
    periods.append((last, _add_years(last, 1) - timedelta(days=1)))
    return periods


def pop_start(today: date | None = None) -> date:
    """Start of the Period of Performance that contains `today`."""
    return pop_periods(today)[-1][0]


def pop_start_month(today: date | None = None) -> str:
    """Start of the current period as YYYY-MM, for comparing month buckets."""
    return pop_start(today).strftime("%Y-%m")


def pop_end(start: date, today: date | None = None) -> date:
    """Last day of the period beginning on `start`.

    Looked up rather than assumed to be a year: a boundary that moved leaves a
    period shorter than twelve months, and that period is the truth about what
    was reported during it.
    """
    for begin, finish in pop_periods(today):
        if begin == start:
            return finish
    return _add_years(start, 1) - timedelta(days=1)


def pop_label(start: date, today: date | None = None) -> str:
    """Identifier for a period, used as its archive filename.

    A full year reads as the two years it spans. A period cut short by a moved
    boundary says so, both to stay unique and because "2025-2026" would claim a
    twelve-month period that did not happen.
    """
    end = pop_end(start, today)
    if end == _add_years(start, 1) - timedelta(days=1):
        return f"{start.year}-{end.year}"
    return f"{start.strftime('%Y-%m')}_to_{end.strftime('%Y-%m')}"


def previous_pop_starts(earliest: date, today: date | None = None) -> list:
    """Starts of every closed period that overlaps history, oldest first."""
    today = today or datetime.now(timezone.utc).date()
    closed = pop_periods(today)[:-1]
    return [begin for begin, finish in closed if finish >= earliest]
