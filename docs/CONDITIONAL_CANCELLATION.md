# Pending-only cancellation during a workflow handoff

The compatible optional third argument to `cancel_job(jobid, False, 'PENDING')`
performs compare-and-cancel under the same manager lock as dispatch. It returns
`{cancelled, jobid, state}`. Missing, ASSIGNED or RUNNING jobs are left unchanged;
normal pending cancellation remains terminal even with retries configured.
Other requested states and conditional requeue are rejected. Existing one/two
argument clients keep their previous behavior and return value.

The manager advertises `conditional_pending_cancel` in health. Operators must
check this capability before using the extension; do not fall back to an
unconditional cancellation when an older manager rejects it. Existing handover
forwarding passes the full positional argument list to the current manager.

This avoids the observation/dispatch race in a transition that stops a producer,
removes only its old pending queue, and lets already assigned/running work finish.
It does not select the owner's jobs, stop the producer, or guarantee the queue
is drained. Those remain explicit caller responsibilities. This is scheduler
state logic, with no Spider/Snellius profile or allocation changes.
