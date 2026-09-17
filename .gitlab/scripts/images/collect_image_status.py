"""Compare approved container image tags with their upstream stable releases.

The registry side uses GitLab's container registry API and deliberately selects
the highest numbered tag. A tag literally named ``latest`` is ignored.

Each image declares its upstream source in image_sources.json because an image
name alone cannot tell us whether its release authority is GitHub, Ubuntu,
Debian, NVIDIA, or somewhere else. The first supported source is GitHub's
latest stable release endpoint.

Environment:
  AI2C_API_RWA or GITLAB_TOKEN  PAT that can read the configured registries
  GITLAB_URL                    GitLab base URL, for example
                                https://code.cdso.army.mil

The CI-provided CI_API_V4_URL or an explicit GITLAB_API_V4_URL can be used
instead of GITLAB_URL.

Optional:
  GITHUB_TOKEN                 raises the GitHub API rate limit
  IMAGE_STATUS_CONFIG          default: this directory/image_sources.json
  IMAGE_STATUS_REPORTS_DIR     default: image_status_reports
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PER_PAGE = 100
MAX_ATTEMPTS = 4
TIMEOUT_SECS = 60
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
VERSION_PATTERN = re.compile(r"^[vV]?(\d+(?:\.\d+)*)$")

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(
    os.environ.get("IMAGE_STATUS_CONFIG", SCRIPT_DIR / "image_sources.json")
)
REPORTS_DIR = Path(
    os.environ.get("IMAGE_STATUS_REPORTS_DIR", "image_status_reports")
)

GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "").strip() or os.environ.get(
    "AI2C_API_RWA", ""
).strip()
_GITLAB_BASE_URL = os.environ.get("GITLAB_URL", "").strip().rstrip("/")
GITLAB_API_URL = (
    os.environ.get("GITLAB_API_V4_URL", "").strip()
    or os.environ.get("CI_API_V4_URL", "").strip()
    or (f"{_GITLAB_BASE_URL}/api/v4" if _GITLAB_BASE_URL else "")
).rstrip("/")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()


def numbered_version(value: str) -> tuple[int, ...] | None:
    match = VERSION_PATTERN.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def normalized_version(value: str) -> str:
    match = VERSION_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError(f"not a numbered stable version: {value}")
    return match.group(1)


def newest_numbered_tag(tags: list[str]) -> str:
    numbered = [
        (version, normalized_version(tag))
        for tag in tags
        if (version := numbered_version(tag)) is not None
    ]
    if not numbered:
        raise RuntimeError("registry contains no numbered stable tags")
    return max(numbered, key=lambda item: item[0])[1]


def api_get_json(
    url: str,
    headers: dict[str, str],
) -> tuple[object, dict[str, str]]:
    backoff = 2

    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECS) as response:
                payload = json.loads(response.read())
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                return payload, response_headers
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            if exc.code in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS:
                retry_after = exc.headers.get("Retry-After", "")
                try:
                    delay = max(1, int(retry_after))
                except ValueError:
                    delay = backoff
                print(
                    f"[WARN] HTTP {exc.code} from {url} "
                    f"(attempt {attempt}/{MAX_ATTEMPTS}); retrying in {delay}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
                backoff *= 2
                continue
            raise RuntimeError(
                f"{url}: HTTP {exc.code}: {body or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < MAX_ATTEMPTS:
                print(
                    f"[WARN] {type(exc).__name__} from {url} "
                    f"(attempt {attempt}/{MAX_ATTEMPTS}); retrying in {backoff}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            raise RuntimeError(f"{url}: {exc}") from exc

    raise RuntimeError(f"unreachable: retries exhausted for {url}")


def gitlab_registry_tags(item: dict[str, Any]) -> list[str]:
    if not GITLAB_TOKEN:
        raise RuntimeError("GITLAB_TOKEN or AI2C_API_RWA must be set")
    if not GITLAB_API_URL:
        raise RuntimeError("GITLAB_API_V4_URL or CI_API_V4_URL must be set")

    project = urllib.parse.quote(str(item["registry_project"]), safe="")
    repository_id = int(item["registry_repository_id"])
    page = 1
    tags: list[str] = []

    while True:
        query = urllib.parse.urlencode(
            {
                "per_page": PER_PAGE,
                "page": page,
            }
        )
        url = (
            f"{GITLAB_API_URL}/projects/{project}/registry/repositories/"
            f"{repository_id}/tags?{query}"
        )
        payload, _ = api_get_json(
            url,
            {"PRIVATE-TOKEN": GITLAB_TOKEN},
        )
        if not isinstance(payload, list):
            raise RuntimeError("GitLab registry tags response was not a list")

        page_tags = [
            str(tag["name"])
            for tag in payload
            if isinstance(tag, dict) and tag.get("name")
        ]
        tags.extend(page_tags)

        if len(payload) < PER_PAGE:
            break
        page += 1

    return tags


def github_latest_release(repository: str) -> tuple[str, str]:
    encoded_repository = "/".join(
        urllib.parse.quote(part, safe="") for part in repository.split("/")
    )
    url = f"https://api.github.com/repos/{encoded_repository}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai2c-kpi-image-status",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    payload, _ = api_get_json(url, headers)
    if not isinstance(payload, dict) or not payload.get("tag_name"):
        raise RuntimeError("GitHub latest release response had no tag_name")
    if payload.get("draft") or payload.get("prerelease"):
        raise RuntimeError("GitHub latest release response was not stable")

    version = normalized_version(str(payload["tag_name"]))
    release_url = str(
        payload.get("html_url")
        or f"https://github.com/{repository}/releases/latest"
    )
    return version, release_url


def upstream_latest_release(item: dict[str, Any]) -> tuple[str, str]:
    upstream = item.get("upstream")
    if not isinstance(upstream, dict):
        raise RuntimeError("upstream configuration is missing")

    source_type = upstream.get("type")
    if source_type == "github_latest_release":
        repository = str(upstream.get("repository") or "").strip()
        if not repository:
            raise RuntimeError("GitHub upstream repository is missing")
        return github_latest_release(repository)

    raise RuntimeError(f"unsupported upstream type: {source_type}")


def compare_versions(current: str, latest: str) -> str:
    current_version = numbered_version(current)
    latest_version = numbered_version(latest)
    if current_version is None or latest_version is None:
        return "unknown"
    width = max(len(current_version), len(latest_version))
    current_version += (0,) * (width - len(current_version))
    latest_version += (0,) * (width - len(latest_version))
    if current_version < latest_version:
        return "outdated"
    if current_version > latest_version:
        return "ahead"
    return "current"


def collect_image(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": str(item.get("name") or "Unnamed image"),
        "image": str(item.get("image") or ""),
        "registry_url": str(item.get("registry_url") or ""),
        "source_url": str(item.get("source_url") or ""),
        "current": None,
        "latest": None,
        "upstream_url": "",
        "status": "unknown",
        "errors": [],
    }

    try:
        result["current"] = newest_numbered_tag(gitlab_registry_tags(item))
    except Exception as exc:
        result["errors"].append(f"registry: {exc}")

    try:
        latest, upstream_url = upstream_latest_release(item)
        result["latest"] = latest
        result["upstream_url"] = upstream_url
    except Exception as exc:
        result["errors"].append(f"upstream: {exc}")

    if result["current"] and result["latest"]:
        result["status"] = compare_versions(
            str(result["current"]),
            str(result["latest"]),
        )

    return result


def load_config(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"image status configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON in {path}: {exc}") from exc

    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"{path} must contain a non-empty JSON list")
    if not all(isinstance(item, dict) for item in payload):
        raise RuntimeError(f"every entry in {path} must be a JSON object")
    return payload


def report_status(items: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status")) for item in items}
    if statuses == {"unknown"}:
        return "failed"
    if "unknown" in statuses:
        return "partial"
    if "outdated" in statuses:
        return "outdated"
    if "ahead" in statuses:
        return "ahead"
    return "current"


def main() -> None:
    try:
        configured_images = load_config(CONFIG_PATH)
    except RuntimeError as exc:
        sys.exit(f"[ERROR] {exc}")

    print(f"[INFO] Checking {len(configured_images)} configured image(s)")
    items: list[dict[str, Any]] = []

    for index, item in enumerate(configured_images, start=1):
        result = collect_image(item)
        items.append(result)
        print(
            f"[INFO] [{index}/{len(configured_images)}] {result['name']}: "
            f"current={result['current'] or 'unavailable'} "
            f"latest={result['latest'] or 'unavailable'} "
            f"status={result['status']}",
            flush=True,
        )
        for error in result["errors"]:
            print(f"[WARN]   {error}", file=sys.stderr, flush=True)

    status = report_status(items)
    report = {
        "collected_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "items": items,
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / "status.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[INFO] Wrote {report_path}; overall status={status}")

    if status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
