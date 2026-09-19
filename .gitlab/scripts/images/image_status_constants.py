"""Shared constants for the approved-image status feature.

Keep image-specific configuration in ``image_sources.json``. This module is
only for values shared by the collector and renderer, plus operational policy
that should have one definition.
"""

from __future__ import annotations


# Files and directories.
CONFIG_ENV_VAR = "IMAGE_STATUS_CONFIG"
DEFAULT_CONFIG_FILENAME = "image_sources.json"
REPORTS_DIR_ENV_VAR = "IMAGE_STATUS_REPORTS_DIR"
DEFAULT_REPORTS_DIR = "image_status_reports"
REPORT_FILENAME = "status.json"
PAGE_FILENAME = "image-status.html"

# HTTP and container execution policy.
HTTP_USER_AGENT = "ai2c-kpi-image-status"
CONTAINER_TIMEOUT_SECONDS = 300

# Upstream release authorities.
GITHUB_API_VERSION = "2022-11-28"
DOTNET_RELEASE_INDEX_URL = (
    "https://dotnetcli.blob.core.windows.net/dotnet/"
    "release-metadata/releases-index.json"
)
DOTNET_DOWNLOAD_BASE_URL = "https://dotnet.microsoft.com/en-us/download/dotnet"
NVIDIA_CUDA_ARCHIVE_URL = "https://developer.nvidia.com/cuda-toolkit-archive"

# Per-image comparison states shared by collection and rendering.
STATUS_CURRENT = "current"
STATUS_OUTDATED = "outdated"
STATUS_AHEAD = "ahead"
STATUS_UNKNOWN = "unknown"
