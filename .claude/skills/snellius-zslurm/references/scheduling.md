# Scheduling, leases, SSD, and resource sizing

## Numeric priority

Every job has an integer priority:

- default `100`;
- higher values dispatch first;
- `0` and negative values are valid;
- already-running work is not preempted.

Priority is strict across eligible waiting jobs. Memory/CPU packing is allowed
to reorder only inside one priority band. FIFO/LIFO and manual
prioritize/deprioritize are also within-band operations.

An ineligible high-priority job is skipped, so it does not strand eligible
lower-priority jobs. Eligibility includes partition, remaining walltime plus
buffer, whole-engine maximum CPU/memory, SSD, durable budgets, and transfer
slots.

Direct:

```bash
zsbatch --priority 200 -- command
```

Native executor:

```bash
snakemake --executor zslurm --zslurm-priority 200 ...
```

Profile:

```yaml
executor: zslurm
zslurm-priority: 200
```

The default `100` is backward compatible with an old manager. A non-default
value extends the submit RPC and is rejected clearly if the running manager is
too old.

## FIFO, LIFO, and packing

The default scheduler mode is LIFO for compute and FIFO for archive:

- priority is evaluated first;
- compute LIFO reverses submission order only within an equal-priority band;
- archive always scans oldest first;
- `prio_fillmem_context` defines how many eligible candidates the greedy
  memory/CPU packer considers;
- context `1` approximates strict queue order;
- higher contexts improve packing but permit more within-band reordering.

Read `scheduler.lastin_first` before reasoning from raw queue position.
`zscontrol prioritize` and `deprioritize` account for LIFO internally, but
always dry-run with `match`.

## CPU and memory sizing

ZSlurm packs jobs against each engine's own CPU and memory capacity. Fractional
CPU requests are supported. Use:

- `n` / `--cpus-per-task` for the scheduling reservation;
- `mem_mb` / `--mem` for memory in MB;
- `limit_auto_threads` / `--limit-threads` to cap common BLAS/OpenMP thread
  environment variables.

`limit_auto_threads` does not rewrite application-specific flags. Pass the same
thread count to BWA, samtools, Java, Python pools, or other tools explicitly.

Estimate from observed peak PSS and CPU use, not nominal thread counts. Compare:

- `zsstats --json`;
- `zscontrol jobs --states RUNNING`;
- `zsnodes --json`;
- `node_usage-*.tsv`.

A failed requeue-enabled job is currently requeued with doubled memory.
Snakemake's native executor submits with manager requeue disabled because
Snakemake handles retry itself. Diagnose which retry layer is active before
predicting behavior.

## Dynamic CPU/memory leases

### Model

A child initially holds the complete resources requested at submission.
`zslurm_lease set` changes the absolute amount held in ZSlurm's node-local
scheduler. It does not resize the underlying Slurm allocation, cgroup, process,
or tool thread pool.

Environment injected into supported child jobs:

- `ZSLURM_LEASE_SOCKET`
- `ZSLURM_LEASE_TOKEN`
- `ZSLURM_JOB_ID`
- `ZSLURM_LEASE_MAX_CORES`
- `ZSLURM_LEASE_MAX_MEM_MB`

The Unix socket is node-local and mode `0600`; each job has a capability token.
Use the CLI instead of speaking the protocol directly.

### Commands

```bash
zslurm_lease status
zslurm_lease --json status
zslurm_lease set --cores 2 --mem-gb 12
zslurm_lease set --cores 24 --mem-mb 49152 --wait 3600
```

Targets are absolute and idempotent. Omitting one axis leaves it unchanged.
Targets cannot be negative or exceed the original request.

### Release

Shrinking updates manager and chief reservations, then makes the released CPU
and memory available to other child jobs on the same engine. The completed job
returns only its current holding, preventing double-release.

Memory cannot shrink below the measured process-tree memory plus headroom:

```yaml
lease_min_cpu: 0.1
lease_memory_headroom_fraction: 0.20
lease_memory_headroom_mb: 512
lease_socket_dir: /node/local/path  # optional
```

The response reports when a safety floor adjusted the request.

### Reacquisition

If growth cannot fit immediately:

1. the request joins a FIFO queue on the local chief;
2. the chief stops admitting new child jobs on that engine;
3. existing child jobs continue;
4. the first waiter receives CPU and memory atomically when both fit;
5. the caller remains blocked until grant or `--wait` timeout.

There is no preemption. A timeout leaves the previous lease unchanged. This
admission barrier is essential: released resources may temporarily serve other
jobs, but a continuous stream of new jobs cannot starve a waiting reacquire and
create the scheduler deadlock discussed during the short-read design.

### Phase design

Preferred sequence:

1. submit the maximum needed by any phase;
2. run the high-thread/high-memory phase;
3. shrink before a serial or low-thread phase;
4. if a later phase needs more, reacquire before starting it;
5. proceed only after a successful grant.

High-to-low is simplest. Low-to-high is safe only when the code explicitly
waits for `zslurm_lease set` and handles timeout/failure. Never start a
high-resource tool and then request the resources it already consumes.

Jobs that never call the lease CLI retain their original reservation and remain
backward compatible.

## SSD placement

### Modes

`ssd_use=no`

- eligible on normal and scratch engines;
- no `ssd_gb` reservation.

`ssd_use=possible`

- eligible on normal engines;
- gains a small scheduling bonus on a scratch engine with sufficient free SSD;
- reserves `ssd_gb` if actually placed on SSD;
- does not wait exclusively for SSD.

`ssd_use=required`

- eligible only on an engine reporting `has_ssd=true`;
- requires `ssd_total_gb - res_ssd_reserved_gb >= ssd_gb`;
- influences autogrow so residual SSD-required demand pulls scratch nodes.

`ssd_gb=0` with `required` demands an SSD engine but does not reserve capacity;
avoid this for storage-heavy work because multiple jobs can overfill the node.

### Realistic estimation

Reserve peak simultaneous local bytes, not just one file:

```text
peak = local input copies
     + expanded/decompressed/converted copies
     + temporary sort/merge/spill data
     + local outputs not yet uploaded
     + indexes and metadata
     + safety headroom
```

For streaming a network file once to SSD before repeated searches, include the
complete local copy plus all concurrent outputs and temp space. A pipe without a
local copy does not need input-sized SSD but rereads the network source if the
consumer repeats scans.

For size-dependent Snakemake rules, compute `ssd_gb` from actual input sizes
with a factor, fixed overhead, minimum, and rounding. Validate during real jobs:

- node-local `df` and `du` at phase boundaries;
- `zsnodes --json` reserved/used SSD;
- node reports and job logs;
- worst-case BAM/CRAM/FASTQ samples, not only medians.

Leave space for unrelated system use and measurement error. Do not reserve
nearly the full reported device.

## Network-storage offload pattern

When a rule repeatedly searches or randomly accesses a large network file:

1. request `ssd_use=required` and a realistic `ssd_gb`;
2. copy or stream one verified copy to node-local SSD;
3. perform repeated scans, sort/merge, and intermediate work locally;
4. write only the required final artifact back to shared storage/dCache;
5. clean local files on success and failure.

Fuse phases only when avoiding shared intermediate I/O outweighs reduced
restart granularity. Dynamic leases make high-thread then low-thread fusion
practical; they do not make later resource growth free or guaranteed.
