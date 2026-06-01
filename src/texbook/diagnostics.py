"""Thread-safe diagnostic logging for CLI and GUI sessions."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from texbook.llm.scheduler import ProgressEvent


RAW_PREVIEW_LIMIT = 800
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "api-key",
    "authorization",
    "password",
    "secret",
    "token",
)


class DiagnosticLog:
    """In-memory JSONL diagnostic log with optional live file mirroring."""

    def __init__(self, log_file: str | Path | None = None) -> None:
        self._lock = RLock()
        self._events: list[dict[str, Any]] = []
        self._log_file: Path | None = None
        if log_file is not None:
            self.start_file(log_file)
            self.record("log_file_started", path=str(self._log_file))

    @property
    def log_file(self) -> Path | None:
        """Return the live log file path when file mirroring is enabled."""
        return self._log_file

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """Return a snapshot of recorded events."""
        with self._lock:
            return tuple(dict(event) for event in self._events)

    def start_file(self, path: str | Path) -> Path:
        """Create or overwrite a live JSONL log file."""
        resolved = Path(path).expanduser()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text("", encoding="utf-8")
        with self._lock:
            self._log_file = resolved
        return resolved

    def record(
        self,
        kind: str,
        *,
        message: str = "",
        task_id: str = "",
        pdf: str | Path = "",
        operation: str = "",
        label: str = "",
        stage: str = "",
        chunk_index: int | None = None,
        total_chunks: int | None = None,
        attempt: int | None = None,
        max_attempts: int | None = None,
        delay: float | None = None,
        error: str = "",
        raw_preview: str = "",
        metadata: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Append one diagnostic event and mirror it to disk when configured."""
        event: dict[str, Any] = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "kind": str(kind),
        }
        optional: dict[str, Any] = {
            "message": message,
            "task_id": task_id,
            "pdf": str(pdf) if pdf else "",
            "operation": operation,
            "label": label,
            "stage": stage,
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "delay": delay,
            "error": error,
            "raw_preview": truncate_preview(raw_preview) if raw_preview else "",
        }
        for key, value in optional.items():
            if value not in ("", None):
                event[key] = value
        if metadata:
            sanitized_metadata = _sanitize_mapping(metadata)
            event["metadata"] = sanitized_metadata
            if "raw_preview" in sanitized_metadata and "raw_preview" not in event:
                event["raw_preview"] = truncate_preview(str(sanitized_metadata["raw_preview"]))
        for key, value in fields.items():
            if value not in ("", None):
                event[key] = _sanitize_value(key, value)

        with self._lock:
            self._events.append(event)
            if self._log_file is not None:
                _append_jsonl(self._log_file, event)
        return event

    def record_progress(
        self,
        event: "ProgressEvent",
        *,
        task_id: str = "",
        pdf: str | Path = "",
    ) -> dict[str, Any]:
        """Record one core progress event."""
        metadata = _sanitize_mapping(event.metadata)
        chunk_index = _metadata_int(metadata, "chunk_index")
        total_chunks = _metadata_int(metadata, "total_chunks")
        return self.record(
            event.kind,
            task_id=task_id,
            pdf=pdf,
            operation=event.operation,
            label=event.label,
            stage=event.operation,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            attempt=event.attempt,
            max_attempts=event.max_attempts,
            delay=event.delay,
            error=event.error,
            raw_preview=str(metadata.get("raw_preview", "")),
            metadata=metadata,
            finish_reason=str(metadata.get("finish_reason", "")),
        )

    def export(self, path: str | Path) -> Path:
        """Overwrite ``path`` with all events recorded so far."""
        resolved = Path(path).expanduser()
        self.record(
            "log_exported",
            path=str(resolved),
            event_count=len(self.events) + 1,
        )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            lines = [
                json.dumps(event, ensure_ascii=False, sort_keys=True)
                for event in self._events
            ]
        resolved.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return resolved


def truncate_preview(text: str, *, limit: int = RAW_PREVIEW_LIMIT) -> str:
    """Return a compact, single-line-safe raw response preview."""
    normalized = " ".join(str(text).split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def exception_metadata(exc: Exception) -> dict[str, object]:
    """Return safe diagnostic fields exposed by an exception."""
    metadata: dict[str, object] = {"exception_type": exc.__class__.__name__}
    raw_preview = getattr(exc, "raw_preview", "")
    if raw_preview:
        metadata["raw_preview"] = truncate_preview(str(raw_preview))
    finish_reason = getattr(exc, "finish_reason", "")
    if finish_reason:
        metadata["finish_reason"] = str(finish_reason)
    return metadata


def _append_jsonl(path: Path, event: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as file:
        json.dump(event, file, ensure_ascii=False, sort_keys=True)
        file.write("\n")


def _sanitize_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _sanitize_value(str(key), item)
        for key, item in value.items()
    }


def _sanitize_value(key: str, value: Any) -> Any:
    if _is_sensitive_key(key):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize_value(str(item_key), item_value)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_value(key, item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(key, item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    lower = key.lower()
    return any(part in lower for part in _SENSITIVE_KEY_PARTS)


def _metadata_int(metadata: Mapping[str, Any], key: str) -> int | None:
    try:
        return int(metadata.get(key))
    except (TypeError, ValueError):
        return None
