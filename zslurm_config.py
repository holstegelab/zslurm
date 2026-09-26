"""Safe, additive installation of a site's ZSlurm configuration template."""

import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

import yaml

import zslurm_shared


SUPPORTED_SITES = ("spider",)


def _site_template(site, template_dir=None):
    if site not in SUPPORTED_SITES:
        raise ValueError(f"unsupported site {site!r}; choose from {SUPPORTED_SITES}")
    candidates = (
        [Path(template_dir) / f"{site}.yaml"]
        if template_dir is not None else [
            Path(__file__).resolve().parent / "config" / "sites" / f"{site}.yaml",
            Path(sys.prefix) / "share" / "zslurm" / "sites" / f"{site}.yaml",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"{site} configuration template was not installed; checked: "
        + ", ".join(str(path) for path in candidates)
    )


def _yaml_mapping(text, path):
    try:
        loaded = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as error:
        raise ValueError(f"{path} contains invalid YAML") from error
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    if any(not isinstance(key, str) for key in loaded):
        raise ValueError(f"{path} must have string configuration keys")
    return loaded


def update_site_config(site, *, home=None, template_dir=None, dry_run=False):
    """Append absent site keys; preserve existing values, comments, and layout.

    Returns only key names, not values, because existing YAML may contain
    credentials. A running manager does not reload this file.
    """
    template_path = _site_template(site, template_dir)
    template = _yaml_mapping(template_path.read_text(encoding="utf-8"), template_path)
    config_path = Path(zslurm_shared.default_storage_layout(home)["config_file"])
    if config_path.is_symlink():
        raise ValueError(f"refusing to replace symlinked config {config_path}")

    existed = config_path.exists()
    if existed and not config_path.is_file():
        raise ValueError(f"{config_path} is not a regular file")
    old_stat = config_path.stat() if existed else None
    old_text = config_path.read_text(encoding="utf-8") if existed else ""
    existing = _yaml_mapping(old_text, config_path)
    current_site = existing.get("cluster_site")
    if current_site not in (None, site):
        raise ValueError(
            f"{config_path} declares cluster_site={current_site!r}; "
            f"refusing to merge {site!r} settings"
        )

    added = {key: value for key, value in template.items() if key not in existing}
    conflicts = [
        key for key, value in template.items()
        if key in existing and existing[key] != value
    ]
    result = {
        "path": str(config_path),
        "added": list(added),
        "conflicts": conflicts,
        "changed": bool(added) and not dry_run,
        "backup": None,
    }
    if not added or dry_run:
        return result

    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if existed:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        backup_path = config_path.with_name(
            f"{config_path.name}.bak.{stamp}.{uuid.uuid4().hex[:8]}")
        backup_fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(backup_fd, "w", encoding="utf-8") as backup:
            backup.write(old_text)
            backup.flush()
            os.fsync(backup.fileno())
        result["backup"] = str(backup_path)

    if existed:
        prefix = old_text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        prefix += f"\n# Added by zslurm --update-config {site}\n"
        new_text = prefix + yaml.safe_dump(added, sort_keys=False)
    else:
        new_text = template_path.read_text(encoding="utf-8")

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".zslurm-config-",
            suffix=".tmp", dir=config_path.parent, delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(new_text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        if config_path.is_symlink():
            raise RuntimeError(f"{config_path} became a symlink during update")
        if existed:
            current_stat = config_path.stat()
            if (
                current_stat.st_ino != old_stat.st_ino
                or current_stat.st_size != old_stat.st_size
                or current_stat.st_mtime_ns != old_stat.st_mtime_ns
            ):
                raise RuntimeError(f"{config_path} changed during update")
        elif config_path.exists():
            raise RuntimeError(f"{config_path} appeared during update")
        os.replace(temporary, config_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return result
