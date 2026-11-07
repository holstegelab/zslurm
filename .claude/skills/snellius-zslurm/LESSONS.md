# LESSONS — snellius-zslurm

## 2026-10-28 — installed zsbatch walltime units can lag current source

An authorized download preflight found that PATH zsbatch submitted six hours as
360 seconds, while the maintained source submits 21600 seconds. Both source
and live job metadata confirmed the mismatch. Invoke the maintained source with
the relocated clustersnake interpreter and inspect requested seconds, priority
and resources after submission. Own affected jobs were stopped and terminal
states plus retained data reconciled before replacement; no manager settings
were changed. Integrated into SKILL.md capability discovery.
