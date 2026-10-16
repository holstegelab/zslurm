# Commands, monitoring and reports

[Overview](../README.md) · [Getting started](getting-started.md) · [Snakemake](snakemake.md)

[Submit and inspect](#main-cli-commands) · [Monitoring tools](#monitoring-and-reporting-utilities) · [Output files](#output-files)

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
- **`--s3-download-slots`**: transient concurrency from the separate S3 download pool
- **`--gpus`**: device count for a `gpu` job
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
- **`gpu`**: GPU work on a device-accounted engine

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
- **GPFS RDMA receive/transmit MiB/s and link utilization**
- **Reserved CPU and reserved memory**
- **SSD availability, total SSD, used SSD, reserved SSD**
- **Engine status**

This makes `zsnodes` the best tool for checking whether engines are full, idle, unmanaged, stopping, or carrying SSD-constrained work.

When GPFS telemetry is enabled, the curses status row uses `CPU/GPFS (%)`: the
first value is host CPU busy; the second is the mean GPFS fabric utilization
across reporting compute engines. When it is disabled (as on Spider), the row
is labeled `Host CPU (%)` and omits the inapplicable GPFS value. GPFS traffic is
measured from the byte counters on the RDMA port that Spectrum Scale is
configured to use. `GPFS%` is
`max(receive_rate, transmit_rate) / link_rate`, because the fabric is full
duplex. It is throughput utilization, not Linux IO-wait time. The old
`sys_iowait_pct` metric remains available through `zsnodes` and node reports.

On the current Snellius node classes the defaults are `mlx5_2/1` for
compute/fat engines and `mlx5_0/1` for archive engines. They can be controlled
through the chief configuration:

```yaml
gpfs_io_enable: true
gpfs_rdma_port: mlx5_2/1       # optional override
gpfs_rdma_counter_unit_bytes: 4
```

The counters cover all GPFS traffic over that client port, not one individual
GPFS filesystem such as `work3`. Per-filesystem GPFS counters require site
monitoring privileges that ordinary pilot jobs do not have.

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
- **`--ssd-feature NAME`**: override the configured SSD feature used to split rows

By default it calls `scontrol` directly and reads `ssd_feature_name` from the
ZSlurm configuration (`scratch-node` on Snellius, `ssd` on Spider).

### `zsstats`

Aggregate one or more `report*.tsv` files for downstream analysis.
New reports prefer lease-integrated effective reservations over the original
maximum request and include memory/PSS efficiency. Older reports retain the
original job-wide fallback calculation.

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

## Output files

ZSlurm writes several useful files in the working directory of the manager or commands:

- **`zslurm-<jobid>.out`**: stdout/stderr log for a job
- **`report-YYYY-MM-DD_HH-MM.tsv`**: per-job resource usage and runtime summary
- **`lease_phases-YYYY-MM-DD_HH-MM.tsv`**: one row per lease interval/phase
- **`cluster.log`**: manager log output
- **`node_usage-YYYY-MM-DD_HH-MM.tsv`**: periodic node usage snapshots when node reports are enabled

Unless a chief is started with `--keep`, successful child logs are removed after
their resource report is recorded. Logs from every nonzero child exit are
retained regardless of their text, so exceptions and tool failures remain
available for diagnosis.

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
- **Lease-integrated usage**: effective reserved/used core-seconds,
  memory-MB-seconds, PSS-MB-seconds, efficiencies, and sampling coverage

This is the main input for `zsstats`.

### `lease_phases-*.tsv`

This file contains one row per effective lease interval. It records the
semantic `phase_name`, transition type, lease epoch, held and requested
resources, duration, average/P50/P95/max CPU and PSS, resource-time integrals,
efficiencies, and sampling coverage. It is enabled by default and controlled
with:

- **`lease_phase_reports_enable`**
- **`lease_phase_reports_file_prefix`**

The file has report-compatible resource column names and can be aggregated
directly:

```bash
python zsstats --files 'lease_phases-*.tsv' --group jobname,phase_name --json
```

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
- **GPFS fabric metrics**: `gpfs_rx_mib_s`, `gpfs_tx_mib_s`,
  `gpfs_io_pct`, `gpfs_io_available`
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
