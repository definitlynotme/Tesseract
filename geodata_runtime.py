from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class CoreStartupTimeoutPlan:
    seconds: float
    base_seconds: float
    extended: bool


DEFAULT_ASSET_FILENAMES = {
    "geoip": "geoip.dat",
    "geosite": "geosite.dat",
}


def runtime_config_text_uses_geodata(text: str) -> bool:
    upper = str(text or "").upper()
    return "GEOSITE," in upper or "GEOIP," in upper


def runtime_config_uses_geodata(runtime_config_path: Path) -> bool:
    try:
        text = Path(runtime_config_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return runtime_config_text_uses_geodata(text)


def missing_geodata_assets(
    work_dir: Path,
    *,
    asset_filenames: Mapping[str, str] | None = None,
) -> list[str]:
    work_dir = Path(work_dir)
    asset_filenames = DEFAULT_ASSET_FILENAMES if asset_filenames is None else asset_filenames
    required = {
        "geoip": {"GeoIP.dat", asset_filenames.get("geoip", "geoip.dat")},
        "geosite": {"GeoSite.dat", asset_filenames.get("geosite", "geosite.dat")},
    }
    missing: list[str] = []
    for key, filenames in required.items():
        if not any((work_dir / filename).exists() for filename in filenames):
            missing.append(key)
    return missing


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def core_startup_timeout_plan(
    proxy_config: dict[str, Any],
    *,
    uses_geodata: bool,
    missing_assets: list[str],
    geo_config: dict[str, Any],
) -> CoreStartupTimeoutPlan:
    proxy_config = proxy_config if isinstance(proxy_config, dict) else {}
    geo_config = geo_config if isinstance(geo_config, dict) else {}
    base_timeout = _coerce_float(proxy_config.get("startup_timeout", 30), 30.0)
    if not uses_geodata or not missing_assets:
        return CoreStartupTimeoutPlan(
            seconds=max(0.1, base_timeout),
            base_seconds=base_timeout,
            extended=False,
        )

    request_timeout = _coerce_float(geo_config.get("request_timeout", 30), 30.0)
    extended_timeout = min(180.0, base_timeout + max(30.0, request_timeout * len(missing_assets)))
    return CoreStartupTimeoutPlan(
        seconds=max(0.1, extended_timeout),
        base_seconds=base_timeout,
        extended=extended_timeout > base_timeout,
    )
