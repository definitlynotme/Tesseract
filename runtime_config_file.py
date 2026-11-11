from __future__ import annotations

from pathlib import Path


def read_existing_runtime_config_text(runtime_config_path: str | Path) -> str | None:
    path = Path(runtime_config_path)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def restore_runtime_config_text(runtime_config_path: str | Path, previous_config_text: str | None) -> bool:
    if previous_config_text is None:
        return False
    path = Path(runtime_config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(previous_config_text, encoding="utf-8")
    return True
