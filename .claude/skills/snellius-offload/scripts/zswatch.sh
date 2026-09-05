#!/bin/bash
# zswatch — background completion/failure watchdog for an offloaded zslurm job.
#
# Purpose: fire ONE notification the moment an offloaded job COMPLETES or FAILS, without the agent
# blocking. Run it via the harness `Bash(run_in_background: true)`; when it exits, the agent is
# re-invoked once. It watches TWO signals so it can't go silent on a death the log never records:
#   (a) the LOG file  — polled every ~15s for a success marker OR a crash signature (cheap);
#   (b) the JOB STATE — polled ~every 2 min via zsqueue (respects "poll no faster than ~15s"; the job
#       state is the authoritative catch for SIGKILL / OOM / node-fail / cancelled-while-pending, which
#       never reach the log). Fires when the job shows a terminal state OR leaves the queue after being
#       seen (done/failed/cancelled all remove it).
# Coverage: matches success AND failure — silence is never mistaken for success.
#
# Usage:  zswatch.sh <jobid> <logfile> [success_regex] [max_minutes]
#   success_regex : extra success marker(s) to grep in the log (default DONE|COMPLETE|TOTAL|PASS)
#   max_minutes   : bail after this long even if nothing seen (default 90)
# Env overrides: ZSWATCH_PY (clustersnake python), ZSWATCH_ZSQUEUE (zsqueue source script).
set -u
JOBID="${1:?usage: zswatch.sh <jobid> <logfile> [success_regex] [max_minutes]}"
LOG="${2:?need logfile}"
SUCCESS="${3:-DONE|COMPLETE|TOTAL|PASS|SUCCESS}"
MAXMIN="${4:-90}"
CRASH='Traceback \(most recent call|AssertionError|[Ee]rror:|Killed|MemoryError|Segmentation fault|OOM|core dumped|CalledProcessError'
# zsqueue's console-script shebang points at a dead conda; run its SOURCE script under the clustersnake python.
PY="${ZSWATCH_PY:-/home/hulsmanm/all_data/research/conda/marc/miniconda3/envs/clustersnake/bin/python}"
ZSQ="${ZSWATCH_ZSQUEUE:-/home/hulsmanm/projects/cluster_manager/zsqueue}"

iters=$(( MAXMIN * 60 / 15 )); seen=0; reason=""
for i in $(seq 1 "$iters"); do
  # (a) log markers — every 15s
  if [ -f "$LOG" ] && grep -qE "$SUCCESS|$CRASH" "$LOG" 2>/dev/null; then reason="log-marker"; break; fi
  # (b) job state — every ~2 min
  if [ $(( i % 8 )) -eq 0 ] && [ -x "$PY" ] && [ -f "$ZSQ" ]; then
    st=$( timeout 25 "$PY" "$ZSQ" 2>/dev/null | awk -v j="$JOBID" '$1==j {print $5}' )
    if [ -n "$st" ]; then
      seen=1
      case "$st" in
        COMPLETED|FAILED|CANCELLED|TIMEOUT|OUT_OF_MEMORY|NODE_FAIL|PREEMPTED|DEADLINE)
          reason="state:$st"; break;;
      esac
    elif [ "$seen" = 1 ]; then
      reason="left-queue(terminal)"; break        # was RUNNING/PENDING, now gone -> done/failed/cancelled
    fi
  fi
  sleep 15
done
[ -z "$reason" ] && reason="max-wait ${MAXMIN}m (no terminal signal — likely a stall; investigate)"
echo "=== zswatch: job $JOBID terminal — $reason (~$(( i * 15 ))s) ==="
if [ -f "$LOG" ]; then tail -15 "$LOG"; else echo "(no log file at $LOG)"; fi
