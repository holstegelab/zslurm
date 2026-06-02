# Worked transcripts

Real output shapes from the implemented interface (verified against a headless manager).
Run clients from the `clustersnake` env. With one running manager, `--instance` auto-resolves.

## 1. Preflight: is the manager up, and what's the state?
```console
$ zsstatus
{
  "ok": true, "instance": "zslurm_int6", "schema_version": 1,
  "health": { "ok": true, "controller_alive": true, "control_enabled": true,
              "lastin_first": true, "prio_fillmem_context": 500, "uptime_s": 29.7 },
  "scheduler": { "lastin_first": true, "prio_fillmem_context": 500,
                 "autogrow_enable": false, "autogrow_max_compute_nodes": 40 },
  "budgets": { "active": {"total":0.0,"inuse":0.0,"pending_add":0.0,"min_pending_add":null}, ... },
  "engines": { "totals": {"engines":0,"cores":0.0,"free_cores":0.0,"max_cores":0.0, ...} },
  "running_engines": 0, "pending_jobs": 0, "budgeted_pending_jobs": 0,
  "alarms": []
}
$ echo $?   # 0 ok; 2 = manager down (retry); 3 = ambiguous instance (pass --instance)
```

## 2. The budget-stall trap (and the fix)
Submit a job that needs 100 GB active scratch while budgets are still 0:
```console
$ zsbatch -c 2 --mem 1000 -t 0:5:0 -p compute --active-use-add 100 -J stage_sampleA -- sleep 1
Submitted batch job 262183
$ zsstatus | jq '.alarms'
[ { "alarm": "budget_stall", "tier": "active",
    "detail": "active budget full: inuse 0 + min pending add 100 > total 0 GB",
    "remedy": "raise the active budget (zscontrol budget --active <GB>) ... or let in-flight jobs release *_use_remove (keep LIFO on)" } ]
```
The job is PENDING with CPU/mem free — classic stall. Provision the budget:
```console
$ export ZSLURM_CONTROL_TOKEN=...      # never echo this in logs
$ zscontrol budget --active 1000 --dcache 500 --archive 2000
{ "ok": true, "budgets": { "active": {"total":1000.0,"inuse":0.0}, ... } }
$ zsstatus | jq '.alarms[].alarm'      # budget_stall is gone
"no_engines"
```

## 3. What-if before changing a budget
```console
$ zscontrol whatif --active 0
{ "proposed_totals": {"active":0.0, ...},
  "current_eligible_by_partition": {"compute": 2},
  "whatif_eligible_by_partition": {"compute": 1},
  "delta_by_partition": {"compute": -1} }   # lowering active to 0 would strand 1 job
```

## 4. Steer scheduling
```console
$ zscontrol lifo on            # depth-first: finish started pipelines first (default)
$ zscontrol context 1          # strict submission order (collapse greedy window)
$ zscontrol match sampleA      # DRY RUN first (blast-radius rail)
{ "ok": true, "matched": 2, "jobs": [["262183","stage_sampleA","PENDING","compute"], ...] }
$ zscontrol prioritize sampleA # LIFO-aware: pins that pipeline to dispatch first
{ "ok": true, "matched": 2, "lastin_first": true }
```

## 5. Idempotent submission (autonomous retry-safe) — via RPC
```python
import zslurm_shared
p = zslurm_shared.TimeoutServerProxy(zslurm_shared.get_job_url(instance="zslurm_int6"), allow_none=True)
# trailing arg is idempotency_key; a retried call with the same key returns the same jobid
args = ["job","cmd","/cwd",{},1.0,500,5,0,None, 0,0,0,0,0,0, "compute",0,"","",'no',0,None, "KEY-XYZ"]
j1 = p.submit_job(*args); j2 = p.submit_job(*args)
assert j1 == j2          # no double-submit
```

## 6. Gating & exit codes
```console
$ zscontrol --token WRONG budget --active 5 ; echo $?
zscontrol: rejected: invalid control token
3                                 # logical rejection — do NOT retry
$ zsstatus            # with 2 managers and no --instance:
zsstatus: multiple instances ['zslurm_int6','zslurm_int6_1']; select one with --instance
$ echo $?
3
```

## 7. Start a manager unattended (only if authorized)
```console
$ zslurm --headless --enable-control --control-token "$TOK" --no-autogrow
{"ok": true, "instance": "zslurm_int6", "job_url": "http://int6:38865/PIBHjbwm", "control_enabled": true}
# prints one JSON banner with the endpoint, then runs until SIGTERM (clean shutdown).
# Omit --no-autogrow only after setting an SBU-aware autogrow cap (zscontrol autogrow --max-nodes N).
```

## 8. Forecast a DAG before submitting (Phase 3)
Write the planned jobs to a JSON list and prove the budgets hold first:
```console
$ cat plan.json
[ {"name":"j1","active_use_add":300,"active_use_remove":300},
  {"name":"j2","active_use_add":300,"active_use_remove":300},
  ... 6 such jobs ... ]
$ zscontrol forecast plan.json | jq '{verdict, active: .budgets.active}'
{ "verdict": "ORDER_SENSITIVE",
  "active": { "total":1000, "current_inuse":0, "worst_case_peak":1800,
              "optimistic_peak":300, "first_blocking_job":null, "verdict":"ORDER_SENSITIVE" } }
# worst_case 1800 > total 1000 but optimistic 300 fits -> keep LIFO on and submit; it drains.
# A single 1500-GB job would be "INFEASIBLE" with first_blocking_job set -> raise budget or split.
$ echo $?   # 0 (forecast is a read; branch on .verdict, not the exit code)
```

## 9. Repair budget drift / inspect running jobs (Phase 3)
```console
$ zscontrol jobs --states RUNNING | jq '.jobs[] | {name, mem_pressure, requeue_remaining}'
$ zscontrol recompute-inuse | jq          # reset inuse to the sum over running jobs
{ "ok": true, "before": {"active":500.0,...}, "after": {"active":0.0,...} }
```

## 10. Scale the engine fleet (Phase 4, gated + --yes)
```console
$ zscontrol grow 4 --partition genoa --constraint scratch-node --time 1-0:0:0 --yes
# refuses without --yes or with a bad token (exit 3, no sbatch). CHECK budget-overview FIRST.
$ zscontrol shrink 2          # stop 2 engines (queued > idle > oldest)
```

## 11. Post-run cost / mem re-tuning
```console
$ zsstats --json --files 'report*.tsv' --group jobname | jq '.[] | {jobname, used_core_hours, reserved_core_hours, cpu_efficiency, rss_max}'
# Use rss_max to set the next run's mem_mb; cpu_efficiency<<1 means over-reserved cores (wasted SBU).
```
