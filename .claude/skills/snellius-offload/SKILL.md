---
name: snellius-offload
description: >-
  Offload heavy or long-running work OFF the Snellius login (UI) node onto a
  SHARED zslurm manager, with multi-agent coordination so several Claude agents
  (and the human) cooperate on one manager instead of each starting their own or
  tearing each other's down. Use whenever a command would run more than a couple
  of minutes, use many cores, or use a lot of memory; when the login node is
  overloaded; or when asked to "offload this", "run this on a worker/compute
  node", "don't run that here", "submit this to the cluster". Wraps zslurm with
  `zsoffload` (submit/up/status/release) + `zsreap` (background janitor).
  Triggers: "offload", "login node busy", "run on a node", "too many cores",
  "this will take a while", "zsoffload", "background this job".
---

# Offloading work off the Snellius login node (shared zslurm)

The login node (`int*`) is shared by ~100 users and **kills processes that run too
long or use too much CPU**. Anything non-trivial must run on a **compute node**.
This skill drives `zsoffload`, a thin coordination layer over the zslurm pilot-job
scheduler (deep zslurm details: the **`snellius-zslurm`** skill). The tooling lives
in `/gpfs/home2/hulsmanm/projects/cluster_manager/` (`zsoffload`, `zsreap`,
`zslurm_coord.py`); state is under `~/.zslurm/`.

## When to offload (the decision)

Offload if **any** of these holds — otherwise just run it inline:

| Offload it | Run inline on the login node |
|---|---|
| est. runtime **> ~2 min** | quick (< a minute) one-shots |
| **> 2 cores** or parallel/threaded | single-core, light |
| **> ~2 GB** memory | small memory |
| heavy tools: bwa, bowtie, STAR, samtools sort/markdup, gatk, bcftools on big files, plink, R/python heavy compute, alignment, sorting, joint calling | `ls`, `grep`, small file edits, quick `python`/`R` snippets, `head`/`awk` on small files, git |
| a loop that launches many such commands | interactive exploration |

When unsure, offload — it is cheap (jobs pack onto a shared node) and keeps the UI
responsive. **Two key gotchas before you submit:**
- **Submit a SCRIPT FILE, not an inline shell one-liner.** zslurm space-joins the
  command, so `bash -c '... | ... > f'` loses its quoting. Either pass a script
  path (`zsoffload submit ... -- /path/to/job.sh`, shebang honoured) or use
  `--shell` (the broker writes your line to a script for you). Plain argv with no
  shell metacharacters (`bwa mem ref r1 r2`) is fine as-is.
- **The job runs on a compute node**, so it only sees **shared** filesystems:
  `/home`, `/projects`, `/gpfs/...`, `/scratch-shared`. It does **not** see the
  login node's `/scratch-local`/`/tmp`. Use absolute paths on shared storage.

## The 4-command workflow

```bash
# 0. (once per agent) a stable id so your lease/idempotency persist across calls
export ZSLURM_OFFLOAD_AGENT=<short-task-slug>     # e.g. hla-align-batch

# 1. SUBMIT — adopts whatever shared manager exists, stakes a lease, submits.
#    NEVER spends SBU. Returns {ok, jobid, instance}. Always pass --key (retry-safe).
zsoffload submit -J align -c 8 --mem 16000 -t 2:0:0 --key align-sampleA \
    -- /path/to/align_sampleA.sh

# 2. POLL — one JSON blob: manager health, your leases, pending/running, alarms.
#    Poll no faster than every ~15s.
zsoffload status

# 3. RELEASE — drop your lease when your jobs are terminal (frees the manager to
#    be reaped if nobody else needs it). Optionally reap if you provisioned it.
zsoffload release            # add --reap only if YOU ran `up` and want it gone
```

`zsoffload` is on PATH only inside the `clustersnake` conda env, OR call it by
absolute path `/gpfs/home2/hulsmanm/projects/cluster_manager/zsoffload` (it has the
right shebang and finds the zs* clients itself — no `conda activate` needed).

If `submit` returns `"no shared manager reachable; run zsoffload up first"`, there
is **no manager running**. Decide (see next section) whether to provision one.

## The shared-manager model (why coordination exists)

There are two very different resources:
- **The manager** — one process that schedules jobs. On a compute node it is
  effectively free (it rides on a node you're paying for anyway); on the login node
  it is free but fragile. It is a **shared singleton**: all agents + the human use
  one. Discovery is via `~/.zslurm/instances/*.yaml` (shared home) so it is found
  no matter which node it runs on.
- **Engines** — whole/partial Slurm node allocations that actually run jobs. These
  **cost SBU**. They auto-shrink when idle (`autoconsolidate`).

Three situations `zsoffload` handles for you:
1. **The human already has a zslurm running** (their usual: claim a node → start
   zslurm + a local engine there). → **Adopt it, submit, NEVER stop it.**
2. **Another agent already provisioned one.** → Adopt + add your lease.
3. **None exists.** → You may provision one with `zsoffload up` (see below).

**Coordination guarantees** (built in, tested):
- A **lock** (`flock` on `~/.zslurm/manager.lock`) serializes start/stop so two
  agents can't create two managers (the `zslurm_int5 + zslurm_int5_1` collision).
- A **lease registry** (`~/.zslurm/leases/<agent>.json`, TTL = job walltime + 30 min)
  is your "near-term intent". `submit` writes it **before** submitting, under the
  lock, so no one can tear the manager down in your submit gap. A **live lease
  always blocks teardown — even `down --force`.**
- **Never-stop-foreign**: teardown only ever touches a manager THIS tooling
  provisioned (recorded in `~/.zslurm/offloader.json` with `autostarted=true` +
  pid/jobid identity, re-verified before any signal). The human's / another agent's
  manager is adopt-only.
- Crashed agents self-heal: their lease expires (TTL) and `zsreap` prunes it.

## Provisioning capacity — `zsoffload up` (the only command that may SPEND)

```bash
# adopt-only (default): use an existing manager, refuse to spend
zsoffload up

# claim a node running manager + a local engine (the human's pattern, automated).
# SPENDS SBU: a 1/8-node slice (genoa=24c, rome=16c), plus capped whole-node
# autogrow overflow up to --max-nodes (auto-shrunk when idle).
# Defaults: --time = 5-day MAX (the reaper/autoconsolidate release it early when
# idle, so max walltime just avoids premature expiry); --partition = auto (picks
# the cheapest genoa/rome/fat_* that currently has room, via zsoccupancy).
zsoffload up --provision node                          # 5-day, auto thin partition, 24c
zsoffload up --provision node --partition auto_fat     # fat nodes (~7.5 GB/core) for hi-mem
zsoffload up --provision node --cores 24 --max-nodes 2 # explicit

# free but fragile: headless manager on the login node (short bursts / tests only;
# the login node may kill it). Capped autogrow still adds compute nodes on demand.
zsoffload up --provision login --max-nodes 2
```

**Decide whether to provision** (don't spend reflexively):
- If you only have a few small/short jobs and no manager exists, prefer asking the
  human to start their usual node-manager, or use `--provision login` for a quick
  burst, OR just note that the work is queued and needs capacity.
- For real batch work, `--provision node` with a small `--cores` (1/8 node) base +
  a low `--max-nodes` cap is the cost-efficient choice: the base node does real
  compute, overflow nodes appear only under load and disappear when idle.
- **SBU rail:** before raising `--max-nodes` or `--cores`, check `budget-overview`
  (the accurate one). The base node bills `cores × walltime × partition-SBU`
  (genoa/rome = 1.0, fat = 1.5). Keep partition ∈ genoa/rome/fat_* — **we have NO
  access to himem or gpu** (the broker refuses them).

When you provisioned a manager and your work is done, `zsoffload down` stops it
(only if idle + no leases). Or let `zsreap` do it (below).

## Background cleanup — `zsreap`

A janitor safe to run repeatedly (cron / `loop`). It prunes expired leases and,
with `--down-idle`, tears down a **self-provisioned** idle manager after a grace
period (never a foreign one). Idle time is tracked across runs, keyed to the
manager's identity.

```bash
zsreap                                   # prune expired leases + report (no teardown)
zsreap --down-idle --grace 1800          # also stop OUR idle manager after 30 min idle
```

Run it from the harness `/loop` or a cron every ~15–30 min when you have offloaded
work in flight, so crashed-agent leases and finished self-managers get cleaned up.

## Multi-agent collaboration (you are probably not alone)

- **Always** set a stable `ZSLURM_OFFLOAD_AGENT` so your lease is yours and your
  `--key` idempotency survives retries.
- **Never** `kill`/`scancel`/SIGTERM a zslurm manager or engine directly, and never
  run `zsoffload down`/`zsreap --down-idle` on a manager you didn't provision — the
  tooling already refuses, but don't fight it. To stop using the manager, just
  `zsoffload release`.
- **Never** start `zslurm`/`zslurm --headless` by hand — use `zsoffload up`, which
  adopts-or-provisions under the lock. A hand-started second manager causes the
  "multiple instances" collision.
- Check `zsoffload status` → `live_leases` to see who else is using the manager.
- `submit` is idempotent on `--key`: a retried identical submit returns the same
  jobid instead of double-submitting.

## Reference

- Partitions we can use, partial-node minimums, filesystems, SBU rates:
  [`snellius_facts.md`](snellius_facts.md).
- Deep zslurm mechanics (budgets, LIFO, memory packing, autogrow internals,
  `zsstatus`/`zscontrol`): the **`snellius-zslurm`** skill.
- Worked transcripts: [`examples.md`](examples.md).

## SAFETY RAILS (MUST / NEVER)

- **NEVER** run heavy/long work directly on the login node — offload it.
- **NEVER** stop, kill, or `down` a manager you did not provision (the human's
  especially). Releasing your lease is how you "let go".
- **`up --provision node/login` is the ONLY command that spends SBU.** `submit`,
  `status`, `release`, `leases` never spend.
- **SBU rail:** check `budget-overview` before raising `--max-nodes`/`--cores`;
  keep the base node small (1/8) and the cap low. Only genoa/rome/fat_* (no himem/gpu).
- **Poll `zsoffload status` no faster than ~15s.**
- **Submit a script file (or `--shell`)** for anything with pipes/redirects/quotes.
- **Use shared-FS absolute paths** in offloaded jobs (no login-node `/tmp`/`/scratch-local`).
- **Set `ZSLURM_OFFLOAD_AGENT` and always pass `--key`** (retry-safe, lease-correct).
