# Native Snakemake executor and pipeline design

## Installed integration

The native plugin directly calls the ZSlurm XML-RPC job server. It uses
Snakemake for DAG dependencies and retries and ZSlurm for cross-job placement
and global resources.

Do not assume the source checkout is what the current Python environment
imports. Verify:

```bash
conda run -n snakemake python -c \
  'import snakemake_executor_plugin_zslurm as p; print(p.__file__)'
```

## Pipeline-wide priority

CLI:

```bash
snakemake --executor zslurm --zslurm-priority 200 ...
```

Profile:

```yaml
executor: zslurm
zslurm-priority: 200
```

Default is `100`. The plugin assigns the value to every job from that run:
compute, staging, download, upload, and final write. This solves competition
between multiple Snakemake processes only for waiting ZSlurm jobs; it does not
preempt work that is already running.

Use separated bands, for example 300/200/100, when strict cross-pipeline order
matters. Negative values are allowed. Do not try to simulate pipeline priority
with rule-level queue shuffling.

## Resource mapping

The plugin reads these rule resources:

| Snakemake resource | ZSlurm meaning | Default |
|---|---|---:|
| `partition` | ZSlurm job class (`compute` or `archive`) | `compute` |
| `time` | estimated seconds | `3600` |
| `n` | cores, fractional allowed | `1` |
| `mem_mb` | memory MB | `500` |
| `arch_use_add/remove` | durable archive GB deltas | `0` |
| `dcache_use_add/remove` | durable dCache GB deltas | `0` |
| `active_use_add/remove` | durable active GB deltas | `0` |
| `dcache_download_slots` | transient inbound dCache concurrency | `0` |
| `dcache_upload_slots` | transient outbound dCache concurrency | `0` |
| `dcache_transfer_slots` | legacy unknown-direction concurrency | `0` |
| `limit_auto_threads` | common thread env-var cap | `0` (off) |
| `input_mb` | report annotation | `0` |
| `ssd_use` | `no`, `possible`, or `required` | `no` |
| `ssd_gb` | per-engine local SSD capacity reservation | `0` |

The plugin sets manager requeue to zero because Snakemake owns retry. It removes
`SNAKEMAKE_PROFILE` from the child environment so the nested job invocation
runs locally.

## Transfer resources

Use exactly one direction per pure transfer rule:

```python
resources:
    partition="archive",
    dcache_download_slots=1
```

or:

```python
resources:
    dcache_upload_slots=1
```

Do not combine legacy and directional names in the same rule. Do not list
directional slots under Snakemake global `resources:` if ZSlurm is the intended
cross-run arbiter.

During an old-manager/new-plugin rolling state, the plugin adds a conservative
combined fallback. The new manager ignores that fallback when direction is
known.

## SSD estimation in rules

Prefer size-dependent functions:

```python
resources:
    ssd_use="required",
    ssd_gb=lambda wildcards, input: size_based_peak(input)
```

The calculation must represent peak simultaneous local files, not just input
or output size. For BAM/CRAM/FASTQ processing, account for:

- input copied from GPFS/dCache/archive;
- BAM/FASTQ expansion or conversion;
- sort/merge/dechimer temporary files;
- indexes;
- output waiting to be copied back;
- safety headroom and a minimum.

Use `possible` only when the rule has a correct shared-storage fallback.
Container binds to node-local paths generally imply `required`.

## Lowering shared-filesystem traffic

Good candidates:

- a file searched or scanned several times;
- random-access work over a large BAM/CRAM;
- sort/merge pipelines with large intermediates;
- a high-thread phase followed by a serial phase using the same data.

Pattern:

1. reserve realistic SSD;
2. copy/stream one verified local input copy;
3. perform repeated accesses and intermediates locally;
4. use dynamic leases between phase resource profiles;
5. copy only final artifacts to shared storage/dCache;
6. clean local temp robustly.

This trades restart granularity for lower network I/O. Keep phases separate
when failures are common, intermediate reuse across jobs is valuable, or one
fused job would require an impractically large maximum reservation.

## Combining phases with leases

For alignment followed by merge/dechimer or other lightly threaded work:

```bash
# Submitted for the peak high-thread phase.
run_high_thread_phase
zslurm_lease set --cores 2 --mem-gb 12
run_serial_phase
```

If a later parallel phase is unavoidable:

```bash
zslurm_lease set --cores 24 --mem-gb 48 --wait 3600
run_later_parallel_phase
```

Acquire before starting the phase. The local admission barrier prevents newly
scheduled child jobs from starving the wait, but existing jobs must still
finish and there is no preemption.

Splitting one high-thread producer into several low-thread parallel jobs is
also valid when Snakemake can express independent outputs. In that design the
producer releases by completing; ZSlurm schedules the low-thread jobs normally.

## `start_sample` routing design

The current short-read pipeline has separate `start_sample`,
`archive_to_active`, and `dcache_to_active` rules. The agreed post-run design is
one simple `start_sample` rule that:

- determines whether input is already active, in archive, or in dCache;
- takes the active lifecycle reservation for every route;
- validates/claims already-active data without copying;
- performs the archive or dCache copy itself for the other routes;
- requests a directional download slot only for the dCache route.

This prevents the deadlock where many start jobs reserve destination space
while the separate transfer jobs that could fill or later release it cannot
run. This design has not yet been implemented in the running pipeline. Do not
change it until the active run is finished and the user authorizes the work.

## Validation sequence for pipeline edits

1. Preserve the dirty worktree; inspect diffs before touching overlapping
   rules.
2. Run Snakemake lint/dry-run or rule-level tests available in the repository.
3. Verify profile defaults and every changed rule's resolved resources.
4. Test new plugin fields against both a new test manager and the rolling old
   manager path when compatibility matters.
5. Start with a small sample set and inspect queue priority, SSD placement,
   transfer reservations, node I/O, and output integrity.
6. Only then scale to the full cohort.
