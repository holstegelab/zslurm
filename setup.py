import sys

from setuptools import setup,find_packages,Extension
import distutils.sysconfig
import os
import os.path

setup(
    name="ZSlurm",
    version="0.2.1",
    scripts = ['zsqueue', 'zsbatch', 'zscancel','zslurm','zsnodes', 'zslurm_chief','zslurm_lease','slurm_to_zslurm','zsqueue_stats','zsoccupancy','zsstats','node_usage_viewer.py','zsstatus','zscontrol'],
    install_requires=['numpy>=1.4.1', 'psutil', 'tabulate', 'PyYAML'],
    extras_require={'ipyparallel': ['ipyparallel']},
     py_modules=['zslurm_shared','zslurm_lease','zslurm_startup','zslurm_version','zslurm_config','zsb'],
     data_files=[('share/zslurm/sites', ['config/sites/spider.yaml'])],
     author = "M. Hulsman",
     author_email = "m.hulsman1@amsterdamumc.nl",
     description = "ZSlurm is a batch system on top of SLURM, which allows for core-level scheduling on systems that only allow (partial) node-level scheduling.",
     license = "LGPLv2.1",

)
