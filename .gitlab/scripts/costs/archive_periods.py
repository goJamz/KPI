#!/usr/bin/env python3
"""
Freeze closed JWCC periods, and render an archive page for each.

Two responsibilities that always run together:

  freeze   A period that has ended is written once to archive/<label>.json —
           its figures and portfolio names as they stood. Never rewritten, so
           a later alias change cannot alter what a closed period reported.

  render   Every frozen period is rendered to public/archive/<label>/index.html
           on every run. `public/` is rebuilt from scratch each time, so an
           archive that were written only once would vanish on the next run.

The split is deliberate: the figures are frozen, the page around them is not.
A styling fix reaches old periods; a naming fix does not reach their numbers.

Freezing happens when a period is found closed with no archive yet, rather than
at the moment it closes. Rollover is a once-a-year event and should not have a
single point of failure — a run that fails or is skipped at the boundary is
picked up by the next one. The cost is that an archive reflects the naming in
effect at first write rather than at the instant of close.

Environment:
  HISTORY_DIR   cost history root (default: cost_history)
  ARCHIVE_DIR   frozen period root (default: archive)
  PUBLIC_DIR    published site root (default: public)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))

import generate_report as gr                                    # noqa: E402
from history_io import update_manifest                          # noqa: E402
from pop import pop_end, pop_label, previous_pop_starts         # noqa: E402

HISTORY_DIR = Path(os.environ.get("HISTORY_DIR", "cost_history"))
ARCHIVE_DIR = Path(os.environ.get("ARCHIVE_DIR", "archive"))
PUBLIC_DIR  = Path(os.environ.get("PUBLIC_DIR",  "public"))


def earliest_week(history: dict) -> date | None:
    """First week any environment recorded, which bounds how far back to look."""
    starts = [s.get("period_start", "") for snaps in history.values() for s in snaps]
    starts = [s for s in starts if s]
    if not starts:
        return None
    try:
        return date.fromisoformat(min(starts))
    except ValueError:
        return None


def freeze_period(history: dict, start: date, end: date) -> dict | None:
    """The period's snapshots, resolved, as a single frozen document."""
    lo, hi = start.isoformat(), end.isoformat()
    envs: dict = {}
    for env, snapshots in history.items():
        inside = [s for s in snapshots if lo <= s.get("period_start", "") <= hi]
        if inside:
            envs[env] = inside
    if not envs:
        return None
    return {
        "label":        pop_label(start),
        "period_start": lo,
        "period_end":   hi,
        "frozen_utc":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environments": envs,
    }


def render_archive(payload: dict, out_root: Path) -> Path:
    """Render one frozen period into its own directory under `out_root`.

    The frozen snapshots are laid out as a history directory and handed to the
    ordinary loaders, so the archive goes through exactly the same code as the
    live page rather than a parallel rendering path that could drift.
    """
    label   = payload["label"]
    out_dir = out_root / label
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        hist_dir = Path(tmp) / "cost_history"
        for env, snapshots in payload["environments"].items():
            env_dir = hist_dir / env
            env_dir.mkdir(parents=True, exist_ok=True)
            for snap in snapshots:
                week = snap.get("period_start", "")
                if week:
                    (env_dir / f"{week}.json").write_text(json.dumps(snap, indent=2))

        previous_pop = gr.RENDER_POP
        gr.RENDER_POP = date.fromisoformat(payload["period_start"])
        gr._HISTORY_CACHE.clear()
        gr._DISPLAY_CACHE.clear()
        try:
            history = gr.load_history(hist_dir)
            reports = gr.normalize_reports(gr.reports_from_history(history))
            html    = gr.build_page(reports, hist_dir, flagged_resources=None,
                                    archive=True)
        finally:
            gr.RENDER_POP = previous_pop
            gr._HISTORY_CACHE.clear()
            gr._DISPLAY_CACHE.clear()

    (out_dir / "index.html").write_text(html, encoding="utf-8")

    # Same per-environment downloads the live page offers, resolved relative to
    # this period's own directory so the links need no special casing.
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for env, snapshots in payload["environments"].items():
        ordered = sorted(snapshots, key=lambda s: s.get("period_start", ""))
        (data_dir / f"{env.lower()}.json").write_text(
            json.dumps(ordered, indent=2), encoding="utf-8")

    return out_dir / "index.html"


def main() -> None:
    history = gr.load_history(HISTORY_DIR)
    if not history:
        print("[INFO] No cost history — nothing to archive.")
        return

    earliest = earliest_week(history)
    if earliest is None:
        print("[INFO] History has no usable dates — nothing to archive.")
        return

    closed  = previous_pop_starts(earliest)
    changed: list = []

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for start in closed:
        label     = pop_label(start)
        snap_file = ARCHIVE_DIR / f"{label}.json"
        if snap_file.exists():
            continue
        payload = freeze_period(history, start, pop_end(start))
        if payload is None:
            print(f"[INFO] Period {label} closed with no recorded history — skipped")
            continue
        snap_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        weeks = sum(len(v) for v in payload["environments"].values())
        print(f"[INFO] Froze {label}: {len(payload['environments'])} environment(s), "
              f"{weeks} week(s)")
        changed.append(snap_file)

    if changed:
        carried, pending = update_manifest(ARCHIVE_DIR, changed)
        detail = f" ({carried} still pending from an earlier attempt)" if carried else ""
        print(f"[INFO] {len(changed)} archive(s) frozen; {pending} awaiting commit{detail}")
    elif closed:
        print(f"[INFO] {len(closed)} closed period(s), all already frozen")
    else:
        print("[INFO] No closed periods yet — the current period is the first")

    archives = sorted(ARCHIVE_DIR.glob("*.json"))
    if not archives:
        return

    out_root = PUBLIC_DIR / "archive"
    for path in archives:
        try:
            payload = json.loads(path.read_text())
        except Exception as e:
            print(f"[WARN] Could not read archive {path}: {e}", file=sys.stderr)
            continue
        written = render_archive(payload, out_root)
        print(f"[INFO] Archive page written: {written.relative_to(PUBLIC_DIR)} "
              f"({payload['period_start']} to {payload['period_end']})")


if __name__ == "__main__":
    main()
