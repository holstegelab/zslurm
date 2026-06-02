# zslurm / cluster_manager — Architecture & Behaviour Report

*Status: analysis report, 2026-06-02. Line references are `file:line` against the
working copy in this repo (`zslurm`, `zslurm_chief`, `zslurm_shared.py`, the `zs*`
clients) and the Snakemake executor plugin in `~/repos/snakemake_executor`.*

---

## 1. What zslurm is, and why it exists

zslurm is a **pilot-job ("meta") scheduler that runs on top of Slurm**. Its stated
purpose (`setup.py:16`) is to *"allow for core-level scheduling on systems that only
allow (partial) node-level scheduling."*

The reason this matters on **SURF Snellius** (the Dutch national supercomputer this
is built for) is purely economic and operational:

- **Snellius bills whole-node core-hours.** CPU thin/fat/himem partitions are charged
  per *whole node* in SBUs (System Billing Units ≈ core-hours), regardless of how many
  cores you actually use. A Genoa node is 192 cores × 1.0 SBU/core-hour; an idle core on
  an allocated node still costs money. (See the Snellius reference in §10 and the cost
  discussion in §7.)
- **Storage is tiered, budgeted and auto-expiring**: 200 GiB home; per-project
  `/projects` (TiBs, no backup, deleted 4 weeks after project end); an 8 TiB scratch
  quota shared between `/scratch-shared` (14-day auto-delete) and `/scratch-local`
  (6-day); truly node-local NVMe via `--constraint=scratch-node` (~5.9 TiB, wiped at job
  end); and `/archive` dCache tape for long-term storage.

A genomics pipeline is thousands of small, heterogeneous tasks (align, dedup, call,
encrypt, stage to/from tape). Submitting each as its own Slurm job would waste whole-node
allocations and hammer the scheduler. zslurm instead **grabs whole Slurm node allocations
("engines") and internally packs many fine-grained jobs onto them**, while also tracking
shared **storage budgets** so a run never blows its disk/tape quota.

### The two-layer model

```
            ┌──────────────────────────────────────────────┐
 you ──▶ zsbatch / snakemake plugin ──▶ submit_job (RPC) ──▶ │  zslurm MANAGER (one curses process)
            (clients, base_port+1)                          │   • JobManager: one ordered queue
                                                            │   • EngineManager: the pilot fleet
                                                            │   • 3 storage budgets + scheduler
            zsqueue/zsnodes/zsstats/... (read RPCs) ◀────────┤   • controller thread (squeue scan,
                                                            │     autogrow, autoconsolidate)
            ┌───────────────────────────────────────────────┘
            │ register / poll / request_jobs (RPC, base_port)
            ▼
   ENGINES = zslurm_chief, one per Slurm allocation (pilot job)
   sbatch -N1 ... slurm_to_zslurm → conda activate → zslurm_chief
   • pull jobs that fit free cores/mem, Popen each in its own process group
   • psutil + cgroup v1/v2 monitoring, stuck/OOM detection, report back
```

- **Manager** = the file `zslurm` (~4300 lines). One curses process per *instance*.
- **Engine** = the file `zslurm_chief`. One worker per Slurm allocation (pilot job).
- **Clients** = `zsbatch` (submit), `zsqueue`/`zsnodes`/`zsqueue_stats`/`zscancel`
  (live), `zsoccupancy`/`zsstats`/`node_usage_viewer.py` (offline analyzers).
- **`zslurm_shared.py`** = transport (`TimeoutServerProxy`), instance discovery, config,
  and Snellius-aware `scontrol` parsing.

> **Naming caveat:** the file `zslurm_chief` is the *worker/engine agent*, **not** the
> manager. The manager is the file literally named `zslurm`.

---

## 2. Process & control-plane model

The manager is started by `curses.wrapper(main)` (`zslurm:4299`) — **it requires a TTY**;
there is no headless/daemon mode today. `main()` (`zslurm:3945`) picks/creates an instance,
opens TSV report files, builds the curses UI, and calls `start_server(port, rpcpath)`
(`zslurm:3055`), which spins up **three daemon threads**:

| Thread | What it serves | Port | Methods |
|---|---|---|---|
| `thread_start_manager_server` | **engine**-facing RPC | `base_port` | `register`, `failed_node`, `unregister`, `poll`, `request_jobs`, `job_finished`, `can_run_assigned_job` (`zslurm:3042-3048`) |
| `thread_start_job_server` | **client**-facing RPC | `base_port+1` | `submit_job`, `cancel_job`, `list_jobs`, `list_done_jobs`, `queue_stats`, `list_nodes` (`zslurm:1527-1532`) |
| `thread_check_commands` | controller loop (every 5 s): squeue scan, engine timeouts, autoconsolidate, autogrow (`zslurm:2703-3029`) | — | — |

Two in-memory singletons hold **all** state: `jobs = JobManager()` (`zslurm:1502`) and
`engines = EngineManager()` (`zslurm:2509`), each guarded by its own `threading.RLock`.

### Transport, instances, "auth"

- Stdlib `SimpleXMLRPCServer(allow_none=True)` over plaintext HTTP, bound to `("", port)`
  (all interfaces). Clients use `TimeoutServerProxy` (70 s timeout, `zslurm_shared.py:73`).
- An **instance** is a YAML file `~/.zslurm/instances/<name>.yaml` holding `name`,
  `bind_host`, `advertise_host`, `base_port`, and `rpcpath` — a **random 8-letter path
  that is the only access control** (`zslurm_shared.py:236-252`). Manager URL =
  `http://{host}:{base_port}/{rpcpath}`; job URL = `…:{base_port+1}/{rpcpath}`.
- Client instance resolution: `--instance` → `$ZSLURM_INSTANCE` → the single discovered
  instance → `config.yaml` default → `DEFAULT_INSTANCE_NAME="zslurm"` (`zsbatch:44-60`).
- Config lives in `~/.zslurm/config.yaml` (cluster policy + worker tunables).

### Engine lifecycle (pilot jobs / glidein)

`EngineManager.start_slurm` (`zslurm:1982`) builds
`sbatch -N1 -p <part> [--exclusive|--cpus-per-task=N] [--constraint <feat>]
[--array=0-(n-1)] -J <instance> -t <time> slurm_to_zslurm -a <addr> -p <port> -t <lpart>`.
`slurm_to_zslurm` is a 6-line shim that conda-activates `clustersnake` and execs
`zslurm_chief`. The engine **self-registers** (`register`, up to 5 retries with 30–210 s
backoff, `zslurm_chief:467-490`), advertising its real cores/mem/SSD, then enters a
20 s poll loop. The manager **never connects to engines**; control flows back as the
return value of `poll` (commands `STOP/DIE/CANCEL/REREGISTER/DEASSIGN`,
`zslurm_shared.py:33-38`).

Liveness: the controller cross-checks engines against `squeue` with anti-flap hysteresis
(2 confirms to mark managed, 2 misses to demote, `zslurm:1810-1888`) and reaps engines
unseen for `TIMEOUT=1200 s` (`zslurm:2718-2746`), requeuing their running jobs.

---

## 3. The job queue & dispatch (the heart of the scheduler)

`JobManager.jobs_by_id` is an **`OrderedDict` keyed by a monotonically increasing integer**
(`zslurm:448-450`). `submit_job` appends (`zslurm:632`). **There is no numeric priority
field — position in this dict *is* the priority.** A `Job` (`zslurm:319-416`) carries:
`ncpu` (**float — fractional cores allowed**), `mem` (MB), `reqtime` (s), `partition`,
the three storage deltas (`{archive,dcache,active}_start_use_add` / `_end_use_remove`),
`ssd_use ∈ {no,possible,required}` + `ssd_gb`, `requeue`, `dependency`,
`input_mb`/`output_file` (reporting only), `owner`. State machine:
`PENDING → ASSIGNED → RUNNING → COMPLETED/FAILED/CANCELLED/REQUEUED`.

Dispatch is **pull-based**: an engine calls `request_jobs(myid, current_cpu, current_mem,
partition)` (`zslurm:854`). Inside, the scheduler runs a **two-phase selection**:

1. **Candidate window (ordering).** Walk `jobs_by_id.values()` collecting `eligible()`
   PENDING/REQUEUED jobs until the window reaches `prio_fillmem_context + current_cpu`
   (default `prio_fillmem_context = 500`, `zslurm:101,954-965`).
2. **Greedy memory-fit (packing).** From the first `prio_fillmem_context` candidates, pop
   the job with the **lowest fit score** (`zslurm:966-1043`), reserve its budgets/cores/mem,
   decrement `current_cpu/current_mem`, and keep packing until the node is full — so one
   RPC returns a *batch* that fills the engine.

`eligible()` (`zslurm:934-951`) matches a job to an engine iff **all** hold: same
partition (exact string), `reqtime ≤ engine.timeleft`, `ncpu ≤ cores`, `mem ≤ totmem`,
SSD fits, **and each storage budget fits**. There is **no host affinity / data locality** —
matching is partition + capacity + budget only.

If a job fits the engine's *static* totals but not its *current* free space, it is
**ASSIGNED** (reserved) rather than started; the engine holds it and calls
`can_run_assigned_job()` once space frees (`zslurm:1216-1271`). An idle engine can even
**steal** an ASSIGNED job from another via `DEASSIGN` (`zslurm:1155-1194`).

---

## 4. Pillar 1 — Depth-first / first-in-last-out (LIFO) pipeline execution

**Mechanism (one line):**
```python
jobs_view = self.jobs_by_id.values()
if status.lastin_first and partition != 'archive':
    jobs_view = reversed(jobs_view)          # zslurm:889-892
```
`status.lastin_first` defaults **True** (`zslurm:100`). Because the queue is in submission
order, reversing it offers the **most-recently-submitted jobs first**.

**Why this is depth-first.** In a Snakemake DAG the downstream steps of a pipeline that is
*already running* were submitted *after* the seeds of any not-yet-started pipeline. LIFO
therefore keeps feeding the in-flight pipeline (drives each sample to completion)
instead of breadth-first starting many new samples. The README frames this exactly
(`README.md:112-123`): *"useful for deep-first job graph traversal … favor finishing
active samples well rather than starting too many fresh samples too early,"* and notes the
synergy with storage budgets: *"these constraints naturally discourage broad fan-out when
shared staging capacity is tight."*

**Precise ordering keys.**
- Primary: `OrderedDict` insertion order, reversed iff `lastin_first and partition≠archive`.
  The **`archive` partition is always FIFO** (`zslurm:891`) — staging/tape work runs oldest-first.
- Secondary (tie-break inside the window): the greedy fit score (Pillar 2), lower-is-better.
  If `prio_fillmem_context ≤ 1` it degenerates to strict (reversed) order `queued.pop(0)`.
- Manual override: `prioritize(pattern)` / `deprioritize(pattern)` (TUI `p`/`n`,
  `zslurm:542-569`) rebuild the dict by job-name substring. **They are LIFO-aware**:
  `prioritize` calls `_reorder_jobs(pattern, up = not status.lastin_first)`, so under
  default LIFO "prioritize" pushes matches to the **end** of the dict precisely so the
  reversed walk reaches them first. *(This inversion is a common foot-gun — never reason
  about raw order without first reading `lastin_first`.)*

**How the real pipeline expresses depth-first.** The `short_read` pipeline does **not**
use zslurm prioritize at all. It relies on `lastin_first=True` plus
`scheduler: greedy` plus **monotonically increasing Snakemake `priority:` integers along
each sample's dependency chain** (`align_reads`=15 → `markdup`=20 → `mCRAM`=30 →
`reblock`=29 → joint-calling 30–70). Snakemake submits downstream rules the instant their
inputs exist; zslurm only ever sees the resulting submission order. (`Aligner.smk`,
`gVCF.smk`, `Genotype.smk`, `VQSR.smk`.)

---

## 5. Pillar 2 — Memory & thread optimization across heterogeneous machines

Snellius nodes are heterogeneous: rome 128 c / ~224 GiB (~1.75 GB/core), genoa 192 c /
~336 GiB, fat_rome ~960 GiB, fat_genoa ~1440 GiB (~7.5 GB/core), himem 4/8 TiB. zslurm
packs each engine according to **its own** memory profile.

**Learning node size.** At `register` the manager stores each engine's real `cores`/`totmem`
(`zslurm:2235-2237`). The engine itself derives these cgroup-aware:
`e_ncpu = min(host_cpu_count, cpu)`; `e_memtot = memory_core_mb·e_ncpu − reserve`; the
advertised `e_memtot_buffer = min(cap_fraction·avail, (1−headroom)·e_memtot)`
(`zslurm_chief:360-369`, defaults `mem_cap_fraction_of_total=0.99`,
`mem_headroom_fraction=0.08`, `mem_static_reserve_mb=100`).

**The greedy fit score** (`zslurm:966-1043`), for each candidate against an engine where
`average_mem_core = totmem/cores` (`zslurm:897`):
```
remain_cores = current_cpu − job.ncpu
remain_mem   = current_mem − job.mem
over_use_penalty = −(min(remain_cores,0) + min(remain_mem/avg_mem_core,0)/1024)   # punish overcommit
memory_penalty   = |max(remain_mem,0)/max(remain_cores,1) − average_mem_core|/1024 # punish leaving a bad mem/core ratio
ssd_bonus        = 0.25 if job.ssd_use=='possible' and engine has free SSD
score = memory_penalty + over_use_penalty − ssd_bonus       # lowest wins
```
This drives the post-assignment mem/core ratio toward each node's natural ratio, so
memory-hungry jobs gravitate to fat/himem nodes and the thin nodes stay densely packed.
The TUI `m` key sets `prio_fillmem_context` (the search window) — *"higher values give the
scheduler more freedom to find a good fit, at the cost of a broader search"* (`README.md:108`).

**Fractional cores.** `ncpu` is a float end-to-end (`zsbatch -c` is `type=float`,
`zsbatch:17`; engine does `current_cpu -= ncpu`, `zslurm_chief:885`). The Snakemake profile
defaults `n="1.0"` with `set-resource-scopes: n=local`. So a 192-core node can pack many
sub-core jobs.

**Thread limiting (`--limit-threads`).** This is done **client-side**, not in the scheduler:
if `≥1`, `zsbatch` injects `OMP_NUM_THREADS / OPENBLAS_NUM_THREADS / MKL_NUM_THREADS /
VECLIB_MAXIMUM_THREADS / NUMEXPR_NUM_THREADS = int(limit_threads)` into the job env
(`zsbatch:77-83`). There is **no "auto" thread computation** anywhere — the name
`limit_auto_threads` is just the Snakemake resource (static default 8 in the profile) wired
to `--limit-threads`. Its job: stop BLAS/OpenMP from spawning a thread-per-core and
oversubscribing a fractionally-reserved node.

**Monitoring & failure handling (engine side, `zslurm_chief:553-784`).** Each job runs in
its own process group; a `job_monitor` thread samples cpu/mem (PSS)/io via psutil over the
whole tree plus cgroup v1/v2. Two notable behaviours an operator/agent must know:
- **Stuck-job watchdog** (compute only): if cpu < 0.05 core and io-delta ≤ 4096 B for
  `stuck_stop_threshold=60` consecutive monitor intervals, the tree is killed with
  `RC_STUCK=-254` (`zslurm_chief:670-698`).
- **OOM-retry by doubling memory**: on a non-success failure with `requeue>0`, `job_done`
  **doubles `job.mem`** and requeues (`zslurm:1342-1351`). The Snakemake plugin sets
  `requeue=0` and lets Snakemake's `restart-times` drive retries instead; the pipeline
  uses `attempt`-scaled `mem_mb` lambdas to grow memory on retry.

---

## 6. Pillar 3 — Active / staging (dCache) / archive storage budgets

This is zslurm's distinctive feature: besides CPU/mem/time/partition, the scheduler gates
on **three instance-wide storage counters**, each a `(total, inuse)` GB pair on the
`JobManager`, **all initialized to 0** (`zslurm:469-474`):

| Budget | User meaning (`README.md:330-332`) | Snellius mapping |
|---|---|---|
| **archive** (`arch_use_*`) | temp space while copying into/out of archive | `/archive` dCache **tape** staging |
| **active** (`active_use_*`) | fast project/working storage | `/scratch-*`, `/projects` working disk |
| **dcache** (`dcache_use_*`) | managed movement through dCache-backed storage | dCache staging/processed area |

**Semantics (add at start, remove on success).** A job declares, per class, a `*_use_add`
(reserved when the job **starts**) and a `*_use_remove` (released when it **succeeds**):
- Admission/eligibility everywhere enforces the invariant **`total ≥ inuse + start_use_add`**
  (or `start_use_add == 0`), checked at submit-eligibility, `eligible()`, the greedy
  re-check, the final pre-assign gate, and again in autogrow/autoconsolidate eligibility
  (`zslurm:945-950, 978-998, 1046-1067, 2523-2525, 2824-2826`). **A job can stay PENDING
  even when CPU/mem are free** if a budget would be exceeded (`README.md:334`).
- Reserve on start: `inuse += start_use_add` (`zslurm:1070-1072`).
- Release on success: `inuse −= end_use_remove`; on failure: `inuse −= start_use_add`
  (the start reservation is rolled back) (`zslurm:1306-1313`).

Three modelling patterns (`README.md:349-353`):
- **Temporary staging**: `add=N, remove=N` → holds N GB while running, frees it at the end.
- **Producer**: `add=N, remove=0` → output that persists; counter stays higher.
- **Cleanup**: `add=0, remove=M` → frees M GB on success; **not gated at start** (add=0
  always passes).

**Where the pipeline expresses it (real examples).**
- *Active reserve/release pairing*: `start_sample` declares
  `active_use_add = calculate_active_use(...)` (`Aligner.smk:389-408`); `finished_sample`
  declares `active_use_remove = calculate_active_use(...)` with the **same function**
  (`Snakefile`), gated on deliverables being uploaded — so a sample's GB returns to the
  pool only when it is truly done. *This is exactly why depth-first matters: finishing a
  sample releases its budget for the next one.*
- *Archive (tape)*: `archive_get` charges `arch_use_add = batch['size']` on
  `partition="archive"` while staging off tape (`Aligner.smk:243-256`); `archive_to_active`
  credits `arch_use_remove = filesize` once copied to active disk (`Aligner.smk:412-428`).
  These two `partition="archive"` rules are the only non-`compute` jobs.
- *dCache*: upload rules carry `dcache_use_add/remove = config.get(..., 0)` (default off,
  per-run switchable) (`Deepvariant.smk`, `Stat.smk`, `Kraken.smk`).
- The standalone `dcache_cp` toolkit and the `ada`/`ada_ls`/`ada_quota` SpiderScripts
  implement the same discipline outside Snakemake: **stage one batch → verify Adler-32 →
  destage immediately**, so the staging footprint stays bounded even for datasets larger
  than the staging quota.

**Critical operational fact:** the three totals are **set only via the curses TUI keys
1/2/3 (totals) and 4/5/6 (inuse)** (`zslurm:4173-4208`); there is **no config key and no
RPC** to set or read them. They default to 0, so **any job with a positive `*_use_add` is
silently un-dispatchable until a human types a total.** This is the single most important
gap for automation (§8).

---

## 7. Autoscaling & cost (autogrow / autoconsolidate)

The controller thread runs two loops (both default **ON**: `autogrow_enable=True`
`zslurm:129`, `autoconsolidate_enable=True` `zslurm:109`):

- **autogrow** (`compute_autogrow_plan`, `zslurm:2517-2701`): pending *eligible* cores/mem
  − free capacity·1.1 − already-queued-engine capacity = shortfall; if it exceeds the
  largest node profile, score the configured `autogrow_prefer_partitions`
  (penalize fat −4 and non-scratch −1, reward mem/core relief +5) and `sbatch` new engines,
  capped at `autogrow_max_compute_nodes` (default **40**) with a 500 s cooldown. Staging
  autogrow is threshold-based. **Autogrow deliberately will *not* grow to relieve a
  storage-budget stall** (`README.md:183`) — because budget-blocked jobs aren't "eligible".
- **autoconsolidate** (`zslurm:2795-2928`): keep the highest-scoring engines covering
  running demand × 1.05, mark the rest `PHASING_OUT` (no new work, stop when idle), but
  skip entirely if eligible jobs are still queued.

**Cost rail (the gap):** there is **no SBU/core-hour ceiling anywhere**. Autogrow at 40
Genoa nodes = 40 × 192 = 7 680 core-hours/hour. SBU budget must be watched out-of-band
with `budget-overview`/`accuse` (the central accounting server lags ~24 h, so
`budget-overview`, which folds in live Slurm data, is the accurate one).

---

## 8. The interface gap: human-oriented today

The current surface is built for a **human operator at a terminal**. Concretely:

1. **Control is curses-only.** Start/stop engines, set the three storage budgets, toggle
   LIFO/autogrow/autoconsolidate, set the memory window, phase out nodes, prioritize, and
   shut down are **all single-key handlers in a blocking `getch` loop** (`zslurm:4092-4286`).
   None are exposed over RPC. The process needs a TTY to start at all (`curses.wrapper`).
2. **Storage budgets can't be provisioned programmatically** — TUI keys 1–6 only, default 0,
   not persisted, not in config, no RPC. Lose them on restart; recover only by retyping.
3. **No machine-readable mode where it matters.** `zsqueue`/`zsnodes` have only
   `tabulate`/`--parseable` output with **units baked into cells** (`"12.3 Gb"`, `HH:MM:SS`,
   `cpu×100`); only `zsqueue_stats`/`zsoccupancy`/`zsstats` have `--json`. RPCs return
   **positional tuples** (`list_nodes` = 20 fields, `list_jobs` = 14) with meaning encoded
   by index — fragile to change, no schema.
4. **No budget visibility.** `queue_stats()` has no budget fields, so an agent can't tell
   *"job is PENDING because the dcache budget is full"* from *"no engines."*
5. **Inconsistent exit codes & errors.** Each client uses ad-hoc 0/1/2/3; errors mix
   stdout/stderr and dump tracebacks; `zsbatch --parsable` (note: single-`a`, unlike the
   `--parseable` of the others) emits only a bare jobid; failures are human strings.
6. **No idempotency.** `submit_job` mints a new jobid every call — a retried submit after a
   timeout double-submits a running job.
7. **No liveness/health, no events.** No `ping`, no completion callback; clients must poll.
   The eligible-count cache is 15 s stale, the controller loop is 5 s, engine poll 20 s,
   autogrow cooldown 500 s — control latency is bounded by these.
8. **zslurm ignores `job.dependency`** in `eligible()` — the agent (or Snakemake) must own
   DAG completion; "done" is not a scheduler concept.
9. **`zsb.py` is a stale trap** (hardcoded `localhost:38865`, old 21-arg signature, old
   10-field unpack) — do not use it as a programmatic entry point.

These are catalogued per-component in the analysis appendix; the plan to close them is in
[`agent-interface-plan.md`](agent-interface-plan.md).

---

## 9. The Snakemake bridge

There are two submission paths, both ending at the same `submit_job` RPC:

- **Legacy `cluster:` profile** (`zslurm.yaml`, `snakemake_profile.yaml`): Snakemake's
  generic cluster mechanism shells out to `zsbatch … {resources.*}`, mapping `mem_mb`,
  `time`, `n`, `partition`, the six storage deltas, `limit_auto_threads`, `input_mb`,
  `output[0]`.
- **Native executor plugin** (`snakemake_executor_plugin_zslurm`): a `RemoteExecutor` that
  calls `submit_job` over XML-RPC directly (22 positional args incl. a per-run `owner`
  UUID), polls `list_jobs`/`list_done_jobs(last_seen_jobid, owner)` scoped to that owner,
  maps zslurm states to Snakemake success/error/running, uses an adaptive 30→180 s backoff,
  hardcodes `requeue=0` (retries via Snakemake `restart-times`), and **does not cancel**
  (its `cancel_jobs` is a no-op + drain). Note a **units mismatch**: the plugin treats
  `resources.time` as **seconds**; `zsbatch` parses `--time` as a SLURM time **string** →
  minutes.

Full resource model exposed to rules: `mem_mb`, `time`, `n` (fractional), `partition`,
`{arch,dcache,active}_use_{add,remove}`, `limit_auto_threads`, `input_mb`, `ssd_use`,
`ssd_gb`.

---

## 10. Snellius reference (context for sizing & cost)

| Partition | Cores | RAM (usable) | GB/core | SBU weight | Local NVMe |
|---|---|---|---|---|---|
| rome (thin) | 128 | ~224 GiB | ~1.75 | 1.0 /core-h | some phases |
| genoa (thin) | 192 | ~336 GiB | ~1.75 | 1.0 /core-h | some phases |
| fat_rome | 128 | ~960 GiB | ~7.5 | 1.5 /core-h | 6.4 TB |
| fat_genoa | 192 | ~1440 GiB | ~7.5 | 1.5 /core-h | 6.4 TB |
| himem_4tb | 128 | ~3840 GiB | ~30 | 2.0 /core-h | — |
| himem_8tb | 128 | ~7680 GiB | ~60 | 3.0 /core-h | — |
| gpu_a100 | 72 | ~480 GiB | — | 128 /GPU-h | some 7.68 TB |
| gpu_h100 | 64 | ~720 GiB | — | 192 /GPU-h | ~22 nodes |
| staging / cbuild | ~16 thr | ~224 GiB | — | 2.0 /thr-h | — (dCache I/O) |

- **No partition literally named `compute`** exists on Snellius — it is a zslurm-internal
  job *class* (`compute` vs `archive`); the Slurm engine partitions are rome/genoa/fat_*/
  staging. The code reads partitions dynamically from `scontrol`/`sinfo` and special-cases
  the **`scratch-node`** feature for node-local NVMe.
- Max walltime 120 h (gpu_vis 24 h). Storage auto-delete: scratch-shared 14 d,
  scratch-local 6 d, scratch-node at job end. Budgets are per-project, fair-share scheduled.
- Check budget with `budget-overview` (accurate; folds in live Slurm) > `accinfo`/`accuse`
  (~24 h lag).

---

## 11. Key file map

| File | Role |
|---|---|
| `zslurm` | manager: JobManager, EngineManager, RPC servers, scheduler, autogrow, curses TUI |
| `zslurm_chief` | engine/worker: register, pull jobs, Popen + monitor, report |
| `zslurm_shared.py` | transport, instance/config, `scontrol` scratch-aware parsing |
| `zsbatch` | submit client (sbatch analog) — carries the budget/ssd/thread flags |
| `zsqueue` / `zsnodes` / `zsqueue_stats` / `zscancel` | live monitoring/cancel clients |
| `zsoccupancy` / `zsstats` / `node_usage_viewer.py` | offline analyzers (scontrol / report TSV / HTML) |
| `zslurm.yaml`, `snakemake_profile.yaml` | Snakemake profiles (resource → zsbatch mapping) |
| `~/repos/snakemake_executor` | native Snakemake 8 executor plugin (RPC submit) |
| `~/projects/short_read_analyzing_pipeline_Snakemake` | the production pipeline that uses all three pillars |
| `~/projects/dcache_cp`, pipeline `ada*` | dCache stage→verify→destage tooling |
