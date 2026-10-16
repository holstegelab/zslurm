# Scheduling and pilot capacity

[Overview](../README.md) · [Getting started](getting-started.md) · [Snakemake](snakemake.md)

[Priorities](#pipeline-priority) · [Memory packing](#memory-aware-filling) · [LIFO](#last-in--first-out-l) · [Consolidation](#autoconsolidate-a) · [Autogrow](#autogrow-g) · [Job classes](#job-classes-and-slurm-partitions) · [GPUs](#gpu-jobs)

## Scheduler behavior and queue strategy

Besides simple FIFO-style scheduling, ZSlurm contains a few higher-level queue management strategies that strongly affect throughput and workflow shape.

### Pipeline priority

Every submitted job has an integer priority. Higher values are dispatched before
lower values; the default is 100. Set it directly with:

    zsbatch --priority 100 -- command arguments

The native Snakemake executor exposes the same value once per pipeline:

    snakemake --executor zslurm --zslurm-priority 100 ...

Priority is evaluated independently in each applicable worker queue, including
compute and archive or transfer workers. It therefore governs CPU work as well
as staging, downloads, uploads, and final writes that carry the pipeline value.
`zsqueue` shows the numeric value in its `PRIO` column; `zsqueue --json`
includes it as the numeric `priority` field.

Already-running work is never preempted. A high-priority job that is ineligible
for an engine because of partition, SSD, walltime, or a storage budget does not
block eligible lower-priority work on that engine.

The memory-aware packer may reorder jobs only within the same numeric priority
band. FIFO remains the default tie-breaker; compute LIFO, when enabled, reverses
order only within a band. Manual prioritize or deprioritize similarly adjusts
the legacy order within a numeric band, not across pipeline priorities.

The one-time `p`/`n` selectors match either a job name or a complete working
directory component. For example, `FUS1` matches jobs submitted from
`.../exome_runs/FUS1` and its subdirectories, but not `FUS10`.

Use an active cwd override when a pipeline must move across numeric priority
bands and stay there as it submits more work. In the UI, `P` asks for the
folder selector and priority; the default is one above the highest current
effective priority. `N` removes the exact selector and restores each affected
job's submitted priority (or another still-matching override). Existing
waiting jobs change immediately, future matching submissions inherit the
override, and running jobs are never preempted. Later overlapping rules win.
The rules are runtime manager state and are carried through an explicit live
handover.

The same controls are available non-interactively:

    zscontrol --instance zslurm_fcn41_ui match FUS1
    zscontrol --instance zslurm_fcn41_ui priority-override set FUS1 101 --yes
    zscontrol --instance zslurm_fcn41_ui priority-override list
    zscontrol --instance zslurm_fcn41_ui priority-override clear FUS1

### Memory-aware filling

The `m` control sets the **memory optimization search window** used by the scheduler.

ZSlurm does not just take the next queued job blindly. Instead, for each engine it looks ahead through a configurable number of eligible queued jobs and tries to choose jobs that fit the remaining **memory-per-core balance** of that engine best.

In practice this means:

- **Small-memory jobs** can be combined with **high-memory jobs** more intelligently
- Partially filled nodes are more likely to be packed efficiently
- Fewer nodes may be needed for the same workload
- Autogrow decisions become more sensible because existing nodes are filled better first

Higher values of `m` give the scheduler more freedom to find a good fit, at the cost of a broader search through the queue.

### Last-in / first-out (`l`)

The `l` toggle changes the job ordering preference for normal partitions so that **newer submitted jobs are considered first**.

This is useful for **deep-first job graph traversal**. In many pipeline/workflow settings, you often want to:

- keep working on samples that are already in progress
- finish downstream jobs for those samples earlier
- avoid starting many new samples at once
- reduce the number of partially processed samples occupying shared resources

That is especially useful when your workflow generates chains of dependent jobs. With last-in/first-out enabled, the queue tends to favor the newest continuation jobs, which often means continuing the sample you were already working on instead of spreading work broadly over many samples.

This works particularly well together with the **global archive/active/dcache constraints**, because these constraints naturally discourage broad fan-out when shared staging capacity is tight.

### Prioritization / deprioritization (`p` / `n`)

The `p` and `n` controls let you manually reorder the queue based on a substring match in the job name.

This is useful when you want to:

- push one project/sample/cohort to the front
- temporarily slow down less important work
- complement the deep-first behavior with explicit operator intent

### Autoconsolidate (`a`)

Autoconsolidation tries to reduce the number of running engines when the current workload fits on fewer nodes.

Operationally, it:

- periodically evaluates each partition
- estimates how much CPU and memory should be kept, with a safety margin
- ranks the engines by a keep-score and phases out the lowest (see below)
- marks the extra engines as **phasing out** instead of killing them immediately

#### Which engine gets phased out

The keep-score leads with **residual work**: for each engine, the longest remaining
`reqtime` over the jobs running on it, i.e. how long until it would run empty if it
were given nothing more. That is what phasing an engine out actually costs, because
a phased-out engine holds its node — and keeps billing for it — until its last job
ends, while accepting no new work. An engine that drains in a minute is nearly free
to drop; one holding a six-hour job is not, and its capacity cannot be reclaimed by
phasing it out anyway, since that job runs to completion either way.

The remaining terms are reserved CPU/memory, occupancy (jobs per core) and remaining
node walltime. All four are normalised to 0..1, so `consolidation_keep_weights`
(`drain` 0.45, `reserves` 0.25, `jobs` 0.15, `time` 0.15) genuinely decides the
balance. `consolidation_drain_horizon_sec` (default 6 h) is the residual work at
which an engine counts as fully loaded.

This depends on jobs carrying a realistic `reqtime`. The short-read pipeline supplies
one per rule from measured runtimes; jobs submitted without one fall back to the
profile default and will all look equally busy.

Phasing out means:

- the engine accepts new jobs only inside the backfill window described below
- existing jobs are allowed to finish
- once idle, the engine is stopped

#### Backfilling a phasing-out engine

A phased-out engine holds its node until its **last** job ends. For consolidation
or manual draining, backfill must fit inside the estimated remaining work, so it
does not deliberately delay releasing surplus capacity. For **end-of-life**
draining, the constraint is instead the remaining Slurm lease: the early default
12-hour drain marker does not blacklist a four-hour job on a busy pilot with ten
hours left. This can extend its useful life, but never its allocation.

This is deliberately conservative and falls back to refusing everything, exactly as
before, unless all of these hold:

- `phaseout_backfill_enable` is on
- the engine is not stopping
- the engine still has running work, since letting an empty one die is the point
- at least `phaseout_backfill_min_window_sec` (default 1 h) remains after subtracting
  `phaseout_backfill_margin_sec` (default 30 min)

The window is also capped by the node's own remaining walltime minus the same margin,
and every job still has to satisfy `job_walltime_buffer_sec` on top, so a backfilled
job needs roughly 45 minutes of slack before it is allowed to start.
These are scheduling estimates, not guarantees that a job cannot overrun. Empty
pilots still stop, and the minimum queue age, actual free cores/memory, dependency
and controller-version checks remain in force on both Spider and Snellius.

#### Fitting jobs to remaining walltime

A job is only handed to an engine when `reqtime + job_walltime_buffer_sec` (default
15 min) fits in that engine's remaining walltime. `reqtime` is an estimate, so a job
that fits exactly is a job that gets killed at the wall after burning its whole
runtime for nothing.

This helps reduce fragmentation and cluster footprint without abruptly interrupting running work.

Autoconsolidation also includes safeguards such as:

- **cooldown** before repeated actions
- **hysteresis** so it does not flap on transient conditions
- **minimum runtime** before newly started engines are phased out again
- **minimum engine count** per partition

If there are still eligible queued jobs, consolidation may be skipped so that useful capacity is not removed too aggressively.

Headless managers expose the same toggle through an identity- and state-checked
control RPC (requires version 0.2.8 or newer). Inspect `zscontrol status` and
manager health first, then explicitly supply the observed UUID and old setting:

```bash
zscontrol --instance MY_PRIMARY consolidate on --expect off --manager-uuid OBSERVED_UUID
```

This changes only the existing demand-aware consolidation policy; it does not
cancel a job, change its runtime, or resize an allocation. Running jobs drain
normally before an excess engine stops. Forwarders, in-progress handovers,
stale identities and changed old settings are rejected; inspect a lost response
before retrying. The setting is retained through subsequent manager handovers.
The control and retirement policy are shared by Spider and Snellius.

### Autogrow (`g`)

Autogrow does the opposite of consolidation: it starts new Slurm-backed engines when the current eligible queue suggests that more capacity is really needed.

The autogrow controller:

- periodically inspects the queue
- only considers jobs that are actually eligible apart from lacking engine capacity
- takes the current reserved resources on running engines into account
- takes already queued-but-not-yet-started engines into account
- picks a preferred partition/node profile based on the configured partition preferences
- honors a cooldown to avoid overshooting
- respects the configured maximum number of compute nodes
- applies the configured walltime and backfill policy, then checks the actual
  remaining allocation time before placing work

This means autogrow is not triggered by every pending job. It is triggered by positive unmet CPU or memory demand, even below one node, after considering:

- work already fitting on current engines
- work that will fit on already queued engines
- global archive/active/dcache constraints

So if jobs are pending only because a global storage quota is full, autogrow will not solve that problem by launching more nodes.

A small eligible backlog can unlock a large downstream DAG and must not wait
for a full node's worth of jobs to accumulate behind long-running work. Already
queued capacity still suppresses duplicate growth. Consolidation, cooldowns and
the node cap remain separate controls; enabling growth does not disable them.

#### Maintenance windows

ZSlurm does **not** try to work out when the next maintenance window starts. On
Snellius it cannot: `PrivateData=reservations` hides the records from normal
users, and `sbatch --test-only` is not a usable substitute, because it reports the
*priority-ordered* start time rather than the backfill one. Measured on
2026-07-21, with 451 idle nodes in `genoa`, `--test-only` answered
`2026-07-25T15:10` for 5 minutes, 12 hours, 1 day and 5 days alike, while a real
1-hour job started immediately — so there is no duration-dependent jump to search
for, and an inference built on it concludes "no maintenance" precisely when a
window is imminent.

With the Snellius maintenance policy, pilot allocations are submitted with both
a maximum and a minimum walltime:

```text
sbatch -t 5-00:00:00 --time-min 12:00:00 ...
```

Slurm may shorten `TimeLimit` to let the allocation backfill; this does not
guarantee an immediate start or bypass priority. The resulting boundary is
`StartTime + TimeLimit`. Below `maintenance_time_min_sec` (default **12 hours**)
the job stays queued
instead: a shorter engine spends more on startup and drain than it gives back,
and the capacity comes back by itself once the window has passed. Autogrow
workers and manually requested engines use this policy; separately submitted
manager allocations have their own walltime settings.

Spider instead enables a workload-aware floor for **compute autogrow only**.
It uses the larger of the configured useful minimum (12 hours), the longest
runnable request plus headroom, and the end-of-life drain horizon plus headroom.
Headroom defaults to one hour and is never less than the job placement margin.
With the default 12-hour drain horizon, six-hour work therefore permits a
13-hour allocation; 48-hour work requires at least 49 hours. Dependencies that
are not ready and jobs exceeding the maximum pilot runtime do not raise this
floor. When the floor reaches the requested maximum, `--time-min` is omitted
and the full duration is required. The maximum, core/memory request, scientific
job runtimes and existing demand/cap/idle guards are unchanged. Actual job
placement still checks remaining time, including after the queue changes.
Manual/staging Spider pilots and Snellius defaults are unchanged; the new
policy is separately configurable on either site.

There is also separate staging/archive-oriented autogrow behavior for `staging` engines when archive jobs are waiting.

### How these features work together

These mechanisms are designed to complement each other:

- **Memory-aware filling** improves packing on existing nodes
- **Last-in/first-out** encourages deep-first progression through job graphs
- **Global storage counters** prevent overcommitting shared staging/storage resources
- **Autoconsolidate** shrinks excess engine capacity when demand drops
- **Autogrow** adds engines only when demand remains after the above constraints are considered

For workflows with many samples and many dependent steps, this combination tends to favor **finishing active samples well** rather than **starting too many fresh samples too early**.

## Job classes and Slurm partitions

ZSlurm distinguishes between three **manager-side job classes**:

- **`compute`**
- **`archive`**
- **`gpu`**

These are ZSlurm scheduling categories, not necessarily the same thing as the raw Slurm partition names on your cluster.

### `compute`

Use `-p compute` in `zsbatch` for normal CPU/memory-driven work such as:

- **Data processing**
- **Analysis jobs**
- **Workflow steps that primarily consume compute resources**

These jobs are scheduled onto manager-side **compute engines**.

### `archive`

Use `-p archive` in `zsbatch` for jobs that are primarily about:

- **Data staging**
- **Transfers into or out of archive storage**
- **Archive-oriented cleanup or movement workflows**

These jobs are scheduled onto manager-side **archive engines**.

### How this maps to Slurm partitions

When you start engines from the interactive manager:

- starting a normal Slurm partition creates manager-side **compute** engines
- starting the configured **staging partition** creates manager-side **archive** engines
- starting the configured **GPU partition** creates manager-side **GPU** engines

So there are two layers:

- **ZSlurm job partition**: `compute`, `archive` or `gpu`
- **Slurm engine partition**: for example `genoa`, `rome`, `fat_genoa`, or `staging`

This is why `staging` appears in the manager UI and cluster configuration, while `compute` and `archive` appear in `zsbatch` and internal scheduling.

Use `archive` for work requiring that configured backend. dCache and S3
transfers can run on compute engines; Spider disables staging by default.

## GPU jobs

One maintained manager can own `compute`, `archive` and `gpu` workers. The
GPU implementation was ported from the CCS/DeepConsensus checkout, retaining
main's dependencies, retry-safe grants, leases, portable pilot launchers and
explicit handover. It does **not** merge two existing populated managers or
automatically replace their running binaries.

```bash
zsbatch --instance MY_MANAGER -p gpu --gpus 1 -c 4 --mem 131072 \
  -t 04:00:00 -- ./deepconsensus.sh
```

GPU jobs default to one device; CPU/archive jobs default to zero. Positive
`--gpus` requires `-p gpu`. A GPU job is placed only when its devices, CPU,
memory, scratch requirements, dependencies and estimated runtime all fit on
one GPU worker. Two GPUs on different workers cannot satisfy a two-GPU job.
CPU demand never requests a GPU worker. CPU and GPU worker caps are separate.

Physical routes and allocation shapes remain site configuration. Spider's
default GPU route is `gpu_a100_22c`; Snellius's is `gpu_a100` (override it, for
example with `gpu_h100`, together with that partition's profile). Absent routes
are disabled when `sinfo` discovery succeeds. No GPU allocation shape is
guessed: configure `cores`, `mem_gb`, `gpus`, and optionally `gpu_type` before
starting workers. This is the allocation used by the existing Spider CCS run:

```yaml
gpu_partition: gpu_a100_22c
autogrow_max_gpu_nodes: 5
consolidation_min_gpu_engines: 0
node_profiles:
  # Merge with existing CPU profiles; do not remove them.
  gpu_a100_22c:
    cores: 44
    mem_gb: 320
    gpus: 2
    gpu_type: a100
```

`mem_gb` follows existing node-profile units (GiB; converted to Slurm MB).
GPU workers use the configured CPU/RAM request and `--gres=gpu:a100:2`, not
the CPU pool's partial/exclusive shape. Review core-to-memory requirements at
the site. Optional `has_ssd`/`ssd_gb` describe GPU-worker scratch. The shipped
site templates set `autogrow_max_gpu_nodes: 0`: GPU growth is opt-in, demand
backed, counts already queued/held allocations, has its own cooldown, and
never grows from dependency/storage/transfer-blocked or unserviceable jobs.
Short remaining pilot leases do not hide long-job demand. Backfill shortening
must retain enough time for the requests being placed.

Manual `d` accepts the logical `gpu` route or a configured physical partition.
`o gpu +1` drains one GPU worker; `o gpu -1` restores one. Automatic GPU
consolidation preserves every busy worker (including unacknowledged grants),
retires only unused idle capacity, and cancels surplus queued tasks with a
PENDING-only Slurm filter. It never cancels a whole array or releases an
administrative hold. New demand restores automatic drains, not manual drains.

Devices are reserved at grant time, retained while a start is deferred, and
released after completion/failure or a rejected start. Lost replies replay
the same device assignment; retrying GPU submissions is idempotent. Start
acknowledgements must match the exact grant's device IDs, so a stale
pre-handover grant cannot start on a subsequently reassigned device. CPU/RAM
lease resizing does not release devices. Explicit handover preserves running
GPU assignments and requeues unstarted grants, and rejects CPU-only targets.
The separate legacy CCS checkpoint/restart mechanism is not transplanted.

The chief sets worker-local `CUDA_VISIBLE_DEVICES`, NVIDIA visibility and
Apptainer/Singularity overrides; CPU children see no GPUs. This is cooperative
visibility, **not** a separate per-job device cgroup/security boundary.
`zsqueue --json`, `zsnodes --json`, detailed job RPCs, queue statistics and
the manager UI expose device counts/assignments. Legacy 14/15-field job and
25-field node RPC rows remain unchanged unless GPU fields are requested.

Install the same GPU-capable release for manager, chiefs and GPU clients before
rollout. New GPU clients/chiefs refuse old managers. Old CPU client/registration
arguments remain valid; the positional GPU protocol of the old CCS fork is
explicitly rejected (its GPU-count argument occupied main's idempotency-token
position). Do not deploy by overwriting a checkout that live processes import.
Qualify the release separately, then plan a staged rollout; mock scheduling
tests do not establish CUDA/container/hardware correctness.

The source GPU helper used for the port had SHA256
`468dbb3d3197778254322b77675e0c41c267e8788d557672df914c8b8a4f1d9d`;
it came from the uncommitted CCS checkout inspected on 13 October 2026.
