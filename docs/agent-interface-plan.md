# Agent Interface Plan for zslurm + Snellius

*Companion to [`zslurm-architecture.md`](zslurm-architecture.md). Goal: let a Claude Code
agent drive zslurm on Snellius autonomously — plan a run, submit it, watch it, and
self-correct — without ever touching the curses TUI, while honouring the three pillars
(depth-first/LIFO, heterogeneous memory packing, active/dcache/archive storage budgets)
and never blowing the disk/tape or SBU budget.*

---

## 1. Design decision

Three approaches were evaluated:

| Approach | Idea | Verdict |
|---|---|---|
| **A. Thin structured layer** | `--json` + stable exit codes on existing `zs*` clients; one read-only `zsstatus` aggregator; a handful of tiny control RPCs that set attributes the TUI already sets. | **Foundation.** Max reuse, no new daemon, rides the existing job-server port, lands incrementally without touching the scheduler hot path. |
| **B. `zsagent` control API** | First-class programmatic API: full control RPCs + a `forecast_budget(DAG)` planner + `watch`. | **Pull the high-value writes from here in later phases** (budget set, scheduler mode, autogrow, what-if/forecast). Heavier; build incrementally, not wholesale. |
| **C. Snakemake-native** | Agent only authors `resources:` + a pre-launch budget linter; drives the existing profile. | **Adopt as a usage pattern** in the skill (most real work is Snakemake), but it doesn't give general control. |

**Chosen: a phased hybrid, anchored on A, pulling B's must-have writes, with C as the
primary documented workflow.** Do **not** scrape the TUI.

### Guiding constraints (verified against the code)
- **Two RPC servers.** Register new client-facing methods on the **job server**
  (`base_port+1`, `zslurm:1527-1532`) where every `zs*` client already connects.
- **Budgets are the #1 blocker.** Totals init to 0, settable only by TUI keys 1–6, no RPC,
  no config — so any budgeted job is undispatchable until provisioned (`zslurm:469-474,
  4173-4208`). Fixing this is the highest-value single change.
- **The manager is curses-only** (`curses.wrapper(main)`, `zslurm:4299`). True autonomy
  needs a **headless launch**; otherwise a human must seed a `tmux`/`screen` TUI first.
- **No idempotency, no dependency check, no liveness RPC, no budget visibility** today.

---

## 2. What to build (capabilities → mechanism)

All new RPCs are thin shims over existing state/methods, registered next to the existing 6
on the job server, taking `jobs.lock` for any mutation. Read RPCs stay open; **all writes
gated behind `enable_control_rpc` (default off) + a `control_token`**.

### Reads (safe, cheap, reuse existing predicates)
| RPC / command | Returns | Backed by |
|---|---|---|
| `ping()` / `health()` | `{schema_version, uptime_s, job_thread_alive, engine_thread_alive, instance, lastin_first, prio_fillmem_context}` | new, ~15 lines |
| `get_status_json()` | budgets `{active,dcache,archive: {total, inuse, pending_add}}` + scheduler mode | the only read data missing from `queue_stats` (`zslurm:469-474`, sum `*_start_use_add` over PENDING) |
| `--json` on `zsqueue` / `zsnodes` | the 14-/20-field rows as keyed objects, **numeric** (no unit suffixes) | mirror existing `--parseable` branches |
| `zsstatus --json` (new client) | one schema-versioned blob: queue (`queue_stats`) + engines (`list_nodes`) + budgets (`get_status_json`) + scheduler + Snellius free nodes (`scontrol`) **+ derived alarms** | fans out existing RPCs |
| `whatif_budget(active,dcache,archive)` | `{eligible_delta_by_partition}` — how many pending jobs become (in)eligible at those totals | reuse `_is_job_eligible_locked` |
| `match_jobs(pattern)` | `[jobids]` matching a substring (dry-run before any pattern write) | walk `jobs_by_id` |
| `list_jobs_detailed(owner,states)` | named-key per-job dicts incl. `requeue_remaining`, `current_mem_mb`, `mem_pressure` | separate from the stable 14-field `list_jobs` tuple |
| `get_autogrow_plan()` | the controller's last autogrow plan (TUI-only today) + cap | reads `status.autogrow_plan` |
| `forecast_budget(plan)` | per-budget `{current_inuse, total, headroom, optimistic_peak, worst_case_peak, verdict, first_blocking_job}` + compute-fit; verdict ∈ SAFE / ORDER_SENSITIVE / INFEASIBLE | DAG topo-walk; pure read |

**Derived alarms** computed in `zsstatus` (the agent's done-vs-hung decision aid):
- `budget_stall`: `inuse + min_pending_add > total` for some tier **and** budgeted-pending > 0.
- `no_engines`: pending > 0, running engines = 0, autogrow off/at-cap.
- `deadlock`: max pending resource requirement (cores/mem/time) > max provisionable node —
  these jobs are silently excluded by `eligible()` forever.

### Writes (gated, minimal, mirror TUI semantics)
| RPC / command | Mirrors | Notes |
|---|---|---|
| `set_budgets(active_total, dcache_total, archive_total, *_inuse=None)` | TUI keys 1–6 (`zslurm:4173-4208`) | **closes the #1 blocker**; takes `jobs.lock` (hardening vs TUI which doesn't) |
| `set_scheduler_mode(lastin_first=None, prio_fillmem_context=None)` | TUI `l`/`m` | `prio_fillmem_context ≥ 1` |
| `prioritize(pattern)` / `deprioritize(pattern)` | TUI `p`/`n` | methods exist (`zslurm:542-546`), just register on the job server; LIFO-aware; return count moved |
| `submit_job(..., idempotency_key=None)` | extend existing | `seen_tokens → jobid` replay map; prevents double-submit on retry |
| `set_autogrow(enable, max_compute_nodes)` | TUI `g` | the dangerous one — paired with the skill's SBU rail |
| `grow(n, partition, stime, constraint, cores)` / `shrink(n)` | TUI `d` / `c` | engine-fleet control; `grow` sbatches real allocations — most gated, client requires `--yes` |
| `recompute_inuse_from_running()` | — | repair budget drift (`inuse>total`, leaked reservations) |

### New clients
- **`zsstatus`** — the single read-only command the agent polls (one JSON blob + alarms).
- **`zscontrol`** — subcommands `budget` / `lifo` / `context` / `prioritize` /
  `deprioritize` / `autogrow` (writes; reads `control_token` from env/file).
- `--json` added to `zsqueue` and `zsnodes`.

### Headless launch
- `zslurm --headless [--no-autogrow] [--no-autoconsolidate]` — run the manager's server +
  controller threads **without** the curses UI (dummy screen / factor the loop out of
  `curses.wrapper`). Required for unattended start and for CI/testing. Autoscale flags let
  an agent own scaling (and make testing safe — autogrow defaults ON and would `sbatch`).

### Exit-code contract (uniform across all clients)
- `0` success · `2` transport/instance-resolution failure (**retryable**, with backoff) ·
  `3` logical rejection (budget would be violated, empty pattern match, control disabled —
  **NOT retryable**). This lets the agent branch on `$?` instead of parsing text.

### Auth & transport hardening
- `enable_control_rpc: false` by default; bind control writes to localhost; a
  `control_token` distinct from the read `rpcpath`, sourced from a file/env, never echoed
  or placed in `ps`-visible argv. Reads stay on the open `rpcpath`.

---

## 3. Phasing (ordered by value/safety)

| Phase | Scope | Status |
|---|---|---|
| **0** | `ping()`/`health()` + `get_status_json()` (budgets+mode) + **headless mode** | ✅ done |
| **1** | `zsstatus --json` aggregator (+ derived alarms `budget_stall`/`no_engines`/`oversized_pending`); `--json` on `zsqueue`/`zsnodes`; uniform exit codes 0/2/3 | ✅ done |
| **2** | `idempotency_key` on `submit_job`; `set_budgets` + `set_scheduler_mode`; register `prioritize`/`deprioritize`; `zscontrol`; `enable_control_rpc` + `control_token` | ✅ done |
| **3** | `forecast_budget(plan)`; `whatif_budget`; `match_jobs`; `list_jobs_detailed` (requeue_remaining + mem_pressure) + `near_oom` alarm; `recompute_inuse_from_running`; `get_autogrow_plan` | ✅ done |
| **4** | `set_autogrow(enable, max_nodes)`; gated `grow`/`shrink` engine-fleet RPCs (wrap `start_slurm`/`stop_slurm`) **paired with the skill's SBU rail** | ✅ done (RPCs); SBU-rail automation is skill-level guidance, not yet enforced server-side |
| **5** | SKILL.md + Snellius reference table + JSON-Schema files (`schema_version`) | ✅ done |

*All phases are implemented and tested against a throwaway headless instance (forecast
SAFE/ORDER_SENSITIVE/INFEASIBLE verdicts, budget-stall detect+clear, idempotent submit,
drift repair, exit-code contract 0/2/3, grow/shrink gating).*

**Still open (deliberate follow-up):** a true server-side SBU/core-hour ceiling that
auto-caps `grow`/autogrow (today the rail is skill guidance that polls `budget-overview`);
a live engine-reported `stuck` flag on running jobs (the engine detects stuck/OOM but only
surfaces it as a terminal return code, so `near_oom` is inferred from mem-pressure);
manager→client push/notify hooks (clients still poll); and a persistent budget store so
totals survive a manager restart without re-provisioning.

---

## 4. How the interface serves the three pillars

- **Depth-first / LIFO** — `get_status_json().scheduler.lastin_first` exposes state;
  `zscontrol lifo on|off` / `context N` steer it; `zscontrol prioritize PATTERN` pins a
  pipeline (LIFO-aware — the skill encodes the inversion). Archive stays FIFO.
- **Memory across heterogeneous machines** — the agent reads `zsnodes --json`
  (cores/totmem/res_cpu/res_mem/SSD) and `zsstats --json` (used-vs-reserved), then sizes
  each job's `mem_mb`/`n`/`partition` so its mem/core matches the target Snellius node ratio
  the greedy optimizer packs against. `--ssd-use required --ssd-gb` for node-local NVMe.
- **Storage budgets** — provision once at run start with `zscontrol budget …` from the
  project's real quota; poll `zsstatus.budgets` + the `budget_stall` alarm; per-job
  footprints flow via `zsbatch --{active,dcache,arch}-use-{add,remove}` (and the Snakemake
  `cluster:` line that already maps them). `whatif_budget`/`forecast_budget` prove a DAG
  fits before submitting.

---

## 5. Safety rails (encoded as MUST/NEVER in the skill)

1. **SBU rail:** there is *no* core-hour ceiling in zslurm; autogrow can reach 40 nodes
   (≈7 680 Genoa core-h/h). The agent MUST poll `budget-overview`/`accuse`, estimate burn
   from running+queued engine cores, and cap/halt autogrow before the SBU ceiling.
2. **Min poll interval:** never poll faster than ~15 s (the eligible-count cache TTL);
   faster buys nothing and contends `jobs.lock` with the dispatch hot path.
3. **Idempotency:** never submit autonomously without an `idempotency_key`.
4. **Blast radius:** before any pattern write, `match_jobs(pattern)` dry-run; refuse if
   matches exceed a threshold without explicit confirmation.
5. **Budget provisioning first:** budgets default to 0 → provision in preflight, else every
   budgeted job hangs PENDING (indistinguishable from "no engines" without the alarm).
6. **Exit codes:** retry on `2`, never retry on `3`.
7. **Secrets:** never echo `control_token`; never place it in argv/logs.
8. **Manager start:** on liveness failure, surface "needs human/tmux start" (or use the
   headless launch where authorized) — never blindly attempt to start the curses TUI.
9. **OOM amplification:** failed jobs requeue with **doubled** memory; watch
   `requeue_remaining`/rising `current_mem` and cancel rather than letting a job climb to
   himem.

---

## 6. Files touched

- **Edit** `zslurm`: new RPC bodies + `register_function` lines near `zslurm:1527-1532`;
  budget/scheduler setters extracted so TUI and RPC share one path; `idempotency_key` in
  `submit_job`; headless entrypoint alongside `curses.wrapper(main)`.
- **Edit** `zsqueue`, `zsnodes`: add `--json` (with row-padding + uniform exit codes).
- **New** `zsstatus`, `zscontrol`.
- **New** Claude Code skill bundled in the repo at `.claude/skills/snellius-zslurm/`
  (SKILL.md + `snellius_nodes.md` + `schemas/` + `examples.md`) — auto-loads for anyone
  using Claude Code in this repo, and can be copied to `~/.claude/skills/` for global use.
- **Untouched**: the scheduler hot path (`request_jobs`, the greedy fit, autogrow logic).
