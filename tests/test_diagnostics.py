"""Tests for diagnostic log formatting."""

import json

from texbook.diagnostics import DiagnosticLog


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
