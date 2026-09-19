"""
GitLab pipeline KPI collector — AI2C group CI activity.

Collects jobs across every project in the configured root group for the last
complete ISO week, then writes:

  pipeline_reports/ai2c.json                artifact the page reads
  pipeline_history/ai2c/<monday>.json       committed to the data branch

The page is static and history-backed, so this collector keeps the same shape
as the Azure KPI domains rather than depending on Magpie's Postgres path.

Environment:
  AI2C_API_RWA or GITLAB_TOKEN  PAT with read access to the AI2C projects
  CI_API_V4_URL or GITLAB_API_V4_URL

Optional:
  GITLAB_ROOT_GROUP             root group path or ID; defaults to the first
                                segment of CI_PROJECT_NAMESPACE
  PIPELINE_METRICS_WORKERS      concurrent project scans; default: 32
  PIPELINE_REPORTS_DIR          default: pipeline_reports
  HISTORY_DIR                   default: pipeline_history
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as time_t, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from gitlab_api import (  # noqa: E402
    API_MAX_ATTEMPTS,
    API_TIMEOUT_SECONDS,
    GITLAB_PAGE_SIZE,
    RETRYABLE_HTTP_STATUSES,
    gitlab_api_url,
    gitlab_token,
)
from history_io import update_manifest, write_if_changed  # noqa: E402


DEFAULT_WORKERS = 32


def worker_count() -> int:
    raw_value = os.environ.get("PIPELINE_METRICS_WORKERS", "").strip()
    if not raw_value:
        return DEFAULT_WORKERS

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"PIPELINE_METRICS_WORKERS must be an integer, got: {raw_value}"
        ) from exc

    if value < 1:
        raise ValueError(
            f"PIPELINE_METRICS_WORKERS must be at least 1, got: {value}"
        )

    return value


def _default_root_group() -> str:
    namespace = os.environ.get("CI_PROJECT_NAMESPACE", "").strip()
    if namespace:
        return namespace.split("/", 1)[0]
    return ""


TOKEN = gitlab_token()
API_URL = gitlab_api_url()
ROOT_GROUP = os.environ.get("GITLAB_ROOT_GROUP", "").strip() or _default_root_group()
REPORTS_DIR = Path(os.environ.get("PIPELINE_REPORTS_DIR", "pipeline_reports"))
HISTORY_DIR = Path(os.environ.get("HISTORY_DIR", "pipeline_history"))


def parse_gitlab_datetime(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso_week_window(today: date | None = None) -> tuple[date, date, datetime, datetime]:
    today = today or datetime.now(timezone.utc).date()
    this_monday = today - timedelta(days=today.weekday())
    period_start = this_monday - timedelta(days=7)
    period_end = this_monday - timedelta(days=1)
    start_at = datetime.combine(period_start, time_t.min, tzinfo=timezone.utc)
    end_exclusive = datetime.combine(this_monday, time_t.min, tzinfo=timezone.utc)
    return period_start, period_end, start_at, end_exclusive


def api_get(path: str, params: dict[str, object] | None = None) -> tuple[object, dict[str, str]]:
    if not TOKEN:
        raise RuntimeError("GITLAB_TOKEN or AI2C_API_RWA must be set")
    if not API_URL:
        raise RuntimeError("GITLAB_API_V4_URL or CI_API_V4_URL must be set")

    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)
    url = f"{API_URL}{path}{query}"
    backoff = 2

    for attempt in range(1, API_MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            url,
            headers={"PRIVATE-TOKEN": TOKEN},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
                payload = resp.read()
                headers = {k: v for k, v in resp.headers.items()}
                return json.loads(payload), headers
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            if (
                exc.code in RETRYABLE_HTTP_STATUSES
                and attempt < API_MAX_ATTEMPTS
            ):
                print(
                    f"[WARN] HTTP {exc.code} on {path} "
                    f"(attempt {attempt}/{API_MAX_ATTEMPTS}) — "
                    f"retrying in {backoff}s",
                    file=sys.stderr,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            raise RuntimeError(f"{path}: HTTP {exc.code}: {body or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < API_MAX_ATTEMPTS:
                print(
                    f"[WARN] {type(exc).__name__} on {path} "
                    f"(attempt {attempt}/{API_MAX_ATTEMPTS}) — "
                    f"retrying in {backoff}s",
                    file=sys.stderr,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            raise RuntimeError(f"{path}: {exc}") from exc

    raise RuntimeError(f"unreachable: retries exhausted for {path}")


def get_projects(root_group: str) -> list[dict[str, object]]:
    projects: list[dict[str, object]] = []
    page = 1
    encoded = urllib.parse.quote(root_group, safe="")

    while True:
        payload, _ = api_get(
            f"/groups/{encoded}/projects",
            {
                "include_subgroups": "true",
                "with_shared": "false",
                "archived": "false",
                "per_page": GITLAB_PAGE_SIZE,
                "page": page,
            },
        )
        page_projects = payload if isinstance(payload, list) else []
        if not page_projects:
            break
        projects.extend(page_projects)
        if len(page_projects) < GITLAB_PAGE_SIZE:
            break
        page += 1

    return projects


def get_recent_jobs(
    project_id: int,
    start_at: datetime,
    end_exclusive: datetime,
) -> list[dict[str, object]]:
    jobs: list[dict[str, object]] = []
    next_url = f"{API_URL}/projects/{project_id}/jobs"
    params: dict[str, object] | None = {
        "pagination": "keyset",
        "per_page": GITLAB_PAGE_SIZE,
        "order_by": "id",
        "sort": "desc",
    }

    while next_url:
        path = next_url.removeprefix(API_URL)
        payload, headers = api_get(path, params)
        page_jobs = payload if isinstance(payload, list) else []
        if not page_jobs:
            break

        page_has_in_window = False
        page_has_newer_than_start = False
        for job in page_jobs:
            created_at = parse_gitlab_datetime(str(job.get("created_at")))
            if created_at >= start_at:
                page_has_newer_than_start = True
            if start_at <= created_at < end_exclusive:
                jobs.append(job)
                page_has_in_window = True

        if not page_has_newer_than_start:
            break

        next_url = ""
        link = headers.get("Link", "")
        if 'rel="next"' in link:
            for chunk in link.split(","):
                if 'rel="next"' not in chunk:
                    continue
                start = chunk.find("<")
                end = chunk.find(">", start + 1)
                if start != -1 and end != -1:
                    next_url = chunk[start + 1:end]
                break

        if not next_url and headers.get("X-Next-Page"):
            next_url = f"{API_URL}/projects/{project_id}/jobs"
            params = {
                "pagination": "keyset",
                "per_page": GITLAB_PAGE_SIZE,
                "order_by": "id",
                "sort": "desc",
                "page": headers["X-Next-Page"],
            }
            continue

        params = None
        if not next_url and page_has_in_window:
            break

    return jobs


def collect_jobs(
    projects: list[dict[str, object]],
    start_at: datetime,
    end_exclusive: datetime,
    workers: int,
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    jobs: list[dict[str, object]] = []
    project_failures: list[dict[str, str]] = []
    total_projects = len(projects)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_project = {
            executor.submit(
                get_recent_jobs,
                int(project["id"]),
                start_at,
                end_exclusive,
            ): project
            for project in projects
        }

        for completed_count, future in enumerate(
            as_completed(future_to_project),
            start=1,
        ):
            project = future_to_project[future]
            project_id = int(project["id"])
            project_path = str(project["path_with_namespace"])

            try:
                project_jobs = future.result()
            except Exception as exc:
                project_failures.append(
                    {"project": project_path, "error": str(exc)}
                )
                print(
                    f"[WARN] [{completed_count}/{total_projects}] "
                    f"{project_path} failed: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                continue

            for job in project_jobs:
                job["project_id"] = project_id
                job["project_path"] = project_path
            jobs.extend(project_jobs)
            print(
                f"[INFO] [{completed_count}/{total_projects}] "
                f"{project_path}: {len(project_jobs)} job(s) in window",
                flush=True,
            )

    project_failures.sort(key=lambda failure: failure["project"])
    return jobs, project_failures


def summarize_jobs(jobs: list[dict[str, object]]) -> dict[str, object]:
    counts = {"total": 0, "success": 0, "failed": 0, "other": 0}
    for job in jobs:
        counts["total"] += 1
        status = str(job.get("status") or "")
        if status == "success":
            counts["success"] += 1
        elif status == "failed":
            counts["failed"] += 1
        else:
            counts["other"] += 1

    terminal = counts["success"] + counts["failed"]
    counts["success_rate"] = round((counts["success"] / terminal) * 100, 1) if terminal else None
    counts["failure_rate"] = round((counts["failed"] / terminal) * 100, 1) if terminal else None
    counts["terminal"] = terminal
    return counts


def pipeline_status_rank(status: str) -> tuple[int, int]:
    if status == "failed":
        return (4, 1)
    if status == "success":
        return (4, 0)
    if status == "canceled":
        return (3, 0)
    if status in {"manual", "skipped"}:
        return (2, 0)
    if status in {"running", "pending", "created", "preparing", "waiting_for_resource"}:
        return (1, 0)
    return (0, 0)


def summarize_pipelines(jobs: list[dict[str, object]]) -> dict[str, object]:
    pipelines: dict[tuple[int, int], str] = {}

    for job in jobs:
        project_id = int(job.get("project_id") or job.get("project", {}).get("id") or 0)
        pipeline = job.get("pipeline") or {}
        pipeline_id = pipeline.get("id")
        if not project_id or not pipeline_id:
            continue
        key = (project_id, int(pipeline_id))
        status = str(pipeline.get("status") or "")
        current = pipelines.get(key, "")
        if pipeline_status_rank(status) >= pipeline_status_rank(current):
            pipelines[key] = status

    counts = {"total": len(pipelines), "success": 0, "failed": 0, "other": 0}
    for status in pipelines.values():
        if status == "success":
            counts["success"] += 1
        elif status == "failed":
            counts["failed"] += 1
        else:
            counts["other"] += 1

    terminal = counts["success"] + counts["failed"]
    counts["success_rate"] = round((counts["success"] / terminal) * 100, 1) if terminal else None
    counts["failure_rate"] = round((counts["failed"] / terminal) * 100, 1) if terminal else None
    counts["terminal"] = terminal
    return counts


def main() -> None:
    if not TOKEN:
        sys.exit("[ERROR] GITLAB_TOKEN or AI2C_API_RWA must be set")
    if not API_URL:
        sys.exit("[ERROR] GITLAB_API_V4_URL or CI_API_V4_URL must be set")
    if not ROOT_GROUP:
        sys.exit("[ERROR] GITLAB_ROOT_GROUP is not set and could not be inferred")

    try:
        workers = worker_count()
    except ValueError as exc:
        sys.exit(f"[ERROR] {exc}")

    period_start, period_end, start_at, end_exclusive = iso_week_window()
    collected_at = datetime.now(timezone.utc)

    print(
        f"[INFO] Collecting GitLab CI metrics for {ROOT_GROUP} "
        f"from {period_start} to {period_end}"
    )

    projects = get_projects(ROOT_GROUP)
    print(
        f"[INFO] Found {len(projects)} project(s) in root group {ROOT_GROUP}; "
        f"scanning with {workers} worker(s)"
    )

    jobs, project_failures = collect_jobs(
        projects,
        start_at,
        end_exclusive,
        workers,
    )

    job_counts = summarize_jobs(jobs)
    pipeline_counts = summarize_pipelines(jobs)

    if not projects:
        status = "failed"
    elif len(project_failures) == len(projects):
        status = "failed"
    elif project_failures:
        status = "partial"
    else:
        status = "ok"

    payload = {
        "scope": ROOT_GROUP,
        "collected_utc": collected_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period_start": str(period_start),
        "period_end": str(period_end),
        "status": status,
        "projects_scanned": len(projects) - len(project_failures),
        "projects_total": len(projects),
        "project_failures": project_failures,
        "job_counts": job_counts,
        "pipeline_counts": pipeline_counts,
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / "ai2c.json"
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[INFO] Wrote {report_path}")

    shard = HISTORY_DIR / ROOT_GROUP / f"{period_start}.json"
    if write_if_changed(shard, payload, ignore_keys=("collected_utc",)):
        carried, pending = update_manifest(HISTORY_DIR, [shard])
        print(f"[INFO] Recorded {ROOT_GROUP}/{period_start}.json; {pending} file(s) awaiting commit")
    else:
        print(f"[INFO] {ROOT_GROUP}/{period_start}.json unchanged — nothing to commit")

    print(
        f"[INFO] Totals: {job_counts['total']} job(s), {pipeline_counts['total']} pipeline(s), "
        f"job success {job_counts['success_rate'] if job_counts['success_rate'] is not None else 'n/a'}%, "
        f"pipeline success {pipeline_counts['success_rate'] if pipeline_counts['success_rate'] is not None else 'n/a'}%"
    )

    if status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
