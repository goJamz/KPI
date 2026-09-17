# KPI Pipeline — Work Log

Tracking the rework of the Azure KPI pipeline, driven primarily by the backfill
job being unable to complete a full run.

Phases are ordered by value: Phase 1 unblocks the backfill, Phase 2 makes it
resumable, Phase 2A separates the KPI collectors so one cannot take down the
others, Phase 3 makes it fast, Phase 4 hardens it. Phase 5 and 6 are
deliberately deferred until the jobs are producing real data.

Phase 2A is numbered out of sequence deliberately. It belongs between 2 and 3,
and renumbering would invalidate the item references used throughout this log.

Boxes stay unchecked until the change has been vetted against the real
GitLab/Azure environment — local review alone is not sufficient to mark
an item complete.

---

## Phase 1 — Unblock the backfill

**Status: verified on the 2026-08-28 backfill run.** All three environments
completed 38/38 windows with zero failures. The empty-window path was exercised
heavily and is what the old code died on: prod hit 29 windows with no cost data
and test hit 7. Items 1.3.1 and 1.3.2 remain unverified because nothing
actually failed — the partial-success path has only been exercised locally.

The backfill currently dies on the first week that has no cost data, which is
guaranteed to happen for subscriptions created within the reporting window.
Nothing else in this log matters until a backfill run can survive that.

### 1.1 Fix the empty-result crash

- [x] **1.1.1** — `.gitlab/scripts/costs/query_azure_costs.sh` builds the
  portfolio list with a `jq | sort -u | grep -v '^$'` pipeline. When a week
  returns zero cost rows, `grep` finds no matches and exits 1; under
  `set -euo pipefail` that terminates the whole script. Replace the `grep`
  filter with something that tolerates empty input.
- [x] **1.1.2** — The portfolio loop is fed by a here-string
  (`done <<< "${PORTFOLIOS}"`). A here-string always emits a trailing newline,
  so an empty portfolio list still iterates once with an empty portfolio name,
  which then builds a malformed project query. Guard the loop so it does not
  execute when there are no portfolios.
- [x] **1.1.3** — When a window legitimately has no data, the script should
  still assemble and write a valid report file with zero portfolios rather
  than skipping it. A recorded empty week is what stops the resume logic in
  Phase 2 from retrying that window forever.

### 1.2 Isolate failures to a single window

- [x] **1.2.1** — `call_cost_api` calls `exit 1` on any API error or unexpected
  response shape. In the backfill loop that means one transient failure at
  week 37 discards the previous 36. Change the failure path so the function
  returns non-zero and lets the caller decide.
- [x] **1.2.2** — Wrap each backfill window so a failed window is logged,
  counted, and skipped, and the loop continues to the next one.
- [x] **1.2.3** — Print an end-of-job summary (windows attempted / succeeded /
  empty / failed) and exit non-zero only if the failure count is above a
  threshold, so a mostly-successful run is still visible as a partial success.

### 1.3 Stop stranding completed work

- [ ] **1.3.1** — `azure_cost_backfill_persist` declares
  `needs: azure_cost_backfill`, so GitLab skips it when the backfill job fails.
  The artifacts already exist (`artifacts: when: always` on `.azure_base`) but
  are unreachable. Allow the persist job to run when the backfill job fails.
- [ ] **1.3.2** — Confirm behavior when the backfill job hits the 4-hour
  project timeout — artifacts should still upload, but this needs verifying
  on the real runner rather than assuming.

### 1.4 Cleanup carried along with this phase

- [x] **1.4.1** — Update the stale `AI2C_API_RWA` references to `OASIS_API_RWA`
  in the `.gitlab-ci.yml` header comment (the Python scripts already read the
  correct name).
- [x] **1.4.2** — Remove the ~140 lines of commented-out job definitions from
  `.gitlab-ci.yml` that duplicate the live files under `.gitlab/jobs/`. They
  have already drifted from the real jobs and are a source of confusion when
  debugging which definition is actually in effect.

---

## Phase 2 — Sharded history and a resumable backfill

Today history is one array per environment (`cost_history/<env>.json`),
rewritten in full on every run. That layout is why the backfill has to be
all-or-nothing: there is no way to ask "which weeks do I already have?"
without reading and parsing the entire file, and no way for two jobs to write
without fighting over the same object.

Target layout, one file per environment per reporting week:

```
cost_history/
  dev/
    2025-12-01.json
    2025-12-08.json
    ...
  test/
  prod/
```

Filename is the ISO Monday anchoring the week. Existence of the file is the
record that the week is done.

### 2.0 Backfill test controls

Built ahead of the rest of Phase 2 so each subsequent change can be smoke
tested in minutes instead of a ~100-minute full run.

**Status: exercised on the 2026-08-28 smoke run** with
`BACKFILL_MAX_WINDOWS=3`. All three environments completed 3/3 windows, every
window ran Monday to Sunday, and the persisted snapshots matched. 2.0.3 stays
open because only the window limit was used — the explicit date range has not
been run against the real pipeline yet.

Caveat for future use: the limit takes the *newest* windows, which are the
densest (dev's recent weeks carry five portfolios and twelve API calls, the
oldest carry two portfolios and six). Per-window timings from a limited run
therefore overestimate a full run — the smoke run averaged 260s per window
against the full run's 155s median. Use it to check correctness, not to
project runtime.

- [x] **2.0.1** — Move window generation out of the inline `python3 -c` block
  in `backfill.yml` and into `.gitlab/scripts/costs/backfill_windows.py`, where
  it can be tested directly and extended by the resume logic in 2.3.
- [x] **2.0.2** — `BACKFILL_MAX_WINDOWS` processes only the first N windows.
- [x] **2.0.3** — `BACKFILL_START` / `BACKFILL_END` set an explicit date range.
  Start rounds forward to a Monday and end rounds back to a Sunday, so every
  generated window stays inside the requested range. This is also the
  re-query override called for by 2.3.4.
- [x] **2.0.4** — Log the full window list before processing, and fail fast
  with a clear message when the range produces no windows.

### 2.1 Anchor all reporting windows to ISO Mondays

- [x] **2.1.1** — The backfill currently derives windows from
  `today - 1 - 7k`, so a backfill triggered on a Thursday produces windows
  running Thu→Wed. `update_history.py` then buckets those by ISO-Monday
  anchor, meaning a Thursday backfill writes a window spanning two ISO weeks
  into a Monday slot and displaces the correctly-aligned weekly snapshot.
  Anchor backfill windows to ISO Mondays so they are identical to what the
  weekly scheduled run produces.
- [x] **2.1.2** — Align the weekly (non-backfill) path as well. Checking this
  showed it is *not* already safe: `query_azure_costs.sh` derived its window
  from `today - 1`, which only lands on a Monday-Sunday week when the pipeline
  runs on a Monday. The workflow rules also allow manual `web` triggers, and a
  mid-week manual run produced a Tue-Mon style window that would then be
  written into a correctly-named Monday shard, overwriting a good week with a
  misaligned one. Both paths now anchor to the most recently completed
  Monday-Sunday week; behavior for the scheduled Monday run is unchanged.
  Confirmed by the 2026-08-28 Cost Analysis run, which executed on a Friday
  and reported `Current period : 2026-08-17 → 2026-08-23`. Under the previous
  logic that same run would have produced 2026-08-21 → 2026-08-27.
- [x] **2.1.3** — Clamp the earliest backfill window to the JWCC POP start
  (Dec 1 of the prior year) rather than letting the loop run past it.

### 2.2 Move history to per-week shards

**Status: verified on the 2026-08-28 Cost Analysis run.**
`fetch_history` reported `Fetched 12 history file(s) from data branch across 3
environment(s)` (9 shards plus 3 legacy flat files), all seven loaders returned
data, and `persist_history` reported `Committed 12 file(s) — 0 new, 12
updated`, which means its tree listing matched real nested paths such as
`cost_history/dev/2026-08-17.json`.


- [x] **2.2.1** — Change `update_history.py` to write one file per
  environment per week instead of appending to a single array. The per-week
  snapshot shape stays as it is today. Dedup by week is now implicit in the
  filename, so the old `update_history()` merge helper is gone. Fixed 4.1.5
  along the way — the summary counts environments rather than report files.
- [x] **2.2.2** — Change `fetch_history.py` to walk the `cost_history/`
  subdirectories on the data branch. The current implementation lists a single
  flat tree with `per_page=100`, which will silently truncate once the shard
  count grows past 100 — pagination is required here. Now walks recursively
  with a page loop and mirrors the layout locally instead of flattening it.
- [x] **2.2.3** — Change `persist_history.py` to build commit actions from the
  nested directory structure. Also replaced the per-file `file_exists` call
  used to pick create-versus-update: that was one API request per file, fine
  for three files but over a hundred before every commit once sharded, and
  repeated on each checkpoint once 2.4.2 lands. One recursive tree listing
  answers it instead.
- [x] **2.2.4** — Add a shard-aware loader in `generate_report.py` that reads
  the per-week files and returns the same in-memory list of snapshots the
  existing functions expect. Implemented as `load_history()`, which groups
  snapshots by environment, dedupes one per ISO week, and applies the 2.2.6a
  merge. It also still reads the legacy flat `<env>.json` files, with shards
  winning where a week exists in both, so a page render works against history
  written before the split — the weekly job runs on its own schedule and must
  not depend on a backfill having happened first. (The flat files were removed
  from the data branch before the 2026-08-28 full backfill, so this path is now
  unused; it is cheap to keep and is the only thing that would read a flat file
  if one reappeared.) Result is cached, since all
  seven loaders walk the whole history. `_load_history_snapshots` is gone,
  its dedup logic having moved into `load_history`.

  Done together with 2.2.1-2.2.3 rather than after them: changing the write
  format without the reader leaves the page unable to read its own data.

  Verified across sharded, legacy-flat, and mixed layouts — all seven loaders
  return identical results in each, a shard takes precedence over a stale
  legacy entry for the same week, and a full `generate_report.py` run against
  shards renders the same four canonical portfolios.
- [x] **2.2.5** — Store raw tag values, not canonical ones. Aliasing is
  currently applied in `update_history.py` *before* persisting, so the merged
  name is all that survives and the raw variants are destroyed at the only
  layer that is permanent. The reports already carry the raw values; the
  history should too. Canonicalization then becomes purely a render-time
  concern, which also makes it retroactive — adding an alias later re-merges
  all existing history with no re-backfill.

  **Precondition — case folding.** `normalize_portfolio` returns unknown names
  unchanged, so `PEO C3N` and `peo c3n` are two distinct canonical keys today.
  That is currently masked because the Resource Graph casing map fixes tag
  casing before anything is stored. Once raw values are persisted that mask is
  gone: if the Resource Graph query fails for one week, or a resource has since
  been deleted, that week stores lowercase values and the portfolio silently
  splits across weeks. The deterministic case/`&`-vs-`and` folding described in
  6.1 therefore has to land with 2.2.5, not after it.

  Implemented. `update_history.py` no longer aliases or merges — its alias map
  is gone and snapshots carry tag values exactly as Azure returned them.
  `generate_report.py` gained `fold_name()`, which collapses case, whitespace,
  punctuation and `&`-versus-`and` to one key, so that entire class of variants
  merges with no configuration; alias entries are now needed only for genuine
  misspellings, and one entry covers every punctuation and casing form of it.

  **Display names are resolved globally, not per snapshot.** Resolving per week
  would let the same portfolio be titled `PEO C3N` in one week and `peo c3n` in
  another, and since the loaders key on the displayed name that would split it
  straight back apart. `_resolve_display_names` scans every week of every
  environment first, then picks one spelling per folded key: alias form if there
  is one, otherwise a spelling carrying capitalization (an all-lowercase value
  usually means the Resource Graph casing lookup missed it), then most frequent,
  then alphabetical for stability. The report side reuses the map history
  resolved, because history has seen every week's spellings and this week's
  report only one — resolving them independently could disagree and split the
  current week from its own monthly columns.

  `send_notification.py` folds identically so the email and the dashboard it
  links to do not disagree about how many portfolios exist. The maps are still
  triplicated (4.1.7).

  Verified: formatting variants fold with no alias entry while the
  `ifrastructure` misspelling correctly does not; a portfolio spelled
  `PEO C3N` in two weeks and `peo c3n` in a third renders as `PEO C3N`
  throughout; all seven loaders return identical results from raw shards and
  from equivalent pre-merged shards; the real canonical-name shards already on
  the data branch still render their four portfolios unchanged; and page and
  email agree on fold and normalize for every variant tested.

  **Note on existing history:** the 114 shards on the branch were written
  before this change, so they hold canonical merged names and their raw
  spellings are not recoverable without re-querying Azure. New weeks store raw
  from now on. 6.1's per-variant attribution will therefore cover this week
  forward unless the backfill is re-run — cheap to defer until 2.3 makes
  re-running selective.
- [x] **2.2.6** — Audit collision handling in the `generate_report.py` history
  loaders before 2.2.5 lands. **Done — two real defects, both silent.**

  All seven history loaders read through one funnel, `_load_history_snapshots`.
  Reproduced against a snapshot carrying two raw variants of one portfolio:

  | Loader | Behavior on a canonical-name collision |
  |---|---|
  | `load_monthly_portfolio_history` | `portfolio_costs[name] =` **assigns** — last variant wins |
  | `load_env_monthly_totals` | keys on env + month only — safe |
  | `load_jwcc_pop_totals` | accumulates — safe |
  | `load_jwcc_pop_with_shared` | cost accumulates, but `week_counts[pname] =` **assigns** |
  | `load_monthly_project_history` | accumulates — safe |
  | `load_env_portfolio_monthly` | accumulates — safe |
  | `load_monthly_shared_per_portfolio` | `week_counts[pname] =` **assigns** |

  Measured on a fixture with I&P split across two raw variants (2 and 1
  non-shared projects) plus a second portfolio, and 30.00 of shared cost:

  | Value | Correct | With raw variants |
  |---|---|---|
  | I&P monthly total | 102.50 | **55.00** |
  | I&P shared allocation | 22.50 | **15.00** |
  | PEO C3N shared allocation | 7.50 | **15.00** |

  The monthly total drives the overview columns and sparklines; the shared
  allocation is split by project count, so an overwritten count misallocates
  shared spend *between* portfolios rather than just losing it.
- [x] **2.2.6a** — Fix by merging in `_load_history_snapshots` rather than
  patching the individual sites. It is the single funnel every loader reads
  through, so merging portfolios by canonical name there — and merging their
  project lists by canonical project name — makes all seven correct with no
  loader changes. Patching the sites individually would be wrong anyway:
  project counts need a de-duplicated union, not a sum, or a project appearing
  under two variants would be counted twice and skew the shared allocation the
  other way. This is the same merge `update_history.py` performs today, moved
  from write time to read time, which is precisely what 2.2.5 requires.

  Implemented as `_merge_snapshot_portfolios`. Verified: all seven loaders now
  return identical results for raw-variant and pre-merged input, including the
  case where the same project appears under two spellings (counted once, not
  twice); the three defect figures above are back to 102.50 / 22.50 / 7.50; the
  merge is a no-op on the real stored `dev.json`; and a full
  `generate_report.py` run renders one row per canonical portfolio with no raw
  variant leaking into the page. Raw spellings and their costs are kept on each
  merged entry under `variants`, so nothing is discarded — 6.1 can read them
  from there.
- [x] **2.2.7** — Persist the Resource Graph tag inventory (previously written
  to `/tmp/azure_tags.json` and discarded). It records which tag values existed
  on live resources at a point in time, which is what makes the "already fixed"
  versus "still broken" distinction in 6.1 possible. Resource Graph has no
  history of its own, so an observation not saved at the time cannot be
  recovered later — unlike cost data, there is no backfill for this.

  Stored at `cost_history/<environment>/tags/<iso-monday>.json`. That location
  was chosen because every history reader uses a non-recursive
  `glob("*.json")`, while `persist_history.py` uses `rglob` and
  `fetch_history.py` archives the whole `cost_history` subtree — so the
  inventory is committed and fetched with **no changes to either**, and stays
  invisible to `load_history` and to the per-environment download bundles.
  The artifact side sits in `cost_reports/tag_inventory/<env>.json` for the
  same reason: `cost_reports/*.json` is globbed non-recursively for reports.

  Keyed by the week the run happened rather than by a reporting window, because
  it describes the subscription now rather than the period being reported. That
  also means a backfill records it once instead of 38 identical times.

  The Resource Graph query itself is unchanged. It is guarded by `fail_window`,
  so a mistake in its KQL would fail every window, and there is no way to test
  a query change here. Counting resources per tag value — which 6.1's tag
  compliance rate would want — needs that query extended, and is left to 6.1.

  Verified: both Resource Graph response shapes parse; `load_history` still
  reports three environments and three weeks each rather than picking up a
  `tags` directory as a fourth environment; `persist_history` would commit the
  inventory files; download bundles contain only cost snapshots; and a
  cross-reference of stored raw spellings against the live inventory correctly
  separates `Ifrastructure & Platforms` (still on live resources, $900
  attributed) from spellings that are already canonical.

  **Confirmed against the first real inventory (dev, 2026-08-29).** Seven live
  portfolio spellings collapse to five portfolios, with three live spellings of
  Infrastructure and Platforms — `Ifrastructure & Platforms` via the alias map,
  `Infrastructure & Platforms` and `Infrastructure and Platforms` via folding.
  All three are on live resources today, so all three are open items in Azure.

  The inventory immediately answered questions the cost data alone could not:

  | Finding | Meaning |
  |---|---|
  | `Operations and Intelligence` tagged live, no cost recorded | new portfolio, or tagged resources not yet billing |
  | `PANGEA`, `swordfish` tagged live, no cost recorded | same |
  | `sqltest` in cost data, on no live resource | resource deleted or untagged; cost persists in history |

  That last row is exactly the distinction 6.1 needs and cost data alone cannot
  provide.
- [x] **2.2.8** — Existing `cost_history/<env>.json` files on the data branch.
  **Decided: regenerate, then delete the flat files — and delete them last.**

  The flat files currently hold three weeks (2026-08-03/10/17), all inside the
  range a full backfill regenerates, so nothing is lost by dropping them.
  `persist_history.py` has no delete action, so they survive a backfill either
  way; removing them is a manual step on the data branch.

  Order: push, smoke-test with `BACKFILL_MAX_WINDOWS=3`, full backfill, a Cost
  Analysis run to rebuild the page, then delete. Deleting last keeps a floor
  for the page while the shards are still incomplete — 2.3 resume does not
  exist yet, so a failed backfill still restarts from scratch.

  They should not be kept indefinitely: nothing writes flat files any more, so
  they are a frozen second source of truth, the weekly job re-fetches and
  re-commits them unchanged on every run, and after 2.2.5 they would hold
  canonical names while the shards hold raw values. The legacy reader in
  `load_history()` is a transition safety net, not a permanent dependency.

  **Both halves now done.** The flat files were deleted from the branch some
  time ago and the shards cover 2026-08-03/10/17, so the safety net had nothing
  left to catch — it was scanning for files that no longer exist.

  Reader removed from `load_history()`. The `overwrite` flag went with it: it
  existed only so shards could take precedence over flat files, and with one
  caller left it was always `True`. Confirmed a stray flat file in the history
  directory is now ignored rather than loaded as a fourth environment.

  **Confirmed on the same run.** `fetch_history` pulled 117 files across three
  environments, the page loaded three reports and monthly totals for all three,
  and no environment went missing. That was the failure mode worth watching:
  dropping an environment's history would not have turned the job red, it would
  just have made the page quietly short.

- [x] **2.2.9** — Fetch the history as one archive, and retry transient
  network failures. Fallout from the shard layout: `fetch_history.py`
  downloaded one file per API call, which was three requests before sharding
  and 116+ after the first full backfill, with no retry — `_api` caught only
  `HTTPError`, so a dropped TLS connection propagated and failed the job. That
  is exactly what happened on the 2026-08-28 page build (`SSLEOFError` partway
  through the loop). At a 0.5% per-request failure rate the job had roughly a
  44% chance of failing per run, and it grows with the history: a year is ~156
  shards, two years ~310.

  Fixed by pulling the whole subtree from the repository archive endpoint
  (`/repository/archive.tar.gz?sha=data&path=cost_history`) in a single
  request, which stays one request however much history accumulates, and by
  adding retry with exponential backoff to `_api` in both `fetch_history.py`
  and `persist_history.py`. Retries cover connection-level errors and 429/5xx;
  other 4xx are real answers and are not retried. A 60s timeout stops a hung
  connection stalling the job. `persist_history.py` makes far fewer requests,
  but a transient failure there discards the commit at the end of a
  multi-hour backfill, which is the same stranded-work problem Phase 1 set out
  to solve.

  A fetch that genuinely cannot complete still fails the job rather than
  rendering a page with no history — a silently blank dashboard is worse than
  keeping the previously published one.

  Verified against a stand-in API serving a real 114-shard archive: one request
  fetches all 114 into the correct nested layout; two injected connection drops
  retry at 2s and 4s and then succeed; an unreachable server exhausts retries
  and exits 1. A full `generate_report.py` run over the 114 shards renders
  `JWCC POP Period: Dec 2025 – Aug 2026`.

  Confirmed on the real pipeline: `Fetched 114 history file(s) from data branch
  across 3 environment(s)` in a single request, page rendered, and the two
  independently computed JWCC POP totals — summed per environment and summed
  per portfolio — agree at $113,432.86.

- [x] **2.2.10** — Restore the per-environment JSON downloads on the page.
  Second piece of shard fallout, missed when 2.2.9 was written. The page
  renders `data/<environment>.json` download links, and the pages job populated
  them with `cp cost_history/*.json public/data/`. Sharding moved those files
  into per-environment directories, so the glob matched nothing — and with
  `2>/dev/null || true` on the line it failed silently. The links were 404 on
  the 2026-08-28 page and nothing in the job log said so.

  `generate_report.py` now writes `public/data/<environment>.json` itself,
  concatenating that environment's shards in period order, so the file and the
  link that references it are produced by the same code. The download is the
  stored history as-is rather than the render-time merged view. The dead `cp`
  line is gone from `kpi_pages.yml`.

  Verified locally against the 114-shard set: three bundles of 38 weeks each
  spanning 2025-12-01 to 2026-08-23, and every `data/` link on the rendered
  page resolves to a file that exists.

### 2.3 Make the backfill resume

The shard filename *is* the week key, so working out what is left to do is a
set difference over filenames — no cost data is read and nothing is parsed.

- [x] **2.3.1** — At job start, fetch the existing shards for this environment
  and build the set of weeks already recorded. The backfill job now runs
  `fetch_history.py` before generating windows.
- [x] **2.3.2** — Compute the target window set from the JWCC POP start to the
  current week, subtract what already exists, and query only the difference.
  Done in `backfill_windows.py`, which reads `cost_history/<env>/*.json`. The
  `tags/` inventory directory is excluded because the glob is non-recursive.
  Empty weeks converge rather than being retried forever: a week with no spend
  still has a shard, which was the reason 1.1.3 insisted on writing one.
- [x] **2.3.3** — Log the resume decision explicitly, e.g.
  `Resume: 38 window(s) in range, 34 already recorded for 'dev', 4 to query.`
- [x] **2.3.4** — `BACKFILL_FORCE=1` re-queries weeks that already have a
  shard. Combined with `BACKFILL_START`/`BACKFILL_END` from 2.0.3 that
  re-gathers one specific range that turned out to be wrong.
- [x] **2.3.5** — A failed fetch is fatal. The script only sets `set -u`, so a
  `fetch_history.py` failure originally left the job running with no history
  and silently re-querying every week — hours of work to reach the state it
  should have started from. The job now stops instead. An empty or absent data
  branch is not a failure and reports itself as such.

  `BACKFILL_MAX_WINDOWS` now applies *after* the resume filter, so a smoke test
  gets N windows of real work rather than N windows that may all be skipped.

  Verified end to end against a stand-in API:

  | Case | Result |
  |---|---|
  | 34 of 38 weeks present | fetched 102 files, queried **4**, exit 0 |
  | All 38 present | `nothing to backfill`, exit 0, no queries |
  | Data branch unreachable | refused, exit 1, **0 windows queried** |
  | `BACKFILL_FORCE` + June range | re-queried exactly those 4 weeks |
  | Second environment | sees none of dev's work, 38 to query |
  | Invalid range | exit 1, unchanged |

  **Confirmed on the real pipeline, 2026-08-29.** With the branch already
  complete the job fetched 117 files (114 cost shards plus the three tag
  inventories from 2.2.7), reported `38 already recorded for 'dev', 0 to
  query`, and finished green **without a single Azure call**. A second run with
  `BACKFILL_FORCE=1 BACKFILL_MAX_WINDOWS=2` re-queried exactly two weeks, and
  its persist wrote 6 snapshots plus 3 inventories — `0 new, 9 updated`.

- [x] **2.3.6** — `update_history.py` treated an empty `cost_reports/` as
  fatal, which was correct until resume existed and wrong the moment it did.
  On the first real resume run the backfill correctly did nothing, wrote no
  reports, and the persist job then failed with
  `[ERROR] No JSON files found in cost_reports` — a correct run reported as a
  broken one. Now a no-op that exits 0. The page build is unaffected:
  `generate_report.py` keeps its own check and still refuses to render without
  reports, so a genuinely missing report is still caught where it matters.

  Also corrected a misleading log line: under `BACKFILL_FORCE` the window count
  was described as "remaining", when forcing means nothing was skipped.

### 2.4 Checkpoint during the run

Resume already means a failed run only costs the windows it had not reached —
*provided the persist job ran*. Checkpointing closes the remaining gap: a job
canceled, timed out, or killed before persist would otherwise lose everything
it gathered, however far it got.

- [x] **2.4.1** — Write each window's snapshot to disk as soon as that window
  completes. Done by running `update_history.py` at each checkpoint; it is
  idempotent and now only rewrites files whose content actually differs.
- [x] **2.4.2** — Commit completed weeks to the data branch during the run.
  **Prerequisite, and the bulk of the work: making commits minimal.** Since the
  backfill fetches existing history first (2.3.1), a naive checkpoint would
  re-send all 117 files every time. `update_history.py` now writes a manifest
  of what changed and `persist_history.py` commits only that. This fixes the
  weekly job too, which was re-committing the entire unchanged history on every
  run — the last pages log read `Committed 114 file(s) — 0 new, 114 updated`.
- [x] **2.4.3** — Handle concurrent commits from the three matrix jobs.

  **It fired, on 2026-09-02, after the cluster collector doubled the number of
  jobs writing to the data branch.** Prod failed with
  `HTTP 400: 9:reference update: reference does not point to expected object`
  while dev and test committed successfully.

  The retry machinery built here was correct and did not run: `CONFLICT_HINTS`
  did not contain GitLab's actual phrasing for this race, so a recoverable
  conflict was classified as a hard failure. Both phrases are now hints.

  The durable manifest from 2.4.5 did its job — prod's shard stayed pending
  rather than being lost, and the next run carried it. That is the second time
  that decision has paid for itself.

  **Proven on 2026-09-02.** The race recurred and was recovered from:

  ```
  [WARN] Branch moved under commit to /repository/commits (attempt 1/4) — retrying in 3.3s
  [INFO] Committed 1 file(s) to 'data' branch (420cecab) — 0 new, 1 updated
  ```

  Detection, jittered backoff and retry all behaved as designed. The item is
  closed on evidence rather than on the code looking right — which is why it sat
  open from Phase 2 until something actually collided.
  `persist_history.py` now retries on 409, and on a 400 whose message reads
  like a moved branch ref, with jittered backoff so three racing jobs do not
  collide again on the same beat. Other 4xx are real answers and are not
  retried.

  **Still unproven against a real race.** On the 2026-08-29 run all three
  environments checkpointed three times each — nine commits to one branch — and
  none reported `Branch moved under commit`. Either the API tolerated the
  overlap or the jobs simply never collided, since their windows take different
  amounts of time. Nothing to do about it beyond watching for the warning on a
  future run; the box stays open because the path has not executed.
- [x] **2.4.4** — `BACKFILL_CHECKPOINT_EVERY`, default 5 windows (~13 minutes
  at the measured 155s per window), 0 to disable. A final checkpoint runs after
  the loop, so a completed backfill needs no separate persist step. The persist
  job now fetches first, which makes it an idempotent safety net rather than a
  second full commit of everything.
- [x] **2.4.5** — The manifest is a durable pending list, not a per-run one.
  Found while testing: a file is only listed on the run that changes it, so if
  a checkpoint's commit failed and the manifest were rewritten from scratch,
  that batch would be unchanged on disk at the next checkpoint, drop out of the
  list, and never reach the branch — silent loss of exactly the work
  checkpointing exists to protect. Entries now accumulate and are cleared only
  after a commit succeeds.

  Verified: repeated checkpoints over unchanged reports commit nothing (the tag
  inventory included, whose capture timestamp is deliberately excluded from the
  comparison); a 7-window run with `BACKFILL_CHECKPOINT_EVERY=3` produced three
  commits of 4, 3 and 1 files, each carrying only that batch; two commits
  rejected as a branch race retried at 2.7s and 6.2s then succeeded; a genuine
  non-retryable 400 failed immediately with exit 1; and a batch whose commit
  failed was carried forward and committed by the next successful checkpoint.

  **Confirmed on the real pipeline, 2026-08-29** with
  `BACKFILL_FORCE=1 BACKFILL_MAX_WINDOWS=6 BACKFILL_CHECKPOINT_EVERY=2`:
  checkpoints after windows 2, 4 and 6 committed 2, 2 and 3 files — **7 files
  across 3 commits, where every commit previously carried all 117.** The
  separate persist job then fetched 117, found all 18 regenerated snapshots
  unchanged, and committed nothing, which is exactly the idempotent safety net
  it was meant to become.

### 2.5 Guard rails

- [x] **2.5.1** — The backfill persist path runs `update_history.py` without a
  preceding `fetch_history.py`, unlike the pages job. Resolved by 2.3 in a
  different way than expected: the *backfill* job fetches, not the persist job.
  The persist job still writes only the weeks the run actually queried, and
  `persist_history.py` has no delete action, so shards already on the branch
  are left untouched. Nothing is replaced.
- [x] **2.5.2** — Confirmation gate against an accidental backfill.
  **Closed as no longer needed — resume removed the hazard it guarded.**

  The item existed because a backfill used to rewrite the data branch wholesale.
  It no longer does: an accidental trigger against a complete history now finds
  every week recorded, queries nothing, and finishes green in seconds. There is
  nothing to protect against.

  What remains is an accidental `BACKFILL_FORCE=1`, which is expensive rather
  than destructive — it re-queries weeks and overwrites shards with freshly
  gathered data for the same periods. Forcing already requires deliberately
  setting a variable on a job that is manual-only, and the run announces itself
  (`re-querying all 38 window(s) in range, including 38 already recorded`). A
  further gate would add friction without adding protection.

---

## Phase 2A — KPI collector architecture

The pipeline is becoming a KPI dashboard rather than a cost report, but the
resource KPIs are gathered from inside the cost script. That has two costs, one
of them already being paid.

**It is expensive today.** The stale-resource scan sits inside
`query_azure_costs.sh`, which the backfill calls once per reporting window. A
38-window backfill therefore runs the VM, disk and Postgres scan 38 times per
environment for data that is point-in-time and identical every time. This phase
removes that by construction rather than optimizing it — **3.1.3 is superseded,
not implemented.**

**It will get worse.** Every future KPI domain either gets bolted into the same
script or invents its own conventions. And a failure in any one of them must not
prevent the others being gathered or the page being published.

Target shape: independent collectors, one per KPI domain, each producing
artifacts and its own history, converging at a page job that renders whatever
arrived and says plainly what did not.

### 2A.1 Extract the shared pieces

Mechanical, no behavior change, and verifiable by diffing page output before
and after. Doing it first is what stops the second collector reinventing auth,
retry and artifact conventions.

- [x] **2A.1.1** — Move `fetch_history.py` and `persist_history.py` to
  `.gitlab/scripts/common/` and parameterize them by domain. They are already
  most of the way there: `HISTORY_DIR` is an environment variable, and the only
  thing tying them to costs is three hardcoded `"cost_history"` literals (the
  archive subpath, the extract prefix, and the tree path in `existing_files`).
  This is not speculative refactoring — it is exactly what a second domain
  needs in order to persist anything.

  Done. `HISTORY_DIR` now also names the path on the data branch
  (`REMOTE_ROOT = HISTORY_DIR.name`), so one variable selects the domain on both
  sides. Proved by running `persist_history.py` with
  `HISTORY_DIR=resource_history` against a stand-in API and watching it commit
  `resource_history/dev/2026-08-24.json`, then running it unset and getting
  `cost_history/dev/2026-08-24.json`.
- [x] **2A.1.2** — Move `fold_name`, `PORTFOLIO_ALIASES`, `PROJECT_ALIASES`,
  `SHARED_PROJECTS` and the normalization helpers into
  `.gitlab/scripts/common/naming.py`. **Closes 4.1.7**, which exists because
  those are currently duplicated verbatim across three files and have already
  drifted once (4.1.9).

  Done — 153 lines of duplication replaced by an import in the two consumers.
  The alias-by-key maps became public API (`PORTFOLIO_ALIAS_BY_KEY`) since
  display-name resolution consults them directly; a leading underscore on a
  cross-module import was the wrong shape. `_resolve_display_names` stayed in
  `generate_report.py` because it walks snapshot structures rather than
  reasoning about names, and `naming.py` is deliberately free of any knowledge
  of report or snapshot shapes.
- [x] **2A.1.3** — Keep one base job template carrying Azure auth, runner tags,
  artifact retention and the retry policy, so a collector defines only what is
  specific to it. `azure-analysis.yml` is now `azure-base.yml` and splits into
  `.azure_env` (environment, tags, retry — everything needed to talk to one
  subscription, and nothing about what is collected) and `.azure_base`
  (`.azure_env` plus the cost collector's stage, script and artifacts). A second
  collector extends `.azure_env`. Verified the two-level `extends` chain still
  resolves the backfill's `retry: max: 0` override correctly.

  **Verification for all of 2A.1:** the page renders **byte-identically** before
  and after — same log, same `index.html`, same download bundles — which is the
  point of doing the mechanical move on its own before anything changes
  behavior.

### 2A.2 Split the resources collector out

- [x] **2A.2.1** — Move the stale-resource scan out of `query_azure_costs.sh`
  into `.gitlab/scripts/resources/scan_resources.py` — 131 lines out of the cost
  script. Rewritten as Python rather than bash-with-embedded-Python, since two
  of the three scans already were. Each scan reports its own status, so a failed
  VM scan does not make the disk findings look untrustworthy, and one unreadable
  resource does not lose the findings for all the others.

  `az login` was extracted to `.gitlab/scripts/common/azure_login.sh` at the
  same time, so the new collector does not re-derive authentication and the two
  cannot drift.
- [x] **2A.2.2** — Give it its own job under `.gitlab/jobs/resources/`, as a
  per-environment matrix like the cost collector, so each environment keeps its
  own runner and credentials.

  **This fixed a live bug.** The scan wrote fixed filenames with no environment
  in them — `cost_reports/flagged_resources/az_orphaned_disks.json` and the
  other two. All three matrix jobs wrote those same paths, and the page job
  downloads every job's artifacts into one workspace, so they overwrote each
  other. The flagged panel has been showing **one subscription's resources as
  though they were all of them**, with which one depending on artifact
  extraction order. One report per environment removes the collision, and each
  finding now carries the environment it came from.
- [x] **2A.2.3** — Do not include it in the backfill pipeline. Resource state is
  point-in-time; there is nothing to backfill. This is what makes 3.1.3
  disappear.
- [x] **2A.2.4** — Persist resource findings to the data branch as
  `resource_history/<environment>/<iso-monday>.json`. **New capability, not a
  move:** flagged resources are currently written to an artifact and copied to
  the page, and never reach the data branch at all, so there is no history and
  no way to answer "are we actually remediating?". This is what 6.2's realized
  savings trend needs, and every week that passes without it is a week of
  evidence thrown away. Written through the same shared history scripts, with
  `HISTORY_DIR: resource_history` selecting the domain — the first real use of
  2A.1.1. `write_if_changed` and the manifest handling moved to
  `.gitlab/scripts/common/history_io.py` for this, rather than being copied
  into the second collector.

### 2A.3 Failure isolation

- [x] **2A.3.1** — `allow_failure: true` on every collector, so one failing
  domain cannot prevent the others being published. Phase 1 already proved the
  mechanism works for satisfying `needs` — that is how
  `azure_cost_backfill_persist` survives an amber backfill.

  Verified: with the resource collector absent entirely, the page still renders
  cost data and reports `Resource scan did not run — no data for any
  environment`; with one environment's VM scan failing it renders the findings
  from the other two and says `Not collected for Test`; and the cost log is
  identical in both cases.

  **Confirmed on the real pipeline, 2026-08-29.** Nine jobs, all passed, 8m40s.
  The resource collector reported `0 underutilized VMs, 3 orphaned disks, 0 idle
  PostgreSQL servers` for dev and committed
  `resource_history/dev/2026-08-24.json` — the first time resource findings have
  ever reached the data branch. The cost job has zero residual references to
  resource scanning and picked up the shared `azure_login.sh` cleanly.

  Also confirmed the content-aware write is doing real work: only two of the
  three tag inventories were rewritten, because prod's tag values had not
  changed since the previous run.

  Gap found in the process: nothing in the page log said how much resource data
  had been loaded, so the run gave no evidence either way that the panel now
  covers all three environments — a collector silently missing from the
  artifacts would have looked identical to one that found nothing. The page now
  logs the environments and per-scan counts it loaded, and warns per environment
  about scans that did not complete.
- [x] **2A.3.2** — `optional: true` on each of the page job's `needs`. That
  covers the separate case of a collector the pipeline rules skipped entirely,
  which `allow_failure` does not.
- [x] **2A.3.3** — *(delivered across 2A.3.3a, 2A.3.3b, 2A.4.2 and 2A.4.3)*
  **A status contract, without which isolation makes the page lie.** If the resource collector dies and the page simply renders without it,
  the panel reads `Underutilized VMs (0) — None detected`, which is
  indistinguishable from a scan that ran and found nothing. That is the same
  confusion as 4.2.2's structurally-zero prod months, but worse, because this
  one reads as an all-clear.

  Every collector writes a status file **always**, including on failure — via
  `after_script` so it survives the script itself dying:

  | Page sees | Renders |
  |---|---|
  | status `ok`, empty results | "None detected" — trustworthy |
  | status `failed` or `partial` | "Unavailable — last good data <date>" |
  | no artifact at all | "Did not run" |

  The third row is the one that matters. Absence has to be distinguishable from
  emptiness, or isolation quietly converts a broken collector into a false
  all-clear.

- [ ] **2A.3.4** — **2A.3.1 was only half done, and this log recorded it as
  complete.** The claim was `allow_failure: true` on *every* collector. Only
  `resource_scan` has it. `azure_cost_analysis` has none — neither in the job
  nor in `.azure_base` — and `kpi_pages` needs it **non-optionally** with
  `artifacts: true`.

  So one environment's cost collector failing blocks the page entirely. Dev,
  test and prod are three matrix instances of one job: if dev dies, test and
  prod have collected fine, and nothing is published at all. That is precisely
  the outcome 2A.3 was written to prevent, and it is the domain where it
  matters most, since cost is the page's primary content.

  It was never caught because the verification under 2A.3.1 only exercised the
  resource collector — absent entirely, then failing for one environment. The
  cost half was never tested, and its absence looked the same as success.

  The fix is `allow_failure: true` on `azure_cost_analysis`, which composes
  correctly with 2A.4.1: a partial loss renders the environments that succeeded,
  and a total loss still fails loudly, because `generate_report.py` already
  refuses to publish an empty page.

  **Applied.** Put on `azure_cost_analysis` rather than `.azure_base`, because
  the backfill extends the same template and declares its own
  `allow_failure: exit_codes: [75]` — inheriting a blanket `true` there would
  mean a backfill that crashed outright still read as an acceptable outcome.

  Checking the page first showed the other half was already built: `main()`
  derives `missing_envs` as the environments with history that did not report,
  warns about them, and renders a banner above the tables. Nothing on the
  Python side needed changing; the design intent was there and only the wiring
  was absent.

  Verified locally against a fixture built from a real snapshot, three
  environments in history:

  | Reports present | Exit | Result |
  |---|---|---|
  | dev, test | 0 | 60KB page, banner reads *Cost data for Prod is missing from this run* |
  | none | 1 | no page written, previous page left in place |

  Not checked off: this changes what a red pipeline means, and wants confirming
  on the real thing before it counts. The next run where an environment
  genuinely fails is the real test — and 1.3.1's stranded-persist case may
  finally be observable at the same time.

### 2A.4 Let the page degrade instead of failing

- [x] **2A.4.1** — **The original item was wrong, and measuring it showed why.**
  It said the page should render whatever it has rather than exiting when
  `cost_reports/` is empty. Rendering with `reports=[]` produces **59 bytes**:
  the page is built from the current period's reports, and history only fills
  columns *within* rows those reports create. Publishing that would replace a
  working dashboard with a blank one, when failing instead leaves the last good
  page in place.

  So this stays a hard failure, but now says which case it is — every collector
  failed with history still on the branch, versus nothing to render at all.
  A history-driven page that can stand up without the current period is real
  work and belongs in Phase 5, not here.
- [x] **2A.4.2** — Load each domain in isolation, so malformed data in one
  degrades to an "unavailable" panel rather than aborting the render.

  The valuable half turned out to be the cost equivalent of 2A.3.3b. If one
  environment's cost collector failed, the page rendered the other two and said
  nothing — every cross-environment total quietly short, with no indication. The
  same asymmetry as the resources bug, in the domain that matters most.

  History is now the record of which environments this pipeline collects, so an
  environment with history but no report this run is a collector that did not
  deliver rather than one that no longer exists. The page carries a banner
  saying so, deliberately outside the collapsed info panel — a missing
  environment changes how every total should be read, so it cannot sit behind a
  disclosure triangle.

  Verified: no banner when all three report; with dev's report withheld,
  `Cost data for Dev is missing from this run. Current-period figures and totals
  below exclude it; monthly history for those environments is unaffected.`
- [x] **2A.4.3** — Surface collector status prominently on the page, and have
  the notify email name any collector that failed. Without this the trade-off
  below turns a loud failure into a silent one.

  The email is read by people who will never look at the pipeline, so a failed
  collector has to be named there or it is invisible to them — and with
  `allow_failure` the pipeline stays green, so the page and the email are now
  the only places failure surfaces. An incomplete report also prefixes the
  subject with `[INCOMPLETE]`, since the subject is the one part guaranteed to
  be read.

  Neither collector alone knows which environments were expected, so the email
  uses the union of the two — which catches the case that matters, one domain
  missing an environment the other has.

  Verified across four states: all collectors delivered (no warning, subject
  unchanged); one environment's disk scan failed; one environment's resource
  report never arrived; and the resource collector not running at all.

  **`kpi_notify` was never included in `.gitlab-ci.yml`**, so the notify stage
  had never run and no email had ever been sent. Now included; the weekly
  scheduled run sends to `SMTP_TO`, a manual run leaves it as a manual job.

  Its first run timed out reaching `smtp.az.cloud.army.mil:587`. **The cause was
  the environment, not the mail configuration:** the job runs on
  `PUBLISH_ENVIRONMENT`, which was `dev`, and the dev subscription is locked
  down for outbound traffic. Pointing it at `prod` sent the mail.

  **Worth knowing generally — the dev runner has restricted egress.** Anything
  added later that needs to reach outside Azure or GitLab from dev fails the
  same way, as a timeout rather than a refusal. `kpi_pages` also defaults to
  `dev` and is fine, because it only talks to the GitLab API.

  Speculative SMTP work (connection timeout, STARTTLS, auth options) was written
  and reverted. The port/TLS theory was wrong, and notification may be dropped
  entirely since people read the page rather than the email. The
  collector-status half was kept, being about the contract rather than email.

- [x] **2A.3.3a** — *(partial — the collector half is done, 2A.4 covers the
  page half in full)* Collectors write per-scan status and the page reads it.
  `resource_reports/<env>.json` carries `status` per scan, and the flagged panel
  distinguishes "scanned, found nothing", "not collected for these environments"
  and "did not run at all".
- [x] **2A.3.3b** — Detect an environment whose report never arrived.

  The first version of the contract had a hole, found by reading the rendered
  page from the 2026-08-29 run. It flagged scans that *failed*, but an
  environment whose report never arrived is simply absent from the status map,
  so nothing noticed. Three orphaned disks in dev rendered identically whether
  the other two environments were scanned and clean or had never run — which
  meant the page could not confirm the very collision fix it was built for.

  The page now takes the environments the cost collector reported as the
  expected set, and treats any missing from the resource reports as "no report"
  rather than silently omitting them. Verified across three cases: all three
  environments reporting with findings only in dev (no warning, correct); only
  dev reporting (`Not collected for Prod, Test` on every section, plus a warning
  per environment in the log); and nothing reporting at all
  (`Not collected — no data for any environment`).

  Worth noting for the rest of 2A.3.3: a status contract that only covers
  failures is not enough. Absence has to be derived from what *should* have been
  there, not from what turned up.

### 2A.5 Layout

```
.gitlab/
  jobs/       costs/  resources/  page/  notify/
  scripts/
    common/   history_store.py   (fetch/persist, domain as a parameter)
              naming.py          (fold_name, aliases, shared projects)
    costs/    query_azure_costs.sh, backfill_windows.py, update_history.py
    resources/scan_resources.py
    page/     generate_report.py
  templates/  azure-base.yml
```

- [x] **2A.5.1** — Adopt the layout above. Done, with `page/` scripts left in
  `costs/` for now: `generate_report.py` and `send_notification.py` still read
  cost data structures directly, so moving them would be a rename without a
  boundary behind it. Worth revisiting when Phase 5 restructures the page around
  domains.
- [x] **2A.5.2** — Reconsider the `RUN_TYPE` names. "Cost Analysis" is already
  inaccurate — that pipeline gathers resource KPIs too — and will read as wrong
  the moment a third domain exists. Renamed to "KPI Collection" and
  "Cost Backfill", with "KPI Collection" as the default.

  A schedule that does not set `RUN_TYPE` picks up the default and is
  unaffected, which per the README is how the weekly schedule is configured. A
  schedule that sets it explicitly would need updating.
- [x] **2A.5.3** — Artifact paths. **Decided: keep them as they are.**
  `cost_reports/` and `resource_reports/` already form a consistent
  `<domain>_reports/` convention, matched by `cost_history/` and
  `resource_history/`. Renaming to `kpi_data/<domain>/` would touch every script
  that reads them for no behavioral gain, and the convention it would replace
  is already predictable enough for a third domain to follow.

### The trade-off, stated plainly

`allow_failure` everywhere means a broken collector no longer turns the pipeline
red. The signal moves from "CI failed" to "the page says this data is stale".

For a dashboard people actually read, that is arguably the better place for it —
the failure reaches the audience rather than whoever happens to check CI. But it
only holds if 2A.3.3 and 2A.4.3 are done properly. Without them the trade is a
loud failure for a silent one, which is worse than what we have now.

### Consequences elsewhere

- **3.1.3 is superseded** — the stale-resource scan leaves the backfill path by
  construction.
- **4.1.7 is closed by 2A.1.2.**
- **2.4.3 will finally be exercised.** Six collector jobs committing to the data
  branch instead of three makes a real branch race likely, which is the one path
  in Phase 2 that has never executed.
- **Phase 5 depends on this.** Whatever the page becomes has to render several
  domains with per-domain status; that is the same contract as 2A.3.3.

---

## Phase 3 — Backfill performance

At ~10 portfolios the current backfill issues roughly 836 cost API calls per
environment. The mandatory 5-second pre-call sleep alone accounts for about
70 minutes of that before any network time. Phases 1 and 2 make the job
survivable; this phase makes it quick enough that re-running it is cheap.

**Measured on the 2026-08-28 run** (per environment, 38 windows):

| | dev | test | prod |
|---|---|---|---|
| Cost API calls | 336 | 250 | 120 |
| Rate-limit waits (30s each) | 51 | 36 | 8 |
| Resource Graph tag queries | 38 | 38 | 38 |
| Stale-resource scans | 38 | 38 | 38 |
| Time spent in `sleep` | **53 min** | 38 min | 14 min |

dev ran roughly 98 minutes end to end, so **about 55% of it was spent sleeping**
rather than waiting on Azure. Every rate-limit wait across all three
environments recovered on the first retry, so the throttling is real but mild.

### 3.1 Remove redundant work from the loop

- [x] **3.1.1** — Consecutive backfill windows overlap: week N's *prior*
  period is week N+1's *current* period. Roughly half of all cost API calls
  re-fetch data the same loop has already retrieved. Carry each window's
  result forward as the next window's prior period.

  Implemented as a response cache keyed on a hash of the request body, rather
  than by carrying values forward explicitly. Windows are separate invocations
  of the script, so there is nothing to carry in memory — but window N's prior
  period produces a byte-identical query body to window N+1's current period, so
  hashing the body catches the overlap on its own. It also catches any other
  repeat, such as the same portfolio and week being queried from a different
  direction, which a hand-rolled carry-forward would miss.

  Keyed by body rather than by dates so it cannot go stale against a query whose
  shape changes. Lives in `/tmp`, which is per-job, so nothing leaks between
  runs.

  Measured over 13 consecutive windows against a stand-in API: **46% of cost API
  calls avoided**, converging on 50% as the run lengthens — each window after
  the first reuses half its queries.
- [x] **3.1.2** — The Resource Graph tag-casing query runs once per window
  (38 times per environment) but returns the same point-in-time data every
  time. Hoist it out of the loop. The per-window scratch reset now keeps
  `/tmp/azure_tags.json`, and the query is skipped when it is already present:
  **1 query per job instead of 38 per environment**, confirmed by counting what
  reached the stand-in API.
- [ ] **3.1.3** — *(superseded by Phase 2A — the scan leaves the backfill path
  entirely when the resources collector is split out; keep this item only as
  the record of why)* The stale-resource block (`az extension add`, VM list, a
  30-day metrics call *per VM*, orphaned-disk graph query, a metrics call
  *per Postgres server*) also runs once per window. These are point-in-time
  facts with no relationship to a historical week and may well dominate the
  runtime on a subscription with many VMs. Move this block out of the backfill
  path entirely — it belongs only in the weekly analysis run.
- [x] **3.1.4** — Replace the unconditional `sleep 5` before every call with
  an adaptive delay, so the backoff only pays for itself when the API is
  actually pushing back. This alone is ~28 minutes of dev's runtime.

  The delay starts at 1s, rises by 2s on each throttle up to a 10s ceiling, and
  eases back by 1s after ten consecutive clean calls. Backing off *between*
  calls as well as before the retry matters: the retry alone only rescues the
  call that was throttled, while the next call is about to meet the same limit.

  Verified: with the first three calls throttled the delay climbed 1s → 7s, then
  decayed 7 → 6 → 5 over the following windows as calls succeeded. Tunable via
  `COST_API_MIN_DELAY` and `COST_API_MAX_DELAY`.

- [x] **3.1.5** — Bookkeeping must not decide a window's fate. The cache
  counters were written with `printf '%s %s'`, with no trailing newline, so the
  `read` that loads them hit EOF and returned non-zero — and under `set -e` that
  aborted the script *after* the report had been written. Every window exited 1
  and would have been counted as failed by the backfill despite having produced
  correct output. The counters now end with a newline and every `read` of them
  is guarded with `|| true`.

  Caught only because the summary line failed to print; the window still wrote
  its report, so nothing else looked wrong. Worth remembering that `set -e`
  makes any statistics-gathering a potential failure path.

- [x] **3.1.6** — Scratch state must be scoped to the job. **A correctness bug
  I introduced with 3.1.1 and 3.1.2**, found in the 2026-08-29 backfill logs:
  all three environments reported *zero* fresh Resource Graph fetches across six
  windows, meaning `/tmp/azure_tags.json` already existed at window one. These
  runners keep `/tmp` between jobs.

  Stale tag casing was the least of it. The response cache persisted across jobs
  too, so a `BACKFILL_FORCE=1` re-query would have been served a previous job's
  cached response and silently returned old data — the exact opposite of what
  forcing is for. The adaptive delay carried over as well, so a run could start
  throttled for no reason of its own.

  Before 3.1.1/3.1.2 the per-window reset deleted these files, so the staleness
  arrived with the caching. Everything now lives under `/tmp/kpi-$CI_JOB_ID`,
  which is unique per job by construction, with a best-effort sweep of scratch
  more than a day old so a long-lived runner does not fill up. The assembly
  heredoc is quoted, so the path is passed through the environment rather than
  interpolated.

  Verified: within one job, window 1 fetches and windows 2-3 reuse; a second job
  on the same machine gets 0% cache and a fresh tag fetch. **Confirmed on the
  real pipeline** — every environment now reports 1 fresh tag fetch and 5
  reuses across 6 windows, where before the fix all three reported 0 fetches.

  Note on why `/tmp` survives: these runners are shared across the org and other
  jobs would normally clean up after themselves. That makes the leftovers
  incidental rather than guaranteed — which is exactly why scoping by
  `CI_JOB_ID` is the right fix. Correctness here should not depend on other
  people's jobs tidying up.

**Confirmed on the real pipeline, 2026-08-29** (`BACKFILL_FORCE=1`,
`BACKFILL_MAX_WINDOWS=6`, all six windows completing in every environment):

| | dev | test | prod |
|---|---|---|---|
| Cost API calls made | 42 | 35 | 26 |
| Served from cache | 30 | 25 | 18 |
| **Avoided** | **41%** | **41%** | **40%** |
| Rate-limit waits | 5 | 8 | 4 |
| Final inter-call delay | 10s | 10s | 8s |

The cache saving holds against the real API. The delay behavior is worth a
second look though: it ratcheted to its 10s ceiling and stayed there, and the
throttle rate (about 12% of calls) is close to what the old fixed 5s produced
(15%). If a higher delay is not buying much less throttling, most of that delay
is pure cost — a lower `COST_API_MAX_DELAY` or a faster decay may well be
quicker overall. Needs measuring against wall-clock rather than reasoning about.

**Measured on the real pipeline.** A 6-window forced backfill took
**dev 17m34s, test 19m39s, prod 8m29s**, against roughly 30 minutes for the same
dev work before Phase 3 — **about 40% faster**.

Where dev's 17m34s goes, approximately:

| | |
|---|---|
| Inter-call delay | ~4m54s |
| Throttle backoff (6 × 30s) | ~3m00s |
| API latency, per-window login, assembly | ~9m40s |

- [x] **3.1.7** — The delay ceiling did not hold, and the ceiling itself was
  too high.

  `[[ delay -lt max ]] && delay=$((delay + 2))` raises whenever *below* the
  ceiling, so it steps straight past it — a 10s maximum produced 11s on test.
  Now clamped properly.

  More importantly, the ceiling was buying nothing. Across three real runs the
  throttle rate was **15% at a fixed 5s, 14% at 10s, and 22% at 11s** — spacing
  calls further apart did not reduce throttling at all. Whatever the limiter is
  measuring, it is not the gap between our calls. So a large delay is close to
  pure cost: dev spent about five minutes on it and got nothing back.

  Default `COST_API_MAX_DELAY` lowered from 10s to 4s, and the decay shortened
  from 10 clean calls to 5, so a burst of throttling no longer slows the rest of
  the job. Verified over 8 windows: the delay rises to the 4s ceiling under
  throttling, holds there, then decays 4 → 3 → 2 → 1 as calls succeed.

  **Result: the change was right, but the reasoning behind it was not.** At a
  4s ceiling the throttle rate rose in every environment — dev 14% to 21%, test
  23% to 26%, prod 15% to 27%. Spacing calls *does* affect throttling; the
  earlier claim that it does not was too strong, drawn from too few points.

  The net was still a clear win, for a different reason than the one given:
  **delay is paid on every call, backoff only on the fifth or so that throttles.**
  For dev, dropping 10s to 4s saved 42 x 6s = 4m12s and cost three extra
  throttles at 30s = 1m30s. Wall-clock went 17m34s to 13m00s, test 19m39s to
  17m00s. Prod went 8m29s to 11m00s, which the arithmetic does not explain and
  is most likely run-to-run variance in API latency — worth remembering that
  single-run comparisons here carry a couple of minutes of noise.

- [x] **3.1.8** — Lower the retry backoff. With the delay tuned, **backoff is
  now the largest controllable cost**: 4m30s of dev's 13m, 5m of test's 17m.

  Across all three environments 23 of 25 throttles cleared on the *first* retry,
  so 30s is sufficient — but nothing in the data shows it is necessary, and
  every throttle pays it in full. The starting wait drops from 30s to 10s,
  still doubling: 10/20/40/80/160 rather than 30/60/120/240/480, and a worst
  case of 310s rather than 930s.

  If throttles keep clearing on the first retry, dev pays 9 x 10s = 1m30s
  instead of 4m30s. If they start needing a second attempt, the ladder absorbs
  it and the saving shrinks rather than disappearing. Tunable via
  `COST_API_BACKOFF`.

  Verified the ladder escalates 10 → 20 → 40 and that the override is honored.

  **Result: a modest win, not the large one hoped for, and the hypothesis was
  half right.** 30s was mostly *necessary*, not merely sufficient. At 10s the
  first wait stopped clearing the throttle: dev 2 of 10, test 7 of 11, prod 1 of
  4, against 23 of 25 at 30s. The throttle window is genuinely longer than 10s.

  The ladder absorbed it as designed — 10 + 20 reaches the same 30s in two steps
  — and the finer first step means the throttles that *can* clear early do,
  which made each throttled call cheaper anyway:

  | | 30s backoff | 10s backoff |
  |---|---|---|
  | dev | 30s per throttle | **26s** |
  | test | 33s | **28s** |
  | prod | 34s | **25s** |

  Kept at 10s: 15-25% cheaper per throttle with no downside observed. But this
  is where delay tuning stops paying. Backoff is now a floor of roughly 4m20s
  for dev out of ~13m, and shortening the wait cannot go below what the API
  actually requires. Reducing it further means making fewer calls that get
  throttled, which is 3.2's territory, not this one.

### 3.2 Investigate a bulk historical query

- [ ] **3.2.1** — The Cost Management query API supports
  `granularity: "Daily"` over a custom time period, which would return per-day
  per-portfolio rows for months at a time in a single call, to be bucketed
  into weeks locally. This would collapse the per-week query loop into a
  handful of chunked calls. **This needs validating against Azure Government
  at subscription scope before any design work commits to it** — write a
  throwaway probe job first and confirm the response shape.
- [ ] **3.2.2** — If 3.2.1 holds, pagination becomes mandatory. There is
  currently no `nextLink` handling anywhere in the codebase, despite the
  README claiming the pipeline warns on paginated responses. Daily granularity
  over 38 weeks × 10 portfolios is far past the 1000-row page limit.
- [ ] **3.2.3** — Chunk the date range (e.g. 60–90 days per call) to keep
  responses within a single page where practical, with pagination as the
  fallback.
- [ ] **3.2.4** — Re-measure and, if the backfill lands comfortably under an
  hour, drop the project job timeout back from 4 hours toward the default.

---

## Phase 4 — Hardening and refinement

Lower-urgency items that make the pipeline less brittle once it is working.

### 4.1 Correctness fragilities

- [x] **4.1.1** — Project result files are written by bash as
  `/tmp/projects/<raw-name>_<period>.json` and read back by the Python
  assembly step as `normalize(name).lower()`. These agree today only because
  `normalize` happens to be a pure case change. If that ever stops being true,
  every project under the affected portfolio silently disappears with no
  error. Pass an explicit key through instead of reconstructing the filename
  on both sides.

  Done, and the failure was closer than the note suggested. The two sides agreed
  only because Cost Management returns tag values lowercased *and* the case map
  only ever changes case. Drop the first of those and it breaks immediately:

  | | |
  |---|---|
  | bash, from `PEO C3N` | `PEO_C3N` |
  | python, `normalize(x).lower()` | `peo_c3n` |

  No file, no error, every project under that portfolio gone from the report.

  Files are now named `p1`, `p2`, … and the query loop writes
  `projects/index.tsv` mapping each key to the tag value **exactly as the API
  returned it**. The assembly step looks the key up rather than recomputing it,
  so there is nothing left for the two sides to disagree about. The parser
  carries `raw_value` alongside the case-corrected name for that lookup, and an
  entry that is somehow missing warns rather than quietly returning no projects.

  Verified against names the old scheme could mangle — `R&D / OPS`,
  `Ünïcode Tëst` — and against mixed-case tag values, which is the case that
  actually breaks it. All resolve correctly.
- [x] **4.1.2** — The bash `safe_name` uses `tr '/  ' '__'`, which maps three
  source characters onto two replacements and works only by accident of how
  `tr` pads. Replace with an explicit substitution. **Closed by 4.1.1** — there
  is no character substitution left, because filenames no longer derive from the
  portfolio name at all.
- [ ] **4.1.3** — Rate-limit detection tests `jq -e '.error.code == "429"'`,
  but `az rest` exits non-zero first, so every failure takes the retry path
  regardless of cause. On the 2026-08-28 run all 95 waits were genuine
  throttling that recovered on the first retry, so this is not currently
  causing harm. The remaining risk is a genuinely non-retryable error burning
  ~15 minutes of escalating backoff before it gives up. Lower priority than
  first assumed.
- [x] **4.1.4** — Review the forecast fallback that allocates a
  subscription-level total proportionally across portfolios. **Confirmed on
  the 2026-08-28 run: the API does not return per-portfolio grouping.** The
  response columns were `Cost, UsageDate, CostStatus, Currency` — no
  `TagValue` — so every per-portfolio forecast figure on the dashboard is
  derived by splitting one subscription total in proportion to current-period
  actuals, not returned by Azure. It is presented identically to measured
  data. Either label it as estimated or drop the per-portfolio breakdown and
  show a single subscription forecast.

  **Chose to label rather than drop.** The proportional split is a reasonable
  estimate and people find it useful; the problem was never the number, it was
  that nothing distinguished it from measured spend. Dropping it would remove
  information to fix a presentation issue.

  The collector now records `basis` in the report — `measured` when Azure
  returned a per-portfolio breakdown, `allocated` when one subscription total
  was split by current-period share. The page reads it and marks every forecast
  heading with `est.`, with a hover explaining why, and an info-panel section
  spelling out how the split is derived and that a portfolio whose spending
  pattern is about to change will be estimated poorly.

  The marker is driven by what the collector observed, not hardcoded, so it
  disappears on its own if Azure ever starts grouping the forecast by tag.
  Verified both bases render correctly: 19 `est.` markers under `allocated`,
  zero under `measured`.
- [x] **4.1.8** — The forecast parser ignores the `CostStatus` column, which
  the API does return and which separates Actual rows from Forecast rows. With
  `includeActualCost: true` in the query, all 31 daily rows are summed into
  one figure, so the "30-Day Forecast" is actually part-actual, part-forecast.
  Small in absolute terms for a window starting today, but the label is wrong.

  The parser now reads `CostStatus` and records the Actual/Forecast split in the
  report, with a third `unclassified` bucket for rows carrying neither — so the
  report never claims a split it did not actually observe.

  **The concern turned out not to apply.** On the real 2026-08-30 run the
  component sentence does not render, which means `actual` and `unclassified`
  are both zero: Azure classified all 31 daily rows as Forecast. The window
  starts today and cost data lags 24-48 hours, so there is no settled actual
  spend inside it. The label was right all along — what changed is that this is
  now measured rather than assumed, and the page will say so on its own if a
  future window ever does contain actual spend.

- [x] **4.1.10** — A report from before 4.1.4 has no `basis` key, and the first
  version of the page treated anything that was not `allocated` as measured —
  rendering "Azure returned the forecast broken down by portfolio, so these
  figures are as reported" over figures whose provenance was entirely unknown.
  That is the same false confidence 4.1.4 exists to remove, reintroduced by a
  fallback branch. Unknown provenance now says it is unknown.

  Verified across four states: allocated, measured, no forecast at all, and a
  report predating the change.

### 4.1b Defects found during the 2026-08-28 run

- [x] **4.1.5** — `update_history.py` miscounted environments: it reported
  `114 environment(s)` on the 38-window run and `9` on the 3-window run, in
  both cases the number of report files rather than the 3 real environments.
  Closed by 2.2.1, which rewrote that loop; the summary now counts distinct
  environments and reads `Wrote 3 weekly snapshot(s) across 3 environment(s)`.
- [x] **4.1.6** — Totals are persisted unrounded, e.g.
  `"total_cost": 1644.3700000000001`. Cosmetic, but it is in the stored data
  and will surface anywhere the raw value is displayed.

  **No re-query needed, and that was worth establishing before doing anything.**
  Every affected value is derived from figures already on the branch: the total
  is the sum of the portfolio costs stored beside it, and `change` is
  `current - prior`. The collector already rounds everything Azure returns, so
  the raw data was never wrong. A `BACKFILL_FORCE` pass would have spent hours
  against a throttled API to recompute arithmetic that runs locally in
  milliseconds.

  Two sources of drift, not one. The sums in `extract_snapshot`, and — the one
  not in the original note — the merge paths in `generate_report.py`, which
  recompute `change` and `change_pct` from scratch when two entries alias to
  the same name. That second one is where the worst of it came from:
  `0.20540369726655483` is a percentage, not a cost.

  Fixed in three parts: `round_costs()` in `history_io.py` as the single rule
  (money to the cent, percentages to a tenth), applied to each snapshot on the
  way in; ten derived-value sites rounded in `generate_report.py`; and
  `normalize_existing()` in `update_history.py` to clean what is already
  stored.

  The migration is a normal pass, not a one-off script. The history is already
  on disk from `fetch_history`, so it costs a few hundred small reads and no API
  calls, and `write_if_changed` makes it self-limiting — it rewrites only the
  shards that actually drift. It converges on the next ordinary KPI Collection
  run and is silent from then on.

  Verified on a 9-shard fixture built from real data: run 1 rewrote 7 (three
  drifted deliberately, four more carrying drift the real data already had, and
  two written fresh by the report path so already clean); run 2 changed
  nothing. No unrounded values left in the shards or in the per-environment
  JSON published beside the page.

  **Confirmed on the real pipeline, 2026-08-30.** 117 history files fetched
  across three environments; **40 of them carried drift** and were rounded —
  about a third of the branch, which is what content-aware writes are for. With
  the week's three snapshots and two tag inventories that made 45 files, and
  `persist_history.py` sent them as **one commit (`5e1c4ace`), 0 new, 45
  updated**. No chunking needed, so none added.

  The published `prod.json` is clean: no value anywhere carries more than two
  decimals, and all 38 weeks reconcile to the cent against the sum of their
  portfolios. That last check matters more than the formatting — rounding a
  total independently of its parts would have made them disagree, and they do
  not.

  One check left, and it is the absence of something: next week's log should
  print no `Rounded ...` line at all. If it does, the pass is not converging and
  is rewriting history every run.
- [x] **4.1.9** — `is_shared_project` matched on `name.lower().strip()` while
  every other tag value is compared on the folded key, so a punctuation variant
  such as `Expedition-0` stopped counting as shared. That silently moves the
  spend out of the shared pool into one portfolio's direct costs *and* changes
  the shared allocation for every other portfolio, since allocation is weighted
  by project count. Found by checking the real tag inventory, which lists the
  live shared project as `GitlabRunners` — that form happened to match either
  way, so the bug was latent rather than active. Now folded in both
  `generate_report.py` and `send_notification.py`; no change for any spelling
  currently in use.
- [x] **4.1.7** — `PORTFOLIO_ALIASES` and `PROJECT_ALIASES` were duplicated
  verbatim across `update_history.py`, `generate_report.py` and
  `send_notification.py`, so a newly discovered tag typo had to be added in
  three places or the three outputs disagreed. **Closed by 2A.1.2** — they now
  live only in `.gitlab/scripts/common/naming.py`, confirmed by grep. This was
  not theoretical: 4.1.9 is an instance of exactly this drift.

### 4.2 Operability

- [ ] **4.2.1** — Emit a machine-readable run summary artifact (windows
  processed, gaps, failures per environment) so pipeline health is visible
  without reading job logs.

  **Cut down to the half that was actually missing.** Most of what this proposed
  is already on the page: collector status from 2A.3.3, never-reported months
  from 4.2.2, pass/fail from the job itself. A whole run was diagnosed from one
  log without wanting for any of it.

  What nothing covers is a week missing from *inside* a covered range. A month
  an environment never reported renders `—`; a month missing one of its five
  weeks just totals low and looks entirely plausible. Same class of problem as
  4.2.2 and the status contract — a number that is wrong without looking wrong.

  `coverage_gaps()` computes it as the anchors absent between an environment's
  first recorded week and the newest week any environment has. Bounded by the
  newest recorded week rather than by today, so a week not yet collected is not
  a hole. The logic already existed in `recorded_weeks()`, which the backfill
  uses for exactly this every run — only the weekly path never asked.

  Reported both ways: a `[WARN]` per environment in the job log, and a banner on
  the page beside the missing-environment one, since a reader looking at an
  understated month has no other cue.

  Verified against contiguous history (silent, logs `History is contiguous for
  every environment`), a hole punched mid-range, and an environment lagging a
  week behind the others — that last one matters, because a collector that fails
  once leaves a hole that persists after the next run moves on.

  Checked against the real branch: prod is contiguous across all 38 weeks — 30
  empty from before the subscription was billing, then 8 consecutive populated
  ones. So this reports clean today, which is the baseline it should have.

  No artifact. Nothing consumes one, and the page and log already reach the
  people who would read it. Left open until a live run confirms the quiet path.
- [x] **4.2.2** — Surface known data gaps in the dashboard. Now concrete
  rather than hypothetical: prod has 29 of its 38 weeks recorded at
  `total_cost: 0` because the subscription did not exist yet. As it stands the
  prod chart will render as a long flat zero line that reads as broken rather
  than as "no data before this date".

  Visible on the 2026-08-30 page: prod's June column read `$0.00` for CPE C2IN
  and Infrastructure and Platforms, asserting a measurement that was never
  taken. The per-environment breakdown now shows `—` for months before an
  environment recorded anything, with a hover and a Page Guide entry saying so.

  **The signal is an empty portfolio list, not a zero total.** A week that
  genuinely cost nothing still records the portfolios it looked at; a week where
  the subscription was not billing has no portfolios at all. That keeps the two
  cases distinguishable, which was the whole point:

  | | |
  |---|---|
  | Prod, June | `—` — never reported |
  | Test, June | `$0.00` — reported and measured at zero |

  Verified with a fixture covering both. The cross-environment monthly columns
  are deliberately unchanged: a month where one environment has no data but
  others do is still a meaningful total.

  **Correct but currently invisible, for a reason worth recording.** The
  2026-08-30 run reports `Environment data begins: Dev 2025-12, Prod 2026-06,
  Test 2026-01`, so the detection works. But the table displays only the last
  three months — Jun, Jul, Aug — and none of those falls before any environment
  started. Prod's `$0.00` for June is therefore a genuine measured zero: prod
  recorded $10.07 under PEO C3N that month, so it was reporting; CPE C2IN and
  Infrastructure and Platforms simply had no spend.

  Prod's six blank months are Dec 2025 to May 2026, and `display_months =
  all_months[-3:]` never shows them. So this fix only becomes visible once the
  Phase 5 decision about how many months to display is made — which is the
  clearest argument yet that the three-month window is hiding something rather
  than just abbreviating.
- [x] **4.2.3** — Consider retention/pruning for the data branch as the shard
  count grows across environments and years.

  **Measured, and it does not arrive. Closing rather than building.**

  The branch holds 117 files today. Growth is three cost shards a week plus at
  most three tag files, and the tag files are written only when values change —
  prod's went untouched this week, so that half is an upper bound.

  | | files | size | commits |
  |---|---|---|---|
  | +1 yr | ~310 | 0.7 MB | ~52 |
  | +5 yr | ~1,560 | 3.5 MB | ~260 |
  | +10 yr | ~3,120 | 6.9 MB | ~520 |

  Nothing degrades at that scale. `fetch_history` pulls one archive regardless
  of file count, so a decade of history is a single 7 MB download;
  `normalize_existing` and `load_history` are a few thousand small reads;
  persist only sends what changed.

  Pruning would trade the one thing the data branch exists for — a complete
  record — against a saving that never materializes. Recorded with the numbers
  so it does not get raised again on intuition.

### 4.3 Documentation

- [x] **4.3.1** — Rewrite the README once the above is settled. Sections that
  currently describe behavior and will be wrong after these changes:
  *How It Works*, *Repository Structure*, *Cost History (Data Branch)* /
  *History File Format*, *Pipeline Configuration*, *Report Output*, and the
  rate-limiting and pagination notes under *Known Considerations*. It also
  still documents `expedition-X-Y` environment naming, `AI2C_API_RWA`, script
  paths missing the `costs/` segment, a template filename that does not exist,
  and says nothing about the forecast or stale-resource features.

  Rewritten, and reorganized around how the pipeline is actually operated:
  what it does, running it (including the backfill), configuration, the data
  branch, the failure model, repository layout, the report artifact, and the
  standing caveats. The failure model and the data branch layout are the two
  sections that did not previously exist anywhere, and are the reason the
  rewrite was worth doing — the resume mechanism and the status contract were
  only recorded in this log, which is a change narrative, not a description of
  the system.

  Every factual claim was checked against the source rather than against this
  log: 20 assertions covering run types, exit codes, job wiring, defaults,
  paths and variable names, plus an existence check on all 12 files named in
  the layout section. All passed.

- [x] **4.3.2** — Add a `CHANGELOG.md` covering everything implemented for the
  1.0.0 tag. Separate from this log by intent: a work log accumulates things
  that were tried, measured and abandoned, and a reader wanting to know what
  the pipeline *does* should not have to sift those out. Entries are grouped by
  theme rather than one per work item — roughly 80 completed items condense to
  about 25 bullets — so the shape of the release is legible without reading it
  as a diff.

  The three documents now divide as: README describes the system as it stands,
  CHANGELOG records what shipped, WORKLOG keeps the reasoning including the
  dead ends.

---

## Phase 5 — Page restructuring

Deferred until the jobs were producing real data. As of the 2026-08-28 full
backfill they are — nine months across three environments.

**Decided: the page is scoped to the Period of Performance.** All of the current
period's months are shown, and a rollover drops the previous period rather than
accumulating. Previous periods stay in the repo and remain reachable; how they
are surfaced is 5.3, still open.

### 5.1 Period boundary

- [x] **5.1.1** — The POP start was derived inline as
  `date(today.year - 1, 12, 1)` in four places, which is right for eleven months
  of the year and wrong for December: on December 1 a new period begins, but
  that expression keeps returning the previous one until January 1. For that
  month every "JWCC POP" total silently covers thirteen months, and then
  December's spend moves from one period to the other overnight — anyone reading
  the figure in December and again in January sees two numbers with no
  explanation.

  Now one helper in `.gitlab/scripts/common/pop.py`, shared by the page and the
  backfill so the range gathered and the range displayed cannot disagree.
- [x] **5.1.2** — `JWCC_POP_START` (YYYY-MM-DD) states the period start
  explicitly instead of inferring it. Inference assumes the period always begins
  on December 1; a contract starting on a different date would shift every total
  with nothing saying so. Set, the anchor rolls forward a year at a time, so it
  can stay fixed at the original contract start. Unset, the corrected December 1
  rule applies.

- [x] **5.1.3** — **A moved boundary rewrote history.** Word came that the JWCC
  POP start changed to 2026-08-30, mid-period. `pop.py` modeled the contract as
  one anchor plus annual rollover — its own docstring said so — and derived
  every earlier period by subtracting whole years. Setting the new anchor would
  have reported a period of 2025-08-30 to 2026-08-29 that never existed, and
  5.3.3 would have **frozen it into the archive**, recoverable only by deleting
  the file from the data branch by hand.

  `JWCC_POP_START` now takes a comma-separated list of boundaries. Each names
  the day a period began; the most recent still rolls forward annually, so the
  list does not need extending every year. A single value behaves exactly as
  before — verified against both the mid-period and post-rollover cases.

  With `2025-12-01,2026-08-30` the previous period reads 2025-12-01 to
  2026-08-29, which is what actually happened: a period cut short after nine
  months. Its archive label is `2025-12_to_2026-08` rather than `2025-2026`,
  because a year-pair label would claim twelve months that did not occur, and
  because two periods in one year would otherwise collide on the filename the
  archive is keyed by.

  Consequence worth knowing: `backfill_windows.py` clamps its default start to
  the period start (2.1.3), so after the change a backfill with no explicit
  range covers only the current period. Earlier weeks need `BACKFILL_START`.

- [x] **5.1.4** — **Two period filters that only agreed by accident.** The
  per-portfolio JWCC figures filter weeks by date (`period_start >= start`); the
  stat-card JWCC Total summed whole monthly buckets (`month >= start month`).
  Identical while the period began on the 1st of a month, which it always had.

  With a period starting August 30 they diverge badly. Measured on 40 weekly
  shards at $100 each: the stat card reported **$4,000** — every week since
  December — where the portfolio tables counted **$100**. Both labeled "JWCC
  POP Total", both on the same page.

  `load_env_pop_totals()` now counts weeks by their own start date, matching the
  filter the portfolio figures already used, and the month-granular comparison
  is gone. After the fix: $4,000 under the old anchor, $100 under the new one.

  This was latent, not introduced. It would have surfaced the first time a
  contract began on any day but the first.

- [x] **5.1.5** — A period beginning mid-month makes its first column cover
  part of a month. Two days of August beside a full September reads as spend
  collapsing. Marked with a dagger and a hover saying when the period began —
  the same treatment as 4.2.2's never-reported months, for the same reason: the
  figure is arithmetically right and says something untrue.

### 5.2 Show the whole period

- [x] **5.2.1** — `display_months` covers every month from the period start to
  the latest with data, replacing `all_months[-3:]`. That default was reasonable
  at a few weeks of history; at nine months it hid six of them, and hid them
  quietly — the header read `Dec 2025 – Aug 2026` while the columns showed Jun
  to Aug, with the single POP total the only trace of the rest.

  Months are generated rather than taken from the history keys, so a month where
  nothing was recorded anywhere still gets a column. Dropping it would hide the
  gap rather than show it.
- [x] **5.2.2** — Absent months in the overview table now read `—` rather than
  `$0.00`, matching what 4.2.2 already did for the per-environment breakdown. A
  month before any environment was reporting has no data at all, and a row of
  zeros across it claims a measurement nobody took.

  **This is what finally made 4.2.2 visible.** With only three months displayed,
  no column reached back before any environment started, so the fix was correct
  and produced no visible change. The full period shows prod's six blank months
  as dashes.

  Confirmed on the 2026-08-30 page, where all three states appear in one table:

  | | Dec 2025 | Jan 2026 | Jun 2026 |
  |---|---|---|---|
  | Dev | `$0.00` | `$0.00` | `$3,482.52` |
  | Test | `—` | `$0.00` | `$3,491.16` |
  | Prod | `—` | `—` | `$0.00` |

  Dev reported from the start and genuinely spent nothing on that portfolio;
  test had not started until January; prod not until June, and then reported a
  real zero for that portfolio while recording $10.07 against another. None of
  that was distinguishable a run ago — every one of those cells read `$0.00`.

  The header and the columns now agree as well: both read `Dec 2025 – Aug 2026`,
  where the header previously claimed nine months over three columns. Totals are
  unchanged — $113,432.86 by environment against $113,432.87 by portfolio, the
  same two independent paths that agreed before.
- [x] **5.2.3** — The trend sparkline is scoped to the displayed period, so it
  and the columns beside it describe the same span rather than diverging after a
  rollover.
- [x] **5.2.4** — Up to twelve monthly columns does not fit most screens. The
  table scrolls horizontally with the toggle and portfolio-name columns pinned.
  Abbreviating figures to fit would trade precision for width, which is the
  worse trade on a page whose whole purpose is the numbers.

  The first attempt was wrong in two ways, both reported from actually scrolling
  it: the pinned name was legible only until it moved, then the scrolling
  columns showed straight through it.

  * `background: inherit` on a `td` resolves to the row, and these rows are
    transparent — so the pinned cell was transparent too. Pinned cells need an
    opaque color of their own, and one for every row state they can be in.
    `:hover` was missing, so a hovered row would have highlighted everywhere
    except the pinned part.
  * Pinning only the name at `left: 0` slid it *on top of* the toggle column
    rather than beside it. Both columns are pinned now, the second offset by the
    first, which is why the toggle column needed a fixed width.

  `border-collapse: collapse` drops borders on sticky cells, so the pinned edge
  is drawn with a box-shadow, which also makes it read as an edge while
  scrolling.

- [x] **5.2.6** — Pin the three summary columns to the right, so scrolling moves
  only the monthly columns. The row's identity stays on one side and its totals
  on the other, which are the two things a monthly figure needs to be read
  against — and the scrollbar then reads as a window over the months rather than
  over the whole table.

  Column order is months, forecast, trend, JWCC, so the offsets accumulate right
  to left: JWCC at 0, trend at 180px, forecast at 276px. Each needs a fixed
  width for the ones outboard of it to be positioned, and the trend header was
  the only one of the three with no class to target.

  **Pinning is on its own classes, not the styling ones.** The forecast and JWCC
  cells switch to `neutral` when the value is zero, so pinning `.forecast-cost`
  would have left exactly those rows scrolling while their neighbours stayed
  put — a layout that looks correct on most rows and broken on the rest. The
  rendered page confirms the mix is real: one portfolio has
  `pin-forecast forecast-cost` while two have `pin-forecast neutral`, and all
  three pin.

  Widths (180/96/130) are estimates and want checking against real content. The
  JWCC cell carries a `(x actual + y shared)` sub-line that may wrap at 180px.

- [x] **5.2.5** — Accordion detail tables scroll badly. **Confirmed in
  practice: a closed table reads well, an open one does not.**

  The cause is structural rather than a missing pin. An open accordion is a
  single `<td colspan>` spanning the entire table, so it scrolls with the parent
  and slides *underneath* the pinned columns on both sides — readable only at
  scroll position zero.

  Pinning its first column, which is what the original note proposed, would not
  have fixed that. The whole block moves, not just its labels.

  The content is now anchored to the left edge of the scroll port
  (`position: sticky; left: 0` on `.detail-content`) so it stays still while the
  months move behind it, with its own horizontal scroll for its own columns. Its
  width has to be stated explicitly, because the cell it lives in is as wide as
  the whole table — `--page-content` tracks the body's content box.

  Confirmed: an open accordion now stays put while the parent scrolls, which
  reads correctly.

- [x] **5.2.7** — The pinning leaked into the accordion tables. Reported from
  the page: the detail table's own *30-Day Forecast* header was frozen at the
  parent's offset instead of sitting at the end of its own row.

  Cause is CSS scoping rather than markup. The detail tables live inside a
  `colspan` cell **of the overview table**, and they reuse the same column
  classes, so a descendant selector reaches them:

  ```
  div.table-scroll > table.overview-table > tbody > tr.detail-row > td
    > div.detail-content > table.detail-table > thead > th.forecast-col
  ```

  `.overview-table th.forecast-col` matches that. Every pinning rule was
  therefore also positioning and resizing the detail tables' columns — pinning
  their forecast header to the parent's `right: 276px` and forcing it to the
  parent's 130px width.

  All 25 pinning rules now use child combinators
  (`.table-scroll > .overview-table > thead > tr > th…`) so they cannot descend
  into a nested table. The one rule deliberately left as a descendant is
  `th.forecast-col { background: #1a6b35 }` — the header color *should* apply
  to both tables.

  Worth remembering when a table is nested inside another: shared class names
  plus descendant selectors means the inner table silently inherits the outer
  one's layout.

- [x] **5.2.8** — **The pinned right-hand columns assumed all three existed.**
  Their offsets were fixed numbers — JWCC at `right: 0`, trend at `180px`,
  forecast at `276px` — each derived by adding the widths of the columns
  supposedly to its right.

  They are not always all there. `show_jwcc`, `show_trend` and `show_forecast`
  are independent, and two combinations occur routinely: an archive has no
  forecast, and a period whose first complete week has not yet arrived has no
  JWCC total. In the second case trend floated 180px short of the edge with
  nothing occupying the space, which is what a new POP looked like.

  Offsets now stack from the right across the columns actually rendered,
  emitted as CSS variables on the scroll container. Verified in all three
  combinations; the all-present case is unchanged.

  The archive had been correct by luck — it lacks the forecast column, and the
  two remaining hardcoded offsets happened to be right for that shape.

### 5.3 Previous periods

**Decided: a separate page per closed period, not a section or a tab.**

The earlier recommendation was collapsed `<details>` sections on the main page.
Rejected once the requirements were stated: the requirement that an archive be
*"a formatted page that could look basically just like the page does"* means
building an archive renderer either way, and once that exists, a section, a tab
and a separate page differ only in how they are linked. A separate page is the
cheapest of the three and the least confusable — the overview table now carries
pinned columns and horizontal scrolling, and period-switching inside it would
interact with all of that to re-render data that never changes.

Two decisions taken deliberately:

- **Hard switch at rollover.** The main page shows only the current period from
  day one, even when that is a single week and looks sparse. A "Previous
  periods" panel is present *always*, not conditionally at rollover, so people
  learn the archive exists before they need it. The failure this guards against
  is not an ugly page, it is someone concluding the data was lost.
- **Freeze the figures, not the pixels.** A closed period's numbers and
  portfolio names are frozen when it is archived; the layout around them is
  rendered fresh each run. That keeps the record a true point-in-time statement
  of what was reported while letting styling fixes reach old pages. Freezing the
  rendered HTML instead would mean generalizing the fetch/persist plumbing —
  `extract_history` filters to `.json` and `files_to_commit` globs the same —
  for the sake of visual immutability nobody asked for.

**Storage is a separate `archive/` root on the data branch**, moved by the
existing fetch/persist with `HISTORY_DIR=archive`, which is the reuse those
modules were written for. Not a subdirectory of `cost_history/`: every history
loader treats each subdirectory there as an environment, so it would render as a
phantom environment named "archive".

`public/` is rebuilt from scratch every run, so nothing survives there by
itself. Archives are stored on the branch and republished each run.

- [ ] **5.3.1** — Make the render period explicit. `pop_start()` is called
  ambiently in four places and always resolves to the period containing today,
  which is right for the live page and impossible for an archive. A module-level
  render period, set once at startup, matches how `ENV_DATA_START` and
  `FORECAST_EST_MARK` already work. Pure refactor — the rendered page must not
  change.

  Done, and verified the way a refactor should be: the rendered page is
  **byte-identical** to the pre-refactor baseline once the generation timestamp
  is excluded.

  Also extracted `build_page()`, since the archive needs the same twelve loaders
  the live page uses and duplicating them would guarantee drift the first time
  one was added. That moved 98 lines out of the `__main__` block — there is no
  `main()` in this file — and needed `global FORECAST_EST_MARK`, which was
  previously assigned at module scope where no declaration was required. The
  render-scoped globals are now cleared at the top of each render, because one
  process renders several pages in a row. Verified byte-identical again after.

- [ ] **5.3.2** — History-only rendering, which is the actual dependency and was
  already flagged here. The page builds its rows from the current week's reports
  and uses history only to fill monthly columns within those rows, so a period
  with no reports has no rows at all — `reports=[]` renders 59 bytes (2A.4.1).

  An archive is exactly that case. The resolution is that a closed period's
  "current week" is its **final** week: synthesize the report set from the last
  week of that period's history, and the existing renderer works unchanged. That
  is also the honest reading of an archive — the period as it stood when it
  closed.

  `reports_from_history()` does that conversion, producing the collector's own
  report shape so nothing downstream needs a history-only branch. Rendering from
  history alone now produces a **56KB page with real rows, stat cards and
  accordions**, against 59 bytes before.

  The forecast column drops out on its own when a report carries no forecast, so
  that needed no special casing. Only the resource panel had to be suppressed.

- [ ] **5.3.3** — Freeze closed periods to `archive/<label>.json`. Written once,
  when a period has closed and no archive exists — deliberately *if absent*
  rather than *at the boundary*, because a once-a-year event should not have a
  single point of failure. A rollover run that fails or is skipped is filled in
  by the next run.

  Consequence worth stating: this is frozen at first write, not at the instant
  of close. If the rollover run fails for weeks and a portfolio alias changed
  meanwhile, the archive captures the later spelling.

  Stored under a separate `archive/` root, moved by the existing fetch and
  persist with `HISTORY_DIR=archive`. Both were already parameterized on that
  variable and needed **no change at all** — `REMOTE_ROOT` derives from it, and
  fetch returns cleanly when the root does not exist, so the first run after
  this ships needs no special casing.

- [ ] **5.3.4** — Render each archived period to `public/archive/<label>.html`
  from its frozen snapshot, every run. Resource tables and the forecast are
  omitted: both describe current state, and a forecast for a closed period is
  meaningless.

  Rendered into `public/archive/<label>/index.html`, a directory per period, so
  the page's relative `data/<env>.json` links resolve without special casing.
  The frozen snapshots are laid out as a history directory and handed to the
  ordinary loaders, so an archive goes through exactly the same code as the live
  page rather than a parallel path that could drift.

  Verified on a fixture straddling two periods: the archive shows only its own
  months with no leakage from the open period, carries the closed-period badge
  and a link back, and omits both the resource panel and the forecast. A second
  run re-renders the page but does not re-freeze the data.

- [ ] **5.3.5** — Surface it. A permanent "Previous periods" panel on the main
  page and a link in the downloads section, plus a link back to the current
  period from each archive page. Each archive page also carries its own
  per-environment JSON downloads, matching the main page.

  The panel renders whenever an archive exists rather than only at rollover, and
  is read from the frozen snapshots rather than the rendered pages — a failed
  render then leaves a broken link, which is visible, instead of a period that
  appears never to have existed, which is not.

  Regression checked: with no archives present the page body is **identical** to
  the pre-5.3 baseline. The only diff is the new CSS, inert until a period
  closes. Before the first rollover the archiver prints `No closed periods yet`
  and writes nothing.

  Left unchecked pending a live run — and note this cannot be fully exercised
  until the first rollover. What a real run confirms now is that nothing
  regressed.

### Still open from earlier observations

- **Prod's early months are structurally zero**, not cheap — the subscription
  did not exist. Now visible as dashes rather than zeros (4.2.2, 5.2.2).
- **Forecast figures are derived, not measured** (4.1.4). With more history on
  the page the forecast column sits next to a lot of real data and reads as
  equally solid. Whatever Phase 5 does with layout should settle how estimated
  values are marked.

---

## Phase 6 — Candidate KPI additions

Ideas for broadening the page beyond cost reporting, in keeping with it being
a general KPI view of the Azure environments. Nothing here is committed —
these are candidates to pick from once Phases 1–3 are done. Roughly ordered by
value-for-effort.

### 6.1 Tag hygiene panel

The pipeline already has everything needed for this and currently throws it
away. Depends on 2.2.5 and 2.2.7.

Portfolio names are dynamic — new ones appear and old ones retire — so a
maintained roster of valid names is not workable. Everything below is derived
from the data itself and needs no list kept up to date.

- **Deterministic normalization, no per-name config** — fold case, trim
  whitespace, and treat `&` and `and` as equivalent. That handles the entire
  formatting-variant class (`infrastructure & platforms` versus
  `infrastructure and platforms`) permanently and with no maintenance,
  leaving only genuine misspellings to be handled by name.
- **Near-duplicate detection** — flag raw tag values that are within a small
  edit distance of another, or that differ only after normalization.
  `ifrastructure & platforms` is one character from `infrastructure &
  platforms` and would be caught automatically. `difflib` covers this with no
  new dependency.
- **Live versus historical** — cross-reference each raw value in the cost
  history against the Resource Graph tag inventory from 2.2.7. A variant that
  no longer appears on any live resource has already been fixed and only
  persists in old cost data, so nobody needs to act on it. A variant still
  present on live resources is an open item. This is the distinction that
  makes the panel actionable rather than a list of historical noise, and it
  answers the awkward part of the problem: an old tag stays in the historical
  cost data forever even after it is corrected in Azure.
- **Spend and lifespan per variant** — cost attributed to each raw value plus
  first-seen and last-seen dates. A variant that stops appearing the same week
  another starts is a rename, and can be labeled as such.
- **Tag compliance rate** — percentage of resources carrying both `portfolio`
  and `project` tags, per environment over time.
- **Untagged spend** — dollars and percentage landing in the `(untagged)`
  bucket, trended weekly.

The 2026-08-28 Cost Analysis run confirms the typo variants are *live*, not
historical: dev's current week still returns both `ifrastructure & platforms`
and `infrastructure & platforms` as distinct tag values on active resources.
These are open items to correct in Azure.

Scale as measured on 2026-08-28: dev queried three separate spellings of one
portfolio (`ifrastructure & platforms` 76 times, `infrastructure & platforms`
46 times, plus the correct form), which is 122 of dev's 260 project queries.

**Correction to an earlier note in this log:** fixing those tags in Azure will
*not* speed up a backfill. Cost Management associates tag values with usage
records at the time the usage is recorded and does not retag historical usage,
so a backfill of past weeks keeps returning the old spellings no matter what
the resources are tagged today. Correcting them helps future weekly runs and
stops the problem growing; it does not shrink the historical query count. It
also means the alias map can never be retired — the typo is in the cost data
permanently, which is the same reason the raw values are worth storing.

Once 2.2.5 is in place the alias map shrinks to confirmed misspellings only —
a handful of lines the panel tells you to add — rather than a document of
portfolio names to maintain. And because aliasing becomes retroactive, adding
an entry re-merges all existing history without re-querying Azure.

### 6.2 Cost efficiency

- **Azure Advisor cost recommendations** — Advisor exposes rightsizing and
  idle-resource recommendations with estimated annual savings attached. A
  single API surface that would considerably expand the existing stale-resource
  panel with dollar figures.
- **Reservation / savings plan coverage** — proportion of eligible spend
  covered by reservations versus paid at on-demand rates, plus utilization of
  what has been purchased. Usually one of the largest single savings levers.
- **Realized savings trend** — whether resources flagged as stale are actually
  being remediated, and the cumulative dollars recovered. Makes the flagging
  feature self-justifying.
- **Non-production shutdown compliance** — dev/test compute running outside
  business hours.

### 6.3 Budget and forecasting

- **Burn-down against the JWCC POP ceiling** — spend to date versus budget,
  with a projected exhaustion date.
- **Forecast accuracy** — last period's forecast against the actual that
  followed. Cheap to compute once history is sharded, and it tells you whether
  the forecast column is worth trusting.
- **Cost anomaly detection** — week-over-week deviation per portfolio and
  project against its own trailing baseline, flagging outliers rather than
  relying on someone eyeballing the delta column.

### 6.4 Expanded resource hygiene

Extending the existing stale-resource scan to other commonly-orphaned types:

- Unattached public IP addresses
- Empty App Service plans
- Orphaned network interfaces and NSGs
- Snapshots older than a retention threshold
- Load balancers with no backend pool members
- Storage accounts with no transactions over the window
- Blobs sitting in hot tier with no recent access

### 6.5 Non-cost KPIs

Broader than cost, but consistent with the page's stated purpose:

- **Defender for Cloud secure score**, trended per environment
- **Azure Policy compliance percentage**
- **Backup coverage** — proportion of eligible resources with a backup policy
- **Resource inventory growth** — resource counts by type and environment over
  time, as a leading indicator of cost growth

---

## Phase 7 — Cluster utilization and spend against usage

The join no other tool can make. Cost lives in Azure Cost Management, utilization
lives in the cluster, and neither knows about the other — so "we are paying for
X and using Y of it" cannot be answered from either side alone. That is the same
reason the cost page exists at all: not new data, but data nobody can see
together.

**Deliberately not on the dashboard yet.** Every job here writes an artifact and
nothing else. The rendered panel is produced as a standalone HTML file that can
be opened from the job artifacts, so it can be judged looking like the real page
without being on it.

The constraint that shapes the design: **putting it on the page later must be a
two-line change.** So the panel is a function returning an HTML fragment, and
the preview is a thin wrapper that puts the fragment inside the page's own
`CSS` constant. Production integration then means importing that function and
adding one placeholder to the template beside `{flagged_panel}` — not rewriting
anything. Anything that would have to be redone at integration time is a
mistake made now.

Related to 6.2, which stays as written: Advisor recommendations and reservation
coverage are a different data source and a different argument.

### What three probe rounds established

Settled, so it does not have to be rediscovered:

| | |
|---|---|
| Cost-to-cluster join | `aks-<pool>-<hash>-vmss` holds in every environment; parse the pool from the VMSS name |
| Cluster access | All nine reachable — ambient kubeconfig for AKS-01, `az aks get-credentials` for the rest |
| Metrics transport | **`kubectl port-forward`.** The API server proxy returns 504 dialing the pod, consistent with ambient mesh intercepting plaintext from outside the mesh |
| Prometheus retention | **7 days confirmed** — weekly averages are real, not sampled |
| Metrics present | cAdvisor, kube-state-metrics, and the `instance:node_cpu_utilisation:rate5m` recording rule |
| Managed Prometheus | Not enabled anywhere |
| Container Insights | Enabled on all nine, per-environment workspaces — query untested, see 7.3.4 |

And the measurements the design is built on:

- **$72k/year total AKS spend, 44% of it on the six clusters nothing runs on.**
- **0.4 CPU per node of UDS Core DaemonSets**, isolated by comparing two
  identical `system` pools where one cluster runs UDS Core and one does not.
- **DaemonSets take 15–30% of allocatable**, which is why an application must be
  measured against *usable* capacity rather than allocatable or it looks wasteful
  for hosting Falco.

### The cost model

Three tiers, because they behave differently and averaging them together would
make the allocation wrong:

| Tier | Examples | Where it runs | Attribution |
|---|---|---|---|
| Per-node agents | Falco, ztunnel, node-exporter, CNI | The application's own nodes | Already attributed — it bills to that pool's VMSS |
| Central services | istiod, gateways, Keycloak, Loki, Prometheus | `udsnp01` / `system` | Allocate across applications |
| Cluster fixed | control plane, `system`, `sysupgrade` | system pools | Allocate, or hold as platform floor |

The consequence worth stating on the page: **more applications makes the central
tier cheaper per application and does not touch the per-node tier.** An app on
five nodes pays five units of Falco however many neighbours it has.

### 7.1 Collector

- [x] **7.1.1** — `cluster_scan`, a matrix job per environment extending
  `.azure_env`, `allow_failure: true`, writing `cluster_reports/<env>.json` and
  a status file from `after_script` so a failure is distinguishable from an
  empty result. Third collector, same shape as the resources one.

  Built as `.gitlab/scripts/cluster/scan_cluster.py` and
  `.gitlab/jobs/cluster/scan.yml`, included under KPI Collection. Status is
  carried inside the report rather than a separate file, matching what the
  resource collector actually does — `ok` / `partial` / `failed` overall, plus a
  status per cluster.

- [x] **7.1.2** — Walk every cluster in the subscription, pairing each with its
  own node resource group and its own kubeconfig. Probe v2 conflated them —
  Azure data from one cluster against Kubernetes data from another — and the
  numbers looked plausible, which is what made it dangerous.

  Credentials are always fetched per cluster rather than trusting the runner's
  ambient context, which points at one cluster and silently made the probe read
  the wrong pair. Cost is gathered *before* the cluster is contacted, so a
  cluster that cannot be reached still reports what it costs — which is the
  case that matters most, since the idle clusters are the expensive finding.

- [x] **7.1.3** — Per cluster, three states, none of which is a failure:
  UDS Core with applications, UDS Core without, no UDS Core at all. Test is
  currently the third and must read as measured, not broken.

  Derived from pool classification rather than asked for directly: a cluster
  with a platform or mixed pool has UDS Core, and whether any pool carries
  applications decides the rest.

- [x] **7.1.4** — Classify pools by the workloads scheduled on them, never by
  name — names get reused, and pool names were explicitly flagged as not stable.
  One app namespace is that application's pool; several is a shared pool needing
  namespace-level allocation; UDS namespaces make it platform; only
  `kube-system` makes it system. **DaemonSet pods do not count as evidence** —
  Falco and ztunnel run everywhere, so counting them made every pool look like
  platform. An earlier version attributed the whole UDS Core pool to a single
  application because of this.

  Verified against a fixture reproducing dev AKS-01: `udsnp01` classified
  `mixed` with apps `[hermes, squidfall]`, `system` classified `system`, and a
  UDS-less cluster reported `no_uds`.

  **Confirmed on the real pipeline, 2026-09-02.** All nine clusters collected,
  status `ok` in every environment, history committed. Dev AKS-01 `mixed` with
  `[hermes, squidfall]`, prod AKS-01 `platform`, the other seven `system`.

  Dev AKS-01 reported `uds_with_apps`, which was **correct** — `hermes` and
  `squidfall` are applications. An earlier reading here had them down as
  first-party platform components, on the mistaken assumption that sharing a
  nodepool implied that. It does not: bundling applications onto a shared pool
  is how workloads that need no cloud resources of their own get into the
  cluster, and says nothing about who built them.

  `PLATFORM_APP_NAMESPACES` survives as an escape hatch for platform namespaces
  the substring hints miss, empty by default. It is not for reclassifying
  applications.

  The consequence for costing: an application may have its own pool, or share
  one with other applications, or sit on the UDS Core pool as these two do. Only
  the first attributes directly; the other two need namespace-level allocation
  within the pool. Both paths are already required, so neither is a special
  case.

- [x] **7.1.8** — **Pods that declare no CPU request at all**, found only once
  real data arrived: 34 in dev AKS-01, 17 in prod, including `neuvector` at 13
  pods, `falco` at 7 and `vector` at 5.

  This undermines every requests-based figure. Such a pod is invisible to the
  scheduler's bin-packing and to allocation by request share: it consumes real
  capacity while claiming none, so "unclaimed capacity" overstates what is
  genuinely spare and an allocation model would charge those namespaces nothing.

  Naively, 87% of pool spend maps to CPU nothing has requested — $64k/year. That
  number must not be published as waste. It is an upper bound built from
  requests, and a large share of what is actually running never appears in it.

  The count now travels with the figures, per namespace and per pool, so the
  panel can qualify them rather than imply a precision they do not have. It is
  also a platform hygiene metric worth showing on its own, and the strongest
  argument for 7.3: usage sees these workloads, requests cannot.

- [x] **7.1.5** — Split container requests four ways: DaemonSet, waypoint or
  gateway, sidecar, application. A sidecar is `istio-proxy` **alongside**
  application containers; a pod whose only container is `istio-proxy` is mesh
  infrastructure. Getting this wrong reported every ambient namespace as a
  failed migration.

  Verified: a waypoint namespace reports `sidecar=0 waypoint=1`, a genuinely
  injected one `sidecar=1 waypoint=0`, and ztunnel lands in the DaemonSet
  bucket rather than either.

- [x] **7.1.6** — Cost per pool from a Cost Management query scoped to the node
  resource group and grouped by `ResourceId`. A different query from the
  tag-based one the cost collector runs — these resources are created by AKS and
  carry no portfolio tag. Capture the OS disks, load balancer and
  `kube-apiserver` rows too; they are real cluster cost.

  The pool parse is `^aks-(.+?)-\d+`, non-greedy so a pool name containing a
  hyphen still resolves, and it folds the OS disks in — their resource names
  repeat the pool prefix, so `aks-udsnp01-…-vmss` at $189.64 and its three OS
  disks at $12.65 each land on `udsnp01` together. Rows that resolve to no pool
  — the internal load balancer, `kube-apiserver`, PVCs — are kept as
  `unattributed` rather than dropped.

  **Every figure is a sum, so all of them are rounded at the source.** The cost
  total drifted to `286.24999999999994` in testing. `round_costs` in
  `history_io` does not cover this schema — it keys on field names, and here
  costs are keyed by pool name and quantities by bucket — so the collector has
  its own pass: CPU to the millicore, memory to whole bytes, everything else to
  the cent. Same defect as 4.1.6, caught before it reached the branch this time.

- [x] **7.1.7** — Persist weekly to `cluster_history/<env>/<monday>.json`
  through the existing fetch/persist with `HISTORY_DIR=cluster_history`. Running
  from the start even while nothing is published, so that by integration there
  is history to draw on rather than a single week.

  Uses the existing fetch/persist unchanged, as `resource_history` does.
  `collected_utc` is excluded from the change comparison so an unchanged week
  is not rewritten every run.

### 7.2 Preview rendering

- [x] **7.2.1** — `render_cluster_panel(data) -> str`, returning a fragment and
  nothing else. No `<html>`, no styles, no file writing. This is the whole
  integration story: production calls the same function.

  Built in `.gitlab/scripts/cluster/render_cluster.py`. Two rules fall out of
  the fragment-only constraint, and breaking either would turn 7.4 back into a
  rewrite:

  * **The panel carries its own scoped `<style>`.** Putting its rules in the
    page's `CSS` constant would make integration a diff in two files, and the
    preview would then need those rules copied to look right.
  * **Nothing here imports `generate_report`,** which will import this. The page
    CSS is imported inside `main()` rather than at module scope, so the
    dependency runs one way only. Verified by importing `generate_report` and
    `render_cluster_panel` together — resolves cleanly.

- [x] **7.2.2** — A preview driver wrapping that fragment in the page's own
  `CSS` constant, already module-level in `generate_report.py`, so the preview
  looks like the page rather than approximating it. Falls back to an unstyled
  preview with a warning rather than failing — a readable unstyled panel beats
  no panel.

- [x] **7.2.3** — `cluster_preview`, needing the collector's artifacts, writing
  `cluster_preview/index.html` as an artifact. Not `public/`, so it cannot reach
  Pages by accident. `needs` is optional and the job is `allow_failure`, matching
  how `kpi_pages` treats its collectors.

- [x] **7.2.4** — What the panel shows without usage data: spend per cluster and
  per pool; capacity, allocatable and usable; requests against usable; unclaimed
  capacity as dollars; the platform/system/application split; and per
  application its direct cost plus its allocated share. Idle clusters as their
  own line.

  **Decided: no single figure for spare capacity.** Requests-derived, that
  number is $64k/year and it would be wrong — 7.1.8 found pods declaring no
  request at all, so the gap between usable and requested overstates what is
  genuinely spare. One confident-looking figure built on that would repeat
  4.1.4's forecast and 4.2.2's structural zeroes: a number that reads as
  measured when it is inferred. The components are shown instead, each sound on
  its own, and the caveat is phrased from what was actually counted rather than
  asserting a count that may not have been taken.

  Rendered against the real collected data: cluster spend $1,460.22/week
  ($75,931/year), 2 of 9 clusters running UDS Core, **52% of spend on clusters
  with no UDS Core**. That figure needs no requests data at all and is the
  soundest thing on the panel.

  The applications table shows `hermes` and `squidfall` on the UDS Core nodepool
  at **0.00 CPU requested each** — 7.1.8 made visible. Allocating that pool by
  request share today would charge two real applications nothing and put their
  whole share on UDS Core.

  **Confirmed on the real pipeline, 2026-09-02.** The panel renders from live
  data and was reviewed as it will look on the page.

  With 7.1.8 collecting, the count came in at **63 pods declaring no CPU
  request** — and the distribution is the finding. Every one of them is on a
  cluster running UDS Core: 38 on dev AKS-01, 25 on prod AKS-01, and **zero on
  all six clusters without it**, which run 37 `kube-system` pods apiece and set
  requests on every one.

  So this is not Kubernetes and it is not the applications. It is UDS Core's own
  components — 63 of roughly 310 pods, a fifth of everything running on the two
  clusters that matter. Which makes it a platform-quality finding in its own
  right, not merely a caveat on the arithmetic: the platform does not specify
  what it needs, so nothing downstream can plan capacity around it or charge for
  it fairly.

### 7.3 Actual usage

- [ ] **7.1.10** — **The collector's window drifted with the day of the run, and
  its history shards were named for the wrong week.**

  Found by reading the first published page rather than by testing: the summary
  strip reported *"not comparable"* because the cluster window was
  2026-08-29 to 2026-09-04 while the cost period was 2026-08-24 to 2026-08-30.
  Correct, and it would have said that on every run except a Monday.

  The cost collector anchors to the most recently completed Monday-to-Sunday
  week, which 2.1.2 established deliberately so a mid-week manual run could not
  produce a skewed window. The cluster collector was written later and derived a
  trailing seven days ending yesterday instead.

  The second half is worse and was not visible on the page at all. The shard was
  keyed to the Monday of the *current* week while holding the previous week's
  spend, so on Monday 2026-08-31 the collector wrote
  `cluster_history/<env>/2026-08-31.json` for exactly the data the cost
  collector files under `2026-08-24`. **The two histories were offset by one
  week in their filenames** — every shard, every environment, since 7.1.7 began
  persisting. Any later join of cluster spend to cost history by week key would
  have been wrong, and nothing would have shown it.

  Both collectors now derive the window the same way. `COST_WINDOW_DAYS` is gone
  with it: the window is a calendar week, and an override could only ever make
  the Prometheus lookback and the actual cost window disagree while the report
  went on claiming they matched.

  Verified the window is identical for a run on any day of the week. Existing
  shards carry the old key and are one week out; they are cheap to leave, since
  nothing joins the two histories yet — but that is the reason to fix it before
  anything does.

- [x] **7.1.9** — **Four defects found by running the collector against a
  second, busier platform** — one cluster per environment, far more nodepools,
  and applications actually deployed. None of them could surface here: this
  environment has two clusters carrying workloads and almost nothing autoscaling.

  **Namespace totals were repeated against every pool a namespace touched.**
  `icarus` split across two nodepools reported four pods on each. The
  Applications table summed to more than existed. The collector now keeps a
  per-namespace-per-pool breakdown and the panel reads that; verified the parts
  sum to the namespace total.

  **A single pod reclassified an entire pool.** `sysupgrade` came back as
  `application (azeiss)` because one pod landed there. AKS marks every pool
  System or User and the collector was already reading it without using it — a
  System pool now stays system or platform, and stray application pods are
  reported as `spillover`, which is the honest description.

  **Node counts and spend describe different periods.** Twenty of twenty-seven
  identically-sized pools showed a full node count against $0.03–$0.13 for the
  week, against $40.62 for one that ran throughout: those nodes scaled up
  shortly before collection. Read together they said 2.83 CPU cost four cents.
  Pools are now compared against the busiest pool of the same VM size — no price
  list, self-calibrating per region and contract — and marked *part window* when
  spend cannot account for the nodes shown. The column is labeled *Nodes now*.

  This is the snapshot-against-integral mismatch flagged before any of this was
  built. Marking it is a stopgap; 7.3's windowed averages are the actual fix.

  **Confirmed live, and the first threshold was too tight.** All four fixes
  render correctly on the busier platform: the per-pool split has `loadout` in
  Test reading 4 + 1 rather than 5 and 5, `sysupgrade` reports
  `system + 1 spilled over` instead of being renamed after the stray pod, and
  `cert-manager` is gone from the application lists.

  But flagging at a tenth of the same-size reference missed three pools, and the
  way it missed them is instructive. `rucksack` at 11% was a near miss. Both GPU
  pools were worse: **every** pool of that VM size had scaled down, so the
  reference was itself a scaled-down figure and two nodes billing about 1% of a
  full week compared favourably against it.

  A relative test cannot see that. The rule is now *below half the same-size
  reference, or under $2 per node-week outright* — half because a pool up for a
  third of the window misleads as much as one up for an hour, and the absolute
  floor because no AKS node runs a week for two dollars. That takes it from 20
  to 23 of 38 pools, catching exactly the three, and the cheapest pool left
  unflagged bills $38.85 per node — unambiguously a full week.

  **`cert-manager` was counted as an application.** Added to the platform hints,
  being universal cluster infrastructure. The rest of what showed up —
  `valkey`, `zookeeper`, `nifi`, `talon` — are per-platform judgements and are
  what `PLATFORM_APP_NAMESPACES` is for.

  Also confirmed there: `udsnp01` hosting **17 application namespaces** in one
  environment. Allocation is the dominant path, not the exception.

- [x] **7.3.1** — Prometheus over `kubectl port-forward`, 7-day averages for
  node and container CPU and memory. Snapshots are not adequate: every pool
  autoscales, some between 0 and 30 nodes, and cost accrues across the week
  while a snapshot describes one moment.

  Built as `.gitlab/scripts/cluster/prom.py`. Three decisions worth keeping:

  * **Every query is evaluated at an explicit end timestamp**, set to where the
    cost window closes. Without it the lookback would end at collection time and
    the two would describe overlapping but different periods — which is the
    whole defect this exists to remove.
  * **Recording-rule and label names are discovered, not assumed.** They differ
    between kube-prometheus-stack versions, so each figure has a list of
    candidate queries and takes the first that returns data. Whether the answer
    came from a real window average or the single-sample fallback is recorded as
    `windowed`, because presenting the second as the first would be a lie the
    page could not detect.
  * **Absence is not failure.** A cluster with no Prometheus returns None and
    still reports requests and cost.

  Average node count per pool is the figure that matters most: it settles the
  part-window question directly, and `_part_window` now consults it and ignores
  the cost inference entirely when it is present. A measurement beats a heuristic.

  Usage is queried by `(namespace, node)` as well as by namespace, so a
  namespace spread across pools is attributed to each. The namespace total
  repeated per pool is the same double-count that made the pod figures wrong,
  and it would matter more here, since usage is what allocation should rest on.
  Verified on a fixture: a namespace split across two pools reports 0.012 and
  0.008 against a namespace total of 0.02.

  **Ran live, and the numbers were impossible — which is how the bug was
  found.** Prod reported `griffin` using **58.77 cores on a four-core node**,
  `amapnp01` 9.87 on 2.73 usable. The queries succeeded, returned plausible
  shapes, and rendered without complaint. Only comparing them against capacity
  showed they could not be true.

  The fault was the order of aggregation:

  ```
  sum by (node) (avg_over_time(rate(...)[7d:10m]))     wrong
  avg_over_time(sum by (node) (rate(...)[5m])[7d:10m]) right
  ```

  Averaging each series first and summing afterwards treats a container that
  lived one hour as though it ran all week — a node hosting many short-lived
  pods sums dozens of alive-time averages as if they were concurrent. Summing at
  each step and averaging the totals is the figure actually wanted. Also added
  `container!="POD"`, without which the pause containers are counted too.

  A guard now warns when a pool's usage exceeds its capacity. That condition
  means the query is wrong, not the cluster, and it would have caught this
  before the figures reached a page.

  Dev's port-forward did not come up and the reason was discarded — the same
  mistake as the first probe round. kubectl's stderr is now kept and reported,
  and the readiness window went from 30s to 90s, dev being much the busiest
  cluster of the three.

  **Confirmed fixed, 2026-09-02.** Every usage figure is now plausible: prod's
  `griffin` reads 0.13 cores against 2.83 usable, where it had read 58.77. The
  capacity guard reported nothing, which is what it is for.

  Across prod and test, on pools with measured usage:

  | | cores | of usable |
  |---|---|---|
  | usable | 101.02 | |
  | requested | 18.98 | 18.8% |
  | actually used | 8.34 | 8.3% |

  The two gaps, finally separable: **82.04 cores nobody asked for** — pools
  sized beyond anything scheduled on them, which the platform team resolves —
  and **10.64 cores reserved and not used**, requests running 2.3× consumption,
  which the teams owning the manifests resolve. Those pools cost $1,029/week.

  With that, holding the single spare-capacity figure back was right: it would
  have been one number spanning two problems with different owners.

  Dev's real error, once stderr was kept: `connection refused inside namespace`
  — kubectl had port-forwarded to a pod with nothing listening. `port-forward
  svc/...` picks a backend itself and will pick one that is not serving, so the
  endpoints are now resolved first and a ready pod targeted directly. A service
  with no ready endpoint reports that as the reason rather than timing out,
  which distinguishes "Prometheus is not serving here" from "the forward broke".

  **Confirmed, and the answer was the cluster.** Dev now reports
  `1 endpoint(s) present but none ready — usage not collected`: its Prometheus
  pod is not serving. The collector carries on and publishes cost, capacity and
  requests for all 16 pools, with usage shown as absent rather than zero.

  Prod and test measured every pool, 22 of 22. Dev measured none, and says so.
  That is the status contract working across a third domain.

- [x] **7.3.2** — The two gaps, kept apart because they have different owners.
  *Usable − requests* is unclaimed capacity, which the platform team fixes by
  scaling pools. *Requests − usage* is over-requesting, which application teams
  fix. The probe measured CPU requests at 3–9× actual while memory requests were
  accurate, so this is a CPU-specific conversation.

  Both are computed and stored per pool — `unclaimed` and `over_requested` — and
  the panel shows usable, requested and used side by side so each gap is visible
  without either being reduced to a headline. Consistent with the decision in
  7.2.4: components, not conclusions.

- [x] **7.3.3** — Allocate by requests, showing usage beside it. Allocating by
  usage lets an over-requester off the hook and hides the thing worth seeing;
  requests charge a team for capacity it denied to others, which is defensible.

  **Built, and the basis had to change.** The original note said allocate by
  requests, because a reservation denies capacity to everyone else and that is
  the more expensive fact. That reasoning holds, but 7.1.8 broke the rule as
  written: a namespace declaring no request would be allocated nothing while
  consuming real capacity.

  The footprint is therefore **the greater of what a namespace reserved and what
  it used**, per pool. Neither direction can be argued away — a team that
  reserves four times what it uses pays for the reservation, and a team that
  reserves nothing pays for its consumption. Demonstrated on a fixture where
  `wordle` reserves nothing and uses one core:

  | basis | wordle | talon |
  |---|---|---|
  | requests only | $0.00 | $80.00 |
  | max(reserved, used) | $60.00 | $60.00 |

  Under the original rule `talon` subsidizes `wordle` entirely.

  DaemonSet requests are excluded from the denominator, so per-node agents are
  spread across whoever occupies the pool rather than billed as a tenant — which
  is the tier-one treatment stated at the top of this phase.

  Two steps: each pool's cost splits across its namespaces by footprint, then
  everything landing on platform and system namespaces, plus cluster-wide costs
  belonging to no pool, divides **equally among the applications in that
  cluster**. Equal and per-cluster, matching `compute_shared_allocation` on the
  cost page rather than inventing a second convention. Cost that lands on a pool
  where nothing reserved or used anything measurable is reported as
  unallocatable instead of being spread.

  Verified conservative: allocated totals sum to the cluster's cost exactly.

  Derived at render time, not stored. The model will be argued with, and it
  should be possible to change it without recollecting — the same reasoning as
  canonicalizing tag values at render rather than at write.

  The basis is stated per row, and an application spanning clusters measured in
  some but not others reads `1 of 2 measured` rather than claiming either.

  **Confirmed live, 2026-09-04.** Twenty-three applications across three
  clusters, allocating to **$1,789.30 against a cluster total of $1,789.29** — a cent
  of rounding, nothing unattributed.

  The shape it reveals:

  | | $/week | share |
  |---|---|---|
  | occupied by applications | 208.74 | 11.7% |
  | platform | 1,580.55 | 88.3% |

  Applications occupy about an eighth of what the clusters cost. That rose from
  73% to 88% when 7.3.5 moved `valkey`, `nifi`, `nifikop` and `zookeeper` into
  the services — they stopped drawing a platform share and their occupied cost
  joined the pot, which is the correct treatment and moved the headline
  substantially. Worth remembering when the figure is quoted: it depends on
  where the service/application line is drawn, and that line is a judgement
  recorded in `PLATFORM_NS_HINTS`.

- [x] **7.3.5** — **Classify by service, not by application.** `valkey`, `nifi`,
  `nifikop` and `zookeeper` were being costed as applications; they ship in the
  UDS bundle. They are now in `PLATFORM_NS_HINTS`, and the list is documented as
  what it is: the services, not the applications.

  That direction matters. Services are few, predictable and change rarely; the
  application roster turns over constantly and would need maintaining by whoever
  deploys. Anything unnamed is an application by default, which is the safer way
  to be wrong — a new application is costed immediately rather than quietly
  absorbed into platform overhead.

  Effect on the live figures: 26 applications become 21, roughly $227/week of
  occupied cost moves into the platform pot, and every remaining application's
  platform share rises about a quarter. `valkey` had been the most expensive
  application on the page at $10,956/year.

- [x] **7.3.6** — **Placement, as a KPI in its own right.** Two rules, both from
  how the platform is meant to be laid out: a service belongs on the platform
  nodepool, and an application belongs on its own or on a shared application
  pool. `talon` on `udsnp01` is the case that prompted it.

  Both directions distort the costing, in opposite ways. An application on the
  platform pool takes capacity that is then billed to the platform; a service
  elsewhere is billed to whichever application owns that pool. Neither is
  visible from the cost figures themselves, which is exactly why it belongs on
  the page.

  Detected from data already collected — pools record which hosted namespaces
  were services and which were Kubernetes' own, so the rules are applied without
  being re-derived. Verified against a fixture carrying all three cases: an
  application on the platform pool, an application spilled onto a System pool,
  and a service off the platform pool.

  Reported, never corrected. Where a workload runs is a deployment decision; the
  page's job is to make a wrong one visible.

  **First live run flagged correct placements as wrong.** `aristotle` on the
  `aristotle` nodepool was reported as sitting on the platform pool. The rule
  keyed on the `mixed` classification, which only means a pool hosts both
  services and applications — equally true of the platform pool with
  applications stranded on it and of an application's own pool with one service
  stranded on it.

  The platform pool is now identified as the one where the services
  predominantly live, needing at least two to qualify, so a single stray service
  cannot promote a pool. Applications are judged against that pool and against
  System-mode pools; services against everything else.

  The stray service is then reported where it belongs — on the fixture,
  `monitoring` on the `aristotle` pool — which is the finding that was hidden
  behind the false positive.

  **Confirmed live, and what was hidden is the more interesting half.** The
  false positives are gone; six services now report as sitting on application
  nodepools in prod:

  ```
  loki                  on aristotle       zarf     on aristotle
  authservice           on atlas           keycloak on mentat
  istio-tenant-gateway  on atlas           loki     on mentat
  ```

  DaemonSets are excluded upstream, so these are ordinary Deployments landing
  wherever the scheduler had room — UDS Core components that do not pin
  themselves to the platform nodepool. Their cost is attributed correctly
  (a service on an application pool adds to the platform pot, not to the
  application), but the *capacity* is consumed on an application's nodes, which
  is invisible from the cost figures alone. That is the case this check was
  built for.

  Twenty-three applications are also flagged, almost all on `udsnp01`.

- [x] **7.3.7** — **A seven-day query answered by two days of data.**
  Investigating why dev had no usage found its Prometheus in CrashLoopBackOff
  with `write /prometheus/queries.active: no space left on device` — the TSDB
  volume full, dying before it could even allocate a 20KB query log, restart
  count 1350.

  The cluster-side cause is that no retention is configured at all: the flags
  carry `--storage.tsdb.path` and `--storage.tsdb.wal-compression` and neither
  `--storage.tsdb.retention.time` nor `--storage.tsdb.retention.size`, so
  nothing capped the database against the volume. Dev fills first because it
  runs 20 nodes to prod's 15.

  The part that belongs here: `avg_over_time(x[7d])` averages whatever points
  exist and ignores the gaps, so a server holding two days of history answers a
  seven-day query without complaint — a two-day average wearing a seven-day
  label. Once dev is repaired it will be short of the window for a week, which
  makes this the normal case rather than an edge one.

  `available_days()` now establishes how far back data really goes before
  averaging, the report carries `window_days_covered`, and a cluster whose usage
  spans less than the cost window says so beneath its row. `windowed` is only
  true when the coverage actually reaches the window.

  Same failure this project keeps finding: a figure that is arithmetically
  correct and describes a different thing from the one it is labeled with.

  **And the alignment turned out to be too strict.** With dev's Prometheus
  repaired and scraping, the next run still reported no usage at all: queries
  anchor to where the cost window closes, 2026-09-03, and the rebuilt TSDB
  begins 2026-09-04. Every query landed before the data existed and came back
  empty, so a healthy cluster read as unmeasured — and would have for days, and
  again after any future outage.

  When nothing exists at the window's end, usage is now measured to now instead
  and the cluster row says so. The figures are real; they describe a later
  period than the spend beside them, which is worth stating rather than
  withholding. Alignment remains the default whenever there is data to align to.

  **The first attempt at that fix only worked for one query in five.** The
  fallback computed the new anchor and logged it, and dev still reported nothing
  — because four of the five metric queries were left evaluating at `end`. The
  edit that switched them had matched one call site and silently missed the rest.

  Both call sites read `end`, and that was the whole problem: `end` meant the
  close of the cost window in one place and the moment a query is evaluated in
  another. Identical while they agree, and they only diverge when the fallback
  fires — exactly when it matters. The evaluation parameter is now `at`, so the
  two cannot be confused again.

  And it failed silently, which is why it took a run to find. `collect()`
  returned `None` with no indication of which query came back empty — the same
  mistake as discarding kubectl's stderr, made a third time. Every attempt now
  reports its series count or the query that returned nothing, and a Prometheus
  that answers everything with no data says so rather than looking absent.

  Verified against a simulated server holding 20 hours of data with a cost
  window closing a day earlier: all six metric queries evaluate at the fallback
  anchor, none before the data begins.

  **Confirmed live: all 38 pools measured across the three clusters**, dev
  included. But neither warning appeared, and both were suppressed by separate
  faults:

  * The collector copied usage metadata through an **allowlist** of field names.
    `window_days_covered` and `aligned_to_cost_window` were added to `prom.py`
    and not to that list, so they were computed, logged, and dropped before
    reaching the report. The panel could not have drawn either notice. It now
    excludes the bulk series maps instead, so new metadata passes through by
    default — an allowlist that has to be updated in a second file is the
    failure, not the omission.
  * The short-history test read `u.get("window_days_covered")` for truth, so
    **zero days of coverage suppressed its own warning** — the most severe case
    being the one that stayed silent. Now tested with `is not None`, and phrased
    "less than a day" rather than "0 day(s)".

  Verified across all three coverage cases: full window silent, three days
  showing short history, zero days showing both.

  **Confirmed live, 2026-09-04.** All 38 pools measured across the three
  clusters, and dev carries both notices — *later window* and *short history,
  less than a day* — which is exactly its state: a TSDB rebuilt the previous
  evening against a cost window that closed before it existed. Both clear on
  their own once dev accumulates seven days.

- [x] **7.3.4** — Evaluate Container Insights as an alternative. Enabled on all
  nine clusters where Prometheus exists on two, queryable through the service
  principal with no cluster access, and likely longer retention — which would
  remove kubeconfig handling and port-forwarding from the collector entirely.
  The probe's query failed with its error truncated by an unrelated Azure CLI
  warning, so the cause is unknown. Worth one look because the simplification is
  large, but not a dependency: Prometheus covers the clusters that matter.

  **This is an evaluation, and the deliverable is a decision with evidence.**
  Writing a second backend before knowing whether the data supports one would be
  the expensive way to find out it does not.

  Four questions, in the order that decides it:

  | | |
  |---|---|
  | 1 | is the addon enabled, and against which workspace |
  | 2 | does the query path work at all |
  | 3 | how far back does the data go, against Prometheus's ten days |
  | 4 | does it carry **both** usage and requests, and can rows be mapped to nodepool and namespace |

  Four is the one that settles it. Usage alone replaces only half of what the
  collector reads — the rest comes from the Kubernetes API — and without the
  mapping none of it is attributable. If Container Insights covers all of it,
  the collector loses kubeconfig handling, port-forwarding and the endpoint
  dance entirely and works on clusters with no monitoring stack. If it covers
  only usage, it buys nothing Prometheus is not already giving us.

  Built as `.gitlab/scripts/probe/container_insights.sh` under the existing
  `Cluster Probe` run type, so it retires with the rest of the probe.

  **The earlier failure has a likely explanation.** `az monitor log-analytics
  query` lives in an extension that may not be on the runner image, and the
  probe never installed it. The error was then hidden behind an unrelated Azure
  CLI `SyntaxWarning` that the `head -2` consumed — the third time in this phase
  that a diagnostic was lost to truncation. The extension is now installed
  explicitly and CLI warnings are stripped before whatever is left is reported.

  **First run: the extension was indeed missing, and the filter hid the error
  again.** All three environments now install it, resolve the workspace, and
  fail the first query with a bare `ERROR:` and nothing after it — because the
  filter that stripped CLI warnings also stripped the blank line the message
  body followed. Twice hidden by two different filters, which is the argument
  for not filtering diagnostics at all. It now prints twenty raw lines of both
  stderr and stdout, since `az` puts some failures on the latter and that file
  was being captured and never read.

  **The leading hypothesis is a permissions gap, and it is now checked rather
  than inferred.** The workspace *lookup* succeeded and the *query* did not,
  which is exactly the split between the two planes:

  | | needs | granted by Reader |
  |---|---|---|
  | `workspace show` | `Microsoft.OperationalInsights/workspaces/read` | yes, via `*/read` |
  | `log-analytics query` | `…/workspaces/query/read` — a **dataAction** | **no** |

  Reader's `*/read` wildcard covers control-plane actions only, so a principal
  can see a workspace it cannot query. `Log Analytics Reader` carries the data
  action. The probe now lists the role assignments the service principal holds
  on the workspace, so the next run answers this outright.

  If that is the cause, it is not fatal but it is a real cost: a role assignment
  per environment, on a resource outside the subscriptions this pipeline
  otherwise touches. Worth weighing against Prometheus already working.

  **Wrong. The principal holds Owner.** The role listing came back
  `Owner`, `Cost Management Contributor` and `Reader`, all at subscription
  scope and reaching the workspace by inheritance — so the workspace is in that
  subscription and the principal has full control-plane rights on it. There is
  no authorization gap to find.

  And with filtering removed entirely, stderr is *still* only `ERROR:` with no
  body, and stdout is empty. So `az` is producing an empty error, which points
  at the tool rather than at the request.

  The remaining suspect is the endpoint. Log Analytics has a data plane separate
  from ARM, and a different one again in Azure Government —
  `api.loganalytics.us`, not `.io` — and the extension has historically
  defaulted to the commercial host regardless of the configured cloud.

  So the probe now falls back to `az rest` against the Government data-plane
  endpoint directly, reshaping the columns/rows response into the same row
  objects the extension returns. That machinery is what the cost collector
  already uses against Cost Management, so it is known to work here. Either it
  succeeds — and the extension is simply broken in Government, which the log
  will say — or it fails with the service's own HTTP error, which is the thing
  three rounds of diagnostics have failed to obtain.

  **It succeeded. `az monitor log-analytics query` does not work in Azure
  Government; `az rest` against `api.loganalytics.us` does.** Worth recording
  for anyone who tries this again: the extension reports a bare `ERROR:` with no
  body, which is indistinguishable from a dozen other faults.

  The working call, kept here because the probe that found it has since been
  removed and this took three rounds to arrive at. The data plane is a separate
  endpoint from ARM, and a different one again in Government — `.us`, not `.io`:

  ```bash
  LA_ENDPOINT="https://api.loganalytics.us"
  encoded=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$QUERY")
  az rest --method get --resource "$LA_ENDPOINT" \
          --uri "${LA_ENDPOINT}/v1/workspaces/${WORKSPACE_GUID}/query?query=${encoded}" \
          -o json
  ```

  The response is columns/rows rather than row objects, so it needs reshaping —
  the same two shapes the cost collector's parser already handles:

  ```python
  tables = raw.get("tables") or []
  cols = [c["name"] for c in tables[0]["columns"]] if tables else []
  rows = [dict(zip(cols, r)) for r in tables[0].get("rows", [])] if tables else []
  ```

  `WORKSPACE_GUID` is the `customerId` from
  `az monitor log-analytics workspace show`, which works through the extension
  because it is a control-plane call. Only the query is data-plane.

  ### Decision: evaluated and declined

  Every question got an answer, and the fourth one settles it against.

  | | |
  |---|---|
  | Enabled | yes, all three, own workspace each |
  | Query path | works, but only via `az rest` |
  | Retention | **520–589 days**, against Prometheus's 10 |
  | Usage and requests | **usage stopped 7 months ago** |

  The retention is extraordinary — a year and a half of history against ten
  days, and billions of rows. Which is exactly what makes the last row decisive
  rather than a quibble: the `Perf` table's `K8SContainer` data **stops** at
  2026-01-26 in dev, 2026-02-04 in prod, 2026-01-26 in test. The newest usage
  metric in any environment is 221 days old.

  That is why the counter list came back empty. The query filtered to `ago(1d)`
  and there has been nothing to find since January.

  The inventory tables are current — `KubePodInventory` and `KubeNodeInventory`
  both answered `ago(1d)` queries, and node labels carry `agentpool`, so the
  pool mapping this needed does exist. But inventory is the half we already get
  from the Kubernetes API, and we need `kubectl` regardless for the pod specs
  that drive DaemonSet and sidecar classification. Replacing only that half
  removes nothing.

  So Container Insights cannot replace Prometheus, because the one thing it was
  wanted for is the one thing it stopped recording. **Prometheus stays.**

  **A finding for the platform, separate from this decision.** Container
  Insights has not recorded container metrics in any environment since January
  or February. The inventory tables still populate, so nothing looks obviously
  broken — anything relying on `Perf` for dashboards or alerting has been
  silently stale for seven months. The likeliest cause is a data collection rule
  change: newer Container Insights versions route metrics to Azure Monitor
  managed Prometheus rather than the `Perf` table, and the earlier probe found
  managed Prometheus **not enabled** on any cluster. If that is what happened,
  the metrics are not going anywhere at all.

- [x] **7.3.8** — **Does the cost page miss the Kubernetes clusters? No. Measured
  and the premise was wrong.**

  The reasoning was: `parse_tagvalue_response` takes `default_label=None` for
  portfolio queries and discards rows with an empty tag value, and 7.1.6 had
  recorded that AKS node resource groups "carry no portfolio tag". If both were
  true the whole cluster footprint would be missing from the page's totals, and the
  JWCC POP figure would be tagged spend wearing the label of spend.

  Measured over one week with three queries that close the arithmetic — the
  subscription ungrouped, grouped by portfolio TagKey, and grouped by resource
  group:

  | | total | untagged | AKS node RG |
  |---|---|---|---|
  | dev | 3,088.77 | 127.14 — 4.1% | 569.72 — 18.4% |
  | prod | 2,718.46 | 92.68 — 3.4% | 789.77 — 29.1% |
  | test | 1,754.36 | 49.94 — 2.8% | 510.24 — 29.1% |
  | **all** | **7,561.59** | **269.76 — 3.6%** | **1,869.73 — 24.7%** |

  **The node resource groups are inside the tagged total.** `caz-caravan-*-e-
  shared-aks-01` appears in the resource group breakdown and the tagged sum
  reconciles to the subscription total, so the portfolio tag is reaching those
  resources — whether by AKS propagation or by how they were created. 7.1.6's
  claim was inferred from the *cost* query returning no portfolio for them,
  which is a different thing from their being untagged, and it was wrong.

  So the cluster spend is on the cost page already, inside some portfolio's
  figure. **A quarter of the bill is Kubernetes**, and the cost page shows it
  without saying which quarter.

  Untagged spend is real but small: **$269.76/week, 3.6%**. The page drops it
  silently. Worth surfacing as `(untagged)` rather than discarding — the same
  treatment untagged *projects* already get inside a portfolio — but it is a
  rounding-error correction, not the structural hole this item assumed.

  **The probe miscounted it, in a way worth recording.** Azure returns untagged
  rows with a JSON `null` TagValue. `str(None)` is `"None"`, which is not
  `"null"`, so the probe's own filter counted them as tagged and reported
  untagged spend as exactly zero — three times, consistently, which is what made
  it look trustworthy. The collector's parser skips falsy tag values and drops
  the same rows correctly. Two components disagreeing about what "untagged"
  looks like, in a check written to measure untagged spend.

  ### What this changes for 7.4

  The two pages describe **the same money at different granularities**, not
  complementary halves. The cost page says a portfolio spent $2,246 this period;
  the cluster page says what the Kubernetes part of that is and how much of it
  is used. That is a drill-down, and the relationship the layout should express
  is nesting.

### 7.4 Putting it on the page

**Decided: two pages, nested.** 7.3.8 settled the relationship — the cluster
data is not a complementary half of the cost data, it is the same money at a
finer granularity. A quarter of the bill is Kubernetes and it is already inside
the portfolio figures; what the cost page cannot say is *which* quarter, or
whether it is being used.

So the layout expresses nesting rather than adjacency: `index.html` keeps the
cost tables and carries a short summary of the clusters with a link across;
`cluster.html` carries the full panel and a link back. That answers the congested
one-page problem without building tabs — the tables that made the page feel heavy
stay where they are, and the second subject gets its own room.

- [ ] **7.4.1** — Import `render_cluster_panel` in `generate_report.py` and add
  the placeholder beside `{flagged_panel}`.

  **It was two lines, as intended, and then the interesting work was elsewhere.**
  The import and the `{cluster_strip}` placeholder are exactly what 7.2 planned
  for. What took the time was making the two pages agree with each other.

  Three things were built beyond the import:

  * **`cluster_summary()`**, so the strip and the panel read one dict rather
    than deriving the same figures twice. Two places computing "what Kubernetes
    costs" is the defect this log keeps recording; one function is the cheapest
    way not to repeat it.
  * **`render_cluster_page()`**, used by both the published page and the preview
    artifact, so reviewing the panel and publishing it exercise the same code.
    The page CSS is passed in as a parameter, which is what keeps the dependency
    running one way — `render_cluster.py` still never imports `generate_report`.
    Confirmed by importing it alone and checking `generate_report` is absent
    from `sys.modules`.
  * **`cluster_comparison()`**, which is the whole reason the headline number is
    trustworthy. See below.

  **The share of the bill is the number that had to be got right.** The two
  collectors do not read the same period by construction: the cost collector
  reads an ISO Monday-to-Sunday week, the cluster collector a trailing seven
  days ending yesterday. Those are the same window on the Monday schedule and
  different on every other day. Dividing one by the other regardless would print
  a confident percentage spanning two different weeks — the same class of defect
  as a monthly total quietly missing one of its weeks.

  A share is now stated only when both sides describe the same days *and* the
  same environments, scoped to the intersection when they differ, and the strip
  says plainly why it is withholding one otherwise.

  **A defect found while wiring it, and it predates this item.** The panel's
  totals excluded any cluster whose status was not `ok`. But 7.1.2 gathers a
  cluster's cost *before* contacting it, precisely so an unreachable cluster
  still reports what it costs — so the panel was dropping known spend, and it
  would drop it for exactly the clusters most likely to be neglected. On the
  test fixture that was $150 of $1,460, an 11% understatement of Kubernetes
  spend.

  Cost and reachability are now separated: every cluster's spend is counted in
  the headline, `unreachable_weekly` is held apart, and every utilization-derived
  share divides by what was actually measured rather than by the total. The
  clusters table shows an unreachable cluster's spend beside its error instead
  of leaving the cell blank, which read as costing nothing.

  Verified against a fixture of four clusters across three environments — one
  unreachable, one with no UDS Core, one with applications, one platform-only:

  | Case | Result |
  |---|---|
  | Windows aligned | `Kubernetes is 22% of the spend reported on this page` |
  | Windows differ | share withheld, strip states both windows and why |
  | Cluster report for dev only | `Across Dev, where both were collected … 23%` |
  | Every cluster unreachable | no all-clear; spend stated, no utilization figures |
  | No cluster reports at all | no strip, no nav link, no `cluster.html` |

  And the regression that matters most: with the cluster collector absent, the
  cost page is **byte-identical** to the one built before this change, apart
  from the deliberate spelling pass. The placeholder is spaced by the caller so
  an empty panel leaves no trace.

  `kpi_pages` now needs `cluster_scan` with `artifacts: true, optional: true`,
  matching how it treats the resource collector: absent artifacts cost the
  cluster page, never the cost page.

- [ ] **7.4.2** — Retire `cluster_preview`, or keep it as the way to review
  changes to the panel without publishing them.

  **Kept.** Retiring it was the other option and would have been reasonable when
  the preview was a second rendering path that could drift. It is not any more:
  it calls `render_cluster_page()`, the same function the published page uses,
  and differs only in having no back-link and writing to an artifact rather than
  to `public/`. So it costs nothing to keep and buys the ability to review a
  change to the panel from a branch that publishes nothing.

- [ ] **7.4.3** — Decide what belongs in the executive summary. At most five
  numbers, each with a direction and a trend.

  **Five, and no trends yet — deliberately.**

  | | Why it is here |
  |---|---|
  | Kubernetes spend, and its share of reported spend | The size of the drill-down. Answers "how much of this page is the clusters?" |
  | Occupied by applications, as $ and % | The headline finding: applications occupy about an eighth of what the clusters cost |
  | Clusters running UDS Core, *n* of *m* | The idle-cluster finding, needing no requests data at all |
  | Pods with no CPU request | A platform-quality metric, and the caveat on every requests-derived figure |
  | Workloads on the wrong nodepool | Only when non-zero. The one row that is directly actionable |

  **Unclaimed capacity in dollars was the original candidate and is still not
  shown.** 7.2.4 and 7.3.2 settled that: requests-derived, it is $64k/year, and
  7.1.8 showed the requests are incomplete. It also spans two problems with
  different owners — capacity nobody asked for, which the platform team resolves,
  and capacity reserved and unused, which application teams resolve. One number
  covering both would be a number nobody can act on. The components are on the
  cluster page instead, each sound on its own.

  **No trend arrows.** `cluster_history/` has been collecting since 7.1.7 but
  holds a couple of weeks. A direction drawn from two points is noise wearing the
  authority of a trend, which is the thing this page keeps being rebuilt to
  avoid. Worth adding once there are enough weeks to mean something.

- [ ] **7.4.4** — **Main page order.** Reported from reading the published page:
  title, Important Information, Previous periods, Kubernetes clusters, stat
  cards, portfolio overview, flagged resources.

  Two things were in the wrong place. The archive index sat above the page's
  actual content and would grow every year, and the cluster summary came before
  the cost figures it is a drill-down *of* — which inverts the relationship 7.3.8
  established.

  Now: Important Information, portfolio overview, Kubernetes clusters, flagged
  resources, previous periods. The clusters section follows the money it
  subdivides, and the archive sits at the foot of the page.

  Previous periods keeps 5.3.5's guarantee of being permanently visible — the
  failure guarded against is someone concluding a year of data was lost — but
  shows only the three most recent, with the rest behind an expander. A period
  is a year, so that stays quiet for three years and then grows behind a
  disclosure rather than into the page. It was also the one dark-themed block on
  a light document, which made an archive index read like an alert; it is now
  themed like everything else.

- [ ] **7.4.5** — **The cluster page was a data dump, and this is the honest
  assessment of it.**

  Measured on the first published version: **151 rows across five tables, 10 to
  12 columns each**, no sorting, no filtering, four caveat paragraphs scattered
  between the tables, and a duplicated page title.

  | Section | Rows | Who acts on it |
  |---|---|---|
  | Clusters | 4 | nobody on this estate — every cluster runs UDS Core |
  | Nodepools | 38 | platform team |
  | Applications | 57 | **mostly restates the other two** |
  | Placement | 29 | platform team |
  | Application cost | 23 | **leadership — and it was last** |

  The fault was not density. It was that the page was **ordered by how the data
  was collected rather than by what anyone needs to decide**, with three
  platform-team tables and one leadership table interleaved and nothing
  separating them. A reader could not tell what question any section answered.

  Rebuilt as four numbered sections, each opening with a sentence stating the
  decision it supports:

  1. **What Kubernetes costs** — tiles and the clusters table
  2. **What each application costs** — the leadership view, moved up
  3. **Where capacity is going unused** — the platform view
  4. **What is on the wrong nodepool** — the actionable one

  Changes that fall out of that ordering:

  * **The 57-row Applications table is gone**, folded into section 2 as an
    expandable row per application. Its unique content was "where does this
    application run", which is detail *about* a costed application rather than
    a subject of its own.
  * **Placement is grouped by what is wrong** rather than listed per workload.
    Twenty-nine rows of "application on the platform nodepool" is a wall; two
    grouped rows with counts and an expander is a finding.
  * **The two capacity gaps are their own columns**, tinted apart and labelled
    with who resolves each — `unclaimed` to the platform team, `over-requested`
    to the application teams. 7.3.2 established they have different owners; the
    table now says so structurally instead of in a note underneath.
  * **Every table sorts**, and an environment/cluster filter hides rows across
    all four at once. Filtering and expansion are deliberately separate
    mechanisms — `[hidden]` for collapse, a class for filtering — so filtering
    cannot silently expand a row and expanding cannot defeat the filter.
  * **A "How to read this page" panel**, collapsed, in the pattern of the cost
    page's Important Information. It absorbs the four scattered notes, which
    were being read after the figures they qualify, if at all. It states what
    the page is for, the three-tier cost model, what occupied and platform share
    mean, what the two gaps are, and — in its own section — what these figures
    *cannot* tell you.

  Two smaller faults fixed on the way: the page rendered its title twice, and
  *Spend without UDS Core* showed `$0.00 / 0%` on an estate where every cluster
  runs it. That tile now appears only when there is idle spend to report, and is
  replaced by platform share per application otherwise — the figure that falls
  as more applications onboard.

  `_short()` also rendered every cluster as `01`, which distinguishes nothing
  when each environment has one cluster. It keeps the segment before a trailing
  number now, so `caz-…-shared-aks-01` reads as `aks-01`.

  The panel carries its own `<script>` for the same reason it carries its own
  `<style>`: it has to work wherever it is dropped. Reusing the cost page's
  `sortTable` would have coupled these tables to that page's DOM conventions for
  the sake of forty lines.

### 7.5 Language convention

- [ ] **7.5.1** — **US English, everywhere.** Reported: the pages had drifted
  into British forms — *utilisation*, *normalise*, *behaviour*, *colour*,
  *labelled*, and *estate* as a collective noun for the clusters.

  This matters more now than it did a phase ago. With two pages that a reader
  moves between, a spelling that changes between them reads as an inconsistency
  in the system rather than a preference in the prose.

  Applied across source, page text, log messages, identifiers and all three
  documents: `normalise_numbers` is `normalize_numbers`, "Cluster Utilisation"
  is "Cluster Utilization", "the Kubernetes estate" is "the Kubernetes clusters",
  and dates written `1 December` are `December 1`.

  **One exception, and it is deliberate.** The Prometheus recording rule
  `instance:node_cpu_utilisation:rate5m` keeps its spelling: it is an upstream
  identifier from kube-prometheus-stack, not prose, and rewriting it would make
  the probe query nothing. Protected explicitly during the pass and verified
  afterwards.

  Verified: JSON keys shared between collector and page were untouched
  (`underutilized_vms` was already US English and matches on both sides), every
  Python file compiles, every shell script parses, every YAML file loads, and
  the rendered cost page differs from its baseline in exactly two CSS comments.

  The convention is recorded in the README so it does not have to be
  rediscovered.

### 7.6 Visual design

- [ ] **7.6.1** — **The pages had no palette. They had 65 colors.**

  Asked to give the pages some life, the first thing worth measuring was what
  was there: **65 distinct hex values across 126 uses**, most appearing exactly
  once. That is why it read as flat. The colors were never the problem — the
  absence of a system was, and adding more would have made it worse.

  Everything now routes through one `:root` token block. Changing the look is an
  edit to that block rather than a hunt through literals, and the cluster panel
  references the same tokens rather than defining its own, so the two pages
  cannot drift.

  Three header tints carry meaning and are documented as not interchangeable:
  deep green for measured spend, warm brown for cumulative totals and
  second-level tables, ochre for estimated figures — so a forecast never reads
  as solid as a measurement. That is 4.1.4's argument expressed in the palette
  rather than only in a footnote.

  Palette is *documentary earth*: warm paper ground, tables lifted onto a
  lighter surface so they no longer dissolve into the page, sage and clay for
  cost down and up, ochre for anything interactive.

  Two things had to be pulled out of the markup first. The sparklines wrote
  `fill="#c0392b"` into every SVG they emitted — direction is now a class on the
  `<svg>` and the shapes inherit from CSS. The pill tints lived in the cluster
  panel's own stylesheet and are now tokens beside the rest.

  Verified on both rendered pages: **zero hex literals outside the token block**,
  and zero colors written into markup.

- [ ] **7.6.2** — **Three faults reported from the recolored pages.**

  **The stat-card trend graphs rendered solid black.** Moving sparkline colors
  out of the markup and into CSS meant the direction had to travel as a class on
  the `<svg>`. Two functions emit sparklines; the edit added the class to one of
  them. The other computed `trend`, never used it, and fell back to SVG's
  default fill.

  Exactly the shape of 7.3.7's anchor bug — an edit that matched one call site
  and silently missed the rest — and it failed the same way, by producing
  plausible output rather than an error. The overview table's sparklines were
  correct, which is what made the black ones look like a color choice rather
  than a defect.

  Both renderers are now covered by a direct check of what class each emits, for
  rising, falling and flat input, plus the no-forecast and single-point paths.
  A grep for `fill=`/`stroke=` in the emitted SVG asserts nothing writes a color
  into markup again.

  **The capacity table needed horizontal scrolling to reach the capacity
  figures.** The Kind cell lists every application on a pool, and a pool hosting
  seventeen of them set the table's width — so the columns that are the reason
  to read the table were off-screen. Capped at 22ch and wrapped, so a long list
  grows the row instead of the table. The pill inside had to be told to wrap
  too, or it simply overflowed the cap.

  **The CPU columns did not say what their numbers were.** *Usable*,
  *Requested*, *Used*, *Unclaimed* and *Over-req.* all showed bare figures with
  nothing indicating cores, dollars or a percentage. They are CPU cores. The
  header's qualifier line says so now, and the two gap columns name the unit
  alongside the owner.

  That fix collided with the previous one: at ~17 characters, `cores · app
  teams` set its column's width and put the scrollbar straight back. The
  qualifier line now wraps while the label does not, so it cannot drive column
  width. Measured at roughly 1106px against a 1252px content box, with the
  widest Kind cell in the fixture at 125 characters.

### 7.7 Retiring the probe

- [ ] **7.7.1** — **Removed: three probe scripts, three jobs, the `Cluster
  Probe` run type and its includes.** 843 lines.

  Every question they existed to answer is answered, and the production
  collector does all of it weekly with better handling. `container_insights.sh`
  backed a closed decision that re-running could not change. `untagged_spend.sh`
  had recorded its measurement — and still carried the `str(None) == "None"`
  defect that produced the wrong figure, so keeping it meant keeping a script
  that would mislead the next person to run it.

  **One thing was preserved before deleting.** `container_insights.sh` held the
  only working recipe for querying Log Analytics in Azure Government, which took
  three rounds to find and which the log described but did not record. The call
  and the response reshaping are now written into 7.3.4 above. Deleting a probe
  should cost nothing; deleting the only copy of a hard-won answer is not the
  same thing.

  The README's language exception cited a Prometheus recording rule that lived
  in the probe. `prom.py` never used it — it builds its own queries from raw
  metrics — so the exception is now stated as a general rule about upstream
  identifiers rather than pointing at a token no longer in the repository.

---

## Where things stand

A sweep of every unchecked box, because "open" has come to mean four different
things and the list is no longer readable at a glance.

### Built, waiting on an event that has not happened

Nothing to do. These execute the first time the condition they handle occurs,
and checking them off before that would be claiming evidence that does not
exist.

| | Waiting for |
|---|---|
| 1.3.1, 1.3.2 | a backfill job that fails or hits the 4-hour timeout |
| 2A.3.4 | an environment's cost collector genuinely failing |
| 4.2.1 | a live run confirming the quiet path — it has now logged `History is contiguous` on every run since, so this is close to closeable |

### Built and now vetted by the rollover

**5.3.1 through 5.3.5 have run for real.** The JWCC POP boundary moved to
2026-08-30, the period closed, and the page carries
`archive/2025-12_to_2026-08/` labelled 2025-12-01 to 2026-08-29. The whole path
— freeze once, render every run, link permanently — executed without
intervention. These are checkable as soon as the archive page has been looked
at.

### Superseded, kept as a record

**3.1.3.** The stale-resource scan left the backfill path by construction when
Phase 2A split the collectors. The box stays unchecked because it was never
implemented, not because it is outstanding.

### This week's work, awaiting the next run

7.1.10, 7.4.1 through 7.4.5, 7.5.1, 7.6.1, 7.7.1.

### Genuinely unbuilt

Two things, and only two.

**Phase 3.2 — the bulk historical query.** Never probed. Daily granularity over
a custom range would collapse the per-week query loop into a handful of chunked
calls, and would need `nextLink` pagination, which does not exist anywhere in
the codebase. It was written when a backfill was a ~100-minute all-or-nothing
run; resume, response caching and delay tuning have since taken that to roughly
13 minutes for six windows and made a failed run cheap. So it is the largest
remaining piece of work and no longer an urgent one.

**Phase 6.1 — the tag hygiene panel.** This one is different: **everything it
needs is already being collected and thrown away every week.** 2.2.5 stores raw
tag spellings, 2.2.7 persists the live tag inventory, and `naming.py` already
folds variants. What is missing is the panel that reads them — near-duplicate
detection, live-versus-historical (the distinction that makes it actionable
rather than a list of noise), spend and lifespan per variant, and untagged
spend trended weekly.

7.3.8 measured that last figure once at **$269.76/week, 3.6% of the bill**, and
found that the page drops it silently. That is the smallest useful piece of 6.1
and the one with a measurement already behind it.

The rest of Phase 6 — 6.2 through 6.5 — remains a candidate list that was never
committed to.
