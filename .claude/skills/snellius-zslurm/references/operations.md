# Operations and diagnosis

## Command surface

Discover the actual executable paths with `command -v`; do not assume the
calling shell has activated a particular conda environment.

| Command | Use |
|---|---|
| `zsstatus --json` | Primary poll: manager health, scheduler mode, durable budgets, queue/engine summary, derived alarms |
| `zsqueue --json` | Active jobs; use `--all` or `--done` for terminal jobs |
| `zsqueue --parseable` | TSV including `PRIO` |
| `zsnodes --json` | Engine CPU/memory/SSD capacity, reservations, load, I/O wait, GPFS RDMA throughput/utilization, and state |
| `zsqueue_stats --json` | Aggregate queue resources by ZSlurm partition |
| `zsoccupancy --json` | Underlying Slurm partition occupancy |
| `zscontrol status` | Raw manager status, including directional dCache transfer pools on a new manager |
| `zscontrol jobs --states RUNNING,PENDING` | Named detailed job rows, memory pressure, requeue count, priority and transfer metadata |
| `zscontrol autogrow-plan` | Last controller scaling plan |
| `zscontrol match PATTERN` | Read-only blast-radius check |
| `zscontrol whatif ...` | Read-only effect of proposed durable budget totals |
| `zscontrol forecast PLAN.json` | Read-only storage feasibility for a planned DAG |
| `zsstats --json --files 'report*.tsv'` | Post-run CPU/memory efficiency |
| `zsbatch` | Submit an ad-hoc job |
| `zscancel` | Cancel or requeue jobs |
| `zscontrol` | Gated manager mutations |
| `zslurm_lease` | Node-local CPU/memory lease for the current child job |

## Instance and schema discovery

Clients auto-select only when exactly one instance is discoverable. With
multiple managers, pass `--instance NAME` or set `ZSLURM_INSTANCE`.

Check both client and manager generations:

```bash
zsstatus --json | jq '{instance,client_schema:.schema_version,manager:.health}'
zsqueue --json | jq '{schema_version,fields}'
```

Important compatibility signals from a new manager `health` response include:

- `schema_version: 2`
- `submit_job_priority: true`
- `default_job_priority: 100`
- `list_jobs_priority: true`
- `directional_dcache_slots: true`

An old running manager can answer through new clients but omit these flags. A
new `zsqueue` emits its own schema version 2 and uses `priority: null` for rows
returned by an old manager.

The `zsstatus` envelope remains schema version 1. Its nested `health` object is
the manager contract and may independently be version 1 or 2.

## Exit codes

For the programmatic clients:

- `0`: success.
- `2`: transport/instance failure; retryable with bounded exponential backoff.
- `3`: logical rejection, ambiguity, disabled control, or bad token; do not
  retry unchanged.

`zslurm_lease` additionally returns its response code and uses `5` for local
transport/environment exceptions. Inspect stderr/JSON rather than applying the
manager-client retry rule blindly.

## Read-only diagnostic sequence

1. `zsstatus --json`.
2. `zsqueue --json` and count states by priority, partition, and rule.
3. `zsnodes --json` and compare requests with per-engine free CPU/memory/SSD.
4. `zscontrol status` for directional transfer totals/in-use/pending.
5. `zscontrol jobs --states RUNNING,PENDING` for exact job metadata.
6. `zscontrol autogrow-plan` if engines appear insufficient.
7. Inspect manager logs and `node_usage-*.tsv` only if the JSON views do not
   explain the state.

Do not equate a large pending count with a compute shortage. Check, in order:

1. manager/controller alive;
2. job priority and queue order;
3. ZSlurm partition and remaining engine walltime;
4. per-job cores/memory vs the largest eligible engine;
5. `ssd_use=required` and free SSD reservation;
6. active/dCache/archive durable budgets;
7. download/upload slot pools;
8. running or queued engines/autogrow cap.

## Derived alarms

`zsstatus.alarms` can contain:

- `budget_stall`: `inuse + smallest pending add > total` for one durable tier.
- `no_engines`: pending work, no running engines, and autogrow off/capped.
- `oversized_pending`: a pending core request exceeds the largest current
  engine while autogrow is off.
- `near_oom`: measured process-tree PSS is near or above reserved memory.

Alarms are evidence, not permission to mutate. For `near_oom`, verify the job's
process tree and report data; shared-memory/PSS aggregation and descendants can
make the number surprising.

## Gated control commands

Mutations require `enable_control_rpc: true` and, when configured, a matching
`ZSLURM_CONTROL_TOKEN`.

```bash
zscontrol budget --active GB --dcache GB --archive GB
zscontrol transfer-slots --download N --upload N
zscontrol lifo on
zscontrol context 500
zscontrol autogrow on --max-nodes N
zscontrol prioritize PATTERN
zscontrol deprioritize PATTERN
zscontrol recompute-inuse
```

Use `zscontrol match PATTERN` before (de)prioritization. More than 50 matches
requires explicit `--yes`.

`zscontrol grow N ... --yes` creates real Slurm allocations and burns SBUs.
`shrink` stops engines, preferring queued, then idle, then oldest engines. Treat
both as high-impact operations.

Changing storage totals or transfer limits at runtime does not edit
`~/.zslurm/config.yaml`; update persistent config separately when authorized.

## Manager lifecycle

The manager imports code once. Updating an editable package or git checkout
does not change an already-running process. A restart may:

- expose new RPC/schema capabilities;
- reload persistent configuration;
- rebuild or drift resource accounting;
- affect active chiefs and assigned/running jobs.

Never restart merely because source and manager schemas differ. If a restart is
authorized, first record instance, active jobs, engine state, durable budgets
including in-use, transfer reservations, and current config. Plan a drain or
explicit recovery and verify the post-restart accounting before resuming
submission.

Dynamic leases require new chiefs as well as a new manager. Deploy the manager
first; existing chiefs and already-running jobs cannot gain lease support in
place.

## Submission caveats

Example:

```bash
zsbatch -c 4 --mem 16000 -t 02:00:00 -p compute \
  --priority 100 --ssd-use possible --ssd-gb 50 \
  -J job_name -- command args
```

The `zsbatch --dependency` field is accepted and stored, but the current manager
does not enforce dependencies during dispatch. Snakemake owns its own DAG;
ad-hoc submitters must not rely on ZSlurm dependency enforcement.

`zsbatch` has no idempotency-key CLI. On a timeout or lost reply, search the
queue by exact job name/comment/owner context before retrying. The native
executor also retries some transport failures without supplying an idempotency
key, so duplicate detection remains relevant.

## Polling and completion

Poll at intervals of at least about 15 seconds; the eligible-count cache is 15
seconds and tighter loops add contention without useful resolution.

Do not infer completion from a quiet scheduler. Confirm every intended job in
terminal state (`COMPLETED`, `FAILED`, or `CANCELLED`) and verify expected
outputs. For Snakemake, use both Snakemake's DAG state and ZSlurm's external job
state.
