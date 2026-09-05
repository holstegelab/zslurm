# LESSONS — snellius-offload

Append-only log of durable lessons. The high-signal ones are folded into `SKILL.md`
(per the self-improvement protocol — integrate, don't just append here).

## 2026-06-12 — a build offload hung silently ~3¼h; + two env-build gotchas
**Context:** offloaded a conda `proteomics` env build (rpy2 + R + ibidas). The agent
submitted the job, then went silent — its output stopped at 15:48 and never moved
while the build ran on the node to 17:22; it never detected completion and stayed hung
for ~3¼ h. The env itself built fine — **the agent's blocking wait was the failure.**

1. **Never block synchronously on an offloaded job.** SUBMIT, then POLL `zsoffload
   status` on a cadence (~15–30 s) **with control returning between polls** and a
   heartbeat. Put a **max-wait / iteration cap**: if the job hasn't reached a terminal
   state within a sane bound, or `status` stops changing, **STOP and report / hand
   back** — never wait indefinitely. A multi-poll silent stall with no failure signal
   IS the bug. (This skill is async by design; the hang came from ignoring that.)

2. **For an offloaded conda env build, verify against the TARGET env's versions.**
   The build copied a working `ibidas` egg from another env (NumPy 1.26) into the new
   env (NumPy 2.4); `np.unicode_` was removed in NumPy 2.0, so it was broken on arrival.
   Install lab/legacy packages from their **maintained source** (ibidas →
   `pip install git+https://github.com/mhulsman/ibidas3`, which is NumPy-2.0 compatible),
   never by blind egg-copy across NumPy majors. Then actually import-test in the target.

3. **rpy2/R envs need `R_HOME`.** Conda activation in the relocated base does NOT set it,
   so rpy2 fails with "Unable to determine R_HOME" on a bare `$env/bin/python` call.
   Add `etc/conda/activate.d/zz_r_home.sh` → `export R_HOME=$PREFIX/lib/R`.

(See also: the relocated-conda gotcha — a raw `mv` of a miniconda base leaves ~90
launcher shebangs pointing at the old `bin/python`; `conda` itself then dies with "bad
interpreter" even though each env's `bin/python` still runs. Fix: rewrite the dead
shebangs to the live base python. The package **cache** (`pkgs/`) is the inode hog —
`conda clean --all` reclaims it in place, no relocation needed.)
