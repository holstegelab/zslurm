# Snakemake and pipeline design

[Overview](../README.md) · [Getting started](getting-started.md) · [Resource reference](resources.md)

Snakemake determines which rules are ready and handles workflow retries. The
[native ZSlurm executor](https://github.com/holstegelab/snakemake-executor-plugin-zslurm)
submits those jobs directly to the manager over XML-RPC. ZSlurm places them
across its engines and enforces the resources shared by all workflows connected
to that instance.

## Connect a workflow

Install ZSlurm and `snakemake-executor-plugin-zslurm` into the environment that
runs Snakemake. For adjacent source checkouts, after installing ZSlurm there:

```bash
python -m pip install /path/to/snakemake_executor
```

A minimal profile at `profiles/zslurm/config.yaml` can contain:

```yaml
executor: zslurm
jobs: 1000
zslurm-priority: 100
```

Start a manager and its engines as described in [Getting started](getting-started.md),
then, from the workflow's working directory:

```bash
export ZSLURM_INSTANCE=my_manager
snakemake --profile profiles/zslurm --snakefile /path/to/workflow/Snakefile
```

Use the actual instance name reported by your manager. Add the workflow's Conda,
container and resource settings to its profile. Set `zslurm-priority` once per
run to order competing pipelines; higher integers have priority over lower
ones, without preempting running work. The default is 100.

The short-read pipeline ships its own
[native profile](https://github.com/holstegelab/short_read_analyzing_pipeline_Snakemake/blob/main/profiles/zslurm/config.yaml)
and [deployment instructions](https://github.com/holstegelab/short_read_analyzing_pipeline_Snakemake/blob/main/DEPLOYMENT.md).
Those cover reference data, tools, site paths and transfer credentials in
addition to this scheduler. Use that profile and its site configuration when
running the pipeline.

## Rule resources

| Rule resource | Meaning |
| --- | --- |
| `n` | CPU reservation; fractional cores are allowed. |
| `cpus_per_task` | Portable CPU alias if `n` is absent; otherwise rule `threads` is the fallback. |
| `mem_mb` | Memory reservation in MB. |
| `time` | Estimated runtime in **seconds**. |
| `runtime` | Portable runtime in **minutes**, used if `time` is absent. |
| `partition` | Logical job class, normally `compute` or `archive`; see [routing](scheduling.md#job-classes-and-slurm-partitions). |
| `ssd_use`, `ssd_gb` | SSD mode (`no`, `possible`, `required`) and per-engine capacity reservation. |
| `active_use_add/remove` | Active-storage lifecycle deltas in GB. |
| `arch_use_add/remove`, `dcache_use_add/remove` | Archive/dCache lifecycle deltas in GB. |
| `dcache_download_slots`, `dcache_upload_slots` | Independent transient dCache transfer reservations. |
| `s3_download_slots` | Transient reservation in the separate S3 download pool. |
| `limit_auto_threads` | Cap common library thread environment variables. |
| `input_mb` | Input-size annotation for reports. |

Native names take precedence over their portable aliases. In particular, a
profile default `n=1` overrides a rule's `threads`; keep explicit reservations
and actual tool concurrency consistent. Likewise, a default `time` overrides
`runtime`. The executor assigns retries to Snakemake rather than also retrying
the same job in the manager.

For a pure dCache download rule, for example:

```python
resources:
    n=1,
    mem_mb=1000,
    time=3600,
    partition="compute",
    dcache_download_slots=1
```

The transfer command remains part of the rule. Use `archive` only when that
operation needs a configured archive/staging worker. dCache and S3 transfers
can use compute workers; Spider's shipped configuration disables the archive
route until a suitable backend is configured.

Leave directional transfer slots out of Snakemake's global `resources:` limits
when ZSlurm should enforce the pool across multiple runs. Do not mix the legacy
`dcache_transfer_slots` resource with directional resources on a rule, or S3
and dCache slots on the same transfer. See [transfer accounting](resources.md#limiting-concurrent-dcache-transfers)
for rolling-version compatibility and release semantics.

## Short-read pipeline: from input to released storage

The [WGS/WES pipeline](https://github.com/holstegelab/short_read_analyzing_pipeline_Snakemake)
illustrates why CPU and memory alone do not describe a runnable job. Its main
calling path is DRAGMAP → DeepVariant → GLnexus, with preprocessing, QC and CRAM
publication around it. The following stages are implemented in the neighbouring
pipeline checkout; exact reservations belong to that pipeline's rules.

| Stage | How the workflow uses ZSlurm |
| --- | --- |
| Input admission | `start_sample_active`, `start_sample_archive`, `start_sample_dcache` and `start_sample_s3` reserve active space for their source route. Remote routes request their appropriate transfer resources. |
| Alignment | `align_reads_fused` reserves SSD for alignment, merge/check/dechimer and sorting within one job. Large temporary files stay local. |
| Merge and duplicate marking | `markdup` performs the readgroup merge and duplicate marking in one SSD-local job, shrinking its lease between phases. The intermediate merged BAM is not published to shared storage. |
| CRAM publication | `cram_encrypt_fused` converts, encrypts and uploads from SSD, reserving local space for overlapping plaintext/encrypted output and a dCache upload share. |
| Variant calling and phasing | `deepvariant_phasing_fused` changes its lease between phases. It uses `ssd_use="possible"`, with a shared-storage fallback; SSD is mandatory for the stages above. |
| Completion and cleanup | Rules release declared active-storage reservations when their associated data is successfully removed or its lifecycle ends. |

The manager's storage counter can therefore span several jobs: the admission
rule adds a reservation, processing rules consume the data, and cleanup removes
that reservation. Set the total budget and initial usage to match the space
available to participating pipelines. Files created outside this accounting,
failed cleanup and real filesystem quota changes need separate handling.

LIFO is useful here because newly ready downstream jobs often belong to samples
already in progress. It is an ordering preference within a numeric priority
band, not a sample-level guarantee. Storage admission is what bounds the
working set; workflow dependencies still determine which jobs can run.

## Design stages around data and resource phases

For a stage that scans or sorts the same large data repeatedly, reserve its
peak simultaneous SSD footprint: local inputs, expanded data, sort/merge
intermediates, indexes and outputs waiting for publication, plus headroom.
Use the assigned `ZSLURM_SCRATCH_DIR` or `TMPDIR` for local files. Choose
`ssd_use="possible"` only if the stage also works on an engine without SSD.

A fused stage can keep those files local while returning unused CPU/memory:

```bash
# The submitted reservation covers the parallel phase.
run_parallel_phase
zslurm_lease set --cores 2 --mem-gb 12 --phase serial_cleanup
run_serial_cleanup
# Reacquire before starting any later parallel phase.
zslurm_lease set --cores 16 --mem-gb 48 --wait 3600 --phase final_parallel
run_final_parallel_phase
```

Each command must succeed before the next phase starts; use normal script error
handling such as `set -e`. The application controls tool threads. Lease growth
waits for free capacity on the same engine and does not preempt other jobs or
enlarge the underlying Slurm allocation. SSD and transfer reservations remain
held for the job's lifetime. See [dynamic leases](resources.md#dynamic-cpu-and-memory-leases)
for fairness, memory safety and intentional-wait annotations.

Fusion reduces shared-filesystem traffic but makes the restart unit larger.
Keep stages separate when intermediate reuse, independent scheduling or failure
recovery matters more than keeping temporary data local.

## Legacy profiles

The repository-root [`zslurm.yaml`](../zslurm.yaml) and
[`snakemake_profile.yaml`](../snakemake_profile.yaml) use Snakemake's older
`cluster: zsbatch ...` interface. They are retained for existing installations;
they are not native executor profiles for Snakemake 8+. New workflows should
use `executor: zslurm` with the plugin, as above. Direct `zsbatch` submissions
remain supported independently of Snakemake.
