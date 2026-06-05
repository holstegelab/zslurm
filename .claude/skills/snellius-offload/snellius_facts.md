# Snellius facts for offloading (refreshed 2026-06, verified vs SURF servicedesk)

> Re-check with `sinfo`, `scontrol show partition`, and `budget-overview`. Sources:
> SURF servicedesk "Snellius partitions" (pages 30660209) and "Snellius filesystems"
> (85295828).

## Partitions WE CAN USE

We have access to **thin (rome/genoa), fat (fat_rome/fat_genoa), and staging** —
**NOT himem, NOT gpu**. `zsoffload up --provision node` refuses anything else.

| Partition | Cores | RAM (usable) | GB/core | SBU/core-h | Partial min (1/8) |
|---|---|---|---|---|---|
| `rome` (thin)  | 128 | ~224 GiB  | ~1.75 | **1.0** | **16 cores** |
| `genoa` (thin) | 192 | ~336 GiB  | ~1.75 | **1.0** | **24 cores** |
| `fat_rome`     | 128 | ~960 GiB  | ~7.5  | **1.5** | 16 cores |
| `fat_genoa`    | 192 | ~1440 GiB | ~7.5  | **1.5** | 24 cores |
| `staging`/`cbuild` | ~16 thr | ~224 GiB | — | **2.0** | 1 thread (dCache I/O, not compute) |
| ~~himem_4tb/8tb~~ | — | — | — | 2.0 / 3.0 | **no access** |
| ~~gpu_*~~ | — | — | — | per-GPU | **no access** |

- **Partial-node allocations are allowed on every partition, down to 1/8 of a node**
  (genoa 24c, rome 16c). This is the cost lever: a 1/8 genoa engine is 24 SBU/h vs
  192 for a whole node. The offloader's node base uses the 1/8 minimum by default.
- **Whole-node billing applies to the cores you hold**: an idle core on an allocated
  *whole* node still bills — which is why zslurm packs many jobs per engine. Partial
  allocations let you hold (and bill) just a slice.
- **Max walltime: 120 h (5 days).** Always set `-t`. `zsoffload up` caps `--time` at 5d.
- `compute` autogrow grows **whole** nodes (exclusive); the node base + explicit
  `grow --cores N` can be **partial**. Pick **cheapest partition whose GB/core ≥ the
  job's need**: ≤1.75 → genoa/rome (1.0); ≤7.5 → fat_* (1.5).

## Filesystems → visible to an offloaded (compute-node) job?

| FS | Path | Quota | Retention | Shared across nodes? |
|---|---|---|---|---|
| Home | `/home/<u>` | 200 GiB, 1M files | 15 wk post-project; **backed up** | **yes** |
| Project | `/projects/<p>`, `/gpfs/work*/...` | per-grant TiB | project duration | **yes** |
| Scratch-shared | `/scratch-shared/<u>` | 8 TiB (shared pool), 14-day delete | none | **yes** |
| Scratch-local | `/scratch-local/<u>` (`$TMPDIR`) | 8 TiB (same pool), 6-day delete | none | **NO — node-local** |
| Scratch-node | `/scratch-node` NVMe | ~5.9 TiB/job, wiped at job end | none | **NO — per-job** |
| Archive/dCache | `/archive/<u>` tape | request-based | project duration; **backed up** | staging only |

**Offloaded jobs must read/write shared FS** (home, projects/gpfs, scratch-shared).
The login node's `/scratch-local` / `/tmp` are **not** visible on the compute node.
For heavy job-local I/O, request node-local NVMe via zslurm `--ssd-use required
--ssd-gb G` (lands on a `--constraint=scratch-node` node) and use `$TMPDIR` there.

## Budget

- Account `vuh15318` (NWO-2025.046-A/L1). Check with **`budget-overview -p <part>`**
  (folds in live Slurm; accurate) rather than `accinfo`/`accuse` (~24 h lag).
- Project budget is shared & fair-share scheduled: heavy use lowers the whole
  group's priority. Keep the node base small and `--max-nodes` low.
