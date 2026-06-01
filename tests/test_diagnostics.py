"""Tests for diagnostic log formatting."""

import json

from texbook.diagnostics import DiagnosticLog
from texbook.llm.scheduler import ProgressEvent


def test_diagnostic_log_writes_jsonl_and_redacts_sensitive_metadata(tmp_path):
    log_file = tmp_path / "diag.jsonl"
    log = DiagnosticLog(log_file)

    log.record(
        "request_failed",
        metadata={
            "api_key": "secret",
            "nested": {"Authorization": "Bearer secret"},
        },
        error="bad response",
    )

    events = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert events[0]["kind"] == "log_file_started"
    assert events[1]["metadata"]["api_key"] == "[redacted]"
    assert events[1]["metadata"]["nested"]["Authorization"] == "[redacted]"


def test_diagnostic_log_promotes_raw_preview_and_finish_reason_from_progress(tmp_path):
    log_file = tmp_path / "diag.jsonl"
    log = DiagnosticLog(log_file)

    log.record_progress(
        ProgressEvent(
            kind="request_failed",
            operation="chunk",
            label="chunk 5/6",
            error="LLM response was truncated before valid JSON could be read.",
            metadata={
                "raw_preview": '{"latex": "\\\\section{集合}"',
                "finish_reason": "length",
            },
        )
    )

    events = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    failed = events[1]
    assert failed["raw_preview"] == '{"latex": "\\\\section{集合}"'
    assert failed["finish_reason"] == "length"
    assert failed["metadata"]["finish_reason"] == "length"
