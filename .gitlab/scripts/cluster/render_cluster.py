"""
The cluster utilization panel, and a standalone preview of it.

`render_cluster_panel(reports)` returns an HTML fragment and nothing else — no
document, no file, no side effects. That is the whole integration story: this
same function is what `generate_report.py` will call, so putting the panel on
the page costs an import and one placeholder rather than a rewrite.

Two consequences of that rule:

  * The panel carries its own scoped <style>. Adding rules to the page's CSS
    constant would make integration a diff in two files instead of one, and the
    preview would then need those rules copied to look right.
  * Nothing here imports generate_report, which will import this. The few
    formatting helpers are duplicated rather than shared, which is the cheaper
    of the two problems.

Run directly, it writes cluster_preview/index.html — the panel inside the page's
own CSS, so it can be judged looking like the real thing without being on it.

What the panel deliberately does not show: a single headline "wasted spend"
figure. Requests are an incomplete picture of what a cluster is doing — pods
that declare no request consume capacity while claiming none — so one number
derived from them would carry a precision it has not earned. The components are
shown instead, each honest on its own, and the caveat travels with them.
"""

import json
import os
import sys
from collections import defaultdict
from html import escape
from pathlib import Path

REPORTS_DIR = Path(os.environ.get("CLUSTER_REPORTS_DIR", "cluster_reports"))
PREVIEW_DIR = Path(os.environ.get("CLUSTER_PREVIEW_DIR", "cluster_preview"))

PANEL_CSS = """
/* Uses the page's palette tokens rather than defining colors of its own, so the
   two pages cannot drift apart. They are declared in generate_report.CSS, which
   both the published page and the preview load; an unstyled preview is the
   documented fallback when that import fails. */
.cluster-panel { margin: 0; }
.cluster-panel h3 {
  margin: 2rem 0 .2rem; font-size: 1rem; color: var(--ink-strong);
  border-bottom: 2px solid var(--border); padding-bottom: .4rem;
}
.cluster-panel h3 .h3-num {
  display: inline-block; width: 1.4rem; color: var(--ink-faint); font-weight: 600;
}
.cluster-lede { margin: .5rem 0 .8rem; font-size: .85rem; color: var(--ink-muted);
                max-width: 78ch; line-height: 1.5; }
.cluster-lede strong { color: var(--ink); }
.cluster-note { margin: .5rem 0 0; font-size: .78rem; color: var(--ink-faint); }

/* ── Tiles ─────────────────────────────────────────────────── */
.cluster-tiles { display: flex; flex-wrap: wrap; gap: .75rem; margin: 0 0 1rem; }
.cluster-tile {
  flex: 1 1 190px; padding: .75rem .95rem; border-radius: 8px;
  background: var(--paper); border: 1px solid var(--border);
  border-top: 3px solid var(--header);
}
.cluster-tile .t-label { font-size: .68rem; color: var(--ink-faint);
  text-transform: uppercase; letter-spacing: .05em; }
.cluster-tile .t-value { font-size: 1.35rem; font-weight: 700;
  color: var(--ink-strong); line-height: 1.25; }
.cluster-tile .t-sub   { font-size: .7rem; color: var(--ink-faint); }

/* ── Tables ────────────────────────────────────────────────── */
.cluster-scroll { overflow-x: auto; }
.cluster-table { width: 100%; border-collapse: collapse; font-size: .8rem;
                 background: var(--paper); }
.cluster-table th {
  text-align: left; background: var(--header); color: var(--on-dark);
  padding: .45rem .55rem; font-weight: 600; white-space: nowrap;
  font-size: .7rem; text-transform: uppercase; letter-spacing: .04em;
  border: 1px solid var(--header);
}
.cluster-table th.sortable { cursor: pointer; user-select: none; }
.cluster-table th.sortable:hover { background: var(--header-hover); }
.cluster-table th .sort-ind { opacity: .85; margin-left: 3px; font-size: .85em; }
.cluster-table td { padding: .38rem .55rem; border: 1px solid var(--border);
                    vertical-align: middle; }
.cluster-table td.num { text-align: right; font-variant-numeric: tabular-nums;
                        white-space: nowrap; }
/* The Kind cell lists every application on a pool, and a pool hosting
   seventeen of them pushed the table wide enough to need horizontal scrolling
   to reach the capacity figures — which are the reason to read the table at
   all. Capped and wrapped, so a long list grows the row instead of the table.
   The pill has to wrap internally too, or it just overflows the cap. */
.cluster-table td.kind-cell { max-width: 22ch; }
.cluster-table td.kind-cell .pill { white-space: normal; display: inline;
                                    line-height: 1.5; }
.cluster-table tbody tr:hover > td { background: var(--row-hover); }
.cluster-table tr.sub td { color: var(--ink-muted); font-size: .76rem;
                           background: var(--surface); }
.cluster-table tr.sub:hover > td { background: var(--surface); }
/* The two capacity gaps have different owners, so they are tinted apart from
   the measurements they are derived from. */
.cluster-table td.gap-platform { background: rgba(26,92,56,.06); }
.cluster-table td.gap-team     { background: rgba(138,109,59,.08); }
.cluster-table tbody tr:hover > td.gap-platform,
.cluster-table tbody tr:hover > td.gap-team { background: var(--row-hover); }
/* The qualifier line wraps while the label does not, so "cores · app teams"
   cannot set the column width. Without this, the units added to fix an
   ambiguous header would have pushed the table back into horizontal scrolling
   — which is what capping the Kind column was for. */
.col-group { font-size: .62rem; letter-spacing: .06em; opacity: .8; display: block;
             font-weight: 500; white-space: normal; }

/* ── Expandable rows ───────────────────────────────────────── */
.cl-toggle { background: none; border: none; cursor: pointer; font-size: .7rem;
             color: var(--ink-muted); padding: 2px 5px; border-radius: 4px;
             line-height: 1; }
.cl-toggle:hover { background: var(--row-hover); color: var(--accent); }
.cl-toggle.open  { color: var(--accent); }
.cl-detail > td { padding: 0 !important; background: var(--surface); }
.cl-detail-inner { padding: .6rem .8rem .7rem 2.2rem; }
.cl-detail-inner table { font-size: .76rem; background: transparent; }
.cl-detail-inner th { background: var(--header-alt); border-color: var(--header-alt); }
.cl-detail-label { font-size: .66rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .07em; color: var(--ink-muted); margin: 0 0 .35rem; }

/* ── Filter bar ────────────────────────────────────────────── */
.cluster-filters {
  display: flex; flex-wrap: wrap; align-items: center; gap: .6rem;
  margin: 1rem 0 0; padding: .6rem .8rem; border-radius: 8px;
  background: var(--surface); border: 1px solid var(--border);
  font-size: .8rem; color: var(--ink-muted);
}
.cluster-filters label { font-weight: 600; color: var(--ink); }
.cluster-filters select {
  font: inherit; padding: .25rem .5rem; border-radius: 5px;
  border: 1px solid var(--border-strong); background: var(--paper);
  color: var(--ink);
}
.cluster-filters select:focus { outline: none; border-color: var(--accent);
                                box-shadow: 0 0 0 2px var(--accent-ring); }
.filter-count { margin-left: auto; font-size: .76rem; color: var(--ink-faint); }

/* ── Strip on the cost page ────────────────────────────────── */
.cluster-strip { margin: 2rem 0 0; padding: 1rem 1.1rem; border-radius: 10px;
  background: var(--surface-soft); border: 1px solid var(--border);
  border-left: 4px solid var(--header); }
.cluster-strip h2 { margin: 0 0 .25rem; font-size: 1.05rem; border: none;
                    padding: 0; color: var(--ink-strong); }
.cluster-strip .cluster-note { margin: 0 0 .8rem; font-size: .84rem;
                               color: var(--ink-muted); max-width: 82ch;
                               line-height: 1.5; }
.cluster-strip .cluster-tiles { margin-bottom: .6rem; }
.cluster-more { display: inline-block; font-size: .84rem; font-weight: 700;
  color: var(--accent); text-decoration: none; }
.cluster-more:hover { text-decoration: underline; }
/* ── Pills ─────────────────────────────────────────────────── */
.pill { display: inline-block; padding: .05rem .45rem; border-radius: 10px;
  font-size: .68rem; font-weight: 600; white-space: nowrap; }
.pill-app      { background: var(--pill-app-bg);      color: var(--pill-app-ink); }
.pill-platform { background: var(--pill-platform-bg); color: var(--pill-platform-ink); }
.pill-system   { background: var(--pill-system-bg);   color: var(--pill-system-ink); }
.pill-shared   { background: var(--pill-shared-bg);   color: var(--pill-shared-ink); }
.pill-idle     { background: var(--pill-idle-bg);     color: var(--pill-idle-ink); }
.pill-warn     { background: var(--pill-warn-bg);     color: var(--pill-warn-ink); }

/* ── The reading guide ─────────────────────────────────────── */
.cluster-guide {
  background: var(--surface-soft); border-left: 4px solid var(--accent);
  border-radius: 6px; margin: 0 0 1.5rem; overflow: hidden;
}
.cluster-guide > summary {
  padding: .7rem 1rem; cursor: pointer; font-size: .88rem; font-weight: 700;
  color: var(--ink-strong); list-style: none; display: flex; align-items: center;
  gap: .5rem; user-select: none;
}
.cluster-guide > summary::-webkit-details-marker { display: none; }
.cluster-guide .g-chevron { font-size: .7em; display: inline-block;
                            transition: transform .2s; color: var(--accent); }
.cluster-guide[open] .g-chevron { transform: rotate(90deg); }
.cluster-guide .g-hint { font-size: .77rem; color: var(--ink-muted);
                         font-weight: 400; margin-left: auto; }
.cluster-guide-body {
  padding: 2px 1rem 1rem; display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem 1.5rem;
}
.guide-section h4 {
  font-size: .75rem; font-weight: 700; color: var(--ink-strong); margin: 0 0 .35rem;
  text-transform: uppercase; letter-spacing: .06em;
  border-bottom: 1px solid var(--border); padding-bottom: 3px;
}
.guide-section p, .guide-section ul { font-size: .82rem; color: var(--ink);
                                      margin: 0; line-height: 1.55; }
.guide-section ul { padding-left: 1rem; margin-top: .25rem; }
.guide-section li { margin-bottom: .2rem; }
.empty-state { font-size: .84rem; color: var(--ink-muted); font-style: italic;
               padding: .5rem 0; }
/* Filtering and expansion are separate mechanisms on purpose: a detail row is
   collapsed with [hidden] and filtered with this class, so filtering never
   silently expands a row and expanding never defeats the filter. */
.cl-hidden { display: none !important; }
"""

# The panel carries its own script for the same reason it carries its own
# style: it has to work wherever it is dropped. Reusing the cost page's
# sortTable would have coupled these tables to that page's DOM conventions
# (tr.portfolio-row, detail-<id>) for the sake of forty lines.
PANEL_JS = """
function clToggle(id) {
  var row = document.getElementById('cld-' + id);
  var btn = document.getElementById('clt-' + id);
  if (!row) return;
  var open = !row.hidden;
  row.hidden = open;
  btn.innerHTML = open ? '&#9658;' : '&#9660;';
  btn.classList.toggle('open', !open);
}

var _clSort = {};
function clSort(tableId, key) {
  var tbody = document.getElementById(tableId);
  if (!tbody) return;
  var st = _clSort[tableId] || { key: null, dir: 'asc' };
  st.dir = (st.key === key && st.dir === 'asc') ? 'desc' : 'asc';
  st.key = key;
  _clSort[tableId] = st;
  var dir = st.dir === 'asc' ? 1 : -1;
  var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr.cl-row'));
  rows.sort(function (a, b) {
    var av = a.dataset[key], bv = b.dataset[key];
    var an = parseFloat(av), bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn)) return (an - bn) * dir;
    return String(av === undefined ? '' : av)
             .localeCompare(String(bv === undefined ? '' : bv)) * dir;
  });
  rows.forEach(function (r) {
    tbody.appendChild(r);
    // Sub-rows and detail rows belong to the row above them and have to travel
    // with it, or a sort silently reattaches a warning to the wrong cluster.
    var kin = tbody.querySelectorAll('[data-for="' + r.dataset.rid + '"]');
    Array.prototype.forEach.call(kin, function (k) { tbody.appendChild(k); });
  });
  var table = tbody.parentNode;
  Array.prototype.forEach.call(table.querySelectorAll('th.sortable'), function (th) {
    var ind = th.querySelector('.sort-ind');
    if (ind) ind.textContent = (th.dataset.sortKey === key)
      ? (st.dir === 'asc' ? ' \u2191' : ' \u2193') : '';
  });
}

function clFilter() {
  var envSel = document.getElementById('cl-env');
  var clsSel = document.getElementById('cl-cluster');
  var env = envSel ? envSel.value : '';
  var cls = clsSel ? clsSel.value : '';
  var shown = 0, total = 0;
  var rows = document.querySelectorAll('.cluster-panel tr[data-env]');
  Array.prototype.forEach.call(rows, function (tr) {
    var e = (tr.dataset.env || '').split(' ');
    var c = (tr.dataset.cluster || '').split(' ');
    var ok = (!env || e.indexOf(env) >= 0) && (!cls || c.indexOf(cls) >= 0);
    tr.classList.toggle('cl-hidden', !ok);
    if (tr.classList.contains('cl-row')) { total++; if (ok) shown++; }
  });
  var out = document.getElementById('cl-filter-count');
  if (out) out.textContent = (shown === total)
    ? total + ' rows' : shown + ' of ' + total + ' rows';
}

document.addEventListener('DOMContentLoaded', clFilter);
"""


_PILL = {"application": "pill-app", "mixed": "pill-platform", "shared": "pill-shared",
         "platform": "pill-platform", "system": "pill-system", "idle": "pill-idle"}

_STATE = {
    "uds_with_apps": ("UDS Core + applications", "pill-app"),
    "uds_no_apps":   ("UDS Core, no applications", "pill-platform"),
    "no_uds":        ("no UDS Core", "pill-idle"),
    "unreachable":   ("unreachable", "pill-warn"),
}


def _money(v) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "—"


def _env_label(env: str) -> str:
    return env.split("-")[-1].capitalize() if env else env


def _short(cluster: str) -> str:
    """The part of a cluster name that distinguishes it from its siblings.

    Taking only the trailing segment rendered every cluster as `01`, which
    distinguishes nothing on an estate with one cluster per environment. A
    trailing number keeps the segment before it, so `caz-…-shared-aks-01`
    reads as `aks-01`.
    """
    parts = cluster.split("-")
    if len(parts) >= 2 and parts[-1].isdigit():
        return "-".join(parts[-2:])
    return parts[-1] if parts else cluster


def _tile(label: str, value: str, sub: str = "") -> str:
    return (f'<div class="cluster-tile"><div class="t-label">{escape(label)}</div>'
            f'<div class="t-value">{escape(value)}</div>'
            + (f'<div class="t-sub">{escape(sub)}</div>' if sub else "")
            + "</div>")


def _avg_nodes(avg: float) -> str:
    """Average node count, without rounding a live pool down to nothing."""
    if avg <= 0:
        return " / 0"
    if avg < 0.1:
        return " / &lt;0.1"
    return f" / {avg:.1f}"


def _pill(text: str, cls: str) -> str:
    return f'<span class="pill {cls}">{escape(text)}</span>'


def _scaling_reference(reports: dict) -> dict:
    """Highest observed cost per node-week, per VM size.

    Spend covers the whole window; node counts are a snapshot at collection. A
    pool that scaled up an hour before shows a full node count against almost no
    spend — twenty of twenty-seven identical pools did exactly that in one real
    report, reading as though 2.83 CPU cost four cents.

    Comparing each pool against the busiest pool of the same VM size flags that
    without needing a price list, and self-calibrates per region and contract.
    It cannot help when every pool of a size was scaled down; then there is
    nothing to compare against and nothing is claimed.
    """
    best: dict = {}
    for _env, _name, c in _iter_clusters(reports):
        if c.get("status") != "ok":
            continue
        for v in (c.get("pools") or {}).values():
            nodes, cost = v.get("nodes", 0), v.get("cost") or 0.0
            for vm in v.get("vm_sizes") or []:
                if nodes and cost:
                    best[vm] = max(best.get(vm, 0.0), cost / nodes)
    return best


# No AKS node runs a full week for less than this. An absolute floor is what
# catches a VM size where *every* pool was scaled down, leaving the relative
# comparison with only scaled-down pools to calibrate against — which is exactly
# how two GPU pools billing 1% of a full week went unflagged.
MIN_NODE_WEEK = 2.0


def _part_window(v: dict, reference: dict) -> bool:
    """Whether a pool's spend is too low for the nodes it currently reports.

    Below half of what the busiest pool of the same VM size charges per node,
    the pool cannot have been running at its current size for most of the
    window. Half rather than a tenth: a pool up for a third of the week is just
    as misleading beside a full-window node count as one up for an hour.
    """
    nodes = v.get("nodes", 0)
    if not nodes:
        return False

    # A measured average settles this directly; the cost inference below exists
    # only for clusters with no Prometheus to ask.
    avg = v.get("avg_nodes")
    if isinstance(avg, (int, float)):
        return avg < nodes * 0.75

    cost = v.get("cost") or 0.0
    rates = [reference.get(vm, 0.0) for vm in (v.get("vm_sizes") or [])]
    ref = max(rates) if rates else 0.0
    return (cost / nodes) < max(ref * 0.5, MIN_NODE_WEEK)


def _iter_clusters(reports: dict):
    for env in sorted(reports):
        clusters = reports[env].get("clusters", {}) or {}
        for name in sorted(clusters):
            yield env, name, clusters[name]


def _footprint(entry: dict) -> float:
    """What a namespace costs a pool: the greater of what it reserved and used.

    Requests alone would charge nothing to the 233 pods that declare none while
    they consume real capacity. Usage alone would let a team reserve four times
    what it uses and pay for the quarter — the reservation denied that capacity
    to everyone else and is the more expensive fact. Taking the larger of the
    two cannot be argued away from either direction.

    DaemonSet requests are excluded. Those agents run on the pool because the
    pool exists, so their cost belongs to whoever occupies it, which is what
    leaving them out of the denominator achieves.
    """
    cpu = entry.get("cpu", {}) or {}
    reserved = (cpu.get("app", 0.0) + cpu.get("sidecar", 0.0)
                + cpu.get("waypoint", 0.0))
    used = ((entry.get("used") or {}).get("cpu") or 0.0)
    return max(reserved, used)


def allocate(cluster: dict) -> dict:
    """Cost per application for one cluster: what it occupies, plus its share.

    Derived here rather than stored, so the model can change without recollecting
    — the same reasoning as canonicalizing tag values at render time.

    Two steps. Each pool's cost is split across the namespaces on it by
    footprint, which gives every namespace a direct cost. Everything landing on
    platform and system namespaces, plus cluster-wide costs that belong to no
    pool, is then divided equally among the applications — equally, and per
    cluster, matching how the cost page already allocates shared projects.

    Equal division is the claim that the platform has to exist before any
    application can run, so each carries the same share of it. The more
    applications there are, the smaller that share: the effect described when
    this was first discussed.
    """
    pools = cluster.get("pools") or {}
    namespaces = cluster.get("namespaces") or {}

    apps = set()
    for v in pools.values():
        apps.update(v.get("applications") or [])
        apps.update(v.get("spillover") or [])

    direct: dict = defaultdict(float)
    platform_cost = 0.0
    unallocatable = 0.0

    for pool, v in pools.items():
        pool_cost = v.get("cost") or 0.0
        if not pool_cost:
            continue
        shares = {}
        for ns_name, ns in namespaces.items():
            entry = (ns.get("by_pool") or {}).get(pool)
            if entry:
                fp = _footprint(entry)
                if fp > 0:
                    shares[ns_name] = fp
        total = sum(shares.values())
        if not total:
            # Nothing on the pool reserved or used anything measurable. Its cost
            # is real but cannot be attributed; saying so beats spreading it.
            unallocatable += pool_cost
            continue
        for ns_name, fp in shares.items():
            portion = pool_cost * (fp / total)
            if ns_name in apps:
                direct[ns_name] += portion
            else:
                platform_cost += portion

    # Load balancers, the control plane and PVCs belong to the cluster rather
    # than to any pool, so they join the platform pot.
    platform_cost += sum((cluster.get("cost") or {}).get("unattributed", {}).values())

    per_app = platform_cost / len(apps) if apps else 0.0
    return {
        "applications": {
            a: {"direct": direct.get(a, 0.0),
                "platform_share": per_app,
                "total": direct.get(a, 0.0) + per_app}
            for a in sorted(apps)
        },
        "platform_cost": platform_cost,
        "per_application": per_app,
        "unallocatable": unallocatable,
        "app_count": len(apps),
        # Whether usage informed the footprints, or only requests were available.
        "measured": bool((cluster.get("usage") or {}).get("available")),
    }


def placement_issues(cluster: dict) -> list:
    """Workloads sitting on a nodepool they do not belong on.

    Two rules, both from how the platform is meant to be laid out:

      * A **service** — anything in the UDS bundle — belongs on the platform
        nodepool. One running elsewhere is consuming an application's capacity
        and will be billed to that application.
      * An **application** belongs on its own nodepool, or on a shared one with
        other applications. One on the platform or a system pool distorts the
        costing in the other direction, and on a System pool it competes with
        the cluster's own components.

    Reported rather than corrected: where a workload runs is a deployment
    decision, and the page's job is to make a wrong one visible.
    """
    issues = []
    pools = cluster.get("pools") or {}

    # Which pool actually carries UDS Core. Not simply the ones classified
    # "mixed": that only means a pool hosts both services and applications, and
    # is equally true of the platform pool with applications stranded on it and
    # of an application's own pool with one service stranded on it. Flagging
    # every application on every mixed pool reported `aristotle` on the
    # `aristotle` nodepool as misplaced, which is exactly where it belongs.
    #
    # The platform pool is where the services predominantly live. One stray
    # service does not make a pool the platform.
    counts = {p: len(v.get("platform_namespaces") or []) for p, v in pools.items()}
    leader = max(counts.values()) if counts else 0
    platform_pools = {p for p, n in counts.items() if n == leader} if leader >= 2 else set()

    for pool, v in sorted(pools.items()):
        on_platform = pool in platform_pools
        is_system = v.get("mode") == "System"

        # Applications where the platform lives, or on a System pool.
        misplaced = list(v.get("applications") or []) if on_platform else []
        misplaced += list(v.get("spillover") or [])
        for app in sorted(set(misplaced)):
            issues.append({
                "severity": "application",
                "namespace": app, "pool": pool,
                "detail": (f"on the {'system' if is_system and not on_platform else 'platform'} "
                           f"nodepool — belongs on its own or a shared application pool"),
            })

        # Services outside the platform pool. DaemonSets are excluded upstream,
        # so anything here has ordinary workloads on the wrong nodepool. System
        # pools legitimately run cluster components, so they are not judged.
        if not on_platform and not is_system:
            for svc in sorted(v.get("platform_namespaces") or []):
                issues.append({
                    "severity": "service",
                    "namespace": svc, "pool": pool,
                    "detail": "off the platform nodepool — belongs with the UDS bundle",
                })
    return issues


def env_key(env: str) -> str:
    """Match an environment across collectors, which label it slightly differently.

    Public because generate_report.py compares the two collectors' environments
    and must apply the same rule. Two copies of it would drift, and the symptom
    would be a share silently computed over the wrong set of subscriptions.
    """
    return (env or "").split("-")[-1].lower()


def cluster_summary(reports: dict) -> dict:
    """The headline figures for the clusters, computed once.

    Both the strip on the cost page and the panel here read this, so the two
    cannot disagree about how much Kubernetes costs or how much of it is
    occupied. Deriving the same number twice is the failure this project keeps
    finding; one function is the cheapest way not to repeat it.

    Spend is reported two ways because they answer different questions. The
    `window` figures are what was actually billed over the collected period,
    which is the only thing comparable with the cost page. The weekly ones scale
    that to seven days for the headline, and are equal whenever the window is
    already a week.

    Cost and reachability are separated deliberately. The collector gathers a
    cluster's spend *before* contacting it, so a cluster that cannot be reached
    still reports what it costs — and dropping that from the total would
    understate Kubernetes spend by exactly the clusters most likely to be
    neglected. It is counted, and held apart as `unreachable`, because it has no
    utilization figures behind it.
    """
    reference = _scaling_reference(reports)
    window = next((r.get("cost_window", {}) for r in reports.values()
                   if r.get("cost_window")), {})
    days = window.get("days") or 7
    scale = (7 / days) if days else 1.0

    per_env: dict = defaultdict(float)
    total = measured_spend = unreachable = 0.0
    uds_free = occupied = platform = unallocatable = 0.0
    clusters_total = clusters_uds = failed = measured = 0
    unrequested = placement = 0
    app_names: set = set()

    for env, _name, c in _iter_clusters(reports):
        clusters_total += 1
        spend = (c.get("cost") or {}).get("total") or 0.0
        total += spend
        per_env[env] += spend

        if c.get("status") != "ok":
            failed += 1
            unreachable += spend
            continue

        measured_spend += spend
        if c.get("state") == "no_uds":
            uds_free += spend
        else:
            clusters_uds += 1
        unrequested += sum(p.get("unrequested_pods", 0)
                           for p in (c.get("pools") or {}).values())
        placement += len(placement_issues(c))

        a = allocate(c)
        occupied += sum(f["direct"] for f in a["applications"].values())
        platform += a["platform_cost"]
        unallocatable += a["unallocatable"]
        app_names.update(a["applications"])
        measured += 1 if a["measured"] else 0

    return {
        "window": window,
        "days": days,
        # Over the collected window — comparable with a cost report covering the
        # same dates, and with nothing else.
        "window_spend": total,
        "per_env_window_spend": dict(per_env),
        # Scaled to seven days, for the headline figures.
        "weekly": total * scale,
        # Reachable clusters only. Every utilization-derived figure below is a
        # share of this, not of the total, or it would be diluted by clusters
        # nothing was measured on.
        "measured_weekly": measured_spend * scale,
        "unreachable_weekly": unreachable * scale,
        "uds_free_weekly": uds_free * scale,
        "occupied_weekly": occupied * scale,
        "platform_weekly": platform * scale,
        "unallocatable_weekly": unallocatable * scale,
        "clusters_total": clusters_total,
        "clusters_uds": clusters_uds,
        "clusters_failed": failed,
        "clusters_measured": measured,
        "unrequested_pods": unrequested,
        "placement_issues": placement,
        "applications": sorted(app_names),
        "scaling_reference": reference,
    }


def _guide(s: dict) -> str:
    """The reading guide, collapsed by default.

    It exists because the honest answer to "what am I looking at" used to be
    four note paragraphs scattered between five tables, which meant the caveats
    were read after the figures they qualify, if at all. Same pattern as the
    cost page's Important Information: one place, closed until wanted.
    """
    return f"""<details class="cluster-guide">
  <summary><span class="g-chevron">&#9658;</span> &#8505;&#65039; How to read this page
    <span class="g-hint">Cost model &middot; What the columns mean &middot; What the figures cannot tell you</span></summary>
  <div class="cluster-guide-body">
    <div class="guide-section">
      <h4>What this page is for</h4>
      <p>The cost page says what each portfolio spent. It cannot say how much of
      that is Kubernetes, or whether the capacity being paid for is used. Cost
      lives in Azure and utilization lives in the cluster, and neither knows
      about the other — this page is the join.</p>
      <p>Every dollar here is already inside the portfolio figures. Nothing is
      additional spend.</p>
    </div>
    <div class="guide-section">
      <h4>How cluster cost is divided</h4>
      <ul>
        <li><strong>Per-node agents</strong> — Falco, ztunnel, node-exporter.
        They run on an application's own nodes, so they are already billed to
        it. An application on five nodes pays for five copies however many
        neighbors it has.</li>
        <li><strong>Central services</strong> — istiod, Keycloak, Loki,
        Prometheus. Shared, so divided.</li>
        <li><strong>Cluster fixed</strong> — the control plane and system
        pools. Also divided.</li>
      </ul>
      <p>The consequence worth knowing: <strong>more applications makes the
      platform cheaper per application</strong> and does not change the per-node
      part.</p>
    </div>
    <div class="guide-section">
      <h4>Occupied, and platform share</h4>
      <p><strong>Occupied</strong> is an application's share of the nodepools it
      runs on, split by the greater of what it reserved and what it used.
      Neither direction can be argued away: a team reserving four times what it
      uses denied that capacity to everyone else, and a team reserving nothing
      still consumes real capacity.</p>
      <p><strong>Platform share</strong> divides the central and fixed tiers
      equally among the applications in each cluster — the same way the cost
      page allocates shared projects.</p>
    </div>
    <div class="guide-section">
      <h4>The two capacity gaps</h4>
      <p>They look alike and have different owners, which is why they are
      separate columns rather than one "wasted spend" figure.</p>
      <ul>
        <li><strong>Unclaimed</strong> — capacity nobody asked for. Pools sized
        beyond anything scheduled on them. The platform team resolves it by
        scaling pools.</li>
        <li><strong>Over-requested</strong> — capacity reserved and not used.
        The teams owning the manifests resolve it.</li>
      </ul>
    </div>
    <div class="guide-section">
      <h4>What these figures cannot tell you</h4>
      <ul>
        <li>Requested CPU is what workloads reserved, not what they use.
        {s['unrequested_pods']} pod(s) declare no request at all, consuming
        capacity while claiming none — so the gap between usable and requested
        overstates what is genuinely spare. That is why no single
        spare-capacity figure appears anywhere on this page.</li>
        <li>Usable CPU is allocatable less the per-node agents every workload is
        obliged to host, not the raw node size.</li>
        <li>Node counts are as at collection while spend covers the whole week,
        so a pool that scaled up recently reports nodes it was not billed for.
        Those are marked <em>part window</em>.</li>
      </ul>
    </div>
    <div class="guide-section">
      <h4>Where the numbers come from</h4>
      <p>Spend is Azure Cost Management scoped to each cluster's node resource
      group. Capacity and requests are read from the Kubernetes API. Usage is a
      seven-day average from each cluster's own Prometheus, evaluated to close
      where the spend window closes — a cluster whose monitoring holds less
      history than that says so beneath its row.</p>
      <p>Cost is gathered before a cluster is contacted, so one that cannot be
      reached still reports what it costs.</p>
    </div>
  </div>
</details>"""


def _filter_bar(reports: dict) -> str:
    """Environment and cluster filters, applied to every table at once."""
    envs, clusters = [], []
    for env, name, _c in _iter_clusters(reports):
        if env_key(env) not in [e[0] for e in envs]:
            envs.append((env_key(env), _env_label(env)))
        if _short(name) not in clusters:
            clusters.append(_short(name))
    if len(envs) < 2 and len(clusters) < 2:
        return ""
    env_opts = "".join(f'<option value="{escape(k)}">{escape(l)}</option>'
                       for k, l in envs)
    cl_opts  = "".join(f'<option value="{escape(c)}">{escape(c)}</option>'
                       for c in sorted(clusters))
    return (f'<div class="cluster-filters">'
            f'<label for="cl-env">Environment</label>'
            f'<select id="cl-env" onchange="clFilter()">'
            f'<option value="">All</option>{env_opts}</select>'
            f'<label for="cl-cluster">Cluster</label>'
            f'<select id="cl-cluster" onchange="clFilter()">'
            f'<option value="">All</option>{cl_opts}</select>'
            f'<span class="filter-count" id="cl-filter-count"></span></div>')


def _h3(num: int, title: str, lede: str) -> str:
    return (f'  <h3><span class="h3-num">{num}.</span>{escape(title)}</h3>\n'
            f'  <p class="cluster-lede">{lede}</p>')


def _sortable(tbody_id: str, cols: list) -> str:
    """Header row. Each entry is (label, key, numeric, group) — key None means
    the column is not sortable, group adds a small owner label above it."""
    out = ["<thead><tr>"]
    for label, key, numeric, group in cols:
        cls = "num" if numeric else ""
        if key:
            cls += " sortable"
            out.append(f'<th class="{cls.strip()}" data-sort-key="{key}" '
                       f'onclick="clSort(\'{tbody_id}\',\'{key}\')">'
                       + (f'<span class="col-group">{escape(group)}</span>' if group else "")
                       + f'{escape(label)}<span class="sort-ind"></span></th>')
        else:
            out.append(f'<th class="{cls.strip()}">'
                       + (f'<span class="col-group">{escape(group)}</span>' if group else "")
                       + f'{escape(label)}</th>')
    out.append("</tr></thead>")
    return "".join(out)


def _section_cost(reports: dict, s: dict) -> list:
    """What Kubernetes costs, and which clusters it is spent on."""
    weekly   = s["weekly"]
    measured = s["measured_weekly"]
    uds_free = s["uds_free_weekly"]

    tiles = [
        _tile("Kubernetes spend", f"{_money(weekly)}/wk", f"{_money(weekly * 52)}/yr"),
        _tile("Occupied by applications", f"{_money(s['occupied_weekly'])}/wk",
              (f"{s['occupied_weekly'] / measured * 100:.0f}% of what was measured, "
               f"{len(s['applications'])} application(s)") if measured else ""),
        _tile("Clusters", f"{s['clusters_uds']} of {s['clusters_total']}",
              "running UDS Core"),
    ]
    # Spend on clusters running nothing is the strongest finding on this page
    # when it exists, and dead weight when it does not — it read "$0.00 / 0%"
    # on an estate where every cluster runs UDS Core.
    if uds_free > 0.005:
        tiles.append(_tile("Spend without UDS Core", f"{_money(uds_free)}/wk",
                           f"{uds_free / measured * 100:.0f}% of what was measured"
                           if measured else ""))
    elif s["applications"]:
        per_app = s["platform_weekly"] / len(s["applications"])
        tiles.append(_tile("Platform share each", f"{_money(per_app)}/wk",
                           "falls as more applications onboard"))
    tiles.append(_tile("Pods with no CPU request", f"{s['unrequested_pods']}",
                       "invisible to capacity planning"))
    if s["clusters_failed"]:
        tiles.append(_tile("Clusters not collected", str(s["clusters_failed"]),
                           f"{_money(s['unreachable_weekly'])}/wk with no figures"))

    window = s["window"]
    out = [_h3(1, "What Kubernetes costs",
               "Every dollar here is already inside the portfolio figures on the "
               "cost page - this is the same money at a finer grain, not "
               "additional spend. Spend covers "
               f"<strong>{escape(str(window.get('from', '?')))} to "
               f"{escape(str(window.get('to', '?')))}</strong>; capacity and "
               "requests are as observed at collection."),
           '  <div class="cluster-tiles">' + "".join(tiles) + "</div>",
           '  <div class="cluster-scroll"><table class="cluster-table">',
           _sortable("cl-clusters", [
               ("Environment", "env", False, ""), ("Cluster", "cluster", False, ""),
               ("State", "state", False, ""), ("Nodepools", None, False, ""),
               ("Nodes", "nodes", True, ""), ("Spend", "spend", True, ""),
           ]),
           '<tbody id="cl-clusters">']

    rid = 0
    for env, name, c in _iter_clusters(reports):
        rid += 1
        label, cls = _STATE.get(c.get("state", ""), (c.get("state", "?"), "pill-warn"))
        attrs = (f'data-rid="c{rid}" data-env="{escape(env_key(env))}" '
                 f'data-cluster="{escape(_short(name))}" '
                 f'data-envlabel="{escape(_env_label(env))}"')
        spend = (c.get("cost") or {}).get("total")
        if c.get("status") != "ok":
            out.append(
                f'    <tr class="cl-row" {attrs} data-state="{escape(label)}" '
                f'data-spend="{spend or 0}" data-nodes="0">'
                f"<td>{escape(_env_label(env))}</td><td>{escape(_short(name))}</td>"
                f"<td>{_pill(label, cls)}</td>"
                f'<td colspan="2">{escape(str(c.get("message", ""))[:90])}</td>'
                f'<td class="num">{_money(spend)}</td></tr>')
            continue
        pools = c.get("pools") or {}
        nodes = sum(p.get("nodes", 0) for p in pools.values())
        out.append(
            f'    <tr class="cl-row" {attrs} data-state="{escape(label)}" '
            f'data-spend="{spend or 0}" data-nodes="{nodes}">'
            f"<td>{escape(_env_label(env))}</td><td>{escape(_short(name))}</td>"
            f"<td>{_pill(label, cls)}</td>"
            f"<td>{escape(', '.join(sorted(pools)))}</td>"
            f'<td class="num">{nodes}</td>'
            f'<td class="num">{_money(spend)}</td></tr>')

        u = c.get("usage") or {}
        covered = u.get("window_days_covered")
        kin = (f'data-for="c{rid}" data-env="{escape(env_key(env))}" '
               f'data-cluster="{escape(_short(name))}"')
        if u.get("available") and u.get("aligned_to_cost_window") is False:
            out.append(f'    <tr class="sub" {kin}><td colspan="6">'
                       f'{_pill("later window", "pill-warn")} usage for '
                       f'{escape(_short(name))} is measured to now rather than to '
                       "the end of the spend window - its monitoring holds "
                       "less history than the period being charged.</td></tr>")
        if (u.get("available") and covered is not None
                and covered < u.get("window_days", 7)):
            out.append(f'    <tr class="sub" {kin}><td colspan="6">'
                       f'{_pill("short history", "pill-warn")} usage for '
                       f'{escape(_short(name))} averages only '
                       + ("less than a day" if covered < 1 else f"{covered} day(s)")
                       + f', not {u.get("window_days", 7)} - the spend beside '
                       "it covers the full window.</td></tr>")
    out.append("</tbody></table></div>")
    return out


def _section_applications(reports: dict, s: dict) -> list:
    """What each application costs, all in. The leadership-facing table."""
    rolled: dict = {}
    detail: dict = {}
    unallocatable = 0.0
    for env, name, c in _iter_clusters(reports):
        if c.get("status") != "ok":
            continue
        a = allocate(c)
        unallocatable += a["unallocatable"]
        for app, figures in a["applications"].items():
            r = rolled.setdefault(app, {"direct": 0.0, "platform_share": 0.0,
                                        "total": 0.0, "envs": {}, "clusters": 0,
                                        "measured": 0, "keys": set(), "cl": set()})
            r["direct"] += figures["direct"]
            r["platform_share"] += figures["platform_share"]
            r["total"] += figures["total"]
            r["envs"][env_key(env)] = _env_label(env)
            r["keys"].add(env_key(env))
            r["cl"].add(_short(name))
            r["clusters"] += 1
            r["measured"] += 1 if a["measured"] else 0
        # Where it actually sits, which used to be a 57-row table of its own.
        for pool, v in sorted((c.get("pools") or {}).items()):
            for app in (v.get("applications") or []) + (v.get("spillover") or []):
                ns_all = (c.get("namespaces") or {}).get(app, {})
                ns = (ns_all.get("by_pool") or {}).get(pool) or ns_all
                placement = {"application": "own nodepool",
                             "shared": "shares a nodepool",
                             "mixed": "on the UDS Core nodepool"}.get(
                                 v.get("classification"), v.get("classification", "?"))
                if app in (v.get("spillover") or []):
                    placement = f"spilled onto {pool}"
                detail.setdefault(app, []).append(
                    (_env_label(env), _short(name), pool, placement,
                     ns.get("pods", 0),
                     ns.get("cpu", {}).get("app", 0),
                     (ns.get("used") or {}).get("cpu"),
                     ns.get("unrequested_pods", 0)))

    out = [_h3(2, "What each application costs",
               "What it would cost to stop running an application: its own "
               "footprint, plus the share of the platform it runs on. "
               "<strong>Expand a row</strong> to see where it actually runs. "
               "Platform share falls as more applications onboard, because it is "
               "divided equally among them.")]
    if not rolled:
        out.append('  <p class="empty-state">No applications are deployed. '
                   "Platform and system workloads only.</p>")
        return out

    out += ['  <div class="cluster-scroll"><table class="cluster-table">',
            _sortable("cl-apps", [
                ("", None, False, ""),
                ("Application", "app", False, ""),
                ("Environments", "envs", False, ""),
                ("Occupied", "direct", True, ""),
                ("Platform share", "share", True, ""),
                ("Total / week", "total", True, ""),
                ("Total / year", "year", True, ""),
                ("Basis", "basis", False, ""),
            ]),
            '<tbody id="cl-apps">']

    for i, (app, r) in enumerate(sorted(rolled.items(), key=lambda kv: -kv[1]["total"])):
        # An application spanning clusters may be measured in some and not
        # others. Claiming either of the whole row would be wrong.
        if r["measured"] == r["clusters"]:
            basis, basis_key = "requests and usage", "2"
        elif r["measured"] == 0:
            basis, basis_key = _pill("requests only", "pill-warn"), "0"
        else:
            basis = _pill(f"{r['measured']} of {r['clusters']} measured", "pill-warn")
            basis_key = "1"
        envs = " ".join(sorted(r["keys"]))
        rows = detail.get(app, [])
        out.append(
            f'    <tr class="cl-row" data-rid="a{i}" data-env="{escape(envs)}" '
            f'data-cluster="{escape(" ".join(sorted(r["cl"])))}" '
            f'data-app="{escape(app)}" data-envs="{escape(", ".join(sorted(r["envs"].values())))}" '
            f'data-direct="{r["direct"]:.2f}" data-share="{r["platform_share"]:.2f}" '
            f'data-total="{r["total"]:.2f}" data-year="{r["total"] * 52:.2f}" '
            f'data-basis="{basis_key}">'
            f'<td><button class="cl-toggle" id="clt-a{i}" onclick="clToggle(\'a{i}\')" '
            f'title="Where it runs">&#9658;</button></td>'
            f"<td>{escape(app)}</td>"
            f'<td>{escape(", ".join(sorted(r["envs"].values())))}</td>'
            f'<td class="num">{_money(r["direct"])}</td>'
            f'<td class="num">{_money(r["platform_share"])}</td>'
            f'<td class="num">{_money(r["total"])}</td>'
            f'<td class="num">{_money(r["total"] * 52)}</td>'
            f"<td>{basis}</td></tr>")
        inner = "".join(
            f"<tr><td>{escape(e)}</td><td>{escape(cl)}</td><td>{escape(pool)}</td>"
            f"<td>{escape(pl)}</td><td class='num'>{pods}</td>"
            f"<td class='num'>{req:.2f}</td>"
            + (f"<td class='num'>{used:.2f}</td>" if used is not None
               else "<td class='num'>&mdash;</td>")
            + f"<td class='num'>{unreq or '—'}</td></tr>"
            for e, cl, pool, pl, pods, req, used, unreq in rows)
        out.append(
            f'    <tr class="cl-detail" id="cld-a{i}" data-for="a{i}" '
            f'data-env="{escape(envs)}" '
            f'data-cluster="{escape(" ".join(sorted(r["cl"])))}" hidden>'
            f'<td colspan="8"><div class="cl-detail-inner">'
            f'<p class="cl-detail-label">Where {escape(app)} runs</p>'
            '<table class="cluster-table"><thead><tr>'
            "<th>Environment</th><th>Cluster</th><th>Nodepool</th><th>Placement</th>"
            "<th class='num'>Pods</th><th class='num'>CPU requested</th>"
            "<th class='num'>CPU used</th><th class='num'>No request</th>"
            f"</tr></thead><tbody>{inner}</tbody></table>"
            "</div></td></tr>")
    out.append("</tbody></table></div>")

    note = ("  <p class=\"cluster-note\">Occupied is split by the greater of what a "
            "namespace reserved and what it used, so neither over-reserving nor "
            "under-declaring escapes the bill. Allocated totals reconcile to the "
            "clusters' cost exactly.")
    if unallocatable > 0.005:
        note += (f" {_money(unallocatable)}/week sits on pools where nothing "
                 "reserved or used anything measurable, and is left unattributed "
                 "rather than spread.")
    out.append(note + "</p>")
    return out


def _section_capacity(reports: dict, reference: dict) -> list:
    """Where capacity is going unused — the platform team's working table."""
    out = [_h3(3, "Where capacity is going unused",
               "Two gaps that look alike and have different owners. "
               "<strong>Unclaimed</strong> is capacity nobody asked for, which "
               "the platform team resolves by scaling pools. "
               "<strong>Over-requested</strong> is capacity reserved and not "
               "used, which the teams owning the manifests resolve. They are "
               "kept apart because one number covering both would be a number "
               "nobody can act on."),
           '  <div class="cluster-scroll"><table class="cluster-table">',
           _sortable("cl-pools", [
               ("Environment", "env", False, ""), ("Cluster", "cluster", False, ""),
               ("Nodepool", "pool", False, ""), ("Kind", "kind", False, ""),
               ("Nodes", "nodes", True, "now / avg"),
               ("Spend", "spend", True, "per week"),
               # Every one of these is a count of CPU cores, and nothing on the
               # first version said so — a reader could not tell whether 12.00
               # was cores, a percentage or dollars. The two gap columns name
               # the unit and the owner, because both are needed to act on them.
               ("Usable", "usable", True, "CPU cores"),
               ("Requested", "requested", True, "CPU cores"),
               ("Used", "used", True, "CPU cores"),
               ("Unclaimed", "unclaimed", True, "cores \u00b7 platform"),
               ("Over-req.", "overreq", True, "cores \u00b7 app teams"),
               ("No request", "noreq", True, "pods"),
           ]),
           '<tbody id="cl-pools">']
    i = 0
    for env, name, c in _iter_clusters(reports):
        if c.get("status") != "ok":
            continue
        for pool, v in sorted((c.get("pools") or {}).items()):
            i += 1
            cpu = v.get("cpu", {})
            kind = v.get("classification", "?")
            apps = v.get("applications") or []
            kind_text = f"{kind} ({', '.join(apps)})" if apps else kind
            spill = v.get("spillover") or []
            if spill:
                kind_text += f" + {len(spill)} spilled over"
            usable = cpu.get("usable", 0) or 0
            req    = cpu.get("requested", 0) or 0
            used   = cpu.get("used")
            unclaimed = max(usable - req, 0)
            overreq   = max(req - used, 0) if used is not None else None
            partial = _part_window(v, reference)
            avg = (_avg_nodes(v["avg_nodes"])
                   if isinstance(v.get("avg_nodes"), (int, float)) else "")
            out.append(
                f'    <tr class="cl-row" data-rid="p{i}" '
                f'data-env="{escape(env_key(env))}" data-cluster="{escape(_short(name))}" '
                f'data-pool="{escape(pool)}" data-kind="{escape(kind)}" '
                f'data-nodes="{v.get("nodes", 0)}" data-spend="{v.get("cost") or 0}" '
                f'data-usable="{usable:.3f}" data-requested="{req:.3f}" '
                f'data-used="{used if used is not None else ""}" '
                f'data-unclaimed="{unclaimed:.3f}" '
                f'data-overreq="{overreq if overreq is not None else ""}" '
                f'data-noreq="{v.get("unrequested_pods", 0)}">'
                f"<td>{escape(_env_label(env))}</td><td>{escape(_short(name))}</td>"
                f"<td>{escape(pool)}</td>"
                f'<td class="kind-cell">'
                f'{_pill(kind_text, _PILL.get(kind, "pill-system"))}</td>'
                f'<td class="num">{v.get("nodes", 0)}{avg}</td>'
                f'<td class="num">{_money(v.get("cost"))}'
                + (f' {_pill("part window", "pill-warn")}' if partial else "") + "</td>"
                f'<td class="num">{usable:.2f}</td>'
                f'<td class="num">{req:.2f}</td>'
                + (f'<td class="num">{used:.2f}</td>' if used is not None
                   else '<td class="num">&mdash;</td>')
                + f'<td class="num gap-platform">{unclaimed:.2f}</td>'
                + (f'<td class="num gap-team">{overreq:.2f}</td>'
                   if overreq is not None else '<td class="num gap-team">&mdash;</td>')
                + f'<td class="num">{v.get("unrequested_pods", 0) or "—"}</td></tr>')
    out.append("</tbody></table></div>")
    out.append('  <p class="cluster-note">A pool marked <em>part window</em> was '
               "not running for the whole week, so its node count and its spend "
               "describe different periods. Usable CPU is allocatable less the "
               "per-node agents every workload has to host.</p>")
    return out


def _section_placement(reports: dict) -> list:
    """Workloads on the wrong nodepool, grouped by what is wrong.

    Reported, never corrected: where a workload runs is a deployment decision,
    and the page's job is to make a wrong one visible. Grouped because 29
    separate rows of "application on the platform nodepool" is a wall rather
    than a finding.
    """
    groups: dict = {}
    for env, name, c in _iter_clusters(reports):
        if c.get("status") != "ok":
            continue
        for issue in placement_issues(c):
            key = (issue["severity"], issue["detail"])
            groups.setdefault(key, []).append(
                (env_key(env), _env_label(env), _short(name),
                 issue["namespace"], issue["pool"]))

    out = [_h3(4, "What is on the wrong nodepool",
               "Services belong on the platform nodepool and applications on "
               "their own or a shared application pool. Both directions distort "
               "the costing above, in opposite ways: an application on the "
               "platform pool takes capacity billed to the platform, and a "
               "service elsewhere is billed to whichever application owns that "
               "pool. Neither is visible from the cost figures alone.")]
    if not groups:
        out.append('  <p class="empty-state">Every workload is on the nodepool it '
                   "belongs on.</p>")
        return out

    out += ['  <div class="cluster-scroll"><table class="cluster-table">',
            '<thead><tr><th></th><th>Issue</th><th>Kind</th>'
            "<th class='num'>Workloads</th><th>Environments</th>"
            "</tr></thead>", '<tbody id="cl-place">']
    for i, ((severity, detail_txt), rows) in enumerate(
            sorted(groups.items(), key=lambda kv: -len(kv[1]))):
        cls = "pill-warn" if severity == "application" else "pill-shared"
        envs = sorted({e for e, _l, _c, _n, _p in rows})
        clusters = sorted({c for _e, _l, c, _n, _p in rows})
        labels = sorted({l for _e, l, _c, _n, _p in rows})
        out.append(
            f'    <tr class="cl-row" data-rid="g{i}" data-env="{escape(" ".join(envs))}" '
            f'data-cluster="{escape(" ".join(clusters))}">'
            f'<td><button class="cl-toggle" id="clt-g{i}" onclick="clToggle(\'g{i}\')" '
            f'title="Which workloads">&#9658;</button></td>'
            f"<td>{escape(detail_txt)}</td>"
            f"<td>{_pill(severity, cls)}</td>"
            f'<td class="num">{len(rows)}</td>'
            f'<td>{escape(", ".join(labels))}</td></tr>')
        inner = "".join(
            f"<tr><td>{escape(l)}</td><td>{escape(cl)}</td>"
            f"<td>{escape(ns)}</td><td>{escape(pool)}</td></tr>"
            for _e, l, cl, ns, pool in sorted(rows))
        out.append(
            f'    <tr class="cl-detail" id="cld-g{i}" data-for="g{i}" '
            f'data-env="{escape(" ".join(envs))}" '
            f'data-cluster="{escape(" ".join(clusters))}" hidden>'
            f'<td colspan="5"><div class="cl-detail-inner">'
            '<table class="cluster-table"><thead><tr>'
            "<th>Environment</th><th>Cluster</th><th>Namespace</th>"
            f"<th>On nodepool</th></tr></thead><tbody>{inner}</tbody></table>"
            "</div></td></tr>")
    out.append("</tbody></table></div>")
    out.append('  <p class="cluster-note">Until these move, their cost lands on '
               "whichever pool they occupy rather than the one they belong to, so "
               "the figures above follow the placement rather than the intent.</p>")
    return out


def render_cluster_panel(reports: dict) -> str:
    """The panel, from {environment: report}. Returns a fragment.

    Ordered by the question each section answers rather than by the order the
    data was collected. The first arrangement had five tables of 151 rows with
    the leadership-facing one last, four caveat paragraphs scattered between
    them, and a 57-row table that mostly restated the other two — which is a
    data dump, not a report.
    """
    if not reports:
        return ('<section class="cluster-panel"><p class="empty-state">'
                "Not collected - no data for any environment.</p></section>")

    s = cluster_summary(reports)
    out = ['<section class="cluster-panel">', f"<style>{PANEL_CSS}</style>",
           _guide(s), _filter_bar(reports)]
    out += _section_cost(reports, s)
    out += _section_applications(reports, s)
    out += _section_capacity(reports, s["scaling_reference"])
    out += _section_placement(reports)
    out.append(f"<script>{PANEL_JS}</script>")
    out.append("</section>")
    return "\n".join(out)


def render_cluster_strip(reports: dict, comparison: dict | None = None,
                         href: str = "cluster.html") -> str:
    """The compact summary that sits on the cost page. Returns a fragment.

    The two pages describe the same money at different granularities: the cost
    page says what a portfolio spent, this says how much of that is Kubernetes
    and how much of it is in use. So the strip states the size of the drill-down
    and gets out of the way — five numbers at most, and a link.

    `comparison` is what the cost page reported over the *same* window, as
    {"total": float, "environments": [env_key, ...], "partial": bool}, or None
    when the caller could not establish a sound comparison. The share is the
    most useful number here and the easiest to get wrong: the cluster collector
    reads a trailing seven days while the cost collector reads an ISO week, and
    those coincide only on the Monday schedule. Dividing them regardless would
    state a confident percentage over two different periods.
    """
    if not reports:
        return ""

    s = cluster_summary(reports)

    # Collected but unreadable is not the same as costing nothing. Reporting
    # "$0.00/week" here would read as an all-clear, which is the failure the
    # status contract exists to prevent.
    if s["clusters_total"] and s["clusters_failed"] == s["clusters_total"]:
        return ('<section class="cluster-strip"><style>' + PANEL_CSS + '</style>'
                '<h2>Kubernetes clusters</h2>'
                f'<p class="cluster-note">No cluster could be reached this run '
                f'({s["clusters_failed"]} attempted), so there are no utilization '
                f'figures for this period. Their spend is still known: '
                f'{_money(s["unreachable_weekly"])} a week. The costs above are '
                f'unaffected.</p>'
                f'<p><a class="cluster-more" href="{escape(href)}">'
                'Cluster utilization &rarr;</a></p></section>')

    weekly   = s["weekly"]
    measured = s["measured_weekly"]
    occupied = s["occupied_weekly"]

    # Scoped to the environments the comparison covers, so numerator and
    # denominator describe the same subscriptions as well as the same days.
    share = scope = None
    if comparison and comparison.get("total"):
        envs = set(comparison.get("environments") or [])
        numerator = sum(v for e, v in s["per_env_window_spend"].items()
                        if env_key(e) in envs)
        share = numerator / comparison["total"] * 100
        scope = sorted(e.capitalize() for e in envs) if comparison.get("partial") else None

    if share is None:
        lede = (f'The Kubernetes clusters cost {_money(weekly)} a week across '
                f'{s["clusters_total"]} cluster(s). Their share of the spend above '
                f'is not stated: utilization was collected over '
                f'{escape(str(s["window"].get("from", "?")))} to '
                f'{escape(str(s["window"].get("to", "?")))}, which is not the '
                f'period reported above, so the two are not comparable.')
    elif scope:
        names = (scope[0] if len(scope) == 1
                 else " and ".join([", ".join(scope[:-1]), scope[-1]]))
        which = "that environment" if len(scope) == 1 else "those environments"
        lede = (f'Across {escape(names)}, where both cost and cluster data were '
                f'collected, Kubernetes is <strong>{share:.0f}%</strong> of the '
                f'spend this page reports for {which}. The clusters cost '
                f'{_money(weekly)} a week in total. The figures above report that '
                f'money by portfolio; the cluster page reports what it buys and '
                f'how much of it is in use.')
    else:
        lede = (f'Kubernetes is <strong>{share:.0f}%</strong> of the spend reported '
                f'on this page - {_money(weekly)} a week across '
                f'{s["clusters_total"]} cluster(s). The figures above report that '
                f'money by portfolio; the cluster page reports what it buys and '
                f'how much of it is in use.')

    tiles = [
        _tile("Kubernetes spend", f"{_money(weekly)}/wk",
              ((f"{share:.0f}% of reported spend"
                + (" (partial)" if scope else "")) if share is not None
               else f"{_money(weekly * 52)}/yr")),
        _tile("Occupied by applications", f"{_money(occupied)}/wk",
              (f"{occupied / measured * 100:.0f}% of what was measured, "
               f"{len(s['applications'])} application(s)") if measured else ""),
        _tile("Clusters", f"{s['clusters_uds']} of {s['clusters_total']}",
              "running UDS Core"),
        _tile("Pods with no CPU request", f"{s['unrequested_pods']}",
              "invisible to capacity planning"),
    ]
    if s["placement_issues"]:
        tiles.append(_tile("Workloads misplaced", f"{s['placement_issues']}",
                           "on a nodepool they do not belong on"))

    out = ['<section class="cluster-strip">', f"<style>{PANEL_CSS}</style>",
           "  <h2>Kubernetes clusters</h2>",
           f'  <p class="cluster-note">{lede}</p>',
           '  <div class="cluster-tiles">' + "".join(tiles) + "</div>"]
    if s["clusters_failed"]:
        out.append(f'  <p class="cluster-note">{s["clusters_failed"]} cluster(s) '
                   f'could not be reached this run. Their '
                   f'{_money(s["unreachable_weekly"])} a week is counted in the '
                   "spend above but has no utilization figures behind it.</p>")
    out.append(f'  <p><a class="cluster-more" href="{escape(href)}">'
               "Cluster utilization &rarr;</a></p>")
    out.append("</section>")
    return "\n".join(out)


def render_cluster_page(reports: dict, css: str = "", generated: str = "",
                        back_href: str = "index.html",
                        image_status_href: str = "image-status.html") -> str:
    """The standalone cluster page: the panel inside a document.

    Used by both the published page and the preview, so reviewing the panel and
    publishing it exercise the same rendering. The page CSS is passed in rather
    than imported, which is what keeps the dependency running one way — this
    module is imported by generate_report.py and must never import it back.
    """
    panel = render_cluster_panel(reports)
    nav = (f'<nav class="kpi-page-nav" aria-label="KPI pages">'
           f'<a href="{escape(back_href)}">KPI Overview</a>'
           '<span aria-current="page">Cluster Utilization</span>'
           f'<a href="{escape(image_status_href)}">Image Status</a></nav>'
           if back_href else
           '<p class="period-str">Preview - not published to the dashboard</p>')
    gen = f'\n    <p class="generated">Generated: {escape(generated)}</p>' if generated else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Expedition-0 KPI Report - Cluster Utilization</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#128202;</text></svg>">
  <style>{css}</style>
</head>
<body>
  <header>
    <h1>Cluster Utilization</h1>
    {nav}{gen}
  </header>
{panel}
</body>
</html>"""


def load_reports(reports_dir: Path) -> dict:
    reports = {}
    for path in sorted(reports_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            print(f"[WARN] Could not read {path}: {e}", file=sys.stderr)
            continue
        env = data.get("environment") or path.stem
        reports[env] = data
    return reports


def main() -> None:
    reports = load_reports(REPORTS_DIR)
    if not reports:
        print(f"[ERROR] No cluster reports in {REPORTS_DIR}", file=sys.stderr)
        sys.exit(1)
    print(f"[INFO] Loaded {len(reports)} report(s): {sorted(reports)}")

    # The page's own CSS, so the preview looks like the page rather than
    # approximating it. Imported here and not at module scope: generate_report
    # will import this module, and the dependency must not run both ways.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "costs"))
    try:
        from generate_report import CSS
    except Exception as e:
        print(f"[WARN] Could not load page CSS ({e}) — preview will be unstyled",
              file=sys.stderr)
        CSS = ""

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out = PREVIEW_DIR / "index.html"
    # No back link: the preview is a standalone artifact with no cost page
    # beside it. Same renderer as the published page, so reviewing the panel
    # here and publishing it cannot diverge.
    out.write_text(render_cluster_page(reports, css=CSS, back_href=""),
                   encoding="utf-8")
    print(f"[INFO] Preview written to {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
