"""
Resource KPI collector — idle, orphaned and underutilized resources.

Runs independently of the cost collector. That separation matters for two
reasons beyond tidiness:

  * These are point-in-time facts about the subscription, with no relationship
    to a cost reporting window. Living inside the cost script meant the backfill
    ran the whole scan once per window — 38 times per environment for identical
    data.

  * A failure here must not stop cost data being gathered or published, and
    vice versa.

Writes two things per environment:

  resource_reports/<env>.json          artifact the page reads
  resource_history/<env>/<monday>.json committed to the data branch

The history shard is new. Findings were previously written to an artifact and
copied to the page, and never reached the data branch, so there was no way to
ask whether flagged resources are actually being remediated.

Every scan reports its own status. A failed VM scan should not make the disk
findings look untrustworthy, and an empty result from a scan that ran must be
distinguishable from a scan that did not run — otherwise the page renders
"None detected" over a collector that died, which reads as an all-clear.

Required environment:
  ENVIRONMENT             dev | test | prod
  AZURE_SUBSCRIPTION_ID   subscription to scan

Optional:
  RESOURCE_REPORTS_DIR    default: resource_reports
  HISTORY_DIR             default: resource_history
  IDLE_DAYS               lookback window, default 30
  CPU_THRESHOLD           underutilized VM threshold, default 20.0
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from history_io import update_manifest, write_if_changed  # noqa: E402


ENVIRONMENT   = os.environ.get("ENVIRONMENT", "").strip()
SUBSCRIPTION  = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
REPORTS_DIR   = Path(os.environ.get("RESOURCE_REPORTS_DIR", "resource_reports"))
HISTORY_DIR   = Path(os.environ.get("HISTORY_DIR", "resource_history"))
IDLE_DAYS     = int(os.environ.get("IDLE_DAYS", "30"))
CPU_THRESHOLD = float(os.environ.get("CPU_THRESHOLD", "20.0"))


def _az(args: list, timeout: int = 300) -> object:
    """Run an `az` command and return its parsed JSON output."""
    result = subprocess.run(
        ["az", *args, "-o", "json"],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip()
                           else f"az {' '.join(args[:2])} exited {result.returncode}")
    return json.loads(result.stdout or "null")


def _metric_average(resource_id: str, metric: str, start, end) -> float | None:
    """Mean of a metric's daily averages, or None when the metric has no data."""
    payload = _az([
        "monitor", "metrics", "list",
        "--resource", resource_id,
        "--metrics", metric,
        "--interval", "P1D",
        "--aggregation", "Average",
        "--start-time", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--end-time", end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    ])
    values = [
        point["average"]
        for m in (payload or {}).get("value", [])
        for series in m.get("timeseries", [])
        for point in series.get("data", [])
        if point.get("average") is not None
    ]
    return sum(values) / len(values) if values else None


def scan_underutilized_vms(start, end) -> list:
    vms = _az([
        "vm", "list", "-d",
        "--query", "[? powerState=='VM running' && resourceGroup!=null]"
                   ".{name:name, resourceGroup:resourceGroup, id:id}",
    ]) or []

    flagged = []
    for vm in vms:
        try:
            avg = _metric_average(vm["id"], "Percentage CPU", start, end)
        except Exception as e:
            # One unreadable VM should not lose the findings for all the others.
            print(f"[WARN] Could not read CPU for {vm['id']}: {e}", file=sys.stderr)
            continue
        if avg is not None and avg < CPU_THRESHOLD:
            flagged.append({**vm, "avg_cpu_30d": round(avg, 2)})
    return flagged


def scan_orphaned_disks(start, end) -> list:
    result = _az([
        "graph", "query", "--subscriptions", SUBSCRIPTION,
        "-q", "Resources | where type=='microsoft.compute/disks' "
              "| where tostring(properties.diskState)=='Unattached' "
              f"| where properties.timeCreated < ago({IDLE_DAYS}d) "
              "| project name, resourceGroup, diskSizeGB=properties.diskSizeGB, "
              "properties.timeCreated",
    ])
    if isinstance(result, dict):
        return result.get("data", [])
    return result or []


def scan_idle_databases(start, end) -> list:
    result = _az([
        "graph", "query", "--subscriptions", SUBSCRIPTION,
        "-q", "Resources | where type in~ ('microsoft.dbforpostgresql/servers', "
              "'microsoft.dbforpostgresql/flexibleservers') "
              "| project name, resourceGroup, id, type",
    ])
    servers = result.get("data", []) if isinstance(result, dict) else (result or [])

    idle = []
    for server in servers:
        try:
            avg = _metric_average(server["id"], "active_connections", start, end)
        except Exception as e:
            print(f"[WARN] Could not read connections for {server['id']}: {e}", file=sys.stderr)
            continue
        if avg is None or avg > 0:
            continue
        idle.append({**server, "avg_active_connections_30d": 0.0})
    return idle


SCANS = [
    ("underutilized_vms", "underutilized VMs",       scan_underutilized_vms),
    ("orphaned_disks",    "orphaned disks",          scan_orphaned_disks),
    ("idle_dbs",          "idle PostgreSQL servers", scan_idle_databases),
]


def main() -> None:
    if not ENVIRONMENT:
        sys.exit("[ERROR] ENVIRONMENT is not set")

    now   = datetime.now(timezone.utc)
    end   = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=IDLE_DAYS)
    week  = str(now.date() - timedelta(days=now.date().weekday()))

    scans: dict = {}
    failures = 0
    for key, label, fn in SCANS:
        print(f"[INFO] Scanning for {label}...")
        try:
            items = fn(start, end)
            scans[key] = {"status": "ok", "items": items}
            print(f"[INFO]   {len(items)} found")
        except Exception as e:
            failures += 1
            scans[key] = {"status": "failed", "items": [], "message": str(e)}
            print(f"[WARN]   {label} scan failed: {e}", file=sys.stderr)

    if failures == 0:
        status = "ok"
    elif failures < len(SCANS):
        status = "partial"
    else:
        status = "failed"

    payload = {
        "environment":   ENVIRONMENT,
        "collected_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "week":          week,
        "status":        status,
        "scans":         scans,
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / f"{ENVIRONMENT}.json").write_text(json.dumps(payload, indent=2))
    print(f"[INFO] Wrote {REPORTS_DIR}/{ENVIRONMENT}.json (status: {status})")

    # collected_utc moves every run; the findings are what matter.
    shard = HISTORY_DIR / ENVIRONMENT / f"{week}.json"
    if write_if_changed(shard, payload, ignore_keys=("collected_utc",)):
        carried, pending = update_manifest(HISTORY_DIR, [shard])
        print(f"[INFO] Recorded {ENVIRONMENT}/{week}.json; {pending} file(s) awaiting commit")
    else:
        print(f"[INFO] {ENVIRONMENT}/{week}.json unchanged — nothing to commit")

    # A scan that could not run at all is a failure worth surfacing, but the
    # job is allow_failure so it will not stop the other collectors or the page.
    if status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
