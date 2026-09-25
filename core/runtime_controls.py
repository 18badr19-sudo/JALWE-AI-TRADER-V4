from __future__ import annotations

import json
import os

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(
    os.getenv(
        "JALWE_CONTROLLER_DATA_DIR",
        str(BASE_DIR / "data"),
    )
)
DATA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

CONTROL_FILE = Path(
    os.getenv(
        "JALWE_RUNTIME_CONTROL_FILE",
        str(
            DATA_DIR
            / "jalwe_runtime_controls.json"
        ),
    )
)


def _default_controls() -> dict[str, Any]:
    return {
        "allow_new_entries": True,
        "updated_at": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
        "updated_by": "DEFAULT",
    }


def load_runtime_controls() -> dict[str, Any]:
    controls = _default_controls()

    if not CONTROL_FILE.exists():
        return controls

    try:
        raw = json.loads(
            CONTROL_FILE.read_text(
                encoding="utf-8",
                errors="ignore",
            )
        )

        if isinstance(raw, dict):
            controls.update(raw)

    except Exception:
        pass

    controls["allow_new_entries"] = bool(
        controls.get(
            "allow_new_entries",
            True,
        )
    )

    return controls


def save_runtime_controls(
    controls: dict[str, Any],
) -> None:
    payload = dict(
        controls or {}
    )

    payload["updated_at"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    CONTROL_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = CONTROL_FILE.with_suffix(
        ".tmp"
    )

    tmp.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    tmp.replace(
        CONTROL_FILE
    )


def new_entries_allowed() -> bool:
    return bool(
        load_runtime_controls().get(
            "allow_new_entries",
            True,
        )
    )


def set_new_entries_allowed(
    allowed: bool,
    *,
    updated_by: str,
    reason: str = "",
) -> dict[str, Any]:
    controls = load_runtime_controls()

    controls[
        "allow_new_entries"
    ] = bool(
        allowed
    )

    controls[
        "updated_by"
    ] = str(
        updated_by
        or "UNKNOWN"
    )

    controls[
        "reason"
    ] = str(
        reason
        or ""
    )

    save_runtime_controls(
        controls
    )

    return controls
