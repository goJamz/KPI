"""
Writing history shards, and tracking which ones still need committing.

Shared by every KPI collector. The manifest is what keeps commits small: without
it, a checkpoint partway through a run would re-send the whole history rather
than the handful of weeks the run has actually added.
"""

import json
from pathlib import Path


MANIFEST_NAME = ".manifest"

# Money is carried to the cent and percentages to a tenth, matching what the
# collector already does to the figures Azure returns.
_MONEY_KEYS = frozenset({
    "cost", "prior_cost", "current_cost", "total_cost", "total_prior_cost",
    "forecast_cost", "change", "actual", "forecast", "unclassified",
})
_PCT_KEYS = frozenset({"change_pct"})


def round_costs(value, _key: str | None = None):
    """Round money and percentage fields to the precision they actually carry.

    Individual costs arrive from the collector already rounded, but anything
    derived from them is not: summing a week's portfolios, or recomputing a
    change after merging two entries that alias to the same name, reintroduces
    binary float error. That lands in the stored history as
    `1644.3700000000001` and surfaces anywhere the raw value is shown, such as
    the per-environment JSON published beside the page.

    Applied on the way in, so the same rule covers both the snapshot being
    written and the shards already on the branch.
    """
    if isinstance(value, dict):
        return {k: round_costs(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [round_costs(v, _key) for v in value]
    if isinstance(value, float):
        if _key in _PCT_KEYS:
            return round(value, 1)
        if _key in _MONEY_KEYS:
            return round(value, 2)
    return value


def write_if_changed(path: Path, payload: dict, ignore_keys: tuple = ()) -> bool:
    """Write payload as JSON only when it differs, reporting whether it did.

    Collectors checkpoint partway through a run, so this is called repeatedly
    over the same source data. Rewriting files that have not changed would make
    every checkpoint re-commit the whole history.

    ignore_keys excludes fields that differ on every call for reasons that are
    not a change in the data — a capture timestamp being the usual one.
    """
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            existing = None
        if isinstance(existing, dict):
            a = {k: v for k, v in existing.items() if k not in ignore_keys}
            b = {k: v for k, v in payload.items() if k not in ignore_keys}
            if a == b:
                return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
    return True


def update_manifest(history_dir: Path, changed: list) -> tuple:
    """Add `changed` to the pending-commit list, returning (carried, total).

    Entries accumulate and are cleared only once a commit succeeds. A file is
    only listed on the run that changes it, so rewriting the manifest from
    scratch would mean a batch whose commit failed is unchanged on disk next
    time round, drops out of the list, and never reaches the branch.
    """
    manifest = history_dir / MANIFEST_NAME
    pending = set()
    if manifest.exists():
        pending = {line.strip() for line in manifest.read_text().splitlines() if line.strip()}
    carried = len(pending)

    pending.update(
        p.relative_to(history_dir).as_posix() if p.is_absolute() or history_dir in p.parents
        else Path(p).as_posix()
        for p in changed
    )
    history_dir.mkdir(parents=True, exist_ok=True)
    manifest.write_text("\n".join(sorted(pending)))
    return carried, len(pending)
