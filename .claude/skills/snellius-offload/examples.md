# Worked transcripts (verified live)

All output is JSON. Run `zsoffload` from the `clustersnake` env or by absolute path
`/gpfs/home2/hulsmanm/projects/cluster_manager/zsoffload`.

## 1. Offload into an existing manager (the common case — no spend)

```console
$ export ZSLURM_OFFLOAD_AGENT=align-batch
$ zsoffload submit -J align -c 8 --mem 16000 -t 2:0:0 --key align-A -- /path/align_A.sh
{ "ok": true, "jobid": "772990", "instance": "zslurm_tcn1185", "replayed": false }

$ zsoffload status            # poll >=15s apart
{ "instance": "zslurm_tcn1185", "reachable": true,
  "owner": { "autostarted": false, ... },          # adopted -> we will NOT stop it
  "live_leases": [ { "agent_id": "align-batch", "jobids": ["772990"], "expires_in_s": 8900 } ],
  "pending_jobs": 0, "running_engines": 1, "alarms": [] }

$ zsoffload release          # when 772990 is terminal
{ "released": true, "agent_id": "align-batch" }
```

A retried submit with the same `--key` replays instead of double-submitting:
```console
$ zsoffload submit -J align -c 8 --mem 16000 -t 2:0:0 --key align-A -- /path/align_A.sh
{ "ok": true, "jobid": "772990", "instance": "zslurm_tcn1185", "replayed": true }
```

## 2. No manager exists → claim a node (the human's pattern, automated; SPENDS)

```console
$ zsoffload up --provision node --partition genoa --cores 24 --time 1-00:00:00 --max-nodes 2
{ "backend": "node", "instance": "zslurm_tcn1185", "is_ours": true, "provisioned": true,
  "slurm_jobid": "23489524",
  "status": { "engines": { "totals": { "engines": 1, "cores": 24.0, ... } }, ... } }
# manager + a 24-core local engine come up ON the compute node in ~20-30s, discoverable
# from the login node via ~/.zslurm/instances. Now `submit` as in example 1.
```

Inline shell needs a script (quoting is otherwise lost). Either:
```console
$ zsoffload submit -J j -c1 --mem 1000 -t 0:5:0 --key j1 -- /path/job.sh        # script file
$ zsoffload submit -J j -c1 --mem 1000 -t 0:5:0 --key j1 --shell -- 'a | b > c'  # --shell wraps it
```

## 3. Teardown is safe by construction

```console
$ zsoffload down               # a live lease ALWAYS blocks teardown (even --force)
{ "stopped": false, "reason": "1 live lease(s) hold the manager", "leases": ["align-batch"] }

$ zsoffload down              # on a FOREIGN manager (one we didn't provision)
{ "stopped": false, "reason": "no self-provisioned manager to stop" }

# after release + queue empty, a manager WE provisioned tears down cleanly:
$ zsoffload release && zsoffload down
{ "stopped": true, "backend": "node", "slurm_jobid": "23489524" }
```

## 4. Background janitor

```console
$ zsreap --json                       # prune expired (crashed-agent) leases, report
{ "pruned": ["crashed-agent-X"], "live_leases": 2, "action": "no_self_manager" }

$ zsreap --down-idle --grace 1800     # also stop OUR idle manager after 30 min idle
{ "action": "grace_pending", "idle_for_s": 420, "grace_s": 1800 }
```

## 5. Multi-agent: two agents, one manager

```console
# agent A and agent B each submit with their own ZSLURM_OFFLOAD_AGENT:
$ ZSLURM_OFFLOAD_AGENT=A zsoffload submit -J a -c2 --mem 1000 -t 0:10:0 --key a1 -- /p/a.sh
$ ZSLURM_OFFLOAD_AGENT=B zsoffload submit -J b -c1 --mem 500  -t 0:5:0  --key b1 -- /p/b.sh
$ zsoffload leases
{ "count": 2, "leases": [ {"agent_id":"A","jobids":["..."]}, {"agent_id":"B","jobids":["..."]} ] }
# both jobs pack onto the same shared engine(s); neither agent can tear the manager
# down while the other holds a lease.
```
