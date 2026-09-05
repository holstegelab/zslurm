from pathlib import Path

import yaml


PROJECT = Path(__file__).resolve().parents[1]


def test_conda_environment_contains_legacy_build_dependency():
    environment = yaml.safe_load((PROJECT / "env.yaml").read_text(encoding="utf-8"))

    dependencies = [str(value) for value in environment["dependencies"]]
    assert any(value.startswith("setuptools>=") for value in dependencies)


def test_pep517_build_declares_setuptools_backend():
    import tomllib

    build = tomllib.loads(
        (PROJECT / "pyproject.toml").read_text(encoding="utf-8")
    )["build-system"]
    assert build["build-backend"] == "setuptools.build_meta"
    assert any(value.startswith("setuptools>=") for value in build["requires"])
