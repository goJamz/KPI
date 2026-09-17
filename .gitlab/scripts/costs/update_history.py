"""
Azure Cost History Updater

Reads cost_reports/*.json (current pipeline run) and writes one compact weekly
snapshot per environment per week, storing tag values exactly as Azure returned
them:

    cost_history/<environment>/<iso-monday>.json

One file per reporting week, named for the Monday that anchors it. The file
existing is the record that the week has been gathered, which is what lets the
backfill work out what is left to do without parsing everything, and lets the
three environment jobs write concurrently without contending for one file.

Each snapshot stores the week's portfolio-level costs. The report generator
(generate_report.py) aggregates these weekly snapshots into monthly buckets
at render time — no format change is needed here as granularity changes.

Re-running a pipeline for the same week is idempotent: the snapshot replaces
the file for that week rather than accumulating alongside it.

Pipeline order (azure_cost_pages job):
  fetch_history.py  → pull existing history from data branch
  update_history.py → append current week (runs BEFORE generate so monthly
                       totals in the dashboard include the current week)
  generate_report.py → build HTML dashboard using updated history
  persist_history.py → commit updated history back to data branch

Usage:
  python3 update_history.py [--reports-dir DIR] [--history-dir DIR]

Defaults:
  --reports-dir  cost_reports/
  --history-dir  cost_history/
"""

import argparse
import json
import sys
from datetime import date as date_t, datetime, timedelta, timezone
from pathlib import Path

# Shared with the other KPI collectors; see naming.py for why these live in
# common/ rather than being copied per domain.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from history_io import round_costs, update_manifest, write_if_changed  # noqa: E402


def extract_snapshot(report: dict) -> dict:
    """Return a compact weekly snapshot from a full cost report JSON.

    Tag values are stored exactly as Azure returned them. Aliasing and merging
    used to happen here, before persisting, which meant the merged name was all
    that survived and the individual spellings were destroyed at the only layer
    that is permanent. Cost Management never retags historical usage, so a
    misspelling stays in the data for as long as the data exists — worth
    recording rather than quietly folding away.

    generate_report.py canonicalizes at render time instead. That makes the
    merge retroactive: adding an alias later re-merges all existing history with
    no re-backfill, and the raw values remain available for reporting on which
    tags need fixing.
    """
    cp   = report.get("current_period", {})
    comp = report.get("comparison",     {})

    comp_portfolios = [
        p for p in comp.get("portfolios", [])
        if p.get("portfolio", "").lower() not in ("null", "")
    ]

    # Rounded because these are sums of already-rounded values: adding a
    # dozen figures that are each exact to the cent still lands a little off.
    total_cost  = round(sum(p.get("current_cost", 0) for p in comp_portfolios), 2)
    total_prior = round(sum(p.get("prior_cost",   0) for p in comp_portfolios), 2)

    portfolios_out = []
    for p in comp_portfolios:
        name = p.get("portfolio", "")
        if not name or name.lower() == "null":
            continue
        projects_out = [
            {
                "project":    pj["project"],
                "cost":       pj.get("current_cost", pj.get("prior_cost", 0) + (pj.get("change") or 0)),
                "prior_cost": pj.get("prior_cost",   0),
                "change":     pj.get("change"),
                "change_pct": pj.get("change_pct"),
            }
            for pj in p.get("projects", []) if pj.get("project")
        ]
        portfolios_out.append({
            "portfolio":  name,
            "cost":       p.get("current_cost", 0),
            "prior_cost": p.get("prior_cost",   0),
            "change":     p.get("change"),
            "change_pct": p.get("change_pct"),
            "projects":   sorted(projects_out, key=lambda pj: pj["project"].lower()),
        })

    portfolios_out.sort(key=lambda p: p["portfolio"].lower())

    return round_costs({
        "period_start":     cp.get("start", "?"),
        "period_end":       cp.get("end",   "?"),
        "report_generated": report.get("report_generated_utc", ""),
        "total_cost":       total_cost,
        "total_prior_cost": total_prior,
        "portfolios":       portfolios_out,
    })


def _week_anchor(period_start: str) -> str:
    """Return the ISO Monday (YYYY-MM-DD) for the calendar week containing period_start."""
    try:
        d = date_t.fromisoformat(period_start)
        return str(d - timedelta(days=d.weekday()))
    except Exception:
        return period_start


def _tag_values(raw: dict) -> tuple:
    """Split a Resource Graph response into distinct portfolio and project values.

    The response arrives either as a list of row dicts or in the older
    columns/rows tabular form, so both are handled — the same two shapes the
    report assembly copes with.
    """
    portfolios: set = set()
    projects:   set = set()
    rows:       list = []

    data = raw.get("data", [])
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
        for row in rows:
            if row.get("portfolio"):
                portfolios.add(row["portfolio"])
            if row.get("project_tag"):
                projects.add(row["project_tag"])
    elif isinstance(data, dict):
        columns = [c.get("name") for c in data.get("columns", [])]
        for values in data.get("rows", []):
            row = dict(zip(columns, values))
            rows.append(row)
            if row.get("portfolio"):
                portfolios.add(row["portfolio"])
            if row.get("project_tag"):
                projects.add(row["project_tag"])

    return sorted(portfolios), sorted(projects), rows


def write_tag_inventory(reports_dir: Path, history_dir: Path) -> list:
    """Record the tag values seen on live resources this run, one file per week.

    Written to cost_history/<environment>/tags/<iso-monday>.json. Unlike a cost
    snapshot this is a point-in-time observation of the subscription rather than
    anything about the reporting window, so it is keyed by the week the run
    happened. That also means a backfill, which reads the same live inventory
    for every window it processes, records it once rather than 38 times.

    The nested directory keeps these clear of the weekly cost shards, which are
    read with a non-recursive glob.
    """
    inventory_dir = reports_dir / "tag_inventory"
    if not inventory_dir.is_dir():
        return []

    today  = datetime.now(timezone.utc)
    anchor = str(today.date() - timedelta(days=today.date().weekday()))
    changed: list = []

    for path in sorted(inventory_dir.glob("*.json")):
        env = path.stem
        try:
            raw = json.loads(path.read_text())
        except Exception as e:
            print(f"[WARN] Could not parse {path}: {e}", file=sys.stderr)
            continue

        portfolios, projects, rows = _tag_values(raw)
        out_file = history_dir / env / "tags" / f"{anchor}.json"
        payload  = {
            "environment":      env,
            "captured_utc":     today.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "week":             anchor,
            "portfolio_values": portfolios,
            "project_values":   projects,
            "pairs":            rows,
        }
        # captured_utc moves on every call; the values are what matter.
        if write_if_changed(out_file, payload, ignore_keys=("captured_utc",)):
            print(f"[INFO] wrote {env}/tags/{anchor}.json "
                  f"({len(portfolios)} portfolio value(s), {len(projects)} project value(s))")
            changed.append(out_file)

    if changed:
        print(f"[INFO] Recorded tag inventory for {len(changed)} environment(s)")
    return changed


def normalize_existing(history_dir: Path) -> list:
    """Round the shards already on the branch, returning the ones that changed.

    Shards written before the rounding was applied still carry the drift, and
    nothing would otherwise rewrite them — a past week is never re-queried.
    Rather than a one-off migration script, this runs every time: the history is
    already on disk from fetch_history, so it costs a few hundred small reads
    and no API calls at all.

    It is self-limiting. write_if_changed only rewrites a shard whose content
    actually differs, so the first run after this lands rewrites the drifted
    weeks, and every run after that rewrites nothing.
    """
    changed: list = []
    if not history_dir.is_dir():
        return changed

    for env_dir in sorted(d for d in history_dir.iterdir() if d.is_dir()):
        for path in sorted(env_dir.glob("*.json")):
            try:
                snap = json.loads(path.read_text())
            except Exception as e:
                print(f"[WARN] Could not read {path} while normalizing: {e}",
                      file=sys.stderr)
                continue
            if not isinstance(snap, dict):
                continue
            if write_if_changed(path, round_costs(snap)):
                changed.append(path)

    if changed:
        print(f"[INFO] Rounded {len(changed)} existing shard(s) carrying "
              f"unrounded derived values")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports-dir", default="cost_reports")
    parser.add_argument("--history-dir", default="cost_history")
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    history_dir = Path(args.history_dir)
    history_dir.mkdir(parents=True, exist_ok=True)

    report_files = sorted(reports_dir.glob("*.json"))
    if not report_files:
        # Not an error since the backfill became resumable. A run where every
        # week in range was already recorded queries nothing and so writes no
        # reports, and failing there would make a correct run look broken.
        # The page build keeps its own check — generate_report.py refuses to
        # render without reports — so a genuinely missing report still surfaces.
        print(f"[INFO] No cost reports in {reports_dir} — nothing to update.")
        return

    written   = 0
    unchanged = 0
    changed: list = []
    environments: set[str] = set()

    for path in report_files:
        try:
            with open(path) as f:
                report = json.load(f)
        except Exception as e:
            print(f"[WARN] Could not parse {path}: {e}", file=sys.stderr)
            continue

        env      = report.get("environment", "unknown")
        snapshot = extract_snapshot(report)

        period_start = snapshot.get("period_start", "")
        anchor       = _week_anchor(period_start)
        if not anchor or anchor == "?":
            print(f"[WARN] {path} has no usable period_start — skipping", file=sys.stderr)
            continue

        out_file = history_dir / env / f"{anchor}.json"
        if write_if_changed(out_file, snapshot):
            print(f"[INFO] wrote {env}/{anchor}.json "
                  f"({period_start} to {snapshot.get('period_end', '?')})")
            changed.append(out_file)
            written += 1
        else:
            unchanged += 1
        environments.add(env)

    suffix = f", {unchanged} unchanged" if unchanged else ""
    print(f"[INFO] Wrote {written} weekly snapshot(s) across "
          f"{len(environments)} environment(s){suffix}")

    changed += write_tag_inventory(reports_dir, history_dir)
    changed += normalize_existing(history_dir)

    # What still needs committing, for persist_history. Without this every
    # checkpoint would re-send the entire history, most of it untouched.
    #
    # Entries accumulate and are cleared only once a commit succeeds. A file is
    # only listed on the run that changes it, so if a checkpoint's commit fails
    # and the manifest were rewritten from scratch, that batch would be
    # unchanged on disk next time round, drop out of the list, and never reach
    # the branch at all.
    carried, pending_total = update_manifest(history_dir, changed)

    detail = f" ({carried} still pending from an earlier attempt)" if carried else ""
    print(f"[INFO] {len(changed)} file(s) changed this run; "
          f"{pending_total} awaiting commit{detail}")


if __name__ == "__main__":
    main()
