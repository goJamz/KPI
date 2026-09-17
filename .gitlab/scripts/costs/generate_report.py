"""
Azure Cost Report — HTML Dashboard Generator

Reads all cost_reports/*.json files (one per subscription/environment) and
generates a single public/index.html file suitable for GitLab Pages.

No third-party dependencies — uses Python standard library only.
"""

import json
import os
import re
import sys
from html import escape
from pathlib import Path
from datetime import datetime, timezone, date as date_t, timedelta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def render_flagged_resources_panel(flagged):
    sections = [
        ("underutilized_vms", "Underutilized VMs (avg CPU < 20%)",
         ["environment", "name", "resourceGroup", "avg_cpu_30d"]),
        ("orphaned_disks", "Orphaned Disks (>30 days unattached)",
         ["environment", "name", "resourceGroup", "diskSizeGB", "properties_timeCreated"]),
        ("idle_dbs", "Idle PostgreSQL DBs (no connections)",
         ["environment", "name", "resourceGroup", "type", "avg_active_connections_30d"]),
    ]
    status = flagged.get("_status", {})

    def get_path(obj, path):
        value = obj
        for part in path.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return "-"
        return value

    parts = [
        '<section class="flagged-resources">',
        "  <h2>Flagged Resources</h2>",
        "  <p class='flagged-note'>Idle, orphaned, and underutilized resources detected outside the cost rollup.</p>",
    ]
    for key, title, fields in sections:
        resources = flagged.get(key, [])
        parts.append(f"  <h3>{escape(title)} ({len(resources)})</h3>")

        # Environments where this scan did not complete. Reporting "None
        # detected" over a scan that never ran would read as an all-clear.
        did_not_run = sorted(env for env, scans in status.items()
                             if scans.get(key) not in ("ok", None))
        if not status or len(did_not_run) == len(status):
            parts.append("  <p class='empty-state'>Not collected — no data for "
                         "any environment.</p>")
            continue
        if did_not_run:
            parts.append("  <p class='empty-state'>Not collected for "
                         f"{escape(', '.join(fmt_env(e) for e in did_not_run))} — "
                         "figures below cover the remaining environments only.</p>")

        if not resources:
            parts.append("  <p class='empty-state'>None detected.</p>")
            continue
        parts.append("  <table class='detail-table flagged-table'>")
        parts.append("    <thead><tr>" + "".join(f"<th>{escape(field)}</th>" for field in fields) + "</tr></thead>")
        parts.append("    <tbody>")
        for resource in resources:
            row = []
            for field in fields:
                value = get_path(resource, field)
                if isinstance(value, float):
                    value = round(value, 2)
                row.append(f"<td>{escape(str(value))}</td>")
            parts.append("      <tr>" + "".join(row) + "</tr>")
        parts.append("    </tbody>")
        parts.append("  </table>")
    parts.append("</section>")
    return "\n".join(parts)


def _coerce_flagged_items(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("data"), list):
        return raw["data"]
    return []


RESOURCE_SCANS = ("underutilized_vms", "orphaned_disks", "idle_dbs")


def load_flagged_resources(base_dir: Path, expected_environments=None) -> dict:
    """Findings from the resource collector, one report per environment.

    Reports used to be written to fixed filenames with no environment in them
    (`cost_reports/flagged_resources/az_orphaned_disks.json`). All three matrix
    jobs wrote those same paths, and the page job downloads every job's
    artifacts into one workspace — so they overwrote each other and the panel
    showed a single subscription's resources as though they were everything.
    One file per environment removes the collision, and each finding carries the
    environment it came from.

    Returns {scan_key: [items]} plus a "_status" entry recording, per
    environment, whether each scan actually ran. Absence has to be
    distinguishable from emptiness, or a collector that died renders as
    "None detected" and reads as an all-clear.

    expected_environments is what the cost collector reported, and is the only
    way to notice an environment whose resource report never arrived at all.
    Without it, one environment reporting three orphaned disks looks exactly the
    same whether the other two were scanned and clean or never ran — the missing
    ones are simply absent from the status map, so nothing flags them.
    """
    flagged: dict = {key: [] for key in RESOURCE_SCANS}
    status: dict = {}

    reports_dir = base_dir / "resource_reports"
    for path in sorted(reports_dir.glob("*.json")) if reports_dir.is_dir() else []:
        try:
            report = json.loads(path.read_text())
        except Exception as e:
            print(f"[WARN] Could not read {path}: {e}", file=sys.stderr)
            status[path.stem] = {key: "unreadable" for key in RESOURCE_SCANS}
            continue

        env = report.get("environment", path.stem)
        scans = report.get("scans", {})
        status[env] = {key: scans.get(key, {}).get("status", "missing")
                       for key in RESOURCE_SCANS}

        for key in RESOURCE_SCANS:
            for item in _coerce_flagged_items(scans.get(key, {}).get("items", [])):
                if isinstance(item, dict):
                    flagged[key].append({"environment": fmt_env(env), **item})

    # An environment the cost collector reported but the resource collector did
    # not is a missing report, not an empty one.
    for env in sorted(expected_environments or ()):
        status.setdefault(env, {key: "no report" for key in RESOURCE_SCANS})

    flagged["_status"] = status

    # Say what was loaded. The point of one report per environment is that the
    # panel now covers all of them; without a line here a run gives no evidence
    # of that either way, and a collector silently missing from the artifacts
    # would look exactly like a collector that found nothing.
    reported = sorted(e for e, scans in status.items()
                      if any(v == "ok" for v in scans.values()))
    counts = ", ".join(f"{key}={len(flagged[key])}" for key in RESOURCE_SCANS)
    if reported:
        print(f"[INFO] Loaded resource findings from {len(reported)} environment(s): "
              f"{', '.join(reported)} ({counts})")
    else:
        print("[INFO] No resource findings loaded — the collector did not report")

    for env, scans in sorted(status.items()):
        failed = [k for k, v in scans.items() if v != "ok"]
        if failed:
            print(f"[WARN] {env}: no data for {', '.join(failed)}", file=sys.stderr)

    return flagged


def load_pipeline_metrics_report(base_dir: Path) -> dict | None:
    reports_dir = base_dir / "pipeline_reports"
    if not reports_dir.is_dir():
        print("[INFO] No pipeline metrics report this run — GitLab CI will show as not collected")
        return None

    for path in sorted(reports_dir.glob("*.json")):
        try:
            report = json.loads(path.read_text())
        except Exception as e:
            print(f"[WARN] Could not read {path}: {e}", file=sys.stderr)
            continue
        print(f"[INFO] Loaded pipeline metrics report from {path.name}")
        return report

    print("[INFO] pipeline_reports/ exists but contained no readable JSON files")
    return None


SYSTEM_LABELS = {"0": "Expedition-0"}

ENV_ORDER = {"dev": 0, "test": 1, "prod": 2}

# ---------------------------------------------------------------------------
# Tag-value naming (shared)
# ---------------------------------------------------------------------------
# Folding, aliases and shared-project identification live in
# .gitlab/scripts/common/naming.py so the page, the email and any future KPI
# collector cannot disagree about how many portfolios exist. Add new alias
# entries there, not here.
#
# These scripts are invoked by path rather than installed, so the shared
# directory is put on sys.path here. That keeps each script runnable on its own;
# pyrightconfig.json tells static analysis where to find it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from pop import pop_label, pop_periods, pop_start, pop_start_month  # noqa: E402
from naming import (  # noqa: E402
    PORTFOLIO_ALIAS_BY_KEY,
    PORTFOLIO_ALIASES,
    PROJECT_ALIAS_BY_KEY,
    PROJECT_ALIASES,
    SHARED_PROJECTS,
    fold_name,
    is_shared_project,
    normalize_portfolio,
    normalize_project,
    pick_display_name,
)

# The cluster panel. Imported, not reimplemented: render_cluster.py owns every
# figure about the clusters, so the strip below and the cluster page cannot
# disagree with each other. The dependency runs one way only — that module takes
# the page CSS as a parameter rather than importing it back.
#
# Guarded because the cluster collector is allow_failure and the cost page is
# the primary content. A broken import here must cost the strip, not the page.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cluster"))
try:
    from render_cluster import (  # noqa: E402
        cluster_summary,
        env_key,
        load_reports as load_cluster_reports,
        render_cluster_page,
        render_cluster_strip,
    )
    CLUSTER_PANEL_AVAILABLE = True
except Exception as _cluster_import_error:      # pragma: no cover
    print(f"[WARN] Cluster panel unavailable ({_cluster_import_error}) — the "
          "page will render without it", file=sys.stderr)
    CLUSTER_PANEL_AVAILABLE = False


def _resolve_display_names(snapshots: list) -> tuple:
    """Map folded key -> display name for portfolios and projects, across ALL weeks.

    This has to be global rather than per-snapshot. Resolving separately per
    week would let the same portfolio be titled `PEO C3N` in one week and
    `peo c3n` in another, and since the history loaders key on the displayed
    name, that would split the portfolio right back apart.
    """
    portfolio_variants: dict = {}
    project_variants:   dict = {}

    def note(store: dict, raw: str) -> None:
        if not raw:
            return
        store.setdefault(fold_name(raw), {})
        store[fold_name(raw)][raw] = store[fold_name(raw)].get(raw, 0) + 1

    for snap in snapshots:
        for p in snap.get("portfolios", []):
            note(portfolio_variants, p.get("portfolio", "") or "")
            for pj in p.get("projects", []):
                note(project_variants, pj.get("project", "") or "")

    def resolve(store: dict, alias_by_key: dict) -> dict:
        return {
            key: alias_by_key.get(key) or pick_display_name(variants)
            for key, variants in store.items()
        }

    return (resolve(portfolio_variants, PORTFOLIO_ALIAS_BY_KEY),
            resolve(project_variants,   PROJECT_ALIAS_BY_KEY))


def _merge_project_list(projects: list, period_key: str, display_names: dict | None = None) -> list:
    """Normalize project names within a portfolio entry and merge cost entries for collisions."""
    merged: dict = {}
    for pj in projects:
        raw = pj.get("project", "")
        if not raw:
            continue
        name    = (display_names or {}).get(fold_name(raw)) or normalize_project(raw)
        pj_copy = dict(pj)
        pj_copy["project"] = name
        if name not in merged:
            merged[name] = pj_copy
        else:
            ep = merged[name]
            if period_key == "current_period":
                ep["cost"] = round(ep.get("cost", 0) + pj_copy.get("cost", 0), 2)
            else:
                ep_cur = ep.get("prior_cost", 0) + (ep.get("change") or 0)
                pj_cur = pj_copy.get("prior_cost", 0) + (pj_copy.get("change") or 0)
                ep["prior_cost"] = round(ep.get("prior_cost", 0) + pj_copy.get("prior_cost", 0), 2)
                new_cur = ep_cur + pj_cur
                new_pri = ep["prior_cost"]
                ep["change"]     = round(new_cur - new_pri, 2)
                ep["change_pct"] = (round((new_cur - new_pri) / new_pri * 100, 1)
                                    if new_pri else None)
    return list(merged.values())


def _merge_period_portfolios(portfolios: list, period_key: str) -> list:
    """Merge portfolio entries that share the same name after normalization.

    After aliasing, multiple distinct Azure tag values can map to the same
    canonical name within a single subscription. A plain dict comprehension
    would silently discard all but the last entry; this function sums costs
    and unions project lists instead.
    """
    merged: dict = {}
    for p in portfolios:
        name = p["portfolio"]
        if name not in merged:
            merged[name] = dict(p)
            merged[name]["projects"] = list(p.get("projects", []))
            continue

        m = merged[name]
        if period_key == "current_period":
            m["total_cost"] = round(m.get("total_cost", 0) + p.get("total_cost", 0), 2)
        else:
            m["current_cost"] = round(m.get("current_cost", 0) + p.get("current_cost", 0), 2)
            m["prior_cost"]   = round(m.get("prior_cost",   0) + p.get("prior_cost",   0), 2)
            cur = m["current_cost"]
            pri = m["prior_cost"]
            m["change"]      = round(cur - pri, 2)
            m["change_pct"]  = (round((cur - pri) / pri * 100, 1) if pri else None)

        # Merge project lists, summing costs for any project that appears in both entries
        proj_map = {pj["project"]: dict(pj) for pj in m["projects"]}
        for pj in p.get("projects", []):
            pj_name = pj["project"]
            if pj_name not in proj_map:
                proj_map[pj_name] = dict(pj)
            else:
                ep = proj_map[pj_name]
                if period_key == "current_period":
                    ep["cost"] = round(ep.get("cost", 0) + pj.get("cost", 0), 2)
                else:
                    # Reconstruct current cost as prior + change before summing
                    ep_cur = ep.get("prior_cost", 0) + (ep.get("change") or 0)
                    pj_cur = pj.get("prior_cost", 0) + (pj.get("change") or 0)
                    ep["prior_cost"]  = round(ep.get("prior_cost", 0) + pj.get("prior_cost", 0), 2)
                    new_cur           = ep_cur + pj_cur
                    new_pri           = ep["prior_cost"]
                    ep["change"]      = round(new_cur - new_pri, 2)
                    ep["change_pct"]  = (round((new_cur - new_pri) / new_pri * 100, 1)
                                         if new_pri else None)
        m["projects"] = list(proj_map.values())

    return list(merged.values())


def normalize_reports(reports: list, portfolio_names: dict | None = None,
                      project_names: dict | None = None) -> list:
    """Normalize portfolio and project names, then merge any entries sharing a canonical name.

    The display maps should be the ones resolved from history, so that this
    week's figures and the monthly columns beside them agree on how a portfolio
    is spelled. History has seen every week's spellings and this week's report
    only one, so resolving the report independently could pick a different
    spelling and split the portfolio across the table.

    Report values not present in history — a portfolio appearing for the first
    time — fall back to resolving among the reports themselves.
    """
    periods = [r.get(k, {}) for r in reports for k in ("current_period", "comparison")]
    own_portfolios, own_projects = _resolve_display_names(periods)
    portfolio_names = {**own_portfolios, **(portfolio_names or {})}
    project_names   = {**own_projects,   **(project_names   or {})}

    for r in reports:
        for period_key in ("current_period", "comparison"):
            period     = r.get(period_key, {})
            portfolios = period.get("portfolios", [])
            for p in portfolios:
                raw = p["portfolio"]
                p["portfolio"] = portfolio_names.get(fold_name(raw)) or normalize_portfolio(raw)
                p["projects"]  = _merge_project_list(p.get("projects", []), period_key,
                                                     project_names)
            period["portfolios"] = _merge_period_portfolios(portfolios, period_key)
    return reports


def get_expedition(report):
    env = report.get("environment", "")
    parts = env.split("-")
    return parts[1] if len(parts) >= 2 else "unknown"


def fmt_env(env: str) -> str:
    """Strip expedition-X- prefix and capitalize: expedition-0-dev → Dev"""
    parts = env.split("-")
    return parts[-1].capitalize() if parts else env


def fmt_count(value) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return "0"


def slug(name: str) -> str:
    """URL-safe ID slug: 'Infrastructure and Platforms' → 'infrastructure-platforms'"""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "portfolio"


def sort_reports_by_env(reports):
    """Sort reports in Dev → Test → Prod order."""
    return sorted(
        reports,
        key=lambda r: ENV_ORDER.get(r.get("environment", "").split("-")[-1], 99)
    )


def group_by_system(reports):
    groups = {}
    order = []
    for r in reports:
        exp = get_expedition(r)
        label = SYSTEM_LABELS.get(exp, f"{exp}")
        if label not in groups:
            groups[label] = []
            order.append(label)
        groups[label].append(r)
    return [(label, sort_reports_by_env(groups[label])) for label in order]


def fmt_cost(value):
    return f"${value:,.2f}"


def fmt_rate(value) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}%"


def fmt_month(month_str: str) -> str:
    """Convert 'YYYY-MM' to 'Mon YYYY': '2026-02' → 'Feb 2026'."""
    _months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    try:
        y, m = month_str.split("-")
        return f"{_months[int(m) - 1]} {y}"
    except Exception:
        return month_str


def change_cell(change, change_pct):
    """Color-coded <td> for a week-over-week change value.

    None  → no prior-period data (—)
    0     → data exists, nothing changed
    other → colored arrow + amount + 3dp percentage
    """
    if change is None:
        return '<td class="neutral" title="No prior-period data available">—</td>'
    if change == 0:
        return '<td class="neutral">No change</td>'
    arrow = "▲" if change > 0 else "▼"
    css   = "increase" if change > 0 else "decrease"
    label = "Increase" if change > 0 else "Decrease"
    pct   = f" ({change_pct:+.3f}%)" if change_pct is not None else ""
    title = f"{label} of {fmt_cost(abs(change))} vs prior week"
    return f'<td class="{css}" title="{title}">{arrow} {fmt_cost(abs(change))}{pct}</td>'


def cost_cell(cost):
    """<td> for a raw cost value; muted when zero to signal no activity."""
    if cost == 0:
        return '<td class="cost muted" title="No cost recorded this period">$0.00</td>'
    return f'<td class="cost">{fmt_cost(cost)}</td>'


def load_reports(reports_dir: Path, portfolio_names: dict | None = None,
                 project_names: dict | None = None):
    reports = []
    for path in sorted(reports_dir.glob("*.json")):
        try:
            with open(path) as f:
                reports.append(json.load(f))
        except Exception as e:
            print(f"[WARN] Could not parse {path}: {e}", file=sys.stderr)
    return normalize_reports(reports, portfolio_names, project_names)


def _week_anchor(period_start: str) -> str:
    """Return the ISO Monday (YYYY-MM-DD) for the calendar week containing period_start.

    Used as the deduplication key across history snapshots. Multiple pipeline
    runs within the same Mon–Sun week all share the same anchor, so only the
    most recent snapshot for that week is kept.
    """
    try:
        d = date_t.fromisoformat(period_start)
        return str(d - timedelta(days=d.weekday()))
    except Exception:
        return period_start


def _merge_snapshot_portfolios(snapshot: dict, portfolio_names: dict | None = None,
                               project_names: dict | None = None) -> dict:
    """Merge a snapshot's portfolios by canonical name.

    A snapshot can carry several raw tag spellings of the same portfolio —
    "ifrastructure & platforms" alongside "infrastructure & platforms". Merging
    them here, in the one place every history loader reads through, keeps those
    loaders correct without each having to handle the collision itself. Several
    of them assign rather than accumulate and would otherwise silently keep only
    the last spelling.

    Project lists are merged by canonical project name rather than concatenated,
    so a project appearing under two spellings is counted once. That matters
    because the shared-cost allocation weights each portfolio by its project
    count, and a double-counted project shifts spend between portfolios.

    Raw spellings and their costs are kept on each entry under "variants" so the
    information is still available to report on; nothing merged away is lost.

    A snapshot that already holds one entry per canonical name passes through
    unchanged, so this is a no-op on history written before the split.

    portfolio_names and project_names map a folded key to the spelling to
    display, resolved once across the whole history by _resolve_display_names.
    Without them each name falls back to its alias-map form, which is correct
    for a single snapshot but not stable across weeks.
    """
    portfolio_names = portfolio_names or {}
    project_names   = project_names   or {}
    merged: dict = {}

    for p in snapshot.get("portfolios", []):
        raw  = p.get("portfolio", "") or ""
        name = portfolio_names.get(fold_name(raw)) or normalize_portfolio(raw)
        entry = merged.get(name)
        if entry is None:
            entry = merged[name] = {
                "portfolio":  name,
                "cost":       0.0,
                "prior_cost": 0.0,
                "projects":   {},
                "variants":   {},
            }

        cost       = p.get("cost", 0)       or 0
        prior_cost = p.get("prior_cost", 0) or 0
        entry["cost"]       += cost
        entry["prior_cost"] += prior_cost
        if raw:
            entry["variants"][raw] = entry["variants"].get(raw, 0) + cost

        for pj in p.get("projects", []):
            pj_raw  = pj.get("project", "") or ""
            pj_name = project_names.get(fold_name(pj_raw)) or normalize_project(pj_raw)
            if not pj_name:
                continue
            pj_entry = entry["projects"].setdefault(
                pj_name, {"project": pj_name, "cost": 0.0, "prior_cost": 0.0}
            )
            pj_entry["cost"]       += pj.get("cost", 0)       or 0
            pj_entry["prior_cost"] += pj.get("prior_cost", 0) or 0

    def _with_change(d: dict) -> dict:
        prior = d["prior_cost"]
        d["change"]     = d["cost"] - prior
        d["change_pct"] = ((d["cost"] - prior) / prior * 100) if prior else None
        return d

    portfolios = []
    for entry in merged.values():
        entry["projects"] = sorted(
            (_with_change(pj) for pj in entry["projects"].values()),
            key=lambda pj: pj["project"].lower(),
        )
        entry["variants"] = sorted(
            ({"portfolio": k, "cost": v} for k, v in entry["variants"].items()),
            key=lambda v: v["portfolio"].lower(),
        )
        portfolios.append(_with_change(entry))

    out = dict(snapshot)
    out["portfolios"] = sorted(portfolios, key=lambda p: p["portfolio"].lower())
    return out


# Set once per render from what the collectors reported. Empty when Azure
# returned a real per-portfolio breakdown, so the marker never appears unless
# the figures actually are derived.
FORECAST_EST_MARK = ""

# First month each environment recorded any cost data, so months before it can
# be shown as absent rather than as zero. Set once per render alongside the
# marker above.
ENV_DATA_START: dict = {}

# Weeks missing from inside an environment's recorded range. Set once per
# render; empty when history is contiguous, so the notice never appears unless
# there is a real hole.
COVERAGE_GAPS: dict = {}

# Period-to-date spend per environment, counted by week start rather than by
# whole months. Set once per render alongside the values above.
ENV_POP_TOTALS: dict = {}

# The period being rendered. None means the one containing today, which is what
# the live page wants. An archive render points it at a closed period instead.
# Calling pop_start() directly would always resolve to today's period, so every
# JWCC-scoped figure would silently describe the wrong year.
RENDER_POP = None


def render_pop_start():
    """Start of the period being rendered."""
    return RENDER_POP or pop_start()


def partial_month_mark(month: str) -> str:
    """Marker for a month the period only partly covers.

    A period beginning on the 30th makes its first month two days long. The
    column is arithmetically right and reads as a collapse in spend beside a
    full month — the same trap as rendering a never-reported month as $0.00.
    Saying so costs a superscript.
    """
    start = render_pop_start()
    if start.day == 1 or month != start.strftime("%Y-%m"):
        return ""
    return (f'<sup class="partial-month" title="Period of performance began '
            f'{start.isoformat()}, so this column covers part of the month only">'
            f'&#8224;</sup>')


def render_pop_month() -> str:
    """Start of the period being rendered, as YYYY-MM."""
    return render_pop_start().strftime("%Y-%m")


def render_pop_label() -> str:
    """Identifier for the period being rendered."""
    return pop_label(render_pop_start())


# Cached because the seven loaders below each walk the whole history; without
# it a year of sharded history is re-read and re-merged seven times per render.
_HISTORY_CACHE: dict = {}
_DISPLAY_CACHE: dict = {}


def load_history(history_dir: Path) -> dict:
    """All weekly snapshots on disk, grouped by environment label.

    Reads the sharded layout, one file per environment per week:

        cost_history/<environment>/<iso-monday>.json

    The older single-array-per-environment files are gone from the branch, and
    with them the reader that merged the two layouts.

    Only one snapshot is kept per ISO week per environment, the one with the
    latest period_start. A pipeline run partway through a week produces a
    rolling seven-day window that overlaps the previous run's by up to six
    days, and summing both would double-count those days in the monthly totals.

    Snapshots come back sorted by period_start and already merged by canonical
    portfolio name. Treat the result as read-only; it is shared between callers.
    """
    key = str(history_dir.resolve())
    if key in _HISTORY_CACHE:
        return _HISTORY_CACHE[key]

    if not history_dir.is_dir():
        _HISTORY_CACHE[key] = {}
        _DISPLAY_CACHE[key] = ({}, {})
        return {}

    # env name -> week anchor -> snapshot
    by_env: dict = {}

    def record(env: str, snap: dict) -> None:
        ps = snap.get("period_start", "")
        if not ps:
            return
        by_env.setdefault(env, {})[_week_anchor(ps)] = snap

    for env_dir in sorted(d for d in history_dir.iterdir() if d.is_dir()):
        for path in sorted(env_dir.glob("*.json")):
            try:
                snap = json.loads(path.read_text())
            except Exception as e:
                print(f"[WARN] Could not read history {path}: {e}", file=sys.stderr)
                continue
            # Tolerate a shard written as a single-entry array.
            if isinstance(snap, list):
                for entry in snap:
                    if isinstance(entry, dict):
                        record(env_dir.name, entry)
            elif isinstance(snap, dict):
                record(env_dir.name, snap)

    # Display names are resolved across every environment at once. A portfolio
    # spelled properly in one subscription and lowercase in another is still one
    # portfolio, and the cross-environment tables key on the displayed name.
    every_snapshot = [snap for weeks in by_env.values() for snap in weeks.values()]
    portfolio_names, project_names = _resolve_display_names(every_snapshot)

    result = {
        fmt_env(env): [
            _merge_snapshot_portfolios(snap, portfolio_names, project_names)
            for snap in sorted(weeks.values(), key=lambda s: s["period_start"])
        ]
        for env, weeks in by_env.items()
    }
    _DISPLAY_CACHE[key] = (portfolio_names, project_names)
    _HISTORY_CACHE[key] = result
    return result


def history_display_names(history_dir: Path) -> tuple:
    """The folded-key to display-name maps that load_history resolved."""
    load_history(history_dir)
    return _DISPLAY_CACHE.get(str(history_dir.resolve()), ({}, {}))


def months_in_period(start_month: str, end_month: str) -> list:
    """Every month from start to end inclusive, including ones with no data.

    Generated rather than taken from the history keys, so a month in which
    nothing was recorded anywhere still gets a column. Dropping it would hide
    the gap instead of showing it, which is the same failure 4.2.2 addresses one
    level down.
    """
    months = []
    year, month = int(start_month[:4]), int(start_month[5:7])
    while f"{year:04d}-{month:02d}" <= end_month:
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def environment_data_start(history_dir: Path) -> dict:
    """First month in which each environment recorded any cost data at all.

    Prod's history runs back to Dec 2025, but 29 of those weeks contain no
    portfolios — the subscription was not billing yet. Rendering those months as
    $0.00 asserts a measurement that was never taken, and reads as "we looked
    and it cost nothing" rather than "there was nothing to look at".

    The signal is an empty portfolio list rather than a zero total: a week that
    genuinely cost nothing still records the portfolios it looked at.
    """
    starts: dict = {}
    for env, snapshots in load_history(history_dir).items():
        for snap in snapshots:            # already ordered by period_start
            if snap.get("portfolios"):
                starts[env] = snap.get("period_start", "")[:7]
                break
    return starts


def archived_periods(archive_dir: Path) -> list:
    """Closed periods that have been frozen, newest first.

    Read from the frozen snapshots rather than from the rendered pages, so the
    list reflects what was archived even if a render failed.
    """
    periods = []
    for path in sorted(archive_dir.glob("*.json")) if archive_dir.is_dir() else []:
        try:
            payload = json.loads(path.read_text())
        except Exception as e:
            print(f"[WARN] Could not read archive {path}: {e}", file=sys.stderr)
            continue
        label = payload.get("label") or path.stem
        periods.append({
            "label": label,
            "start": payload.get("period_start", ""),
            "end":   payload.get("period_end", ""),
        })
    return sorted(periods, key=lambda p: p["start"], reverse=True)


def reports_from_history(history: dict) -> list:
    """Synthesize a report set from the last week of each environment's history.

    The page builds its rows from the current week's reports and uses history
    only to fill monthly columns within those rows. A period with no reports
    therefore has no rows at all, which is why `reports=[]` renders an empty
    shell (2A.4.1) — and an archived period has no reports by definition.

    A closed period's "current week" is its final week, which is also the
    honest reading of an archive: the period as it stood when it closed. The
    shape produced here is the collector's, so every downstream renderer works
    unchanged rather than growing a history-only branch.
    """
    reports = []
    for env, snapshots in sorted(history.items()):
        if not snapshots:
            continue
        last = snapshots[-1]           # load_history returns them period-ordered

        current, comparison = [], []
        for pf in last.get("portfolios", []):
            projects = pf.get("projects", [])
            current.append({
                "portfolio":  pf["portfolio"],
                "total_cost": pf.get("cost", 0),
                "projects":   [{"project": pj["project"], "cost": pj.get("cost", 0)}
                               for pj in projects],
            })
            comparison.append({
                "portfolio":    pf["portfolio"],
                "current_cost": pf.get("cost", 0),
                "prior_cost":   pf.get("prior_cost", 0),
                "change":       pf.get("change"),
                "change_pct":   pf.get("change_pct"),
                "projects":     [{"project":      pj["project"],
                                  "current_cost": pj.get("cost", 0),
                                  "prior_cost":   pj.get("prior_cost", 0),
                                  "change":       pj.get("change"),
                                  "change_pct":   pj.get("change_pct")}
                                 for pj in projects],
            })

        reports.append({
            "environment":          env,
            "report_generated_utc": last.get("report_generated", ""),
            "current_period": {
                "start":      last.get("period_start", ""),
                "end":        last.get("period_end", ""),
                "portfolios": current,
            },
            "prior_period": {"start": "", "end": "", "portfolios": []},
            "comparison":   {"portfolios": comparison},
            # A forecast for a closed period is a prediction of the past.
            "forecast": None,
        })
    return reports


def coverage_gaps(history_dir: Path) -> dict:
    """Weeks with no shard at all, inside the range an environment does cover.

    This is the failure mode 4.2.2 does not catch. A month an environment never
    reported renders as `—`, which is honest. A single week missing from inside
    an otherwise-covered month shows up nowhere: the month simply totals low,
    and looks entirely plausible.

    The backfill already computes this set every time it runs, as target minus
    recorded. The weekly run never asks, which is the whole gap here.

    Bounded by the newest week any environment recorded rather than by today, so
    a week that has not been collected yet is not reported as a hole.
    """
    weeks: dict = {}
    for env, snapshots in load_history(history_dir).items():
        anchors = {_week_anchor(s["period_start"]) for s in snapshots
                   if s.get("period_start")}
        anchors.discard("")
        if anchors:
            weeks[env] = anchors

    if not weeks:
        return {}

    newest = max(a for anchors in weeks.values() for a in anchors)
    try:
        last = date_t.fromisoformat(newest)
    except ValueError:
        return {}

    gaps: dict = {}
    for env, anchors in weeks.items():
        try:
            cur = date_t.fromisoformat(min(anchors))
        except ValueError:
            continue
        expected = set()
        while cur <= last:
            expected.add(str(cur))
            cur += timedelta(days=7)
        missing = sorted(expected - anchors)
        if missing:
            gaps[env] = missing
    return gaps


def load_monthly_portfolio_history(history_dir: Path) -> dict | None:
    """Aggregate weekly snapshots into monthly attributed totals per portfolio across all envs.

    Returns {portfolio: {month_str: total_cost}} where total_cost is
    direct (non-shared) spend plus proportionally-allocated shared infrastructure
    costs for that week. Used for the overview table monthly columns.
    """
    if not history_dir.is_dir():
        return None

    result: dict = {}
    found_any = False
    for snapshots in load_history(history_dir).values():
        for snap in snapshots:
            ps = snap.get("period_start")
            if not ps:
                continue
            month = ps[:7]

            # First pass: subtract shared project costs and count non-shared projects per portfolio
            week_shared = 0.0
            week_counts: dict[str, int] = {}
            portfolio_costs: dict[str, float] = {}

            for p in snap.get("portfolios", []):
                name = normalize_portfolio(p.get("portfolio", ""))
                if not name or name.lower() in ("null", ""):
                    continue
                portfolio_cost = p.get("cost", 0)
                non_shared_cnt = 0
                seen_projects: set[str] = set()
                for pj in p.get("projects", []):
                    pj_name = normalize_project(pj.get("project", ""))
                    if is_shared_project(pj_name):
                        portfolio_cost -= pj.get("cost", 0)
                        week_shared += pj.get("cost", 0)
                    elif pj_name and pj_name not in seen_projects:
                        non_shared_cnt += 1
                        seen_projects.add(pj_name)
                portfolio_costs[name] = portfolio_cost
                week_counts[name] = non_shared_cnt
                found_any = True

            # Second pass: add week's allocated shared costs to each portfolio's monthly total
            week_total = sum(week_counts.values())
            per_proj = (week_shared / week_total) if (week_total > 0 and week_shared > 0) else 0.0
            for name, base_cost in portfolio_costs.items():
                cnt = week_counts.get(name, 0)
                attributed = base_cost + (per_proj * cnt if cnt > 0 else 0.0)
                result.setdefault(name, {})
                result[name][month] = result[name].get(month, 0) + attributed

    return result if found_any else None


def load_env_pop_totals(history_dir: Path) -> dict:
    """Period-to-date spend per environment, filtered by date rather than month.

    The stat card used to sum whole monthly buckets from the period's first
    month onward. That agreed with the per-portfolio figures only while the
    period began on the 1st: with a period starting August 30, the monthly sum
    swept in the whole of August while the portfolio tables counted two days.
    Five weeks against one, both labeled "JWCC POP Total", on the same page.

    Weeks are counted here by their own start date, matching the filter the
    portfolio figures already use.
    """
    if not history_dir.is_dir():
        return {}
    start = render_pop_start().isoformat()
    totals: dict = {}
    for env_label, snapshots in load_history(history_dir).items():
        for snap in snapshots:
            ps = snap.get("period_start")
            if not ps or ps < start:
                continue
            totals[env_label] = totals.get(env_label, 0.0) + snap.get("total_cost", 0)
    return totals


def load_env_monthly_totals(history_dir: Path) -> dict | None:
    """Aggregate weekly snapshots into monthly totals per environment.

    Returns {env_label: [(month_str, total_cost), ...]} sorted ascending.
    Used for stat card monthly spend and MoM change.
    """
    if not history_dir.is_dir():
        return None

    buckets: dict = {}
    found_any = False
    for env_label, snapshots in load_history(history_dir).items():
        for snap in snapshots:
            ps = snap.get("period_start")
            if not ps:
                continue
            month = ps[:7]
            buckets.setdefault(env_label, {})
            buckets[env_label][month] = buckets[env_label].get(month, 0) + snap.get("total_cost", 0)
            found_any = True

    if not found_any:
        return None

    return {
        env: sorted(month_costs.items(), key=lambda x: x[0])
        for env, month_costs in buckets.items()
    }


def load_jwcc_pop_totals(history_dir: Path) -> dict | None:
    """Sum all weekly history since Dec 1 of the previous year, per portfolio across all envs.

    Returns {portfolio: total_cost} or None if no history available.
    """
    if not history_dir.is_dir():
        return None

    jwcc_start = render_pop_start().isoformat()

    result: dict = {}
    found_any = False
    for snapshots in load_history(history_dir).values():
        for snap in snapshots:
            ps = snap.get("period_start", "")
            if not ps or ps < jwcc_start:
                continue
            for p in snap.get("portfolios", []):
                name = normalize_portfolio(p.get("portfolio", ""))
                if not name or name.lower() in ("null", ""):
                    continue
                result[name] = result.get(name, 0) + p.get("cost", 0)
                found_any = True

    return result if found_any else None


def forecast_provenance(reports: list) -> dict:
    """Whether the per-portfolio forecast figures were measured or derived.

    Azure returns the forecast without a TagValue column, so the collector
    splits one subscription total across portfolios in proportion to
    current-period spend. That is a reasonable estimate, but it is an estimate,
    and it currently sits in a column beside measured spend looking exactly as
    solid. This is what lets the page say which it is.

    Also carries the Actual/Forecast split, since the query includes actual cost
    and a window that has partly elapsed is not entirely a prediction.
    """
    bases, actual, forecast, unclassified = set(), 0.0, 0.0, 0.0
    for r in reports:
        fc = r.get("forecast") or {}
        if not fc:
            continue
        bases.add(fc.get("basis", "unknown"))
        components = fc.get("components") or {}
        actual       += components.get("actual", 0) or 0
        forecast     += components.get("forecast", 0) or 0
        unclassified += components.get("unclassified", 0) or 0

    return {
        # Only "measured" if every environment reported it that way.
        "basis":        "measured" if bases == {"measured"} else
                        ("allocated" if "allocated" in bases else "unknown"),
        "actual":       round(actual, 2),
        "forecast":     round(forecast, 2),
        "unclassified": round(unclassified, 2),
        "known":        bool(bases),
    }


def get_forecast_by_portfolio(reports: list) -> dict:
    """Aggregate raw 30-day forecast costs across all subscription report JSONs.

    Returns {portfolio: forecast_cost} including any shared project costs.
    Prefer get_adjusted_forecast_by_portfolio() for display purposes.
    """
    result: dict = {}
    for r in reports:
        fc = r.get("forecast")
        if not fc:
            continue
        for p in fc.get("portfolios", []):
            name = normalize_portfolio(p.get("portfolio", ""))
            if not name:
                continue
            result[name] = result.get(name, 0) + p.get("forecast_cost", 0)
    return result


def get_adjusted_forecast_by_portfolio(reports: list) -> dict:
    """30-day forecast per portfolio with shared project costs excluded.

    Estimates the non-shared fraction using each subscription's current-period
    cost ratio (shared_cost / total_cost) as a proxy for the forecast breakdown,
    then applies that fraction to the portfolio's forecast total.
    """
    adjusted: dict = {}
    for r in reports:
        fc = r.get("forecast")
        if not fc:
            continue
        comp_portf_map = {
            normalize_portfolio(p.get("portfolio", "")): p
            for p in r.get("comparison", {}).get("portfolios", [])
        }
        for p in fc.get("portfolios", []):
            pname = normalize_portfolio(p.get("portfolio", ""))
            if not pname:
                continue
            fc_cost = p.get("forecast_cost", 0)
            comp_p  = comp_portf_map.get(pname)
            if comp_p:
                projs      = comp_p.get("projects", [])
                total_cur  = sum(pj.get("current_cost", 0) for pj in projs)
                shared_cur = sum(
                    pj.get("current_cost", 0) for pj in projs
                    if is_shared_project(normalize_project(pj.get("project", "")))
                )
                non_shared = (total_cur - shared_cur) / total_cur if total_cur > 0 else 1.0
                fc_cost    = fc_cost * non_shared
            adjusted[pname] = adjusted.get(pname, 0) + fc_cost
    return {pname: round(cost, 2) for pname, cost in adjusted.items()}


def compute_shared_allocation(reports: list) -> dict | None:
    """Identify SHARED_PROJECTS costs in current reports and allocate proportionally.

    Computed per-environment: each subscription's shared project costs are
    distributed only among portfolios that have active non-shared projects in
    that subscription. A portfolio with projects in only one environment does
    not pay for shared infrastructure in environments where it has no resources.

    Returns a dict with keys:
      shared_total   – combined weekly cost of all shared projects (all envs)
      project_counts – {portfolio: union count across all envs, for display}
      total_projects – sum of project_counts
      per_project    – cross-env average (for tooltip display only)
      allocation     – {portfolio: per-env-aware allocated amount}
    Returns None when no shared project costs are found.
    """
    cross_env_shared_total = 0.0
    allocation: dict[str, float] = {}
    portfolio_projects_all: dict[str, set] = {}  # union across envs, for display/tooltip

    for r in reports:
        env_shared = 0.0
        env_projects: dict[str, set] = {}

        for p in r.get("comparison", {}).get("portfolios", []):
            pname = normalize_portfolio(p.get("portfolio", ""))
            if not pname or pname.lower() == "null":
                continue
            env_projects.setdefault(pname, set())
            portfolio_projects_all.setdefault(pname, set())
            for pj in p.get("projects", []):
                pj_name = normalize_project(pj.get("project", ""))
                if not pj_name:
                    continue
                if is_shared_project(pj_name):
                    env_shared += pj.get("current_cost", 0)
                else:
                    env_projects[pname].add(pj_name)
                    portfolio_projects_all[pname].add(pj_name)

        if env_shared == 0:
            continue

        cross_env_shared_total += env_shared
        env_counts = {pname: len(projs) for pname, projs in env_projects.items()}
        env_total  = sum(env_counts.values())
        if env_total == 0:
            continue

        per_proj = env_shared / env_total
        for pname, cnt in env_counts.items():
            if cnt > 0:
                allocation[pname] = allocation.get(pname, 0) + per_proj * cnt

    if cross_env_shared_total == 0:
        return None

    project_counts = {pname: len(projs) for pname, projs in portfolio_projects_all.items()}
    total_projects = sum(project_counts.values())
    per_project    = cross_env_shared_total / total_projects if total_projects > 0 else 0.0

    return {
        "shared_total":   round(cross_env_shared_total, 2),
        "project_counts": project_counts,
        "total_projects": total_projects,
        "per_project":    round(per_project, 2),
        "allocation":     {pname: round(amt, 2) for pname, amt in allocation.items()},
    }


def load_jwcc_pop_with_shared(history_dir: Path) -> dict | None:
    """JWCC POP totals split into actual (ex-shared) and shared-infrastructure allocation.

    For each portfolio returns {"actual": float, "shared": float}.

    Allocation is computed week-by-week so a portfolio only pays for shared
    infrastructure during the weeks it actually had active (non-shared) projects.
    A portfolio that joined in April pays nothing toward shared costs from December.
    """
    if not history_dir.is_dir():
        return None

    jwcc_start = render_pop_start().isoformat()

    actual_totals: dict[str, float] = {}
    shared_alloc_totals: dict[str, float] = {}
    found_any = False

    for snapshots in load_history(history_dir).values():
        for snap in snapshots:
            ps = snap.get("period_start", "")
            if not ps or ps < jwcc_start:
                continue

            # First pass: collect this week's shared cost and per-portfolio project counts
            week_shared = 0.0
            week_counts: dict[str, int] = {}

            for p in snap.get("portfolios", []):
                pname = normalize_portfolio(p.get("portfolio", ""))
                if not pname or pname.lower() in ("null", ""):
                    continue
                portfolio_cost  = p.get("cost", 0)
                non_shared_cnt  = 0
                seen_projects: set[str] = set()
                for pj in p.get("projects", []):
                    pj_name = normalize_project(pj.get("project", ""))
                    if is_shared_project(pj_name):
                        sc              = pj.get("cost", 0)
                        portfolio_cost -= sc
                        week_shared    += sc
                    elif pj_name and pj_name not in seen_projects:
                        non_shared_cnt += 1
                        seen_projects.add(pj_name)
                actual_totals[pname] = actual_totals.get(pname, 0) + portfolio_cost
                week_counts[pname]   = non_shared_cnt
                found_any = True

            # Second pass: allocate this week's shared costs only to portfolios
            # that had active projects this week
            week_total = sum(week_counts.values())
            if week_total > 0 and week_shared > 0:
                per_proj = week_shared / week_total
                for pname, cnt in week_counts.items():
                    if cnt > 0:
                        shared_alloc_totals[pname] = (
                            shared_alloc_totals.get(pname, 0) + per_proj * cnt
                        )

    if not found_any:
        return None

    result: dict = {}
    for pname in set(actual_totals) | set(shared_alloc_totals):
        if pname.lower() in ("null", ""):
            continue
        act    = round(actual_totals.get(pname, 0), 2)
        shared = round(shared_alloc_totals.get(pname, 0), 2)
        if act > 0 or shared > 0:
            result[pname] = {"actual": act, "shared": shared}

    return result if result else None


def load_monthly_project_history(history_dir: Path) -> dict | None:
    """Aggregate weekly snapshots into monthly totals per project per portfolio across all envs.

    Returns {portfolio: {project: {month_str: total_cost}}}. Shared projects are excluded.
    """
    if not history_dir.is_dir():
        return None

    result: dict = {}
    found_any = False

    for snapshots in load_history(history_dir).values():
        for snap in snapshots:
            ps = snap.get("period_start")
            if not ps:
                continue
            month = ps[:7]
            for p in snap.get("portfolios", []):
                pname = normalize_portfolio(p.get("portfolio", ""))
                if not pname or pname.lower() in ("null", ""):
                    continue
                result.setdefault(pname, {})
                for pj in p.get("projects", []):
                    pj_name = normalize_project(pj.get("project", ""))
                    if not pj_name or is_shared_project(pj_name):
                        continue
                    result[pname].setdefault(pj_name, {})
                    result[pname][pj_name][month] = (
                        result[pname][pj_name].get(month, 0) + pj.get("cost", 0)
                    )
                    found_any = True

    return result if found_any else None


def load_env_portfolio_monthly(history_dir: Path) -> dict | None:
    """Aggregate weekly snapshots into monthly totals per portfolio per environment.

    Returns {portfolio: {env_label: {month_str: cost}}}. Shared project costs excluded.
    """
    if not history_dir.is_dir():
        return None

    result: dict = {}
    found_any = False

    for env_label, snapshots in load_history(history_dir).items():
        for snap in snapshots:
            ps = snap.get("period_start")
            if not ps:
                continue
            month = ps[:7]
            for p in snap.get("portfolios", []):
                pname = normalize_portfolio(p.get("portfolio", ""))
                if not pname or pname.lower() in ("null", ""):
                    continue
                portfolio_cost = p.get("cost", 0)
                for pj in p.get("projects", []):
                    if is_shared_project(normalize_project(pj.get("project", ""))):
                        portfolio_cost -= pj.get("cost", 0)
                result.setdefault(pname, {})
                result[pname].setdefault(env_label, {})
                result[pname][env_label][month] = (
                    result[pname][env_label].get(month, 0) + portfolio_cost
                )
                found_any = True

    return result if found_any else None


def load_monthly_shared_per_portfolio(history_dir: Path) -> dict | None:
    """Monthly allocated shared infrastructure costs per portfolio.

    Returns {portfolio: {month_str: allocated_cost}}.
    Allocation is computed week-by-week (same logic as load_jwcc_pop_with_shared)
    so a portfolio only pays for shared costs during weeks it had active projects.
    Used to populate the Shared Infrastructure row in the project accordion.
    """
    if not history_dir.is_dir():
        return None

    result: dict = {}
    found_any = False

    for snapshots in load_history(history_dir).values():
        for snap in snapshots:
            ps = snap.get("period_start")
            if not ps:
                continue
            month = ps[:7]

            week_shared = 0.0
            week_counts: dict[str, int] = {}

            for p in snap.get("portfolios", []):
                pname = normalize_portfolio(p.get("portfolio", ""))
                if not pname or pname.lower() in ("null", ""):
                    continue
                non_shared_cnt = 0
                seen_projects: set[str] = set()
                for pj in p.get("projects", []):
                    pj_name = normalize_project(pj.get("project", ""))
                    if is_shared_project(pj_name):
                        week_shared += pj.get("cost", 0)
                    elif pj_name and pj_name not in seen_projects:
                        non_shared_cnt += 1
                        seen_projects.add(pj_name)
                week_counts[pname] = non_shared_cnt

            week_total = sum(week_counts.values())
            if week_total > 0 and week_shared > 0:
                per_proj = week_shared / week_total
                for pname, cnt in week_counts.items():
                    if cnt > 0:
                        result.setdefault(pname, {})
                        result[pname][month] = result[pname].get(month, 0) + per_proj * cnt
                        found_any = True

    return result if found_any else None


def get_env_portfolio_forecast(reports: list) -> dict:
    """Per-environment, per-portfolio 30-day forecast with shared costs excluded.

    Returns {portfolio: {env_label: adjusted_forecast_cost}}.
    """
    result: dict = {}
    for r in reports:
        env_label = fmt_env(r.get("environment", "unknown"))
        fc = r.get("forecast")
        if not fc:
            continue
        comp_portf_map = {
            normalize_portfolio(p.get("portfolio", "")): p
            for p in r.get("comparison", {}).get("portfolios", [])
        }
        for p in fc.get("portfolios", []):
            pname = normalize_portfolio(p.get("portfolio", ""))
            if not pname:
                continue
            fc_cost = p.get("forecast_cost", 0)
            comp_p  = comp_portf_map.get(pname)
            if comp_p:
                projs      = comp_p.get("projects", [])
                total_cur  = sum(pj.get("current_cost", 0) for pj in projs)
                shared_cur = sum(
                    pj.get("current_cost", 0) for pj in projs
                    if is_shared_project(normalize_project(pj.get("project", "")))
                )
                non_shared = (total_cur - shared_cur) / total_cur if total_cur > 0 else 1.0
                fc_cost    = fc_cost * non_shared
            result.setdefault(pname, {})
            result[pname][env_label] = round(result[pname].get(env_label, 0) + fc_cost, 2)
    return result


def get_shared_forecast_per_portfolio(reports: list) -> dict:
    """30-day forecast for shared infrastructure allocation per portfolio.

    Computed per-environment: each subscription's shared project forecast is
    distributed only among portfolios that have active projects in that
    subscription. A portfolio with projects in only one environment does not
    pay for shared infrastructure forecast in environments where it has nothing.
    Returns {portfolio: forecast_cost}.
    """
    result: dict[str, float] = {}

    for r in reports:
        fc = r.get("forecast")
        if not fc:
            continue

        # Estimate this environment's shared forecast using current-period shared fraction
        comp_portf_map = {
            normalize_portfolio(p.get("portfolio", "")): p
            for p in r.get("comparison", {}).get("portfolios", [])
        }
        env_shared_fc = 0.0
        for p in fc.get("portfolios", []):
            pname   = normalize_portfolio(p.get("portfolio", ""))
            fc_cost = p.get("forecast_cost", 0)
            comp_p  = comp_portf_map.get(pname)
            if comp_p:
                projs      = comp_p.get("projects", [])
                total_cur  = sum(pj.get("current_cost", 0) for pj in projs)
                shared_cur = sum(
                    pj.get("current_cost", 0) for pj in projs
                    if is_shared_project(normalize_project(pj.get("project", "")))
                )
                if total_cur > 0:
                    env_shared_fc += fc_cost * (shared_cur / total_cur)

        if env_shared_fc == 0:
            continue

        # Distribute only among portfolios with active non-shared projects in this environment
        env_projects: dict[str, set] = {}
        for p in r.get("comparison", {}).get("portfolios", []):
            pname = normalize_portfolio(p.get("portfolio", ""))
            if not pname or pname.lower() == "null":
                continue
            env_projects.setdefault(pname, set())
            for pj in p.get("projects", []):
                pj_name = normalize_project(pj.get("project", ""))
                if pj_name and not is_shared_project(pj_name):
                    env_projects[pname].add(pj_name)

        env_counts   = {pname: len(projs) for pname, projs in env_projects.items()}
        total_in_env = sum(env_counts.values())
        if total_in_env == 0:
            continue

        per_proj = env_shared_fc / total_in_env
        for pname, cnt in env_counts.items():
            if cnt > 0:
                result[pname] = result.get(pname, 0) + per_proj * cnt

    return {pname: round(cost, 2) for pname, cost in result.items()}


def render_sparkline(series: list, width: int = 72, height: int = 22) -> str:
    """Return an inline SVG sparkline for a (period_start, cost) series."""
    if not series or len(series) < 2:
        return '<span class="sparkline-na">—</span>'

    pts_data = series[-12:]   # cap at 12 weeks
    values   = [v for _, v in pts_data]
    min_v    = min(values)
    max_v    = max(values)
    span     = max_v - min_v or 1
    n        = len(values)
    pad      = 3

    coords = [
        (pad + i / (n - 1) * (width - 2 * pad),
         pad + (1 - (v - min_v) / span) * (height - 2 * pad))
        for i, v in enumerate(values)
    ]

    # Color based on the slope of the last 3 points (recent direction, not overall span)
    recent = values[-min(3, len(values)):]
    slope  = recent[-1] - recent[0]
    # Direction is carried as a class rather than a literal so the palette owns
    # the color. An inline fill="#c0392b" is exactly the kind of stray value the
    # token block exists to eliminate.
    trend = "sp-up" if slope > 0.01 else ("sp-down" if slope < -0.01 else "sp-flat")

    pts_str  = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    lx, ly   = coords[-1]
    baseline = height - pad
    area_d   = (f"M {coords[0][0]:.1f},{baseline} "
                + " ".join(f"L {x:.1f},{y:.1f}" for x, y in coords)
                + f" L {lx:.1f},{baseline} Z")
    tooltip  = f"{pts_data[0][0]} → {pts_data[-1][0]}: {fmt_cost(values[0])} → {fmt_cost(values[-1])}"

    return (
        f'<svg class="spark {trend}" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" '
        f'style="vertical-align:middle;overflow:visible" title="{tooltip}">'
        f'<path class="sp-area" d="{area_d}"/>'
        f'<polyline class="sp-line" points="{pts_str}" '
        f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle class="sp-dot" cx="{lx:.1f}" cy="{ly:.1f}" r="2.5"/>'
        f'</svg>'
    )


def render_forecast_sparkline(actuals: list, forecast_value: float | None = None,
                               width: int = 88, height: int = 44) -> str:
    """Stat-card sparkline: up to 3 actual monthly points + 1 dashed forecast point.

    actuals: list of (month_str, cost) — last 3 are used.
    forecast_value: 30-day forecast total; appended as a dashed open-circle point.
    """
    recent = actuals[-3:] if actuals else []
    values = [v for _, v in recent]

    if forecast_value is not None:
        all_values = values + [forecast_value]
    else:
        all_values = values

    if len(all_values) < 2:
        return '<span class="sparkline-na">—</span>'

    n     = len(all_values)
    min_v = min(all_values)
    max_v = max(all_values)
    span  = max_v - min_v or 1
    pad   = 4

    coords = [
        (pad + i / (n - 1) * (width - 2 * pad),
         pad + (1 - (v - min_v) / span) * (height - 2 * pad))
        for i, v in enumerate(all_values)
    ]

    # Color from slope of actual data only
    if len(values) >= 2:
        slope = values[-1] - values[-2]
    elif len(values) == 1:
        slope = (forecast_value or 0) - values[0]
    else:
        slope = 0

    trend = "sp-up" if slope > 0.01 else ("sp-down" if slope < -0.01 else "sp-flat")

    n_act        = len(values)
    actual_coords = coords[:n_act]

    svg_parts = []

    # Shaded area under actual points
    if len(actual_coords) >= 2:
        lx, ly   = actual_coords[-1]
        baseline = height - pad
        area_d   = (f"M {actual_coords[0][0]:.1f},{baseline} "
                    + " ".join(f"L {x:.1f},{y:.1f}" for x, y in actual_coords)
                    + f" L {lx:.1f},{baseline} Z")
        svg_parts.append(f'<path class="sp-area" d="{area_d}"/>')

    # Solid polyline through actual points
    if len(actual_coords) >= 2:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in actual_coords)
        svg_parts.append(
            f'<polyline class="sp-line" points="{pts}" '
            f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
        )

    # Last actual dot
    if actual_coords:
        lx, ly = actual_coords[-1]
        svg_parts.append(f'<circle class="sp-dot" cx="{lx:.1f}" cy="{ly:.1f}" r="2.5"/>')

    # Dashed segment + open circle for forecast point
    if forecast_value is not None and actual_coords:
        fx, fy = coords[-1]
        svg_parts.append(
            f'<line x1="{lx:.1f}" y1="{ly:.1f}" x2="{fx:.1f}" y2="{fy:.1f}" '
            f'class="sp-line sp-forecast" stroke-width="1.5" stroke-dasharray="3,2"/>'
            f'<circle class="sp-dot-open" cx="{fx:.1f}" cy="{fy:.1f}" r="3" '
            f'stroke-width="1.5"/>'
        )

    return (
        f'<svg class="spark {trend}" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" '
        f'style="vertical-align:middle;overflow:visible">'
        + "".join(svg_parts)
        + "</svg>"
    )


def build_cross_env_matrix(reports):
    """Build portfolio → project → env_label → {cost, change, change_pct}.

    Includes projects from both current_period and comparison so that projects
    with prior-week spend but zero this week still appear in the accordion.
    """
    matrix: dict = {}
    for r in reports:
        env_label  = fmt_env(r.get("environment", ""))
        cur_portf  = {p["portfolio"]: p for p in r.get("current_period", {}).get("portfolios", [])}
        comp_portf = {p["portfolio"]: p for p in r.get("comparison",     {}).get("portfolios", [])}

        all_port_names = set(cur_portf) | set(comp_portf)
        for port_name in all_port_names:
            if port_name.lower() == "null":
                continue
            matrix.setdefault(port_name, {})
            cur_p     = cur_portf.get(port_name,  {})
            comp_p    = comp_portf.get(port_name, {})
            cur_proj  = {p["project"]: p for p in cur_p.get("projects",  [])}
            comp_proj = {p["project"]: p for p in comp_p.get("projects", [])}

            for pj_name in set(cur_proj) | set(comp_proj):
                if is_shared_project(pj_name):
                    continue
                matrix[port_name].setdefault(pj_name, {})
                cur_pj  = cur_proj.get(pj_name,  {})
                comp_pj = comp_proj.get(pj_name, {})
                matrix[port_name][pj_name][env_label] = {
                    "cost":       cur_pj.get("cost", 0),
                    "change":     comp_pj.get("change"),
                    "change_pct": comp_pj.get("change_pct"),
                }
    return matrix


# ---------------------------------------------------------------------------
# HTML building blocks
# ---------------------------------------------------------------------------

CSS = """
/* ── Palette ───────────────────────────────────────────────────
   One token set, defined here and referenced everywhere else. This page
   previously carried 65 distinct hex values across 126 uses, most appearing
   once — which is why it read as flat rather than as designed. The colors were
   never the problem; the absence of a system was.

   Changing the look now means editing this block, not hunting literals.

   Three header tints carry meaning and are not interchangeable:
     --header       measured spend, the default column
     --header-alt   cumulative totals and second-level tables
     --header-est   estimated figures — deliberately lighter and warmer, so a
                    forecast never reads as solid as a measurement
   ───────────────────────────────────────────────────── */
:root {
  --ground:        #f0ece4;   /* the page */
  --paper:         #faf8f4;   /* tables and cards lifted off it */
  --surface:       #efeae1;   /* nested surfaces: detail rows, panels */
  --surface-soft:  #f4efe6;

  --border:        #d8d2c8;
  --border-strong: #c3bbae;
  --border-soft:   #e8e2d8;

  --ink:           #2b2620;   /* warm near-black */
  --ink-strong:    #1f1b16;   /* headings */
  --ink-muted:     #6b6259;
  --ink-faint:     #948a7e;
  --ink-faintest:  #b5ab9d;
  --on-dark:       #f7f3ec;

  --header:        #1a5c38;   /* deep green — measured */
  --header-hover:  #227046;
  --header-alt:    #4a3f2e;   /* warm brown — cumulative */
  --header-est:    #8a6d3b;   /* ochre — estimated */

  --accent:        #7a5a24;   /* interactive */
  --accent-ring:   rgba(122,90,36,.22);
  --row-hover:     #ece4d6;
  --row-pin:       #faf8f4;

  --good:          #4a7c52;   /* sage — cost down */
  --good-strong:   #2f6b45;
  --bad:           #9c4a34;   /* clay — cost up */
  --warn:          #b5822e;

  /* Pill tints. Declared here with the rest of the palette rather than in the
     cluster panel, so one block owns every color on either page. */
  --pill-app-bg:      #dce9dd;  --pill-app-ink:      #2b5c39;
  --pill-platform-bg: #e4ddcd;  --pill-platform-ink: #5c4a2a;
  --pill-system-bg:   #e6e2da;  --pill-system-ink:   #5f584e;
  --pill-shared-bg:   #ece2cd;  --pill-shared-ink:   #6d551f;
  --pill-idle-bg:     #ecd9d2;  --pill-idle-ink:     #8a4433;
  --pill-warn-bg:     #f0e0c0;  --pill-warn-ink:     #7d5a18;

  --notice-bg:     #4a3418;   /* dark warm panels: banners, badges */
  --notice-border: #8a6428;
  --notice-ink:    #f0dcb8;
  --notice-mark:   #e8bd72;
}

/* A full period is up to twelve monthly columns, which does not fit most
   screens. Scrolling the table keeps the figures exact — abbreviating them to
   fit would trade precision for width, and the toggle and portfolio name stay
   pinned so a scrolled row is still identifiable.

   Two details that the first attempt got wrong:

   `background: inherit` on a td resolves to the row, and the rows here are
   transparent, so the pinned name had the scrolling columns visible straight
   through it. Pinned cells need an opaque color of their own, and one for
   every row state they can be in — including :hover, or the pinned cell stops
   tracking the row it belongs to.

   Pinning only the name at left:0 also slid it on top of the toggle column
   rather than beside it. Both columns are pinned, with the second offset by the
   first, which is why the toggle column needs a fixed width. */
.table-scroll { overflow-x: auto; }

.overview-table > tbody > tr > td.toggle-cell { width: 34px; min-width: 34px; box-sizing: border-box; }

.table-scroll > .overview-table > thead > tr > th:nth-child(1),
.table-scroll > .overview-table > thead > tr > th:nth-child(2) {
  position: sticky; z-index: 3; background: var(--header);
}
.table-scroll > .overview-table > tbody > tr > td:nth-child(1),
.table-scroll > .overview-table > tbody > tr > td:nth-child(2) {
  position: sticky; z-index: 2; background: var(--row-pin);
}
.table-scroll > .overview-table > thead > tr > th:nth-child(1),
.table-scroll > .overview-table > tbody > tr > td:nth-child(1) { left: 0; }
.table-scroll > .overview-table > thead > tr > th:nth-child(2),
.table-scroll > .overview-table > tbody > tr > td:nth-child(2) { left: 34px; }

/* Track the row's own states, or the pinned cells read as a separate row. */
.overview-table > tbody > tr.portfolio-row:hover > td:nth-child(1),
.overview-table > tbody > tr.portfolio-row:hover > td:nth-child(2) { background: var(--row-hover); }

/* border-collapse drops borders on sticky cells, so the pinned edge is drawn
   with a shadow instead — which also makes it read as an edge while scrolling. */
.table-scroll > .overview-table > thead > tr > th:nth-child(2),
.table-scroll > .overview-table > tbody > tr > td:nth-child(2) {
  box-shadow: 2px 0 0 0 var(--border), 6px 0 8px -6px rgba(0,0,0,.25);
}

/* An open accordion is one cell spanning the whole table, so it scrolls with
   the parent and slides under the pinned columns — legible only at scroll
   position zero, which is why a closed table reads well and an open one does
   not.

   Anchoring the content to the left edge of the scroll port keeps it still
   while the months move behind it. Its width has to be stated, because the cell
   it sits in is as wide as the entire table; --page-content tracks the body's
   content box so the detail occupies the visible area and no more, and scrolls
   its own columns within that. */
:root { --page-content: min(calc(100vw - 48px), 1252px); }

.table-scroll .detail-content {
  position: sticky;
  left: 0;
  width: var(--page-content);
  box-sizing: border-box;
  overflow-x: auto;
}

/* The three summary columns pin to the right, so only the monthly columns move.
   Scrolling then reads as moving a window over the months, with the row's
   identity on one side and its totals on the other — both of which are what a
   monthly figure needs to be interpreted against.

   Offsets are measured from the right, so each column needs a fixed width and
   the ones outboard of it have to be counted. Order is forecast, trend, JWCC,
   which is why the offsets accumulate in that direction.

   Every selector here uses child combinators rather than descendants. The
   accordion detail tables live inside a colspan cell of this table and reuse
   the same column classes, so `.overview-table th.forecast-col` matched their
   headers too — pinning the detail's own forecast column to the parent's
   offset and forcing it to the parent's width. */
.overview-table > thead > tr > th.jwcc-col,     .overview-table > tbody > tr > td.pin-jwcc     { width: 180px; min-width: 180px; }
.overview-table > thead > tr > th.trend-col,    .overview-table > tbody > tr > td.pin-trend    { width:  96px; min-width:  96px; }
.overview-table > thead > tr > th.forecast-col, .overview-table > tbody > tr > td.pin-forecast { width: 130px; min-width: 130px; }

.table-scroll > .overview-table > thead > tr > th.jwcc-col,
.table-scroll > .overview-table > thead > tr > th.trend-col,
.table-scroll > .overview-table > thead > tr > th.forecast-col { position: sticky; z-index: 3; }
.table-scroll > .overview-table > tbody > tr > td.pin-jwcc,
.table-scroll > .overview-table > tbody > tr > td.pin-trend,
.table-scroll > .overview-table > tbody > tr > td.pin-forecast {
  position: sticky; z-index: 2; background: var(--row-pin);
}

.table-scroll > .overview-table > thead > tr > th.jwcc-col,
.table-scroll > .overview-table > tbody > tr > td.pin-jwcc     { right: var(--pin-jwcc, 0); }
.table-scroll > .overview-table > thead > tr > th.trend-col,
.table-scroll > .overview-table > tbody > tr > td.pin-trend    { right: var(--pin-trend, 180px); }
.table-scroll > .overview-table > thead > tr > th.forecast-col,
.table-scroll > .overview-table > tbody > tr > td.pin-forecast { right: var(--pin-forecast, 276px); }

/* Headers keep their own colors; the default th background is not sticky-safe
   because these two are deliberately tinted. */
.table-scroll > .overview-table > thead > tr > th.trend-col { background: var(--header); }

.overview-table > tbody > tr.portfolio-row:hover > td.pin-jwcc,
.overview-table > tbody > tr.portfolio-row:hover > td.pin-trend,
.overview-table > tbody > tr.portfolio-row:hover > td.pin-forecast { background: var(--row-hover); }

/* Edge on the inboard side of the pinned group, mirroring the left. */
.table-scroll > .overview-table > thead > tr > th.forecast-col,
.table-scroll > .overview-table > tbody > tr > td.pin-forecast {
  box-shadow: -2px 0 0 0 var(--border), -6px 0 8px -6px rgba(0,0,0,.25);
}
.est-mark { font-size: .72em; font-weight: 600; color: var(--notice-mark); letter-spacing: .02em;
            cursor: help; }
.missing-banner { margin: 0 0 1rem; padding: .7rem 1rem; border-radius: 6px;
                  background: var(--notice-bg); color: var(--notice-ink);
                  border: 1px solid var(--notice-border); font-size: .92rem; }
.missing-banner strong { color: var(--notice-mark); }

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--ground);
  color: var(--ink);
  padding: 28px 20px;
  max-width: 1300px;
  margin: 0 auto;
}
header { margin-bottom: 28px; text-align: center; }
header h1 { font-size: 1.75rem; color: var(--ink-strong); }
header p  { color: var(--ink-muted); font-size: 0.9rem; margin-top: 4px; }
h2 {
  font-size: 1.2rem;
  color: var(--ink-strong);
  border-bottom: 2px solid var(--border);
  padding-bottom: 8px;
  margin: 32px 0 14px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.875rem;
  margin-bottom: 4px;
  background: var(--paper);
}
th {
  background: var(--header);
  color: var(--on-dark);
  text-align: left;
  padding: 9px 13px;
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: .05em;
  white-space: nowrap;
}
th.right  { text-align: right; }
th.center { text-align: center; }
td {
  padding: 9px 13px;
  border-bottom: 1px solid var(--border-soft);
  vertical-align: middle;
}
tr:last-child td { border-bottom: none; }
td.cost     { font-weight: 600; text-align: right; }
td.count      { text-align: center; color: var(--ink-muted); }
td.sparkline  { text-align: center; white-space: nowrap; padding: 4px 10px; }
.sparkline-na { color: var(--ink-faintest); font-size: 11px; }

/* Sparklines are inline SVG, so their colors come from here rather than from
   fill/stroke attributes written into the markup. Direction is a class on the
   <svg>; the shapes inside inherit from it. */
.spark .sp-area { opacity: .12; }
.spark .sp-line { fill: none; }
.spark .sp-forecast { opacity: .65; }
.spark .sp-dot-open { fill: var(--paper); opacity: .8; }
.sp-up   .sp-area, .sp-up   .sp-dot { fill: var(--bad); }
.sp-up   .sp-line, .sp-up   .sp-dot-open { stroke: var(--bad); }
.sp-down .sp-area, .sp-down .sp-dot { fill: var(--good); }
.sp-down .sp-line, .sp-down .sp-dot-open { stroke: var(--good); }
.sp-flat .sp-area, .sp-flat .sp-dot { fill: var(--ink-faint); }
.sp-flat .sp-line, .sp-flat .sp-dot-open { stroke: var(--ink-faint); }
td.increase  { color: var(--bad); font-weight: 600; text-align: right; }
td.decrease  { color: var(--good); font-weight: 600; text-align: right; }
td.neutral   { color: var(--ink-faint); text-align: right; }
td.muted     { color: var(--ink-faintest); font-weight: 400; }
div.increase { color: var(--bad); font-weight: 600; }
div.decrease { color: var(--good); font-weight: 600; }
div.neutral  { color: var(--ink-faint); }

/* ── Overview table ────────────────────────────────────────── */
.overview-table td, .overview-table th { border: 1px solid var(--border); }
.overview-table thead th               { border-color: var(--header); }
.overview-table tr:last-child td       { border-bottom: 1px solid var(--border); }
.overview-table .portfolio-row:hover td { background: var(--row-hover); }

/* ── Sort ──────────────────────────────────────────────────── */
.sortable { cursor: pointer; user-select: none; }
.sortable:hover { background: var(--header-hover); }
.sort-ind { opacity: 0.85; margin-left: 3px; font-size: 0.82em; }

/* ── Accordion toggle ──────────────────────────────────────── */
.toggle-col  { width: 36px; }
.toggle-cell { text-align: center; padding: 4px 6px; border-right: 1px solid var(--border); }
.toggle-btn {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 0.78rem;
  color: var(--ink-muted);
  padding: 3px 6px;
  border-radius: 4px;
  line-height: 1;
}
.toggle-btn:hover { background: var(--row-hover); color: var(--accent); }
.toggle-btn.open  { color: var(--accent); }

/* ── Accordion detail row ──────────────────────────────────── */
.detail-row { background: var(--surface); }
.detail-cell { padding: 0 !important; border-top: none !important; }
.detail-content {
  padding: 14px 14px 14px 52px;
  border-top: 1px solid var(--border);
  border-bottom: 2px solid var(--border);
}
.detail-table { font-size: 0.84rem; }
.detail-table th { background: var(--header-alt); font-size: 0.73rem; }
.detail-table td, .detail-table th { border: 1px solid var(--border-strong); }
.detail-table thead th { border-color: var(--header-alt); }
.detail-table tr:last-child td { border-bottom: 1px solid var(--border-strong); }
.detail-table tr:hover td { background: var(--row-hover); }
.no-detail { font-size: 0.82rem; color: var(--ink-faint); font-style: italic; padding: 4px 0; }
.breakdown-label {
  font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .07em; color: var(--ink-muted); margin: 0 0 6px;
}
.breakdown-label + .breakdown-label { margin-top: 16px; }

/* ── Filter input ──────────────────────────────────────────── */
.table-controls { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.filter-input {
  padding: 7px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 0.875rem;
  width: 260px;
  outline: none;
  color: var(--ink);
}
.filter-input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-ring); }
.filter-hint { font-size: 0.78rem; color: var(--ink-faint); }

/* ── Summary cards ─────────────────────────────────────────── */
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
  margin-bottom: 28px;
}
.stat-card {
  background: var(--paper);
  border-radius: 8px;
  padding: 16px 20px;
  box-shadow: 0 1px 4px rgba(0,0,0,.08);
  border-left: 4px solid var(--accent);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.stat-card-left  { flex: 1; min-width: 0; }
.stat-card-right { flex-shrink: 0; display: flex; align-items: center; justify-content: center; padding-left: 14px; }
.stat-card .env-label { font-size: 0.78rem; color: var(--ink-faint); text-transform: uppercase; letter-spacing:.05em; }
/* ── Stat card metric rows ─────────────────────────────────── */
.stat-metrics { margin-top: 8px; display: flex; flex-direction: column; gap: 5px; }
.stat-metric-row { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.metric-label { font-size: 0.69rem; color: var(--ink-faint); white-space: nowrap; }
.metric-value { font-weight: 700; font-size: 0.88rem; color: var(--ink-strong); text-align: right; }
.metric-value.primary-value { font-size: 1.35rem; line-height: 1.2; }
.metric-value.forecast-value { color: var(--good-strong); }
.metric-value.jwcc-value { color: var(--ink-strong); }
.metric-sub { font-size: 0.67rem; color: var(--ink-faint); display: block; text-align: right; margin-top: 1px; }

/* ── Collapsible info panel ────────────────────────────────── */
.info-panel {
  background: var(--surface-soft);
  border-left: 4px solid var(--accent);
  border-radius: 6px;
  margin-bottom: 24px;
  overflow: hidden;
}
.info-panel-summary {
  padding: 11px 16px;
  cursor: pointer;
  font-size: 0.88rem;
  font-weight: 700;
  color: var(--ink-strong);
  list-style: none;
  display: flex;
  align-items: center;
  gap: 8px;
  user-select: none;
}
.info-panel-summary::-webkit-details-marker { display: none; }
.info-panel-chevron {
  font-size: 0.7em;
  display: inline-block;
  transition: transform 0.2s;
  color: var(--accent);
}
details[open] .info-panel-chevron { transform: rotate(90deg); }
.info-panel-hint { font-size: 0.77rem; color: var(--ink-muted); font-weight: 400; margin-left: auto; }
.info-panel-body {
  padding: 2px 16px 16px;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 16px 24px;
}
.info-section h4 {
  font-size: 0.75rem;
  font-weight: 700;
  color: var(--ink-strong);
  margin: 0 0 5px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  border-bottom: 1px solid var(--border);
  padding-bottom: 3px;
}
.info-section p, .info-section ul {
  font-size: 0.82rem;
  color: var(--ink);
  margin: 0;
  line-height: 1.55;
}
.info-section ul { padding-left: 16px; margin-top: 4px; }
.info-section ul li { margin-bottom: 3px; }
.info-section a { color: var(--accent); text-decoration: none; }
.info-section a:hover { text-decoration: underline; }
/* ── JWCC POP and Forecast column accents ───────────────────── */
th.jwcc-col     { background: var(--header-alt) !important; }
th.forecast-col { background: var(--header-est) !important; }
td.jwcc-cost    { font-weight: 700; color: var(--ink-strong); text-align: right; }
td.forecast-cost { font-weight: 700; color: var(--good-strong); text-align: right; }
.period-meta { font-size: 0.82rem; color: var(--ink-muted); margin-bottom: 10px; }
.overview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  border-bottom: 2px solid var(--border);
  padding-bottom: 10px;
  margin: 32px 0 14px;
}
.overview-header h2 {
  margin: 0;
  border: none;
  padding: 0;
  flex: 1;
  min-width: 0;
}
.period-inline { font-size: 0.82rem; font-weight: 400; color: var(--ink-muted); }
/* Moved below the tables and re-themed to match the page. It was the one dark
   block on a light document, which made an archive index look like an alert.
   It stays permanently visible — the failure guarded against is someone
   concluding a period was lost — but it no longer competes for attention, and
   collapses past the third entry so it cannot grow into the page. */
.prior-periods {
  margin: 2rem 0 0; padding: .9rem 1.1rem; border-radius: 8px;
  background: var(--surface); border: 1px solid var(--border);
}
.prior-periods h2 { margin: 0 0 .3rem; font-size: 1rem; color: var(--ink-strong);
                    border: none; padding: 0; }
.prior-periods p  { margin: 0 0 .5rem; font-size: .85rem; color: var(--ink-muted); }
.prior-periods ul { margin: 0; padding-left: 1.1rem; }
.prior-periods li { margin-bottom: .2rem; }
.prior-periods a  { color: var(--accent); text-decoration: none; font-weight: 600; }
.prior-periods a:hover { text-decoration: underline; }
.prior-range { color: var(--ink-faint); font-size: .8rem; }
.prior-more summary { cursor: pointer; font-size: .82rem; color: var(--ink-muted);
                      margin-top: .4rem; user-select: none; }
.prior-more[open] summary { margin-bottom: .3rem; }
.partial-month { color: var(--warn); font-weight: 700; margin-left: 1px; cursor: help; }
.archive-mark {
  display: inline-block; margin-left: .5rem; padding: .1rem .5rem;
  border-radius: 4px; background: var(--notice-bg); color: var(--notice-ink);
  font-size: .75rem; font-weight: 600; vertical-align: middle;
}
.archive-nav { margin: .35rem 0 0; font-size: .85rem; }
.archive-nav a { color: var(--accent); text-decoration: none; }
.archive-nav a:hover { text-decoration: underline; }
header .period-str {
  font-size: 1.05rem; font-weight: 600; color: var(--ink-strong); margin-top: 6px;
}
header .generated { color: var(--ink-muted); font-size: 0.82rem; margin-top: 3px; }

/* ── Help tooltip icon ─────────────────────────────────────── */
.help-tip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 13px;
  height: 13px;
  border-radius: 50%;
  border: 1px solid currentColor;
  font-size: 8px;
  font-weight: 700;
  cursor: help;
  opacity: 0.65;
  margin-left: 4px;
  vertical-align: middle;
  text-transform: none;
  letter-spacing: 0;
  line-height: 1;
  flex-shrink: 0;
}

/* ── System tabs (shown only when multiple systems present) ─── */
.tabs { display: flex; border-bottom: 2px solid var(--border); margin-bottom: 28px; gap: 4px; }
.tab-btn {
  padding: 10px 28px;
  border: none; background: none; cursor: pointer;
  font-size: 0.95rem; font-weight: 600; color: var(--ink-muted);
  border-bottom: 3px solid transparent; margin-bottom: -2px;
  border-radius: 4px 4px 0 0;
  transition: color 0.15s, border-color 0.15s;
}
.tab-btn:hover { color: var(--accent); background: var(--row-hover); }
.tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-content { display: none; }
.tab-content.active { display: block; }
"""

JS = """
// ── Accordion ──────────────────────────────────────────────────────────────
function toggleDetail(id) {
  var row = document.getElementById('detail-' + id);
  var btn = document.getElementById('toggle-' + id);
  var open = row.style.display !== 'none';
  row.style.display = open ? 'none' : 'table-row';
  btn.innerHTML     = open ? '&#9658;' : '&#9660;';
  btn.title         = open ? 'Expand projects' : 'Collapse projects';
  btn.classList.toggle('open', !open);
}

// ── Sort ────────────────────────────────────────────────────────────────────
var _sortState = {};

function _camel(s) {
  return s.replace(/-([a-z])/g, function(_, c) { return c.toUpperCase(); });
}

function _rowVal(row, key) {
  var v = row.dataset[_camel(key)];
  return (v !== undefined && v !== '') ? v : null;
}

function sortTable(tableId, key) {
  var tbody = document.getElementById(tableId + '-tbody');
  if (!tbody) return;
  var state = _sortState[tableId] || { key: null, dir: 'asc' };

  if (state.key === key) {
    state.dir = state.dir === 'asc' ? 'desc' : 'asc';
  } else {
    state.key = key;
    state.dir = 'asc';
  }
  _sortState[tableId] = state;

  var rows = Array.from(tbody.querySelectorAll('tr.portfolio-row'));
  var dir  = state.dir === 'asc' ? 1 : -1;

  rows.sort(function(a, b) {
    var av = _rowVal(a, key), bv = _rowVal(b, key);
    var an = av !== null ? parseFloat(av) : null;
    var bn = bv !== null ? parseFloat(bv) : null;
    if (an !== null && !isNaN(an) && bn !== null && !isNaN(bn)) return (an - bn) * dir;
    if (av === null && bv === null) return 0;
    if (av === null) return 1;
    if (bv === null) return -1;
    return String(av).localeCompare(String(bv)) * dir;
  });

  rows.forEach(function(row) {
    var detail = document.getElementById('detail-' + row.dataset.id);
    tbody.appendChild(row);
    if (detail) tbody.appendChild(detail);
  });

  // Update sort indicators on this table's header
  var table = tbody.closest('table');
  table.querySelectorAll('.sortable').forEach(function(th) {
    var ind = th.querySelector('.sort-ind');
    if (!ind) return;
    ind.textContent = (th.dataset.sortKey === key)
      ? (state.dir === 'asc' ? ' ↑' : ' ↓')
      : '';
  });
}

// ── Filter ──────────────────────────────────────────────────────────────────
// Matches against portfolio names AND project names.
// When a project matches, its parent portfolio row is shown and the accordion
// auto-expands. Clearing the filter collapses any auto-expanded accordions.
function filterPortfolios(inputEl, tableId) {
  var q     = inputEl.value.toLowerCase().trim();
  var tbody = document.getElementById(tableId + '-tbody');
  if (!tbody) return;

  tbody.querySelectorAll('tr.portfolio-row').forEach(function(row) {
    var pid    = row.dataset.id;
    var detail = document.getElementById('detail-' + pid);
    var btn    = document.getElementById('toggle-' + pid);

    // Filter cleared — show all rows, collapse anything the filter auto-opened
    if (!q) {
      row.style.display = '';
      if (detail && detail.dataset.filterExpanded) {
        detail.style.display = 'none';
        delete detail.dataset.filterExpanded;
        if (btn) { btn.innerHTML = '&#9658;'; btn.classList.remove('open'); }
      }
      return;
    }

    var portfolioMatch = (row.dataset.name || '').includes(q);
    var projectMatch   = false;

    // Only scan projects if the portfolio name didn't already match
    if (!portfolioMatch && detail) {
      var projCells = detail.querySelectorAll('.detail-table tbody tr td:first-child');
      projectMatch  = Array.from(projCells).some(function(c) {
        return c.textContent.toLowerCase().includes(q);
      });
    }

    var show = portfolioMatch || projectMatch;
    row.style.display = show ? '' : 'none';

    if (detail) {
      if (!show) {
        // Row hidden — clean up any filter-expand state
        if (detail.dataset.filterExpanded) {
          detail.style.display = 'none';
          delete detail.dataset.filterExpanded;
          if (btn) { btn.innerHTML = '&#9658;'; btn.classList.remove('open'); }
        } else {
          detail.style.display = 'none';
        }
      } else if (projectMatch && detail.style.display === 'none') {
        // Project matched and accordion is closed — auto-expand it
        detail.style.display = 'table-row';
        detail.dataset.filterExpanded = '1';
        if (btn) { btn.innerHTML = '&#9660;'; btn.classList.add('open'); }
      }
    }
  });
}

// ── System tabs ─────────────────────────────────────────────────────────────
function showTab(tabId, btn) {
  document.querySelectorAll('.tab-content').forEach(function(el) { el.classList.remove('active'); });
  document.querySelectorAll('.tab-btn').forEach(function(el)     { el.classList.remove('active'); });
  document.getElementById('tab-' + tabId).classList.add('active');
  btn.classList.add('active');
}
"""


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------

def render_summary_cards(reports, env_monthly=None, forecast=None,
                          jwcc_shared=None, jwcc_pop=None):
    """Stat cards — one per subscription environment.

    Shows three metrics per card: 90-day monthly avg (primary), 30-day forecast,
    and JWCC POP total. Sparkline uses the last 3 actual monthly data points plus
    a dashed forecast point.
    """
    today = datetime.now(timezone.utc).date()

    # Build per-env forecast total from individual report objects (each report = one sub/env)
    env_forecast: dict[str, float] = {}
    for r in reports:
        env_label = fmt_env(r.get("environment", "unknown"))
        fc = r.get("forecast")
        if fc:
            env_forecast[env_label] = sum(
                p.get("forecast_cost", 0) for p in fc.get("portfolios", [])
            )

    cards = []
    for r in sort_reports_by_env(reports):
        env = fmt_env(r.get("environment", "unknown"))

        # 90-day avg: mean of last 3 calendar months for this environment
        if env_monthly and env in env_monthly:
            monthly = env_monthly[env]
            last_3  = monthly[-3:]
            avg_90  = sum(c for _, c in last_3) / len(last_3) if last_3 else 0
        else:
            monthly = []
            last_3  = []
            avg_90  = 0

        # 30-day forecast for this environment
        fc_total = env_forecast.get(env, 0)

        # Counted from weekly snapshots by date, not by summing whole months:
        # a period starting mid-month would otherwise include the weeks before
        # it began, and disagree with every per-portfolio figure on the page.
        jwcc_total = ENV_POP_TOTALS.get(env, 0.0)

        # Sparkline: actual monthly points + dashed forecast point
        sparkline_html = render_forecast_sparkline(last_3, fc_total if fc_total else None)

        # Forecast metric row (hidden when no forecast data)
        fc_row = (
            f'<div class="stat-metric-row">'
            f'<span class="metric-label">30-Day Forecast{FORECAST_EST_MARK}</span>'
            f'<span class="metric-value forecast-value">{fmt_cost(fc_total)}</span>'
            f'</div>'
        ) if fc_total else ""

        # JWCC POP metric row
        if jwcc_total:
            jwcc_row = (
                f'<div class="stat-metric-row">'
                f'<span class="metric-label">JWCC POP Total</span>'
                f'<span class="metric-value jwcc-value">{fmt_cost(jwcc_total)}</span>'
                f'</div>'
            )
        else:
            jwcc_row = ""

        cards.append(f"""
        <div class="stat-card">
          <div class="stat-card-left">
            <div class="env-label">{env}</div>
            <div class="stat-metrics">
              <div class="stat-metric-row">
                <span class="metric-label">90-Day Monthly Avg</span>
                <span class="metric-value primary-value">{fmt_cost(avg_90)}</span>
              </div>
              {fc_row}
              {jwcc_row}
            </div>
          </div>
          <div class="stat-card-right">{sparkline_html}</div>
        </div>""")

    return f'<div class="summary-grid">{"".join(cards)}</div>'


def render_pipeline_metrics_panel(report: dict | None) -> str:
    if not report:
        return (
            '<section class="pipeline-metrics">'
            '<h2>Piepline Performance</h2>'
            '<p class="period-meta">Not collected this run.</p>'
            '</section>'
        )

    job_counts = report.get("job_counts") or {}
    pipeline_counts = report.get("pipeline_counts") or {}
    period_start = report.get("period_start", "?")
    period_end = report.get("period_end", "?")
    scope = escape(str(report.get("scope") or "AI2C"))
    projects_scanned = int(report.get("projects_scanned") or 0)
    projects_total = int(report.get("projects_total") or 0)
    status = str(report.get("status") or "unknown")
    failures = report.get("project_failures") or []

    partial_note = ""
    if status == "partial":
        partial_note = (
            '<p class="missing-banner">⚠️ GitLab CI metrics are partial this run. '
            f'{len(failures)} of {projects_total} project(s) could not be read, '
            'so totals below are understated.</p>'
        )
    elif status == "failed":
        partial_note = (
            '<p class="missing-banner">⚠️ GitLab CI metrics could not be collected '
            'for this run.</p>'
        )

    cards = [
        (
            "Total Jobs",
            fmt_count(job_counts.get("total")),
            f"{fmt_count(job_counts.get('success'))} succeeded, "
            f"{fmt_count(job_counts.get('failed'))} failed, "
            f"{fmt_count(job_counts.get('other'))} other",
        ),
        (
            "Total Pipelines",
            fmt_count(pipeline_counts.get("total")),
            f"{fmt_count(pipeline_counts.get('success'))} succeeded, "
            f"{fmt_count(pipeline_counts.get('failed'))} failed, "
            f"{fmt_count(pipeline_counts.get('other'))} other",
        ),
        (
            "Job Success Rate",
            fmt_rate(job_counts.get("success_rate")),
            f"Based on {fmt_count(job_counts.get('terminal'))} settled jobs",
        ),
        (
            "Pipeline Success Rate",
            fmt_rate(pipeline_counts.get("success_rate")),
            f"Based on {fmt_count(pipeline_counts.get('terminal'))} settled pipelines",
        ),
    ]

    cards_html = "".join(
        f"""
        <div class="stat-card">
          <div class="stat-card-left">
            <div class="env-label">{escape(label)}</div>
            <div class="stat-metrics">
              <div class="stat-metric-row">
                <span class="metric-value primary-value">{escape(value)}</span>
              </div>
              <div class="stat-metric-row">
                <span class="metric-sub">{escape(subtext)}</span>
              </div>
            </div>
          </div>
        </div>"""
        for label, value, subtext in cards
    )

    return (
        '<section class="pipeline-metrics">'
        '<h2>Piepline Performance</h2>'
        f'<p class="period-meta">{scope} group activity for the last complete ISO week, '
        f'{escape(str(period_start))} to {escape(str(period_end))}. '
        f'{projects_scanned} of {projects_total} project(s) were scanned. '
        'Success rates count final <code>success</code> and <code>failed</code> outcomes; '
        'all other statuses are shown separately.</p>'
        f'{partial_note}'
        f'<div class="summary-grid">{cards_html}</div>'
        '</section>'
    )


def render_env_breakdown_table(env_data: dict, display_months: list,
                               env_forecast: dict) -> str:
    """Compact table showing per-environment monthly costs + forecast for one portfolio."""
    has_forecast = bool(env_forecast)
    month_ths    = "".join(f'<th class="right">{fmt_month(m)}{partial_month_mark(m)}</th>'
        for m in display_months)
    forecast_th  = ('<th class="right forecast-col">30-Day Forecast'
                    f'{FORECAST_EST_MARK}</th>' if has_forecast else "")

    sorted_envs = sorted(env_data.keys(), key=lambda e: ENV_ORDER.get(e.lower(), 99))

    rows = []
    for env in sorted_envs:
        month_data = env_data[env]
        start      = ENV_DATA_START.get(env)
        cells = [f'<td><strong>{env}</strong></td>']
        for m in display_months:
            if start and m < start:
                # Before this subscription reported anything. Not zero spend.
                cells.append('<td class="neutral" title="No data — this '
                             'subscription was not reporting costs yet">&mdash;</td>')
            else:
                cells.append(cost_cell(month_data.get(m, 0)))
        if has_forecast:
            fc = env_forecast.get(env, 0)
            cells.append(
                f'<td class="forecast-cost">{fmt_cost(fc)}</td>'
                if fc else '<td class="neutral">—</td>'
            )
        rows.append(f"<tr>{''.join(cells)}</tr>")

    return f"""<table class="detail-table" style="margin-bottom:0">
      <thead>
        <tr><th>Environment</th>{month_ths}{forecast_th}</tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>"""


def render_accordion_detail(pid, proj_matrix, env_labels, total_cols,
                             shared_alloc=None, portfolio_name="",
                             proj_monthly=None, display_months=None,
                             portfolio_forecast=None,
                             env_portfolio_monthly=None,
                             env_portfolio_forecast=None,
                             monthly_shared=None,
                             shared_forecast=None):
    """Hidden detail row that expands under a portfolio row.

    When proj_monthly and display_months are available, renders:
      1. An environment breakdown table (per-env monthly costs + forecast)
      2. A project breakdown table (per-project monthly costs + forecast)
    Falls back to current-week env-by-env layout when no history is present.
    """
    alloc_amount   = (shared_alloc or {}).get("allocation", {}).get(portfolio_name, 0)
    has_shared_row = alloc_amount > 0
    pj_data        = proj_monthly or {}

    # Project list = union of current-week matrix + historical monthly data
    all_pj_names = sorted(
        set(proj_matrix.keys()) | set(pj_data.keys()),
        key=lambda x: ("" if x != "(untagged)" else "zzz") + x.lower()
    )

    use_monthly = bool(pj_data and display_months)

    if not all_pj_names and not has_shared_row:
        content = '<p class="no-detail">No project tags found under this portfolio.</p>'

    elif use_monthly:
        # ── Monthly layout: Project | Jan | Feb | Mar | [30-Day Forecast] ──────
        has_forecast = bool(portfolio_forecast)

        # Allocate portfolio-level forecast to projects proportionally by last month's spend
        if has_forecast:
            last_month    = display_months[-1]
            recent_costs  = {pj: pj_data.get(pj, {}).get(last_month, 0) for pj in all_pj_names}
            recent_total  = sum(recent_costs.values())
            proj_forecasts = (
                {pj: round(portfolio_forecast * (recent_costs[pj] / recent_total), 2)
                 for pj in all_pj_names}
                if recent_total > 0 else {}
            )
        else:
            proj_forecasts = {}

        month_ths    = "".join(f'<th class="right">{fmt_month(m)}{partial_month_mark(m)}</th>'
        for m in display_months)
        forecast_th  = ('<th class="right forecast-col">30-Day Forecast'
                        f'{FORECAST_EST_MARK}</th>'
                        if has_forecast else "")

        proj_rows = []
        for pj in all_pj_names:
            label = ('<span style="color:#888;font-style:italic">(untagged resources)</span>'
                     if pj == "(untagged)" else pj)
            cells = [f"<td>{label}</td>"]
            for m in display_months:
                cells.append(cost_cell(pj_data.get(pj, {}).get(m, 0)))
            if has_forecast:
                fc = proj_forecasts.get(pj, 0)
                cells.append(
                    f'<td class="forecast-cost">{fmt_cost(fc)}</td>'
                    if fc else '<td class="neutral">—</td>'
                )
            proj_rows.append(f"<tr>{''.join(cells)}</tr>")

        if has_shared_row:
            per_proj   = (shared_alloc or {}).get("per_project", 0)
            proj_count = (shared_alloc or {}).get("project_counts", {}).get(portfolio_name, 0)
            tip_text   = (
                "Shared infrastructure costs from expedition-0 and gitlabrunners projects, "
                "allocated proportionally by active project count each week "
                f"({proj_count} project(s) x {fmt_cost(per_proj)}/project this week)."
            )
            q_icon       = f'<span class="help-tip" title="{tip_text}">?</span>'
            shared_label = f'<span style="color:#6b2fa0;font-style:italic">⊕ Shared Infrastructure{q_icon}</span>'

            if monthly_shared:
                cells = [f"<td>{shared_label}</td>"]
                for m in display_months:
                    mc = monthly_shared.get(m, 0)
                    cells.append(
                        f'<td class="cost" style="color:#6b2fa0">{fmt_cost(mc)}</td>'
                        if mc else '<td class="neutral muted">—</td>'
                    )
                if has_forecast:
                    cells.append(
                        f'<td class="forecast-cost" style="color:#6b2fa0">{fmt_cost(shared_forecast)}</td>'
                        if shared_forecast else '<td class="neutral">—</td>'
                    )
                proj_rows.append(f'<tr style="background:#f5f0fb">{"".join(cells)}</tr>')
            else:
                n          = len(display_months)
                blank      = '<td class="neutral muted">—</td>'
                alloc_cell = (f'<td class="cost" style="color:#6b2fa0">'
                              f'{fmt_cost(alloc_amount)}'
                              f'<small style="font-weight:400;color:#999;font-size:0.78em">'
                              f'&thinsp;(this week)</small></td>')
                proj_rows.append(
                    f'<tr style="background:#f5f0fb">'
                    f'<td>{shared_label}</td>'
                    + blank * (n - 1)
                    + alloc_cell
                    + (blank if has_forecast else "")
                    + "</tr>"
                )

        proj_table = f"""<table class="detail-table">
          <thead>
            <tr>
              <th>Project</th>{month_ths}{forecast_th}
            </tr>
          </thead>
          <tbody>{"".join(proj_rows)}</tbody>
        </table>"""

        if env_portfolio_monthly and display_months:
            env_table = render_env_breakdown_table(
                env_portfolio_monthly, display_months, env_portfolio_forecast or {}
            )
            content = (
                f'<div class="breakdown-label">By Environment</div>'
                f'{env_table}'
                f'<div class="breakdown-label" style="margin-top:14px">By Project</div>'
                f'{proj_table}'
            )
        else:
            content = proj_table

    else:
        # ── Fallback: current-week env-by-env layout ──────────────────────────
        env_group_ths = (
            "".join(f'<th colspan="2" class="center">{e}</th>' for e in env_labels)
            + '<th rowspan="2" class="right" style="background:#243d5c">Total</th>'
        )
        env_sub_ths = "".join(
            '<th class="right">Current Spend</th><th class="right">Weekly Change</th>'
            for _ in env_labels
        )

        proj_rows = []
        for pj in all_pj_names:
            env_data = proj_matrix.get(pj, {})
            label = ('<span style="color:#888;font-style:italic">(untagged resources)</span>'
                     if pj == "(untagged)" else pj)
            cells     = [f"<td>{label}</td>"]
            row_total = 0
            for e in env_labels:
                ed = env_data.get(e)
                if ed is None:
                    cells.append('<td class="neutral muted" title="Not present in this environment">—</td>')
                    cells.append('<td class="neutral muted">—</td>')
                else:
                    cells.append(cost_cell(ed["cost"]))
                    cells.append(change_cell(ed.get("change"), ed.get("change_pct")))
                    row_total += ed.get("cost", 0)
            cells.append(cost_cell(row_total))
            proj_rows.append(f"<tr>{''.join(cells)}</tr>")

        if has_shared_row:
            per_proj   = (shared_alloc or {}).get("per_project", 0)
            proj_count = (shared_alloc or {}).get("project_counts", {}).get(portfolio_name, 0)
            tip_text   = (
                "Shared infrastructure costs from expedition-0 and gitlabrunners projects, "
                "allocated proportionally by active project count each week "
                f"({proj_count} project(s) x {fmt_cost(per_proj)}/project this week)."
            )
            q_icon       = f'<span class="help-tip" title="{tip_text}">?</span>'
            shared_label = f'<span style="color:#6b2fa0;font-style:italic">⊕ Shared Infrastructure{q_icon}</span>'
            blank_pair   = '<td class="neutral muted">—</td><td class="neutral muted">—</td>'
            proj_rows.append(
                f'<tr style="background:#f5f0fb">'
                f'<td>{shared_label}</td>'
                + blank_pair * len(env_labels)
                + f'<td class="cost" style="color:#6b2fa0">{fmt_cost(alloc_amount)}</td>'
                + "</tr>"
            )

        content = f"""
        <table class="detail-table">
          <thead>
            <tr>
              <th rowspan="2">Project</th>
              {env_group_ths}
            </tr>
            <tr>{env_sub_ths}</tr>
          </thead>
          <tbody>{"".join(proj_rows)}</tbody>
        </table>"""

    return f"""
    <tr class="detail-row" id="detail-{pid}" style="display:none;">
      <td colspan="{total_cols}" class="detail-cell">
        <div class="detail-content">{content}</div>
      </td>
    </tr>"""


def render_overview_table(reports, period_str: str, table_id: str = "overview",
                           monthly_history: dict | None = None,
                           jwcc_pop: dict | None = None,
                           forecast: dict | None = None,
                           jwcc_shared: dict | None = None,
                           shared_alloc: dict | None = None,
                           proj_monthly: dict | None = None,
                           env_portfolio_monthly: dict | None = None,
                           env_portfolio_forecast: dict | None = None,
                           monthly_shared_per_portfolio: dict | None = None,
                           shared_forecast_per_portfolio: dict | None = None):
    """Overview table — monthly cost columns, JWCC POP total, 30-day forecast, trend sparkline."""
    # Collect portfolio names from both current reports and history
    all_portfolios: list[str] = []
    seen: set[str] = set()
    for r in reports:
        for p in r.get("comparison", {}).get("portfolios", []):
            name = p["portfolio"]
            if name not in seen and name.lower() != "null":
                all_portfolios.append(name)
                seen.add(name)
    if monthly_history:
        for name in monthly_history:
            if name not in seen and name.lower() != "null":
                all_portfolios.append(name)
                seen.add(name)

    if not all_portfolios:
        return ""

    all_portfolios = sorted(all_portfolios)
    env_labels     = [fmt_env(r.get("environment", "")) for r in reports]
    cross_matrix   = build_cross_env_matrix(reports)

    portfolio_project_counts: dict[str, int] = {
        pname: len(projs) for pname, projs in cross_matrix.items()
    }

    # Every month of the current Period of Performance, not a trailing window.
    #
    # This used to be the last three months, which was reasonable when history
    # was a few weeks deep. With a full period recorded it hid six of nine
    # months, and hid them silently: the header said Dec 2025 - Aug 2026 while
    # the columns showed Jun to Aug, and the only place the rest appeared was
    # the single POP total.
    #
    # Scoping to the period also means a rollover drops the previous period's
    # months rather than accumulating forever.
    all_months: list[str] = sorted(set(
        m for months in monthly_history.values() for m in months.keys()
    )) if monthly_history else []

    pop_month = render_pop_month()
    in_period = [m for m in all_months if m >= pop_month]
    display_months = (months_in_period(pop_month, in_period[-1])
                      if in_period else [])

    show_jwcc     = bool(jwcc_pop) or bool(jwcc_shared)
    show_forecast = bool(forecast)
    show_trend    = bool(monthly_history)

    # The right-hand columns are pinned by offset, and the offsets were fixed
    # numbers assuming all three were present. They are not: a period whose
    # first complete week has not arrived yet has no JWCC total, and an archive
    # has no forecast. The absent column still reserved its width, leaving the
    # remaining ones floating short of the edge. Stacking from the right by
    # what is actually rendered keeps them flush in every combination.
    _offset = 0
    _pins = {}
    for _shown, _var, _width in ((show_jwcc, "--pin-jwcc", 180),
                                 (show_trend, "--pin-trend", 96),
                                 (show_forecast, "--pin-forecast", 130)):
        if _shown:
            _pins[_var] = _offset
            _offset += _width
    pin_offsets = (' style="' + "".join(f"{k}:{v}px;" for k, v in _pins.items()) + '"'
                   if _pins else "")

    # toggle + name + count + months + [jwcc] + [forecast] + [trend]
    total_cols = (3 + len(display_months)
                  + (1 if show_jwcc else 0)
                  + (1 if show_forecast else 0)
                  + (1 if show_trend else 0))

    # Single-row header
    month_ths = "".join(
        f'<th class="right sortable" data-sort-key="m{i}-cost"'
        f' onclick="sortTable(\'{table_id}\', \'m{i}-cost\')">'
        f'{fmt_month(m)}{partial_month_mark(m)}'
        f'<span class="sort-ind"></span></th>'
        for i, m in enumerate(display_months)
    )
    jwcc_tip = (
        "JWCC Period of Performance (POP) total — attributed spend since the start of the current period. "
        "Includes direct portfolio spend plus proportionally-allocated shared infrastructure costs "
        "(expedition-0 and gitlabrunners projects)."
    )
    jwcc_th = (
        f'<th class="right sortable jwcc-col" data-sort-key="jwcc-cost"'
        f' onclick="sortTable(\'{table_id}\', \'jwcc-cost\')">'
        f'JWCC POP Total'
        f'<span class="help-tip" title="{jwcc_tip}">?</span>'
        f'<span class="sort-ind"></span></th>'
    ) if show_jwcc else ""
    forecast_th = (
        f'<th class="right sortable forecast-col" data-sort-key="forecast-cost"'
        f' onclick="sortTable(\'{table_id}\', \'forecast-cost\')"'
        f' title="Projected spend for the next 30 days">'
        f'30-Day Forecast{FORECAST_EST_MARK}<span class="sort-ind"></span></th>'
    ) if show_forecast else ""
    trend_th = '<th class="center trend-col">Trend</th>' if show_trend else ""

    rows_html = []
    for pname in all_portfolios:
        pid        = slug(pname)
        proj_count = portfolio_project_counts.get(pname, 0)

        sort_attrs = f'data-id="{pid}" data-name="{pname.lower()}" data-count="{proj_count}"'
        for i, month in enumerate(display_months):
            cost = monthly_history.get(pname, {}).get(month, 0) if monthly_history else 0
            sort_attrs += f' data-m{i}-cost="{cost:.2f}"'
        if show_jwcc:
            if jwcc_shared and pname in jwcc_shared:
                js = jwcc_shared[pname]
                jwcc_sort_val = js["actual"] + js["shared"]
            else:
                jwcc_sort_val = (jwcc_pop or {}).get(pname, 0)
            sort_attrs += f' data-jwcc-cost="{jwcc_sort_val:.2f}"'
        if show_forecast:
            sort_attrs += f' data-forecast-cost="{forecast.get(pname, 0):.2f}"'

        cells = [
            f'<td class="toggle-cell">'
            f'<button class="toggle-btn" id="toggle-{pid}"'
            f' onclick="toggleDetail(\'{pid}\')" title="Expand projects">&#9658;</button></td>',
            f'<td><strong>{pname}</strong></td>',
            f'<td class="count" title="{proj_count} project(s) tagged under this portfolio">'
            f'{proj_count}</td>',
        ]

        # A month before any environment was reporting has no data at all, so
        # showing $0.00 across the row claims a measurement nobody took. Same
        # distinction as the per-environment breakdown, one level up.
        earliest = min(ENV_DATA_START.values()) if ENV_DATA_START else None
        for month in display_months:
            if earliest and month < earliest:
                cells.append('<td class="neutral" title="No data — no '
                             'subscription was reporting costs yet">&mdash;</td>')
                continue
            cost = monthly_history.get(pname, {}).get(month, 0) if monthly_history else 0
            cells.append(cost_cell(cost))

        if show_forecast:
            fcost = forecast.get(pname, 0)
            cells.append(
                f'<td class="pin-forecast forecast-cost">{fmt_cost(fcost)}</td>'
                if fcost else '<td class="pin-forecast neutral">—</td>'
            )

        if show_trend:
            # Scoped to the displayed period, so the trend and the columns
            # beside it describe the same span rather than diverging after a
            # period rollover.
            sparkline_series = [
                (m, v) for m, v in sorted(monthly_history.get(pname, {}).items())
                if m in set(display_months)
            ]
            cells.append('<td class="pin-trend sparkline">'
                         f'{render_sparkline(sparkline_series)}</td>')

        if show_jwcc:
            if jwcc_shared and pname in jwcc_shared:
                js  = jwcc_shared[pname]
                tot = js["actual"] + js["shared"]
                sub = ""
                if js["shared"] > 0:
                    sub = (f'<br><small style="font-weight:400;color:#888;font-size:0.75em">'
                           f'({fmt_cost(js["actual"])} actual'
                           f' + {fmt_cost(js["shared"])} shared)</small>')
                cells.append(f'<td class="pin-jwcc jwcc-cost">{fmt_cost(tot)}{sub}</td>')
            elif jwcc_pop:
                jcost = jwcc_pop.get(pname, 0)
                cells.append(
                    f'<td class="pin-jwcc jwcc-cost">{fmt_cost(jcost)}</td>'
                    if jcost else '<td class="pin-jwcc neutral">—</td>'
                )
            else:
                cells.append('<td class="pin-jwcc neutral">—</td>')

        rows_html.append(
            f'<tr class="portfolio-row" {sort_attrs}>{"".join(cells)}</tr>'
        )
        rows_html.append(
            render_accordion_detail(
                pid, cross_matrix.get(pname, {}), env_labels, total_cols,
                shared_alloc=shared_alloc, portfolio_name=pname,
                proj_monthly=proj_monthly.get(pname) if proj_monthly else None,
                display_months=display_months if proj_monthly else None,
                portfolio_forecast=(forecast or {}).get(pname, 0) or None,
                env_portfolio_monthly=(env_portfolio_monthly or {}).get(pname) or None,
                env_portfolio_forecast=(env_portfolio_forecast or {}).get(pname) or None,
                monthly_shared=(monthly_shared_per_portfolio or {}).get(pname) or None,
                shared_forecast=(shared_forecast_per_portfolio or {}).get(pname) or None,
            )
        )

    if display_months:
        period_label = f'Monthly data: {fmt_month(display_months[0])} – {fmt_month(display_months[-1])}'
    else:
        period_label = f'Reporting period: {period_str}'

    return f"""
    <div class="overview-header">
      <h2>Cross-Subscription Portfolio Overview
        <span class="period-inline">({period_label})</span></h2>
      <input type="text" class="filter-input"
             placeholder="Filter by portfolio or project…"
             oninput="filterPortfolios(this, '{table_id}')">
    </div>
    <div class="table-scroll"{pin_offsets}>
    <table class="overview-table" id="{table_id}">
      <thead>
        <tr>
          <th class="toggle-col"></th>
          <th class="sortable" data-sort-key="name"
              onclick="sortTable('{table_id}', 'name')">
            Portfolio<span class="sort-ind"> ↑</span>
          </th>
          <th class="center sortable" data-sort-key="count"
              onclick="sortTable('{table_id}', 'count')">
            Projects<span class="sort-ind"></span>
          </th>
          {month_ths}
          {forecast_th}
          {trend_th}
          {jwcc_th}
        </tr>
      </thead>
      <tbody id="{table_id}-tbody">{"".join(rows_html)}</tbody>
    </table>
    </div>"""


def render_system_content(reports: list, period_str: str, table_id: str = "overview",
                           monthly_history: dict | None = None,
                           env_monthly: dict | None = None,
                           jwcc_pop: dict | None = None,
                           forecast: dict | None = None,
                           jwcc_shared: dict | None = None,
                           shared_alloc: dict | None = None,
                           proj_monthly: dict | None = None,
                           env_portfolio_monthly: dict | None = None,
                           env_portfolio_forecast: dict | None = None,
                           monthly_shared_per_portfolio: dict | None = None,
                           shared_forecast_per_portfolio: dict | None = None) -> str:
    summary_cards  = render_summary_cards(reports, env_monthly,
                                          forecast=forecast,
                                          jwcc_shared=jwcc_shared,
                                          jwcc_pop=jwcc_pop)
    overview_table = render_overview_table(reports, period_str, table_id,
                                           monthly_history, jwcc_pop, forecast,
                                           jwcc_shared, shared_alloc,
                                           proj_monthly=proj_monthly,
                                           env_portfolio_monthly=env_portfolio_monthly,
                                           env_portfolio_forecast=env_portfolio_forecast,
                                           monthly_shared_per_portfolio=monthly_shared_per_portfolio,
                                           shared_forecast_per_portfolio=shared_forecast_per_portfolio)
    return f"{summary_cards}\n{overview_table}"


def generate_html(reports: list, missing_environments=None,
                  monthly_history: dict | None = None,
                  env_monthly: dict | None = None, jwcc_pop: dict | None = None,
                  forecast: dict | None = None,
                  jwcc_shared: dict | None = None,
                  shared_alloc: dict | None = None,
                  proj_monthly: dict | None = None,
                  env_portfolio_monthly: dict | None = None,
                  env_portfolio_forecast: dict | None = None,
                  monthly_shared_per_portfolio: dict | None = None,
                  shared_forecast_per_portfolio: dict | None = None,
                  flagged_resources: dict | None = None,
                  pipeline_metrics: dict | None = None,
                  archive: bool = False,
                  prior_periods: list | None = None,
                  cluster_strip: str = "",
                  cluster_href: str = "cluster.html") -> str:
    if not reports:
        return "<html><body><p>No cost report data found.</p></body></html>"

    first     = reports[0]
    generated = first.get("report_generated_utc",
                          datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    # Period string: show monthly range when history is available, else fall back to current week
    if monthly_history:
        all_months = sorted(set(m for ms in monthly_history.values() for m in ms.keys()))
        period_str = (f"{fmt_month(all_months[0])} – {fmt_month(all_months[-1])}"
                      if all_months else "No data")
    else:
        cur        = first.get("current_period", {})
        period_str = f'{cur.get("start", "?")} – {cur.get("end", "?")}'

    systems = group_by_system(reports)

    if len(systems) > 1:
        tab_buttons, tab_panels = [], []
        for i, (label, sys_reports) in enumerate(systems):
            tab_id  = slug(label)
            active  = "active" if i == 0 else ""
            tbl_id  = f"overview-{tab_id}"
            tab_buttons.append(
                f'<button class="tab-btn {active}" onclick="showTab(\'{tab_id}\', this)">{label}</button>'
            )
            content = render_system_content(sys_reports, period_str, tbl_id,
                                            monthly_history, env_monthly, jwcc_pop, forecast,
                                            jwcc_shared, shared_alloc,
                                            proj_monthly=proj_monthly,
                                            env_portfolio_monthly=env_portfolio_monthly,
                                            env_portfolio_forecast=env_portfolio_forecast,
                                            monthly_shared_per_portfolio=monthly_shared_per_portfolio,
                                            shared_forecast_per_portfolio=shared_forecast_per_portfolio)
            tab_panels.append(f'<div id="tab-{tab_id}" class="tab-content {active}">{content}</div>')
        main_html = f'<div class="tabs">{"".join(tab_buttons)}</div>{"".join(tab_panels)}'
    else:
        _, sys_reports = systems[0]
        main_html = render_system_content(sys_reports, period_str,
                                          monthly_history=monthly_history,
                                          env_monthly=env_monthly,
                                          jwcc_pop=jwcc_pop,
                                          forecast=forecast,
                                          jwcc_shared=jwcc_shared,
                                          shared_alloc=shared_alloc,
                                          proj_monthly=proj_monthly,
                                          env_portfolio_monthly=env_portfolio_monthly,
                                          env_portfolio_forecast=env_portfolio_forecast,
                                          monthly_shared_per_portfolio=monthly_shared_per_portfolio,
                                          shared_forecast_per_portfolio=shared_forecast_per_portfolio)

    # Deliberately outside the collapsed info panel. An environment missing from
    # this week's figures changes how every total on the page should be read, so
    # it cannot sit behind a disclosure triangle.
    missing_banner = ""
    if missing_environments:
        names = ", ".join(fmt_env(e) for e in sorted(missing_environments))
        missing_banner = (
            '\n  <p class="missing-banner">⚠️ Cost data for '
            f'<strong>{escape(names)}</strong> is missing from this run. '
            "Current-period figures and totals below exclude it; monthly history "
            "for those environments is unaffected.</p>"
        )

    # A hole inside the recorded range understates whichever month contains it,
    # and unlike a missing environment there is nothing on the page that would
    # otherwise hint at it.
    gap_banner = ""
    if COVERAGE_GAPS:
        parts = []
        for env, weeks in sorted(COVERAGE_GAPS.items()):
            shown = ", ".join(weeks[:4])
            more  = f" +{len(weeks) - 4} more" if len(weeks) > 4 else ""
            parts.append(f"<strong>{escape(env)}</strong> {len(weeks)} week(s) "
                         f"({escape(shown)}{escape(more)})")
        gap_banner = (
            '\n  <p class="missing-banner">⚠️ Gaps in recorded history: '
            + "; ".join(parts)
            + ". Months containing those weeks are understated — a backfill "
              "over the affected range will fill them.</p>"
        )

    # An archive has to say what it is on its face. Someone arriving from a
    # link, or on a stale tab, must not read a closed period as current.
    archive_title = " (archive)" if archive else ""
    archive_mark  = ' <span class="archive-mark">closed period</span>' if archive else ""
    archive_nav   = ('\n    <p class="archive-nav">'
                     '<a href="../../index.html">&larr; Current period</a></p>'
                     if archive else "")

    # The two pages describe the same money at different granularities, so the
    # link between them is the layout. An archive keeps its own back-link
    # instead: cluster data is current state and has no place in a closed record.
    # Spaced by the caller so an absent panel leaves the page byte-identical
    # to one built before it existed, which is what makes the regression check
    # on the cost page meaningful.
    cluster_block = f"\n  {cluster_strip}" if cluster_strip and not archive else ""

    page_nav = archive_nav
    if not archive:
        cluster_link = (
            f'<a href="{escape(cluster_href)}">Cluster utilization &rarr;</a> · '
            if cluster_strip else ""
        )
        page_nav = (
            '\n    <p class="cluster-nav">'
            f'{cluster_link}'
            '<a href="image-status.html">Image status &rarr;</a></p>'
        )

    prov = forecast_provenance(reports)
    if not prov["known"]:
        forecast_note = "<p>No forecast data was returned for this period.</p>"
    elif prov["basis"] == "allocated":
        forecast_note = (
            "<p>Azure returns the 30-day forecast as a <strong>single total per "
            "subscription</strong>, with no breakdown by portfolio. The "
            "per-portfolio figures marked <strong>est.</strong> are therefore "
            "not measured: each subscription's forecast is split across its "
            "portfolios in proportion to their share of current-period spend. "
            "A portfolio whose spending pattern is about to change will be "
            "estimated poorly, and the split says nothing Azure did not.</p>"
        )
    elif prov["basis"] == "measured":
        forecast_note = ("<p>Azure returned the forecast broken down by "
                         "portfolio, so these figures are as reported.</p>")
    else:
        # A report from before the collector recorded provenance. Claiming
        # either way would be a guess, and asserting "as reported" is precisely
        # the false confidence this is meant to remove.
        forecast_note = ("<p>These figures come from a collector run that did "
                         "not record how the forecast was derived, so whether "
                         "they are measured per portfolio or split from a "
                         "subscription total is not known. The next run will "
                         "say.</p>")

    if prov["known"] and (prov["actual"] or prov["unclassified"]):
        total = prov["actual"] + prov["forecast"] + prov["unclassified"]
        share = (prov["actual"] / total * 100) if total else 0
        forecast_note += (
            f"<p>The window starts today, so part of it has already happened: "
            f"<strong>{fmt_cost(prov['actual'])}</strong> of the total is spend "
            f"Azure has already recorded ({share:.0f}%), the rest is projected.</p>"
        )

    # Present whenever an archive exists, not only at rollover. The failure this
    # guards against is someone concluding a year of data was lost, and by then
    # a notice that appears only at the boundary has already been missed.
    prior_panel = ""
    prior_dl    = ""
    if prior_periods and not archive:
        # Newest first, and only the three most recent are shown. A period of
        # performance is a year, so this stays quiet for three years and then
        # grows behind an expander rather than into the page.
        ordered = sorted(prior_periods, key=lambda p: p.get("start", ""), reverse=True)

        def _link(p):
            return (f'<li><a href="archive/{escape(p["label"])}/index.html">'
                    f'JWCC POP {escape(p["label"])}</a>'
                    f' <span class="prior-range">{escape(p["start"])} to '
                    f'{escape(p["end"])}</span></li>')

        links = "".join(_link(p) for p in ordered[:3])
        rest  = ordered[3:]
        more  = ""
        if rest:
            more = ('<details class="prior-more"><summary>'
                    f'{len(rest)} earlier period(s)</summary>'
                    f'<ul>{"".join(_link(p) for p in rest)}</ul></details>')
        prior_panel = (
            '\n  <section class="prior-periods">'
            '<h2>Previous periods</h2>'
            '<p>Earlier periods of performance are kept in full and stay '
            'available. This page shows the current period only.</p>'
            f'<ul>{links}</ul>{more}</section>'
        )
        prior_dl = (
            '<li class="prior-dl">Earlier periods: '
            + ", ".join(f'<a href="archive/{escape(p["label"])}/index.html">'
                        f'{escape(p["label"])}</a>' for p in prior_periods)
            + '</li>'
        )

    dl_items = "".join(
        f'<li><a href="data/{r.get("environment","unknown")}.json"'
        f' download="{r.get("environment","unknown")}.json">'
        f'{fmt_env(r.get("environment","unknown"))} — cost history (JSON)</a></li>'
        for r in sort_reports_by_env(reports)
    )
    # Resource findings describe the subscription as it is now. On a closed
    # period they would be current data under a historical heading, which is
    # worse than omitting them.
    flagged_panel = ("" if archive
                     else render_flagged_resources_panel(flagged_resources or {}))
    pipeline_panel = ("" if archive
                      else render_pipeline_metrics_panel(pipeline_metrics))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Expedition-0 KPI Report{archive_title} — {period_str}</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📊</text></svg>">
  <style>{CSS}</style>
</head>
<body>
  <header>
    <h1>Expedition-0 KPI Report</h1>
    <p class="period-str">JWCC POP Period: {period_str}{archive_mark}</p>{page_nav}
    <p class="generated">Generated: {generated}</p>
  </header>
{missing_banner}{gap_banner}

  <details class="info-panel">
    <summary class="info-panel-summary">
      <span class="info-panel-chevron">&#9658;</span>
      ℹ️ Important Information
      <span class="info-panel-hint">Tag data latency · Page guide · Downloads</span>
    </summary>
    <div class="info-panel-body">
      <div class="info-section">
        <h4>⚠️ Tag Data Latency</h4>
        <p>Azure Cost Management reflects resource tag values with a <strong>24–48 hour delay</strong>.
        Tag changes made recently may not yet appear in this report.
        Incorrect or misspelled tag values will show as separate rows until corrected directly
        on the resource in Azure.</p>
      </div>
      <div class="info-section">
        <h4>📖 Page Guide</h4>
        <ul>
          <li><strong>▶ row toggle</strong> — Expand a portfolio row to see project-level cost detail.</li>
          <li><strong>? icons</strong> — Hover over a circled ? for an inline explanation of that column or value.</li>
          <li><strong>Trend sparkline</strong> — Mini chart showing monthly cost trajectory over the displayed period.</li>
          <li><strong>⊕ Shared Infrastructure</strong> — Expedition-0 and gitlabrunners costs, allocated proportionally by active project count each week.</li>
          <li><strong>JWCC POP Total</strong> — Cumulative attributed spend since Dec 1 of the prior year (start of JWCC Period of Performance). Includes direct portfolio spend plus allocated shared infrastructure.</li>
          <li><strong>Column headers</strong> — Click any underlined header to sort the portfolio table by that column.</li>
          <li><strong>- in a monthly column</strong> — No data was recorded for that
          environment in that month, which is not the same as spending nothing. A
          subscription created partway through the reporting period has no data before it
          existed; a month it was measured in and cost nothing shows $0.00.</li>
        </ul>
      </div>
      <div class="info-section">
        <h4>🔮 How the forecast is produced</h4>
        {forecast_note}
      </div>
      <div class="info-section">
        <h4>⬇️ Download Data</h4>
        <p>Raw JSON history files used to generate this dashboard:</p>
        <ul>{dl_items}{prior_dl}</ul>
      </div>
    </div>
  </details>


  {main_html}
{cluster_block}
  {pipeline_panel}

  {flagged_panel}
{prior_panel}

  <script>{JS}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_page(reports: list, history_dir: Path, flagged_resources=None,
               missing_envs=None, archive: bool = False,
               prior_periods: list | None = None,
               pipeline_metrics: dict | None = None,
               cluster_strip: str = "") -> str:
    """Load everything the page needs from history, and render it.

    Shared by the live page and the archive pages so the two cannot drift
    apart. An archive passes a history directory holding a single closed
    period, reports synthesized by reports_from_history(), and no resource
    findings — those describe current state and have no place in a closed
    record.

    RENDER_POP must already point at the period being rendered: every
    JWCC-scoped figure below reads it.
    """
    global FORECAST_EST_MARK

    # Render-scoped, and a process may render several pages in a row.
    ENV_DATA_START.clear()
    COVERAGE_GAPS.clear()
    ENV_POP_TOTALS.clear()
    FORECAST_EST_MARK = ""

    monthly_history = load_monthly_portfolio_history(history_dir)
    if monthly_history:
        print(f"[INFO] Loaded monthly history for {len(monthly_history)} portfolio(s)")
    else:
        print("[INFO] No portfolio history found — monthly columns and sparklines will be hidden")

    # State the period being rendered and where it came from. If the variable
    # is not visible to the job — unset, or protected on an unprotected branch —
    # the code silently falls back to its December 1 default and the page looks
    # unchanged, which is indistinguishable from the change not having been made.
    _anchor = os.environ.get("JWCC_POP_START", "").strip()
    _pop_from, _pop_to = pop_periods()[-1]
    print(f"[INFO] Reporting period {_pop_from} to {_pop_to} "
          + (f"(JWCC_POP_START={_anchor})" if _anchor
             else "(JWCC_POP_START not set — using the December 1 default)"))

    ENV_POP_TOTALS.update(load_env_pop_totals(history_dir))

    env_monthly = load_env_monthly_totals(history_dir)
    if env_monthly:
        print(f"[INFO] Loaded monthly env totals for {len(env_monthly)} environment(s)")
    else:
        print("[INFO] No env history found — stat cards will fall back to weekly data")

    shared_alloc = compute_shared_allocation(reports)
    if shared_alloc:
        print(f"[INFO] Shared cost allocation: {fmt_cost(shared_alloc['shared_total'])} "
              f"across {shared_alloc['total_projects']} project(s) "
              f"({fmt_cost(shared_alloc['per_project'])} per project)")
    else:
        print("[INFO] No shared project costs found — shared allocation skipped")

    jwcc_shared = load_jwcc_pop_with_shared(history_dir)
    if jwcc_shared:
        print(f"[INFO] Loaded JWCC POP with shared allocation for {len(jwcc_shared)} portfolio(s)")
    else:
        print("[INFO] No JWCC POP history found — JWCC POP column will be hidden")

    jwcc_pop = load_jwcc_pop_totals(history_dir)

    forecast = get_adjusted_forecast_by_portfolio(reports)
    if forecast:
        print(f"[INFO] Loaded forecast for {len(forecast)} portfolio(s) (shared costs excluded)")
    else:
        print("[INFO] No forecast data in reports — Forecast column will be hidden")

    proj_monthly = load_monthly_project_history(history_dir)
    if proj_monthly:
        print(f"[INFO] Loaded monthly project history for {len(proj_monthly)} portfolio(s)")
    else:
        print("[INFO] No project history found — project accordion will use current-week layout")

    env_portfolio_monthly = load_env_portfolio_monthly(history_dir)
    if env_portfolio_monthly:
        print(f"[INFO] Loaded env/portfolio monthly breakdown for {len(env_portfolio_monthly)} portfolio(s)")
    else:
        print("[INFO] No env/portfolio history found — env breakdown table will be hidden")

    env_portfolio_forecast = get_env_portfolio_forecast(reports)
    if env_portfolio_forecast:
        print(f"[INFO] Computed env/portfolio forecast for {len(env_portfolio_forecast)} portfolio(s)")

    monthly_shared_per_portfolio = load_monthly_shared_per_portfolio(history_dir)
    if monthly_shared_per_portfolio:
        print(f"[INFO] Loaded monthly shared allocation for {len(monthly_shared_per_portfolio)} portfolio(s)")
    else:
        print("[INFO] No shared project history found — shared row will show current-week only")

    shared_forecast_per_portfolio = get_shared_forecast_per_portfolio(reports)
    if shared_forecast_per_portfolio:
        print(f"[INFO] Computed shared infrastructure forecast for {len(shared_forecast_per_portfolio)} portfolio(s)")

    # Set before rendering: the marker is read by every forecast heading.
    ENV_DATA_START.update(environment_data_start(history_dir))
    if ENV_DATA_START:
        print(f"[INFO] Environment data begins: "
              + ", ".join(f"{e} {m}" for e, m in sorted(ENV_DATA_START.items())))

    COVERAGE_GAPS.update(coverage_gaps(history_dir))
    if COVERAGE_GAPS:
        for env, weeks in sorted(COVERAGE_GAPS.items()):
            shown = ", ".join(weeks[:8])
            more  = f" (+{len(weeks) - 8} more)" if len(weeks) > 8 else ""
            print(f"[WARN] {env} history is missing {len(weeks)} week(s): "
                  f"{shown}{more} — months containing them total low",
                  file=sys.stderr)
    else:
        print("[INFO] History is contiguous for every environment")

    _prov = forecast_provenance(reports)
    if _prov["basis"] == "allocated":
        FORECAST_EST_MARK = ('<span class="est-mark" title="Estimated — Azure '
                             'returns one forecast per subscription; this is '
                             'split across portfolios by current-period share">'
                             ' est.</span>')
        print(f"[INFO] Forecast is derived, not measured — "
              f"{fmt_cost(_prov['actual'])} of the total is already-recorded spend",
              file=sys.stderr)

    return generate_html(reports, missing_environments=missing_envs,
                         monthly_history=monthly_history, env_monthly=env_monthly,
                         jwcc_pop=jwcc_pop, forecast=forecast,
                         jwcc_shared=jwcc_shared, shared_alloc=shared_alloc,
                         proj_monthly=proj_monthly,
                         env_portfolio_monthly=env_portfolio_monthly,
                         env_portfolio_forecast=env_portfolio_forecast,
                         monthly_shared_per_portfolio=monthly_shared_per_portfolio,
                         shared_forecast_per_portfolio=shared_forecast_per_portfolio,
                         flagged_resources=flagged_resources,
                         pipeline_metrics=pipeline_metrics,
                         archive=archive, prior_periods=prior_periods,
                         cluster_strip=cluster_strip)


def cluster_comparison(reports: list, summary: dict) -> dict | None:
    """What this page reported over exactly the cluster collector's window.

    The share of the bill that is Kubernetes is the most useful number the
    summary strip can carry, and the easiest to get wrong. The two collectors do
    not read the same period by construction: the cost collector reads an ISO
    Monday-to-Sunday week, the cluster collector a trailing seven days ending
    yesterday. Those coincide on the Monday schedule and diverge on every other
    day. Dividing one by the other regardless would state a confident percentage
    over two different weeks — the same defect as a monthly total quietly
    missing a week and still looking plausible.

    So a figure comes back only when both sides describe the same days, and it
    is scoped to the environments that reported on both. An environment missing
    from either side is excluded from both, and the strip says so rather than
    dividing across two different sets of subscriptions. None is not a failure:
    the strip then reports the spend without a share and explains why.

    The denominator is what this page reports, which is tagged spend — untagged
    rows are dropped upstream, about 3.6% of the bill as last measured. That is
    the right comparison anyway, because the reader is being told what share of
    the figures in front of them is Kubernetes.
    """
    window = summary.get("window") or {}
    start, end = window.get("from"), window.get("to")
    if not start or not end:
        return None

    cost_by_env: dict = {}
    for r in reports:
        cp = r.get("current_period") or {}
        if cp.get("start") != start or cp.get("end") != end:
            print(f"[INFO] Cluster window {start}..{end} does not match the cost "
                  f"period {cp.get('start')}..{cp.get('end')} — the summary will "
                  "not state a share")
            return None
        cost_by_env[env_key(r.get("environment", ""))] = sum(
            pf.get("total_cost", 0) for pf in cp.get("portfolios") or [])

    cluster_envs = {env_key(e) for e in (summary.get("per_env_window_spend") or {})}
    shared = cluster_envs & set(cost_by_env)
    if not shared:
        print("[INFO] No environment reported both cost and cluster data — the "
              "summary will not state a share")
        return None

    total = sum(cost_by_env[e] for e in shared)
    if not total:
        return None
    partial = shared != cluster_envs or shared != set(cost_by_env)
    if partial:
        print(f"[INFO] Kubernetes share is scoped to {sorted(shared)} — cost "
              f"environments {sorted(cost_by_env)}, cluster environments "
              f"{sorted(cluster_envs)}")
    return {"total": total, "environments": sorted(shared), "partial": partial}


if __name__ == "__main__":
    repo_root    = Path(os.environ.get("CI_PROJECT_DIR", "."))
    reports_dir  = repo_root / "cost_reports"
    history_dir  = repo_root / "cost_history"
    public_dir   = repo_root / "public"

    public_dir.mkdir(parents=True, exist_ok=True)

    # History has seen every week's spellings, so it decides how each portfolio
    # is displayed; the reports then reuse that decision.
    hist_portfolio_names, hist_project_names = history_display_names(history_dir)
    reports = load_reports(reports_dir, hist_portfolio_names, hist_project_names)
    if not reports:
        # Deliberately still a hard failure. The page is built from the current
        # period's reports — history only fills columns within rows those reports
        # create — so with none of them there is nothing to render but an empty
        # shell. Failing here leaves the previously published page in place,
        # which is strictly more useful than replacing it with a blank one.
        # Rendering from history alone would need the page restructured to be
        # history-driven, which is Phase 5 work.
        if load_history(history_dir):
            print("[ERROR] No cost reports in cost_reports/ — every cost collector "
                  "failed this run.", file=sys.stderr)
            print("[ERROR] History is on the data branch, but the page is built from "
                  "the current period. Leaving the previously published page in "
                  "place.", file=sys.stderr)
        else:
            print("[ERROR] No cost reports and no history — nothing to render.",
                  file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Loaded {len(reports)} report(s): {[r.get('environment') for r in reports]}")

    # Which environments should have reported resources, so a missing report is
    # distinguishable from an empty one.
    reported_envs = {r.get("environment") for r in reports if r.get("environment")}
    flagged_resources = load_flagged_resources(repo_root, reported_envs)
    pipeline_metrics = load_pipeline_metrics_report(repo_root)

    # History is the record of which environments this pipeline collects, so an
    # environment with history but no report this run is a collector that did
    # not deliver — not an environment that no longer exists. Without this the
    # page silently drops it and every cross-environment total is quietly short.
    known_envs = {e.lower() for e in load_history(history_dir)}
    missing_envs = sorted(e for e in known_envs
                          if e not in {r.lower() for r in reported_envs})
    if missing_envs:
        print(f"[WARN] No cost report this run for: {', '.join(missing_envs)} — "
              f"current-period figures exclude them", file=sys.stderr)

    # Frozen periods, if any. Read from the archive root rather than from the
    # rendered pages so a failed render does not silently drop a period from
    # the list — the link would be broken, which is visible, rather than the
    # period appearing never to have existed, which is not.
    archive_dir = Path(os.environ.get("ARCHIVE_DIR", "archive"))
    prior_periods = archived_periods(archive_dir)
    if prior_periods:
        print(f"[INFO] Linking {len(prior_periods)} archived period(s): "
              + ", ".join(p["label"] for p in prior_periods))

    # The Kubernetes clusters, as a drill-down rather than a second subject.
    # A quarter of the bill is the clusters and this page cannot say which
    # quarter or whether it is being used — so the detail goes on its own page
    # and only a summary sits here. Keeping both granularities on one page is
    # what made it congested.
    cluster_strip = ""
    if CLUSTER_PANEL_AVAILABLE:
        cluster_reports = load_cluster_reports(repo_root / "cluster_reports")
        if not cluster_reports:
            print("[INFO] No cluster reports this run — cluster page and summary "
                  "omitted")
        else:
            print(f"[INFO] Loaded {len(cluster_reports)} cluster report(s): "
                  f"{sorted(cluster_reports)}")
            summary = cluster_summary(cluster_reports)
            comparison = cluster_comparison(reports, summary)
            if comparison:
                scoped = sum(
                    v for e, v in summary["per_env_window_spend"].items()
                    if env_key(e) in set(comparison["environments"]))
                print(f"[INFO] Kubernetes is {fmt_cost(scoped)} of "
                      f"{fmt_cost(comparison['total'])} reported "
                      f"({scoped / comparison['total'] * 100:.1f}%) over "
                      f"{summary['window'].get('from')} to "
                      f"{summary['window'].get('to')}")
            cluster_strip = render_cluster_strip(cluster_reports, comparison)
            cluster_out = public_dir / "cluster.html"
            cluster_out.write_text(
                render_cluster_page(cluster_reports, css=CSS,
                                    generated=next(
                                        (r.get("collected_utc", "")
                                         for r in cluster_reports.values()
                                         if r.get("collected_utc")), ""),
                                    back_href="index.html"),
                encoding="utf-8")
            print(f"[INFO] Cluster page written to {cluster_out} "
                  f"({cluster_out.stat().st_size:,} bytes)")

    html = build_page(reports, history_dir, flagged_resources,
                      missing_envs=missing_envs, prior_periods=prior_periods,
                      pipeline_metrics=pipeline_metrics,
                      cluster_strip=cluster_strip)
    out  = public_dir / "index.html"
    out.write_text(html, encoding="utf-8")

    print(f"[INFO] Dashboard written to {out}")

    # The page links to data/<environment>.json for each environment. Those used
    # to be copied straight from cost_history/, but sharding moved the files
    # into per-environment directories and the copy silently stopped matching,
    # leaving the links pointing at nothing. Writing them here keeps the file
    # and the link that references it in one place. The download is the stored
    # history as-is, not the render-time merged view.
    data_dir = public_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for env_dir in sorted(d for d in history_dir.iterdir() if d.is_dir()) if history_dir.is_dir() else []:
        snapshots = []
        for shard in sorted(env_dir.glob("*.json")):
            try:
                snapshots.append(json.loads(shard.read_text()))
            except Exception as e:
                print(f"[WARN] Could not read {shard}: {e}", file=sys.stderr)
        if not snapshots:
            continue
        snapshots.sort(key=lambda s: s.get("period_start", ""))
        (data_dir / f"{env_dir.name}.json").write_text(
            json.dumps(snapshots, indent=2), encoding="utf-8")
        print(f"[INFO] Download bundle written: data/{env_dir.name}.json "
              f"({len(snapshots)} week(s))")
    flagged_out = public_dir / "flagged_resources.json"
    flagged_out.write_text(json.dumps(flagged_resources, indent=2), encoding="utf-8")
    print(f"[INFO] Flagged resources written to {flagged_out}")
