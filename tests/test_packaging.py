from pathlib import Path
import ast

import yaml

from zslurm_version import VERSION


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


def test_package_version_matches_runtime_version_without_importing_it_at_build():
    setup_tree = ast.parse((PROJECT / "setup.py").read_text(encoding="utf-8"))
    setup_call = next(
        node.value for node in setup_tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "setup"
    )
    package_version = next(
        keyword.value.value for keyword in setup_call.keywords
        if keyword.arg == "version"
    )
    assert package_version == VERSION
