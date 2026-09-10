from __future__ import annotations

import json
import os

import pytest

from kanban_store import snapshot_io


def _temporary_files(directory, destination):
    return list(directory.glob(f".{destination.name}.*.tmp"))


def test_write_json_atomic_writes_snapshot_json_and_leaves_no_temp_file(tmp_path):
    destination = tmp_path / "2026-05-09.json"
    payload = {"projects": [{"name": "Café"}], "tasks": [1, 2]}

    result = snapshot_io.write_json_atomic(destination, payload)

    assert result == destination
    assert json.loads(destination.read_text(encoding="utf-8")) == payload
    assert destination.read_text(encoding="utf-8") == json.dumps(
        payload, ensure_ascii=False, indent=2
    )
    assert _temporary_files(tmp_path, destination) == []


def test_serialization_failure_happens_before_filesystem_write(tmp_path):
    destination = tmp_path / "snapshot.json"
    destination.write_text('{"existing": true}', encoding="utf-8")

    with pytest.raises(TypeError):
        snapshot_io.write_json_atomic(destination, {"not_json": object()})

    assert destination.read_text(encoding="utf-8") == '{"existing": true}'
    assert _temporary_files(tmp_path, destination) == []


def test_temp_write_failure_preserves_destination_and_cleans_temp_file(
    monkeypatch, tmp_path
):
    destination = tmp_path / "snapshot.json"
    original = '{"existing": true}'
    destination.write_text(original, encoding="utf-8")
    real_fdopen = snapshot_io.os.fdopen

    class FailingWriter:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self._wrapped.__exit__(exc_type, exc_value, traceback)

        def write(self, value):
            self._wrapped.write(value)
            raise OSError("injected write failure")

        def flush(self):
            return self._wrapped.flush()

        def fileno(self):
            return self._wrapped.fileno()

    def failing_fdopen(file_descriptor, *args, **kwargs):
        return FailingWriter(real_fdopen(file_descriptor, *args, **kwargs))

    monkeypatch.setattr(snapshot_io.os, "fdopen", failing_fdopen)

    with pytest.raises(OSError, match="injected write failure"):
        snapshot_io.write_json_atomic(destination, {"replacement": True})

    assert destination.read_text(encoding="utf-8") == original
    assert _temporary_files(tmp_path, destination) == []


def test_replace_failure_preserves_destination_and_cleans_temp_file(
    monkeypatch, tmp_path
):
    destination = tmp_path / "snapshot.json"
    original = '{"existing": true}'
    destination.write_text(original, encoding="utf-8")

    def fail_replace(source, target):
        assert os.path.dirname(os.fspath(source)) == os.fspath(tmp_path)
        assert os.fspath(target) == os.fspath(destination)
        assert os.path.exists(source)
        raise OSError("injected replace failure")

    monkeypatch.setattr(snapshot_io.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        snapshot_io.write_json_atomic(destination, {"replacement": True})

    assert destination.read_text(encoding="utf-8") == original
    assert _temporary_files(tmp_path, destination) == []
