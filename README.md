Installation
------------

1. Install conda in folder $HOME/miniconda3
2. Create the clustersnake environment with conda env create -f env.yaml
4. conda activate clustersnake
5. Install with python setup.py install


Update
------
1. git pull origin
2. conda activate clustersnake
3. python setup.py install

(if installed in other environments, also update there with 'python setup.py install')


Quick start
-----------

1. run 'zslurm'
2. start a local engine ('s')   or slurm engines ('d'). 
3. submit jobs with 'zsbatch', similar to 'sbatch'. 


User interface commands
-----------------------
* 's': start a local engine
* 'x': stop a local engine
* 'd': start slurm engines. Fill in the partition. Note that if you fill in 'staging' it will start 'archive' engines that are specifically used for transferring data to archive. 
* 'c': stop slurm engines.
* 'o': indicate engines to phaseout (by node name). These engines will not accept new jobs, and will stop once all jobs on that engine are done. 
* 'k': autostop engines: stop engines if there are no new jobs

* 'p': prioritize jobs with a specific pattern in jobname
* 'n': deprioritize jobs with a specific pattern in jobname
* 'l': toggle to last/in first/out behaviour (jobs last submitted are prioritized first)

* 1: set storage quota for archive staging (see options in zsbatch to set add/remove values for archive staging). 
* 2: set storage quote for active storage (see options in zsbatch to set add/remove values for active)
* 3: set storage quota for dcache staging (see options in zsbatch to set add/remove values for dcache staging)


* 'm': set the number of jobs to search through to optimally fill a nodes memory. Default: 100. 


Config
------

Zslurm uses a default port. When used on the main user interface machine, it might clash with other users also using zslurm.
To modifiy the port, make a file ~/.zslurm with the following content:

```
port: 39123

```
Adapt the port number to something else (> 1024, <65536).



Output files
------------

* For each job, a file 'zslurm-<jobid>.out' will be written with the log output. 
* For each job, once it is finished, an entry will be written into the file 'reports.tsv', which stores all kinds of statistics (cpu/memory/io/thread usage)
* All logging is stored in 'cluster.log'


Snakemake profile
-----------------

See zslurm.yaml for an example profile which can be used for snakemake integration. 



zsbatch
-------

```
usage: zsbatch [-h] [-c CPUS_PER_TASK] [--mem MEM] [-t TIME] [-p PARTITION] [-q QOS] [--requeue] [-n NTASKS] [-d DEPENDENCY] [-J JOB_NAME] [--arch-use-add ARCH_USE_ADD] [--arch-use-remove ARCH_USE_REMOVE] [--dcache-use-add DCACHE_USE_ADD] [--dcache-use-remove DCACHE_USE_REMOVE]
               [--active-use-add ACTIVE_USE_ADD] [--active-use-remove ACTIVE_USE_REMOVE] [--parsable] [--limit-threads LIMIT_THREADS] [--info-input-mb INFO_INPUT_MB] [--info-output-file INFO_OUTPUT_FILE]
               [job_args ...]

Submit ZSlurm job

positional arguments:
  job_args

options:
  -h, --help            show this help message and exit
  -c CPUS_PER_TASK, --cpus-per-task CPUS_PER_TASK
                        Advise the Slurm controller that ensuing job steps will require ncpus number of processors per task. Without this option, the controller will just try to allocate one processor per task.
  --mem MEM             Specify the real memory required per node in MegaBytes. Default value is 1024.
  -t TIME, --time TIME  Advise on total run time of the job allocation. Used by scheduler to prevent scheduling on nodes that will run out of time before job completion. Acceptable time formats include "minutes", "minutes:seconds", "hours:minutes:seconds", "days-hours", "days-
                        hours:minutes" and "days-hours:minutes:seconds".
  -p PARTITION, --partition PARTITION
                        Either compute or archive
  -q QOS, --qos QOS     Included for SLURM compatibility, not used
  --requeue             Specifies that the batch job should eligible to being requeue. The job may be requeued after node failure. When a job is requeued, the batch script is initiated from its beginning.
  -n NTASKS, --ntasks NTASKS
                        Number of tasks to run
  -d DEPENDENCY, --dependency DEPENDENCY
                        Defer the start of this job until the specified dependencies have been satisfied completed. Param is of the form <type:job_id[:job_id][,type:job_id[:job_id]]> or <type:job_id[:job_id][?type:job_id[:job_id]]>, with type equal to after, afterany, afternotok,
                        afterok, expand or singleton. All dependencies must be satifisied if the "," separtor used, any dependencies if the "?" separator is used.
  -J JOB_NAME, --job-name JOB_NAME
                        Specify a name for the job allocation. The specified name will appear along with the job id number when querying running jobs on the system. The default is the name of the batch script, or just "zsbatch" if the script is read on zsbatchs standard input.
  --arch-use-add ARCH_USE_ADD
                        Archive usage added by this job.
  --arch-use-remove ARCH_USE_REMOVE
                        Archive usage removed by this job.
  --dcache-use-add DCACHE_USE_ADD
                        DCache usage added by this job.
  --dcache-use-remove DCACHE_USE_REMOVE
                        DCache usage removed by this job.
  --active-use-add ACTIVE_USE_ADD
                        Active usage added by this job.
  --active-use-remove ACTIVE_USE_REMOVE
                        Active usage removed by this job.
  --parsable            Outputs only the jobid and cluster name (if present), separated by semicolon, only on successful submission.
  --limit-threads LIMIT_THREADS
                        Changes environment to limit thread creation by OMP/OPENBLAS/MKL/VECLIB/NUMEXPR to a max value (default:-1 (disabled))
  --info-input-mb INFO_INPUT_MB
                        Used to set file size of the input files in the reports.tsv file
  --info-output-file INFO_OUTPUT_FILE
                        Used to set path of primary output file (helpful for identifiying specific jobs in reports.tsv)
```



zsqueue
-------

```
usage: zsqueue [-h] [--all] [--done] [--pretty]

ZSlurm Job queue listing

options:
  -h, --help  show this help message and exit
  --all       Show also completed jobs.
  --done      Show only completed jobs.
  --pretty    Pretty print the table.
```


zsnodes
-------

```
usage: zsnodes [-h] [--all] [--pretty]

ZSlurm node listing

options:
  -h, --help  show this help message and exit
  --all       Show also queued nodes.
  --pretty    Pretty print the table
```


zscancel
--------

```
usage: zscancel [-h] [--requeue] [job_id ...]

Cancel ZSlurm job

positional arguments:
  job_id

options:
  -h, --help  show this help message and exit
  --requeue   Requeue after cancel. Will skip non-running jobs.
```

Example: zsqueue  | cut -f1 | xargs zscancel


Node Usage Viewer
-----------------

Generates an interactive HTML report (Chart.js) of cluster fill rates over time from node usage logs written by `zslurm`.

- **Script**: `node_usage_viewer.py`
- **Inputs**: `node_usage-*.tsv` files produced by `zslurm`
- **Output**: An HTML file you can open locally in your browser (no server needed)

Usage
-----

```
python3 node_usage_viewer.py \
  -i 'node_usage-*.tsv' \
  -o node_usage_report.html \
  --bin-sec 60
```

- **`-i/--input`**: Glob(s) for TSV inputs. Repeatable.
- **`-o/--output`**: HTML output path (default: `node_usage_report.html`).
- **`--bin-sec`**: Time bin size in seconds (default: 60).
- **`--include-partition`**: Only include specific partitions. Repeat per partition.
- **`--include-unmanaged`**: Include unmanaged engines.
- **`--include-stopping`**: Include engines with `stopping==1`.
- **`--include-phasing-out`**: Include engines with status `PHASING_OUT`.

Examples
--------

- **All logs in current directory**:
```
python3 node_usage_viewer.py -i 'node_usage-*.tsv' -o viewer.html
```

- **Specific days / multiple globs**:
```
python3 node_usage_viewer.py \
  -i 'node_usage-2025-09-16_*.tsv' \
  -i 'node_usage-2025-09-18_*.tsv' \
  -o viewer_sep16_18.html
```

- **Only the `compute` partition**:
```
python3 node_usage_viewer.py -i 'node_usage-*.tsv' --include-partition compute
```

Where logs are written
----------------------

- **ZSlurm node usage logs**: Written by `zslurm` to files named `node_usage-YYYY-MM-DD_HH-MM.tsv` in the working directory. Names are controlled by:
  - `node_reports_enable` (default: `True`)
  - `node_reports_file_prefix` (default: `node_usage`)
  - `node_reports_include_partitions` (default: `["compute"]`)

  See `zslurm_shared.py` for defaults. The header includes fields such as `ts_iso`, `partition`, `cores`, `totmem_mb`, `res_cores_reserved`, `res_mem_reserved_mb`, etc.

- **ZSlurm logs**: General logs are written to `cluster.log`.

Notes
-----

- If no input files match, the viewer exits with an error and prints a traceback to stderr.
- The HTML viewer shows lines for `ALL` and each partition present in the inputs.
- Fill rate is computed as `reserved / total * 100` per bin for cores and memory.
