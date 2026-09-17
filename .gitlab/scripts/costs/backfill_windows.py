"""
Backfill reporting window generator.

Emits one window per line on stdout:

    <current_start> <current_end> <prior_start> <prior_end>

Windows are whole ISO weeks (Monday through Sunday). That matters for two
reasons: a backfilled week becomes indistinguishable from one produced by the
weekly scheduled run, and the week's Monday can be used directly as the
week's identifier without having to derive it from an arbitrary span.

Only complete weeks are emitted — the in-progress week is never included,
because its cost data is still accumulating.

Windows are emitted newest first, so an interrupted run has covered the most
recent (and most useful) weeks.

Weeks already recorded for this environment are skipped. A shard's filename is
the Monday that identifies its week, so working out what is left to do is a set
difference over filenames — no parsing, and no need to read any cost data. A
week with no spend still has a shard (written deliberately, so an environment
that did not exist yet is recorded as covered rather than retried forever), so
resume converges instead of retrying empty weeks on every run.

Environment variables — all optional:
  BACKFILL_START        Earliest date to cover. Default: Dec 1 of the prior
                        year, the JWCC POP start. Rounded forward to the first
                        Monday on or after this date so no window reaches back
                        past it.
  BACKFILL_END          Latest date to cover. Default: the most recently
                        completed week. Rounded back to the last Sunday on or
                        before it, and never allowed past the last complete
                        week. Together with the forward rounding of the start
                        date this keeps every window inside the requested
                        range.
  BACKFILL_MAX_WINDOWS  Stop after this many windows — applied after the
                        already-recorded weeks are removed, so a smoke test
                        gets N windows of real work. Used to try a backfill
                        change without waiting on a full run.
  BACKFILL_FORCE        Set to 1/true/yes to re-query weeks that already have
                        a shard. Combined with BACKFILL_START/BACKFILL_END this
                        re-gathers a specific range that turned out to be wrong.
  ENVIRONMENT           Which environment's shards to check for resume. Set by
                        the job matrix.
  HISTORY_DIR           Where the fetched shards are (default: cost_history).

Diagnostics go to stderr so stdout stays parseable.

Exit codes:
  0  windows were emitted
  1  the requested range contains no complete week — a configuration mistake
  3  the range is complete: every week already has a shard, nothing to do
"""

# Keeps the `X | None` annotations below parseable on older interpreters.
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Shared so the backfill range and the page's POP totals cannot disagree
# about where the period starts.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from pop import pop_start  # noqa: E402


def iso_monday(d: date) -> date:
    """The Monday of the ISO week containing d."""
    return d - timedelta(days=d.weekday())


def parse_env_date(name: str) -> date | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        sys.exit(f"[ERROR] {name}='{raw}' is not a valid YYYY-MM-DD date")


def parse_env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def recorded_weeks(history_dir: Path, environment: str) -> set:
    """Mondays already covered for this environment.

    The `tags/` inventory directory alongside the shards is not picked up: the
    glob is deliberately non-recursive.
    """
    env_dir = history_dir / environment
    if not environment or not env_dir.is_dir():
        return set()
    return {path.stem for path in env_dir.glob("*.json")}


def parse_env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"[ERROR] {name}='{raw}' is not a valid integer")
    if value < 1:
        sys.exit(f"[ERROR] {name}='{raw}' must be 1 or greater")
    return value


def main() -> None:
    today = datetime.now(timezone.utc).date()

    # Sunday of the most recently completed Monday-Sunday week.
    last_complete_end = today - timedelta(days=today.weekday() + 1)

    requested_start = parse_env_date("BACKFILL_START") or pop_start(today)
    requested_end   = parse_env_date("BACKFILL_END")
    max_windows     = parse_env_int("BACKFILL_MAX_WINDOWS")

    # Round the start forward to a Monday so no window covers days before the
    # requested start date.
    first_monday = iso_monday(requested_start)
    if first_monday < requested_start:
        first_monday += timedelta(days=7)
        skipped = (first_monday - requested_start).days
        print(f"[INFO] {requested_start} is not a Monday — starting at {first_monday} "
              f"and leaving {skipped} day(s) before it uncovered.", file=sys.stderr)

    if requested_end is None:
        end = last_complete_end
    else:
        # Round back to the last Sunday on or before the requested end, so the
        # generated windows stay inside [BACKFILL_START, BACKFILL_END] rather
        # than spilling into the week after it. Mirrors the forward rounding
        # applied to the start date.
        end = requested_end - timedelta(days=(requested_end.weekday() + 1) % 7)
        if end < requested_end:
            print(f"[INFO] {requested_end} is not a Sunday — ending at {end} so no "
                  f"window reaches past it.", file=sys.stderr)
        if end > last_complete_end:
            print(f"[INFO] BACKFILL_END rounds to {end}, which is not a complete "
                  f"week yet — clamping to {last_complete_end}.", file=sys.stderr)
            end = last_complete_end

    if end < first_monday + timedelta(days=6):
        print(f"[ERROR] No complete week between {first_monday} and {end} — "
              f"check BACKFILL_START / BACKFILL_END.", file=sys.stderr)
        sys.exit(1)

    windows = []
    monday = iso_monday(end)
    while monday >= first_monday:
        current_start = monday
        current_end   = monday + timedelta(days=6)
        prior_end     = current_start - timedelta(days=1)
        prior_start   = prior_end - timedelta(days=6)
        windows.append((current_start, current_end, prior_start, prior_end))
        monday -= timedelta(days=7)

    in_range = len(windows)

    environment = os.environ.get("ENVIRONMENT", "").strip()
    history_dir = Path(os.environ.get("HISTORY_DIR", "cost_history"))
    force       = parse_env_flag("BACKFILL_FORCE")
    already     = recorded_weeks(history_dir, environment)

    if force:
        if already:
            print(f"[INFO] BACKFILL_FORCE set — re-querying all {in_range} window(s) "
                  f"in range, including {len(already)} already recorded.", file=sys.stderr)
    else:
        windows = [w for w in windows if str(w[0]) not in already]
        skipped = in_range - len(windows)
        print(f"[INFO] Resume: {in_range} window(s) in range, {skipped} already "
              f"recorded for '{environment or 'unknown'}', {len(windows)} to query.",
              file=sys.stderr)

    if not windows:
        print(f"[INFO] Every week in range is already recorded — nothing to do. "
              f"Set BACKFILL_FORCE=1 to re-query anyway.", file=sys.stderr)
        sys.exit(3)

    remaining = len(windows)
    if max_windows is not None and max_windows < remaining:
        windows = windows[:max_windows]
        scope = "window(s) in range" if force else "remaining window(s)"
        print(f"[INFO] BACKFILL_MAX_WINDOWS={max_windows} — emitting {max_windows} "
              f"of {remaining} {scope}.", file=sys.stderr)

    print(f"[INFO] {len(windows)} window(s) to query: "
          f"{windows[-1][0]} through {windows[0][1]}", file=sys.stderr)

    for current_start, current_end, prior_start, prior_end in windows:
        print(current_start, current_end, prior_start, prior_end)


if __name__ == "__main__":
    main()
