"""Render the standalone approved-image status page.

This stays separate from the established cost dashboard generator: it reads the
image collector artifact and writes only ``public/image-status.html``. The
existing dashboard and cluster render paths do not depend on this module.
"""

from __future__ import annotations

import json
import os
import sys
from html import escape
from pathlib import Path

from image_status_constants import (
    DEFAULT_REPORTS_DIR,
    PAGE_FILENAME,
    REPORT_FILENAME,
    STATUS_AHEAD,
    STATUS_CURRENT,
    STATUS_OUTDATED,
    STATUS_UNKNOWN,
)


COSTS_DIR = Path(__file__).resolve().parent.parent / "costs"
sys.path.insert(0, str(COSTS_DIR))
from generate_report import CSS  # noqa: E402


IMAGE_STATUS_CSS = """
.image-status-panel { max-width: 70rem; margin: 0 auto; }
.image-status-summary { margin-bottom: 1rem; color: var(--ink-muted); }
.image-status-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(18rem, 1fr));
  gap: 1rem;
}
.image-status-card {
  overflow: hidden;
  background: var(--paper);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.image-status-banner {
  padding: .55rem 1rem;
  color: var(--on-dark);
  font-size: .78rem;
  font-weight: 700;
  letter-spacing: .04em;
  text-transform: uppercase;
}
.image-status-card.status-current .image-status-banner { background: var(--good-strong); }
.image-status-card.status-outdated .image-status-banner { background: var(--bad); }
.image-status-card.status-ahead .image-status-banner { background: var(--accent); }
.image-status-card.status-unknown .image-status-banner { background: var(--warn); }
.image-status-body { padding: 1rem; }
.image-status-body h2 { margin: 0 0 .8rem; padding: 0; border: 0; }
.image-version-row {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  padding: .35rem 0;
  border-bottom: 1px solid var(--border-soft);
}
.image-version-row strong { color: var(--ink-strong); }
.image-status-links { margin-top: .9rem; font-size: .82rem; }
.image-status-links a { color: var(--accent); }
.image-status-error { margin-top: .75rem; color: var(--bad); font-size: .82rem; }
.image-status-empty {
  padding: 2rem;
  text-align: center;
  background: var(--paper);
  border: 1px solid var(--border);
  border-radius: 8px;
}
"""


def load_report(base_dir: Path) -> dict | None:
    report_path = base_dir / DEFAULT_REPORTS_DIR / REPORT_FILENAME
    if not report_path.is_file():
        print("[INFO] No image status report this run — page will show not collected")
        return None

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] Could not read {report_path}: {exc}", file=sys.stderr)
        return None

    print(f"[INFO] Loaded image status report from {report_path.name}")
    return report


def version_row(label: str, value: str) -> str:
    """Render one consistently escaped image-version row."""
    return (
        '<div class="image-version-row">'
        f'<span>{escape(label)}</span><strong>{escape(value)}</strong>'
        '</div>'
    )


def render_page(report: dict | None, cluster_available: bool) -> str:
    cluster_link = (
        '<a href="cluster.html">Cluster Utilization</a>'
        if cluster_available else ""
    )
    generated = str((report or {}).get("collected_utc") or "Not collected")
    items = (report or {}).get("items") or []
    status_labels = {
        STATUS_CURRENT: "Current",
        STATUS_OUTDATED: "Update available",
        STATUS_AHEAD: "Ahead of upstream",
        STATUS_UNKNOWN: "Check unavailable",
    }

    cards = []
    for item in items:
        status = str(item.get("status") or STATUS_UNKNOWN)
        if status not in status_labels:
            status = STATUS_UNKNOWN
        name = escape(str(item.get("name") or "Unnamed image"))
        registry_tag = str(item.get("registry_tag") or "")
        current = str(item.get("current") or "Unavailable")
        latest = str(item.get("latest") or "Unavailable")
        registry_tag_row = (
            version_row("Approved image tag", registry_tag)
            if registry_tag and registry_tag != current
            else ""
        )

        links = []
        for label, key in (
            ("Registry", "registry_url"),
            ("Source", "source_url"),
            ("Upstream release", "upstream_url"),
        ):
            url = str(item.get(key) or "").strip()
            if url:
                links.append(
                    f'<a href="{escape(url, quote=True)}">{label}</a>'
                )
        links_html = (
            f'<p class="image-status-links">{" · ".join(links)}</p>'
            if links else ""
        )

        errors = item.get("errors") or []
        error_html = (
            '<p class="image-status-error">'
            + escape("; ".join(str(error) for error in errors))
            + "</p>"
            if errors else ""
        )

        cards.append(
            f'<article class="image-status-card status-{status}">'
            f'<div class="image-status-banner">{status_labels[status]}</div>'
            '<div class="image-status-body">'
            f'<h2>{name}</h2>'
            f'{version_row("Current", current)}'
            f'{registry_tag_row}'
            f'{version_row("Latest available", latest)}'
            f'{links_html}{error_html}'
            '</div></article>'
        )

    if cards:
        outdated = sum(
            1 for item in items if item.get("status") == STATUS_OUTDATED
        )
        content = (
            f'<p class="image-status-summary">{len(items)} image(s) checked; '
            f'{outdated} update(s) available.</p>'
            f'<div class="image-status-list">{"".join(cards)}</div>'
        )
    else:
        content = (
            '<div class="image-status-empty">Image status was not collected '
            'this run.</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Expedition-0 KPI Report - Image Status</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#128202;</text></svg>">
  <style>{CSS}{IMAGE_STATUS_CSS}</style>
</head>
<body>
  <header>
    <h1>Image Status</h1>
    <nav class="kpi-page-nav" aria-label="KPI pages">
      <a href="index.html">KPI Overview</a>
      {cluster_link}
      <span aria-current="page">Image Status</span>
    </nav>
    <p class="generated">Generated: {escape(generated)}</p>
  </header>
  <main class="image-status-panel">
    {content}
  </main>
</body>
</html>"""


def main() -> None:
    repo_root = Path(os.environ.get("CI_PROJECT_DIR", "."))
    public_dir = repo_root / "public"
    public_dir.mkdir(parents=True, exist_ok=True)

    report = load_report(repo_root)
    output = public_dir / PAGE_FILENAME
    output.write_text(
        render_page(report, cluster_available=(public_dir / "cluster.html").is_file()),
        encoding="utf-8",
    )
    print(f"[INFO] Image status page written to {output}")


if __name__ == "__main__":
    main()
