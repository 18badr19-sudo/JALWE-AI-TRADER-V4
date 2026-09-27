from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_FILENAME = "jalwe_persistence_probe.json"

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _load_marker(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}

def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)

def record_persistence_boot(directory: Path) -> dict[str, Any]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    marker_path = directory / PROBE_FILENAME
    previous = _load_marker(marker_path)
    now = _utc_now_iso()

    probe_id = str(previous.get("probe_id", "") or "").strip()
    if not probe_id:
        probe_id = uuid.uuid4().hex

    try:
        previous_boot_count = int(previous.get("boot_count", 0) or 0)
    except (TypeError, ValueError):
        previous_boot_count = 0

    first_seen = str(previous.get("first_seen_utc", "") or "").strip() or now

    payload = {
        "version": 1,
        "probe_id": probe_id,
        "boot_count": previous_boot_count + 1,
        "first_seen_utc": first_seen,
        "last_boot_utc": now,
        "railway_service_id": os.getenv("RAILWAY_SERVICE_ID", "") or "",
        "railway_deployment_id": os.getenv("RAILWAY_DEPLOYMENT_ID", "") or "",
    }
    _atomic_write_json(marker_path, payload)
    return {**payload, "marker_path": str(marker_path), "directory": str(directory)}

def persistence_status(directory: Path) -> dict[str, Any]:
    directory = Path(directory)
    marker_path = directory / PROBE_FILENAME
    marker = _load_marker(marker_path)

    write_test_ok = False
    write_test_error = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        test_path = directory / ".jalwe_write_probe"
        with test_path.open("w", encoding="utf-8") as handle:
            handle.write("ok")
            handle.flush()
            os.fsync(handle.fileno())
        write_test_ok = test_path.read_text(encoding="utf-8") == "ok"
        test_path.unlink(missing_ok=True)
    except Exception as exc:
        write_test_error = str(exc)

    try:
        is_mount = bool(os.path.ismount(directory))
    except Exception:
        is_mount = False

    return {
        "directory": str(directory),
        "marker_path": str(marker_path),
        "marker_exists": bool(marker),
        "write_test_ok": write_test_ok,
        "write_test_error": write_test_error,
        "is_mount": is_mount,
        "probe_id": marker.get("probe_id"),
        "boot_count": marker.get("boot_count"),
        "first_seen_utc": marker.get("first_seen_utc"),
        "last_boot_utc": marker.get("last_boot_utc"),
        "railway_service_id": marker.get("railway_service_id"),
        "railway_deployment_id": marker.get("railway_deployment_id"),
    }
