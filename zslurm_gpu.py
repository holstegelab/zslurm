"""GPU devices, child visibility and worker packing.

Ported from the CCS/DeepConsensus GPU implementation. Device identifiers are
worker-local CUDA identifiers (including GPU/MIG UUIDs), never submission-host
device numbers. Reservations include unacknowledged ASSIGNED grants.
"""


def device_ids(value):
    if value is None:
        return []
    values = value.split(',') if isinstance(value, str) else value
    devices = [str(v).strip() for v in values if str(v).strip()]
    if any(d.lower() in ('-1', 'none', 'nodevfiles', 'void', 'all') for d in devices):
        if len(devices) == 1 and devices[0].lower() != 'all':
            return []
        raise ValueError('GPU devices must be explicit CUDA identifiers')
    if len(devices) != len(set(devices)):
        raise ValueError('Duplicate GPU device identifiers')
    return devices


def gpu_count(partition, value=None):
    if partition not in ('compute', 'archive', 'gpu'):
        raise ValueError('Unknown ZSlurm job partition')
    if value is None:
        value = 1 if partition == 'gpu' else 0
    if isinstance(value, bool) or int(value) != value or value < 0:
        raise ValueError('GPU count must be a nonnegative integer')
    if (partition == 'gpu') != (value > 0):
        raise ValueError('Positive GPU requests require the gpu partition')
    return int(value)


def available_devices(engine):
    if engine is None:
        return []
    reserved = {d for devices in getattr(engine, 'gpu_allocations', {}).values() for d in devices}
    return [d for d in getattr(engine, 'gpu_devices', []) if d not in reserved]


def reserve_devices(engine, job):
    if not job.ngpus:
        job.gpu_devices = []
        return
    if job.jobid in engine.gpu_allocations:
        raise ValueError('GPUs already reserved for job')
    devices = available_devices(engine)
    if len(devices) < job.ngpus:
        raise ValueError('Insufficient free GPUs')
    job.gpu_devices = devices[:job.ngpus]
    engine.gpu_allocations[job.jobid] = list(job.gpu_devices)


def release_devices(engine, job):
    if getattr(job, 'gpu_devices', None):
        job.last_gpu_devices = list(job.gpu_devices)
    if engine is not None:
        getattr(engine, 'gpu_allocations', {}).pop(job.jobid, None)
    job.gpu_devices = []


def execution_environment(job):
    env = dict(job.env)
    env['ZSLURM_GPU_DEVICES'] = ','.join(job.gpu_devices)
    env['ZSLURM_GPUS'] = str(job.ngpus)
    env['ZSLURM_JOB_ID'] = str(job.jobid)
    return env


def worker_devices(environment):
    for key in ('CUDA_VISIBLE_DEVICES', 'SLURM_STEP_GPUS', 'SLURM_JOB_GPUS'):
        if key in environment:
            return device_ids(environment[key])
    return []


def child_environment(submitted, worker):
    env = {k: v for k, v in submitted.items()
           if not k.startswith('SLURM_') and k not in
           ('CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES',
            'APPTAINERENV_CUDA_VISIBLE_DEVICES', 'SINGULARITYENV_CUDA_VISIBLE_DEVICES',
            'APPTAINERENV_NVIDIA_VISIBLE_DEVICES', 'SINGULARITYENV_NVIDIA_VISIBLE_DEVICES')}
    env.update({k: v for k, v in worker.items() if k.startswith('SLURM_')})
    assigned = device_ids(submitted.get('ZSLURM_GPU_DEVICES', ''))
    count = int(submitted.get('ZSLURM_GPUS', '0'))
    allocated = worker_devices(worker) if assigned else []
    if count < 0 or count != len(assigned) or not set(assigned).issubset(allocated):
        raise ValueError('Job GPU assignment exceeds the worker allocation')
    env['CUDA_VISIBLE_DEVICES'] = ','.join(assigned)
    # Visibility is cooperative, not a separate per-job device cgroup. Never
    # let container overrides from the submission host undo the assignment.
    for prefix in ('', 'APPTAINERENV_', 'SINGULARITYENV_'):
        env[prefix + 'CUDA_VISIBLE_DEVICES'] = env['CUDA_VISIBLE_DEVICES']
        env[prefix + 'NVIDIA_VISIBLE_DEVICES'] = ','.join(assigned) if assigned else 'void'
    return env


def worker_memory_mb(available_mb, requested_mb, config):
    limit = available_mb if requested_mb is None else min(available_mb, requested_mb)
    usable = max(0.0, limit - float(config.get('mem_static_reserve_mb', 100.0)))
    return min(float(config.get('mem_cap_fraction_of_total', 0.99)) * available_mb,
               (1.0 - float(config.get('mem_headroom_fraction', 0.08))) * usable)


def fits(request, worker):
    """CPU/RAM/devices, runtime and scratch must fit on the SAME worker."""
    return (all(need <= free for need, free in zip(request['resources'], worker['resources']))
            and request.get('runtime', 0) <= worker.get('runtime', float('inf'))
            and (not request.get('ssd_required', False) or worker.get('has_ssd', False))
            and (not worker.get('has_ssd', False)
                 or request.get('ssd_gb', 0) <= worker.get('ssd_gb', float('inf'))))


def pack_workers(requests, workers, profile=None):
    """Credit existing/queued workers individually; optionally add fresh ones.

    Busy workers stay in place. Runtime/SSD metadata remains attached to its
    worker, so a short lease or spare GPU on another node cannot supply a slot.
    Returns (number added, keys used, number unserviceable).
    """
    bins = [dict(w, resources=list(w['resources'])) for w in workers]
    added, used, unfit = 0, set(), 0
    ordered = sorted(requests, key=lambda r: (
        r.get('priority', 100), r['resources'][2], r['resources'][1], r['resources'][0]
    ), reverse=True)
    for request in ordered:
        target = next((w for w in bins if fits(request, w)), None)
        if target is None:
            if profile is None or not fits(request, profile):
                unfit += 1
                continue
            target = dict(profile, resources=list(profile['resources']), key=('new', added))
            bins.append(target)
            added += 1
        used.add(target['key'])
        for i, need in enumerate(request['resources']):
            target['resources'][i] -= need
        if target.get('has_ssd', False):
            target['ssd_gb'] = max(0.0, target.get('ssd_gb', float('inf')) - request.get('ssd_gb', 0))
    return added, used, unfit
