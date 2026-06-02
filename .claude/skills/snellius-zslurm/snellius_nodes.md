# SURF Snellius reference (for zslurm sizing & cost)

> ⚠️ **May go stale.** SURF changes hardware/policy across build phases. Verify node
> states with `zsoccupancy --json` / `scontrol show nodes`, and budget with
> `budget-overview`. Sourced from servicedesk.surf.nl (Snellius hardware / partitions &
> accounting / filesystems / SLURM batch system).

## Partitions / nodes

| Partition | Cores | RAM (usable) | GB/core | SBU weight | Local NVMe |
|---|---|---|---|---|---|
| `rome` (thin) | 128 | ~224 GiB | ~1.75 | **1.0** /core-h | some phases |
| `genoa` (thin) | 192 | ~336 GiB | ~1.75 | **1.0** /core-h | some phases |
| `fat_rome` | 128 | ~960 GiB | ~7.5 | **1.5** /core-h | 6.4 TB |
| `fat_genoa` | 192 | ~1440 GiB | ~7.5 | **1.5** /core-h | 6.4 TB |
| `himem_4tb` | 128 | ~3840 GiB | ~30 | **2.0** /core-h | — |
| `himem_8tb` | 128 | ~7680 GiB | ~60 | **3.0** /core-h | — |
| `gpu_a100` | 72 | ~480 GiB | — | **128** /GPU-h | some 7.68 TB |
| `gpu_h100` | 64 | ~720 GiB | — | **192** /GPU-h | ~22 nodes |
| `gpu_mig` | 72 | ~480 GiB | — | **64** /MIG-h | — |
| `gpu_vis` | 72 | ~480 GiB | — | 128 /GPU-h | — (24h max) |
| `staging` / `cbuild` | ~16 thr | ~224 GiB | — | **2.0** /thr-h | dCache I/O, not compute |

- **No partition literally named `compute`** on Snellius. In zslurm, `compute` is an internal
  *job class* (vs `archive`); the Slurm engine partitions are `rome`/`genoa`/`fat_*`/`staging`.
  Sizing decisions still map to the table above. `node_profiles` defaults in zslurm:
  genoa 192c/336GB, rome 128c/224GB, fat_genoa 192c/1440GB, fat_rome 128c/960GB.
- Whole-node billing on CPU partitions: an **idle core on an allocated node still costs**
  its SBU rate — the reason zslurm packs many tasks per allocation. GPU partitions bill
  fractional (per-GPU / quarter-node).
- Worked SBU: full Rome node 1 h = 128 SBU; full Genoa node 1 h = 192 SBU; 1 A100 1 h = 128
  SBU; full H100 node (4 GPU) 24 h = 18 432 SBU.

## GB/core → partition picker (Pillar 2)
`mem_mb / n` (job mem-per-core) vs the node ratio:
- ≤ ~1.75 GB/core → `genoa`/`rome` (cheapest, 1.0 SBU)
- ≤ ~7.5 GB/core → `fat_genoa`/`fat_rome` (1.5 SBU)
- ≤ ~30 → `himem_4tb` (2.0); ≤ ~60 → `himem_8tb` (3.0)
Pick the **cheapest** partition whose GB/core ≥ the job's need. Never request more
cores/mem than the node has (silently un-eligible).

## Filesystems → which storage budget (Pillar 3)
| FS | Path | Quota | Retention | Budget tier |
|---|---|---|---|---|
| Home | `/home/<u>` | 200 GiB | backed up, 15 wk post-project | — (not bulk) |
| Project | `/projects/<p>` | per-grant TiB, per-group | deleted 4 wk post-project | `active` (persistent working) |
| Scratch-shared | `/scratch-shared/<u>` | 8 TiB (shared pool) | **14-day** auto-delete | `active` / `dcache` staging |
| Scratch-local | `/scratch-local/<u>` (`$TMPDIR`) | 8 TiB (same pool) | **6-day** auto-delete | per-job temp |
| Scratch-node | `/scratch-node` NVMe | ~5.9 TiB/job | **deleted at job end** | `--ssd-use required` |
| Archive/dCache | `/archive/<u>` tape | large | project duration | `archive` (tape staging) |

The 8 TiB scratch quota is **shared** between scratch-shared and scratch-local — size the
`active`/`dcache` budgets against it so the run never trips the quota.

## SLURM / budget facts
- Max walltime **120 h** (gpu_vis 24 h). Always set `-t`.
- Node-local NVMe: `--constraint=scratch-node` (zslurm: `--ssd-use required --ssd-gb G`).
- Budget: **`budget-overview`** (combines central accounting + live Slurm — accurate) >
  `accinfo`/`accuse` (central server lags ~24 h). Project budget shared by all project
  users; **fair-share** scheduling (heavy use lowers the whole group's priority).
