# Storage, transfers and dynamic resource leases

[Overview](../README.md) · [Getting started](getting-started.md) · [Snakemake](snakemake.md)

[Storage budgets](#storage-quotas-as-a-global-resource-monitor) · [Transfers](#limiting-concurrent-dcache-transfers) · [SSD](#local-ssd-placement-and-sizing) · [Leases](#dynamic-cpu-and-memory-leases) · [Intentional waits](#intentional-waits-and-live-job-activity)

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

These are declared accounting budgets, not a filesystem quota scan. Set their
totals and initial usage to match the participating workflows, and reconcile
usage when cleanup fails or files change outside those workflows.

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
s3_download_slots: 4
```

A Snakemake transfer job requests a directional slot:

```python
resources:
    dcache_download_slots=1  # use dcache_upload_slots=1 for outbound data
```

An S3 ingress job instead requests the independent S3 pool:

```python
resources:
    s3_download_slots=1
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

S3 requests use the same rolling-upgrade pattern: the executor sends native S3
metadata plus dCache-download and legacy fallbacks. A manager with
`s3_download_slots` capability charges only the S3 pool; the current older
manager charges the dCache download pool. Do not combine S3 and dCache transfer
resources explicitly on one job.

Limits can also be changed at runtime (in-use reservations are left intact):

```bash
zscontrol transfer-slots --download 4 --upload 2 --s3-download 4
```

## Local SSD placement and sizing

SSD capacity is per engine, separate from the instance-wide storage counters.
Choose a mode with `zsbatch --ssd-use` or a rule's `ssd_use` resource:

| Mode | Placement |
| --- | --- |
| `no` | No SSD requirement or reservation. |
| `possible` | May run on an ordinary engine; reserves `ssd_gb` when placed on SSD. The job needs a working shared-storage fallback. |
| `required` | Runs only on an SSD engine with enough unreserved capacity. |

Use `--ssd-gb` / `ssd_gb` for peak simultaneous local files, including input
copies, expanded data, sort/merge temporaries, indexes and outputs awaiting
publication, plus headroom. Final output size alone is insufficient.

A scratch-capable chief gives each child a private `ZSLURM_SCRATCH_DIR` and
sets `TMPDIR`, `TMP`, `TEMP` and `TEMPDIR` to it. Applications must direct their
intermediates there and publish durable results before finishing. CPU/memory
lease changes do not release the SSD reservation. See [site configuration](configuration.md)
for scratch detection and partial-pilot capacity limits, and the
[short-read example](snakemake.md#short-read-pipeline-from-input-to-released-storage)
for pipeline usage.

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

# Keep only the resources needed by the serial phase and name it in reports.
zslurm_lease set --cores 2 --mem-gb 12 --phase merge
run_merge

# A later increase blocks until the local chief can grant all of it.
zslurm_lease set --cores 16 --mem-gb 48 --wait 3600
run_parallel_postprocessing
```

Targets are absolute, not relative. Repeating the same `set` command is safe:
it cannot release the same cores or memory twice. A target may not exceed the
job's original `zsbatch` request.

Every effective lease change starts a new reported lease interval. A semantic
phase can also start without a resource change:

```bash
zslurm_lease phase sort
```

Repeated no-op targets do not create extra intervals. Relative releases create
separate intervals but inherit the current semantic phase name, so parallel
consumer releases remain groupable as one phase.

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

For node-wide cgroup pressure, the chief subtracts ordinary filesystem page
cache from `memory.current` using `memory.stat` (`file - shmem`). Shared memory,
anonymous memory, and kernel memory remain charged. The resulting value drives
both the reported node memory usage and the physical-memory gate used for new
job admission and lease growth. If the required cgroup counters are missing,
the complete charge is retained conservatively.

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
`ZSLURM_LEASE_MAX_MEM_MB` into every child. On a scratch-capable pilot it also
injects `ZSLURM_SCRATCH_ROOT` and the private `ZSLURM_SCRATCH_DIR`, and rewrites
`TMPDIR`, `TMP`, `TEMP`, and `TEMPDIR` to the latter. The socket is node-local and mode
`0600`; every request additionally requires the job-specific capability token.
Jobs should use `zslurm_lease` rather than speaking the JSON protocol directly.

Dynamic leases are backward compatible: a job that never calls
`zslurm_lease` holds its original resources until completion. Deploy the
manager before restarting chiefs because new chiefs call the manager's
`resize_running_job` RPC. Existing chiefs and already-running jobs cannot gain
lease support in place.

### Intentional waits and live job activity

> Development reference: this extension is implemented in local work that has
> not yet been published with the scheduler. The commands and fields below
> require that extension; ordinary CPU/memory leases are already supported.

A running child can annotate a quota wait in the same request that releases
CPU/memory. This is status metadata, not a new scheduler state:

```bash
zslurm_lease set --cores 0.1 --mem-mb 768 \
    --phase storage_quota_wait --wait-reason quota
# The application checks quota and sleeps here; ZSlurm does not check quota.
zslurm_lease set --cores 1 --mem-mb 768 --wait 60 \
    --phase copy --wait-reason none
# Start copying only after this request succeeds.
```

`--wait-reason` accepts `quota`, `lease`, or `none` (explicitly clear). It can
also be used without resource targets. Omitting it preserves the current
annotation; changing phase alone does not clear a wait. Repeating the same
reason preserves its original start timestamp. Explicit `lease` waits can
remain visible across acquisition timeouts and application retry backoff;
clear them with `none` after successful acquisition. While any growth request
is blocked in the chief's FIFO, it automatically appears as a lease wait even
for callers that do not set an annotation. This temporary annotation disappears
when the request is granted, times out, or the job ends.

Chief piggybacks a small, token-free per-job activity snapshot on its normal
manager heartbeat. No additional polling loop, quota query, or scheduling
reservation is introduced. `zsqueue` adds an `ACTIVITY` column such as
`WAIT_QUOTA 42m`; the primary `STATE` remains `RUNNING`. The manager TUI shows
quota/lease waiter counts and their longest current waits. `zsqueue --json`
schema 3 and `zscontrol jobs` expose `phase_name`, `phase_since`, `wait_reason`,
`wait_since` (Unix timestamps), and `wait_seconds`. Parseable queue output
appends the five corresponding columns, retaining all earlier column positions.
`queue_stats().waiting` gives counts and longest durations per reason.

Intentional waits are excluded from the chief's low-CPU/low-I/O stuck-job
detector. Its idle window restarts after waiting ends; profiling data is kept.
This does not suspend Slurm walltime, bypass memory checks, release SSD/durable
storage/transfer reservations, or protect a job from cancellation. Applications
must clear their annotations when they resume actual work.

Deploy the new manager **and new chiefs** before enabling wait annotations in
pipelines. New chiefs fall back to older manager poll methods when activity
reporting is unavailable, and the new queue client tolerates older row formats.
Legacy clients still get the exact 14-field job rows (15 with priority, 17
with priority and GPU fields). Activity metadata is appended only on request;
combined GPU/activity rows contain 22 fields. The queue client also accepts
earlier activity-only managers.
Requests that explicitly use wait metadata use local lease protocol version 2,
so an old chief rejects them before changing resources rather than silently
ignoring the reason. Ordinary lease requests remain protocol version 1.
Manager health advertises `job_activity` and `list_jobs_activity`; local lease
responses advertise `wait_reason_supported`. Already-running chiefs are not
hot-reloaded. Pipeline-local copies of the lease client must be updated when
the integration is enabled; merely naming a quota phase does not set a reason.
