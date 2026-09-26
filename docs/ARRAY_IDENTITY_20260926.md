# Pending Slurm array identity

Live Spider handover on 2026-09-26 preserved all 8,259 logical jobs, but exposed
an allocation census regression: `squeue -r -O jobid,arrayjobid,arraytaskid`
can report the *same* raw JobID for every unmaterialized pending array sibling.
Deduplicating the name and exact-ID query results by raw JobID counted two
pending arrays (18 + 15 pilots) as only two allocations. Autogrow consequently
submitted unnecessary pilots. It was disabled and the additional 31 pending
pilots were cancelled using Slurm's PENDING-state filter; running work and the
original allocations were retained.

Deduplication now uses ArrayJobID + ArrayTaskID for array rows. When raw JobID
equals the shared array base, per-pilot actions also use the indexed ID so a
single cancellation/hold cannot target every sibling. Distinct materialized
job IDs and non-array jobs retain their existing behavior. This is portable
across Spider and Snellius; it does not alter resource profiles.

An independent safety fix rejects an explicitly unknown manager name instead
of silently choosing the sole discovered manager. Omitted names retain the
single-instance convenience; existing alias records remain valid.

Regression: 217 tests and 15 subtests passed, including a real-shaped 18-task
pending array, duplicate query rows, exact single-pilot cancellation, existing
materialized-ID tests, and explicit missing-manager resolution. Deployment must
still use an orderly handover; committing does not change a running manager.
