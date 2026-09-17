"""
Azure Cost History — GitLab Commits API persistence

Writes a history tree to the 'data' branch via the GitLab Commits API,
preserving the <environment>/<iso-monday>.json layout.

The history root is whatever HISTORY_DIR names — `cost_history` by default —
and the same name is used on the data branch. Nothing here knows about costs,
so a second KPI collector can persist `resource_history/` through the same code
rather than growing its own copy.
 Because the commit is
created server-side through the API, it bypasses push rules (including GPG
signing requirements).

Required CI/CD variables:
  AI2C_API_RWA        — bot PAT with api + write_repository scope
                        (already configured at group/instance level)

Auto-set by GitLab:
  CI_API_V4_URL       — e.g. https://gitlab.example.com/api/v4
  CI_PROJECT_ID       — numeric project ID
  CI_DEFAULT_BRANCH   — default branch name (used as ref when creating data branch)
  CI_PIPELINE_CREATED_AT — timestamp used in commit message
"""

import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


TOKEN        = os.environ["AI2C_API_RWA"]
API_URL      = os.environ["CI_API_V4_URL"].rstrip("/")
PROJECT_ID   = os.environ["CI_PROJECT_ID"]
DEFAULT_REF  = os.environ.get("CI_DEFAULT_BRANCH", "main")
PIPELINE_AT  = os.environ.get("CI_PIPELINE_CREATED_AT", "")
DATA_BRANCH  = "data"
HISTORY_DIR  = Path(os.environ.get("HISTORY_DIR", "cost_history"))
# The directory name is also the path on the data branch, so one variable
# selects the domain on both sides.
REMOTE_ROOT  = HISTORY_DIR.name


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

# Transient conditions worth another attempt: the server asking us to slow down,
# and its bad days. Anything else is a real answer and retrying only hides it.
RETRY_STATUSES = {429, 500, 502, 503, 504}

# Three environment jobs commit to this branch at once. Each writes a disjoint
# path, so a race is only ever about the branch ref moving underneath us —
# re-posting against the new head succeeds. GitLab signals that inconsistently,
# so both the status and the message are checked.
CONFLICT_STATUSES = {409}
CONFLICT_HINTS    = ("conflict", "could not update", "changed since",
                     "stale", "fast-forward", "is not a valid",
                     # What GitLab actually returns when two matrix jobs commit
                     # to the same branch at once. The retry machinery was right;
                     # this phrasing simply was not among the hints, so a
                     # recoverable race was treated as a hard failure.
                     "reference update", "does not point to expected object")
MAX_ATTEMPTS   = 4
TIMEOUT_SECS   = 60


def _looks_like_conflict(status: int, payload: str) -> bool:
    if status in CONFLICT_STATUSES:
        return True
    if status != 400:
        return False
    lowered = payload.lower()
    return any(hint in lowered for hint in CONFLICT_HINTS)


def _api(method: str, path: str, body: dict | None = None):
    """Call the API, retrying transient network and server failures.

    This runs at the end of a backfill that may have taken hours, so losing the
    commit to a dropped connection is expensive out of all proportion to the
    blip that caused it.
    """
    url  = f"{API_URL}/projects/{urllib.parse.quote(PROJECT_ID, safe='')}{path}"
    data = json.dumps(body).encode() if body else None
    backoff = 2

    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            url,
            data=data,
            headers={"PRIVATE-TOKEN": TOKEN, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
                return json.loads(resp.read()), resp.status
        except urllib.error.HTTPError as e:
            payload = e.read().decode()
            if _looks_like_conflict(e.code, payload) and attempt < MAX_ATTEMPTS:
                # Spread the retries so three racing jobs do not collide again
                # on the same beat.
                wait = backoff + random.uniform(0, backoff)
                print(f"[WARN] Branch moved under commit to {path} "
                      f"(attempt {attempt}/{MAX_ATTEMPTS}) — retrying in {wait:.1f}s",
                      file=sys.stderr)
                time.sleep(wait)
                backoff *= 2
                continue
            if e.code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                print(f"[WARN] HTTP {e.code} on {path} "
                      f"(attempt {attempt}/{MAX_ATTEMPTS}) — retrying in {backoff}s",
                      file=sys.stderr)
                time.sleep(backoff)
                backoff *= 2
                continue
            return (json.loads(payload) if payload else {}), e.code
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
    _, status = _api("GET", f"/repository/branches/{urllib.parse.quote(branch, safe='')}")
    return status == 200


def existing_files(path: str, branch: str) -> set:
    """Paths already on the branch below `path`, as one paginated listing.

    Each commit action has to say whether it creates or updates. Asking the
    files API once per file was fine for three files; with one file per
    environment per week it would be a hundred-odd requests before every
    commit, repeated on each checkpoint. One recursive listing answers the
    same question.
    """
    encoded_path   = urllib.parse.quote(path, safe="")
    encoded_branch = urllib.parse.quote(branch, safe="")

    found: set = set()
    page = 1
    while True:
        result, status = _api(
            "GET",
            f"/repository/tree?path={encoded_path}&ref={encoded_branch}"
            f"&recursive=true&per_page=100&page={page}",
        )
        if status != 200 or not isinstance(result, list) or not result:
            break
        found.update(item["path"] for item in result if item.get("type") == "blob")
        if len(result) < 100:
            break
        page += 1

    return found


def ensure_branch(branch: str, ref: str) -> None:
    if branch_exists(branch):
        return
    result, status = _api("POST", "/repository/branches", {"branch": branch, "ref": ref})
    if status not in (200, 201):
        print(f"[ERROR] Could not create branch '{branch}': {result}", file=sys.stderr)
        sys.exit(1)
    print(f"[INFO] Created branch '{branch}' from '{ref}'")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def files_to_commit() -> list:
    """Which files to send, preferring the manifest update_history wrote.

    The manifest lists only what changed. Without it every commit would carry
    the whole history — a hundred-plus unchanged files on each checkpoint of a
    backfill, and on every weekly run. Falling back to everything keeps this
    working if the manifest is missing.
    """
    manifest = HISTORY_DIR / ".manifest"
    if manifest.exists():
        listed = [HISTORY_DIR / line.strip()
                  for line in manifest.read_text().splitlines() if line.strip()]
        present = [p for p in listed if p.is_file()]
        missing = len(listed) - len(present)
        if missing:
            print(f"[WARN] {missing} manifest entr(ies) no longer on disk — skipped",
                  file=sys.stderr)
        return sorted(present)

    print("[INFO] No manifest found — committing every history file.")
    return sorted(HISTORY_DIR.rglob("*.json"))


def main() -> None:
    history_files = files_to_commit()
    if not history_files:
        print(f"[INFO] Nothing changed in {HISTORY_DIR} — nothing to persist.")
        return

    ensure_branch(DATA_BRANCH, DEFAULT_REF)

    already_present = existing_files(REMOTE_ROOT, DATA_BRANCH)

    actions  = []
    created  = 0
    updated  = 0
    for path in history_files:
        relative  = path.relative_to(HISTORY_DIR)
        file_path = f"{REMOTE_ROOT}/{relative.as_posix()}"
        if file_path in already_present:
            action   = "update"
            updated += 1
        else:
            action   = "create"
            created += 1
        actions.append({
            "action":    action,
            "file_path": file_path,
            "content":   path.read_text(),
        })

    result, status = _api("POST", "/repository/commits", {
        "branch":         DATA_BRANCH,
        "commit_message": f"chore: cost history update {PIPELINE_AT}",
        "actions":        actions,
    })

    if status not in (200, 201):
        print(f"[ERROR] Commit failed (HTTP {status}): {result}", file=sys.stderr)
        sys.exit(1)

    # Clear the pending list only now. Leaving it in place after a failure is
    # what lets the next checkpoint pick up this batch as well as its own.
    manifest = HISTORY_DIR / ".manifest"
    if manifest.exists():
        manifest.write_text("")

    sha = result.get("id", "")[:8]
    print(f"[INFO] Committed {len(actions)} file(s) to '{DATA_BRANCH}' branch ({sha}) "
          f"— {created} new, {updated} updated")


if __name__ == "__main__":
    main()
