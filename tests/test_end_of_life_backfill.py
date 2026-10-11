"""Early walltime draining must not strand safely fitting scientific work."""
import time

import pytest

from tests.test_large_placement_window import manager, node


def ready(z, *, site='spider', left=10*3600, seconds=4*3600, eol=True):
    z._apply_cluster_config({'cluster_site':site})
    engine=node(z,30,220708)
    resident=z.Job('resident','resident','true','/tmp',{},2,16000,21600,0,None,
                   0,0,0,0,0,0,'compute',0,None,'')
    z.jobs.jobs_by_id['resident']=resident
    resident.assigned(engine.engine_id)
    assert z.jobs.can_run_assigned_job(engine.engine_id,'resident')
    z.engines.job_running(engine.engine_id,resident)
    resident.starttime=time.time()-7200
    engine.status=z.PHASING_OUT;engine.end_of_life_drain=eol;engine.timeleft=left
    pending=z.Job('alignment','alignment','true','/tmp',{},8,64000,seconds,0,None,
                  0,0,0,0,0,0,'compute',0,None,'')
    pending.submit_ts=time.time()-600
    z.jobs.jobs_by_id['alignment']=pending
    return engine,pending


@pytest.mark.parametrize('site',['spider','snellius'])
def test_four_hour_alignment_can_use_busy_pilots_final_ten_hours(manager,site):
    z=manager;e,j=ready(z,site=site)
    assert z.phaseout_backfill_window(e)==10*3600-1800
    grant=z.jobs.request_jobs(e.engine_id,28,204708,'compute')
    assert [r[0] for r in grant]==['alignment']
    assert j.state=='ASSIGNED' and e.end_of_life_drain and e.status==z.PHASING_OUT
    assert z.jobs.request_jobs(e.engine_id,28,204708,'compute')==grant


@pytest.mark.parametrize('left,accepted',[(4*3600+2700-1,False),(4*3600+2700,True),(1800,False),(0,False)])
def test_assignment_requires_both_drain_margin_and_job_walltime_margin(manager,left,accepted):
    z=manager;e,j=ready(z,left=left)
    grant=z.jobs.request_jobs(e.engine_id,28,204708,'compute')
    assert bool(grant)==accepted
    assert j.state==('ASSIGNED' if accepted else 'PENDING')


@pytest.mark.parametrize('change',['disabled','empty','stopping','young','cores','memory','long_job','unmanaged','unknown_walltime'])
def test_backfill_keeps_existing_placement_and_shutdown_guards(manager,change):
    z=manager;e,j=ready(z);cores,mem=28,204708
    if change=='disabled':z.status.config['phaseout_backfill_enable']=False
    elif change=='empty':e.jobs=[]
    elif change=='stopping':e.stopping=True
    elif change=='young':j.submit_ts=time.time()
    elif change=='cores':cores=7
    elif change=='memory':mem=63000
    elif change=='long_job':j.reqtime=24*3600
    elif change=='unmanaged':e.cluster_id='123';e.managed=False;e.slurm_state='R'
    elif change=='unknown_walltime':e.timeleft=0
    assert z.jobs.request_jobs(e.engine_id,cores,mem,'compute')==[]
    assert j.state=='PENDING'


@pytest.mark.parametrize('seconds,accepted',[(2*3600,True),(4*3600,False)])
def test_non_walltime_consolidation_still_respects_original_drain_estimate(manager,seconds,accepted):
    z=manager;e,j=ready(z,seconds=seconds,eol=False)
    assert bool(z.jobs.request_jobs(e.engine_id,28,204708,'compute'))==accepted


@pytest.mark.parametrize('key,value',[('phaseout_backfill_margin_sec',-1),('phaseout_backfill_margin_sec',float('nan')),
    ('phaseout_backfill_min_window_sec',0),('phaseout_backfill_min_window_sec',float('inf'))])
def test_invalid_safety_window_cannot_authorize_work(manager,key,value):
    z=manager;e,_=ready(z);z.status.config[key]=value
    assert z.phaseout_backfill_window(e)==0
