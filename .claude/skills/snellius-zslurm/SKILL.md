---
name: snellius-zslurm
description: >-
  Operate, inspect, diagnose, configure, or develop the lab's ZSlurm pilot-job
  scheduler on SURF Snellius, including zslurm, zsbatch, zsqueue, zsnodes,
  zsstatus, zscontrol, zslurm_lease, the native Snakemake executor plugin,
  pipeline priority, LIFO/FIFO and memory packing, CPU/memory leases,
  node-local SSD placement and sizing, active/dCache/archive storage budgets,
  directional dCache download/upload slots, rolling upgrades, engine scaling,
  stuck jobs, storage or network-I/O pressure, and SBU usage. Use for ZSlurm
  implementation work and for Snakemake workflows running through ZSlurm.
---

# Operate ZSlurm on Snellius

Treat ZSlurm as a pilot-job scheduler layered on Slurm. A manager acquires
whole-node Slurm allocations ("engines"); node-local `zslurm_chief` processes
pack child jobs into those engines.

Prefer the machine-readable clients over the curses TUI. Explain TUI state or
keys when useful, but do not launch an interactive TUI from a non-interactive
agent session.

## Route to the right reference

Read only the references needed for the task:

- General preflight, monitoring, alarms, control commands, manager lifecycle,
  exit codes, and safety: [operations.md](references/operations.md)
- Priority, LIFO/FIFO, memory packing, dynamic CPU/memory leases, SSD placement,
  and resource-sizing rules: [scheduling.md](references/scheduling.md)
- Durable storage budgets, directional dCache transfer slots, rolling-upgrade
  behavior, and TUI rows/keys: [storage-and-transfers.md](references/storage-and-transfers.md)
- Native Snakemake executor resources, profiles, phased jobs, local-SSD I/O
  patterns, and short-read design guidance: [snakemake.md](references/snakemake.md)
- Snellius node shapes, filesystems, and SBU caveats:
  [snellius-nodes.md](references/snellius-nodes.md)
- Exact examples and diagnostic transcripts: [examples.md](references/examples.md)
- Dated paths, commits, active-manager compatibility, and current short-read
  state: [local-deployment.md](references/local-deployment.md)
- Machine-output schemas: [schemas/](references/schemas/)

For implementation or behavior questions, inspect the current source instead
of relying only on this skill:

- ZSlurm: `~/projects/cluster_manager`
- Native executor: `~/repos/snakemake_executor`
- Short-read pipeline: `~/projects/short_read_analyzing_pipeline_Snakemake`

Preserve unrelated or uncommitted changes in these repositories.

## Start every operational task with capability discovery

1. Resolve the installed clients without assuming a conda environment:

   ```bash
   command -v zsbatch zsqueue zsnodes zsstatus zscontrol zslurm_lease
   ```

2. Discover instances. If a client reports ambiguity, select one explicitly
   with `--instance NAME` or set `ZSLURM_INSTANCE`.
3. Run:

   ```bash
   zsstatus --json
   zsqueue --json
   zsnodes --json
   ```

4. Inspect `.health.schema_version` and capability flags. Client code can be
   newer than a running manager because the manager keeps its imported code in
   memory until restart.
5. For directional transfer state, use:

   ```bash
   zscontrol status
   ```

   The current `zsstatus` client does not forward `dcache_transfers`, although
   the new manager returns it through `get_status_json`.
6. Before any mutation, state the intended change, verify the exact instance,
   inspect current state, and distinguish runtime-only changes from persistent
   `~/.zslurm/config.yaml` changes.

Do not restart a manager merely to expose new capabilities while pipelines are
active. A restart changes scheduling/accounting state and requires explicit
authorization and a planned drain or recovery procedure.

## Core scheduling model

Reason about these independent resource axes:

- numeric pipeline/job priority;
- per-engine CPU and memory;
- per-engine local SSD eligibility and capacity;
- instance-wide durable active/dCache/archive GB budgets;
- instance-wide transient dCache download and upload slots;
- partition, walltime, and engine eligibility.

A job starts only when it fits every required axis. CPU or memory idleness does
not prove a job is runnable.

### Priority and queue order

- Default priority is `100`.
- Higher integers run first. `0` and negative priorities are valid.
- Priority does not preempt running work.
- Packing, FIFO/LIFO, and manual (de)prioritization reorder only within one
  numeric priority band.
- An ineligible high-priority job does not block eligible lower-priority work.
- Compute uses FIFO or configured LIFO within a band; archive remains FIFO.
- `zsqueue` shows `PRIO`; JSON schema version 2 has numeric `priority`, or
  `null` when connected to an older manager.

Use `zsbatch --priority N` for one job or the native executor's
`--zslurm-priority N` / profile key `zslurm-priority: N` for a whole pipeline.

### Dynamic CPU/memory leases

A child starts holding its complete submitted CPU and memory reservation. It can
set a smaller absolute target:

```bash
zslurm_lease status
zslurm_lease set --cores 2 --mem-gb 12
zslurm_lease set --cores 24 --mem-gb 48 --wait 3600
```

Released resources become available to other child jobs on the same engine.
Growth waits FIFO and creates a local admission barrier so newly admitted work
cannot continually steal the capacity needed for reacquisition. There is no
preemption. The tool must still change its own thread count.

Use leases primarily for high-thread to low-thread phases. Before entering a
later high-resource phase, acquire its target and proceed only after success.
Never assume a lease changes the underlying Slurm allocation or cgroup.

### SSD

- `ssd_use=no`: no SSD constraint or reservation.
- `ssd_use=possible`: may run on a normal engine; a scratch engine gets a small
  placement preference, and `ssd_gb` is reserved if it lands there.
- `ssd_use=required`: only an SSD engine with sufficient free reserved capacity
  is eligible.

Size `ssd_gb` from peak simultaneous node-local bytes, including local input
copies, uncompressed/converted data, temporary sort/merge files, indices, and
outputs, plus headroom. Do not use final output size alone.

### Storage and transfers

Do not confuse durable dCache GB with transient dCache transfer concurrency:

- `dcache_use_add/remove` changes durable GB accounting.
- `dcache_download_slots` and `dcache_upload_slots` reserve independent
  transient pools while a transfer job is assigned/running.

Current persistent manager keys are:

```yaml
dcache_download_slots: 4
dcache_upload_slots: 4
```

Runtime adjustment:

```bash
zscontrol transfer-slots --download 4 --upload 4
```

TUI row: `DCache xfer D/U: download_inuse/download_total |
upload_inuse/upload_total`; keys `7` and `8` change the respective limits.

## Safe operating loop

1. Inspect status, jobs, engines, capabilities, budgets, and transfer pools.
2. Classify pending work by the first binding axis: priority/order, partition,
   walltime, CPU, memory, SSD, durable storage, transfer slots, or no engines.
3. Use read-only helpers (`match`, `whatif`, `forecast`, `jobs`,
   `autogrow-plan`) before mutations.
4. Make the narrowest reversible change.
5. Poll no faster than about 15 seconds.
6. Confirm terminal job states and inspect reports after completion.

Use `zsstats --json` and node-usage reports to compare requested resources with
observed CPU, PSS, I/O wait, and SSD use. Treat apparent PSS above a reservation
as a signal to investigate process-tree accounting and the rule's memory
estimate, not as automatic proof of one specific failure mode.

## Non-negotiable safety rules

- Never expose the control token. Prefer `ZSLURM_CONTROL_TOKEN`; do not print it.
- Do not start, restart, grow, shrink, cancel, or rewrite budgets merely because
  monitoring shows a problem. Read-only diagnosis does not authorize mutation.
- Before `grow`, check `budget-overview`, estimate SBU burn, and cap autogrow.
- Dry-run pattern operations with `zscontrol match PATTERN`.
- Retry exit code `2` only with backoff and duplicate checks. Do not retry exit
  code `3`.
- `zsbatch` has no idempotency option. A lost reply can leave an uncertain
  submission; inspect the queue before resubmitting.
- Do not put Snakemake's directional transfer resources in its global
  `--resources` capacity list when ZSlurm is intended to enforce the
  cross-pipeline pool.
- Do not mix legacy `dcache_transfer_slots` with directional slots on one rule.
- Do not silently edit a running short-read pipeline. The current repository is
  intentionally dirty; inspect and preserve the user's work.
