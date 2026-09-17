"""
Azure Cost Report — Email Notification

Sends an HTML summary email via SMTP with stat cards per subscription and a
cross-subscription portfolio overview (with project rows expanded) plus a
link to the full GitLab Pages dashboard.

Required CI/CD variables:
  SMTP_HOST          - SMTP server hostname
  SMTP_PORT          - SMTP port (default: 25)
  SMTP_FROM          - Sender address
  SMTP_TO            - Comma-separated recipient list
  CI_PAGES_URL       - Auto-set by GitLab when Pages is configured
  CI_PROJECT_NAME    - Auto-set by GitLab
  CI_PIPELINE_URL    - Auto-set by GitLab
"""

import json
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

SMTP_HOST    = os.environ.get("SMTP_HOST", "")
SMTP_PORT    = int(os.environ.get("SMTP_PORT", "25"))
SMTP_FROM    = os.environ.get("SMTP_FROM", "")
SMTP_TO_RAW  = os.environ.get("SMTP_TO", "")
PAGES_URL    = os.environ.get("CI_PAGES_URL", "")
PROJECT      = os.environ.get("CI_PROJECT_NAME", "azure-cost-analysis")
PIPELINE_URL = os.environ.get("CI_PIPELINE_URL", "")

if not all([SMTP_HOST, SMTP_FROM, SMTP_TO_RAW]):
    print("[ERROR] SMTP_HOST, SMTP_FROM, and SMTP_TO must all be set.", file=sys.stderr)
    sys.exit(1)

RECIPIENTS = [r.strip() for r in SMTP_TO_RAW.split(",") if r.strip()]

ENV_ORDER = {"dev": 0, "test": 1, "prod": 2}

# ---------------------------------------------------------------------------
# Tag-value naming (shared)
# ---------------------------------------------------------------------------
# Folding, aliases and shared-project identification live in
# .gitlab/scripts/common/naming.py so the page, the email and any future KPI
# collector cannot disagree about how many portfolios exist. Add new alias
# entries there, not here.
#
# These scripts are invoked by path rather than installed, so the shared
# directory is put on sys.path here. That keeps each script runnable on its own;
# pyrightconfig.json tells static analysis where to find it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from naming import (  # noqa: E402
    PORTFOLIO_ALIAS_BY_KEY,
    PORTFOLIO_ALIASES,
    PROJECT_ALIAS_BY_KEY,
    PROJECT_ALIASES,
    SHARED_PROJECTS,
    fold_name,
    is_shared_project,
    normalize_portfolio,
    normalize_project,
    pick_display_name,
)


# ---------------------------------------------------------------------------
# Data loading and normalization
# ---------------------------------------------------------------------------

def _merge_period_portfolios(portfolios: list, period_key: str) -> list:
    merged: dict = {}
    for p in portfolios:
        name = p["portfolio"]
        if name not in merged:
            merged[name] = dict(p)
            merged[name]["projects"] = list(p.get("projects", []))
            continue
        m = merged[name]
        if period_key == "current_period":
            m["total_cost"] = m.get("total_cost", 0) + p.get("total_cost", 0)
        else:
            m["current_cost"] = m.get("current_cost", 0) + p.get("current_cost", 0)
            m["prior_cost"]   = m.get("prior_cost",   0) + p.get("prior_cost",   0)
            cur = m["current_cost"]
            pri = m["prior_cost"]
            m["change"]      = cur - pri
            m["change_pct"]  = ((cur - pri) / pri * 100) if pri else None
        proj_map = {pj["project"]: dict(pj) for pj in m["projects"]}
        for pj in p.get("projects", []):
            pj_name = pj["project"]
            if pj_name not in proj_map:
                proj_map[pj_name] = dict(pj)
            else:
                ep = proj_map[pj_name]
                if period_key == "current_period":
                    ep["cost"] = ep.get("cost", 0) + pj.get("cost", 0)
                else:
                    ep_cur = ep.get("prior_cost", 0) + (ep.get("change") or 0)
                    pj_cur = pj.get("prior_cost", 0) + (pj.get("change") or 0)
                    ep["prior_cost"]  = ep.get("prior_cost", 0) + pj.get("prior_cost", 0)
                    new_cur           = ep_cur + pj_cur
                    new_pri           = ep["prior_cost"]
                    ep["change"]      = new_cur - new_pri
                    ep["change_pct"]  = ((new_cur - new_pri) / new_pri * 100) if new_pri else None
        m["projects"] = list(proj_map.values())
    return list(merged.values())


def normalize_reports(reports: list) -> list:
    for r in reports:
        for period_key in ("current_period", "comparison"):
            period     = r.get(period_key, {})
            portfolios = period.get("portfolios", [])
            for p in portfolios:
                p["portfolio"] = normalize_portfolio(p["portfolio"])
                for pj in p.get("projects", []):
                    if pj.get("project"):
                        pj["project"] = normalize_project(pj["project"])
            period["portfolios"] = _merge_period_portfolios(portfolios, period_key)
    return reports


def load_reports(reports_dir: Path):
    reports = []
    for path in sorted(reports_dir.glob("*.json")):
        try:
            with open(path) as f:
                reports.append(json.load(f))
        except Exception as e:
            print(f"[WARN] Could not parse {path}: {e}", file=sys.stderr)
    return normalize_reports(reports)


def fmt_env(env: str) -> str:
    parts = env.split("-")
    return parts[-1].capitalize() if parts else env


def sort_reports_by_env(reports):
    return sorted(
        reports,
        key=lambda r: ENV_ORDER.get(r.get("environment", "").split("-")[-1], 99)
    )


def fmt_cost(v):
    return f"${v:,.2f}"


def _change_html(change, change_pct):
    if change is None:
        return '<span style="color:#888">—</span>'
    if change == 0:
        return '<span style="color:#888">No change</span>'
    arrow = "▲" if change > 0 else "▼"
    color = "#c0392b" if change > 0 else "#1a7a3c"
    pct   = f" ({change_pct:+.3f}%)" if change_pct is not None else ""
    return f'<span style="color:{color};font-weight:600">{arrow} {fmt_cost(abs(change))}{pct}</span>'


def _sort_key(name: str) -> str:
    return ("zzz" if name == "(untagged)" else "") + name.lower()


def _pj_label(name: str) -> str:
    if name == "(untagged)":
        return '<span style="color:#aaa;font-style:italic">(untagged)</span>'
    return name


def _pf_label(name: str) -> str:
    if name == "(untagged)":
        return '<span style="color:#ccc;font-style:italic">(untagged resources)</span>'
    return name


# ---------------------------------------------------------------------------
# Cross-subscription aggregation
# ---------------------------------------------------------------------------

def _cross_env_portfolios(reports):
    """Merge portfolio + project comparison data across all subscription environments.

    Shared projects (expedition-0, gitlabrunners) are excluded from project rows.
    Returns a list sorted alphabetically, (untagged) last.
    """
    portf_map: dict = {}

    for r in sort_reports_by_env(reports):
        for p in r.get("comparison", {}).get("portfolios", []):
            pname = p.get("portfolio", "")
            if not pname or pname.lower() == "null":
                continue

            pm = portf_map.setdefault(pname, {
                "portfolio":    pname,
                "current_cost": 0.0,
                "prior_cost":   0.0,
                "projects":     {},
            })
            pm["current_cost"] += p.get("current_cost", 0)
            pm["prior_cost"]   += p.get("prior_cost", 0)

            for pj in p.get("projects", []):
                pj_name = pj.get("project", "")
                if not pj_name or is_shared_project(pj_name):
                    continue
                # current_cost may be explicit or derived as prior + change
                pj_cur = (pj.get("current_cost")
                          if pj.get("current_cost") is not None
                          else (pj.get("prior_cost") or 0) + (pj.get("change") or 0))
                pj_pri = pj.get("prior_cost") or 0
                if pj_name not in pm["projects"]:
                    pm["projects"][pj_name] = {
                        "project":      pj_name,
                        "current_cost": pj_cur,
                        "prior_cost":   pj_pri,
                    }
                else:
                    pm["projects"][pj_name]["current_cost"] += pj_cur
                    pm["projects"][pj_name]["prior_cost"]   += pj_pri

    result = []
    for pm in portf_map.values():
        cur = pm["current_cost"]
        pri = pm["prior_cost"]
        pm["change"]     = cur - pri
        pm["change_pct"] = ((cur - pri) / pri * 100) if pri else None

        projects = []
        for pj in pm["projects"].values():
            pj_cur = pj["current_cost"]
            pj_pri = pj["prior_cost"]
            pj["change"]     = pj_cur - pj_pri
            pj["change_pct"] = ((pj_cur - pj_pri) / pj_pri * 100) if pj_pri else None
            projects.append(pj)

        pm["projects"] = sorted(projects, key=lambda x: x["current_cost"], reverse=True)
        result.append(pm)

    return sorted(result, key=lambda p: _sort_key(p["portfolio"]))


# ---------------------------------------------------------------------------
# Build HTML email body
# ---------------------------------------------------------------------------

def collector_status(repo_root: Path, reports: list) -> dict:
    """Which collectors delivered this run, and which did not.

    The email is read by people who will not look at the pipeline, so a
    collector that failed has to be named here or it is invisible to them. That
    is the whole trade of running collectors with allow_failure: the pipeline
    stays green, so the page and the email become where failure surfaces.

    Neither collector alone knows which environments were expected. The union of
    the two is the best available answer without reading history, and it catches
    the case that matters — one domain missing an environment the other has.
    """
    cost_envs = {r.get("environment") for r in reports if r.get("environment")}

    resource_envs: set = set()
    degraded: dict = {}
    reports_dir = repo_root / "resource_reports"
    for path in sorted(reports_dir.glob("*.json")) if reports_dir.is_dir() else []:
        try:
            report = json.loads(path.read_text())
        except Exception:
            degraded[path.stem] = ["unreadable report"]
            continue
        env = report.get("environment", path.stem)
        resource_envs.add(env)
        failed = [k for k, v in report.get("scans", {}).items()
                  if v.get("status") != "ok"]
        if failed:
            degraded[env] = failed

    expected = cost_envs | resource_envs
    return {
        "missing_cost":      sorted(expected - cost_envs),
        "missing_resources": sorted(expected - resource_envs),
        "degraded_scans":    degraded,
        "resources_ran":     bool(resource_envs),
    }


def render_collector_warnings(status: dict) -> str:
    """A banner naming anything that did not deliver, or empty when all is well."""
    lines = []
    if status["missing_cost"]:
        names = ", ".join(fmt_env(e) for e in status["missing_cost"])
        lines.append(f"Cost data missing for <b>{names}</b> — totals below exclude it.")
    if not status["resources_ran"]:
        lines.append("Resource scan did not run — flagged-resource figures are unavailable.")
    elif status["missing_resources"]:
        names = ", ".join(fmt_env(e) for e in status["missing_resources"])
        lines.append(f"Resource scan did not report for <b>{names}</b>.")
    for env, scans in sorted(status["degraded_scans"].items()):
        lines.append(f"{fmt_env(env)}: resource scan incomplete — {', '.join(scans)}.")

    if not lines:
        return ""
    items = "".join(f"<li style='margin:2px 0;'>{line}</li>" for line in lines)
    return (
        "<div style=\"background:#4a2c14;color:#ffd7a8;border:1px solid #a8641e;"
        "border-radius:6px;padding:10px 14px;margin:0 0 16px;font-size:13px;\">"
        "<b>&#9888; Incomplete data</b>"
        f"<ul style='margin:6px 0 0;padding-left:18px;'>{items}</ul></div>"
    )


def build_email_html(reports, collector_warnings: str = ""):
    sorted_rpts  = sort_reports_by_env(reports)
    has_forecast = any(r.get("forecast") for r in sorted_rpts)

    first_report = sorted_rpts[0] if sorted_rpts else {}
    cp           = first_report.get("current_period", {})
    period_str   = f'{cp.get("start","?")} – {cp.get("end","?")}'

    pages_link    = (f'<a href="{PAGES_URL}" style="color:#7eb8f7">{PAGES_URL}</a>'
                     if PAGES_URL else "(Pages URL not available)")
    pipeline_link = (f'<a href="{PIPELINE_URL}" style="color:#7eb8f7">View pipeline</a>'
                     if PIPELINE_URL else "")

    # ── Stat cards ──────────────────────────────────────────────────────────
    # One card per subscription environment, side-by-side using table layout.
    # Email clients don't support flexbox/grid, so each card is a nested table.
    n_envs     = len(sorted_rpts)
    card_width = f"{100 // n_envs}%" if n_envs else "100%"
    card_cells = []

    for i, r in enumerate(sorted_rpts):
        env        = fmt_env(r.get("environment", "unknown"))
        comp_portf = r.get("comparison", {}).get("portfolios", [])
        total_cur  = sum(p.get("current_cost", 0) for p in comp_portf)
        total_pri  = sum(p.get("prior_cost",   0) for p in comp_portf)
        delta      = total_cur - total_pri
        delta_pct  = (delta / total_pri * 100) if total_pri else None

        fc         = r.get("forecast") or {}
        fc_total   = sum(p.get("forecast_cost", 0) for p in fc.get("portfolios", []))

        if total_pri == 0 or delta == 0:
            wow_html = '<span style="color:#aaa">No change</span>'
        else:
            arrow    = "▲" if delta > 0 else "▼"
            color    = "#e74c3c" if delta > 0 else "#27ae60"
            pct_str  = f" ({delta_pct:+.3f}%)" if delta_pct is not None else ""
            wow_html = f'<span style="color:{color};font-weight:600">{arrow} {fmt_cost(abs(delta))}{pct_str}</span>'

        fc_rows = (
            f'<tr><td style="padding-top:6px;font-size:10px;color:#aaa;">30-Day Forecast</td></tr>'
            f'<tr><td style="font-size:13px;font-weight:700;color:#27ae60;">{fmt_cost(fc_total)}</td></tr>'
        ) if fc_total else ""

        cell_pad = "0 0 0 10px" if i > 0 else "0"
        card_cells.append(
            f'<td width="{card_width}" valign="top" style="padding:{cell_pad};">'
            f'<table width="100%" cellpadding="0" cellspacing="0"'
            f' style="background:#ffffff;border-left:4px solid #0366d6;">'
            f'<tr><td style="padding:12px 14px;">'
            f'<div style="font-size:10px;color:#888;text-transform:uppercase;'
            f'letter-spacing:.05em;margin-bottom:8px;">{env}</div>'
            f'<table cellpadding="0" cellspacing="0" width="100%">'
            f'<tr><td style="font-size:11px;color:#aaa;padding-bottom:2px;">Current Week Total</td></tr>'
            f'<tr><td style="font-size:20px;font-weight:700;color:#0f2044;'
            f'padding-bottom:6px;">{fmt_cost(total_cur)}</td></tr>'
            f'<tr><td style="font-size:11px;color:#aaa;padding-top:4px;">WoW Change</td></tr>'
            f'<tr><td style="font-size:12px;padding-bottom:2px;">{wow_html}</td></tr>'
            f'{fc_rows}'
            f'</table>'
            f'</td></tr></table>'
            f'</td>'
        )

    cards_html = (
        f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">'
        f'<tr>{"".join(card_cells)}</tr>'
        f'</table>'
    )

    # ── Portfolio overview with project rows ─────────────────────────────────
    portfolios = _cross_env_portfolios(reports)

    # Per-portfolio forecast totals across all subscriptions
    fc_portf: dict[str, float] = {}
    for r in sorted_rpts:
        fc = r.get("forecast") or {}
        for p in fc.get("portfolios", []):
            pname = normalize_portfolio(p.get("portfolio", ""))
            if pname:
                fc_portf[pname] = fc_portf.get(pname, 0) + p.get("forecast_cost", 0)

    th = ("font-size:11px;text-transform:uppercase;letter-spacing:.05em;"
          "color:white;background:#0f2044;padding:9px 14px;")
    th_r = th + "text-align:right;"
    th_fc = (th + "text-align:right;background:#1a6b35;")

    forecast_th = (
        f'<th style="{th_fc}">30-Day Forecast</th>'
    ) if has_forecast else ""

    portf_rows = []
    for portf in portfolios:
        pname  = portf["portfolio"]
        cc     = portf["current_cost"]
        change = portf.get("change")
        cpct   = portf.get("change_pct")
        fc_val = fc_portf.get(pname, 0)

        # Portfolio header row — dark navy
        fc_pf_cell = (
            f'<td style="padding:9px 14px;background:#1a2e4a;text-align:right;'
            f'color:#5dbc82;font-weight:700;">{fmt_cost(fc_val)}</td>'
        ) if has_forecast else ""

        portf_rows.append(
            f'<tr>'
            f'<td style="padding:9px 14px;background:#1a2e4a;color:white;font-weight:700;">'
            f'{_pf_label(pname)}</td>'
            f'<td style="padding:9px 14px;background:#1a2e4a;color:white;'
            f'font-weight:700;text-align:right;">{fmt_cost(cc)}</td>'
            f'<td style="padding:9px 14px;background:#1a2e4a;text-align:right;">'
            f'{_change_html(change, cpct)}</td>'
            f'{fc_pf_cell}'
            f'</tr>'
        )

        # Project rows — indented, alternating background
        for j, pj in enumerate(portf.get("projects", [])):
            pj_name  = pj["project"]
            pj_cur   = pj["current_cost"]
            pj_chg   = pj.get("change")
            pj_cpct  = pj.get("change_pct")
            row_bg   = "#fafbfc" if j % 2 == 1 else "#ffffff"
            cost_sty = "color:#bbb;" if pj_cur == 0 else "font-weight:600;"

            fc_pj_cell = (
                '<td style="padding:7px 14px;border-bottom:1px solid #eee;'
                'text-align:right;color:#aaa;">—</td>'
            ) if has_forecast else ""

            portf_rows.append(
                f'<tr style="background:{row_bg};">'
                f'<td style="padding:7px 14px 7px 26px;border-bottom:1px solid #eee;'
                f'font-size:12px;color:#444;">{_pj_label(pj_name)}</td>'
                f'<td style="padding:7px 14px;border-bottom:1px solid #eee;'
                f'text-align:right;{cost_sty}font-size:12px;">{fmt_cost(pj_cur)}</td>'
                f'<td style="padding:7px 14px;border-bottom:1px solid #eee;'
                f'text-align:right;font-size:12px;">{_change_html(pj_chg, pj_cpct)}</td>'
                f'{fc_pj_cell}'
                f'</tr>'
            )

    portf_table = (
        f'<table width="100%" cellpadding="0" cellspacing="0"'
        f' style="border-collapse:collapse;font-size:13px;margin-bottom:16px;">'
        f'<thead><tr>'
        f'<th style="{th}text-align:left;">Portfolio / Project</th>'
        f'<th style="{th_r}">Current Spend</th>'
        f'<th style="{th_r}">WoW Change</th>'
        f'{forecast_th}'
        f'</tr></thead>'
        f'<tbody>{"".join(portf_rows)}</tbody>'
        f'</table>'
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
             max-width:700px;margin:0 auto;padding:0;color:#222;background:#f0f2f5;">

  <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td style="background:#0f2044;padding:18px 24px 14px;">
        <div style="font-size:18px;font-weight:700;color:white;">
          Caravan Weekly Cost Report (Azure)
        </div>
        <div style="font-size:12px;color:#a0b4cc;margin-top:4px;">
          Period: {period_str}
        </div>
      </td>
    </tr>
    <tr>
      <td style="padding:{"14px 24px 0" if collector_warnings else "0"};">
        {collector_warnings}
      </td>
    </tr>
    <tr>
      <td style="background:#1a3a5c;padding:9px 24px;font-size:13px;color:#d0dff0;">
        Full dashboard: {pages_link}
        {"&nbsp;&nbsp;&middot;&nbsp;&nbsp;" + pipeline_link if pipeline_link else ""}
      </td>
    </tr>
  </table>

  <div style="padding:20px 24px;">

    <div style="background:#fff8e1;border-left:4px solid #f0a500;border-radius:4px;
                padding:9px 13px;font-size:12px;color:#5a4000;margin-bottom:20px;line-height:1.5;">
      <strong>Tag data latency:</strong> Azure Cost Management reflects tag values with a
      24&#8211;48&nbsp;hour delay. Recent tag changes may not yet appear, and tag value
      inconsistencies will show as separate rows until corrected on the resource in Azure.
    </div>

    {cards_html}

    <div style="font-size:14px;font-weight:700;color:#0f2044;margin-bottom:12px;
                border-bottom:2px solid #d0d8e8;padding-bottom:8px;">
      Cross-Subscription Portfolio Overview
      <span style="font-size:11px;font-weight:400;color:#666;">
        &nbsp;&mdash;&nbsp;Current week spend
      </span>
    </div>

    {portf_table}

    <p style="font-size:11px;color:#999;margin-top:4px;line-height:1.5;">
      Portfolio totals include shared infrastructure costs (expedition-0, gitlabrunners).
      Shared project rows are omitted here. See the full dashboard for the allocated
      shared cost breakdown per portfolio.
    </p>

  </div>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Send email
# ---------------------------------------------------------------------------

def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_FROM
    msg["To"]      = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    print(f"[INFO] Connecting to {SMTP_HOST}:{SMTP_PORT}...")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.sendmail(SMTP_FROM, RECIPIENTS, msg.as_string())

    print(f"[INFO] Email sent to: {', '.join(RECIPIENTS)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    repo_root   = Path(os.environ.get("CI_PROJECT_DIR", "."))
    reports_dir = repo_root / "cost_reports"

    reports = load_reports(reports_dir)
    if not reports:
        print("[ERROR] No JSON files found in cost_reports/", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Building notification for {len(reports)} report(s)...")

    cur_period = reports[0].get("current_period", {})
    period_str = f'{cur_period.get("start","?")} to {cur_period.get("end","?")}'
    subject    = f"Caravan Cost Report (Azure) — {period_str} ({len(reports)} subscription(s))"

    status = collector_status(repo_root, reports)
    for key, label in (("missing_cost", "cost"), ("missing_resources", "resource")):
        if status[key]:
            print(f"[WARN] No {label} data this run for: {', '.join(status[key])}",
                  file=sys.stderr)
    for env, scans in sorted(status["degraded_scans"].items()):
        print(f"[WARN] {env}: resource scan incomplete — {', '.join(scans)}",
              file=sys.stderr)

    warnings = render_collector_warnings(status)
    if warnings:
        # Collectors run with allow_failure, so the pipeline stays green. If the
        # subject does not carry it, an incomplete report reads as a complete one.
        subject = f"[INCOMPLETE] {subject}"

    html_body = build_email_html(reports, warnings)
    send_email(subject, html_body)
