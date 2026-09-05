# Snellius nodes, storage, and accounting

> Verified against SURF documentation on 2026-07-29. Node counts and policy can
> change; recheck `sinfo`, `zsoccupancy --json`, `myquota`, and
> `budget-overview` before operational decisions.

Official references:

- https://servicedesk.surf.nl/wiki/spaces/WIKI/pages/30660209/Snellius+partitions
- https://servicedesk.surf.nl/wiki/spaces/WIKI/pages/85295828/Snellius+filesystems
- https://servicedesk.surf.nl/wiki/spaces/WIKI/pages/300974109/Accounting
- https://servicedesk.surf.nl/wiki/spaces/WIKI/pages/300974114/Shared+usage+accounting

## CPU and service partitions

| Slurm partition | Cores/node | Available RAM | GiB/core | Smallest allocation | SBU weight |
|---|---:|---:|---:|---:|---:|
| `rome` | 128 | 224 GiB | 1.75 | 16 cores / 28 GiB | 1.0 per CPU-hour |
| `genoa` | 192 | 336 GiB | 1.75 | 24 cores / 42 GiB | 1.0 |
| `fat_rome` | 128 | 960 GiB | 7.5 | 16 cores / 120 GiB | 1.5 |
| `fat_genoa` | 192 | 1440 GiB | 7.5 | 24 cores / 180 GiB | 1.5 |
| `himem_4tb` | 128 | 3840 GiB | 30 | 16 cores / 480 GiB | 2.0 |
| `himem_8tb` | 128 | 7680 GiB | 60 | 16 cores / 960 GiB | 3.0 |
| `staging` | 16 physical / 32 SMT threads | 224 GiB | service node | 1 thread / 7 GiB | 2.0 per CPU-hour |
| `cbuild` | 16 physical / 32 SMT threads | 224 GiB | service node | 1 thread / 7 GiB | 2.0 |

Maximum walltime is 120 hours except `gpu_vis` (24 hours). ZSlurm's
manager-side `compute` and `archive` job classes are not literal Snellius
partition names. Compute engines use partitions such as `genoa`; archive jobs
use staging engines.

ZSlurm's current default node profiles match the table for `rome`, `genoa`,
`fat_rome`, and `fat_genoa`. Inspect current config/source before assuming which
profiles autogrow can select.

## Memory-per-core picker

For a single job, compare requested `mem_mb / n` with node ratios:

- up to about 1.75 GiB/core: `rome` or `genoa`;
- up to about 7.5 GiB/core: `fat_rome` or `fat_genoa`;
- up to about 30 GiB/core: `himem_4tb`;
- up to about 60 GiB/core: `himem_8tb`.

This is a placement guide, not a license to request fractional Slurm
allocations directly. ZSlurm packs child jobs inside whole-node engines.

Never request more cores or memory than one eligible engine provides. Such a
job remains pending.

## Shared-resource accounting nuance

Direct single-node Slurm jobs are accounted in 1/8-node increments on CPU
nodes, using whichever of CPU or memory consumes the larger fraction.
Multi-node jobs are exclusive.

ZSlurm itself acquires engine allocations and multiplexes children inside them.
SBU cost follows the engine allocation's elapsed time and node type, not each
child's fractional reservation. Idle capacity inside an engine therefore still
costs the project; packing and timely shrink matter.

Before growing:

1. inspect live and queued engines;
2. calculate full engine core-hours times SBU weight;
3. inspect `budget-overview` for current project budget;
4. set a conservative `autogrow_max_compute_nodes`.

## Filesystems

| Storage | Mount | Capacity/retention | ZSlurm meaning |
|---|---|---|---|
| Home | `/home/<user>` | 200 GiB; backed up | code/config, not bulk I/O |
| Scratch-local | `/scratch-local/<user>` | shared 8 TiB scratch quota; files >6 days removed | GPFS, node-specific namespace; still network storage |
| Scratch-shared | `/scratch-shared/<user>` | same shared 8 TiB quota; files >14 days removed | shared GPFS |
| Scratch-node | `/scratch-node/<job-user-dir>` | no persistent quota; deleted at job end | true node-local NVMe, modeled by `ssd_use/ssd_gb` |
| Project | `/projects/<project>` | project-specific group quota; no backup | durable active working space |
| Archive | `/archive/<user>` | tape-backed; login/staging access; nightly backup | archive tier/staging |
| Grid/dCache | remote/API/mount tooling | independent distributed disk+tape service | durable dCache tier plus transfer slots |

Critical distinction:

- `/scratch-local` looks node-specific but is backed by the same GPFS system as
  scratch-shared and contributes to the same per-user quota. It does not offload
  network storage.
- `/scratch-node` is true node-local NVMe. Requesting a scratch-node engine
  makes `$TMPDIR` point there; without that constraint `$TMPDIR` normally points
  into `/scratch-local`.

Never use `/tmp` or `/var/tmp` for pipeline bulk data.

SURF reports roughly 5.9 TiB in an example full-node scratch mount, but partial
allocations can receive a proportional part of local NVMe. ZSlurm reserves
reported per-engine capacity; inspect `zsnodes --json` instead of hardcoding a
device size.

All scratch-node data disappears when the engine allocation/job filesystem
ends. Copy final artifacts out and make failure cleanup/recovery explicit.
