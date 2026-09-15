import importlib.machinery
import importlib.util
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def load_zslurm():
    loader = importlib.machinery.SourceFileLoader(
        "zslurm_engine_runtime_under_test", str(ROOT / "zslurm")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_manager_pins_installed_launcher_chief_and_python(tmp_path):
    runtime = tmp_path / "bin"
    runtime.mkdir()
    launcher = runtime / "slurm_to_zslurm"
    chief = runtime / "zslurm_chief"
    python = runtime / "python"
    for path in (launcher, chief, python):
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)

    env = {"PATH": "/usr/bin:/bin"}
    zslurm = load_zslurm()
    resolved = zslurm._prepare_engine_runtime(
        runtime_dir=runtime, python_executable=python, environ=env
    )

    assert resolved == str(launcher)
    assert env["ZSLURM_CHIEF_PATH"] == str(chief)
    assert env["ZSLURM_ENGINE_PYTHON"] == str(python)
    assert env["ZSLURM_ENGINE_ENV_BIN"] == str(runtime)


def test_explicit_engine_runtime_overrides_remain_authoritative(tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "ZSLURM_ENGINE_LAUNCHER": "/configured/launcher",
        "ZSLURM_CHIEF_PATH": "/configured/chief",
        "ZSLURM_ENGINE_PYTHON": "/configured/python",
        "ZSLURM_ENGINE_ENV_BIN": "/configured/bin",
    }
    zslurm = load_zslurm()
    assert zslurm._prepare_engine_runtime(
        runtime_dir=tmp_path,
        python_executable="/another/python",
        environ=env,
    ) == "/configured/launcher"
    assert env["ZSLURM_CHIEF_PATH"] == "/configured/chief"
    assert env["ZSLURM_ENGINE_PYTHON"] == "/configured/python"
    assert env["ZSLURM_ENGINE_ENV_BIN"] == "/configured/bin"


def test_launcher_uses_pinned_python_for_copied_chief(tmp_path):
    chief = tmp_path / "chief.sh"
    chief.write_text("printf '%s\\n' \"$1\"\n")
    result = subprocess.run(
        [str(ROOT / "slurm_to_zslurm"), "from-pinned-runtime"],
        env={
            **os.environ,
            "ZSLURM_CHIEF_PATH": str(chief),
            "ZSLURM_ENGINE_PYTHON": "/bin/sh",
        },
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == "from-pinned-runtime\n"
