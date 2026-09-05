# Durable storage budgets and dCache transfer pools

## Two separate dCache controls

ZSlurm models two independent concerns:

1. Durable GB accounting for active, dCache, and archive storage.
2. Transient concurrency for dCache downloads and uploads.

A job can require either or both. Do not interpret `dcache_use_add` as a
transfer slot, or a transfer slot as GB capacity.

## Durable budgets

Each instance tracks:

- `active_total`, `active_inuse`;
- `dcache_total`, `dcache_inuse`;
- `archive_total`, `archive_inuse`.

Per job, every tier has:

- `*_use_add`: space reserved when the job is assigned/started;
- `*_use_remove`: space released only after successful completion.

If a job fails, its start add is rolled back because newly created space is
assumed free. A successful producer with `add=N, remove=0` leaves N in use. A
temporary transform can use `add=N, remove=N`. A cleanup job can use
`add=0, remove=N`.

A positive start add is eligible only when:

```text
inuse + start_add <= total
```

Totals commonly start at zero until provisioned. Use `zsstatus` and:

```bash
zscontrol whatif --active GB --dcache GB --archive GB
zscontrol budget --active GB --dcache GB --archive GB
```

Persistent and physical quota are not the same. Never raise a ZSlurm total
without verifying the actual filesystem/project quota.

### Reservation design

Reserve for the lifetime in which the pipeline owns the bytes, not only during
the copy syscall. A logical `start_sample` reservation is still needed when the
input already exists on active disk: the route performs no copy, but later
sample jobs must still be protected against active-space overcommit.

Avoid architectures in which one job reserves all destination space and
separate source-specific copy jobs need another mutually exclusive resource to
make progress. That can deadlock when many starts consume active capacity while
the only jobs able to fill/release it cannot dispatch.

For the planned short-read redesign, one simple `start_sample` job should choose
the route and perform the action:

- active input: validate/claim; no source copy;
- archive input: copy/convert to active;
- dCache input: download to active.

All routes take the same active lifecycle reservation. This is a design plan,
not the current running implementation; see `local-deployment.md`.

### Drift

Negative `inuse` or values inconsistent with running jobs indicate accounting
drift or asymmetric add/remove estimates. Diagnose the underlying job deltas
before repair.

`zscontrol recompute-inuse` reconstructs in-use from assigned/running jobs. It
can discard deliberately persistent producer accounting, so treat it as a
mutation requiring explicit authorization and a recorded before/after state.

## Directional transfer slots

### Manager configuration

Persistent configuration:

```yaml
dcache_download_slots: 4
dcache_upload_slots: 4
```

Both default to legacy `dcache_transfer_slots` when their directional key is
absent. If neither directional nor legacy configuration is supplied, current
code defaults each pool to 4.

Runtime:

```bash
zscontrol transfer-slots --download 4 --upload 2
```

Either axis can be changed independently. Values are non-negative numbers.
Lowering a total does not cancel or rewrite existing reservations; new matching
jobs remain blocked until in-use drains below the new total.

### Per-job requests

Direct submission:

```bash
zsbatch --dcache-download-slots 1 -- command
zsbatch --dcache-upload-slots 1 -- command
```

Snakemake:

```python
resources:
    dcache_download_slots=1
```

or:

```python
resources:
    dcache_upload_slots=1
```

The manager reserves slots while a job is `ASSIGNED` or `RUNNING` and releases
them on completion, failure, requeue, cancellation, or worker loss.

A full download pool does not block upload-only jobs and vice versa. Numeric
priority is applied to transfer jobs too, so a higher-priority pipeline gets
first choice among eligible waiting transfers.

Do not add these directional names to Snakemake's global resource-capacity
list. Doing so creates a per-Snakemake-run limit in addition to ZSlurm's
instance-wide cross-run limit and can prevent the intended shared arbitration.
Pipeline-local advisory locks, if retained, are separate and must be understood
as an extra constraint.

### Rolling compatibility

Legacy:

- manager config: `dcache_transfer_slots`;
- job metadata/env: `ZSLURM_DCACHE_TRANSFER_SLOTS`;
- an unknown-direction job consumes its request from both new pools.

New plugin behavior:

- sends `ZSLURM_DCACHE_DOWNLOAD_SLOTS` and/or
  `ZSLURM_DCACHE_UPLOAD_SLOTS`;
- also sends a conservative legacy fallback equal to
  `max(download, upload)` for an old manager.

New manager behavior:

- if either directional field is positive, it ignores the legacy fallback;
- if no directional field is present, legacy demand consumes both pools.

Never explicitly set `dcache_transfer_slots` together with either directional
resource on one Snakemake rule; the plugin rejects this combination.

Deployment order for the direction split is safe:

1. update/install the plugin;
2. let an old manager throttle via the combined fallback;
3. restart the manager only at an authorized pipeline boundary;
4. verify `health.directional_dcache_slots` and raw transfer status.

## Observability

New manager raw status:

```json
{
  "dcache_transfers": {
    "download": {
      "total_slots": 4,
      "inuse_slots": 1,
      "pending_slots": 10
    },
    "upload": {
      "total_slots": 4,
      "inuse_slots": 2,
      "pending_slots": 20
    },
    "legacy_pending_slots": 0
  }
}
```

Retrieve it with `zscontrol status`. The current `zsstatus` envelope omits this
field even though it calls the same manager status RPC.

In the curses statistics view, the row immediately after durable `DCache` is:

```text
DCache xfer D/U: <download inuse>/<download total> | <upload inuse>/<upload total>
```

Keyboard:

- `7`: set download slot total;
- `8`: set upload slot total.

These TUI changes are runtime-only, like `zscontrol transfer-slots`.

## Choosing limits

Slot counts are concurrency controls, not bandwidth guarantees. Choose them
from measured:

- dCache endpoint throughput and latency;
- staging-node network and CPU;
- shared filesystem write/read load;
- transfer file-size distribution;
- failure/retry rate;
- whether download and upload contend for the same external bottleneck.

Increase one step at a time and compare aggregate throughput, per-transfer
duration, host I/O wait, and impact on compute jobs. More slots can reduce total
throughput when dCache or the destination becomes saturated.
