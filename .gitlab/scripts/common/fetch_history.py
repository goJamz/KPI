"""
Azure Cost History — GitLab repository archive fetch

Downloads a history tree from the 'data' branch and writes it to the working
directory, preserving the <environment>/<iso-monday>.json layout.

The history root is whatever HISTORY_DIR names — `cost_history` by default —
and the same name is used on the data branch. Nothing here knows about costs,
so a second KPI collector can persist `resource_history/` through the same code
rather than growing its own copy.


The whole subtree arrives as a single tar.gz from the repository archive
endpoint rather than one request per file. With one file per environment per
week that difference matters: a full backfill produces well over a hundred
shards, and fetching them individually meant a hundred-plus sequential HTTPS
round trips where any one transient failure lost the job. One request stays one
request however much history accumulates.

Counterpart to persist_history.py — both go through the API, so no git worktree
or signed-commit access is needed.

Required CI/CD variables:
  AI2C_API_RWA        — bot PAT (already configured at group/instance level)

Auto-set by GitLab:
  CI_API_V4_URL, CI_PROJECT_ID
"""

import io
import json
import os
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


TOKEN       = os.environ["AI2C_API_RWA"]
API_URL     = os.environ["CI_API_V4_URL"].rstrip("/")
PROJECT_ID  = os.environ["CI_PROJECT_ID"]
DATA_BRANCH = "data"
HISTORY_DIR = Path(os.environ.get("HISTORY_DIR", "cost_history"))
# The directory name is also the path on the data branch, so one variable
# selects the domain on both sides.
REMOTE_ROOT = HISTORY_DIR.name

# Transient conditions worth another attempt: the server asking us to slow down,
# and its bad days. Anything else is a real answer and retrying only hides it.
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS   = 4
TIMEOUT_SECS   = 60


def _request(method: str, path: str, *, binary: bool = False):
    """Call the API, retrying transient network and server failures.

    Connection-level errors (URLError, and the SSL EOF that surfaces as one)
    are retried as well as retryable HTTP statuses — a dropped TLS handshake
    partway through a fetch is not a reason to discard the run.
    """
    url = f"{API_URL}/projects/{urllib.parse.quote(str(PROJECT_ID), safe='')}{path}"
    backoff = 2

    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": TOKEN}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
                payload = resp.read()
                return (payload if binary else json.loads(payload)), resp.status
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                print(f"[WARN] HTTP {e.code} on {path} "
                      f"(attempt {attempt}/{MAX_ATTEMPTS}) — retrying in {backoff}s",
                      file=sys.stderr)
                time.sleep(backoff)
                backoff *= 2
                continue
            if binary:
                return body, e.code
            return (json.loads(body) if body else {}), e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < MAX_ATTEMPTS:
                print(f"[WARN] {type(e).__name__} on {path} "
                      f"(attempt {attempt}/{MAX_ATTEMPTS}) — retrying in {backoff}s",
                      file=sys.stderr)
                time.sleep(backoff)
                backoff *= 2
                continue
            raise

    raise RuntimeError(f"unreachable: retries exhausted for {path}")


def branch_exists(branch: str) -> bool:
    _, status = _request("GET", f"/repository/branches/{urllib.parse.quote(branch, safe='')}")
    return status == 200


def download_archive(branch: str, subpath: str) -> bytes | None:
    """The given subtree of `branch` as tar.gz bytes, or None if it is not there."""
    query = urllib.parse.urlencode({"sha": branch, "path": subpath})
    payload, status = _request("GET", f"/repository/archive.tar.gz?{query}", binary=True)
    if status != 200:
        return None
    return payload


def extract_history(archive: bytes, subpath: str, dest: Path) -> tuple:
    """Unpack the archive's `subpath` entries into `dest`.

    Archive members are prefixed with a generated top-level directory
    (<project>-<ref>-<sha>/), which is stripped. Destination paths are built
    here rather than taken from the archive, so a member cannot write outside
    dest.
    """
    written      = 0
    environments = set()

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue

            parts = Path(member.name).parts
            if len(parts) < 2 or parts[1] != subpath:
                continue
            relative = Path(*parts[2:])
            if not relative.parts or relative.suffix != ".json":
                continue

            extracted = tar.extractfile(member)
            if extracted is None:
                continue

            out = dest / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(extracted.read())

            if len(relative.parts) > 1:
                environments.add(relative.parts[0])
            written += 1

    return written, environments


def main() -> None:
    if not branch_exists(DATA_BRANCH):
        print("[INFO] data branch not found — no history to fetch")
        return

    archive = download_archive(DATA_BRANCH, REMOTE_ROOT)
    if archive is None:
        print(f"[INFO] {REMOTE_ROOT}/ not found on data branch — no history to fetch")
        return

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    written, environments = extract_history(archive, REMOTE_ROOT, HISTORY_DIR)

    if written:
        detail = f" across {len(environments)} environment(s)" if environments else ""
        print(f"[INFO] Fetched {written} history file(s) from data branch{detail}")
    else:
        print(f"[WARN] data branch has {REMOTE_ROOT}/ but no files were extracted")


if __name__ == "__main__":
    main()
