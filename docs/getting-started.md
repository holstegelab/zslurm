# Installation and first jobs

[Overview](../README.md) · [Getting started](getting-started.md) · [Snakemake](snakemake.md)

[Install](#installation) · [Update](#updating-an-existing-installation) · [First job](#quick-start)

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

Install the manager and worker entry points into the same environment. The
manager resolves the pilot launcher, `zslurm_chief`, and its Python interpreter
before calling `sbatch`, then exports their exact paths to the pilot. This is
portable across Snellius and Spider even though Slurm copies the launcher to a
spool directory. `ZSLURM_ENGINE_LAUNCHER`, `ZSLURM_CHIEF_PATH`,
`ZSLURM_ENGINE_PYTHON`, and `ZSLURM_ENGINE_ENV_BIN` remain explicit overrides
for relocated or deliberately split installations.

Check the installed manager and pilot binaries before starting an instance:

```bash
zslurm --version
zslurm_chief --version
```

The manager reports its version in its health response and each chief's
version in `zsnodes --json`. At registration it warns in `cluster.log` and
the UI if a chief reports a different version or no version at all. A new
chief also warns when connected to an older manager without versioned
registration. These warnings do not stop pilots; replace old pilots during a
planned drain. Bump both `VERSION` in `zslurm_version.py` and the package
version in `setup.py` when deploying changed manager or chief code; a test
requires them to match.

On Spider, preview and add missing site settings without overwriting existing
values or comments:

```bash
zslurm --update-config spider --dry-run
zslurm --update-config spider
```

The command uses `~/.zslurm/config.yaml`, or the legacy `~/.zslurm` YAML file
when present. It makes a timestamped backup before changing an existing file,
keeps existing values even when they differ from the template, and reports
those key names without printing their values. Review conflicts manually;
rerunning the command will not override them. It does not restart or reconfigure
a running manager. The Spider template enables autogrow with a cap of 15
partial compute pilots of up to 30 cores on the next manager start, so review those
settings first.

Site templates remain available under [`config/sites/`](../config/sites/) for manual review. For
Snellius, copy or merge `config/sites/snellius.yaml` into
`~/.zslurm/config.yaml` as appropriate. In the original Spider layout where
`~/.zslurm` itself is a YAML file, it remains the active configuration and
per-instance/runtime state is written to `~/.zslurm.d/`.

The environment file includes the base runtime dependencies for ZSlurm,
including `pyyaml` and `tabulate`, plus `setuptools` as an explicit build
dependency. Prefer `python -m pip install .`; the `pyproject.toml` build-system
declaration also lets pip create an isolated build environment when the active
environment does not already contain setuptools.

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

Running managers, chiefs and Snakemake controllers retain their loaded code.
Plan a drain or an [explicit manager handover](operations.md#explicit-manager-handover)
when deploying changes to an active instance; installing a package alone does
not update running processes.

## Quick start

Run from a working directory where the manager can write logs and reports.
After reviewing [site configuration](configuration.md), start the manager with
automatic growth disabled for this first job:

```bash
zslurm --no-autogrow
```

Press `d` to request Slurm engines, choosing a physical partition, count and
runtime for your site. Pressing `s` instead starts a local engine on the manager's
host; use it only on a host where running the workload is appropriate.

In another terminal, activate the same environment. If several instances are
running, set `ZSLURM_INSTANCE` to the intended manager's name. Submit a small job:

```bash
zsbatch -c 1 --mem 1000 -t 00:05:00 -- /bin/hostname
zsqueue --all
zsnodes
zsstatus --json
```

Slurm must start the pilot before it can run the child. `zsqueue --all` includes
completed jobs; inspect the job log in `zslurm_logs/` and the manager's
`report-*.tsv` for the result. See [commands and reports](cli.md) for options.

Once this works, connect a [Snakemake workflow](snakemake.md) or submit more
`zsbatch` jobs. Use `g` to enable bounded autogrow after setting the desired
pilot cap. The [operations guide](operations.md) covers controls and shutdown.
