# Site configuration

[Overview](../README.md) · [Getting started](getting-started.md) · [Snakemake](snakemake.md)

[Defaults](#current-fallback-defaults) · [Config keys](#config-keys-now-supported) · [Snellius example](#example-config) · [Spider example](#spider-example)

## Site selection and defaults

Set `cluster_site` to `snellius`, `spider` or `generic` in
`~/.zslurm/config.yaml`. When omitted, the manager detects Snellius or Spider
from its fully qualified hostname and otherwise uses generic defaults.
Explicit configuration overrides those defaults. See the
[installation guide](getting-started.md) for config locations and updates.

### Current fallback defaults

| Setting | Snellius | Spider | Generic |
| --- | --- | --- | --- |
| Compute partition | `genoa` | `normal` | `normal` |
| Archive route | `staging` | Disabled | `staging` |
| GPU route | `gpu_a100` | `gpu_a100_22c` | Disabled |
| SSD feature | `scratch-node` | `ssd` | `scratch-node` |
| Pilot CPU shape | Exclusive node | Partial, up to 30 cores for autogrow | Exclusive node |
| Compute autogrow | Enabled, cap 40 | Enabled, cap 15 | Disabled |
| Require idle nodes for autogrow | Yes | No | Yes |

GPU growth stays disabled until configured with an allocation profile and cap.
The Snellius node profiles are `genoa` (192 cores, 336 GiB), `rome` (128 cores,
224 GiB), `fat_genoa` (192 cores, 1440 GiB) and `fat_rome` (128 cores, 960 GiB).
These are scheduling profiles; chiefs derive actual usable resources from their
allocations. Generic sites need their own profiles before enabling autogrow.

### Config keys now supported

The manager reads the following cluster-policy keys from `~/.zslurm/config.yaml`:

- **`cluster_site`**
  - explicit `snellius`, `spider`, or `generic` site selection; otherwise the
    manager detects Snellius/Spider from its FQDN
- **`default_partition`**
  - default partition shown in the interactive `d` prompt
- **`staging_partition`**
  - Slurm partition name used for archive/staging-oriented engines
- **`gpu_partition`**, **`autogrow_max_gpu_nodes`**, **`consolidation_min_gpu_engines`**
  - physical GPU route and separate GPU fleet limits; see [GPU jobs](scheduling.md#gpu-jobs)
- **`scratch_use_tmpdir`**, **`scratch_capacity_gb`**, **`scratch_capacity_gb_per_core`**
  - chief settings for accepting allocation-local `TMPDIR` and bounding scratch
    capacity; when both capacity limits are set, the smaller applies
- **`enable_ssd_prompt`**
  - whether the interactive UI asks about SSD-capable nodes
- **`enable_feature_prompt`**
  - whether the manual-engine dialog asks for an arbitrary Slurm feature;
    Spider disables this because its normal nodes expose local scratch
- **`ssd_feature_name`**
  - Slurm feature/constraint string used for SSD-capable nodes
- **`autogrow_prefer_partitions`**
  - ordered partition preference list for compute autogrow
- **`node_profiles`**
  - per-partition `cores` and `mem_gb` used by autogrow planning
- **`default_engine_cores`**
  - default CPU count for manually started pilots; `0` requests an exclusive
    node
- **`autogrow_engine_cores`**
  - CPU count for automatically started pilots, or their upper bound when
    `autogrow_dynamic_engine_cores` is enabled; `0` requests an exclusive node
- **`autogrow_dynamic_engine_cores`** / **`autogrow_engine_min_cores`**
  - size partial pilots between the configured minimum and
    `autogrow_engine_cores` from runnable CPU and memory demand
- **`autogrow_require_idle_nodes`**
  - require a wholly idle Slurm node before submitting an autogrow pilot;
    Spider disables this for partial pilots so Slurm can place them on mixed
    nodes or queue them until capacity becomes available. Snellius and generic
    defaults require idle capacity. Queued pilots count against residual demand
    and the fleet cap in either mode.
- **`autogrow_engine_memory_mb_per_core`**
  - hard Slurm memory grant per requested pilot core; required for memory-aware
    dynamic sizing
- **`autogrow_engine_memory_headroom_fraction`**,
  **`autogrow_engine_memory_cap_fraction`** and
  **`autogrow_engine_memory_static_reserve_mb`**
  - mirror the chief's advertised-memory envelope while sizing and accounting
    for queued partial pilots
- **`autogrow_enable`** / **`autogrow_max_compute_nodes`**
  - whether automatic allocation is initially enabled and its hard fleet cap
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
    may shorten it for backfill (see [walltime policy](scheduling.md#maintenance-windows))
- **`maintenance_time_min_sec`**
  - that floor: the shortest allocation still worth having. Below it the job stays
    queued until the window has passed, instead of starting and dying immediately
    (default `43200`, i.e. 12 hours)
- **`autogrow_time_min_enable`**
  - workload-aware `--time-min` for compute autogrow, overriding the legacy
    maintenance floor only on that path (default true on Spider, false elsewhere)
- **`autogrow_time_min_sec`**
  - minimum useful allocation before workload/drain constraints (default `43200`)
- **`autogrow_time_min_headroom_sec`**
  - startup/placement headroom, at least the job walltime buffer (default `3600`,
    minimum `60`); add this to the longest runnable job and the drain horizon
- **`job_walltime_buffer_sec`**
  - slack a job's `reqtime` must leave inside an engine's remaining walltime before
    it may start there (default `900`)
- **`capacity_wait_min_age_sec`**, **`capacity_wait_max_sec`**, **`capacity_wait_cooldown_sec`**
  - defaults `600`, `1800`, `300` seconds. If an older higher-priority job fits
    a pilot's total resources but not its current free CPU/memory, one busy
    pilot per logical partition can temporarily accumulate its required slot
    instead of continually refilling each small hole with lower-priority work.
    Surplus can still backfill; equal/higher-priority work is not blocked.
  - The job remains PENDING, available to other pilots and autogrowth. No
    grant, storage, SSD or transfer reservation is made until it actually fits.
    Dependencies, time remaining, SSD and transfer/storage gates still apply.
    The wait expires within `capacity_wait_max_sec` (or remaining usable pilot
    time), followed by a cooldown; set the maximum to `0` to disable it.
  - This bounds deliberate idle capacity, not waiting time or runtime. It is
    not a fair-share guarantee for equal-priority jobs. Running dynamic leases
    retain their existing rights; the soft hint is recomputed after handover.
    The policy uses the actual pilot shape, including fractional CPU requests,
    with no Spider/Snellius hostname or node-size assumptions.
- **`phaseout_backfill_enable`**
  - let a busy phasing-out engine accept work fitting its drain estimate, or its
    remaining lease for end-of-life draining
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

### Spider example

Spider grants memory through allocated cores (`DefMemPerCPU=8000`). The
supplied `config/sites/spider.yaml` therefore treats 30 cores/240 GB as a
partial-pilot upper bound. Autogrow sizes a smaller request when the runnable
demand fits one such pilot; for example, a 1-core/8-GB logical task needs a
2-core pilot because the chief retains its configured memory headroom. Larger
backlogs still use the 30-core ceiling. This is an allocation profile, not the
physical 960-GB or 1440-GB node size:

```yaml
cluster_site: spider
default_partition: normal
staging_partition: __disabled__
staging_autogrow_enable: false
enable_feature_prompt: false
enable_ssd_prompt: true
ssd_feature_name: ssd
scratch_use_tmpdir: true
scratch_capacity_gb_per_core: 73
gpfs_io_enable: false
default_engine_cores: 30
autogrow_engine_cores: 30
autogrow_dynamic_engine_cores: true
autogrow_require_idle_nodes: false
autogrow_engine_min_cores: 2
autogrow_engine_memory_mb_per_core: 8000
autogrow_engine_memory_headroom_fraction: 0.08
autogrow_engine_memory_cap_fraction: 0.99
autogrow_engine_memory_static_reserve_mb: 100
node_profiles:
  normal:
    cores: 30
    mem_gb: 240
autogrow_enable: true
autogrow_max_compute_nodes: 15
maintenance_window_enable: false
```

The chief derives its final schedulable memory from the Slurm environment and
cgroup, so a site or partition with a different memory-per-core grant is never
allowed to advertise the physical node's complete RAM accidentally.

Spider binds node-local XFS to `TMPDIR=/tmp` inside each Slurm allocation.
`scratch_use_tmpdir` is deliberately site-specific: without it, a chief never
mistakes ordinary system `/tmp` for schedulable SSD. The chief creates a private
mode-0700 `.zslurm/jobs/job-<zslurm-id>-<suffix>` directory for every child
attempt, exports it
as `ZSLURM_SCRATCH_DIR`, points the standard temp variables there, and removes
only that directory when the child ends. A per-core capacity cap prevents
multiple partial pilots on one node from each advertising the complete 12-TiB
device. The supplied 73-GiB/core cap stays below Spider's documented
80-decimal-GB/core entitlement; a 30-core pilot advertises at most 2190 GiB.
The actual filesystem free space remains an additional hard bound.

Manual pilot creation asks whether SSD scratch should be reserved. Enter or
`yes` supplies `--constraint=ssd`; `no` omits it. Spider currently exposes the
`ssd` feature on all normal nodes, so this is explicit without narrowing the
current normal-node pool. Compute autogrow starts enabled and is capped at 15
running-plus-queued pilots (at most 450 requested cores at the 30-core ceiling).

Spider has no physical `staging` partition and its project filesystem is
CephFS. The supplied site file therefore disables staging autogrow and GPFS
RDMA telemetry. Archive-class input jobs require a separately implemented and
tested Spider backend; dCache and S3 jobs continue to use compute pilots.
