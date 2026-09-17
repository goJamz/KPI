"""
Tag-value naming: folding, aliasing, and shared-project identification.

Shared by every script that has to decide whether two tag values are the same
thing. It used to be copied into three of them, which is how they drifted —
`is_shared_project` was left comparing on a lowercased string while everything
else had moved to the folded key, so a punctuation variant silently stopped
counting as shared.

Kept free of any knowledge of report or snapshot structure, so a new KPI
collector can use it without inheriting the cost pipeline's data shapes.
"""

import re


PORTFOLIO_ALIASES: dict[str, str] = {
    "i&p":                          "Infrastructure and Platforms",
    "i & p":                        "Infrastructure and Platforms",
    "i and p":                      "Infrastructure and Platforms",
    "infrastructure & platforms":   "Infrastructure and Platforms",
    "infrastructure and platforms": "Infrastructure and Platforms",
    "infastructure & platforms":    "Infrastructure and Platforms",
    "ifrastructure & platforms":    "Infrastructure and Platforms",
}

# Maps known lowercase project tag variants → canonical display name.
# Add entries whenever a project appears under multiple tag spellings in Azure.
PROJECT_ALIASES: dict[str, str] = {
    "talon":                                          "Talon",
    "gmap":                                           "GMAP",
    "ground maintenance analytics platform":          "GMAP",
    "pangea":                                         "PANGEA",
    "artificial intelligence development environment": "AIDE",
}


def fold_name(name: str) -> str:
    """Collapse formatting-only differences to one comparison key.

    Case, surrounding and repeated whitespace, punctuation, and `&` versus
    `and` are all spellings of the same tag value, not different values. Folding
    them here means that entire class needs no per-name configuration and no
    maintenance: `Infrastructure & Platforms`, `infrastructure and platforms`
    and `INFRASTRUCTURE  AND  PLATFORMS` share a key automatically.

    Genuine misspellings do not fold together — `ifrastructure and platforms`
    keeps its own key — and still need an alias entry to be merged.
    """
    if not name:
        return ""
    key = name.strip().lower().replace("&", " and ")
    key = re.sub(r"[^a-z0-9]+", " ", key)
    return " ".join(key.split())


# Alias lookups go through the folded key, so one entry covers every
# punctuation and casing variant of the same misspelling. Exported because
# callers resolving display names need to consult them directly.
PORTFOLIO_ALIAS_BY_KEY = {fold_name(k): v for k, v in PORTFOLIO_ALIASES.items()}
PROJECT_ALIAS_BY_KEY   = {fold_name(k): v for k, v in PROJECT_ALIASES.items()}


def normalize_portfolio(name: str) -> str:
    """Return the canonical portfolio name for a raw tag value, or the original if unknown."""
    if not name:
        return name
    return PORTFOLIO_ALIAS_BY_KEY.get(fold_name(name), name)


def normalize_project(name: str) -> str:
    """Return the canonical project name for a raw tag value, or the original if unknown."""
    if not name:
        return name
    return PROJECT_ALIAS_BY_KEY.get(fold_name(name), name)


def pick_display_name(variants: dict) -> str:
    """Choose one spelling to display for a group of folded-equal raw values.

    `variants` maps each raw spelling to how many times it was seen. An
    all-lowercase spelling usually means the Resource Graph casing lookup missed
    that value, so a spelling carrying capitalization is preferred; then the
    most frequently seen; then alphabetical, purely so the result is stable.
    """
    return sorted(
        variants.items(),
        key=lambda kv: (not any(c.isupper() for c in kv[0]), -kv[1], kv[0]),
    )[0][0]


# Projects whose costs are shared infrastructure and should be redistributed
# across all portfolios weighted by each portfolio's non-shared project count.
SHARED_PROJECTS: frozenset[str] = frozenset({"expedition 0", "gitlabrunners"})


# Compared on the folded key, like every other tag value. Matching on
# `.lower().strip()` alone meant a punctuation variant such as `Expedition-0`
# stopped counting as shared, which silently moves that spend out of the shared
# pool and into one portfolio's direct costs — and shifts the shared allocation
# for every other portfolio at the same time.
_SHARED_PROJECT_KEYS = frozenset(fold_name(n) for n in SHARED_PROJECTS)


def is_shared_project(name: str) -> bool:
    return bool(name) and fold_name(name) in _SHARED_PROJECT_KEYS
