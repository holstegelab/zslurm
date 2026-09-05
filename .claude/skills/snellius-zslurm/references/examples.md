# Worked examples

## Preflight and capability check

```bash
command -v zsbatch zsqueue zsnodes zsstatus zscontrol zslurm_lease
zsstatus --json | jq '{
  instance,
  client_schema:.schema_version,
  manager_schema:.health.schema_version,
  capabilities:.health,
  scheduler,
  budgets,
  alarms
}'
zsqueue --json | jq '{schema_version,fields}'
zsnodes --json | jq '{schema_version,fields}'
zscontrol status | jq '.dcache_transfers'
```

If `manager_schema` is 1, `priority` is absent from manager rows and
directional transfer state is unavailable. Do not restart during active work
just to change this.

## Two pipelines with strict waiting-job priority

```bash
snakemake --executor zslurm --zslurm-priority 200 --cores all target_a
snakemake --executor zslurm --zslurm-priority 100 --cores all target_b
zsqueue --parseable
```

The first run wins among eligible waiting jobs. Work from the second run that
is already running continues, and ineligible priority-200 work does not block an
eligible priority-100 job.

## Negative background priority

```bash
zsbatch --priority -100 -c 1 --mem 1000 -t 01:00:00 \
  -p compute -J background_check -- command
```

Default-priority jobs (`100`) dispatch first. Confirm `PRIO` in `zsqueue`.

## Directional dCache control

Persistent config:

```yaml
dcache_download_slots: 4
dcache_upload_slots: 2
```

Runtime mutation after authorization:

```bash
zscontrol status | jq '.dcache_transfers'
zscontrol transfer-slots --download 4 --upload 2
zscontrol status | jq '.dcache_transfers'
```

Snakemake inbound rule:

```python
resources:
    partition="archive",
    dcache_download_slots=1
```

Outbound rule:

```python
resources:
    dcache_upload_slots=1
```

## Durable budget stall

```bash
zsstatus --json | jq '.alarms[] | select(.alarm=="budget_stall")'
zscontrol whatif --active 210000
```

Only after verifying real quota and receiving authorization:

```bash
zscontrol budget --active 210000
```

Do not use `recompute-inuse` as a first response; first explain why accounting
drift exists and whether persistent producer output should remain charged.

## High-thread to low-thread lease

Inside a supported running ZSlurm child:

```bash
zslurm_lease --json status
align_with_24_threads
zslurm_lease set --cores 2 --mem-gb 12
merge_and_dechimer_with_2_threads
```

For a later parallel phase:

```bash
zslurm_lease set --cores 24 --mem-gb 48 --wait 3600
parallel_postprocess_with_24_threads
```

Check the exit code before starting the later phase. A timeout leaves the old
smaller lease in place.

## SSD-backed repeated scan

Snakemake resource idea:

```python
resources:
    ssd_use="required",
    ssd_gb=lambda wildcards, input: peak_local_gb(input.bam)
```

Job structure:

```bash
local_input="$TMPDIR/input.bam"
cp "$network_input" "$local_input"
samtools index "$local_input"
run_scan_one "$local_input"
run_scan_two "$local_input"
cp "$final_output" "$network_output"
```

The estimate must include input BAM, index, both scans' concurrent temp/output,
and headroom. Add checksums or application-level validation where required.

## Queue and node analysis

```bash
zsqueue --json | jq '
  [.instances[][]]
  | group_by([.priority,.partition,.state])
  | map({priority:.[0].priority,partition:.[0].partition,
         state:.[0].state,count:length})'

zsnodes --json | jq '
  [.instances[][]]
  | map({engine_id,partition,
         free_cores:(.cores-.res_cpu_reserved),
         free_mem_mb:(.totmem_mb-.res_mem_reserved_mb),
         has_ssd,
         free_ssd_gb:(.ssd_total_gb-.res_ssd_reserved_gb),
         sys_iowait_pct})'
```

## Post-run sizing

```bash
zsstats --json --files 'report*.tsv' --group jobname |
  jq '.[] | {
    jobname,
    used_core_hours,
    reserved_core_hours,
    cpu_efficiency,
    rss_max
  }'
```

Combine report data with phase-level local `du`/`df` logs when updating SSD
estimates. A final output-size report is insufficient for peak scratch use.
