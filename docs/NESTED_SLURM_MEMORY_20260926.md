# Nested Slurm memory environment correction

Observed on Spider: a manager allocated 8 GiB with per-node memory submitted
eight-core pilots using the partition's 8,000 MiB/core default. Slurm allocated
64,000 MiB, but exported both the child's `SLURM_MEM_PER_CPU=8000` and inherited
`SLURM_MEM_PER_NODE=8192`. The chief preferred the stale per-node value and
advertised only 7,444.64 MiB after headroom; no queued 8,000 MiB job could start.

The manager now removes parent `SLURM_MEM_PER_NODE`, `SLURM_MEM_PER_CPU`, and
`SLURM_MEM_PER_GPU` from the environment passed to sbatch. It does not modify
its own environment or erase intentional SBATCH request settings.

For previously queued pilots and older managers, the chief also resolves
conflicting memory modes against `scontrol show job` for its exact, single-node
allocation. The query has a five-second timeout and verifies job ID, node count
and CPU count. Failure uses the smaller environmental limit; it never guesses
the larger one. Existing cgroup/explicit/physical caps and headroom still apply.
Unambiguous and mutually consistent environment values need no controller call.
This supports both Spider's per-CPU grant and Snellius's per-node grant.

Focused memory/submission/site regression: **35 passed**. Full local suite:
**183 passed, 4 failed**, with 13 subtests passed. The identical four failures
were reproduced from untouched HEAD e5627c8 in an isolated archive: three
queued-scratch planning expectations and the headless-handover subprocess's
missing numpy environment. These are not claimed fixed by this patch.

An AST-only compute-node preflight of the minimal installed-chief backport used
the actual two chief environments on wn-dc-11. Both resolved **64,000 MiB** from
their Slurm allocations instead of 8,192. Both chiefs had zero child processes.
Installed Spider code predates other current-main changes, so the operational
backport contains only the memory resolver/import; no unrelated worker behavior
is silently deployed. The live manager need not lose/recreate its logical queue.
Its submission sanitization takes effect on a later orderly manager upgrade;
the chief-side resolver covers its pilots in the meantime.

After merging current upstream main (through 156f678), its scratch-test updates
resolve the first three baseline failures. Preserving the existing user-site
packages in the subprocess PYTHONPATH resolves the isolated-HOME test's numpy
dependency without changing test or production behavior. The merged full suite
passed **212 tests and 15 subtests** before the following additional guard.

## Separate startup walltime race

Live startup exposed another issue: before the first Slurm observation, an
engine's five-day placeholder could admit a 48-hour task on a 30-hour pilot.
The manager now waits for the actual Slurm RUNNING observation before assigning
work to a cluster-backed engine. Local non-Slurm engines are unaffected. A
regression demonstrates unknown-state refusal, rejection of a 48-hour job on
the observed 30-hour allocation, and admission of a fitting one-hour job.
This manager-side correction requires an orderly manager upgrade; the minimal
chief backport does not change code already loaded by a live manager.
