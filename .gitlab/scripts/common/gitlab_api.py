"""Shared GitLab API configuration used by KPI collectors."""

from __future__ import annotations

import os


GITLAB_PAGE_SIZE = 100
API_MAX_ATTEMPTS = 4
API_TIMEOUT_SECONDS = 60
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


def gitlab_token() -> str:
    """Return the configured GitLab token using the established precedence."""
    return os.environ.get("GITLAB_TOKEN", "").strip() or os.environ.get(
        "AI2C_API_RWA",
        "",
    ).strip()


def gitlab_api_url(*, allow_base_url: bool = False) -> str:
    """Return the normalized API v4 URL from the supported CI variables."""
    api_url = (
        os.environ.get("GITLAB_API_V4_URL", "").strip()
        or os.environ.get("CI_API_V4_URL", "").strip()
    )
    if not api_url and allow_base_url:
        base_url = os.environ.get("GITLAB_URL", "").strip().rstrip("/")
        api_url = f"{base_url}/api/v4" if base_url else ""
    return api_url.rstrip("/")
