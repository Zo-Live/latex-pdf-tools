"""Tests for CLI helper behavior."""

import json
from pathlib import PurePosixPath
import threading
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from texbook import cli as cli_module
from texbook.convert import LatexProjectResult
from texbook.cli import (
    BeamerBoxStyleOption,
    CtexFontProfileOption,
    DocumentClassOption,
    TitleSource,
    _build_converter,
    _parse_pages,
    app,
)
from texbook.extract.base import DocumentExtractionError
from texbook.llm.presets import PromptPreset, default_prompt_preset


class DummyClient:
    def generate_latex_chunk(self, **kwargs):
        raise AssertionError("Not used in converter construction test")


runner = CliRunner()


def _command_option_names(command: str) -> set[str]:
    click_command = typer.main.get_command(app)
    subcommand = click_command.commands[command]
    names: set[str] = set()
    for parameter in subcommand.params:
        names.update(getattr(parameter, "opts", ()))
        names.update(getattr(parameter, "secondary_opts", ()))
    return names


def _custom_preset():
    base = default_prompt_preset()
    return PromptPreset(
        name="custom-cli",
        description="Custom CLI preset",
        version="1",
        chunk_system_prompt=base.chunk_system_prompt,
        chunk_user_template=base.chunk_user_template,
        page_image_label_template=base.page_image_label_template,
        title_system_prompt=base.title_system_prompt,
        title_user_template=base.title_user_template,
        extra_prompt="CLI preset extra",
    )


def test_parse_pages_accepts_ranges_and_deduplicates():
    assert _parse_pages("1,3-5,3") == [1, 3, 4, 5]


def test_parse_pages_all_when_empty():
    assert _parse_pages(None) is None
    assert _parse_pages(" ") is None


def test_parse_pages_rejects_invalid_range():
    with pytest.raises(typer.BadParameter):
        _parse_pages("5-3")


def test_build_converter_normalizes_image_options(tmp_path):
    converter = _build_converter(
        model="test-model",
        api_key="test-key",
        base_url=None,
        temperature=1.0,
        timeout=10.0,
        max_tokens=128,
        chunk_pages=2,
        image_dpi=144,
        image_dpi_min=90,
        image_dpi_max=None,
        image_format="jpg",
        jpeg_quality=92,
        prefetch_chunks=2,
        cache_dir=tmp_path / "cache",
        client=DummyClient(),
    )

    assert converter.image_options.dpi == 144
    assert converter.image_options.dpi_min == 90
    assert converter.image_options.dpi_max == 144
    assert converter.image_options.image_format == "jpeg"
    assert converter.image_options.jpeg_quality == 92
    assert converter.prefetch_chunks == 2
    assert converter.cache_options is not None
    assert converter.cache_options.cache_dir == tmp_path / "cache"
    assert converter.cache_options.llm_model == "test-model"
    assert converter.cache_options.clear is False
    assert converter.prompt_preset.name == "math"
    assert converter.title_source == "filename"
    assert converter.manual_title is None
    assert converter.show_date is False


def test_build_converter_passes_title_and_date_options(tmp_path):
    converter = _build_converter(
        model="test-model",
        api_key="test-key",
        base_url=None,
        temperature=1.0,
        timeout=10.0,
        max_tokens=128,
        chunk_pages=2,
        image_dpi=144,
        cache_dir=tmp_path / "cache",
        title_source=TitleSource.filename,
        manual_title=" 手动标题 ",
        show_date=True,
        client=DummyClient(),
    )

    assert converter.title_source == "filename"
    assert converter.manual_title == "手动标题"
    assert converter.show_date is True

    llm_converter = _build_converter(
        model="test-model",
        api_key="test-key",
        base_url=None,
        temperature=1.0,
        timeout=10.0,
        max_tokens=128,
        chunk_pages=2,
        image_dpi=144,
        cache_dir=tmp_path / "cache",
        title_source=TitleSource.llm,
        client=DummyClient(),
    )

    assert llm_converter.title_source == "llm"


def test_build_converter_passes_document_class_option(tmp_path):
    converter = _build_converter(
        model="test-model",
        api_key="test-key",
        base_url=None,
        temperature=1.0,
        timeout=10.0,
        max_tokens=128,
        chunk_pages=2,
        image_dpi=144,
        cache_dir=tmp_path / "cache",
        document_class=DocumentClassOption.ctexbeamer,
        client=DummyClient(),
    )

    assert converter.document_class_mode.value == "ctexbeamer"


def test_build_converter_passes_latex_output_options(tmp_path):
    converter = _build_converter(
        model="test-model",
        api_key="test-key",
        base_url=None,
        temperature=1.0,
        timeout=10.0,
        max_tokens=128,
        chunk_pages=2,
        image_dpi=144,
        cache_dir=tmp_path / "cache",
        beamer_box_style=BeamerBoxStyleOption.tcolorbox,
        ctex_font_profile=CtexFontProfileOption.local,
        beamer_title_page=False,
        client=DummyClient(),
    )

    assert converter.output_options.beamer_box_style.value == "tcolorbox"
    assert converter.output_options.ctex_font_profile.value == "local"
    assert converter.output_options.beamer_title_page is False


def test_build_converter_accepts_prompt_preset_object(tmp_path):
    preset = _custom_preset()
    converter = _build_converter(
        model="test-model",
        api_key="test-key",
        base_url=None,
        temperature=1.0,
        timeout=10.0,
        max_tokens=128,
        chunk_pages=2,
        image_dpi=144,
        cache_dir=tmp_path / "cache",
        prompt_preset=preset,
        client=DummyClient(),
    )

    assert converter.prompt_preset is preset


def test_build_converter_rejects_unknown_prompt_preset(tmp_path):
    with pytest.raises(typer.BadParameter, match="Unknown prompt preset"):
        _build_converter(
            model="test-model",
            api_key="test-key",
            base_url=None,
            temperature=1.0,
            timeout=10.0,
            max_tokens=128,
            chunk_pages=2,
            image_dpi=144,
            cache_dir=tmp_path / "cache",
            preset="missing-one",
            client=DummyClient(),
        )


def test_build_converter_rejects_invalid_title_options(tmp_path):
    with pytest.raises(typer.BadParameter, match="title"):
        _build_converter(
            model="test-model",
            api_key="test-key",
            base_url=None,
            temperature=1.0,
            timeout=10.0,
            max_tokens=128,
            chunk_pages=2,
            image_dpi=144,
            cache_dir=tmp_path / "cache",
            manual_title=" ",
            client=DummyClient(),
        )

    with pytest.raises(typer.BadParameter, match="title-source"):
        _build_converter(
            model="test-model",
            api_key="test-key",
            base_url=None,
            temperature=1.0,
            timeout=10.0,
            max_tokens=128,
            chunk_pages=2,
            image_dpi=144,
            cache_dir=tmp_path / "cache",
            title_source=TitleSource.llm,
            manual_title="手动标题",
            client=DummyClient(),
        )


def test_build_converter_accepts_unlimited_timeout(tmp_path):
    converter = _build_converter(
        model="test-model",
        api_key="test-key",
        base_url=None,
        temperature=1.0,
        timeout=None,
        max_tokens=128,
        chunk_pages=2,
        image_dpi=144,
        cache_dir=tmp_path / "cache",
        client=DummyClient(),
    )

    assert converter.chunk_pages == 2


def test_build_converter_passes_temperature_to_client_and_cache(tmp_path):
    converter = _build_converter(
        model="test-model",
        api_key="test-key",
        base_url=None,
        temperature=0.2,
        timeout=10.0,
        max_tokens=128,
        chunk_pages=2,
        image_dpi=144,
        cache_dir=tmp_path / "cache",
    )

    assert converter.client.config.temperature == 0.2
    assert converter.cache_options is not None
    assert converter.cache_options.llm_temperature == 0.2


def test_build_converter_accepts_scheduler_options(tmp_path):
    converter = _build_converter(
        model="test-model",
        api_key="test-key",
        base_url=None,
        temperature=1.0,
        timeout=10.0,
        max_tokens=128,
        chunk_pages=2,
        image_dpi=144,
        cache_dir=tmp_path / "cache",
        llm_retries=4,
        llm_retry_initial_delay=0.5,
        llm_retry_max_delay=3.0,
        llm_max_concurrency=2,
        llm_min_request_interval=0.25,
        client=DummyClient(),
    )

    assert converter.scheduler.retry_options.retries == 4
    assert converter.scheduler.retry_options.initial_delay == 0.5
    assert converter.scheduler.retry_options.max_delay == 3.0
    assert converter.scheduler.rate_limiter.max_concurrency == 2
    assert converter.scheduler.rate_limiter.min_request_interval == 0.25


def test_build_converter_supports_cache_controls(tmp_path):
    disabled = _build_converter(
        model="test-model",
        api_key="test-key",
        base_url=None,
        temperature=1.0,
        timeout=10.0,
        max_tokens=128,
        chunk_pages=2,
        image_dpi=144,
        cache_dir=tmp_path / "cache",
        no_cache=True,
        client=DummyClient(),
    )

    assert disabled.cache_options is None

    clearing = _build_converter(
        model="test-model",
        api_key="test-key",
        base_url=None,
        temperature=1.0,
        timeout=10.0,
        max_tokens=128,
        chunk_pages=2,
        image_dpi=144,
        cache_dir=tmp_path / "cache",
        clear_cache=True,
        client=DummyClient(),
    )

    assert clearing.cache_options is not None
    assert clearing.cache_options.clear is True

    with pytest.raises(typer.BadParameter, match="clear-cache"):
        _build_converter(
            model="test-model",
            api_key="test-key",
            base_url=None,
            temperature=1.0,
            timeout=10.0,
            max_tokens=128,
            chunk_pages=2,
            image_dpi=144,
            cache_dir=tmp_path / "cache",
            no_cache=True,
            clear_cache=True,
            client=DummyClient(),
        )


def test_build_converter_rejects_invalid_image_settings():
    with pytest.raises(typer.BadParameter):
        _build_converter(
            model="test-model",
            api_key="test-key",
            base_url=None,
            temperature=1.0,
            timeout=10.0,
            max_tokens=128,
            chunk_pages=2,
            image_dpi=144,
            image_format="gif",
            client=DummyClient(),
        )

    with pytest.raises(typer.BadParameter):
        _build_converter(
            model="test-model",
            api_key="test-key",
            base_url=None,
            temperature=1.0,
            timeout=10.0,
            max_tokens=128,
            chunk_pages=2,
            image_dpi=144,
            image_dpi_min=200,
            image_dpi_max=100,
            client=DummyClient(),
        )

    with pytest.raises(typer.BadParameter):
        _build_converter(
            model="test-model",
            api_key="test-key",
            base_url=None,
            temperature=1.0,
            timeout=10.0,
            max_tokens=128,
            chunk_pages=2,
            image_dpi=144,
            jpeg_quality=0,
            client=DummyClient(),
        )

    with pytest.raises(typer.BadParameter):
        _build_converter(
            model="test-model",
            api_key="test-key",
            base_url=None,
            temperature=1.0,
            timeout=10.0,
            max_tokens=128,
            chunk_pages=2,
            image_dpi=144,
            prefetch_chunks=-1,
            client=DummyClient(),
        )


def test_build_converter_rejects_non_positive_timeout():
    with pytest.raises(typer.BadParameter, match="Timeout"):
        _build_converter(
            model="test-model",
            api_key="test-key",
            base_url=None,
            temperature=1.0,
            timeout=0,
            max_tokens=128,
            chunk_pages=2,
            image_dpi=144,
            client=DummyClient(),
        )


def test_build_converter_rejects_invalid_scheduler_options(tmp_path):
    with pytest.raises(typer.BadParameter, match="retries"):
        _build_converter(
            model="test-model",
            api_key="test-key",
            base_url=None,
            temperature=1.0,
            timeout=10.0,
            max_tokens=128,
            chunk_pages=2,
            image_dpi=144,
            cache_dir=tmp_path / "cache",
            llm_retries=-1,
            client=DummyClient(),
        )

    with pytest.raises(typer.BadParameter, match="concurrency"):
        _build_converter(
            model="test-model",
            api_key="test-key",
            base_url=None,
            temperature=1.0,
            timeout=10.0,
            max_tokens=128,
            chunk_pages=2,
            image_dpi=144,
            cache_dir=tmp_path / "cache",
            llm_max_concurrency=0,
            client=DummyClient(),
        )


def test_presets_cli_lists_builtin_preset():
    result = runner.invoke(app, ["presets", "list"])

    assert result.exit_code == 0
    assert "math" in result.output
    assert "builtin" in result.output


@pytest.mark.parametrize("command", ["extract", "batch"])
def test_conversion_commands_expose_temperature_option(command):
    result = runner.invoke(app, [command, "--help"])

    assert result.exit_code == 0
    assert "--temperature" in result.output


@pytest.mark.parametrize("command", ["extract", "batch"])
def test_conversion_commands_expose_project_options(command):
    options = _command_option_names(command)

    assert "--project" in options
    assert "--force" in options
    assert "--structure" in options
    assert "--structure-chunk-pages" in options
    assert "--structure-max-pages" in options


@pytest.mark.parametrize("command", ["extract", "batch"])
def test_conversion_commands_expose_scheduler_options(command):
    options = _command_option_names(command)

    assert "--llm-retries" in options
    assert "--llm-max-concurrency" in options
    assert "--llm-min-request-interval" in options
    if command == "batch":
        assert "--batch-workers" in options


@pytest.mark.parametrize("command", ["extract", "batch"])
def test_conversion_commands_expose_log_file_option(command):
    options = _command_option_names(command)

    assert "--log-file" in options


def test_presets_cli_adds_and_shows_repository_preset(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_repo_root", lambda: tmp_path)

    result = runner.invoke(
        app,
        ["presets", "add", "--name", "custom-one"],
        input="\n只保留证明\n\n标题短一些\n默认额外\n",
    )
    show_result = runner.invoke(app, ["presets", "show", "custom-one"])

    assert result.exit_code == 0, result.output
    assert show_result.exit_code == 0, show_result.output
    assert "custom-one.json" in result.output
    assert '"name": "custom-one"' in show_result.output
    assert "只保留证明" in show_result.output
    assert "标题短一些" in show_result.output
    assert "默认额外" in show_result.output


def test_extract_cli_reports_conversion_failure_without_traceback(tmp_path, monkeypatch):
    source = tmp_path / "bad.pdf"
    source.write_bytes(b"not a pdf")

    class FailingConverter:
        def convert(self, pdf_path, *, pages=None):
            raise DocumentExtractionError(
                "Cannot open document: unsupported or damaged document.",
                source_file=pdf_path,
            )

    monkeypatch.setattr(cli_module, "_build_converter", lambda **kwargs: FailingConverter())

    result = runner.invoke(app, ["extract", str(source)])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "bad.pdf" in result.output
    assert "unsupported or damaged" in result.output


def test_extract_cli_writes_diagnostic_log_file(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"pdf")
    output = tmp_path / "out.tex"
    log_file = tmp_path / "logs" / "extract.jsonl"

    class LoggingConverter:
        def convert(self, pdf_path, *, pages=None):
            return SimpleNamespace(latex=f"% converted {pdf_path.name}", notes=["note"])

    monkeypatch.setattr(cli_module, "_build_converter", lambda **kwargs: LoggingConverter())

    result = runner.invoke(
        app,
        ["extract", str(source), "-o", str(output), "--log-file", str(log_file)],
    )

    assert result.exit_code == 0, result.output
    assert log_file.exists()
    events = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert [event["kind"] for event in events] == [
        "log_file_started",
        "command_started",
        "write_completed",
        "command_completed",
    ]
    assert events[2]["path"] == str(output)
    assert events[3]["output_kind"] == "tex_file"


def test_extract_project_writes_project_files_and_entrypoint(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_repo_root", lambda: tmp_path)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"pdf")

    class ProjectConverter:
        def convert_project(self, pdf_path, *, pages=None):
            assert pdf_path == source
            return LatexProjectResult(
                files={
                    PurePosixPath("main.tex"): r"\input{chapters/chapter01}",
                    PurePosixPath("preamble.tex"): r"\usepackage{amsmath}",
                    PurePosixPath("chapters/chapter01.tex"): r"\section{A}",
                },
                entrypoint=PurePosixPath("main.tex"),
                notes=["note"],
            )

    monkeypatch.setattr(cli_module, "_build_converter", lambda **kwargs: ProjectConverter())

    result = runner.invoke(app, ["extract", str(source), "--project", "-o", "book"])

    project_dir = tmp_path / "src" / "book"
    assert result.exit_code == 0, result.output
    assert (project_dir / "main.tex").read_text(encoding="utf-8") == (
        r"\input{chapters/chapter01}"
    )
    assert (project_dir / "preamble.tex").read_text(encoding="utf-8") == (
        r"\usepackage{amsmath}"
    )
    assert (project_dir / "chapters" / "chapter01.tex").read_text(
        encoding="utf-8"
    ) == r"\section{A}"
    assert f"入口文件：{project_dir / 'main.tex'}" in result.output
    assert "Note: note" in result.output


def test_extract_project_requires_output_before_conversion(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"pdf")

    def fail_build_converter(**kwargs):
        raise AssertionError("converter should not be built")

    monkeypatch.setattr(cli_module, "_build_converter", fail_build_converter)

    result = runner.invoke(app, ["extract", str(source), "--project"])

    assert result.exit_code != 0
    assert "-o" in result.output
    assert "Traceback" not in result.output


def test_extract_rejects_force_without_project_before_conversion(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"pdf")

    def fail_build_converter(**kwargs):
        raise AssertionError("converter should not be built")

    monkeypatch.setattr(cli_module, "_build_converter", fail_build_converter)

    result = runner.invoke(app, ["extract", str(source), "--force"])

    assert result.exit_code != 0
    assert "--project" in result.output
    assert "Traceback" not in result.output


def test_extract_rejects_structure_without_project_before_conversion(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"pdf")

    def fail_build_converter(**kwargs):
        raise AssertionError("converter should not be built")

    monkeypatch.setattr(cli_module, "_build_converter", fail_build_converter)

    result = runner.invoke(app, ["extract", str(source), "--structure", "off"])

    assert result.exit_code != 0
    assert "--project" in result.output
    assert "Traceback" not in result.output


def test_extract_project_passes_structure_options_to_converter(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_repo_root", lambda: tmp_path)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"pdf")
    captured = {}

    class ProjectConverter:
        def convert_project(self, pdf_path, *, pages=None):
            return LatexProjectResult(
                files={PurePosixPath("main.tex"): "main"},
                entrypoint=PurePosixPath("main.tex"),
            )

    def build_converter(**kwargs):
        captured.update(kwargs)
        return ProjectConverter()

    monkeypatch.setattr(cli_module, "_build_converter", build_converter)

    result = runner.invoke(
        app,
        [
            "extract",
            str(source),
            "--project",
            "-o",
            "book",
            "--structure",
            "llm",
            "--structure-chunk-pages",
            "3",
            "--structure-max-pages",
            "9",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["structure"].value == "llm"
    assert captured["structure_chunk_pages"] == 3
    assert captured["structure_max_pages"] == 9


def test_extract_project_rejects_nonempty_directory_without_force(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_repo_root", lambda: tmp_path)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"pdf")
    project_dir = tmp_path / "src" / "book"
    project_dir.mkdir(parents=True)
    existing = project_dir / "existing.txt"
    existing.write_text("keep", encoding="utf-8")

    class ProjectConverter:
        def convert_project(self, pdf_path, *, pages=None):
            raise AssertionError("conversion should not run for a blocked target")

    monkeypatch.setattr(cli_module, "_build_converter", lambda **kwargs: ProjectConverter())

    result = runner.invoke(app, ["extract", str(source), "--project", "-o", "book"])

    assert result.exit_code == 1
    assert "非空" in result.output
    assert existing.read_text(encoding="utf-8") == "keep"
    assert not (project_dir / "main.tex").exists()


def test_extract_project_force_keeps_old_files_if_conversion_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_repo_root", lambda: tmp_path)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"pdf")
    project_dir = tmp_path / "src" / "book"
    project_dir.mkdir(parents=True)
    existing = project_dir / "existing.txt"
    existing.write_text("keep", encoding="utf-8")

    class ProjectConverter:
        def convert_project(self, pdf_path, *, pages=None):
            raise RuntimeError("LLM failed")

    monkeypatch.setattr(cli_module, "_build_converter", lambda **kwargs: ProjectConverter())

    result = runner.invoke(
        app,
        ["extract", str(source), "--project", "-o", "book", "--force"],
    )

    assert result.exit_code == 1
    assert "LLM failed" in result.output
    assert existing.read_text(encoding="utf-8") == "keep"


def test_extract_project_force_clears_directory_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_repo_root", lambda: tmp_path)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"pdf")
    project_dir = tmp_path / "src" / "book"
    (project_dir / "old").mkdir(parents=True)
    (project_dir / "old" / "stale.txt").write_text("stale", encoding="utf-8")
    (project_dir / "existing.txt").write_text("old", encoding="utf-8")

    class ProjectConverter:
        def convert_project(self, pdf_path, *, pages=None):
            return LatexProjectResult(
                files={
                    PurePosixPath("main.tex"): "new main",
                    PurePosixPath("chapters/chapter01.tex"): "new chapter",
                },
                entrypoint=PurePosixPath("main.tex"),
            )

    monkeypatch.setattr(cli_module, "_build_converter", lambda **kwargs: ProjectConverter())

    result = runner.invoke(
        app,
        ["extract", str(source), "--project", "-o", "book", "--force"],
    )

    assert result.exit_code == 0, result.output
    assert (project_dir / "main.tex").read_text(encoding="utf-8") == "new main"
    assert (project_dir / "chapters" / "chapter01.tex").read_text(
        encoding="utf-8"
    ) == "new chapter"
    assert not (project_dir / "existing.txt").exists()
    assert not (project_dir / "old").exists()


def test_extract_project_rejects_file_output_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_repo_root", lambda: tmp_path)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"pdf")
    output_file = tmp_path / "src" / "book"
    output_file.parent.mkdir()
    output_file.write_text("not a directory", encoding="utf-8")

    class ProjectConverter:
        def convert_project(self, pdf_path, *, pages=None):
            return LatexProjectResult(
                files={PurePosixPath("main.tex"): "new"},
                entrypoint=PurePosixPath("main.tex"),
            )

    monkeypatch.setattr(cli_module, "_build_converter", lambda **kwargs: ProjectConverter())

    result = runner.invoke(app, ["extract", str(source), "--project", "-o", "book"])

    assert result.exit_code == 1
    assert "不是目录" in result.output
    assert output_file.read_text(encoding="utf-8") == "not a directory"


def test_extract_project_force_rejects_protected_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_repo_root", lambda: tmp_path)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"pdf")
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    sentinel = src_dir / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    class ProjectConverter:
        def convert_project(self, pdf_path, *, pages=None):
            return LatexProjectResult(
                files={PurePosixPath("main.tex"): "new"},
                entrypoint=PurePosixPath("main.tex"),
            )

    monkeypatch.setattr(cli_module, "_build_converter", lambda **kwargs: ProjectConverter())

    result = runner.invoke(
        app,
        ["extract", str(source), "--project", "-o", str(src_dir), "--force"],
    )

    assert result.exit_code == 1
    assert "受保护路径" in result.output
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_batch_cli_skips_failed_files_and_summarizes(tmp_path, monkeypatch):
    input_dir = tmp_path / "docs"
    input_dir.mkdir()
    bad_pdf = input_dir / "bad.pdf"
    good_pdf = input_dir / "good.pdf"
    bad_pdf.write_bytes(b"bad")
    good_pdf.write_bytes(b"good")
    output_dir = tmp_path / "out"

    class BatchConverter:
        def convert(self, pdf_path, *, pages=None):
            if pdf_path.name == "bad.pdf":
                raise RuntimeError("LLM failed")
            return SimpleNamespace(latex=f"% converted {pdf_path.name}", notes=["note"])

    monkeypatch.setattr(cli_module, "_build_converter", lambda **kwargs: BatchConverter())

    result = runner.invoke(
        app,
        ["batch", str(input_dir), "-o", str(output_dir), "--pattern", "*.pdf"],
    )

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert (output_dir / "good.tex").read_text(encoding="utf-8") == "% converted good.pdf"
    assert not (output_dir / "bad.tex").exists()
    assert "1 files written" in result.output
    assert "1 failed" in result.output
    assert "bad.pdf" in result.output
    assert "LLM failed" in result.output


def test_batch_project_writes_each_pdf_to_own_project(tmp_path, monkeypatch):
    input_dir = tmp_path / "docs"
    input_dir.mkdir()
    (input_dir / "a.pdf").write_bytes(b"a")
    (input_dir / "b.pdf").write_bytes(b"b")
    output_dir = tmp_path / "out"

    class BatchProjectConverter:
        def convert_project(self, pdf_path, *, pages=None):
            return LatexProjectResult(
                files={
                    PurePosixPath("main.tex"): f"% main {pdf_path.stem}",
                    PurePosixPath("preamble.tex"): "% preamble",
                },
                entrypoint=PurePosixPath("main.tex"),
                notes=[f"note {pdf_path.stem}"],
            )

    monkeypatch.setattr(
        cli_module,
        "_build_converter",
        lambda **kwargs: BatchProjectConverter(),
    )

    result = runner.invoke(
        app,
        ["batch", str(input_dir), "--project", "-o", str(output_dir)],
    )

    assert result.exit_code == 0, result.output
    assert (output_dir / "a" / "main.tex").read_text(encoding="utf-8") == "% main a"
    assert (output_dir / "b" / "main.tex").read_text(encoding="utf-8") == "% main b"
    assert not (output_dir / "a.tex").exists()
    assert "a.pdf: 入口文件" in result.output
    assert "b.pdf: note b" in result.output


def test_batch_project_rejects_nonempty_target_before_conversion(tmp_path, monkeypatch):
    input_dir = tmp_path / "docs"
    input_dir.mkdir()
    (input_dir / "book.pdf").write_bytes(b"book")
    output_dir = tmp_path / "out"
    project_dir = output_dir / "book"
    project_dir.mkdir(parents=True)
    existing = project_dir / "existing.txt"
    existing.write_text("keep", encoding="utf-8")

    def fail_build_converter(**kwargs):
        raise AssertionError("converter should not be built")

    monkeypatch.setattr(cli_module, "_build_converter", fail_build_converter)

    result = runner.invoke(
        app,
        ["batch", str(input_dir), "--project", "-o", str(output_dir)],
    )

    assert result.exit_code == 1
    assert "非空" in result.output
    assert existing.read_text(encoding="utf-8") == "keep"
    assert not (project_dir / "main.tex").exists()
    assert "Traceback" not in result.output


def test_batch_workers_process_files_concurrently(tmp_path, monkeypatch):
    input_dir = tmp_path / "docs"
    input_dir.mkdir()
    (input_dir / "a.pdf").write_bytes(b"a")
    (input_dir / "b.pdf").write_bytes(b"b")
    output_dir = tmp_path / "out"
    first_entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    class BatchConverter:
        def convert(self, pdf_path, *, pages=None):
            with calls_lock:
                calls.append(pdf_path.name)
                call_count = len(calls)
            if call_count == 1:
                first_entered.set()
                assert second_entered.wait(timeout=1)
                release.set()
            else:
                second_entered.set()
                assert first_entered.wait(timeout=1)
                assert release.wait(timeout=1)
            return SimpleNamespace(latex=f"% converted {pdf_path.name}", notes=[])

    monkeypatch.setattr(cli_module, "_build_converter", lambda **kwargs: BatchConverter())

    result = runner.invoke(
        app,
        ["batch", str(input_dir), "-o", str(output_dir), "--batch-workers", "2"],
    )

    assert result.exit_code == 0, result.output
    assert sorted(calls) == ["a.pdf", "b.pdf"]
    assert (output_dir / "a.tex").exists()
    assert (output_dir / "b.tex").exists()


def test_batch_cli_writes_thread_safe_diagnostic_log(tmp_path, monkeypatch):
    input_dir = tmp_path / "docs"
    input_dir.mkdir()
    (input_dir / "a.pdf").write_bytes(b"a")
    (input_dir / "b.pdf").write_bytes(b"b")
    output_dir = tmp_path / "out"
    log_file = tmp_path / "logs" / "batch.jsonl"

    class LoggingConverter:
        def convert(self, pdf_path, *, pages=None):
            return SimpleNamespace(latex=f"% converted {pdf_path.name}", notes=[])

    monkeypatch.setattr(cli_module, "_build_converter", lambda **kwargs: LoggingConverter())

    result = runner.invoke(
        app,
        [
            "batch",
            str(input_dir),
            "-o",
            str(output_dir),
            "--batch-workers",
            "2",
            "--log-file",
            str(log_file),
        ],
    )

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert events[0]["kind"] == "log_file_started"
    assert any(event["kind"] == "write_completed" and event["pdf"] == "a.pdf" for event in events)
    assert any(event["kind"] == "write_completed" and event["pdf"] == "b.pdf" for event in events)
    assert any(event["kind"] == "command_completed" and event["success"] is True for event in events)


def test_batch_rejects_duplicate_output_targets_before_conversion(tmp_path, monkeypatch):
    input_dir = tmp_path / "docs"
    input_dir.mkdir()
    (input_dir / "a.pdf").write_bytes(b"a")
    (input_dir / "a.PDF").write_bytes(b"b")
    output_dir = tmp_path / "out"

    def fail_build_converter(**kwargs):
        raise AssertionError("converter should not be built")

    monkeypatch.setattr(cli_module, "_build_converter", fail_build_converter)

    result = runner.invoke(
        app,
        ["batch", str(input_dir), "-o", str(output_dir), "--pattern", "a.*"],
    )

    assert result.exit_code == 1
    assert "collision" in result.output
    assert "Traceback" not in result.output


def test_batch_project_continues_after_single_failure(tmp_path, monkeypatch):
    input_dir = tmp_path / "docs"
    input_dir.mkdir()
    (input_dir / "bad.pdf").write_bytes(b"bad")
    (input_dir / "good.pdf").write_bytes(b"good")
    output_dir = tmp_path / "out"

    class BatchProjectConverter:
        def convert_project(self, pdf_path, *, pages=None):
            if pdf_path.name == "bad.pdf":
                raise RuntimeError("project failed")
            return LatexProjectResult(
                files={PurePosixPath("main.tex"): f"% {pdf_path.name}"},
                entrypoint=PurePosixPath("main.tex"),
            )

    monkeypatch.setattr(
        cli_module,
        "_build_converter",
        lambda **kwargs: BatchProjectConverter(),
    )

    result = runner.invoke(
        app,
        ["batch", str(input_dir), "--project", "-o", str(output_dir)],
    )

    assert result.exit_code == 1
    assert (output_dir / "good" / "main.tex").read_text(encoding="utf-8") == "% good.pdf"
    assert not (output_dir / "bad").exists()
    assert "1 files written" in result.output
    assert "1 failed" in result.output
    assert "project failed" in result.output


def test_batch_project_force_only_clears_per_pdf_project_dir(tmp_path, monkeypatch):
    input_dir = tmp_path / "docs"
    input_dir.mkdir()
    (input_dir / "book.pdf").write_bytes(b"book")
    output_dir = tmp_path / "out"
    project_dir = output_dir / "book"
    project_dir.mkdir(parents=True)
    (project_dir / "old.txt").write_text("old", encoding="utf-8")
    unrelated = output_dir / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")

    class BatchProjectConverter:
        def convert_project(self, pdf_path, *, pages=None):
            return LatexProjectResult(
                files={PurePosixPath("main.tex"): "new"},
                entrypoint=PurePosixPath("main.tex"),
            )

    monkeypatch.setattr(
        cli_module,
        "_build_converter",
        lambda **kwargs: BatchProjectConverter(),
    )

    result = runner.invoke(
        app,
        ["batch", str(input_dir), "--project", "--force", "-o", str(output_dir)],
    )

    assert result.exit_code == 0, result.output
    assert (project_dir / "main.tex").read_text(encoding="utf-8") == "new"
    assert not (project_dir / "old.txt").exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_batch_rejects_force_without_project_before_conversion(tmp_path, monkeypatch):
    input_dir = tmp_path / "docs"
    input_dir.mkdir()
    (input_dir / "sample.pdf").write_bytes(b"pdf")

    def fail_build_converter(**kwargs):
        raise AssertionError("converter should not be built")

    monkeypatch.setattr(cli_module, "_build_converter", fail_build_converter)

    result = runner.invoke(app, ["batch", str(input_dir), "--force"])

    assert result.exit_code != 0
    assert "--project" in result.output
    assert "Traceback" not in result.output


def test_batch_rejects_structure_without_project_before_conversion(tmp_path, monkeypatch):
    input_dir = tmp_path / "docs"
    input_dir.mkdir()
    (input_dir / "sample.pdf").write_bytes(b"pdf")

    def fail_build_converter(**kwargs):
        raise AssertionError("converter should not be built")

    monkeypatch.setattr(cli_module, "_build_converter", fail_build_converter)

    result = runner.invoke(app, ["batch", str(input_dir), "--structure", "off"])

    assert result.exit_code != 0
    assert "--project" in result.output
    assert "Traceback" not in result.output
