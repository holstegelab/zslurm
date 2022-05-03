import sys

from setuptools import setup,find_packages,Extension
import distutils.sysconfig
import os
import os.path

setup(
    name="XSlurm",
    version="0.1",
    scripts = ['zsqueue', 'zsbatch', 'zscancel','zslurm','zsnodes', 'zslurm_chief','slurm_to_zslurm'],
     install_requires=['numpy>=1.4.1','psutil','dnspython'],
     py_modules=['zslurm_shared','zsb'],
     author = "M. Hulsman",
     author_email = "m.hulsman@tudelft.nl",
     description = "ZSlurm is a batch system on top of SLURM, which allows for core-level scheduling on systems that only allow  node-level scheduling.",
     license = "LGPLv2.1",

)

