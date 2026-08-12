# ZSlurm

ZSlurm is a lightweight batch layer on top of Slurm. It provides a local manager process plus CLI tools that let you submit and monitor fine-grained jobs while ZSlurm manages engines on local or Slurm-backed nodes.

## Installation

### Fresh install with conda

1. Create the environment:

```bash
conda env create -f env.yaml
```

2. Activate it:

```bash
conda activate clustersnake
```

3. Install ZSlurm into that environment:

```bash
python -m pip install .
```

The environment file includes the base runtime dependencies for ZSlurm, including `pyyaml` and `tabulate`.

`ipyparallel` is optional. If you want the manager UI to query and display IPython parallel queue statistics, install it separately:

```bash
python -m pip install .[ipyparallel]
```


## Updating an existing installation

```bash
git pull
conda activate clustersnake
python -m pip install .
```

If you installed ZSlurm into multiple environments, repeat the install step in each environment.

## Quick start

1. Start the manager UI:

```bash
zslurm
```

2. Start either:

- **A local engine** with `s`
- **Slurm-backed engines** with `d`

3. Submit jobs with `zsbatch`.

4. Inspect jobs with `zsqueue` and nodes with `zsnodes`.

## Interactive manager controls

Inside the `zslurm` curses UI:

- **`s`**: Start a local engine
- **`x`**: Stop a local engine
- **`d`**: Start Slurm engines for a partition
- **`c`**: Stop Slurm engines
- **`a`**: Toggle automatic consolidation of engines
- **`o`**: Phase out engines by node name so they stop accepting new jobs
- **`p`**: Prioritize jobs whose job names match a pattern
- **`n`**: Deprioritize jobs whose job names match a pattern
- **`l`**: Toggle last-in/first-out job preference
- **`1`**: Set archive staging quota
- **`2`**: Set active storage quota
- **`3`**: Set dcache staging quota
- **`4`**: Manually set archive currently-used capacity
- **`5`**: Manually set active currently-used capacity
- **`6`**: Manually set dcache currently-used capacity
- **`m`**: Set the scheduling search window used for memory-aware filling
- **`g`**: Toggle autogrow and set the maximum number of compute nodes it may add
- **`u`**: Refresh the ncurses screen
- **`q`**: Stop the manager

When starting Slurm engines, entering `staging` maps to archive-oriented engines used for transfer/archive work.

On Snellius, starting Slurm engines also offers an SSD scratch-node reservation prompt. For regular partitions, the UI asks for:

- **Partition**
- **Number of engines**
- **Optional Slurm feature/constraint**
- **Cores per engine**
- **Runtime**

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

A phased-out engine holds its node until its **last** job ends. When that job still
has hours to run, the rest of the node would otherwise sit idle for those hours, so
the engine may still accept work that is certain to finish first -- it then stops at
about the moment it would have stopped anyway.

This is deliberately conservative and falls back to refusing everything, exactly as
before, unless all of these hold:

- `phaseout_backfill_enable` is on
- the engine is **not** draining because its node walltime is nearly up -- that one is
  already racing a hard kill
- the engine still has running work, since letting an empty one die is the point
- at least `phaseout_backfill_min_window_sec` (default 1 h) remains after subtracting
  `phaseout_backfill_margin_sec` (default 30 min)

The window is also capped by the node's own remaining walltime minus the same margin,
and every job still has to satisfy `job_walltime_buffer_sec` on top, so a backfilled
job needs roughly 45 minutes of slack before it is allowed to start.

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
- caps every new engine at the next applicable Slurm `MAINT` reservation and
  gives the job a matching Slurm deadline

This means autogrow is not triggered by every pending job. It is triggered when there is meaningful unmet demand after considering:

- work already fitting on current engines
- work that will fit on already queued engines
- global archive/active/dcache constraints

So if jobs are pending only because a global storage quota is full, autogrow will not solve that problem by launching more nodes.

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

Instead every allocation is submitted with both a maximum and a minimum walltime:

```text
sbatch -t 5-00:00:00 --time-min 12:00:00 ...
```

Slurm then starts the job in the first slot that fits and truncates `TimeLimit`
to exactly the time available before the next reservation, so allocations are
self-sizing and the boundary is simply `StartTime + TimeLimit` of the resulting
job. Below `maintenance_time_min_sec` (default **12 hours**) the job stays queued
instead: a shorter engine spends more on startup and drain than it gives back,
and the capacity comes back by itself once the window has passed. Autogrow
workers, manually requested engines and the coordinator's manager node all go
through the same path.

There is also separate staging/archive-oriented autogrow behavior for `staging` engines when archive jobs are waiting.

### How these features work together

These mechanisms are designed to complement each other:

- **Memory-aware filling** improves packing on existing nodes
- **Last-in/first-out** encourages deep-first progression through job graphs
- **Global storage counters** prevent overcommitting shared staging/storage resources
- **Autoconsolidate** shrinks excess engine capacity when demand drops
- **Autogrow** adds engines only when demand remains after the above constraints are considered

For workflows with many samples and many dependent steps, this combination tends to favor **finishing active samples well** rather than **starting too many fresh samples too early**.

## Snellius-specific behavior and cluster configuration

The code still ships with **Snellius-oriented fallback defaults**, but the main cluster-specific behavior is now configurable through `~/.zslurm/config.yaml`.

### Current fallback defaults

If you do not set any cluster-specific config, ZSlurm still falls back to behavior that is convenient for Snellius-like environments:

- **Default interactive partition**:
  - on Snellius: `genoa`
  - elsewhere: `normal`
- **Default staging/archive Slurm partition**: `staging`
- **Default SSD feature name**: `scratch-node`
- **Default autogrow partition preferences**:
  - `genoa`
  - `rome`
  - `fat_genoa`
  - `fat_rome`
- **Default node profiles**:
  - `genoa`: 192 cores, 336 GB
  - `rome`: 128 cores, 224 GB
  - `fat_genoa`: 192 cores, 1440 GB
  - `fat_rome`: 128 cores, 960 GB

So the code is still friendly to Snellius out of the box, but these are no longer fixed assumptions.

### Config keys now supported

The manager now reads the following cluster-policy keys from `~/.zslurm/config.yaml`:

- **`default_partition`**
  - default partition shown in the interactive `d` prompt
- **`staging_partition`**
  - Slurm partition name used for archive/staging-oriented engines
- **`enable_ssd_prompt`**
  - whether the interactive UI asks about SSD-capable nodes
- **`ssd_feature_name`**
  - Slurm feature/constraint string used for SSD-capable nodes
- **`autogrow_prefer_partitions`**
  - ordered partition preference list for compute autogrow
- **`node_profiles`**
  - per-partition `cores` and `mem_gb` used by autogrow planning
- **`autogrow_fallback_partition`**
  - partition to fall back to if no preferred autogrow partition scores better
- **`autogrow_fat_partitions`**
  - partitions treated as large/fat nodes during autogrow heuristics
- **`archive_job_partitions`**
  - job partitions that should count toward staging/archive autogrow pressure
- **`staging_autogrow_base_nodes`**
  - number of staging nodes to target when archive pressure is nonzero
- **`staging_autogrow_burst_threshold`**
  - pending archive-job threshold for switching to burst staging behavior
- **`staging_autogrow_burst_nodes`**
  - maximum target staging-node count under burst conditions
- **`maintenance_window_enable`**
  - submit every manual or automatic allocation with a `--time-min` floor, so Slurm
    shortens it to fit before the next maintenance window (see *Maintenance windows*)
- **`maintenance_time_min_sec`**
  - that floor: the shortest allocation still worth having. Below it the job stays
    queued until the window has passed, instead of starting and dying immediately
    (default `43200`, i.e. 12 hours)
- **`job_walltime_buffer_sec`**
  - slack a job's `reqtime` must leave inside an engine's remaining walltime before
    it may start there (default `900`)
- **`phaseout_backfill_enable`**
  - let a phasing-out engine still accept work that finishes before its last job does
- **`phaseout_backfill_margin_sec`**
  - safety margin subtracted from both the drain estimate and the node walltime when
    sizing that backfill window (default `1800`)
- **`phaseout_backfill_min_window_sec`**
  - do not backfill at all unless at least this much is reclaimable (default `3600`)
- **`consolidation_drain_horizon_sec`**
  - residual work at which an engine counts as fully loaded in the keep-score
    (default `21600`, i.e. 6 hours)

### What is now config-driven

The following behavior now follows config instead of being hardcoded:

- **Interactive Slurm engine startup defaults**
- **Which partition name is treated as staging/archive-oriented**
- **Whether SSD-capable nodes are offered in the UI**
- **Which Slurm feature string is used for SSD-capable nodes**
- **Autogrow partition ordering**
- **Autogrow node-size assumptions**
- **Which job partitions count as archive/staging pressure**
- **Staging autogrow thresholds and target node counts**

### Example config

For a Snellius-style setup, `~/.zslurm/config.yaml` can look like:

```yaml
default_partition: genoa
staging_partition: staging
enable_ssd_prompt: true
ssd_feature_name: scratch-node

autogrow_max_compute_nodes: 40
autogrow_fallback_partition: genoa
maintenance_window_enable: true
maintenance_time_min_sec: 43200
autogrow_fat_partitions:
  - fat_genoa
  - fat_rome

autogrow_prefer_partitions:
  - [genoa, true]
  - [genoa, false]
  - [rome, true]
  - [rome, false]
  - [fat_genoa, true]
  - [fat_rome, true]

node_profiles:
  genoa:
    cores: 192
    mem_gb: 336
  rome:
    cores: 128
    mem_gb: 224
  fat_genoa:
    cores: 192
    mem_gb: 1440
  fat_rome:
    cores: 128
    mem_gb: 960

archive_job_partitions:
  - archive
  - staging

staging_autogrow_base_nodes: 1
staging_autogrow_burst_threshold: 50
staging_autogrow_burst_nodes: 4
```

## Storage quotas as a global resource monitor

Besides CPU, memory, runtime, and partition constraints, ZSlurm can also schedule against three instance-wide storage counters:

- **Archive**
- **Active**
- **DCache**

These counters are managed globally by the running `zslurm` instance and are shown in the manager status panel as:

- **Archive**: `inuse / total`
- **Active**: `inuse / total`
- **DCache**: `inuse / total`

This is useful when jobs are limited not just by compute resources, but also by shared staging/storage capacity. For example:

- **Archive staging**: temporary space used while copying data into or out of archive storage
- **Active storage**: space used on fast project or working storage
- **DCache staging**: temporary or managed data movement through dcache-backed storage

The scheduler checks these counters before a job starts. A job can stay queued even if CPU and memory are available when one of these global capacities would be exceeded.

### How jobs affect the counters

Each job can declare storage effects through `zsbatch`:

- **`--arch-use-add`** / **`--arch-use-remove`**
- **`--active-use-add`** / **`--active-use-remove`**
- **`--dcache-use-add`** / **`--dcache-use-remove`**

The semantics are:

- **`*-use-add`**: reserve/add that amount when the job starts
- **`*-use-remove`**: release/remove that amount when the job finishes successfully

This lets you model several common workflows:

- **Temporary staging job**: add space at start and remove the same amount at the end
- **Producer job**: add space at start and remove nothing, so the global in-use counter stays higher after success
- **Cleanup job**: remove space at the end without adding new space at the start

If a job fails, ZSlurm assumes the start reservation should be released again.

### Setting the total capacities in the UI

Inside `zslurm`:

- **`1`** sets the total archive capacity in GB
- **`2`** sets the total active capacity in GB
- **`3`** sets the total dcache capacity in GB

Advanced/manual correction keys:

- **`4`** sets the currently used archive capacity in GB
- **`5`** sets the currently used active capacity in GB
- **`6`** sets the currently used dcache capacity in GB

Those manual in-use overrides are useful if you want to recover from a situation where the ZSlurm instance lost track of some storage usage (e.g. a restart or a crash).

### Example `zsbatch` usage

Temporary archive staging during a job:

```bash
zsbatch -p compute --arch-use-add 500 --arch-use-remove 500 -- python stage_to_archive.py
```

Create 200 GB of new active-storage output that remains allocated after success:

```bash
zsbatch -p compute --active-use-add 200 -- python produce_results.py
```

Cleanup 300 GB from dcache after a successful cleanup job:

```bash
zsbatch -p compute --dcache-use-remove 300 -- python cleanup_dcache.py
```

In practice, this means ZSlurm can act as a lightweight global resource monitor for shared storage bottlenecks, not just a CPU/memory scheduler.

### Limiting concurrent dCache transfers

The durable dCache counter above measures storage in GB. Transfer concurrency is
a separate, transient resource with independent download and upload pools.
Configure their instance-wide capacities in `~/.zslurm/config.yaml`:

```yaml
dcache_download_slots: 4
dcache_upload_slots: 2
```

A Snakemake transfer job requests a directional slot:

```python
resources:
    dcache_download_slots=1  # use dcache_upload_slots=1 for outbound data
```

The native executor passes these requests as job metadata. ZSlurm reserves them
while the job is `ASSIGNED` or `RUNNING` and releases them on completion,
failure, requeue, cancellation, or loss of the assigned worker. A full download
pool does not block an upload, and vice versa. Leave these resources out of
Snakemake's global `resources:` capacity list: Snakemake can then submit the
whole runnable DAG while ZSlurm enforces the cross-node limits.

For compatibility, the old manager setting `dcache_transfer_slots` supplies the
default for both directional maxima when the new settings are absent. A legacy
per-job `dcache_transfer_slots` request consumes the requested amount from both
pools, conservatively throttling clients whose direction is unknown. Updated
plugins also send a legacy fallback value during rolling upgrades, so an old
manager still throttles the job; a directional manager ignores that fallback
whenever download or upload metadata is present. Do not configure both legacy
and directional Snakemake resources explicitly on one job.

Limits can also be changed at runtime (in-use reservations are left intact):

```bash
zscontrol transfer-slots --download 4 --upload 2
```

## Dynamic CPU and memory leases

A running child job can temporarily return CPU cores and memory to its local
`zslurm_chief`, then wait to reacquire them for a later phase. The underlying
Slurm allocation is not resized: the lease controls only zslurm's internal
multiplexing of jobs within that allocation.

This is intended for multi-phase jobs, for example a highly parallel alignment
followed by a lightly threaded merge:

```bash
# The job initially holds its complete zsbatch request.
zslurm_lease status

run_alignment

# Keep only the resources needed by the serial phase.
zslurm_lease set --cores 2 --mem-gb 12
run_merge

# A later increase blocks until the local chief can grant all of it.
zslurm_lease set --cores 16 --mem-gb 48 --wait 3600
run_parallel_postprocessing
```

Targets are absolute, not relative. Repeating the same `set` command is safe:
it cannot release the same cores or memory twice. A target may not exceed the
job's original `zsbatch` request.

Parallel consumers within one child job can return their individual shares
without racing on an absolute target:

```bash
consumer_a
zslurm_lease release --cores 2 --mem-gb 4 --release-id consumer-a
```

`release` amounts are relative to the holding at the moment the chief handles
the request. The update is atomic. A stable `--release-id` makes retries
idempotent; reusing that id with different amounts is rejected. The same
observed-memory and minimum-CPU safety floors apply as for an absolute shrink.

### Reacquisition and fairness

When an increase cannot be granted immediately:

- the request enters a FIFO queue on the local chief
- the chief stops admitting new child jobs on that engine
- already-running child jobs are allowed to finish
- the request is granted all-or-nothing once both CPU and memory fit
- `zslurm_lease set` remains blocked until grant or `--wait` expires

There is no preemption. A timeout leaves the job's previous lease unchanged.

### Memory safety

The chief monitors the complete process tree's proportional set size (PSS). It
will not shrink a lease below observed memory plus configurable headroom. The
response reports when this safety floor adjusted the requested target.

Relevant manager/chief configuration keys are:

```yaml
lease_min_cpu: 0.1
lease_memory_headroom_fraction: 0.20
lease_memory_headroom_mb: 512
# Optional; defaults to this chief's node-local scratch, then TMPDIR or /tmp.
lease_socket_dir: /some/node-local/path
```

CPU leases are cooperative scheduling reservations; they do not change an
application's thread count or create a new Slurm step/cgroup. The pipeline must
set each tool's thread arguments consistently with the lease it acquires.

### Local protocol and environment

The chief injects `ZSLURM_LEASE_SOCKET`, `ZSLURM_LEASE_TOKEN`,
`ZSLURM_JOB_ID`, `ZSLURM_LEASE_MAX_CORES`, and
`ZSLURM_LEASE_MAX_MEM_MB` into every child. The socket is node-local and mode
`0600`; every request additionally requires the job-specific capability token.
Jobs should use `zslurm_lease` rather than speaking the JSON protocol directly.

Dynamic leases are backward compatible: a job that never calls
`zslurm_lease` holds its original resources until completion. Deploy the
manager before restarting chiefs because new chiefs call the manager's
`resize_running_job` RPC. Existing chiefs and already-running jobs cannot gain
lease support in place.

## Configuration and instances

ZSlurm stores user configuration under `~/.zslurm`.

- **Global config file**: `~/.zslurm/config.yaml`
- **Per-instance files**: `~/.zslurm/instances/*.yaml`

Each running `zslurm` manager creates its own instance file containing runtime connection details such as:

- **`bind_host`**
- **`advertise_host`**
- **`base_port`**
- **`rpcpath`**

CLI tools auto-detect instances if there is only one running instance. If there are multiple, select one explicitly.

### Selecting an instance

- **Per command**: use `--instance NAME`
- **Session-wide**: set `ZSLURM_INSTANCE=NAME`

## Output files

ZSlurm writes several useful files in the working directory of the manager or commands:

- **`zslurm-<jobid>.out`**: stdout/stderr log for a job
- **`report-YYYY-MM-DD_HH-MM.tsv`**: per-job resource usage and runtime summary
- **`cluster.log`**: manager log output
- **`node_usage-YYYY-MM-DD_HH-MM.tsv`**: periodic node usage snapshots when node reports are enabled

### `report-*.tsv`

This file is written by the manager and contains one row per finished job.

The filename prefix is configurable via:

- **`reports_file_prefix`**

By default the file contains columns such as:

- **Job identity**: `jobname`, `comment`, `jobid`, `machine`
- **Exit/result**: `retcode`
- **Timing**: `starttime`, `endtime`, `runtime`, `montime`
- **Reserved resources**: `cores_reserved`, `mem_reserved_mb`
- **Workflow annotations**: `input_mb`, `output_file`
- **Sampled process memory/cpu percentiles**: `uss_*`, `rss_*`, `vms_*`, `cpu_percentage_*`
- **Thread and CPU summaries**: `avg_cpu_percentage`, `nthreads_avg`, `nthreads_max`, `user`, `system`
- **Memory and IO summaries**: `maxrss`, `mon_user`, `mon_system`, `iowait`, `read_count`, `write_count`, `read_bytes`, `write_bytes`
- **Memory trace**: `memory_over_time`

This is the main input for `zsstats`.

### `cluster.log`

This is the manager log file written by `zslurm` itself.

Each line is timestamped and contains operational messages such as:

- **Engine startup and shutdown**
- **Autogrow and consolidation actions**
- **Job completion/failure messages**
- **Manager/server startup information**
- **Exceptions and warnings**

Format-wise, it is a plain text log with lines of the form:

- **`HH:MM:SS: message...`**

### `node_usage-*.tsv`

This file contains periodic per-engine snapshots produced by the manager while node reporting is enabled.

The filename prefix is configurable via:

- **`node_reports_file_prefix`**

The file contains columns such as:

- **Timestamp and identity**: `ts_iso`, `engine_id`, `cluster_id`, `partition`, `status`
- **Manager state**: `managed`, `stopping`
- **Capacity and usage**: `cores`, `totmem_mb`, `cpu_pct`, `mem_pct`, `load`
- **System metrics**: `sys_cpu_busy_pct`, `sys_iowait_pct`
- **Reserved resources**: `res_cores_reserved`, `res_mem_reserved_mb`
- **Job count**: `jobs_running`
- **SSD state**: `has_ssd`, `ssd_total_gb`, `ssd_used_gb`, `res_ssd_reserved_gb`
- **Timing**: `timeleft_sec`, `uptime_sec`
- **Pending queue pressure for that partition**: `pending_jobs`, `pending_cores`, `pending_mem_mb`

This file is intended for downstream inspection with `node_usage_viewer.py`.

Node usage reporting is controlled from the manager config. Relevant keys include:

- **`reports_file_prefix`**
- **`node_reports_enable`**
- **`node_reports_file_prefix`**
- **`node_reports_include_partitions`**

## Compute vs archive partitions

ZSlurm distinguishes between two **manager-side job classes**:

- **`compute`**
- **`archive`**

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

So there are two layers:

- **ZSlurm job partition**: `compute` or `archive`
- **Slurm engine partition**: for example `genoa`, `rome`, `fat_genoa`, or `staging`

This is why `staging` appears in the manager UI and cluster configuration, while `compute` and `archive` appear in `zsbatch` and internal scheduling.

## Main CLI commands

### `zsbatch`

Submit a job to ZSlurm.

Useful options:

- **`-c, --cpus-per-task`**: requested CPU cores
- **`--mem`**: requested memory in MB
- **`-t, --time`**: requested runtime
- **`-p, --partition`**: target partition
- **`-n, --ntasks`**: number of tasks
- **`-d, --dependency`**: Slurm-style dependency string (`after`, `afterany`, `afterok`, `afternotok`, or `singleton`)
- **`-J, --job-name`**: job name override
- **`--requeue`**: allow requeue after failure/cancel
- **`--arch-use-add/--arch-use-remove`**: archive storage accounting in GB
- **`--dcache-use-add/--dcache-use-remove`**: dcache storage accounting in GB
- **`--active-use-add/--active-use-remove`**: active storage accounting in GB
- **`--limit-threads`**: limit thread-related environment variables
- **`--info-input-mb`**: annotate input size in `report-*.tsv`
- **`--info-output-file`**: annotate primary output path in `report-*.tsv`
- **`--priority`**: integer scheduling priority; higher values run first (default 100)
- **`--dcache-download-slots`**: transient download concurrency requested by the job
- **`--dcache-upload-slots`**: transient upload concurrency requested by the job
- **`--ssd-use`**: SSD requirement mode (`no`, `possible`, `required`)
- **`--ssd-gb`**: requested SSD capacity in GB
- **`--instance`**: submit to a specific ZSlurm instance
- **`--parsable`**: print only job id / parse-friendly output

The storage flags are interpreted by the manager as instance-wide resource accounting. Jobs may stay queued until enough archive/active/dcache capacity is available.

Dependencies are enforced by the ZSlurm manager before any CPU, memory, SSD,
storage-budget, or dCache transfer resources are reserved. Examples:

```bash
first=$(zsbatch --parsable -- preprocess.sh)
zsbatch --dependency="afterok:${first}" -- analyse.sh
zsbatch --dependency="afterany:${first}" -- cleanup.sh
zsbatch --dependency="afterok:${first}?afternotok:12345" -- fallback.sh
zsbatch --job-name nightly-import --dependency=singleton -- import.sh
```

Within one dependency string, `,` means all conditions must be satisfied and
`?` means any condition may release the job; the separators cannot be mixed.
`after:JOBID+MINUTES` adds a delay after the predecessor starts (or is cancelled
before starting). A bare job id is treated as `afterany:JOBID`.

Dependency job ids must belong to the same running ZSlurm instance and must
still be active or known in its terminal history when the dependent job is
submitted. A dependency that becomes impossible (for example `afterok` after a
failed predecessor) remains `PENDING` and never runs, matching Slurm's default
behaviour without `--kill-on-invalid-dep`. Its `dependency_state` and
`dependency_reason` are available from `list_jobs_detailed`; manager status also
reports counts for waiting and never-satisfiable dependencies. `expand`, job
arrays, cross-instance dependencies, and changing dependencies after submission
are not supported.

The `--partition` flag here refers to the **ZSlurm job partition**:

- **`compute`**: regular compute work
- **`archive`**: archive/staging-oriented work

Example:

```bash
zsbatch -c 4 --mem 8000 -t 02:00:00 -p compute -- python my_script.py
```

Example with storage accounting and thread limiting:

```bash
zsbatch -c 8 --mem 32000 -t 08:00:00 -p compute \
  --active-use-add 150 \
  --limit-threads 8 \
  --info-output-file results/output.tsv \
  -- python workflow_step.py
```

### `zsqueue`

Show queued/running/completed jobs.

Useful options:

- **`--all`**: include completed jobs
- **`--done`**: show only completed jobs
- **`--parseable`**: emit TSV output
- **`--instance NAME`**: query only a specific instance

By default, `zsqueue` queries all discovered instances. In parseable mode it prints:

- **Instance name**
- **Job id**
- **Partition**
- **Job name**
- **State**
- **Runtime**
- **Requested cores**
- **Assigned node**
- **Observed CPU/memory usage**
- **Active/archive/dcache deltas for that job**
- **Working directory**

If a job has not been placed on a node yet, the node field is shown as `("Resources")` internally and appears as a placeholder in queue output.

### `zsnodes`

Show engines/nodes known to ZSlurm.

Useful options:

- **`--all`**: include queued nodes
- **`--parseable`**: emit TSV output
- **`--instance NAME`**: query only a specific instance

`zsnodes` is the main per-engine monitoring tool. Its output includes:

- **ZSlurm engine id**
- **Backing Slurm job id**
- **Partition**
- **Engine cores and memory**
- **Runtime and remaining time**
- **Number of jobs on the engine**
- **CPU usage, memory usage, load, system CPU busy, IO wait**
- **Reserved CPU and reserved memory**
- **SSD availability, total SSD, used SSD, reserved SSD**
- **Engine status**

This makes `zsnodes` the best tool for checking whether engines are full, idle, unmanaged, stopping, or carrying SSD-constrained work.

### `zscancel`

Cancel one or more jobs.

Useful options:

- **`--requeue`**: requeue cancelled running jobs
- **`--instance NAME`**: target specific instance(s)

If no job ids are given on the command line, `zscancel` reads them from stdin.

Examples:

```bash
zscancel 12345 12346
```

```bash
zsqueue --parseable | tail -n +2 | cut -f2 | xargs zscancel
```

## Monitoring and reporting utilities

### `zsqueue_stats`

Show aggregated queue totals across one or more instances.

Useful options:

- **`--json`**: JSON output
- **`--partition`**: filter to one partition
- **`--instance NAME`**: restrict to one or more instances

This command summarizes:

- **Running jobs/cores/memory**
- **Pending jobs/cores/memory**
- **Counts by partition**
- **Queue states**
- **Running and queued engine counts by partition**

Use it when you want a compact operational overview rather than a full per-job listing.

### `zsoccupancy`

Summarize partition occupancy from `scontrol show nodes`.

Useful options:

- **`--input FILE`**: read captured `scontrol` output from a file
- **`--input -`**: read from stdin
- **`--json`**: JSON output
- **`--parseable`**: TSV output

By default it calls `scontrol` directly.

### `zsstats`

Aggregate one or more `report*.tsv` files for downstream analysis.

Useful options:

- **`--files`**: input glob(s), default `report*.tsv`
- **`--group`**: grouping fields, default `jobname,cwd`
- **`--json`**: JSON output
- **`--out`**: output file path
- **`--include-failed`**: include failed jobs
- **Plot options**: `--plot-used-vs-reserved`, `--plot-reserved-vs-avg-core`, `--plot-mem-reserved-vs-rss`, `--plot-metric`

Example:

```bash
python zsstats --files 'report*.tsv' --group jobname --json
```

### `node_usage_viewer.py`

Generate an interactive HTML report from `node_usage-*.tsv` logs.

Example:

```bash
python3 node_usage_viewer.py \
  -i 'node_usage-*.tsv' \
  -o node_usage_report.html \
  --bin-sec 60
```

Useful options:

- **`-i, --input`**: input glob(s), repeatable
- **`-o, --output`**: output HTML file
- **`--bin-sec`**: aggregation bin size in seconds
- **`--include-partition`**: include only selected partitions
- **`--include-unmanaged`**: include unmanaged engines
- **`--include-stopping`**: include stopping engines
- **`--include-phasing-out`**: include engines with `PHASING_OUT` status

Notes:

- **Input requirement**: the logs must come from a recent ZSlurm run with node reports enabled
- **Output**: a standalone HTML file, no web server required
- **Computation**: fill rate is calculated as reserved divided by total resources per time bin

## Typical tool workflow

A common operational flow is:

- **Start the manager** with `zslurm`
- **Start engines** from the UI with `s` or `d`
- **Submit jobs** with `zsbatch`
- **Inspect job state and storage deltas** with `zsqueue`
- **Inspect engine pressure and reserved resources** with `zsnodes`
- **Get a compact queue summary** with `zsqueue_stats`
- **Cancel/requeue work** with `zscancel`
- **Analyze finished runs** with `zsstats` and `node_usage_viewer.py`

## Snakemake integration

Example profiles are included in:

- **`zslurm.yaml`**
- **`snakemake_profile.yaml`**

The newer `zslurm.yaml` example includes additional fields such as:

- **`--limit-threads {resources.limit_auto_threads}`**
- **`--info-input-mb {resources.input_mb}`**
- **`--info-output-file {output[0]}`**

To use a profile with Snakemake, copy one of these files to your Snakemake profile location and adapt paths such as `conda-prefix` to your own environment.

## Agent / programmatic interface

The commands above are built for a human at a terminal (interactive curses UI, pretty
tables). ZSlurm also ships a **machine-readable interface** so an autonomous agent — or any
script — can drive it without the TUI. See [`docs/zslurm-architecture.md`](docs/zslurm-architecture.md)
for how ZSlurm works and [`docs/agent-interface-plan.md`](docs/agent-interface-plan.md) for
the interface design and phasing.

- **Headless manager**: `zslurm --headless` runs the RPC servers + controller without the
  curses UI (for unattended/agent operation and testing). Flags: `--no-autogrow`,
  `--no-autoconsolidate`, `--enable-control`, `--control-token TOKEN`. It prints a one-line
  JSON banner with the instance endpoint, then runs until SIGTERM.
- **`zsstatus`**: one JSON snapshot an agent polls — health, the three storage budgets,
  scheduler mode, queue, an engine summary, and derived **alarms** (`budget_stall`,
  `no_engines`, `oversized_pending`, `near_oom`).
- **`zscontrol`**: a control plane mirroring the TUI keys. Reads (no token): `status`,
  `whatif`, `match`, `forecast` (will a planned DAG fit the budgets?), `jobs`,
  `autogrow-plan`. Gated writes (require `enable_control_rpc` + `control_token`): `budget`,
  `transfer-slots`, `lifo`, `context`, `autogrow`, `prioritize`/`deprioritize`, `recompute-inuse`,
  `grow`/`shrink`.
- **`--json`** on `zsqueue` and `zsnodes` emits numeric, schema-versioned rows;
  `zsqueue_stats`/`zsoccupancy`/`zsstats` already have `--json`. Exit-code contract across
  the agent clients: `0` ok, `2` transport/instance failure (retryable), `3` logical
  rejection (e.g. control disabled, bad token) — not retryable.
- New RPCs on the job server (gated writes behind `enable_control_rpc` + `control_token`,
  set in `~/.zslurm/config.yaml` or via the `--enable-control`/`--control-token` flags):
  `ping`/`health`, `get_status_json`, `match_jobs`, `whatif_budget`, `forecast_budget`,
  `list_jobs_detailed`, `get_autogrow_plan`, `set_budgets`, `set_transfer_limits`, `set_scheduler_mode`,
  `set_autogrow`, `prioritize`/`deprioritize`, `recompute_inuse_from_running`,
  `grow`/`shrink`. `submit_job` accepts optional `idempotency_key` and `priority`
  arguments for retry-safe, priority-aware submission.

### Claude Code skill

A ready-to-use **Claude Code skill** that teaches an agent to drive ZSlurm + Snellius is
bundled at [`.claude/skills/snellius-zslurm/`](.claude/skills/snellius-zslurm/) — it
auto-loads for anyone using Claude Code in this repository. To make it available globally
(across all your projects), copy it into your user skills directory:

```bash
cp -r .claude/skills/snellius-zslurm ~/.claude/skills/
```

The skill covers the three scheduling pillars (depth-first/LIFO, memory packing across
heterogeneous nodes, storage budgets), a Snellius node/SBU/quota reference, JSON schemas,
worked transcripts, and the safety rails (SBU/core-hour limits, idempotency, poll cadence).
