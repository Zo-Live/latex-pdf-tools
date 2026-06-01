"""GUI-only conversion settings state and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

from texbook.gui.display import GuiLanguage
from texbook.gui.i18n import tr
from texbook.gui.selection import GuiInputKind, GuiPathSelectionState
from texbook.llm.presets import DEFAULT_PROMPT_PRESET_NAME


def default_gui_log_file_path() -> str:
    """Return the default GUI diagnostic log file path."""
    return _join_path_text("build/.texbook_cache", "texbook.log")


class GuiOutputKind(str, Enum):
    """Output forms exposed by the GUI."""

    tex_file = "tex_file"
    single_file = "tex_file"
    project = "project"


# Backward-compatible alias for tests and callers from the earlier GUI stage.
GuiConversionMode = GuiOutputKind


class GuiApiKeySource(str, Enum):
    """API Key sources supported by the GUI model panel."""

    direct = "direct"
    environment = "environment"


@dataclass(frozen=True)
class GuiConversionSettings:
    """All conversion options currently represented by the GUI panel."""

    path_state: GuiPathSelectionState = field(default_factory=GuiPathSelectionState)
    output_kind: GuiOutputKind = GuiOutputKind.tex_file
    conversion_mode: GuiOutputKind | None = None
    confirm_overwrite: bool = True
    batch_pattern: str = "*.pdf"
    pages: str = ""
    document_class: str = "auto"
    structure_mode: str = "auto"
    structure_chunk_pages: int = 8
    structure_max_pages: int = 32
    manual_title: str = ""
    title_source: str = "filename"
    show_date: bool = False
    beamer_title_page: bool = True
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_source: GuiApiKeySource = GuiApiKeySource.direct
    prompt_preset: str = DEFAULT_PROMPT_PRESET_NAME
    extra_prompt: str = ""
    temperature: float = 1.0
    timeout_seconds: float | None = None
    max_tokens: int = 128000
    cache_enabled: bool = True
    cache_directory: str = "build/.texbook_cache"
    clear_cache: bool = False
    chunk_pages: int = 4
    prefetch_chunks: int = 1
    llm_max_concurrency: int = 1
    llm_min_request_interval: float = 0.0
    batch_workers: int = 1
    image_dpi: int = 160
    image_dpi_min: int = 100
    image_dpi_max: int | None = None
    image_format: str = "png"
    jpeg_quality: int = 85
    llm_retries: int = 2
    llm_retry_initial_delay: float = 2.0
    llm_retry_max_delay: float = 30.0
    beamer_box_style: str = "block"
    ctex_font_profile: str = "default"

    def __post_init__(self) -> None:
        output_kind = self.conversion_mode or self.output_kind
        if isinstance(output_kind, str):
            output_kind = GuiOutputKind(output_kind)
        api_key_source = self.api_key_source
        if isinstance(api_key_source, str):
            api_key_source = GuiApiKeySource(api_key_source)
        object.__setattr__(self, "output_kind", output_kind)
        object.__setattr__(self, "conversion_mode", output_kind)
        object.__setattr__(self, "api_key_source", api_key_source)


def validate_gui_settings(
    settings: GuiConversionSettings,
    *,
    language: GuiLanguage | str = GuiLanguage.zh_CN,
) -> list[str]:
    """Return localized validation errors for GUI settings."""
    errors: list[str] = []

    if settings.output_kind not in {item.value for item in GuiOutputKind}:
        errors.append(tr(language, "error.invalid_output_kind"))
    if (
        settings.path_state.input_selection.kind == GuiInputKind.directory
        and not settings.batch_pattern.strip()
    ):
        errors.append(tr(language, "error.empty_batch_pattern"))
    if settings.document_class not in {
        "auto",
        "article",
        "book",
        "beamer",
        "ctexart",
        "ctexbook",
        "ctexbeamer",
    }:
        errors.append(tr(language, "error.invalid_document_class"))
    if settings.structure_mode not in {"auto", "off", "local", "llm"}:
        errors.append(tr(language, "error.invalid_structure_mode"))
    if settings.title_source not in {"filename", "llm"}:
        errors.append(tr(language, "error.invalid_title_source"))
    api_key_source_value = (
        settings.api_key_source.value
        if isinstance(settings.api_key_source, GuiApiKeySource)
        else str(settings.api_key_source)
    )
    if api_key_source_value not in {item.value for item in GuiApiKeySource}:
        errors.append(tr(language, "error.invalid_api_key_source"))
    if api_key_source_value == GuiApiKeySource.environment.value:
        env_name = settings.api_key.strip()
        if not env_name:
            errors.append(tr(language, "error.empty_api_key_env"))
        elif env_name not in os.environ:
            errors.append(tr(language, "error.missing_api_key_env", env_name=env_name))
    if settings.image_format not in {"auto", "png", "jpeg"}:
        errors.append(tr(language, "error.invalid_image_format"))
    if settings.beamer_box_style not in {"block", "tcolorbox"}:
        errors.append(tr(language, "error.invalid_beamer_box_style"))
    if settings.ctex_font_profile not in {"default", "local"}:
        errors.append(tr(language, "error.invalid_ctex_font_profile"))
    if parse_gui_pages(settings.pages) is None and settings.pages.strip():
        errors.append(tr(language, "error.invalid_pages"))
    if settings.manual_title.strip() and settings.title_source == "llm":
        errors.append(tr(language, "error.manual_title_with_llm"))
    if settings.image_dpi_min <= 0:
        errors.append(tr(language, "error.image_dpi_min_positive"))
    if settings.image_dpi_max is not None and settings.image_dpi_max <= 0:
        errors.append(tr(language, "error.image_dpi_max_positive"))
    if settings.image_dpi_max is not None and settings.image_dpi_min > settings.image_dpi_max:
        errors.append(tr(language, "error.image_dpi_range"))
    if not 1 <= settings.jpeg_quality <= 100:
        errors.append(tr(language, "error.jpeg_quality_range"))
    if settings.timeout_seconds is not None and settings.timeout_seconds <= 0:
        errors.append(tr(language, "error.timeout_positive"))
    if settings.llm_retry_max_delay < settings.llm_retry_initial_delay:
        errors.append(tr(language, "error.retry_delay_order"))
    if settings.chunk_pages <= 0:
        errors.append(tr(language, "error.chunk_pages_positive"))
    if settings.structure_chunk_pages <= 0:
        errors.append(tr(language, "error.structure_chunk_pages_positive"))
    if settings.structure_max_pages <= 0:
        errors.append(tr(language, "error.structure_max_pages_positive"))
    if settings.max_tokens <= 0:
        errors.append(tr(language, "error.max_tokens_positive"))
    if settings.prefetch_chunks < 0:
        errors.append(tr(language, "error.prefetch_non_negative"))
    if settings.llm_retries < 0:
        errors.append(tr(language, "error.llm_retries_non_negative"))
    if settings.llm_max_concurrency <= 0:
        errors.append(tr(language, "error.llm_concurrency_positive"))
    if settings.llm_min_request_interval < 0:
        errors.append(tr(language, "error.request_interval_non_negative"))
    if settings.batch_workers <= 0:
        errors.append(tr(language, "error.batch_workers_positive"))
    if settings.clear_cache and not settings.cache_enabled:
        errors.append(tr(language, "error.clear_cache_requires_cache"))
    if settings.temperature < 0:
        errors.append(tr(language, "error.temperature_non_negative"))

    return errors


def parse_gui_pages(pages: str) -> list[int] | None:
    """Parse GUI page ranges into unique 1-based page numbers."""
    text = pages.strip()
    if not text:
        return None

    resolved: list[int] = []
    try:
        for chunk in text.split(","):
            item = chunk.strip()
            if not item:
                continue
            if "-" in item:
                start_text, end_text = item.split("-", 1)
                start = int(start_text.strip())
                end = int(end_text.strip())
                if start <= 0 or end <= 0 or end < start:
                    return None
                resolved.extend(range(start, end + 1))
            else:
                page = int(item)
                if page <= 0:
                    return None
                resolved.append(page)
    except ValueError:
        return None

    if not resolved:
        return None

    seen: set[int] = set()
    unique_pages: list[int] = []
    for page in resolved:
        if page in seen:
            continue
        seen.add(page)
        unique_pages.append(page)
    return unique_pages


def _join_path_text(directory: str, *parts: str) -> str:
    base = directory.rstrip("/\\")
    separator = "\\" if "\\" in directory and "/" not in directory else "/"
    return separator.join((base, *parts)) if base else separator.join(parts)
