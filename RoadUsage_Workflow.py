"""Shared configuration and command-line helpers for the Road Usage steps."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(__file__).with_name("RoadUsage_Config.json")
_DLL_DIRECTORY_HANDLES: list[Any] = []


def prepare_environment() -> None:
    """Make the active conda/ArcGIS DLL folders available before GIS imports."""
    if os.name != "nt":
        return

    candidates = (
        Path(sys.prefix),
        Path(sys.prefix) / "Library" / "bin",
        Path(sys.prefix) / "ArcGISPro" / "bin",
    )
    existing = [str(path) for path in candidates if path.is_dir()]
    if existing:
        current = os.environ.get("PATH", "").split(os.pathsep)
        os.environ["PATH"] = os.pathsep.join(existing + [p for p in current if p and p not in existing])
        if hasattr(os, "add_dll_directory"):
            for path in existing:
                try:
                    _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(path))
                except OSError:
                    pass

    # A stale user-level PROJ_LIB can override the PROJ database bundled with
    # the active ArcGIS clone. Point both old and current PROJ variables at the
    # active environment's database before GeoPandas/pyproj is imported.
    proj_candidates = (
        Path(sys.prefix) / "Library" / "share" / "proj",
        Path(sys.prefix) / "Lib" / "site-packages" / "pyproj" / "proj_dir" / "share" / "proj",
    )
    proj_dir = next((path for path in proj_candidates if (path / "proj.db").is_file()), None)
    if proj_dir is not None:
        os.environ["PROJ_LIB"] = str(proj_dir)
        os.environ["PROJ_DATA"] = str(proj_dir)


def _resolve_config_path(value: Any, base: Path) -> Any:
    if value is None or not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        return value
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path.resolve())


def load_config(path: Path | str = DEFAULT_CONFIG) -> dict[str, Any]:
    """Load JSON settings and resolve configured data/output paths from its folder."""
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8-sig") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise ValueError(f"Configuration must be a JSON object: {config_path}")

    if not isinstance(config.get("disposition_filter"), str) or not config["disposition_filter"].strip():
        raise ValueError("Configuration requires a nonempty disposition_filter SQL expression")
    minimum = config.get("min_disposition_overlap_m", 0.0)
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not 0 <= minimum < float("inf"):
        raise ValueError("min_disposition_overlap_m must be a nonnegative finite number")
    base = config_path.parent
    if "output_root" in config:
        config["output_root"] = _resolve_config_path(config["output_root"], base)
    step1 = config.get("step1", {})
    for key in ("road_network", "roads", "dispositions"):
        if key in step1:
            step1[key] = _resolve_config_path(step1[key], base)
    for origin in config.get("origins", []):
        if "path" in origin:
            origin["path"] = _resolve_config_path(origin["path"], base)
    return config


def config_hash(config: dict[str, Any]) -> str:
    """Return a stable SHA-256 fingerprint of the effective configuration."""
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def disposition_filter_layer(arcpy: Any, source: str | Path, where: str, name: str):
    """Select the configured disposition subset without changing its source."""
    if not isinstance(where, str) or not where.strip():
        raise ValueError("disposition_filter must be a nonempty SQL expression")
    available = {field.name.upper() for field in arcpy.ListFields(str(source))}
    missing = {"DISP_TYPE", "PURPCD"} - available
    if missing:
        raise ValueError(f"Disposition source is missing filter fields: {sorted(missing)}")
    source_count = int(arcpy.management.GetCount(str(source))[0])
    layer = arcpy.management.MakeFeatureLayer(str(source), name, where_clause=where).getOutput(0)
    selected_count = int(arcpy.management.GetCount(layer)[0])
    return layer, source_count, selected_count


def write_json(path: Path | str, value: Any) -> None:
    """Write readable UTF-8 JSON, supporting Path and common NumPy scalar values."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    def convert(item: Any) -> Any:
        if isinstance(item, Path):
            return str(item)
        if hasattr(item, "item"):
            return item.item()
        raise TypeError(f"Object of type {type(item).__name__} is not JSON serializable")

    temporary = target.with_name(target.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, default=convert)
        stream.write("\n")
    temporary.replace(target)


def arguments(description: str, reviewed: bool = False) -> argparse.Namespace:
    """Parse the shared Step 1/2 command-line options."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-dir", type=Path, required=True)
    if reviewed:
        parser.add_argument(
            "--reviewed-cache",
            type=Path,
            help="Optional reviewed routing cache used for a reachability consistency check.",
        )
    return parser.parse_args()
