# Local deployment snapshot

> Dated 2026-07-29. Re-run every verification command before relying on this
> state. This file records recent context; it is not a promise that the active
> manager or pipeline still has the same state.

## Source and installed paths

ZSlurm source:

```text
~/projects/cluster_manager
main @ 0ac7e2d
```

The base Python environment currently imports:

```text
/gpfs/home2/hulsmanm/projects/cluster_manager/zslurm_shared.py
```

CLI entry points are under:

```text
/gpfs/work3/0/qtholstg/research/conda/marc/miniconda3/bin/
```

Native executor source:

```text
~/repos/snakemake_executor
main @ f13f7d4
```

The `snakemake` conda environment currently imports the plugin editably from:

```text
/gpfs/home2/hulsmanm/repos/snakemake_executor/
  snakemake_executor_plugin_zslurm/__init__.py
```

That environment still contains an older standalone
`envs/snakemake/bin/zsqueue` client (JSON schema 1). A login shell can resolve
the newer base client instead, so `command -v` may differ by invocation style.
The executor plugin itself imports the editable current source, but operational
CLI/schema checks must record the exact executable path. Do not infer manager or
plugin capability from a shadowed old CLI.

The plugin worktree has untracked build/reference/editor artifacts. Do not
delete or overwrite them as part of an unrelated task.

Short-read pipeline:

```text
~/projects/short_read_analyzing_pipeline_Snakemake
main @ d3391bf
```

That worktree is heavily modified and contains many untracked run/build files.
Treat all of it as user work. Inspect `git diff` before edits and never clean or
reset it.

## Persistent manager configuration

Non-secret relevant keys in `~/.zslurm/config.yaml`:

```yaml
enable_control_rpc: true
dcache_download_slots: 4
dcache_upload_slots: 4
```

These directional values apply on the next new-manager start or can be set in
the current manager at runtime only if its schema supports the directional
control RPC.

## Active manager at snapshot time

Instance:

```text
zslurm_fcn76
```

It was started before the recent priority/lease/directional-transfer code and
reports manager health schema version 1. Therefore:

- source and installed clients are newer than the process;
- `zsqueue` shows blank/`null` priority;
- `zscontrol status` has legacy flat transfer information rather than
  directional pools;
- jobs do not gain dynamic lease support in-place;
- the new persistent 4-download/4-upload config is not active as two pools.

The native plugin's directional jobs still send a conservative legacy combined
fallback, so the old manager can throttle them through its single pool.

Do not restart this manager while the current pipeline is active. At the
snapshot it had a large active queue and 38 running engines. Restart only after
the pipeline finishes and the user explicitly authorizes the deployment step.

The active status also showed:

- active durable accounting close to its configured total;
- negative dCache `inuse`, indicating pre-existing accounting drift;
- several `DVWhatshapPhasingMerge` near-OOM alarms relative to a 1500 MB
  reservation.

Re-measure rather than copying these numbers into a fix.

## Current short-read resource work

Uncommitted pipeline edits currently include:

- directional dCache requests on two download sites and fourteen upload sites;
- compatibility fallback in the transfer helper/config;
- size-dependent SSD functions such as `ssd_gb_for_inputs`;
- SSD-required rules in alignment, GLnexus, chrM, statistics, DeepVariant, and
  database-import paths.

These values are implementation candidates, not universally validated peak
measurements. Use running-job `df`/`du`, node reports, network I/O, and
worst-case samples before tightening them.

The running pipeline still has separate `start_sample`, `archive_to_active`,
and `dcache_to_active` rules. The agreed later work item is to make
`start_sample` itself choose/execute the active, archive, or dCache route while
taking the active lifecycle reservation even when data is already on disk.
This has intentionally not been changed during the run.

## Reverification commands

```bash
git -C ~/projects/cluster_manager rev-parse --short HEAD
git -C ~/projects/cluster_manager status --short
git -C ~/repos/snakemake_executor rev-parse --short HEAD
git -C ~/repos/snakemake_executor status --short
git -C ~/projects/short_read_analyzing_pipeline_Snakemake status --short

python -c 'import zslurm_shared; print(zslurm_shared.__file__)'
conda run -n snakemake python -c \
  'import snakemake_executor_plugin_zslurm as p; print(p.__file__)'
conda run -n snakemake python -c \
  'import shutil,subprocess,json; p=shutil.which("zsqueue"); print(p); print(json.loads(subprocess.check_output([p,"--json"],text=True))["schema_version"])'

zsstatus --json | jq '{instance,health,scheduler,budgets,alarms}'
zsqueue --json | jq '{schema_version,fields}'
zscontrol status
```
