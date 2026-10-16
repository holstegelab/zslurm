# ZSlurm

**Run many pipeline tasks inside shared Slurm allocations, with scheduling for
CPU, memory, local SSD, storage budgets and data transfers.**

ZSlurm is a pilot-job scheduler for workloads such as large WGS/WES cohorts.
It requests Slurm allocations called **engines** (or pilots), then packs smaller
jobs into them. Several pipelines can share one manager and its resource limits.
It supports whole-node pilots on Snellius and partial-node pilots on Spider,
with site configuration controlling allocation sizes and partitions.

The `zslurm` manager holds the queue and requests allocations. Each pilot runs
a `zslurm_chief` worker, which launches child jobs and reports their resource
use. Submit work through `zsbatch` or the native Snakemake executor.

## Why use it?

A sequencing pipeline mixes multithreaded alignment, memory-heavy processing,
short QC tasks, lightly threaded cleanup, and large transfers. Their resource
needs change between stages, while intermediate files can occupy more space
than the final results. ZSlurm coordinates these demands across the work
submitted to one instance.

| Pipeline need | What ZSlurm adds within Slurm allocations |
| --- | --- |
| Many small tasks with different CPU/memory needs | Packs multiple jobs into each pilot, including fractional CPU reservations, and looks ahead for a useful memory-per-core fit. Slurm sees the pilots; ZSlurm dispatches the child jobs. |
| Too many samples partially processed at once | Optional LIFO ordering favours newly submitted continuation jobs. Combined with storage budgets, this helps finish samples and release space sooner. |
| Several cohorts competing for capacity | Pipeline-wide numeric priorities apply to compute and transfer work. Higher priorities run first when eligible; running jobs continue. |
| Working storage fills before CPUs do | Active, archive and dCache accounting can retain a reservation across producer and cleanup jobs, limiting how much data enters the pipeline. |
| Downloads and uploads compete for bandwidth | Separate dCache download/upload pools and an S3 download pool limit concurrent transfers across participating pipelines. |
| Sort/merge and other I/O-heavy stages | SSD placement and capacity reservations give jobs private local scratch directories for intermediates. |
| A parallel phase followed by a serial phase | CPU/memory leases let a running job return capacity to its pilot, then wait to reacquire it if needed. |
| Changing demand or heterogeneous nodes | Bounded autogrow and consolidation adjust the pilot fleet; placement accounts for memory, SSD, GPU devices and remaining allocation time. |
| Hard-to-explain waiting or poor utilisation | Queue/node views, JSON status, job and lease-phase reports expose reservations and observed use. |

Slurm still allocates the nodes, enforces allocation walltime and site policy,
and accounts for their cost. Snakemake still owns the workflow DAG and retries.
ZSlurm schedules the tasks inside those allocations. Its storage counters depend
on declared usage and cleanup; they do not measure or enforce filesystem quotas.
CPU/memory leases change ZSlurm's reservations, so applications must also adjust
their own thread counts and memory use.

## Example: a short-read sequencing pipeline

The lab's [short-read Snakemake pipeline](https://github.com/holstegelab/short_read_analyzing_pipeline_Snakemake)
(`../short_read_analyzing_pipeline_Snakemake` in a neighbouring checkout) processes
WGS/WES data through preprocessing, DRAGMAP alignment, duplicate marking,
DeepVariant per-sample calling and GLnexus cohort calling, with QC and encrypted
CRAM publication alongside the calling workflow.

ZSlurm supports the resource lifecycle around that analysis:

1. **Admit and stage a sample.** Reserve active storage before bringing input
   from archive, dCache or S3; request the relevant transfer slot for remote input.
2. **Process it near the data.** Alignment, merge/duplicate marking and CRAM
   publication use SSD-local stages to avoid writing large intra-stage
   intermediates to shared storage.
3. **Share capacity as phases change.** Fused jobs such as merge/duplicate
   marking and DeepVariant/phasing change their CPU/memory leases between phases.
4. **Publish and release space.** Output transfers use the appropriate pool;
   successful cleanup releases the declared storage reservation. With LIFO,
   downstream work can progress before more samples enter the working set.

These are workflow resource declarations and application behaviours: ZSlurm
provides scheduling and accounting, while the pipeline performs the analysis,
copies, validation and cleanup. See the [Snakemake guide](docs/snakemake.md) for
the executor setup, resource mapping and a closer look at this example.

The same model suits independent batch analyses, parameter sweeps, transfer-heavy
workflows, and mixed CPU/GPU pipelines such as DeepConsensus. Jobs can be
submitted directly with `zsbatch`; Snakemake is optional. A program requiring a
single multi-node MPI allocation still needs a suitable Slurm submission;
ZSlurm places each child job within one engine.

## Get started

From a clone of this repository:

```bash
conda env create -f env.yaml
conda activate clustersnake
python -m pip install .
```

Review the [site configuration](docs/configuration.md) and the templates for
[Snellius](config/sites/snellius.yaml) or [Spider](config/sites/spider.yaml),
including pilot caps, storage budgets and transfer limits. Then start a manager
with automatic growth disabled for the first job:

```bash
zslurm --no-autogrow
```

Press `d` in the manager to request Slurm engines. In another terminal with the
same environment, submit and inspect a small job:

```bash
zsbatch -c 1 --mem 1000 -t 00:05:00 -- /bin/hostname
zsqueue
zsnodes
```

If several managers are running, select one with `--instance NAME` or
`ZSLURM_INSTANCE`. Follow [Installation and first jobs](docs/getting-started.md)
for site setup, updates and a complete first-job walkthrough, or the
[Snakemake guide](docs/snakemake.md) to connect a workflow.

## Documentation

| Guide | Read it for |
| --- | --- |
| [Installation and first jobs](docs/getting-started.md) | Environment, site setup, versions and updates. |
| [Snakemake and pipeline design](docs/snakemake.md) | Native executor, short-read example, rule resources and legacy profiles. |
| [Scheduling](docs/scheduling.md) | Priorities, FIFO/LIFO, memory packing, autogrow, consolidation and GPU work. |
| [Storage and resource leases](docs/resources.md) | Storage lifecycle accounting, transfer pools, SSD sizing and CPU/memory phases. |
| [Site configuration](docs/configuration.md) | Snellius/Spider settings, node profiles, scratch and allocation policy. |
| [Operations](docs/operations.md) | Instances, interactive controls, headless operation, handover and recovery. |
| [CLI and reports](docs/cli.md) | Submission, monitoring, JSON/TSV output and utilisation reports. |

Developer background: the [architecture report](docs/zslurm-architecture.md) and
[agent-interface plan](docs/agent-interface-plan.md) record earlier design work;
the guides above describe current usage.
