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
