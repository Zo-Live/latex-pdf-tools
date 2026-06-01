"""CLI entry point for texbook."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Callable, Optional

import click
import typer

from .convert import LatexProjectResult
from .diagnostics import DiagnosticLog
from .document_class import DocumentClassMode
from .extract.base import DocumentExtractionError, ImageRenderOptions
from .llm.cache import ChunkCacheOptions
from .llm.client import OpenAICompatibleClient
from .llm.config import LLMConfig, LLMConfigError
from .llm.factory import PdfConversionOptions, build_pdf_converter
from .llm.pipeline import LLMPdfConverter
from .llm.presets import (
    DEFAULT_PROMPT_PRESET_NAME,
    PromptPreset,
    PromptPresetError,
    build_prompt_preset_from_inputs,
    list_prompt_presets,
    load_prompt_preset,
    save_prompt_preset,
)
from .llm.scheduler import LLMRateLimiter, LLMScheduler, ProgressEvent, RetryOptions
from .output_options import BeamerBoxStyle, CtexFontProfile, LatexOutputOptions
from .structure import StructureMode, StructurePlannerOptions


class TitleSource(str, Enum):
    """Document title source strategy."""

    filename = "filename"
    llm = "llm"


class StructureOption(str, Enum):
    """Project structure planning mode for CLI options."""

    auto = "auto"
    off = "off"
    local = "local"
    llm = "llm"


class DocumentClassOption(str, Enum):
    """LaTeX document class selection mode for CLI options."""

    auto = "auto"
    article = "article"
    book = "book"
    beamer = "beamer"
    ctexart = "ctexart"
    ctexbook = "ctexbook"
    ctexbeamer = "ctexbeamer"


class BeamerBoxStyleOption(str, Enum):
    """Beamer emphasis box style for CLI options."""

    block = "block"
    tcolorbox = "tcolorbox"


class CtexFontProfileOption(str, Enum):
    """CTeX font profile for CLI options."""

    default = "default"
    local = "local"


@dataclass(frozen=True)
class _CliSchedulerOptions:
    retries: int = 2
    retry_initial_delay: float = 2.0
    retry_max_delay: float = 30.0
    max_concurrency: int = 1
    min_request_interval: float = 0.0


class _CliProgressReporter:
    def __init__(self, diagnostic_log: DiagnosticLog | None = None):
        self._lock = Lock()
        self._diagnostic_log = diagnostic_log

    def __call__(self, event: ProgressEvent) -> None:
        if self._diagnostic_log is not None:
            self._diagnostic_log.record_progress(event)
        message = _format_progress_event(event)
        if not message:
            return
        with self._lock:
            typer.echo(message, err=True)


def _format_progress_event(event: ProgressEvent) -> str:
    if event.kind == "batch_item_started":
        return f"Processing: {event.label}"
    if event.kind == "batch_item_failed":
        return f"Failed: {event.label}: {event.error}"
    if event.kind == "stage_started":
        return f"Starting: {event.label or event.operation}"
    if event.kind == "request_started":
        return f"Requesting: {event.label or event.operation}"
    if event.kind == "cache_hit":
        return f"Cache hit: {event.label or event.operation}"
    if event.kind == "retry_scheduled":
        delay = event.delay or 0.0
        return (
            f"Retrying {event.label or event.operation} after {delay:.1f}s "
            f"(attempt {event.attempt}/{event.max_attempts}): {event.error}"
        )
    if event.kind == "request_failed":
        return f"Failed {event.label or event.operation}: {event.error}"
    return ""


def _build_llm_scheduler(
    *,
    llm_retries: int,
    llm_retry_initial_delay: float,
    llm_retry_max_delay: float,
    llm_max_concurrency: int,
    llm_min_request_interval: float,
    progress_reporter: Optional[Callable[[ProgressEvent], None]] = None,
) -> LLMScheduler:
    try:
        scheduler_options = _CliSchedulerOptions(
            retries=llm_retries,
            retry_initial_delay=llm_retry_initial_delay,
            retry_max_delay=llm_retry_max_delay,
            max_concurrency=llm_max_concurrency,
            min_request_interval=llm_min_request_interval,
        )
        retry_options = RetryOptions(
            retries=scheduler_options.retries,
            initial_delay=scheduler_options.retry_initial_delay,
            max_delay=scheduler_options.retry_max_delay,
        )
        rate_limiter = LLMRateLimiter(
            max_concurrency=scheduler_options.max_concurrency,
            min_request_interval=scheduler_options.min_request_interval,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    return LLMScheduler(
        retry_options=retry_options,
        rate_limiter=rate_limiter,
        reporter=progress_reporter,
    )


def _repo_root() -> Path:
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "src" / "texbook"
        ).is_dir():
            return candidate
    return cwd


def _resolve_existing_path(path: Path) -> Path:
    path = path.expanduser()
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, _repo_root() / path]

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    raise typer.BadParameter(f"Path does not exist: {path}")


def _resolve_tex_output(path: Path) -> Path:
    path = path.expanduser()
    if path.is_absolute():
        return path

    root = _repo_root()
    if path.parent == Path("."):
        return root / "src" / path
    return root / path


def _resolve_output_dir(path: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else _repo_root() / path


def _resolve_project_output_dir(path: Path) -> Path:
    path = path.expanduser()
    if path.is_absolute():
        return path

    root = _repo_root()
    if path.parent == Path("."):
        return root / "src" / path
    return root / path


def _directory_has_entries(path: Path) -> bool:
    return any(path.iterdir())


def _assert_safe_project_directory(path: Path) -> None:
    resolved = path.resolve(strict=False)
    root = _repo_root().resolve()
    protected = {
        root,
        root / "src",
        root / "src" / "texbook",
    }
    package_dir = root / "src" / "texbook"
    if resolved in protected or package_dir in resolved.parents:
        raise ValueError(f"拒绝将项目目录写入受保护路径：{resolved}")
    if (resolved / ".git").exists() or (resolved / "pyproject.toml").exists():
        raise ValueError(f"拒绝清空疑似仓库或源码目录：{resolved}")


def _clear_directory(path: Path) -> None:
    for item in path.iterdir():
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()


def _write_project_result(
    project: LatexProjectResult,
    target_dir: Path,
    *,
    force: bool = False,
) -> Path:
    target_dir = target_dir.expanduser()
    _validate_project_output_target(target_dir, force=force)

    if target_dir.exists() and _directory_has_entries(target_dir):
        _clear_directory(target_dir)
    else:
        target_dir.mkdir(parents=True, exist_ok=True)

    for relative_path, content in project.files.items():
        destination = _project_file_destination(target_dir, relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")

    return _project_file_destination(target_dir, project.entrypoint)


def _validate_project_output_target(
    target_dir: Path,
    *,
    force: bool,
) -> None:
    if target_dir.exists() and not target_dir.is_dir():
        raise ValueError(f"项目输出路径已存在但不是目录：{target_dir}")

    if target_dir.exists() and _directory_has_entries(target_dir):
        if not force:
            raise ValueError(
                f"项目输出目录非空：{target_dir}。如需清空后重写，请添加 --force。"
            )
        _assert_safe_project_directory(target_dir)
    else:
        _assert_safe_project_directory(target_dir)


def _project_file_destination(
    target_dir: Path,
    relative_path: PurePosixPath,
) -> Path:
    return target_dir.joinpath(*relative_path.parts)


app = typer.Typer(
    name="texbook",
    help="Convert PDFs to LaTeX source using an LLM-assisted pipeline.",
)
presets_app = typer.Typer(help="Manage prompt presets.")
app.add_typer(presets_app, name="presets")


def _parse_pages(pages: Optional[str]) -> Optional[list[int]]:
    if pages is None or not pages.strip():
        return None

    resolved: list[int] = []
    for chunk in pages.split(","):
        item = chunk.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start = int(start_text.strip())
            end = int(end_text.strip())
            if start <= 0 or end <= 0 or end < start:
                raise typer.BadParameter(f"Invalid page range: {item}")
            resolved.extend(range(start, end + 1))
        else:
            page = int(item)
            if page <= 0:
                raise typer.BadParameter(f"Invalid page number: {item}")
            resolved.append(page)

    if not resolved:
        raise typer.BadParameter("No valid pages were parsed.")
    seen: set[int] = set()
    unique_pages: list[int] = []
    for page in resolved:
        if page in seen:
            continue
        seen.add(page)
        unique_pages.append(page)
    return unique_pages


def _build_converter(
    *,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    temperature: float,
    timeout: Optional[float],
    max_tokens: int,
    chunk_pages: int,
    image_dpi: int,
    image_dpi_min: int = 100,
    image_dpi_max: Optional[int] = None,
    image_format: str = "png",
    jpeg_quality: int = 85,
    prefetch_chunks: int = 1,
    cache_dir: Path = Path("build/.texbook_cache"),
    no_cache: bool = False,
    clear_cache: bool = False,
    extra_prompt: Optional[str] = None,
    preset: str = DEFAULT_PROMPT_PRESET_NAME,
    prompt_preset: Optional[PromptPreset] = None,
    title_source: TitleSource | str = TitleSource.filename,
    manual_title: Optional[str] = None,
    show_date: bool = False,
    document_class: DocumentClassOption | str = DocumentClassOption.auto,
    beamer_box_style: BeamerBoxStyleOption | str = BeamerBoxStyleOption.block,
    ctex_font_profile: CtexFontProfileOption | str = CtexFontProfileOption.default,
    beamer_title_page: bool = True,
    structure: StructureOption | str = StructureOption.auto,
    structure_chunk_pages: int = 8,
    structure_max_pages: int = 32,
    client: Optional[OpenAICompatibleClient] = None,
    scheduler: Optional[LLMScheduler] = None,
    progress_reporter: Optional[Callable[[ProgressEvent], None]] = None,
    llm_retries: int = 2,
    llm_retry_initial_delay: float = 2.0,
    llm_retry_max_delay: float = 30.0,
    llm_max_concurrency: int = 1,
    llm_min_request_interval: float = 0.0,
) -> LLMPdfConverter:
    try:
        resolved_title_source = (
            title_source.value if isinstance(title_source, TitleSource) else str(title_source)
        )
        if resolved_title_source not in {TitleSource.filename.value, TitleSource.llm.value}:
            raise ValueError("--title-source must be filename or llm.")
        resolved_manual_title = None
        if manual_title is not None:
            resolved_manual_title = manual_title.strip()
            if not resolved_manual_title:
                raise ValueError("--title cannot be empty.")
        if resolved_manual_title is not None and resolved_title_source == TitleSource.llm.value:
            raise ValueError("--title cannot be used with --title-source llm.")
        resolved_structure = (
            structure.value if isinstance(structure, StructureOption) else str(structure)
        )
        resolved_document_class = (
            document_class.value
            if isinstance(document_class, DocumentClassOption)
            else str(document_class)
        )
        structure_options = StructurePlannerOptions(
            mode=StructureMode(resolved_structure),
            chunk_pages=structure_chunk_pages,
            max_pages=structure_max_pages,
        )
        document_class_mode = DocumentClassMode.from_value(resolved_document_class)
        resolved_beamer_box_style = (
            beamer_box_style.value
            if isinstance(beamer_box_style, Enum)
            else str(beamer_box_style)
        )
        resolved_ctex_font_profile = (
            ctex_font_profile.value
            if isinstance(ctex_font_profile, Enum)
            else str(ctex_font_profile)
        )
        output_options = LatexOutputOptions(
            beamer_box_style=BeamerBoxStyle(resolved_beamer_box_style),
            ctex_font_profile=CtexFontProfile(resolved_ctex_font_profile),
            beamer_title_page=beamer_title_page,
        )

        config = LLMConfig.from_values(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            timeout=timeout,
            max_tokens=max_tokens,
        )
        image_options = ImageRenderOptions(
            dpi=image_dpi,
            dpi_min=image_dpi_min,
            dpi_max=image_dpi if image_dpi_max is None else image_dpi_max,
            image_format=image_format,
            jpeg_quality=jpeg_quality,
        )
        if prefetch_chunks < 0:
            raise ValueError("prefetch_chunks must be non-negative.")
        if no_cache and clear_cache:
            raise ValueError("--clear-cache cannot be used with --no-cache.")
        resolved_prompt_preset = prompt_preset or load_prompt_preset(
            preset,
            repo_root=_repo_root(),
        )
    except (LLMConfigError, PromptPresetError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    cache_options = None
    if not no_cache:
        cache_options = ChunkCacheOptions(
            cache_dir=_resolve_output_dir(cache_dir),
            clear=clear_cache,
            llm_model=config.model,
            llm_base_url=config.base_url,
            llm_temperature=config.temperature,
            llm_max_tokens=config.max_tokens,
        )
    try:
        conversion_options = PdfConversionOptions(
            chunk_pages=chunk_pages,
            image_dpi=image_dpi,
            image_options=image_options,
            prefetch_chunks=prefetch_chunks,
            cache_options=cache_options,
            extra_prompt=extra_prompt or "",
            prompt_preset=resolved_prompt_preset,
            title_source=resolved_title_source,
            manual_title=resolved_manual_title,
            show_date=show_date,
            document_class=document_class_mode,
            structure_options=structure_options,
            output_options=output_options,
            retry_options=RetryOptions(
                retries=llm_retries,
                initial_delay=llm_retry_initial_delay,
                max_delay=llm_retry_max_delay,
            ),
            llm_max_concurrency=llm_max_concurrency,
            llm_min_request_interval=llm_min_request_interval,
        )
        return build_pdf_converter(
            config,
            options=conversion_options,
            client=client,
            scheduler=scheduler,
            progress_reporter=progress_reporter,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _load_cli_prompt_preset(name: str) -> PromptPreset:
    try:
        return load_prompt_preset(name, repo_root=_repo_root())
    except PromptPresetError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _format_conversion_failure(path: Path, exc: Exception) -> str:
    return f"Failed to convert {path.name}: {_describe_cli_error(exc)}"


def _record_command_failure(
    diagnostic_log: DiagnosticLog | None,
    *,
    command: str,
    path: Path,
    exc: Exception,
) -> None:
    if diagnostic_log is None:
        return
    diagnostic_log.record(
        "command_failed",
        command=command,
        pdf=path,
        error=_describe_cli_error(exc),
        exception_type=exc.__class__.__name__,
    )


def _describe_cli_error(exc: Exception) -> str:
    message = str(exc).strip()
    if isinstance(exc, DocumentExtractionError):
        return message or "document extraction failed"
    if isinstance(exc, OSError):
        return message or exc.__class__.__name__
    return message or exc.__class__.__name__


@dataclass(frozen=True)
class _BatchJob:
    pdf: Path
    target: Path


@dataclass(frozen=True)
class _BatchJobResult:
    pdf: Path
    entrypoint: Path | None = None
    notes: tuple[str, ...] = ()


def _validate_batch_targets(
    jobs: list[_BatchJob],
    *,
    project: bool,
    force: bool,
) -> None:
    seen: dict[Path, Path] = {}
    for job in jobs:
        resolved = job.target.resolve(strict=False)
        existing = seen.get(resolved)
        if existing is not None:
            raise click.ClickException(
                f"Output target collision: {existing.name} and {job.pdf.name} "
                f"would both write to {job.target}"
            )
        seen[resolved] = job.pdf
        if project:
            try:
                _validate_project_output_target(job.target, force=force)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc


def _run_batch_job(
    *,
    job: _BatchJob,
    build_converter: Callable[[], LLMPdfConverter],
    project: bool,
    page_selection: list[int] | None,
    force: bool,
    diagnostic_log: DiagnosticLog | None = None,
) -> _BatchJobResult:
    converter = build_converter()
    if project:
        project_result = converter.convert_project(job.pdf, pages=page_selection)
        entrypoint = _write_project_result(
            project_result,
            job.target,
            force=force,
        )
        if diagnostic_log is not None:
            diagnostic_log.record(
                "write_completed",
                pdf=job.pdf.name,
                path=str(entrypoint),
                output_kind="project",
                target=str(job.target),
            )
        return _BatchJobResult(
            pdf=job.pdf,
            entrypoint=entrypoint,
            notes=tuple(project_result.notes),
        )

    result = converter.convert(job.pdf, pages=page_selection)
    job.target.write_text(result.latex, encoding="utf-8")
    if diagnostic_log is not None:
        diagnostic_log.record(
            "write_completed",
            pdf=job.pdf.name,
            path=str(job.target),
            output_kind="tex_file",
        )
    return _BatchJobResult(
        pdf=job.pdf,
        notes=tuple(result.notes),
    )


@presets_app.command("list")
def presets_list():
    """List built-in and repository-local prompt presets."""
    try:
        items = list_prompt_presets(repo_root=_repo_root())
    except PromptPresetError as exc:
        raise typer.BadParameter(str(exc)) from exc

    for item in items:
        location = item.source
        typer.echo(f"{item.preset.name}\t{location}\t{item.preset.description}")


@presets_app.command("show")
def presets_show(
    name: str = typer.Argument(..., help="Prompt preset name"),
):
    """Show one prompt preset as JSON."""
    preset = _load_cli_prompt_preset(name)
    typer.echo(
        json.dumps(
            preset.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


@presets_app.command("add")
def presets_add(
    name: Optional[str] = typer.Option(
        None,
        "--name",
        help="New preset name, e.g. math-lite",
    ),
    from_preset: str = typer.Option(
        DEFAULT_PROMPT_PRESET_NAME,
        "--from-preset",
        help="Existing preset to copy before applying interactive instructions",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace an existing repository-local preset with the same name",
    ),
):
    """Interactively add a repository-local prompt preset."""
    try:
        base_preset = load_prompt_preset(from_preset, repo_root=_repo_root())
    except PromptPresetError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo("创建 Prompt 预设。直接回车表示沿用当前基础预设。")
    preset_name = (name or typer.prompt("预设名称（小写英文、数字、- 或 _）")).strip()
    description = str(
        typer.prompt(
            "简短说明",
            default=f"{base_preset.description} 自定义版",
        )
    ).strip()
    chunk_rule = str(
        typer.prompt(
            "正文整理规则",
            default="",
            show_default=False,
        )
    ).strip()
    chunk_context = str(
        typer.prompt(
            "分块输入说明",
            default="",
            show_default=False,
        )
    ).strip()
    title_rule = str(
        typer.prompt(
            "标题生成规则",
            default="",
            show_default=False,
        )
    ).strip()
    preset_extra = str(
        typer.prompt(
            "默认额外说明",
            default="",
            show_default=False,
        )
    ).strip()

    try:
        preset = build_prompt_preset_from_inputs(
            name=preset_name,
            description=description,
            base_preset=base_preset,
            chunk_rule=chunk_rule,
            chunk_context=chunk_context,
            title_rule=title_rule,
            extra_prompt=preset_extra,
        )
        path = save_prompt_preset(
            preset,
            repo_root=_repo_root(),
            overwrite=overwrite,
        )
    except PromptPresetError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"已保存预设：{path}")


@app.command()
def extract(
    pdf_path: Path = typer.Argument(
        ...,
        help="Path to the PDF file to extract",
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    output: Optional[Path] = typer.Option(
        None,
        "-o",
        "--output",
        help="Output .tex file path, or project directory with --project",
    ),
    project: bool = typer.Option(
        False,
        "--project",
        help="Write a directory-style LaTeX project instead of one .tex file",
    ),
    structure: StructureOption = typer.Option(
        StructureOption.auto,
        "--structure",
        case_sensitive=False,
        help="Project structure planning mode: auto, off, local, or llm",
    ),
    structure_chunk_pages: int = typer.Option(
        8,
        "--structure-chunk-pages",
        help="Pages per structure-planning LLM request",
    ),
    structure_max_pages: int = typer.Option(
        32,
        "--structure-max-pages",
        help="Maximum leading pages to inspect with images for structure planning",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Clear an existing project output directory before writing",
    ),
    pages: Optional[str] = typer.Option(
        None, "--pages", help="Page selection such as 1,3-5 (1-based)"
    ),
    title: Optional[str] = typer.Option(
        None,
        "--title",
        help="Manual LaTeX document title for this single PDF",
    ),
    title_source: TitleSource = typer.Option(
        TitleSource.filename,
        "--title-source",
        case_sensitive=False,
        help="Title source: filename or llm",
    ),
    show_date: bool = typer.Option(
        False,
        "--show-date/--hide-date",
        help="Show \\today in the generated LaTeX title block",
    ),
    document_class: DocumentClassOption = typer.Option(
        DocumentClassOption.auto,
        "--document-class",
        case_sensitive=False,
        help=(
            "LaTeX document class: auto, article, book, beamer, "
            "ctexart, ctexbook, or ctexbeamer"
        ),
    ),
    beamer_box_style: BeamerBoxStyleOption = typer.Option(
        BeamerBoxStyleOption.block,
        "--beamer-box-style",
        case_sensitive=False,
        help="Beamer box style: block or tcolorbox",
    ),
    ctex_font_profile: CtexFontProfileOption = typer.Option(
        CtexFontProfileOption.default,
        "--ctex-font-profile",
        case_sensitive=False,
        help="CTeX font profile: default or local",
    ),
    beamer_title_page: bool = typer.Option(
        True,
        "--beamer-title-page/--no-beamer-title-page",
        help="Generate an extra Beamer title page from the LaTeX title block",
    ),
    preset: str = typer.Option(
        DEFAULT_PROMPT_PRESET_NAME,
        "--preset",
        help="Prompt preset name",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        envvar="TEXBOOK_MODEL",
        help="LLM model name",
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        envvar="TEXBOOK_API_KEY",
        help="LLM API key",
        hide_input=True,
    ),
    base_url: Optional[str] = typer.Option(
        None,
        "--base-url",
        envvar="TEXBOOK_BASE_URL",
        help="OpenAI-compatible API base URL",
    ),
    temperature: float = typer.Option(
        1.0, "--temperature", help="Sampling temperature"
    ),
    timeout: Optional[float] = typer.Option(
        None,
        "--timeout",
        help="LLM request read timeout in seconds (default: wait indefinitely)",
    ),
    max_tokens: int = typer.Option(
        128000, "--max-tokens", help="Maximum tokens in LLM response"
    ),
    chunk_pages: int = typer.Option(
        4, "--chunk-pages", help="Number of pages per LLM request"
    ),
    image_dpi: int = typer.Option(
        160, "--image-dpi", help="Render DPI for page images"
    ),
    image_dpi_min: int = typer.Option(
        100, "--image-dpi-min", help="Minimum render DPI for auto image mode"
    ),
    image_dpi_max: Optional[int] = typer.Option(
        None,
        "--image-dpi-max",
        help="Maximum render DPI for auto image mode (default: --image-dpi)",
    ),
    image_format: str = typer.Option(
        "png", "--image-format", help="Image format: png, jpeg, jpg, or auto"
    ),
    jpeg_quality: int = typer.Option(
        85, "--jpeg-quality", help="JPEG quality for rendered page images"
    ),
    prefetch_chunks: int = typer.Option(
        1, "--prefetch-chunks", help="Number of future chunks to pre-render"
    ),
    llm_retries: int = typer.Option(
        2, "--llm-retries", help="Retries for recoverable LLM request failures"
    ),
    llm_retry_initial_delay: float = typer.Option(
        2.0,
        "--llm-retry-initial-delay",
        help="Initial retry delay in seconds for recoverable LLM failures",
    ),
    llm_retry_max_delay: float = typer.Option(
        30.0,
        "--llm-retry-max-delay",
        help="Maximum retry delay in seconds for recoverable LLM failures",
    ),
    llm_max_concurrency: int = typer.Option(
        1, "--llm-max-concurrency", help="Maximum concurrent LLM requests"
    ),
    llm_min_request_interval: float = typer.Option(
        0.0,
        "--llm-min-request-interval",
        help="Minimum seconds between LLM request starts",
    ),
    cache_dir: Path = typer.Option(
        Path("build/.texbook_cache"),
        "--cache-dir",
        help="Directory for resumable chunk cache",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Disable resumable chunk cache",
    ),
    clear_cache: bool = typer.Option(
        False,
        "--clear-cache",
        help="Clear matching chunk cache before conversion",
    ),
    log_file: Optional[Path] = typer.Option(
        None,
        "--log-file",
        help="Write a live JSONL diagnostic log to the given file",
    ),
    extra_prompt: Optional[str] = typer.Option(
        None, "--extra-prompt", help="额外的系统提示文字（追加到默认要求之后）"
    ),
):
    """Extract content from a single PDF and convert to LaTeX."""
    diagnostic_log = DiagnosticLog(log_file) if log_file is not None else None
    try:
        pdf_path = _resolve_existing_path(pdf_path)
        if not pdf_path.is_file():
            raise typer.BadParameter(f"Not a file: {pdf_path}")
        if project and output is None:
            raise typer.BadParameter("--project 需要同时指定 -o/--output 项目目录。")
        if force and not project:
            raise typer.BadParameter("--force 只能与 --project 一起使用。")
        if not project and structure != StructureOption.auto:
            raise typer.BadParameter("--structure 只能与 --project 一起使用。")

        page_selection = _parse_pages(pages)
        if diagnostic_log is not None:
            diagnostic_log.record(
                "command_started",
                command="extract",
                pdf=pdf_path,
                project=project,
            )
        progress_reporter = _CliProgressReporter(diagnostic_log)
        converter = _build_converter(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            timeout=timeout,
            max_tokens=max_tokens,
            chunk_pages=chunk_pages,
            image_dpi=image_dpi,
            image_dpi_min=image_dpi_min,
            image_dpi_max=image_dpi_max,
            image_format=image_format,
            jpeg_quality=jpeg_quality,
            prefetch_chunks=prefetch_chunks,
            cache_dir=cache_dir,
            no_cache=no_cache,
            clear_cache=clear_cache,
            extra_prompt=extra_prompt,
            preset=preset,
            title_source=title_source,
            manual_title=title,
            show_date=show_date,
            document_class=document_class,
            beamer_box_style=beamer_box_style,
            ctex_font_profile=ctex_font_profile,
            beamer_title_page=beamer_title_page,
            structure=structure,
            structure_chunk_pages=structure_chunk_pages,
            structure_max_pages=structure_max_pages,
            progress_reporter=progress_reporter,
            llm_retries=llm_retries,
            llm_retry_initial_delay=llm_retry_initial_delay,
            llm_retry_max_delay=llm_retry_max_delay,
            llm_max_concurrency=llm_max_concurrency,
            llm_min_request_interval=llm_min_request_interval,
        )
        if project:
            assert output is not None
            project_dir = _resolve_project_output_dir(output)
            _validate_project_output_target(project_dir, force=force)
            project_result = converter.convert_project(pdf_path, pages=page_selection)
            entrypoint = _write_project_result(
                project_result,
                project_dir,
                force=force,
            )
            if diagnostic_log is not None:
                diagnostic_log.record(
                    "write_completed",
                    pdf=pdf_path.name,
                    path=str(entrypoint),
                    output_kind="project",
                    target=str(project_dir),
                )
                diagnostic_log.record(
                    "command_completed",
                    command="extract",
                    pdf=pdf_path,
                    target=entrypoint,
                    output_kind="project",
                )
            typer.echo(f"项目目录：{project_dir}", err=True)
            typer.echo(f"入口文件：{entrypoint}", err=True)
            notes = project_result.notes
        else:
            result = converter.convert(pdf_path, pages=page_selection)
            latex = result.latex
            if output:
                output = _resolve_tex_output(output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(latex, encoding="utf-8")
                if diagnostic_log is not None:
                    diagnostic_log.record(
                        "write_completed",
                        pdf=pdf_path.name,
                        path=str(output),
                        output_kind="tex_file",
                    )
                    diagnostic_log.record(
                        "command_completed",
                        command="extract",
                        pdf=pdf_path,
                        target=output,
                        output_kind="tex_file",
                    )
                typer.echo(f"Written to {output}", err=True)
            else:
                if diagnostic_log is not None:
                    diagnostic_log.record(
                        "command_completed",
                        command="extract",
                        pdf=pdf_path,
                        output_kind="stdout",
                    )
                typer.echo(latex)
            notes = result.notes

        for note in notes:
            typer.echo(f"Note: {note}", err=True)
    except Exception as exc:
        _record_command_failure(diagnostic_log, command="extract", path=pdf_path, exc=exc)
        if isinstance(exc, (typer.BadParameter, click.ClickException, typer.Exit)):
            raise
        raise click.ClickException(_format_conversion_failure(pdf_path, exc)) from exc


@app.command()
def batch(
    directory: Path = typer.Argument(
        ...,
        help="Directory containing PDF files",
        file_okay=False,
        dir_okay=True,
    ),
    output_dir: Path = typer.Option(
        Path("src"),
        "-o",
        "--output-dir",
        help="Directory to write .tex files or project directories to",
    ),
    project: bool = typer.Option(
        False,
        "--project",
        help="Write each PDF to its own directory-style LaTeX project",
    ),
    structure: StructureOption = typer.Option(
        StructureOption.auto,
        "--structure",
        case_sensitive=False,
        help="Project structure planning mode: auto, off, local, or llm",
    ),
    structure_chunk_pages: int = typer.Option(
        8,
        "--structure-chunk-pages",
        help="Pages per structure-planning LLM request",
    ),
    structure_max_pages: int = typer.Option(
        32,
        "--structure-max-pages",
        help="Maximum leading pages to inspect with images for structure planning",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Clear existing project directories before writing",
    ),
    pattern: str = typer.Option(
        "*.pdf", "--pattern", help="Glob pattern for PDF files"
    ),
    pages: Optional[str] = typer.Option(
        None, "--pages", help="Page selection such as 1,3-5 (1-based)"
    ),
    title_source: TitleSource = typer.Option(
        TitleSource.filename,
        "--title-source",
        case_sensitive=False,
        help="Title source: filename or llm",
    ),
    show_date: bool = typer.Option(
        False,
        "--show-date/--hide-date",
        help="Show \\today in the generated LaTeX title block",
    ),
    document_class: DocumentClassOption = typer.Option(
        DocumentClassOption.auto,
        "--document-class",
        case_sensitive=False,
        help=(
            "LaTeX document class: auto, article, book, beamer, "
            "ctexart, ctexbook, or ctexbeamer"
        ),
    ),
    beamer_box_style: BeamerBoxStyleOption = typer.Option(
        BeamerBoxStyleOption.block,
        "--beamer-box-style",
        case_sensitive=False,
        help="Beamer box style: block or tcolorbox",
    ),
    ctex_font_profile: CtexFontProfileOption = typer.Option(
        CtexFontProfileOption.default,
        "--ctex-font-profile",
        case_sensitive=False,
        help="CTeX font profile: default or local",
    ),
    beamer_title_page: bool = typer.Option(
        True,
        "--beamer-title-page/--no-beamer-title-page",
        help="Generate an extra Beamer title page from the LaTeX title block",
    ),
    preset: str = typer.Option(
        DEFAULT_PROMPT_PRESET_NAME,
        "--preset",
        help="Prompt preset name",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        envvar="TEXBOOK_MODEL",
        help="LLM model name",
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        envvar="TEXBOOK_API_KEY",
        help="LLM API key",
        hide_input=True,
    ),
    base_url: Optional[str] = typer.Option(
        None,
        "--base-url",
        envvar="TEXBOOK_BASE_URL",
        help="OpenAI-compatible API base URL",
    ),
    temperature: float = typer.Option(
        1.0, "--temperature", help="Sampling temperature"
    ),
    timeout: Optional[float] = typer.Option(
        None,
        "--timeout",
        help="LLM request read timeout in seconds (default: wait indefinitely)",
    ),
    max_tokens: int = typer.Option(
        128000, "--max-tokens", help="Maximum tokens in LLM response"
    ),
    chunk_pages: int = typer.Option(
        4, "--chunk-pages", help="Number of pages per LLM request"
    ),
    image_dpi: int = typer.Option(
        160, "--image-dpi", help="Render DPI for page images"
    ),
    image_dpi_min: int = typer.Option(
        100, "--image-dpi-min", help="Minimum render DPI for auto image mode"
    ),
    image_dpi_max: Optional[int] = typer.Option(
        None,
        "--image-dpi-max",
        help="Maximum render DPI for auto image mode (default: --image-dpi)",
    ),
    image_format: str = typer.Option(
        "png", "--image-format", help="Image format: png, jpeg, jpg, or auto"
    ),
    jpeg_quality: int = typer.Option(
        85, "--jpeg-quality", help="JPEG quality for rendered page images"
    ),
    prefetch_chunks: int = typer.Option(
        1, "--prefetch-chunks", help="Number of future chunks to pre-render"
    ),
    llm_retries: int = typer.Option(
        2, "--llm-retries", help="Retries for recoverable LLM request failures"
    ),
    llm_retry_initial_delay: float = typer.Option(
        2.0,
        "--llm-retry-initial-delay",
        help="Initial retry delay in seconds for recoverable LLM failures",
    ),
    llm_retry_max_delay: float = typer.Option(
        30.0,
        "--llm-retry-max-delay",
        help="Maximum retry delay in seconds for recoverable LLM failures",
    ),
    llm_max_concurrency: int = typer.Option(
        1, "--llm-max-concurrency", help="Maximum concurrent LLM requests"
    ),
    llm_min_request_interval: float = typer.Option(
        0.0,
        "--llm-min-request-interval",
        help="Minimum seconds between LLM request starts",
    ),
    batch_workers: int = typer.Option(
        1,
        "--batch-workers",
        help="Number of PDF files to convert concurrently in batch mode",
    ),
    cache_dir: Path = typer.Option(
        Path("build/.texbook_cache"),
        "--cache-dir",
        help="Directory for resumable chunk cache",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Disable resumable chunk cache",
    ),
    clear_cache: bool = typer.Option(
        False,
        "--clear-cache",
        help="Clear matching chunk cache before conversion",
    ),
    log_file: Optional[Path] = typer.Option(
        None,
        "--log-file",
        help="Write a live JSONL diagnostic log to the given file",
    ),
    extra_prompt: Optional[str] = typer.Option(
        None, "--extra-prompt", help="额外的系统提示文字（追加到默认要求之后）"
    ),
):
    """Extract all matching PDFs in a directory to LaTeX outputs."""
    diagnostic_log = DiagnosticLog(log_file) if log_file is not None else None
    failures: list[tuple[Path, str]] = []
    written_count = 0

    def handle_success(job_result: _BatchJobResult) -> None:
        nonlocal written_count
        written_count += 1
        progress_reporter(
            ProgressEvent(
                kind="batch_item_completed",
                operation="batch",
                label=job_result.pdf.name,
            )
        )
        if project and job_result.entrypoint is not None:
            typer.echo(f"{job_result.pdf.name}: 入口文件：{job_result.entrypoint}", err=True)
        for note in job_result.notes:
            typer.echo(f"{job_result.pdf.name}: {note}", err=True)

    def handle_failure(pdf: Path, exc: Exception) -> None:
        reason = _describe_cli_error(exc)
        failures.append((pdf, reason))
        progress_reporter(
            ProgressEvent(
                kind="batch_item_failed",
                operation="batch",
                label=pdf.name,
                error=reason,
            )
        )

    try:
        directory = _resolve_existing_path(directory)
        if not directory.is_dir():
            raise typer.BadParameter(f"Not a directory: {directory}")
        if force and not project:
            raise typer.BadParameter("--force 只能与 --project 一起使用。")
        if not project and structure != StructureOption.auto:
            raise typer.BadParameter("--structure 只能与 --project 一起使用。")
        if batch_workers <= 0:
            raise typer.BadParameter("--batch-workers must be positive.")

        output_dir = _resolve_output_dir(output_dir)
        page_selection = _parse_pages(pages)
        if diagnostic_log is not None:
            diagnostic_log.record(
                "command_started",
                command="batch",
                pdf=directory,
                project=project,
            )
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise click.ClickException(
                f"Cannot create output directory {output_dir}: {_describe_cli_error(exc)}"
            ) from exc

        pdf_files = sorted(Path(directory).glob(pattern))
        if not pdf_files:
            typer.echo(f"No files found matching '{pattern}' in {directory}", err=True)
            raise typer.Exit(1)

        jobs = [
            _BatchJob(
                pdf=pdf,
                target=(output_dir / pdf.stem if project else output_dir / f"{pdf.stem}.tex"),
            )
            for pdf in pdf_files
        ]
        _validate_batch_targets(jobs, project=project, force=force)

        progress_reporter = _CliProgressReporter(diagnostic_log)
        shared_scheduler = _build_llm_scheduler(
            llm_retries=llm_retries,
            llm_retry_initial_delay=llm_retry_initial_delay,
            llm_retry_max_delay=llm_retry_max_delay,
            llm_max_concurrency=llm_max_concurrency,
            llm_min_request_interval=llm_min_request_interval,
            progress_reporter=progress_reporter,
        )

        def build_converter() -> LLMPdfConverter:
            return _build_converter(
                model=model,
                api_key=api_key,
                base_url=base_url,
                temperature=temperature,
                timeout=timeout,
                max_tokens=max_tokens,
                chunk_pages=chunk_pages,
                image_dpi=image_dpi,
                image_dpi_min=image_dpi_min,
                image_dpi_max=image_dpi_max,
                image_format=image_format,
                jpeg_quality=jpeg_quality,
                prefetch_chunks=prefetch_chunks,
                cache_dir=cache_dir,
                no_cache=no_cache,
                clear_cache=clear_cache,
                extra_prompt=extra_prompt,
                preset=preset,
                title_source=title_source,
                show_date=show_date,
                document_class=document_class,
                beamer_box_style=beamer_box_style,
                ctex_font_profile=ctex_font_profile,
                beamer_title_page=beamer_title_page,
                structure=structure,
                structure_chunk_pages=structure_chunk_pages,
                structure_max_pages=structure_max_pages,
                scheduler=shared_scheduler,
                progress_reporter=progress_reporter,
                llm_retries=llm_retries,
                llm_retry_initial_delay=llm_retry_initial_delay,
                llm_retry_max_delay=llm_retry_max_delay,
                llm_max_concurrency=llm_max_concurrency,
                llm_min_request_interval=llm_min_request_interval,
            )

        if diagnostic_log is not None:
            diagnostic_log.record(
                "batch_planned",
                command="batch",
                pdf=directory,
                target=output_dir,
                project=project,
                jobs=[str(job.pdf) for job in jobs],
            )

        if batch_workers == 1:
            for job in jobs:
                progress_reporter(
                    ProgressEvent(
                        kind="batch_item_started",
                        operation="batch",
                        label=job.pdf.name,
                    )
                )
                try:
                    handle_success(
                        _run_batch_job(
                            job=job,
                            build_converter=build_converter,
                            project=project,
                            page_selection=page_selection,
                            force=force,
                            diagnostic_log=diagnostic_log,
                        )
                    )
                except Exception as exc:
                    handle_failure(job.pdf, exc)
                    continue
        else:
            with ThreadPoolExecutor(max_workers=batch_workers) as executor:
                future_jobs = {}
                for job in jobs:
                    progress_reporter(
                        ProgressEvent(
                            kind="batch_item_started",
                            operation="batch",
                            label=job.pdf.name,
                        )
                    )
                    future = executor.submit(
                        _run_batch_job,
                        job=job,
                        build_converter=build_converter,
                        project=project,
                        page_selection=page_selection,
                        force=force,
                        diagnostic_log=diagnostic_log,
                    )
                    future_jobs[future] = job

                for future in as_completed(future_jobs):
                    job = future_jobs[future]
                    try:
                        handle_success(future.result())
                    except Exception as exc:
                        handle_failure(job.pdf, exc)

        if failures:
            typer.echo(
                f"Done with failures. {written_count} files written to {output_dir}; "
                f"{len(failures)} failed.",
                err=True,
            )
            typer.echo("Failures:", err=True)
            for failed_file, reason in failures:
                typer.echo(f"- {failed_file.name}: {reason}", err=True)
            if diagnostic_log is not None:
                diagnostic_log.record(
                    "command_completed",
                    command="batch",
                    pdf=directory,
                    target=output_dir,
                    project=project,
                    success=False,
                    written_count=written_count,
                    failed_count=len(failures),
                )
            raise typer.Exit(1)

        if diagnostic_log is not None:
            diagnostic_log.record(
                "command_completed",
                command="batch",
                pdf=directory,
                target=output_dir,
                project=project,
                success=True,
                written_count=written_count,
            )
        typer.echo(f"Done. {written_count} files written to {output_dir}", err=True)
    except Exception as exc:
        if not isinstance(exc, typer.Exit):
            _record_command_failure(diagnostic_log, command="batch", path=directory, exc=exc)
        if isinstance(exc, (typer.BadParameter, click.ClickException, typer.Exit)):
            raise
        raise click.ClickException(
            f"Failed to convert {directory.name}: {_describe_cli_error(exc)}"
        ) from exc


if __name__ == "__main__":
    app()
