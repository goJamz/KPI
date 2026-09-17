#!/usr/bin/env bash
# =============================================================================
# Azure Weekly Cost Analysis Script
#
# The Azure Government Cost Management API returns tag groupings as generic
# TagKey/TagValue columns rather than named columns per tag key. To get the
# portfolio → project hierarchy we therefore use a two-step query strategy:
#
#   Step 1: Group by 'portfolio' TagKey → all portfolios + their total costs
#   Step 2: For each portfolio, group by 'project' TagKey filtered to that
#           portfolio → project costs nested under the correct portfolio
#
# This runs for both the current and prior 7-day periods, then a Python
# assembly script merges them and computes week-over-week deltas.
#
# Cost only. Idle and orphaned resources are gathered by
# .gitlab/scripts/resources/scan_resources.py, which runs as its own job — they
# are point-in-time facts with no relationship to a reporting window, and used
# to be re-scanned once per window by the backfill.
#
# Required CI/CD variables (environment-scoped via $ENVIRONMENT):
#   RUNNER_CLIENT_ID      - Service principal client ID
#   RUNNER_CLIENT_SECRET  - Service principal client secret
#   AZURE_TENANT_ID       - Azure tenant ID
#   AZURE_SUBSCRIPTION_ID - Azure subscription ID
#
# Required pipeline variables:
#   ENVIRONMENT  — dev | test | prod
# =============================================================================

set -euo pipefail

ENVIRONMENT_LABEL="${ENVIRONMENT}"
echo "[INFO] Running cost analysis for environment: ${ENVIRONMENT_LABEL}"

# -----------------------------------------------------------------------------
# 1. Date ranges — Python for portability across GNU/busybox/Alpine
#    Reporting windows are whole ISO weeks (Monday-Sunday), so a week produced
#    by the weekly run and the same week produced by the backfill are identical.
#    Set OVERRIDE_CURRENT_START/END and OVERRIDE_PRIOR_START/END to target a
#    specific week (used by the backfill job). Otherwise the most recently
#    completed week is used.
# -----------------------------------------------------------------------------
if [[ -n "${OVERRIDE_CURRENT_START:-}" && -n "${OVERRIDE_CURRENT_END:-}" ]]; then
  CURRENT_START="${OVERRIDE_CURRENT_START}"
  CURRENT_END="${OVERRIDE_CURRENT_END}"
  PRIOR_START="${OVERRIDE_PRIOR_START}"
  PRIOR_END="${OVERRIDE_PRIOR_END}"
  echo "[INFO] Backfill mode — using overridden date ranges"
else
  read -r CURRENT_END CURRENT_START PRIOR_END PRIOR_START <<< "$(python3 -c "
from datetime import datetime, timedelta, timezone
today = datetime.now(timezone.utc).date()
# The most recently completed Monday-Sunday week. Anchoring to the calendar
# week rather than to yesterday means the window is the same no matter which
# day the pipeline runs, so a scheduled Monday run and a manual mid-week run
# produce the same week rather than two overlapping ones that both claim it.
current_end   = today - timedelta(days=today.weekday() + 1)
current_start = current_end - timedelta(days=6)
prior_end     = current_start - timedelta(days=1)
prior_start   = prior_end - timedelta(days=6)
print(current_end, current_start, prior_end, prior_start)
")"
fi

echo "[INFO] Current period : ${CURRENT_START} → ${CURRENT_END}"
echo "[INFO] Prior period   : ${PRIOR_START} → ${PRIOR_END}"

# Forecast window — always relative to today, unaffected by backfill overrides
read -r FORECAST_START FORECAST_END <<< "$(python3 -c "
from datetime import datetime, timedelta, timezone
today = datetime.now(timezone.utc).date()
print(today, today + timedelta(days=30))
")"
echo "[INFO] Forecast period : ${FORECAST_START} → ${FORECAST_END}"

# -----------------------------------------------------------------------------
# Scratch space, scoped to this job.
#
# These runners keep /tmp between jobs, so a bare /tmp path is shared with every
# earlier pipeline that ran on the same machine. That is fine for state meant to
# live for one window, and wrong for anything meant to live for one job: a
# cached API response from a previous run would be served to this one, and
# BACKFILL_FORCE would silently return old data instead of re-querying — the
# exact opposite of what forcing is for.
#
# CI_JOB_ID is unique per job, so everything below is isolated by construction.
# Falling back to the PID keeps the script usable outside CI.
# -----------------------------------------------------------------------------
SCRATCH="/tmp/kpi-${CI_JOB_ID:-$$}"
export SCRATCH   # the assembly step reads it from the environment
mkdir -p "${SCRATCH}"

# Best effort tidy-up of scratch left by jobs that are long gone, so a
# long-lived runner does not slowly fill /tmp.
find /tmp -maxdepth 1 -name 'kpi-*' -type d -mtime +1 -exec rm -rf {} + 2>/dev/null || true

# Cleared per window. The response cache and the tag map are deliberately not:
# both are valid for the whole job, and both are inside SCRATCH so neither
# outlives it.
rm -rf "${SCRATCH}/projects" "${SCRATCH}/portfolio_current.json" \
       "${SCRATCH}/portfolio_prior.json" "${SCRATCH}/forecast_portfolio.json"
mkdir -p "${SCRATCH}/projects"

# -----------------------------------------------------------------------------
# Response cache — the largest saving available in a backfill.
#
# Windows step backwards a week at a time, so window N's prior period is exactly
# window N+1's current period: same dates, same query, same response. Caching on
# a hash of the request body catches that automatically, and catches the same
# portfolio being queried for the same week from any other direction too.
#
# Keyed by body rather than by dates so it cannot go stale against a query whose
# shape changes. Lives in /tmp, which is per-job, so nothing leaks between runs.
# -----------------------------------------------------------------------------
CACHE_DIR="${SCRATCH}/cost_api_cache"
mkdir -p "${CACHE_DIR}"

if command -v sha256sum > /dev/null 2>&1; then
  CACHE_ENABLED=1
else
  CACHE_ENABLED=0
  echo "[WARN] sha256sum unavailable — response caching disabled"
fi

# Counters survive the per-window reset so the job can report totals at the end.
CACHE_STATS="${SCRATCH}/cost_api_cache_stats"
[[ -f "${CACHE_STATS}" ]] || printf '0 0\n' > "${CACHE_STATS}"

# -----------------------------------------------------------------------------
# Adaptive inter-call delay.
#
# A fixed 5s pause before every call was roughly half the runtime of a backfill
# — about 28 minutes of dev's 98 — while still not preventing throttling. The
# delay now starts small and moves with the API: up when it pushes back, and
# back down after a run of clean calls.
# -----------------------------------------------------------------------------
DELAY_FILE="${SCRATCH}/cost_api_delay"
STREAK_FILE="${SCRATCH}/cost_api_streak"
API_MIN_DELAY="${COST_API_MIN_DELAY:-1}"
# Lowered from 10s on the evidence of three real runs: the throttle rate was 15%
# at a fixed 5s, 14% at 10s and 22% at 11s. Spacing calls further apart did not
# buy less throttling, so most of a large delay is cost with no return — better
# to keep calls close together and let the retry absorb the 429s.
API_MAX_DELAY="${COST_API_MAX_DELAY:-4}"
# Successes needed before easing the delay back down.
API_DECAY_AFTER="${COST_API_DECAY_AFTER:-5}"
[[ -f "${DELAY_FILE}" ]]  || printf '%s' "${API_MIN_DELAY}" > "${DELAY_FILE}"
[[ -f "${STREAK_FILE}" ]] || printf '0' > "${STREAK_FILE}"

# -----------------------------------------------------------------------------
# 2. Authenticate with Azure
# -----------------------------------------------------------------------------
# Shared with the other collectors so authentication cannot drift between them.
source "$(dirname "${BASH_SOURCE[0]}")/../common/azure_login.sh"
COST_API_URL="${MANAGEMENT_ENDPOINT}/subscriptions/${AZURE_SUBSCRIPTION_ID}/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
FORECAST_API_URL="${MANAGEMENT_ENDPOINT}/subscriptions/${AZURE_SUBSCRIPTION_ID}/providers/Microsoft.CostManagement/forecast?api-version=2025-03-01"
echo "[INFO] Cost Management endpoint: ${MANAGEMENT_ENDPOINT}"

# -----------------------------------------------------------------------------
# 3. API helpers
# -----------------------------------------------------------------------------

# Abandon this reporting window with a clear message. The backfill loop treats a
# non-zero exit as "this window did not complete" and moves on to the next one,
# so nothing already written for earlier windows is affected.
fail_window() {
  echo "[ERROR] $1"
  echo "[ERROR] Window ${CURRENT_START} → ${CURRENT_END} did not complete — no report written."
  exit 1
}

# Wrapper that calls the Cost Management API with retry + exponential backoff.
# az rest exits non-zero on HTTP errors (including 429) and writes the JSON
# response body to stdout — we capture that to detect rate-limit responses
# and retry rather than aborting the script.
#
# Returns non-zero on an unrecoverable error rather than exiting, so the caller
# can decide whether to abandon just this reporting window or the whole run.
#
# A base 5-second pause before every call keeps us well under the limit
# during the per-portfolio loop. On a 429, backoff starts at 30s and doubles.
call_cost_api() {
  local body="$1"
  local output_file="$2"
  local label="$3"
  local url="${4:-${COST_API_URL}}"
  local max_retries=5
  # Starting point for the retry wait, doubling each attempt.
  #
  # Was a flat 30s. Across three environments 23 of 25 throttles cleared on the
  # first retry, so 30s is enough — but nothing showed it was needed, and at
  # ~20% of calls throttling this was the single largest remaining cost
  # (4m30s of dev's 13m). Starting lower and letting it double finds the real
  # figure instead of assuming it: 10/20/40/80/160 rather than 30/60/120/240/480.
  local backoff="${COST_API_BACKOFF:-10}"
  local attempt=1

  local cache_file=""
  if [[ ${CACHE_ENABLED} -eq 1 ]]; then
    cache_file="${CACHE_DIR}/$(printf '%s|%s' "${url}" "${body}" | sha256sum | cut -d' ' -f1).json"
    if [[ -s "${cache_file}" ]]; then
      cp "${cache_file}" "${output_file}"
      read -r hits misses < "${CACHE_STATS}" || true
      printf '%s %s\n' "$((hits + 1))" "${misses}" > "${CACHE_STATS}"
      echo "[INFO] Reusing an earlier response for '${label}' — no API call"
      return 0
    fi
  fi

  sleep "$(cat "${DELAY_FILE}")"

  while [[ $attempt -le $((max_retries + 1)) ]]; do
    local az_exit=0

    # || az_exit=$? captures the exit code without triggering set -e.
    # az rest writes the HTTP response body (JSON) to stdout even on failure,
    # so output_file will contain the parseable error payload.
    az rest \
      --method POST \
      --url "${url}" \
      --resource "${MANAGEMENT_ENDPOINT}/" \
      --headers "Content-Type=application/json" "X-Ms-Command-Name=CostAnalysis" "ClientType=COST-Tracker" \
      --body "${body}" \
      > "${output_file}" 2>/dev/null || az_exit=$?

    if [[ $az_exit -ne 0 ]] || jq -e '.error.code == "429"' "${output_file}" > /dev/null 2>&1; then
      if [[ $attempt -le $max_retries ]]; then
        # Back off between calls as well as before the retry — the retry alone
        # only fixes this call, and the next one is about to hit the same limit.
        # Clamp rather than "raise only while below the ceiling" — the latter
        # steps straight past it, which is how a 10s maximum produced 11s.
        local delay
        delay=$(( $(cat "${DELAY_FILE}") + 2 ))
        [[ ${delay} -gt ${API_MAX_DELAY} ]] && delay=${API_MAX_DELAY}
        printf '%s' "${delay}" > "${DELAY_FILE}"
        printf '0' > "${STREAK_FILE}"
        echo "[WARN] Rate limited on '${label}' (attempt ${attempt}/${max_retries}). Waiting ${backoff}s..."
        sleep "${backoff}"
        backoff=$((backoff * 2))
        attempt=$((attempt + 1))
        continue
      else
        echo "[ERROR] Rate limit retries exhausted for '${label}'"
        return 1
      fi
    fi

    if jq -e '.error' "${output_file}" > /dev/null 2>&1; then
      echo "[ERROR] API error for '${label}':"
      jq '.error' "${output_file}"
      return 1
    fi

    if ! jq -e '.properties.columns' "${output_file}" > /dev/null 2>&1; then
      echo "[ERROR] Unexpected response structure for '${label}' — raw response:"
      cat "${output_file}"
      return 1
    fi

    [[ -n "${cache_file}" ]] && cp "${output_file}" "${cache_file}"
    read -r hits misses < "${CACHE_STATS}" || true
    printf '%s %s\n' "${hits}" "$((misses + 1))" > "${CACHE_STATS}"

    # Ease the delay back down after a clean run, so one burst of throttling
    # does not slow every remaining call for the rest of the job.
    local streak delay
    streak=$(( $(cat "${STREAK_FILE}") + 1 ))
    if [[ ${streak} -ge ${API_DECAY_AFTER} ]]; then
      delay=$(cat "${DELAY_FILE}")
      [[ ${delay} -gt ${API_MIN_DELAY} ]] && printf '%s' "$((delay - 1))" > "${DELAY_FILE}"
      printf '0' > "${STREAK_FILE}"
    else
      printf '%s' "${streak}" > "${STREAK_FILE}"
    fi

    return 0
  done
}

# Builds a query that groups by 'portfolio' TagKey only.
# Uses jq -n so that date strings are safely embedded as JSON values.
build_portfolio_query() {
  local start="$1" end="$2"
  jq -n --arg s "${start}" --arg e "${end}" '{
    type: "ActualCost",
    timeframe: "Custom",
    timePeriod: { from: ($s + "T00:00:00+00:00"), to: ($e + "T23:59:59+00:00") },
    dataset: {
      granularity: "None",
      aggregation: { totalCost: { name: "Cost", function: "Sum" } },
      grouping: [{ type: "TagKey", name: "portfolio" }]
    }
  }'
}

# Builds a query that groups by 'project' TagKey, filtered to one portfolio.
# Using jq -n ensures the portfolio name is properly JSON-escaped.
build_project_query() {
  local portfolio="$1" start="$2" end="$3"
  jq -n --arg p "${portfolio}" --arg s "${start}" --arg e "${end}" '{
    type: "ActualCost",
    timeframe: "Custom",
    timePeriod: { from: ($s + "T00:00:00+00:00"), to: ($e + "T23:59:59+00:00") },
    dataset: {
      granularity: "None",
      aggregation: { totalCost: { name: "Cost", function: "Sum" } },
      grouping: [{ type: "TagKey", name: "project" }],
      filter: {
        tags: { name: "portfolio", operator: "In", values: [$p] }
      }
    }
  }'
}

# Builds a 30-day forecast query grouped by 'portfolio' TagKey.
# Uses the /forecast endpoint (FORECAST_API_URL), not /query.
# granularity:None returns one total row per portfolio for the full window.
build_forecast_query() {
  local start="$1" end="$2"
  jq -n --arg s "${start}" --arg e "${end}" '{
    type: "Usage",
    timeframe: "Custom",
    timePeriod: { from: ($s + "T00:00:00+00:00"), to: ($e + "T23:59:59+00:00") },
    dataset: {
      granularity: "Daily",
      aggregation: { totalCost: { name: "Cost", function: "Sum" } },
      grouping: [{ type: "TagKey", name: "portfolio" }]
    },
    includeActualCost: true
  }'
}

# -----------------------------------------------------------------------------
# 4. Fetch proper-case tag values via Azure Resource Graph
#
# The Cost Management API returns tag values in lowercase. We query Resource
# Graph once to get the original-case values (e.g. "Infrastructure and
# Platforms", "AIDE", "HQDA ASAALT") directly from resource metadata.
# The Tags API is not supported at subscription scope in Azure Government.
# -----------------------------------------------------------------------------
# Point-in-time and identical for every window, so it is fetched once per job
# rather than once per window — 38 identical queries per environment before.
if [[ -s "${SCRATCH}/azure_tags.json" ]]; then
  echo "[INFO] Reusing tag value casing fetched earlier in this job"
else
echo "[INFO] Fetching tag value casing from Resource Graph..."
az rest \
  --method POST \
  --url "${MANAGEMENT_ENDPOINT}/providers/Microsoft.ResourceGraph/resources?api-version=2021-03-01" \
  --resource "${MANAGEMENT_ENDPOINT}/" \
  --headers "Content-Type=application/json" \
  --body "$(jq -n \
      --arg sub "${AZURE_SUBSCRIPTION_ID}" \
      --arg q 'Resources | project portfolio=tostring(tags["portfolio"]), project_tag=tostring(tags["project"]) | where isnotempty(portfolio) or isnotempty(project_tag) | distinct portfolio, project_tag' \
      '{"subscriptions": [$sub], "query": $q}')" \
  > "${SCRATCH}/azure_tags.json" || fail_window "Resource Graph tag query failed"
fi

# Keep the response. It records which tag values existed on live resources at
# this moment, which is the only way to tell a spelling that has since been
# corrected in Azure from one that is still wrong: Cost Management never
# retags historical usage, so an old spelling stays in the cost data forever
# either way. Resource Graph has no history of its own, so an observation not
# saved now cannot be recovered later.
#
# The subdirectory keeps it clear of cost_reports/*.json, which is globbed
# non-recursively for cost reports.
mkdir -p cost_reports/tag_inventory
cp "${SCRATCH}/azure_tags.json" "cost_reports/tag_inventory/${ENVIRONMENT_LABEL}.json"

# -----------------------------------------------------------------------------
# 5. Query portfolio totals for both periods
# -----------------------------------------------------------------------------
echo "[INFO] Querying portfolio totals — current period..."
call_cost_api "$(build_portfolio_query "${CURRENT_START}" "${CURRENT_END}")" \
  "${SCRATCH}/portfolio_current.json" "portfolio totals (current)" \
  || fail_window "Portfolio totals query failed for the current period"

echo "[INFO] Querying portfolio totals — prior period..."
call_cost_api "$(build_portfolio_query "${PRIOR_START}" "${PRIOR_END}")" \
  "${SCRATCH}/portfolio_prior.json" "portfolio totals (prior)" \
  || fail_window "Portfolio totals query failed for the prior period"

# Extract all unique portfolio names across both periods.
# Columns from a TagKey groupBy are: [Cost, TagKey, TagValue, Currency]
# TagValue (index 2) contains the portfolio name.
# Filtering happens inside jq rather than through `grep -v`, because grep exits
# non-zero when it matches nothing — which under `set -o pipefail` aborted the
# run for any window that legitimately has no cost data (e.g. dates before the
# subscription existed). `.rows[]?` also tolerates a missing or null rows array,
# and `.[2] // empty` drops rows whose TagValue is JSON null, which the report
# assembly discards anyway and which would otherwise cost two wasted API calls.
PORTFOLIOS=$(jq -r '(.properties.rows // [])[] | .[2] // empty | select(. != "")' \
  "${SCRATCH}/portfolio_current.json" "${SCRATCH}/portfolio_prior.json" | sort -u)

PORTFOLIO_COUNT=0
if [[ -n "${PORTFOLIOS}" ]]; then
  PORTFOLIO_COUNT=$(printf '%s\n' "${PORTFOLIOS}" | wc -l | tr -d ' ')
fi
echo "[INFO] Portfolios found (${PORTFOLIO_COUNT}): $(printf '%s' "${PORTFOLIOS}" | tr '\n' ' ')"

# -----------------------------------------------------------------------------
# 6. Query project breakdown per portfolio, for both periods
#
# A here-string always emits a trailing newline, so feeding an empty portfolio
# list to the loop would still run one iteration with an empty portfolio name
# and build a malformed query. The guard skips the loop instead.
# -----------------------------------------------------------------------------
mkdir -p "${SCRATCH}/projects"

if [[ -z "${PORTFOLIOS}" ]]; then
  echo "[INFO] No portfolio tag values in this window — skipping project breakdown."
else
  # An index rather than a name derived from the portfolio itself.
  #
  # Filenames used to be built by substituting characters out of the portfolio
  # name, and the assembly step rebuilt the same string to read them back. That
  # only worked while both sides transformed the name identically — the Python
  # side also lowercased, which happened to match because the case-correction
  # map only ever changes case. Any divergence, or a portfolio containing a
  # character neither side accounted for, would silently find no file and drop
  # every project under that portfolio with no error.
  #
  # A counter cannot collide, cannot be mangled, and needs no agreement about
  # character classes. The mapping is written down instead of recomputed.
  PORTFOLIO_INDEX="${SCRATCH}/projects/index.tsv"
  : > "${PORTFOLIO_INDEX}"
  portfolio_n=0

  while IFS= read -r portfolio; do
    portfolio_n=$((portfolio_n + 1))
    safe_name="p${portfolio_n}"
    printf '%s\t%s\n' "${safe_name}" "${portfolio}" >> "${PORTFOLIO_INDEX}"

    echo "[INFO] Querying projects for '${portfolio}' — current period..."
    call_cost_api \
      "$(build_project_query "${portfolio}" "${CURRENT_START}" "${CURRENT_END}")" \
      "${SCRATCH}/projects/${safe_name}_current.json" \
      "projects in '${portfolio}' (current)" \
      || fail_window "Project query failed for portfolio '${portfolio}' (current period)"

    echo "[INFO] Querying projects for '${portfolio}' — prior period..."
    call_cost_api \
      "$(build_project_query "${portfolio}" "${PRIOR_START}" "${PRIOR_END}")" \
      "${SCRATCH}/projects/${safe_name}_prior.json" \
      "projects in '${portfolio}' (prior)" \
      || fail_window "Project query failed for portfolio '${portfolio}' (prior period)"
  done <<< "${PORTFOLIOS}"
fi

# -----------------------------------------------------------------------------
# 7. Query 30-day portfolio forecast (skipped in backfill mode)
#
# Forecast data is not persisted to history — it is always queried fresh and
# stored only in the current run's cost_reports artifact.
# -----------------------------------------------------------------------------
if [[ -z "${OVERRIDE_CURRENT_START:-}" ]]; then
  echo "[INFO] Querying 30-day forecast by portfolio..."
  (
    call_cost_api \
      "$(build_forecast_query "${FORECAST_START}" "${FORECAST_END}")" \
      "${SCRATCH}/forecast_portfolio.json" \
      "forecast (portfolio totals)" \
      "${FORECAST_API_URL}"
  ) || {
    echo "[WARN] Forecast query failed after retries — continuing without forecast data"
    printf '{"properties":{"columns":[],"rows":[]}}' > "${SCRATCH}/forecast_portfolio.json"
  }
else
  echo "[INFO] Backfill mode — skipping forecast query"
  printf '{"properties":{"columns":[],"rows":[]}}' > "${SCRATCH}/forecast_portfolio.json"
fi

# Log what the forecast endpoint actually returned so pipeline output shows
# whether per-portfolio grouping was honored or just a subscription total.
echo "[INFO] Forecast response columns: $(jq -r '[.properties.columns[].name] | join(", ")' "${SCRATCH}/forecast_portfolio.json" 2>/dev/null || echo 'n/a')"
echo "[INFO] Forecast response rows: $(jq '.properties.rows | length' "${SCRATCH}/forecast_portfolio.json" 2>/dev/null || echo '?')"

# -----------------------------------------------------------------------------
# 8. Assemble hierarchical JSON and compute comparison — Python
#
# The API returns [Cost, TagKey, TagValue, Currency] rows. Python reads all
# the response files, builds the portfolio → project hierarchy for each
# period, computes week-over-week deltas, and writes the final report JSON.
# -----------------------------------------------------------------------------
mkdir -p cost_reports

REPORT_FILE="cost_reports/cost_report_${ENVIRONMENT_LABEL}_${CURRENT_END}.json"
echo "[INFO] Assembling final report → ${REPORT_FILE}"

python3 - \
  "${CURRENT_START}" "${CURRENT_END}" \
  "${PRIOR_START}"   "${PRIOR_END}" \
  "${FORECAST_START}" "${FORECAST_END}" \
  "${ENVIRONMENT_LABEL}" "${AZURE_SUBSCRIPTION_ID}" \
  "${REPORT_FILE}" \
  <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone

current_start, current_end, prior_start, prior_end, \
    forecast_start, forecast_end, \
    env_label, sub_id, report_file = sys.argv[1:]

# Job-scoped scratch; see the note where SCRATCH is defined for why it is not
# a bare /tmp path.
SCRATCH = os.environ["SCRATCH"]


def col_index(columns, name):
    for i, col in enumerate(columns):
        if col["name"] == name:
            return i
    raise ValueError(f"Column '{name}' not found in: {[c['name'] for c in columns]}")


def build_tag_case_map():
    """
    Read the scratch copy of the Resource Graph response and return a dict mapping
    lowercase tag values to their original-case equivalents.
    e.g. {"infrastructure and platforms": "Infrastructure and Platforms",
          "aide": "AIDE", "temp": "Temp"}

    Resource Graph response format:
      { "data": { "columns": [...], "rows": [[portfolio, project_tag], ...] } }
    """
    case_map = {}
    tags_file = f"{SCRATCH}/azure_tags.json"
    if not os.path.exists(tags_file):
        return case_map
    with open(tags_file) as f:
        data = json.load(f)
    # Resource Graph returns data as a list of dicts: [{"portfolio": "...", "project_tag": "..."}, ...]
    graph_data = data.get("data", [])
    if isinstance(graph_data, list):
        for row in graph_data:
            for value in row.values():
                if value and isinstance(value, str):
                    case_map[value.lower()] = value
    elif isinstance(graph_data, dict):
        # Fallback: older columns/rows tabular format
        for row in graph_data.get("rows", []):
            for value in row:
                if value and isinstance(value, str):
                    case_map[value.lower()] = value
    return case_map


TAG_CASE_MAP = build_tag_case_map()


def load_portfolio_file_keys():
    """Portfolio tag value -> the key its project files were written under.

    Written by the query loop rather than recomputed here, so the two sides
    cannot disagree about how a name becomes a filename.
    """
    keys = {}
    path = f"{SCRATCH}/projects/index.tsv"
    if not os.path.exists(path):
        return keys
    with open(path) as f:
        for line in f:
            key, _, name = line.rstrip("\n").partition("\t")
            if key and name:
                keys[name] = key
    return keys


PORTFOLIO_FILE_KEYS = load_portfolio_file_keys()


def normalize(value):
    """Return the proper-case version of a tag value if known, else return as-is."""
    return TAG_CASE_MAP.get(value.lower(), value) if value else value


def parse_tagvalue_response(filepath, default_label=None):
    """
    Parse a Cost Management response that uses generic TagKey/TagValue columns.
    Returns a list of { tag_value, cost, currency } dicts.

    default_label: if set, rows with an empty TagValue are kept and assigned
                   this label instead of being discarded. Pass None (default)
                   to skip empty-tagged rows (used for portfolio queries where
                   untagged resources are not meaningful at the portfolio level).
    """
    if not os.path.exists(filepath):
        return []
    with open(filepath) as f:
        data = json.load(f)
    props = data.get("properties", {})
    columns = props.get("columns", [])
    rows = props.get("rows", [])
    if not columns or not rows:
        return []
    ci  = col_index(columns, "Cost")
    vi  = col_index(columns, "TagValue")
    uri = col_index(columns, "Currency")
    result = []
    for row in rows:
        tag_value = row[vi]
        if not tag_value:
            if default_label is None:
                continue
            tag_value = default_label
        else:
            tag_value = normalize(tag_value)
        result.append({
            "tag_value": tag_value,
            # As the API returned it, before case correction. This is the key
            # the query loop recorded its filenames against.
            "raw_value": row[vi] or "",
            "cost":      round(row[ci] or 0, 2),
            "currency":  row[uri] or "USD",
        })
    return result


def build_period(period_label):
    """
    Build the portfolio hierarchy for one period (current or prior).
    Returns a list of portfolio dicts, sorted by total_cost desc.
    """
    portfolios_raw = parse_tagvalue_response(f"{SCRATCH}/portfolio_{period_label}.json")

    result = []
    for p in portfolios_raw:
        name      = p["tag_value"]
        # Looked up from the index the query loop wrote, keyed on the tag value
        # exactly as the API returned it — before any case correction, which is
        # what the two sides used to have to agree about.
        safe_name = PORTFOLIO_FILE_KEYS.get(p.get("raw_value", ""))
        if safe_name is None:
            print(f"[WARN] No project file recorded for portfolio {name!r} — "
                  f"its projects will be missing from this report", file=sys.stderr)
            proj_file = None
        else:
            proj_file = f"{SCRATCH}/projects/{safe_name}_{period_label}.json"

        projects_raw = (parse_tagvalue_response(proj_file, default_label="(untagged)")
                        if proj_file else [])
        projects = sorted(
            [
                {
                    "project":  pr["tag_value"],
                    "cost":     pr["cost"],
                    "currency": pr["currency"],
                }
                for pr in projects_raw
            ],
            key=lambda x: x["cost"],
            reverse=True,
        )

        result.append(
            {
                "portfolio":  name,
                "total_cost": p["cost"],
                "currency":   p["currency"],
                "projects":   projects,
            }
        )

    return sorted(result, key=lambda x: x["total_cost"], reverse=True)


def build_comparison(current_portfolios, prior_portfolios):
    """
    Merge both periods and compute absolute + percentage change per
    portfolio and per project.
    """
    cur_map = {p["portfolio"]: p for p in current_portfolios}
    pri_map = {p["portfolio"]: p for p in prior_portfolios}
    all_names = sorted(
        set(list(cur_map) + list(pri_map)),
        key=lambda n: cur_map.get(n, {}).get("total_cost", 0),
        reverse=True,
    )

    result = []
    for name in all_names:
        c = cur_map.get(name, {})
        p = pri_map.get(name, {})
        c_cost = c.get("total_cost", 0)
        p_cost = p.get("total_cost", 0)
        change = round(c_cost - p_cost, 2)
        change_pct = round((change / p_cost) * 100, 1) if p_cost else None

        cp_map = {pr["project"]: pr for pr in c.get("projects", [])}
        pp_map = {pr["project"]: pr for pr in p.get("projects", [])}
        all_projects = sorted(
            set(list(cp_map) + list(pp_map)),
            key=lambda n: cp_map.get(n, {}).get("cost", 0),
            reverse=True,
        )

        project_comparison = []
        for proj in all_projects:
            cp = cp_map.get(proj, {})
            pp = pp_map.get(proj, {})
            cp_cost = cp.get("cost", 0)
            pp_cost = pp.get("cost", 0)
            p_change = round(cp_cost - pp_cost, 2)
            p_change_pct = round((p_change / pp_cost) * 100, 1) if pp_cost else None
            project_comparison.append(
                {
                    "project":      proj,
                    "current_cost": cp_cost,
                    "prior_cost":   pp_cost,
                    "change":       p_change,
                    "change_pct":   p_change_pct,
                    "currency":     cp.get("currency") or pp.get("currency") or "USD",
                }
            )

        result.append(
            {
                "portfolio":    name,
                "current_cost": c_cost,
                "prior_cost":   p_cost,
                "change":       change,
                "change_pct":   change_pct,
                "currency":     c.get("currency") or p.get("currency") or "USD",
                "projects":     project_comparison,
            }
        )

    return result


current_portfolios = build_period("current")
prior_portfolios   = build_period("prior")
comparison         = build_comparison(current_portfolios, prior_portfolios)

# Forecast — daily granularity returns one row per (portfolio, date); sum by portfolio.
# Falls back gracefully when running in backfill mode (file has no rows) or when
# the API returns an unexpected column layout (e.g. no TagValue column).
def parse_forecast_response(filepath):
    """Parse the forecast response, keeping track of what the total is made of.

    The query sets includeActualCost, so the response mixes rows the API marks
    Actual with rows it marks Forecast, in a CostStatus column. Summing them
    blindly labels spend that has already happened as forecast. Both are wanted
    in the figure — it is a projection for the whole window, part of which has
    already elapsed — but the split is worth recording so the page can say so
    rather than implying the entire number is predicted.

    Returns (totals, meta). meta carries the Actual/Forecast split and whether
    the API grouped by tag at all, which decides whether the per-portfolio
    figures are measured or derived.
    """
    meta = {"actual": 0.0, "forecast": 0.0, "unclassified": 0.0, "grouped_by_tag": False}
    if not os.path.exists(filepath):
        return [], meta
    with open(filepath) as f:
        data = json.load(f)
    props   = data.get("properties", {})
    columns = props.get("columns", [])
    rows    = props.get("rows",    [])
    if not columns or not rows:
        return [], meta

    def find(name):
        return next((i for i, c in enumerate(columns) if c["name"] == name), None)

    ci  = find("Cost")
    vi  = find("TagValue")
    uri = find("Currency")
    si  = find("CostStatus")
    if ci is None:
        return [], meta

    meta["grouped_by_tag"] = vi is not None

    totals = {}
    for row in rows:
        tag_value = row[vi] if vi is not None else "(subscription total)"
        if not tag_value:
            tag_value = "(untagged)"
        else:
            tag_value = normalize(tag_value)
        cost     = round(row[ci] or 0, 2)
        currency = row[uri] if uri is not None else "USD"

        status = str(row[si] or "").strip().lower() if si is not None else ""
        if status == "actual":
            meta["actual"] += cost
        elif status == "forecast":
            meta["forecast"] += cost
        else:
            # No CostStatus column, or a value we do not recognize. Counted
            # separately rather than guessed at, so the report never claims a
            # split it did not actually observe.
            meta["unclassified"] += cost

        if tag_value not in totals:
            totals[tag_value] = {"cost": 0.0, "currency": currency}
        totals[tag_value]["cost"] = round(totals[tag_value]["cost"] + cost, 2)

    for key in ("actual", "forecast", "unclassified"):
        meta[key] = round(meta[key], 2)

    return ([{"tag_value": k, "cost": v["cost"], "currency": v["currency"]}
             for k, v in totals.items()], meta)


forecast_raw, forecast_meta = parse_forecast_response(f"{SCRATCH}/forecast_portfolio.json")

# If the API returned only a subscription-level total (no per-portfolio TagValue
# grouping), allocate proportionally based on each portfolio's share of
# current-period actual spend.  When the API eventually does return per-portfolio
# data this branch is simply bypassed.
forecast_basis = "measured"
if len(forecast_raw) == 1 and forecast_raw[0]["tag_value"] == "(subscription total)":
    # The per-portfolio figures below are derived, not returned by Azure. The
    # report records that so the page can label them rather than presenting an
    # estimate alongside measured spend as though the two were equivalent.
    forecast_basis = "allocated"
    sub_total    = forecast_raw[0]["cost"]
    sub_currency = forecast_raw[0]["currency"]
    cur_total    = sum(p["total_cost"] for p in current_portfolios)
    print(f"[INFO] Forecast: no per-portfolio breakdown from API — allocating "
          f"${sub_total:,.2f} proportionally across {len(current_portfolios)} portfolio(s)")
    if cur_total > 0:
        forecast_raw = [
            {
                "tag_value": p["portfolio"],
                "cost":      round(sub_total * (p["total_cost"] / cur_total), 2),
                "currency":  sub_currency,
            }
            for p in current_portfolios
        ]
    else:
        forecast_raw = []

forecast_portfolios = sorted(
    [
        {
            "portfolio":     p["tag_value"],
            "forecast_cost": p["cost"],
            "currency":      p["currency"],
        }
        for p in forecast_raw
    ],
    key=lambda x: x["forecast_cost"],
    reverse=True,
)

report = {
    "environment":         env_label,
    "subscription_id":     sub_id,
    "report_generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "current_period": {
        "start":      current_start,
        "end":        current_end,
        "portfolios": current_portfolios,
    },
    "prior_period": {
        "start":      prior_start,
        "end":        prior_end,
        "portfolios": prior_portfolios,
    },
    "comparison": {
        "description": "Week-over-week cost comparison: current period vs prior period",
        "portfolios":  comparison,
    },
    "forecast": {
        "start":      forecast_start,
        "end":        forecast_end,
        # "measured" when Azure returned a per-portfolio breakdown; "allocated"
        # when one subscription total was split by current-period share.
        "basis":      forecast_basis,
        # What the total is made of. A window that starts today is almost all
        # forecast, but the split is observed rather than assumed.
        "components": {
            "actual":       forecast_meta["actual"],
            "forecast":     forecast_meta["forecast"],
            "unclassified": forecast_meta["unclassified"],
        },
        "portfolios": forecast_portfolios,
    } if forecast_portfolios else None,
}

with open(report_file, "w") as f:
    json.dump(report, f, indent=2)

if not current_portfolios and not prior_portfolios:
    print(f"[INFO] No cost data for {current_start} - {current_end}. "
          f"Writing an empty report so the window is recorded as covered "
          f"rather than being re-queried on every run.")

print(f"[INFO] Report written: {report_file}")
PYEOF

echo "[INFO] Report complete."

# Cumulative across every window in this job, so a backfill can be judged on
# whether the cache actually earned its place rather than assumed to have.
if [[ -f "${CACHE_STATS}" ]]; then
  CACHE_HITS=0; CACHE_MISSES=0
  read -r CACHE_HITS CACHE_MISSES < "${CACHE_STATS}" || true
  CACHE_TOTAL=$((CACHE_HITS + CACHE_MISSES))
  if [[ ${CACHE_TOTAL} -gt 0 ]]; then
    echo "[INFO] Cost API calls so far: ${CACHE_MISSES} made, ${CACHE_HITS} served from cache" \
         "($((CACHE_HITS * 100 / CACHE_TOTAL))% avoided). Current inter-call delay: $(cat "${DELAY_FILE}")s"
  fi
fi

# Dumping the full report is useful for a single weekly run, but a backfill
# writes one report per window and the output drowns the progress log.
if [[ -z "${OVERRIDE_CURRENT_START:-}" ]]; then
  echo "------------------------------------------------------------"
  cat "${REPORT_FILE}"
  echo "------------------------------------------------------------"
fi
