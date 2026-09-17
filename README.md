# Azure KPI Pipeline

A GitLab CI pipeline that collects KPIs across three Azure Government
subscriptions, keeps a durable weekly history on a `data` branch, and publishes
a GitLab Pages dashboard.

Two domains are collected today — **cost** (spend by portfolio and project) and
**resources** (underutilized VMs, orphaned disks, idle databases). The two are
gathered by separate jobs so that a failure in one cannot prevent the other
being published.

> See `CHANGELOG.md` for what shipped in each version, and `WORKLOG.md` for the
> working record, including approaches that were evaluated and rejected.

---

## What it does

```
  ┌──────────────── analysis ────────────────┐
  │  azure_cost_analysis   × dev/test/prod   │   Cost Management API
  │  resource_scan         × dev/test/prod   │   Resource Graph + Monitor
  │  cluster_scan          × dev/test/prod   │   AKS + Kubernetes + Prometheus
  └──────────────────────┬───────────────────┘
                         │  artifacts
  ┌──────────────── publish ─────────────────┐
  │  kpi_pages                               │   fetch history → merge →
  │                                          │   render pages → persist history
  │  cluster_preview                         │   the cluster page as an
  │                                          │   artifact, for review
  └──────────────────────┬───────────────────┘
                         │
  ┌──────────────── notify ──────────────────┐
  │  kpi_notify                              │   HTML summary email
  └──────────────────────────────────────────┘
```

Each collector runs once per environment as a `parallel:matrix` job, resolving
its credentials from the matching GitLab environment scope. The publish job
gathers every collector's artifacts, merges them into the history held on the
`data` branch, renders the dashboard, and commits any changed history files back.

Because history lives on a branch rather than in artifacts, the dashboard can
show the full period even though each run only measures one week.

The site is three pages. `index.html` reports cost by portfolio and project;
`cluster.html` reports what the Kubernetes clusters cost and how much of that is
in use; and `image-status.html` currently displays the static result `Pass`.
Kubernetes is not a separate subject — it is roughly a quarter of the bill and
is already inside the portfolio figures, so the cluster page is a drill-down on
the first. A navigation row links the three live pages.

`index.html` runs: important information, cost by portfolio, the Kubernetes
summary, flagged resources, previous periods. The clusters section follows the
money it subdivides; the archive sits at the foot of the page and shows the
three most recent periods with any others behind an expander.

`cluster.html` is four numbered sections, each opening with the decision it
supports: what Kubernetes costs, what each application costs, where capacity is
going unused, and what is on the wrong nodepool. Every table sorts, and an
environment/cluster filter applies to all of them at once. A collapsed
*How to read this page* panel carries the cost model and the caveats, including
what the figures cannot tell you.

Closed periods of performance are frozen and republished under `archive/`; see
[Periods of performance](#periods-of-performance).

---

## Running it

`RUN_TYPE` is a pipeline input, chosen when the pipeline is triggered:

| `RUN_TYPE` | Jobs | Use it for |
|---|---|---|
| **KPI Collection** *(default)* | cost + resource collectors, page, notify | The normal weekly run. Measures the week just ended and rebuilds the page. |
| **Cost Backfill** | backfill collector, manual persist | Filling in missing weeks of cost history. Does not run the other collectors or rebuild the page. |

The pipeline only runs from a **schedule** or a **web** trigger; pushes do not
start it. The recommended schedule is Monday 06:00 UTC (`0 6 * * 1`).

A schedule that does not set `RUN_TYPE` gets the default and needs no change.

### Backfill

The backfill exists because history only accumulates one week per run. It
computes every ISO week from the period start to the present, subtracts the
weeks already on the `data` branch, and queries only what is missing.

It is designed to be interrupted:

- **Resume** — the weeks already present are read from the `data` branch at job
  start, so a re-run picks up where the last one stopped.
- **Checkpointing** — completed weeks are committed during the run, every
  `BACKFILL_CHECKPOINT_EVERY` windows (default 5), not only at the end.
- **Partial success** — a run that completes some windows exits `75`, which is
  allowed to pass. The job goes amber, the pipeline continues, and
  `azure_cost_backfill_persist` (manual) can harvest whatever finished.
- **No retry** — re-running a multi-hour backfill from scratch after a timeout
  produces nothing the first attempt did not already have, so `retry: 0`.

Expect roughly two minutes per window per environment.

Because a full backfill is expensive, it is gated behind a confirmation
variable; set the range explicitly when testing:

| Variable | Effect |
|---|---|
| `BACKFILL_START` / `BACKFILL_END` | Process an explicit date range (`YYYY-MM-DD`) instead of the default period-start-to-now. |
| `BACKFILL_MAX_WINDOWS` | Process only the first N windows. |
| `BACKFILL_FORCE` | Re-query weeks that already have data, overwriting them. |
| `BACKFILL_CHECKPOINT_EVERY` | Windows per checkpoint commit (default `5`). |

The full window list is logged before any query runs, so a mistaken range is
visible in the first few lines of the log rather than an hour in.

---

## Configuration

### Environment-scoped variables

One set per environment scope (`dev`, `test`, `prod`). GitLab injects the right
values per job from its `environment: name` setting, so no per-subscription
mapping is needed in the YAML.

| Variable | Description |
|---|---|
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID |
| `AZURE_TENANT_ID` | Azure tenant ID |
| `RUNNER_CLIENT_ID` | Service principal client ID |
| `RUNNER_CLIENT_SECRET` | Service principal client secret *(masked)* |

### Global variables

| Variable | Default | Description |
|---|---|---|
| `AI2C_API_RWA` | — | Bot PAT, set at group/instance level. Used to read and write the `data` branch through the GitLab Commits API, which bypasses push rules and needs no GPG signing. |
| `JWCC_POP_START` | previous 1 Dec | Period of performance boundaries — see below. Set in `.gitlab-ci.yml`, not in CI/CD settings. |
| `SMTP_HOST` | — | SMTP server hostname |
| `SMTP_PORT` | `25` | SMTP port |
| `SMTP_FROM` | — | Sender address |
| `SMTP_TO` | — | Comma-separated recipient list |

### Tunables

Rarely changed; defaults are in the scripts.

| Variable | Applies to | Description |
|---|---|---|
| `COST_API_MIN_DELAY` | cost | Floor for the adaptive inter-call delay. |
| `COST_API_MAX_DELAY` | cost | Ceiling for the same. |
| `COST_API_DECAY_AFTER` | cost | Consecutive successes before the delay decays. |
| `COST_API_BACKOFF` | cost | Base for the retry backoff ladder. |
| `IDLE_DAYS` | resources | Lookback window for utilization metrics (default `30`). |
| `CPU_THRESHOLD` | resources | Average CPU % below which a VM is flagged (default `20.0`). |
| `PIPELINE_METRICS_WORKERS` | pipeline | Concurrent GitLab project scans (default `32`). Lower this if the GitLab API begins throttling the collector. |
| `PUBLISH_ENVIRONMENT` | page / notify | Which runner publishes. The page uses `dev`; **notify uses `prod`, because dev's egress is restricted and cannot reach the SMTP relay.** |

---

## The `data` branch

All history is kept on a dedicated `data` branch, written by the publish job
through the GitLab Commits API.

```
cost_history/
  dev/
    2026-08-24.json          one file per ISO week
    2026-08-17.json
    tags/
      2026-08-24.json        tag values observed that week
  test/
  prod/
resource_history/
  dev/
    2026-08-24.json          resource findings for that week
  test/
  prod/
```

**One file per week, named by its ISO Monday.** The filename *is* the week key,
which is what lets the backfill compute what is missing as a set difference
rather than parsing and diffing a large document. It also means two
environments writing concurrently never touch the same file.

Writes are content-aware: a file whose content has not changed is not rewritten,
and commits are built from a manifest of what actually changed. A typical weekly
run commits three to six files. The manifest is a durable pending list, so a
batch that fails to commit is retried on the next run rather than being dropped.

Stored cost figures hold **raw tag values as Azure returned them**. Grouping
variant spellings together happens when the page is rendered, not when the data
is written — so the history remains evidence of what the tags actually said.

---

## Periods of performance

The dashboard is scoped to the JWCC period of performance containing today.
When a period closes, the page switches to the new one — it does not carry the
old months forward.

`JWCC_POP_START` is a **comma-separated list of the days periods began**, oldest
first, set in the `variables:` block of `.gitlab-ci.yml`:

```yaml
JWCC_POP_START: "2025-12-01,2026-08-30"
```

The most recent date rolls forward a year at a time, so the list only needs
touching when a boundary actually moves. **Append; never replace.** Removing an
earlier date erases the record that the period existed, and closed periods are
then re-derived by subtracting whole years from whatever remains — inventing
periods that never happened. Since archives are frozen on first write, a wrong
boundary has to be deleted from the data branch by hand.

A period that a moved boundary cut short is labeled by its months
(`2025-12_to_2026-08`) rather than by years, because a year-pair label would
claim twelve months that did not occur.

Every run states the period it resolved and where it came from:

```
[INFO] Reporting period 2026-08-30 to 2027-08-29 (JWCC_POP_START=2025-12-01,2026-08-30)
```

If that line reports the December 1 default when you expected otherwise, the
variable is not reaching the job — most often a CI/CD variable marked Protected
on an unprotected branch. The fallback is silent, so this line is the check.

A period beginning mid-month leaves its first monthly column covering only part
of that month; those are marked with a dagger and a hover explaining why.

Closed periods are not lost. Each is frozen once to `archive/<period>.json` on
the data branch and rendered to its own page under `public/archive/<period>/`,
which looks like the live dashboard and carries its own per-environment
downloads. A "Previous periods" panel on the main page links them, and is
present whenever an archive exists rather than appearing at rollover.

What is frozen is the **data** — the figures and portfolio names as they stood.
The page around them is rendered fresh each run, so styling changes reach old
periods while their numbers do not move. A period is frozen the first time a run
finds it closed and unarchived, rather than at the instant it closes, so a
missed or failed run at the boundary is picked up by the next one.

Archive pages carry neither resource findings nor a forecast. Both describe
current state.

---

## Failure model

The pipeline is built so that partial data still produces a page.

| What fails | What happens |
|---|---|
| One environment's **resource scan** | `allow_failure: true`. The page renders the other environments' findings and marks this one *Not collected*. |
| The **whole resource domain** | The page's `needs` are `optional`. Cost renders normally; the resource panel reads *Resource scan did not run*. |
| One environment's **cost collector** | `allow_failure: true`. The page renders the environments that did report, names the missing one in a banner, and excludes it from current-period totals. Its monthly history is unaffected. |
| **Every** cost collector | The page job fails and publishes nothing, leaving the last good page in place. An empty shell is worse than a stale page. |
| A **backfill window** | Logged and skipped; the run continues and reports attempted/succeeded/failed at the end. |
| A **partial backfill** | Exit `75`, job amber, pipeline continues, persist job can still run. |

### The status contract

Failure isolation is only safe if the page can tell *absent* from *empty*.
A collector that dies and is silently omitted would render as
`Underutilized VMs (0) — None detected`, which reads as an all-clear.

So every collector writes a status file **always**, including on failure, via
`after_script` so it survives the script itself dying:

| Page sees | Renders |
|---|---|
| status `ok`, no results | *None detected* — trustworthy |
| status `failed` / `partial` | *Unavailable — last good data \<date\>* |
| no artifact at all | *Did not run* |

The same principle applies to cost figures: a month with no data renders `—`,
never `$0.00`. Measured zero and missing are different facts.

And a week missing from *inside* a covered range is reported separately, in the
log and in a banner on the page. That case has no visual cue of its own — the
month containing it simply totals low — so it has to be stated. A backfill over
the affected range fills it.

The distinction in the last two rows matters. A partial run still tells the
truth as long as it says what is missing, so it publishes with a banner. A total
run has nothing to say, so it declines to overwrite the last page that did.

---

## Prerequisites

### Azure

- A **service principal per subscription**, holding `Reader` and
  `Cost Management Reader`, scoped to that subscription.
- `Reader` also covers the **Resource Graph** queries used for tag casing and
  resource inventory.
- Resources tagged with `portfolio` and/or `project`.

### GitLab

- **ACI-based runners** registered per subscription and tagged `$ENVIRONMENT`
  (`dev`, `test`, `prod`).
- **GitLab Pages** enabled.
- A **`data` branch**, and a bot PAT (`AI2C_API_RWA`) able to write to it.
- CI/CD variables configured per the tables above.

---

## Repository layout

```
.gitlab-ci.yml                      stages, RUN_TYPE input, includes
.gitlab/
  templates/
    azure-base.yml                  .azure_env — credentials, runner, retry
                                    .azure_base — plus the cost collector
  jobs/
    costs/analysis.yml              weekly cost collector matrix
    costs/backfill.yml              backfill collector + manual persist
    costs/notify.yml                summary email
    resources/scan.yml              resource collector matrix
    cluster/scan.yml                cluster collector matrix
    cluster/preview.yml             cluster page as a reviewable artifact
    page/kpi_pages.yml              history merge, render, persist
  scripts/
    common/                         shared across domains
      azure_login.sh                service principal login
      fetch_history.py              pull the data branch as one archive
      persist_history.py            commit changed files via the Commits API
      history_io.py                 content-aware writes, manifest handling
      naming.py                     name folding, aliases, shared projects
      pop.py                        period-of-performance arithmetic
    costs/
      query_azure_costs.sh          the cost collector
      backfill_windows.py           window generation and resume
      update_history.py             merge reports into history shards
      archive_periods.py            freeze and re-render closed periods
      generate_report.py            the dashboard, cluster and image-status pages
      send_notification.py          the email
    resources/
      scan_resources.py             the resource collector
    cluster/
      scan_cluster.py               the cluster collector
      prom.py                       Prometheus access over port-forward
      render_cluster.py             the cluster panel, summary and page
    pipeline/
      collect_gitlab_metrics.py     concurrent GitLab CI activity collector
```

`generate_report.py` imports `render_cluster.py`, never the other way round:
the cluster module takes the page CSS as a parameter rather than importing it,
so the same rendering serves the published page and the preview artifact.

**Colors live in one place.** The `:root` block at the top of `CSS` in
`generate_report.py` holds every color either page uses; the cluster panel
references those tokens rather than declaring its own. Nothing writes a color
into markup — sparkline direction is a class, not a `fill` attribute. Restyling
is an edit to that block.

The `.azure_env` / `.azure_base` split exists so a new KPI domain inherits Azure
authentication, runner routing and retry policy without re-deriving them, and so
those cannot drift between collectors.

---

## Report artifact

Each cost collector writes `cost_reports/<environment>.json`, retained 90 days:

```jsonc
{
  "environment": "dev",
  "subscription_id": "...",
  "report_generated_utc": "2026-08-24T06:05:00Z",
  "current_period": { "start": "...", "end": "...", "portfolios": [ ... ] },
  "prior_period":   { "...": "..." },
  "comparison":     { "portfolios": [ ... ] },
  "forecast": {
    "basis": "reported",          // or a locally derived estimate
    "components": {
      "actual":       0.0,        // the actual/forecast split behind the figure
      "forecast":     0.0,
      "unclassified": 0.0
    }
  }
}
```

`basis` and `components` exist so the page never presents a derived estimate
with the same confidence as Azure's own forecast.

The resource collector writes `resource_reports/<environment>.json`, carrying an
overall `status` plus a per-scan `status` and `items` list, so one failing scan
does not invalidate the other two.

The cluster collector writes `cluster_reports/<environment>.json`: a
`cost_window`, then one entry per cluster carrying its own `status`, its spend
per nodepool, capacity and requests per nodepool and namespace, and Prometheus
usage where it could be read. Spend is gathered *before* the cluster is
contacted, so a cluster that cannot be reached still reports what it costs.

Both collectors report the most recently completed Monday-to-Sunday week, so
their windows and their history keys agree whatever day the pipeline runs. The
summary on `index.html` states Kubernetes as a share of reported spend only when
the two windows and the two environment sets do in fact match, and says why when
they do not — the check stays because the figure is worth nothing if the two
sides ever describe different periods.

---

## Known considerations

**Tag data latency** — Cost Management reflects tag values 24–48 hours behind.
Recent tag changes will not appear in the current report.

**Retagging is not retroactive** — Cost Management never re-tags historical
usage. Fixing a tag today improves future weeks only; already-recorded weeks
keep the value that was in effect at the time, and no backfill will change that.

**Tag value casing** — the Cost Management API lowercases tag values. Original
casing is recovered from Resource Graph. A resource that has never appeared in
Resource Graph may still show lowercase.

**Tag inconsistency** — variant values (`infrastructure & platforms` vs
`... and platforms`) or keys (`Portfolio` vs `portfolio`) are distinct to Azure
and appear separately in the stored data. The page groups known variants for
display; the underlying fix is to correct the tags on the resources.

**Untagged project costs** — resources with `portfolio` but no `project` appear
under their portfolio as **(untagged resources)**, making the spend visible and
flagging the gap.

**Rate limiting** — the Azure Government Cost Management API throttles
aggressively. The collector paces itself with an adaptive delay that decays on
sustained success and backs off on throttling, plus retry with exponential
backoff. Pacing dominates backfill runtime.

**API pagination** — the collector warns if a response exceeds 1000 rows. Not
expected at current volume, but worth watching as tagged resources grow.

**Untagged spend is dropped** — rows Azure returns with no `portfolio` value are
not carried into the report, so the page totals tagged spend rather than the
subscription total. Measured at 3.6% of the bill over one week in September
2026. Surfacing it as an `(untagged)` row is open work.

**Language** — everything a reader sees, and everything in the source, is US
English: *utilization*, *normalize*, *behavior*, *color*, *labeled*. The pages
are read together, so a spelling that changes between them reads as an
inconsistency rather than a preference. Upstream identifiers are exempt: a
metric, label or recording rule name is a name, not prose, and several in
kube-prometheus-stack are spelled the British way. Never rewrite one.
