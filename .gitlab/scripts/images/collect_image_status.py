"""Compare approved container image tags with their upstream stable releases.

The registry side uses GitLab's container registry API and deliberately selects
the highest numbered tag. A tag literally named ``latest`` is ignored.

Each image declares its current-version and upstream release authorities in
image_sources.json because an image name alone cannot tell us whether its
release authority is GitHub, npm, Microsoft, NVIDIA, or somewhere else. Most
images use their highest numbered registry tag as the current version. A
floating tag can instead run a small command in the approved image to obtain
the actual installed version.

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
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
COMMON_DIR = SCRIPT_DIR.parent / "common"
sys.path.insert(0, str(COMMON_DIR))
from gitlab_api import (  # noqa: E402
    API_MAX_ATTEMPTS,
    API_TIMEOUT_SECONDS,
    GITLAB_PAGE_SIZE,
    RETRYABLE_HTTP_STATUSES,
    gitlab_api_url,
    gitlab_token,
)

from image_status_constants import (
    CONFIG_ENV_VAR,
    CONTAINER_TIMEOUT_SECONDS,
    DEFAULT_CONFIG_FILENAME,
    DEFAULT_REPORTS_DIR,
    DOTNET_DOWNLOAD_BASE_URL,
    DOTNET_RELEASE_INDEX_URL,
    GITHUB_API_VERSION,
    HTTP_USER_AGENT,
    NVIDIA_CUDA_ARCHIVE_URL,
    REPORT_FILENAME,
    REPORTS_DIR_ENV_VAR,
    STATUS_AHEAD,
    STATUS_CURRENT,
    STATUS_OUTDATED,
    STATUS_UNKNOWN,
)

VERSION_PATTERN = re.compile(r"^[vV]?(\d+(?:\.\d+)*)$")
RELEASE_VERSION_PATTERN = re.compile(
    r"^[vV]?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
CUDA_VERSION_PATTERN = re.compile(r"\bCUDA Toolkit\s+(\d+\.\d+(?:\.\d+)?)\b")

CONFIG_PATH = Path(
    os.environ.get(CONFIG_ENV_VAR, SCRIPT_DIR / DEFAULT_CONFIG_FILENAME)
)
REPORTS_DIR = Path(
    os.environ.get(REPORTS_DIR_ENV_VAR, DEFAULT_REPORTS_DIR)
)

GITLAB_TOKEN = gitlab_token()
GITLAB_API_URL = gitlab_api_url(allow_base_url=True)
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


def release_version(
    value: str,
) -> tuple[tuple[int, ...], tuple[int | str, ...] | None] | None:
    match = RELEASE_VERSION_PATTERN.fullmatch(value.strip())
    if not match:
        return None
    core = tuple(int(part) for part in match.group(1).split("."))
    prerelease_text = match.group(2)
    if prerelease_text is None:
        return core, None
    prerelease = tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in prerelease_text.split(".")
    )
    return core, prerelease


def normalized_release_version(value: str) -> str:
    match = RELEASE_VERSION_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError(f"not a supported release version: {value}")
    core = match.group(1)
    prerelease = match.group(2)
    return core + (f"-{prerelease}" if prerelease else "")


def newest_numbered_tag(tags: list[str]) -> str:
    numbered = [
        (version, normalized_version(tag))
        for tag in tags
        if (version := numbered_version(tag)) is not None
    ]
    if not numbered:
        raise RuntimeError("registry contains no numbered stable tags")
    return max(numbered, key=lambda item: item[0])[1]


def _api_get_bytes(
    url: str,
    headers: dict[str, str],
) -> tuple[bytes, dict[str, str]]:
    backoff = 2

    for attempt in range(1, API_MAX_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(
                request,
                timeout=API_TIMEOUT_SECONDS,
            ) as response:
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                return response.read(), response_headers
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            if (
                exc.code in RETRYABLE_HTTP_STATUSES
                and attempt < API_MAX_ATTEMPTS
            ):
                retry_after = exc.headers.get("Retry-After", "")
                try:
                    delay = max(1, int(retry_after))
                except ValueError:
                    delay = backoff
                print(
                    f"[WARN] HTTP {exc.code} from {url} "
                    f"(attempt {attempt}/{API_MAX_ATTEMPTS}); "
                    f"retrying in {delay}s",
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
            if attempt < API_MAX_ATTEMPTS:
                print(
                    f"[WARN] {type(exc).__name__} from {url} "
                    f"(attempt {attempt}/{API_MAX_ATTEMPTS}); "
                    f"retrying in {backoff}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            raise RuntimeError(f"{url}: {exc}") from exc

    raise RuntimeError(f"unreachable: retries exhausted for {url}")


def api_get_json(
    url: str,
    headers: dict[str, str],
) -> tuple[object, dict[str, str]]:
    payload, response_headers = _api_get_bytes(url, headers)
    return json.loads(payload), response_headers


def api_get_text(url: str, headers: dict[str, str]) -> str:
    payload, _ = _api_get_bytes(url, headers)
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{url}: response was not UTF-8 text") from exc


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
                "per_page": GITLAB_PAGE_SIZE,
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

        if len(payload) < GITLAB_PAGE_SIZE:
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
        "User-Agent": HTTP_USER_AGENT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
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


def npm_dist_tag_release(package: str, dist_tag: str) -> tuple[str, str]:
    encoded_package = urllib.parse.quote(package, safe="")
    url = f"https://registry.npmjs.org/{encoded_package}"
    payload, _ = api_get_json(
        url,
        {"Accept": "application/json", "User-Agent": HTTP_USER_AGENT},
    )
    dist_tags = payload.get("dist-tags") if isinstance(payload, dict) else None
    if not isinstance(dist_tags, dict) or not dist_tags.get(dist_tag):
        raise RuntimeError(f"npm response had no {dist_tag} dist-tag")

    version = normalized_release_version(str(dist_tags[dist_tag]))
    package_url = "https://www.npmjs.com/package/" + urllib.parse.quote(
        package,
        safe="@/",
    )
    return version, package_url


def dotnet_latest_release(
    channel: str,
    component: str,
) -> tuple[str, str]:
    url = DOTNET_RELEASE_INDEX_URL
    payload, _ = api_get_json(
        url,
        {"Accept": "application/json", "User-Agent": HTTP_USER_AGENT},
    )
    if not isinstance(payload, dict) or not isinstance(
        payload.get("releases-index"),
        list,
    ):
        raise RuntimeError(".NET release index response had no releases-index")

    fields = {
        "sdk": "latest-sdk",
        "runtime": "latest-runtime",
        "release": "latest-release",
    }
    field = fields.get(component)
    if not field:
        raise RuntimeError(f"unsupported .NET component: {component}")

    release = next(
        (
            entry
            for entry in payload["releases-index"]
            if isinstance(entry, dict)
            and str(entry.get("channel-version") or "") == channel
        ),
        None,
    )
    if not release or not release.get(field):
        raise RuntimeError(
            f".NET release index had no {component} version for channel {channel}"
        )

    version = normalized_version(str(release[field]))
    release_url = DOTNET_DOWNLOAD_BASE_URL + "/" + urllib.parse.quote(
        channel,
        safe="",
    )
    return version, release_url


class CudaArchiveParser(HTMLParser):
    """Collect stable CUDA Toolkit versions and their release links."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self._href: str | None = None
        self._text: list[str] = []
        self.releases: list[tuple[str, str]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "a":
            return
        self._href = next((value for key, value in attrs if key == "href"), None)
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return

        label = " ".join("".join(self._text).split())
        match = CUDA_VERSION_PATTERN.search(label)
        unstable_markers = (
            "developer preview",
            "release candidate",
            "alpha",
            "beta",
            " rc",
        )
        if match and not any(
            marker in label.casefold() for marker in unstable_markers
        ):
            self.releases.append(
                (
                    normalized_version(match.group(1)),
                    urllib.parse.urljoin(self.base_url, self._href),
                )
            )
        self._href = None
        self._text = []


def nvidia_cuda_latest_release() -> tuple[str, str]:
    url = NVIDIA_CUDA_ARCHIVE_URL
    page = api_get_text(url, {"User-Agent": HTTP_USER_AGENT})
    parser = CudaArchiveParser(url)
    parser.feed(page)
    if not parser.releases:
        raise RuntimeError("NVIDIA CUDA archive contained no stable toolkit versions")
    return max(
        parser.releases,
        key=lambda release: numbered_version(release[0]) or (),
    )


def container_command_version(
    item: dict[str, Any],
    registry_tag: str,
    current: dict[str, Any],
) -> str:
    image = str(item.get("image") or "").strip()
    if not image:
        raise RuntimeError("image is missing for container version command")

    entrypoint = str(current.get("entrypoint") or "").strip()
    args = current.get("args") or []
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise RuntimeError("container version command args must be a list of strings")

    command = ["docker", "run", "--rm", "--pull", "always"]
    if entrypoint:
        command.extend(["--entrypoint", entrypoint])
    command.append(f"{image}:{registry_tag}")
    command.extend(args)

    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=CONTAINER_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("docker executable was not found on the runner") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"container version command timed out after "
            f"{CONTAINER_TIMEOUT_SECONDS}s"
        ) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if len(detail) > 500:
            detail = detail[-500:]
        raise RuntimeError(
            f"container version command exited {completed.returncode}"
            + (f": {detail}" if detail else "")
        )

    versions = [
        normalized_release_version(line.strip())
        for line in completed.stdout.splitlines()
        if RELEASE_VERSION_PATTERN.fullmatch(line.strip())
    ]
    if not versions:
        raise RuntimeError("container version command returned no version")
    return versions[-1]


def current_image_version(item: dict[str, Any], registry_tag: str) -> str:
    current = item.get("current_version")
    if current is None:
        return normalized_version(registry_tag)
    if not isinstance(current, dict):
        raise RuntimeError("current_version configuration must be an object")

    source_type = current.get("type")
    if source_type == "container_command":
        return container_command_version(item, registry_tag, current)
    raise RuntimeError(f"unsupported current version type: {source_type}")


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

    if source_type == "npm_dist_tag":
        package = str(upstream.get("package") or "").strip()
        dist_tag = str(upstream.get("dist_tag") or "").strip()
        if not package:
            raise RuntimeError("npm upstream package is missing")
        if not dist_tag:
            raise RuntimeError("npm upstream dist-tag is missing")
        return npm_dist_tag_release(package, dist_tag)

    if source_type == "dotnet_releases_index":
        channel = str(upstream.get("channel") or "").strip()
        component = str(upstream.get("component") or "").strip()
        if not channel:
            raise RuntimeError(".NET upstream channel is missing")
        if not component:
            raise RuntimeError(".NET upstream component is missing")
        return dotnet_latest_release(channel, component)

    if source_type == "nvidia_cuda_archive":
        return nvidia_cuda_latest_release()

    raise RuntimeError(f"unsupported upstream type: {source_type}")


def compare_versions(current: str, latest: str) -> str:
    current_version = release_version(current)
    latest_version = release_version(latest)
    if current_version is None or latest_version is None:
        return STATUS_UNKNOWN

    current_core, current_prerelease = current_version
    latest_core, latest_prerelease = latest_version
    width = max(len(current_core), len(latest_core))
    current_core += (0,) * (width - len(current_core))
    latest_core += (0,) * (width - len(latest_core))
    if current_core < latest_core:
        return STATUS_OUTDATED
    if current_core > latest_core:
        return STATUS_AHEAD

    if current_prerelease is None and latest_prerelease is None:
        return STATUS_CURRENT
    if current_prerelease is None:
        return STATUS_AHEAD
    if latest_prerelease is None:
        return STATUS_OUTDATED

    for current_part, latest_part in zip(current_prerelease, latest_prerelease):
        if current_part == latest_part:
            continue
        if isinstance(current_part, int) and isinstance(latest_part, str):
            return STATUS_OUTDATED
        if isinstance(current_part, str) and isinstance(latest_part, int):
            return STATUS_AHEAD
        if isinstance(current_part, int) and isinstance(latest_part, int):
            return (
                STATUS_OUTDATED if current_part < latest_part else STATUS_AHEAD
            )
        if isinstance(current_part, str) and isinstance(latest_part, str):
            return (
                STATUS_OUTDATED if current_part < latest_part else STATUS_AHEAD
            )
        return STATUS_UNKNOWN

    if len(current_prerelease) < len(latest_prerelease):
        return STATUS_OUTDATED
    if len(current_prerelease) > len(latest_prerelease):
        return STATUS_AHEAD
    return STATUS_CURRENT


def collect_image(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": str(item.get("name") or "Unnamed image"),
        "image": str(item.get("image") or ""),
        "registry_url": str(item.get("registry_url") or ""),
        "source_url": str(item.get("source_url") or ""),
        "registry_tag": None,
        "current": None,
        "latest": None,
        "upstream_url": "",
        "status": STATUS_UNKNOWN,
        "errors": [],
    }

    registry_tag: str | None = None
    try:
        registry_tag = newest_numbered_tag(gitlab_registry_tags(item))
        result["registry_tag"] = registry_tag
    except Exception as exc:
        result["errors"].append(f"registry: {exc}")

    if registry_tag:
        try:
            result["current"] = current_image_version(item, registry_tag)
        except Exception as exc:
            result["errors"].append(f"current version: {exc}")

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
    if statuses == {STATUS_UNKNOWN}:
        return "failed"
    if STATUS_UNKNOWN in statuses:
        return "partial"
    if STATUS_OUTDATED in statuses:
        return STATUS_OUTDATED
    if STATUS_AHEAD in statuses:
        return STATUS_AHEAD
    return STATUS_CURRENT


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
    report_path = REPORTS_DIR / REPORT_FILENAME
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[INFO] Wrote {report_path}; overall status={status}")

    if status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
