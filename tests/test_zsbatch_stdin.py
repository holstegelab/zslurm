import io
import runpy
import shlex
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run(monkeypatch, argv, stdin):
    submitted = []

    class Proxy:
        def submit_job(self, *arguments):
            submitted.append(arguments)
            return "42"

    shared = types.ModuleType("zslurm_shared")
    shared.DEFAULT_INSTANCE_NAME = "default"
    shared.get_instance_names = lambda: ["test"]
    shared.get_config = lambda: {}
    shared.get_job_url = lambda instance: "http://test.invalid/jobs"
    shared.TimeoutServerProxy = lambda *args, **kwargs: Proxy()
    monkeypatch.setitem(sys.modules, "zslurm_shared", shared)
    monkeypatch.setattr(sys, "argv", ["zsbatch", "--parsable", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(ROOT / "zsbatch"), run_name="__main__")
    assert stopped.value.code == 0
    assert len(submitted) == 1
    return submitted[0]


def test_stdin_batch_script_is_transported_in_full(monkeypatch):
    script = "#!/bin/bash\nset -euo pipefail\nprintf '%s\\n' \"$VALUE with spaces\"\n"
    submitted = _run(monkeypatch, [], script)
    command = submitted[1]
    assert shlex.split(command) == ["/bin/bash", "-c", script]
    assert submitted[0] == "zsbatch"


def test_positional_command_arguments_preserve_spaces(monkeypatch):
    submitted = _run(
        monkeypatch,
        ["/bin/printf", "%s\\n", "argument with spaces"],
        "",
    )
    assert shlex.split(submitted[1]) == [
        "/bin/printf",
        "%s\\n",
        "argument with spaces",
    ]


@pytest.mark.parametrize('duration,seconds', [
    ('1',60),('90',5400),('1:30',90),('75:05',4505),
    ('0:1:0',60),('1:30:00',5400),('48:00:00',172800),
    ('2-03',183600),('2-03:04',183840),('2-03:04:05',183845),
])
def test_slurm_time_formats_are_submitted_as_seconds_on_every_site(monkeypatch,duration,seconds):
    submitted=_run(monkeypatch,['--time',duration,'/bin/true'],'')
    assert submitted[6]==seconds


def test_default_runtime_is_one_minute_not_one_second(monkeypatch):
    assert _run(monkeypatch,['/bin/true'],'')[6]==60


@pytest.mark.parametrize('duration',['0','-1','1:60','1:60:00','1:2:3:4','2-3:4:5:6','not-time'])
def test_invalid_duration_fails_before_rpc(monkeypatch,duration):
    shared=types.ModuleType('zslurm_shared')
    shared.get_job_url=lambda **_: 'http://test.invalid/jobs'
    shared.TimeoutServerProxy=lambda *_,**__:types.SimpleNamespace(
        submit_job=lambda *_:pytest.fail('invalid duration submitted'))
    monkeypatch.setitem(sys.modules,'zslurm_shared',shared)
    monkeypatch.setattr(sys,'argv',['zsbatch','--instance','test','--time='+duration,'/bin/true'])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(ROOT/'zsbatch'),run_name='__main__')
    assert stopped.value.code==2
