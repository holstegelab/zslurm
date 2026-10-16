# Operating a ZSlurm instance

[Overview](../README.md) · [Getting started](getting-started.md) · [Snakemake](snakemake.md)

[Instances and handover](#configuration-and-instances) · [UI controls](#interactive-manager-controls) · [Headless and API](#agent--programmatic-interface) · [Startup failures](#temporary-job-start-filesystem-failures)

## Configuration and instances

ZSlurm stores user configuration under `~/.zslurm`.

- **Global config file**: `~/.zslurm/config.yaml`
- **Per-instance files**: `~/.zslurm/instances/*.yaml`

Each running `zslurm` manager creates its own instance file containing runtime connection details such as:

- **`bind_host`**
- **`advertise_host`**
- **`base_port`**
- **`rpcpath`**

CLI tools auto-detect instances if there is only one running instance. If there are multiple, select one explicitly.

### Selecting an instance

- **Per command**: use `--instance NAME`
- **Session-wide**: set `ZSLURM_INSTANCE=NAME`

### Explicit manager handover

Handover is always initiated explicitly from a newly started, empty manager;
ZSlurm does not elect a replacement automatically. Press **Shift-H**, enter the
old instance name, and type `yes` to confirm. The new manager imports the queue,
budgets, leases, dependencies, scheduler settings, and Slurm-engine records. The
old instance name is atomically changed into an alias for the new endpoint, and
running chiefs reconnect without stopping their child processes.

The manager bounds new grants by the smaller of the chief's reported free
resources and the engine's actual remaining CPU/memory reservations. Running
jobs count their current mutable leases, not immutable maxima; unacknowledged
`ASSIGNED` grants already count at their initial shape. A lost grant reply
replays the same engine's existing grants before assigning new work, without
claiming their resources twice. Start acknowledgements and lease growth check
the same holdings. This protects Spider and Snellius pilots when an older
chief has stale free counters. A pre-existing overbooked pilot keeps its live
children, but receives no additional work until its holdings fit again.

The target must not contain jobs or engines, and a source with a local/unmanaged
engine is rejected. Both managers must run a version that supports explicit
handover. The equivalent gated control command is:

```bash
zscontrol --instance NEW handover-from OLD --yes
```

After a successful handover, queued chiefs resolve the alias when they start;
already-running chiefs receive a migration command and also follow the alias if
the old manager exits before that command arrives.

Do not retire the source forwarding process merely because all chiefs migrated.
Older Snakemake ZSlurm executors cache their job-RPC URL and can keep polling the
old endpoint even when discovery points elsewhere. Keep the source until those
controllers exit or verify that their executor supports endpoint rediscovery on
status failure (plugin commit `1f3ffe1` or later). Updating an installed package
does not update objects already loaded by a running controller.

If the source was already stopped, `zslurm_legacy_proxy.py` can restore only its
legacy job-RPC endpoint, forwarding to the explicitly identified target manager.
It requires the preserved source startup banner log, target instance name, and
expected target manager UUID. It creates no queue or Slurm allocations and
refuses nonlocal source addresses, direct forwarding loops and mismatched target
identity. Keep its log private and retain the helper until legacy clients are
gone. This is a compatibility bridge, not automatic recovery of a lost queue.

## Interactive manager controls

Inside the `zslurm` curses UI:

- **`s`**: Start a local engine
- **`x`**: Stop a local engine
- **`d`**: Start Slurm engines for a partition
- **`c`**: Stop Slurm engines
- **`a`**: Toggle automatic consolidation of engines
- **`o`**: Phase out engines by node name so they stop accepting new jobs
- **`p`**: One-time prioritize jobs whose name or working-directory component matches a pattern
- **`n`**: One-time deprioritize jobs whose name or working-directory component matches a pattern
- **`P`**: Set an active numeric priority override for a working-directory pattern
- **`N`**: Clear an active working-directory priority override
- **`l`**: Toggle last-in/first-out job preference
- **`1`**: Set archive staging quota
- **`2`**: Set active storage quota
- **`3`**: Set dcache staging quota
- **`4`**: Manually set archive currently-used capacity
- **`5`**: Manually set active currently-used capacity
- **`6`**: Manually set dcache currently-used capacity
- **`m`**: Set the scheduling search window used for memory-aware filling
- **`g`**: Toggle autogrow and set the maximum number of compute nodes it may add
- **`u`**: Refresh the ncurses screen
- **`q`**: Stop the manager

When starting Slurm engines, entering `staging` maps to archive-oriented engines used for transfer/archive work.

Workflow coordinators that legitimately wait on queued child jobs can explicitly
submit `ZSLURM_JOB_MONITOR_ROLE=controller` in their job environment. Updated
chiefs retain resource accounting and cancellation but do not terminate these
jobs solely for low CPU/I/O activity. Ordinary jobs keep the configured stuck
detection. The launch option is removed from the child environment so it cannot
silently exempt the coordinator's subsequently submitted scientific jobs.
`compute` explicitly selects normal detection; other values are rejected before
launch. This is site-independent (Spider and Snellius). Already running or old
chiefs are not modified by a software update; rolling deployment is required.
From release 0.2.4, an updated manager places explicitly tagged controllers only
on chiefs reporting 0.2.4 or later. Older/unversioned chiefs keep processing
ordinary work; they are not stopped. Existing running tasks are unaffected.
This prevents a recovered controller from silently receiving the old heuristic
during a mixed-version rollout. Ensure an updated pilot is available; the tag
does not itself replace already queued legacy pilot allocations.

From release 0.2.5, an operator can also explicitly register an application-owned
controller namespace with the authenticated manager RPC
`register_controller_owner_prefix(token, "my-controller:")`. This is a literal
prefix of the job's **owner**, not its name, cwd, command, or inherited environment.
Registration is additive and idempotent; it tags matching queued and future jobs
with the same controller launch option and survives manager handover. Ordinary
scientific jobs must use a different owner namespace. The default registry is
empty on every site, including Spider and Snellius. Explicit conflicting roles
and untagged in-flight grants cause registration to fail before any change.
Running jobs are reported but left untouched; this cannot retroactively protect
a running controller on an old chief. Tagged jobs still require a chief >=0.2.4,
normal resources, satisfied dependencies, and sufficient allocation time.

The manager RPC `cancel_job(jobid, False, expected_state)` supports exact
compare-and-cancel for `PENDING` and `REQUEUED`. The state check and cancellation
share the dispatch lock: a job that became assigned or running is left alone,
without signalling a worker. A successful cancellation is terminal, including
when retries remain. Clients must check `health()['conditional_cancel_states']`
before using `REQUEUED`; older managers advertise only the legacy
`conditional_pending_cancel` capability. Never fall back to unconditional
cancellation after a rejected comparison. This behavior is site-independent.

On Snellius, starting Slurm engines also offers an SSD scratch-node reservation prompt. For regular partitions, the UI asks for:

- **Partition**
- **Number of engines**
- **Optional Slurm feature/constraint**
- **Cores per engine**
- **Runtime**

## Agent / programmatic interface

ZSlurm provides JSON clients and a control API for scripts and agents to operate
without the TUI. The [CLI reference](cli.md) covers monitoring output; the
[agent-interface plan](agent-interface-plan.md) records the original design.

- **Headless manager**: `zslurm --headless` runs the RPC servers + controller without the
  curses UI (for unattended/agent operation and testing). Flags: `--no-autogrow`,
  `--no-autoconsolidate`, `--enable-control`, `--control-token TOKEN`, and
  `--instance-name NAME`. It prints a one-line JSON banner with the instance endpoint,
  then runs until SIGTERM.
  On shutdown, failed Slurm cancellation requests are logged with their allocation
  IDs and cause a nonzero manager exit. A failed request does not unregister the
  engine in the cancellation operation. Even successful `scancel` requests are
  reported as requests, not confirmed termination; inspect Slurm before assuming
  allocations have ended. Logs must be retained for recovery after manager loss.
  Silent chiefs are fenced against new assignments. Their jobs remain owned
  until repeated fresh queue observations show absence and `sacct` confirms a
  terminal state for the exact allocation. Missing or failed observations are
  not termination evidence. This avoids requeueing onto a second worker while
  the original may still be writing outputs; accounting delays can therefore
  delay recovery. The controller no longer blocks for 60 seconds per chief
  waiting for its own cached queue snapshot to change.
- **`zsstatus`**: one JSON snapshot an agent polls — health, the three storage budgets,
  scheduler mode, queue, an engine summary, and derived **alarms** (`budget_stall`,
  `no_engines`, `oversized_pending`, `near_oom`).
- **`zscontrol`**: a control plane mirroring the TUI keys. Reads (no token): `status`,
  `whatif`, `match`, `forecast` (will a planned DAG fit the budgets?), `jobs`,
  `autogrow-plan`. Gated writes (require `enable_control_rpc` + `control_token`): `budget`,
  `transfer-slots`, `lifo`, `context`, `autogrow`, `prioritize`/`deprioritize`,
  `priority-override`, `recompute-inuse`,
  `grow`/`shrink`, and `handover-from OLD --yes`.
- **`--json`** on `zsqueue` and `zsnodes` emits numeric, schema-versioned rows;
  `zsqueue_stats`/`zsoccupancy`/`zsstats` already have `--json`. Exit-code contract across
  the agent clients: `0` ok, `2` transport/instance failure (retryable), `3` logical
  rejection (e.g. control disabled, bad token) — not retryable.
- New RPCs on the job server (gated writes behind `enable_control_rpc` + `control_token`,
  set in `~/.zslurm/config.yaml` or via the `--enable-control`/`--control-token` flags):
  `ping`/`health`, `get_status_json`, `match_jobs`, `whatif_budget`, `forecast_budget`,
  `list_jobs_detailed`, `get_autogrow_plan`, `set_budgets`, `set_transfer_limits`, `set_scheduler_mode`,
  `set_autogrow`, `prioritize`/`deprioritize`, `list_cwd_priority_overrides`,
  `set_cwd_priority_override`, `clear_cwd_priority_override`,
  `recompute_inuse_from_running`,
  `grow`/`shrink`. `submit_job` accepts optional `idempotency_key` and `priority`
  arguments for retry-safe, priority-aware submission.

### Temporary job-start filesystem failures

New chiefs retain a granted job and retry startup after temporary filesystem
errors such as `EDQUOT` (quota exceeded), `ENOSPC`, `EIO` and `ESTALE`. They do
not fail the job or spend a Snakemake retry merely because its log cannot yet
be created. Retries back off from 20 seconds to at most 300 seconds, configured
with `job_start_retry_seconds` and `job_start_retry_max_seconds`.

The chief keeps polling, reporting other completions and handling cancellation.
Its accounting lock is not held while creating the log. It keeps the job's
existing CPU/memory/storage/transfer grant exactly once, does not request more
work while startup is deferred, and retries even when those grants leave zero
unreserved cores. Actual memory headroom is rechecked before spawn. A cancelled
waiting start releases its local holding and cannot launch later.

The manager first returns a grant as `ASSIGNED`; the chief acknowledges it
before retaining it as a deferred `RUNNING` start with zero measured usage. Its
log reports `START DEFERRED`, the error and the next retry delay. The existing
`can_run_assigned_job` RPC is idempotent, so a lost acknowledgement response can
be retried safely. Invalid commands still fail the individual job. This
mechanism cannot rescue a computation that has already started and then fails
while writing its own output files.

Chief diagnostic write/flush errors are best-effort so a full Slurm log cannot
kill coordination. This does not suppress errors writing child-job outputs.
Unexpected fatal chief errors release locks and terminate children before
unregistering, avoiding stranded monitor threads and unsupervised duplicate
attempts. Deploy the new chief before starting engines; existing chief processes
retain their already-loaded implementation and are not hot-reloaded.

Regression tests inject quota/disk/I/O errors, exercise a real child after
recovery, check exact reservation/lease accounting, bounded backoff, cancellation
during log creation, and failed process/thread creation:

```bash
python -m pytest -q tests/test_chief_startup.py tests/test_dynamic_leases.py
```

### Claude Code skill

A ready-to-use **Claude Code skill** that teaches an agent to drive ZSlurm + Snellius is
bundled at [`.claude/skills/snellius-zslurm/`](../.claude/skills/snellius-zslurm/) — it
auto-loads for anyone using Claude Code in this repository. To make it available globally
(across all your projects), copy it into your user skills directory:

```bash
cp -r .claude/skills/snellius-zslurm ~/.claude/skills/
```

The skill covers the three scheduling pillars (depth-first/LIFO, memory packing across
heterogeneous nodes, storage budgets), a Snellius node/SBU/quota reference, JSON schemas,
worked transcripts, and the safety rails (SBU/core-hour limits, idempotency, poll cadence).
