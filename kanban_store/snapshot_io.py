"""Atomic JSON persistence helpers for snapshot payloads."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(destination: str | Path, payload: Any) -> Path:
    """Write *payload* as JSON, replacing *destination* atomically.

    Serialization happens before a temporary file is created.  The temporary
    file is kept beside the destination so that ``os.replace`` is atomic on
    the filesystems involved.
    """
    destination = Path(destination)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)

    file_descriptor: int | None = None
    temporary_path: str | None = None
    try:
        file_descriptor, temporary_path = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary_file = os.fdopen(file_descriptor, "w", encoding="utf-8")
        file_descriptor = None
        with temporary_file:
            temporary_file.write(serialized)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass

    return destination


__all__ = ["write_json_atomic"]
