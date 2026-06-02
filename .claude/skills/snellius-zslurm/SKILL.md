---
name: snellius-zslurm
description: >-
  Drive the zslurm pilot-job meta-scheduler on SURF Snellius (or run/monitor a
  Snakemake pipeline through it) as an autonomous agent. Use when the user wants
  to submit, monitor, steer, or budget compute on Snellius via zslurm/zsbatch,
  run a Snakemake workflow on the zslurm executor, manage the three storage
  budgets (active / dcache / archive), tune depth-first (LIFO) scheduling or
  memory packing, diagnose stuck/pending jobs, or check SBU/core-hour cost.
  Triggers: "zslurm", "zsbatch", "zsqueue", "zsstatus", "zscontrol", "Snellius",
  "SURF", "the cluster manager", "run the short-read pipeline", "budget stall",
  "why is my job pending".
---

# Driving zslurm on Snellius

zslurm is a **pilot-job meta-scheduler on top of Slurm**: one manager process grabs
whole-node Slurm allocations ("engines") and packs many fine-grained jobs onto them, so
Snellius' whole-node core-hour billing isn't wasted on idle cores. You drive it through
small CLI clients that speak XML-RPC to the manager — **never the curses TUI**.

Architecture, mechanisms, and file:line detail live in the cluster_manager repo under
`docs/` (`docs/zslurm-architecture.md` and `docs/agent-interface-plan.md`). This skill is
bundled in that repo at `.claude/skills/snellius-zslurm/`, so it auto-loads for anyone
using Claude Code there; it can also be copied to `~/.claude/skills/` for global use.
Snellius hardware/budget facts: read [`snellius_nodes.md`](snellius_nodes.md) in this skill
folder (they go stale — re-check against `scontrol`/`budget-overview`).

## The agent command surface

All commands live in the `clustersnake` conda env (`conda activate clustersnake`) on a
Snellius node. JSON output is numeric and `schema_version`-tagged.

| Command | Purpose |
|---|---|
| `zsstatus [--instance N]` | **The poll command.** One JSON blob: health, budgets, scheduler mode, queue, engine summary, and derived **alarms**. Start here. |
| `zsqueue --json [--all\|--done]` | jobs as numeric objects (state, cores, mem, the 3 budget uses) |
| `zsnodes --json` | engines: cores/totmem, reserved cpu/mem, SSD, status |
| `zsqueue_stats --json` | aggregate running/pending cores+mem by partition |
| `zsoccupancy --json` | real Slurm partition occupancy (from `scontrol`) — free nodes |
| `zsstats --json --files 'report*.tsv'` | **post-run** used-vs-reserved core-hours & peak RSS per rule (re-tune mem from this) |
| `zsbatch …` | submit one job (sbatch analog) — carries budget/ssd/thread flags |
| `zscancel [--requeue] JID…` | cancel/requeue |
| `zscontrol …` | **control plane**: reads (no token) `status` / `whatif` / `match` / `forecast` / `jobs` / `autogrow-plan`; gated writes (token) `budget` / `lifo` / `context` / `autogrow` / `prioritize` / `recompute-inuse` / `grow` / `shrink` |
| `zscontrol forecast PLAN.json` | **plan before you submit:** will a DAG fit the budgets? verdict `SAFE` / `ORDER_SENSITIVE` / `INFEASIBLE` (+ `first_blocking_job`) |

### Exit-code contract (branch on `$?`)
- `0` ok · `2` transport/instance-resolution failure → **retry with backoff** ·
  `3` logical rejection (control disabled, bad token, empty/oversized pattern,
  ambiguous instance) → **do NOT retry**.

## PREFLIGHT — always first

1. **Resolve the instance.** With one running manager, clients auto-detect it. With
   several, pass `--instance NAME` or set `ZSLURM_INSTANCE` (else you get exit 3).
2. **`zsstatus`** to confirm the manager is alive (`health.ok`, `controller_alive`).
   - exit 2 / unreachable ⇒ **the manager is down**. Do **not** try to start the curses
     TUI yourself. Either ask the user to start it (`zslurm` in a tmux/screen), or, if
     authorized for unattended operation, start headless:
     `zslurm --headless --enable-control --control-token <tok>` (add `--no-autogrow` if
     the agent will own scaling). It prints a one-line JSON banner with the endpoint.
3. **Provision storage budgets** (see Pillar 3) — the #1 cause of silent stalls.
4. For control writes, read the token from `$ZSLURM_CONTROL_TOKEN` (or `--token`).
   **Never echo it; never put it in argv you log.**

## The three pillars (decision trees)

### Pillar 1 — Depth-first / LIFO (finish started pipelines first)
`scheduler.lastin_first` defaults **true**: the newest-submitted jobs dispatch first, so a
pipeline already in flight (whose downstream steps were submitted most recently) is driven
to completion before new ones start. This is what keeps peak active-storage bounded.
- Drain in-flight work first (the usual goal) ⇒ leave LIFO **on**.
- Fair round-robin start of many independent pipelines ⇒ `zscontrol lifo off`.
- Strict submission order ⇒ `zscontrol context 1` (collapses the greedy window).
- Pin a specific pipeline ⇒ `zscontrol prioritize <name-substring>`.
  ⚠️ **LIFO inversion**: under default LIFO, `prioritize` moves matches to the *end* of the
  queue so the reversed scan reaches them first. Always read `scheduler.lastin_first` from
  `zsstatus` before reasoning about order; don't assume raw order.
- The `archive` partition is **always FIFO** (oldest-first) regardless of LIFO.

In Snakemake, depth-first is expressed by increasing `priority:` along each sample's rule
chain + `scheduler: greedy` (zslurm only sees the resulting submission order). Don't fight
it with `zscontrol prioritize`.

### Pillar 2 — Memory & threads across heterogeneous machines
zslurm packs each engine to **its own** mem/core ratio (`average_mem_core = totmem/cores`).
Mis-sizing `mem` hurts packing more than cores.
- Read `zsnodes --json` (per-engine cores/totmem/res_cpu/res_mem/SSD) and the post-run
  `zsstats --json` (used-vs-reserved mem & cpu) to size jobs from **observed peak**, not guesses.
- Pick the partition by **GB/core need** (see [`snellius_nodes.md`](snellius_nodes.md)):
  ≤ ~1.75 GB/core → `rome`/`genoa` (thin, cheapest 1.0 SBU); up to ~7.5 → `fat_*` (1.5);
  more → `himem_4tb`/`himem_8tb` (2.0/3.0). Never request more cores/mem than a node has —
  the job is silently un-eligible forever (no error).
- `n` (cores) is **fractional** — use `n=0.5` etc. to pack many tiny jobs per node.
- Set `--limit-threads K` (Snakemake `limit_auto_threads`) so BLAS/OpenMP libraries don't
  spawn a thread-per-core and oversubscribe a fractionally-reserved node. There is **no
  auto thread tuning** — you must pass the value.
- Heavy local I/O → `--ssd-use required --ssd-gb G` (lands on a `--constraint=scratch-node`
  NVMe node). On `compute` jobs with `requeue>0`, a failed job is **requeued with doubled
  memory** — watch for runaway OOM-retries and cancel rather than let it climb to himem.

### Pillar 3 — Active / dcache / archive storage budgets
Three instance-wide GB counters gate dispatch: a job stays PENDING (even with CPU/mem free)
if a budget would be exceeded. **Totals default to 0**, so any job with a positive
`*_use_add` is undispatchable until provisioned.
- **MUST provision in preflight**:
  `zscontrol budget --active <GB> --dcache <GB> --archive <GB>` from the project's real
  Snellius quota (8 TiB scratch shared by `/scratch-*`; `/projects` TiB; `/archive` tape).
- Per-job footprint: `--active-use-add/-remove`, `--dcache-use-add/-remove`,
  `--arch-use-add/-remove` (GB). Semantics: **add reserved at job start, remove released on
  success** (failure rolls back the add). Patterns: temporary staging `add=remove=N`;
  producer `add=N,remove=0`; cleanup `add=0,remove=M`.
- Diagnose with the **`budget_stall`** alarm in `zsstatus` (inuse + smallest pending add >
  total). Remedy: raise the budget if real quota allows (`zscontrol budget …`, cross-check
  `budget-overview`), or rely on LIFO so in-flight jobs release `*_use_remove` first.
- `zscontrol whatif --active <GB> …` previews how many pending jobs become (in)eligible at
  proposed totals — call it before changing a budget.
- **Forecast a whole DAG before submitting:** write the planned jobs to a JSON list
  (`[{name, partition, ncpu, mem, active_use_add, active_use_remove, dcache_use_add, …,
  depends_on:[names]}, …]`) and run `zscontrol forecast plan.json`. Verdict:
  `SAFE` (fits any order) → submit; `ORDER_SENSITIVE` (fits only if drained in order) →
  keep LIFO on and submit; `INFEASIBLE` → raise the budget (if quota allows) or split the
  DAG, then re-forecast. `worst_case_peak` = all reservations live at once;
  `optimistic_peak` = perfectly serialized; `first_blocking_job` names where it overflows.
- Repair drift: if a budget's `inuse` looks wrong (e.g. after a manager restart or a leaked
  reservation), `zscontrol recompute-inuse` resets each `inuse` to the sum over currently
  running/assigned jobs.
- mapping: `active` = working scratch/projects; `dcache` = dCache staging; `archive` = tape
  staging via the `archive` partition / `staging` engines.

## Canonical workflows

**Submit + monitor an ad-hoc job set**
1. preflight (instance, `zsstatus`, budgets).
2. submit with `zsbatch -c N --mem MB -t D-HH:MM:SS -p compute [--active-use-add G …]
   [--ssd-use required --ssd-gb G] [-d afterok:JID] -J <name> -- <cmd>`.
   For autonomous retries, prefer the RPC with an `idempotency_key` (a retried submit with
   the same key returns the existing jobid instead of double-submitting).
3. poll `zsstatus` **no faster than ~15 s** (the eligible cache TTL; faster just contends
   the dispatch lock). On each poll evaluate alarms → act per the symptom table below.
4. confirm done: a job is done only when terminal in `zsqueue --done --json`
   (`COMPLETED`/`FAILED`/`CANCELLED`). zslurm ignores job dependencies, so **you** own the
   jobid set — don't infer DAG completion from the scheduler.

**Run a Snakemake pipeline (the common case — e.g. short-read)**
Snakemake owns the DAG/retries; zslurm owns packing; you own correct `resources:` + the
budget proof. Use the `executor: zslurm` profile (`zslurm_snakemake8.yaml`) or the legacy
`cluster:` profile (`zslurm.yaml`). Per rule set `mem_mb`, `n`, `partition`, the six
`*_use_add/remove` deltas, `limit_auto_threads`, `input_mb`. Preflight: `zscontrol budget
…` then launch `snakemake --profile zslurm <target>`; monitor with `zsstatus` +
`snakemake`'s own progress; post-run re-tune mem from `zsstats --json`.

**Symptom → action (from `zsstatus.alarms`)**
| Alarm | Meaning | Action |
|---|---|---|
| `budget_stall` | a storage tier is full | raise that budget (if quota allows) or let LIFO drain `*_use_remove`; `whatif` first |
| `no_engines` | pending jobs, 0 engines, autogrow off/at-cap | `zscontrol grow N --partition genoa --yes` or `zscontrol autogrow on --max-nodes N` (within SBU rail) |
| `oversized_pending` | a job needs more cores than the biggest engine | enable autogrow onto a larger partition, or split the job |
| `near_oom` | a RUNNING job's live mem ≈ its reserved mem | raise that rule's `mem_mb`; on compute a hard fail requeues at **2× mem** — cancel a runaway rather than let it climb to himem |

## SAFETY RAILS (MUST / NEVER)
- **SBU rail:** zslurm has **no core-hour ceiling**; autogrow can reach ~40 nodes
  (≈7 680 Genoa core-h/h). Before enabling/raising autogrow, check `budget-overview` (more
  accurate than `accinfo`/`accuse`, which lag ~24 h), estimate burn from running+queued
  engine cores, and cap with `zscontrol autogrow --max-nodes N`. Halt if the SBU ceiling is near.
- **Min poll interval ≥ 15 s.** Never tight-loop `zsstatus`.
- **Idempotency:** never submit autonomously without an idempotency key / dedup guard.
- **Blast radius:** `zscontrol prioritize/deprioritize` dry-run via `zscontrol match
   <pattern>` first; the client refuses > 50 matches without `--yes`.
- **Exit codes:** retry on `2`, never on `3`.
- **Secrets:** never echo the control token or place it in logged argv.
- **Don't start the curses TUI blindly** on a liveness failure — surface "manager down,
   needs start" or use the headless launch where authorized.
- **OOM amplification:** failed compute jobs requeue with doubled mem — cancel a runaway
   rather than letting it climb to himem.

## Reference
- Snellius nodes / SBU / storage tiers: [`snellius_nodes.md`](snellius_nodes.md)
- RPC + client schemas: [`schemas/`](schemas/) (`schema_version` for drift detection)
- Worked transcripts: [`examples.md`](examples.md)
