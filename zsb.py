#!/usr/bin/env python
import os
import socket
import sys
import shlex
import time

class Args(object):
    pass
import sys
if sys.version_info.major == 2:
    import xmlrpclib
    import httplib
else:
    import xmlrpc.client as xmlrpclib
    import http.client as httplib

import socket
import time

class TimeoutHTTPConnection(httplib.HTTPConnection):
    def __init__(self,host,timeout=70):
        httplib.HTTPConnection.__init__(self, host, timeout = timeout)

class TimeoutTransport(xmlrpclib.Transport):
    def __init__(self, timeout = 70, *l, **kw):
        xmlrpclib.Transport.__init__(self, *l, **kw)
        self.timeout = timeout

    def make_connection(self, host):
        conn = TimeoutHTTPConnection(host, self.timeout)
        return conn

class TimeoutServerProxy(xmlrpclib.ServerProxy):
    def __init__(self, uri, timeout = 70, *l, **kw):
        kw['transport'] = TimeoutTransport(timeout = timeout, use_datetime = kw.get('use_datetime', 0))
        xmlrpclib.ServerProxy.__init__(self, uri, *l, **kw)

#self register
port = 38864
address = '127.0.0.1'
job_url = 'http://' + address + ':' + str(port + 1)


s = TimeoutServerProxy(job_url, allow_none = True)
def get_jobs():
    attempt = 4
    while attempt > 0 :
        try:
            jobs = s.list_jobs()
            return [int(jobid) for jobid, job_name, state, runtime, cpus, partition, node, arch_use, active_use, dcache_use in jobs]
        except (socket.error, httplib.HTTPException) as serror:
            time.sleep(15)
            attempt -= 1
    print(('Queue request failed, could not connect to XSlurm manager on ' + job_url))
    return []

def run(command, mem=1024, runtime='1:0:0', cpus=1, arch_use_add=0, arch_use_remove=0, active_use_add=0, active_use_remove=0, partition='compute'):
    runtime = str(runtime)

    args = Args()
    args.time = runtime
    args.cpus_per_task = cpus
    args.mem = mem
    args.requeue = 0
    args.dependency = None
    args.job_args = command
    args.job_name = None
    args.ntasks = 1
    args.arch_use_add = float(arch_use_add)
    args.arch_use_remove = float(arch_use_remove)
    args.active_use_add = float(active_use_add)
    args.active_use_remove = float(active_use_remove)
    args.partition = partition

    cwd = os.getcwd()
    env = dict([(str(a), str(b)) for a, b in list(os.environ.items())])


    #parse time format
    try:
        atime = args.time.split('-')

        if len(atime) == 2:
            days = int(atime[0])
            atime = atime[1]
        elif len(atime) == 1:
            days = 0
            atime = atime[0]
        else:
            raise RuntimeError
        
        atime = atime.split(':')
        if len(atime) == 3:
            hours = int(atime[0])
            atime = atime[1:]
        else:
            hours = 0

        minutes = int(atime[0])
        if len(atime) == 2:
            seconds = int(atime[1])
        elif len(atime) == 1:
            seconds = 0
        else:
            raise RuntimeError
        
        reqtime = days * 24 * 60 + hours * 60 + minutes + (seconds > 0)

    except: 
        print(('zsbatch: error: Incorrect time format: %s' % args.time))
        raise

    job_name = args.job_name


    job_args = args.job_args

    if not job_args:
        job_args = shlex.split(sys.stdin.readline())
        if job_name is None:
            job_name = 'zsbatch'
        
            

    if not job_args:
        print('zsbatch: error: No command given on input')
        return -1


    cmd = " ".join(job_args) # default

    if os.path.exists(job_args[0]): #executes file
        if not os.access(args.job_args[0], os.X_OK): #executes non-executable file: search for interpreter
            f = open(args.job_args[0],'r')
            firstline = f.readline()
            f.close()
            if not firstline.startswith('#!'):
                print('zsbatch: error: This does not look like a bash script. The first line must start with #! followed by the path to an interpreter. For instance #!/bin/sh')
                return -1
            interpreter = firstline[2:]
            cmd = interpreter + " "  + " ".join(job_args)

    if job_name is None:
        job_name = os.path.basename(job_args[0])

    attempt = 4
    while attempt > 0 :
        try:
            jobids = []
            for i in range(int(args.ntasks)):
                jobid = s.submit_job(job_name, cmd, cwd, env, args.cpus_per_task, args.mem, reqtime, args.requeue, args.dependency, args.arch_use_add, args.arch_use_remove, args.active_use_add, args.active_use_remove, args.partition)
                jobids.append(jobid)
            if int(args.ntasks) == 1:
                return jobids[0]
            else:
                return jobids
        except (socket.error, httplib.HTTPException) as serror:
            attempt -= 1
            time.sleep(15)

    print(('zsbatch: error: Job submission failed, could not connect to XSlurm manager on ' + job_url))
