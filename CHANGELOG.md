# Changelog

Notable changes to the KPI pipeline. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file records what shipped. `README.md` describes how the pipeline works
today, and `WORKLOG.md` is the working record — it includes approaches that
were tried, measured and abandoned, which deliberately do not appear here.

---

## [1.0.0] — unreleased

First tagged release. Covers the rework of the original weekly cost report into
a multi-domain KPI pipeline with a resumable backfill and a period-scoped
dashboard.

### Added

- **Approved-image freshness page.** A collector compares the highest numbered
  tag in each configured GitLab container registry repository with its
  authoritative upstream release. Configured checks cover Traefik, TileServer
  GL Light, .NET SDK 10, and CUDA Toolkit using their validated release
  authorities. TileServer and .NET resolve the versions installed behind their
  simplified or floating image tags; TileServer also follows its active npm
  prerelease channel. The dashboard shows current and available versions, marks
  updates in red, and reports API or container-inspection failures as
  unavailable rather than as a pass.
- **Resumable cost backfill.** The backfill derives its target weeks from the
  JWCC period start, subtracts the weeks already present on the `data` branch,
  and processes only the difference — so a run that dies resumes where it
  stopped instead of starting over. Completed weeks are committed during the
  run (`BACKFILL_CHECKPOINT_EVERY`, default 5) rather than only at the end, and
  a partial run exits 75 so the pipeline continues and the persist job can
  harvest what finished.
- **Backfill controls** for testing and recovery: `BACKFILL_START` /
  `BACKFILL_END` for an explicit range, `BACKFILL_MAX_WINDOWS` to cap a run,
  and `BACKFILL_FORCE` to re-query weeks that already have data. The window
  list is logged before any work starts, and a confirmation gate guards against
  an accidental full backfill.
- **Resource KPI collector** as a job in its own right — underutilized VMs,
  orphaned disks and idle PostgreSQL servers — with its own history under
  `resource_history/` and its own per-scan status. Thresholds are tunable via
  `IDLE_DAYS` and `CPU_THRESHOLD`.
- **Collector status contract.** Every collector writes a status file even when
  it fails, and the page distinguishes "ran, found nothing" from "failed" from
  "did not run". Without it, a dead collector rendered as an all-clear.
- **Period scoping to the JWCC POP.** `JWCC_POP_START` anchors the reporting
  period; the dashboard shows every month from that start to the present and
  rolls to the next period automatically.
- **Tag inventory** persisted to the `data` branch, giving a durable record of
  the tag values seen in each environment rather than a per-run snapshot.
- **Shared modules** under `.gitlab/scripts/common/` — history fetch/persist,
  naming, period arithmetic and Azure login — so a second KPI domain does not
  re-derive them.

### Added

- **Archived periods of performance.** When a JWCC POP closes, its figures and
  portfolio names are frozen once and kept; each archived period gets its own
  full dashboard page at `archive/<period>/`, styled like the live one and
  reachable from a permanent "Previous periods" panel. The main page shows only
  the current period, so the panel is there before anyone needs it — the point
  is that nobody concludes a year of data was lost at rollover.

  Archive pages omit the resource tables and the forecast: both describe current
  state, and a forecast for a closed period is a prediction of the past.

- **Cluster utilization**, as a third KPI domain and a second page. The
  collector walks every AKS cluster in each subscription, pairs each with its
  own node resource group and its own kubeconfig, and joins Azure spend to what
  is running: cost and capacity per nodepool, requests split between DaemonSets,
  mesh proxies and applications, and seven-day CPU and memory averages read from
  Prometheus over a port-forward. History is kept under `cluster_history/`.

  Published as `cluster.html`, with a summary on the cost page linking across.
  The two are the same money at two granularities — Kubernetes is roughly a
  quarter of the bill and already inside the portfolio figures — so the cluster
  page is a drill-down rather than a separate subject.

  What it makes visible that neither source could alone: spend on clusters
  running nothing, capacity reserved and not used against capacity nobody asked
  for, cost per application including its share of the platform, workloads
  running on a nodepool they do not belong on, and pods that declare no CPU
  request at all and so are invisible to capacity planning.

  The two collectors read slightly different windows — an ISO week against a
  trailing seven days — so the share of spend is stated only when they coincide,
  which is the Monday schedule.

### Changed

- **Published-page navigation is visually consistent.** KPI Overview, Cluster
  Utilization, and Image Status now use the same oval-button navigation, label
  order, and selected-page treatment. Archive and cluster-preview navigation
  remain unchanged.
- **Cost history is sharded per week.** One file per environment per ISO Monday
  (`cost_history/<env>/<yyyy-mm-dd>.json`) instead of a single per-environment
  file. The filename is the week key, which is what makes resume a set
  difference rather than a parse. Commits are built from a manifest so a run
  touches only the files that actually changed.
- **All reporting windows anchor to ISO Mondays**, weekly and backfill alike,
  so weeks line up across environments and across runs.
- **Raw tag values are stored; canonicalization happens at render.** Aliasing on
  the way in destroyed the evidence of a tag problem. The stored data now shows
  what Azure actually returned, and the page folds variants together for
  display.
- **Backfill runtime roughly halved** (~30m to ~13m for six windows) by removing
  redundant work from the loop: overlapping windows are queried once, the
  Resource Graph tag-casing query is hoisted out of the per-window loop, and
  responses are cached on a hash of the request body.
- **API pacing is adaptive.** The fixed five-second sleep before every call was
  replaced with a delay that decays on sustained success and backs off on
  throttling, tunable via `COST_API_MIN_DELAY`, `COST_API_MAX_DELAY`,
  `COST_API_DECAY_AFTER` and `COST_API_BACKOFF`.
- **Dashboard shows the full period.** Every month in the current POP is
  rendered, with the summary columns (JWCC total, 30-day forecast, trend)
  pinned to the right and the month columns scrolling horizontally. Months with
  no data read `—` rather than `$0.00`, so a gap in collection is no longer
  indistinguishable from measured zero spend.
- **Forecast figures carry their provenance** — whether they are Azure's
  reported forecast or a locally derived estimate, and the actual/forecast
  split behind them — instead of being presented uniformly as reported.
- **The pipeline is named for what it does.** `RUN_TYPE` options are now
  "KPI Collection" and "Cost Backfill"; the pipeline gathers resource KPIs as
  well as cost.
- **Repository layout** reorganized into `.gitlab/jobs/`, `.gitlab/templates/`
  and `.gitlab/scripts/{common,costs,resources}/`, with Azure credentials,
  runner routing and retry policy factored into a shared base template.

### Fixed

- **A week with no cost data killed the job.** Filtering with `grep -v` under
  `pipefail` exited non-zero on empty input; prod alone had 29 such windows out
  of 38. Filtering now happens inside `jq`, and a failed window is logged and
  skipped rather than aborting the run.
- **Bookkeeping could fail a window that had succeeded.** A `printf` without a
  trailing newline left `read` at EOF, and `set -e` aborted the script after the
  report had already been written — every window would have been counted failed.
- **Scratch state leaked between jobs.** `/tmp` persists across jobs on these
  runners, so the response cache and tag map could serve a previous job's data;
  `BACKFILL_FORCE` would silently return cached results. Scratch is now scoped
  to the job ID.
- **A failed checkpoint could strand a batch permanently.** The manifest was
  rewritten per run, so an unchanged-on-disk batch dropped out of it and was
  never committed. It is now a durable pending list, cleared only on success.
- **Project result files could not always be read back.** Filenames were built
  in bash and reconstructed in Python, and a name-mangling mismatch meant some
  projects silently vanished; an explicit index file now carries the mapping.
- Shared-project detection compared differently-normalized strings and matched
  nothing; environment counts in the history writer were miscounted; alias maps
  were duplicated in two files and had begun to diverge.
- Forecast parsing ignored the `CostStatus` column, mixing actual and forecast
  rows into a single figure.
- **A week missing from inside a covered range went unreported.** An
  environment that never reported a month renders `—`, but a month missing one
  of its weeks simply totalled low and looked plausible. Gaps are now detected
  per environment and reported in the job log and on the page.
- **Derived costs were stored unrounded**, e.g. `1644.3700000000001`, from
  summing already-rounded figures and from recomputing `change` when two
  portfolios alias to the same name. Money is now rounded to the cent and
  percentages to a tenth at every point a value is derived. Existing history is
  corrected in place on the next run — no re-query, since every affected value
  is recomputable from data already stored.
- **One environment's cost collector failing took the whole page with it.**
  The three environments are matrix instances of one job, which had no
  `allow_failure`, and the page needed it non-optionally — so a single
  subscription failing meant nothing was published at all, even though the other
  two collected normally. The page already knew how to render a partial run and
  name the missing environments; only the job wiring was missing.
- **Sticky column styling leaked into the accordion tables.** The detail tables
  are nested inside the overview table, so descendant selectors reached them and
  pinned their columns to the parent's offsets. All pinning rules now use child
  combinators.

- **US English throughout.** Page text, log messages, identifiers and comments
  use one spelling convention, so the two pages a reader moves between do not
  disagree with each other.

- **One palette instead of 65 colors.** The page had accumulated 65 distinct hex
  values across 126 uses, most appearing once, which is why it read as flat
  rather than as designed. Every color now routes through a single token block
  shared by both pages, and three header tints carry meaning — measured spend,
  cumulative totals, and estimated figures, so a forecast never looks as solid
  as a measurement.

- **The cluster page is organized by decision, not by collection order.** It was
  151 rows across five tables with the leadership-facing one last, caveats
  scattered between them, and a 57-row table that mostly restated the other two.
  It is now four numbered sections — what Kubernetes costs, what each
  application costs, where capacity is going unused, what is on the wrong
  nodepool — with sortable tables, a filter that applies to all of them, per
  application detail behind an expander, placement grouped by what is wrong, and
  a collapsed reading guide holding the cost model and the caveats.

- **Main page order.** The clusters summary now follows the cost figures it
  subdivides rather than preceding them, and the archive index moved from above
  the content to the foot of the page, showing three periods with the rest
  behind an expander.

### Fixed (this release, page and collector)

- **The cluster collector's window drifted with the day of the run**, so it
  matched the cost period only on the Monday schedule, and its history shards
  were keyed to the Monday of the *current* week while holding the previous
  week's spend — leaving cluster history and cost history offset by one week in
  their filenames. Both collectors now derive the reporting week identically.

### Removed

- **The temporary cluster probe** — three diagnostic scripts, their jobs, and
  the `Cluster Probe` run type. Every question they were written to answer is
  settled and the production collectors cover the same ground. The one finding
  that lived only in probe source — how to query Log Analytics in Azure
  Government, where the CLI extension fails with an empty error — was recorded
  before deletion.

- The resource scan was removed from the cost collector (131 lines) and now runs
  as its own job, so a failing resource scan cannot take cost collection with it.
- Alias maps no longer transform data at storage time — they apply at render
  only.
- The legacy single-file-per-environment history reader, now that the flat files
  are off the branch and the shards cover the same weeks.
