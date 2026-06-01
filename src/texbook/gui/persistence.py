"""GUI settings persistence and path memory."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import PurePath, PurePosixPath, PureWindowsPath

from PySide6.QtCore import QSettings, QStandardPaths

from texbook.gui.display import (
    GuiDisplayPreferences,
    coerce_font_point_size,
    coerce_language,
    coerce_theme_mode,
)
from texbook.gui.resources import APP_DISPLAY_NAME, APP_ORGANIZATION_NAME
from texbook.gui.selection import GuiInputKind, GuiInputSelection, GuiPathSelectionState
from texbook.gui.settings import (
    GuiApiKeySource,
    GuiConversionSettings,
    GuiOutputKind,
    default_gui_log_file_path,
)


SCHEMA_VERSION = 2


@dataclass(frozen=True)
class GuiPathMemory:
    """Recently used directories for GUI file dialogs."""

    last_input_directory: str = ""
    last_output_directory: str = ""
    last_cache_directory: str = ""
    last_log_file_path: str = ""

    def remember_input_selection(self, selection: GuiInputSelection) -> "GuiPathMemory":
        if not selection.paths:
            return self
        if selection.kind == GuiInputKind.directory:
            directory = selection.paths[0]
        else:
            directory = parent_directory_text(selection.paths[0])
        if not directory:
            return self
        return replace(self, last_input_directory=directory)

    def remember_output_path(self, path: str, *, is_file: bool) -> "GuiPathMemory":
        directory = parent_directory_text(path) if is_file else path.strip()
        if not directory:
            return self
        return replace(self, last_output_directory=directory)

    def remember_cache_directory(self, directory: str) -> "GuiPathMemory":
        directory = directory.strip()
        if not directory:
            return self
        return replace(self, last_cache_directory=directory)

    def remember_log_file_path(self, path: str) -> "GuiPathMemory":
        path = path.strip()
        if not path:
            return self
        return replace(self, last_log_file_path=path)


@dataclass(frozen=True)
class GuiPersistentState:
    """Persistent GUI state restored on application startup."""

    settings: GuiConversionSettings
    path_memory: GuiPathMemory
    display_preferences: GuiDisplayPreferences = field(default_factory=GuiDisplayPreferences)


class GuiSettingsStore:
    """Read and write GUI state through Qt QSettings."""

    def __init__(
        self,
        settings: QSettings | None = None,
        *,
        default_directory_provider: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings or QSettings(APP_ORGANIZATION_NAME, APP_DISPLAY_NAME)
        self._default_directory_provider = default_directory_provider or system_default_dialog_directory

    def load_state(self) -> GuiPersistentState:
        """Load stored GUI settings with safe defaults for missing or invalid values."""
        return GuiPersistentState(
            settings=self.load_conversion_settings(),
            path_memory=self.load_path_memory(),
            display_preferences=self.load_display_preferences(),
        )

    def save_state(self, state: GuiPersistentState) -> None:
        """Persist GUI settings and path memory."""
        self.save_conversion_settings(state.settings)
        self.save_path_memory(state.path_memory)
        self.save_display_preferences(state.display_preferences)

    def load_conversion_settings(self) -> GuiConversionSettings:
        """Load conversion settings without restoring active path selections."""
        defaults = GuiConversionSettings()
        output_kind = _output_kind_value(
            self._read_str("conversion/output_kind", defaults.output_kind.value),
            defaults.output_kind,
        )
        api_key_source = _api_key_source_value(
            self._read_str("model/api_key_source", defaults.api_key_source.value),
            defaults.api_key_source,
        )
        return GuiConversionSettings(
            path_state=GuiPathSelectionState(),
            output_kind=output_kind,
            confirm_overwrite=self._read_bool(
                "conversion/confirm_overwrite",
                defaults.confirm_overwrite,
            ),
            batch_pattern=self._read_str("input/batch_pattern", defaults.batch_pattern),
            pages=self._read_str("document/pages", defaults.pages),
            document_class=_choice_value(
                self._read_str("document/document_class", defaults.document_class),
                defaults.document_class,
                {"auto", "article", "book", "beamer", "ctexart", "ctexbook", "ctexbeamer"},
            ),
            structure_mode=_choice_value(
                self._read_str("document/structure_mode", defaults.structure_mode),
                defaults.structure_mode,
                {"auto", "off", "local", "llm"},
            ),
            structure_chunk_pages=self._read_int(
                "document/structure_chunk_pages",
                defaults.structure_chunk_pages,
            ),
            structure_max_pages=self._read_int(
                "document/structure_max_pages",
                defaults.structure_max_pages,
            ),
            manual_title=self._read_str("document/manual_title", defaults.manual_title),
            title_source=_choice_value(
                self._read_str("document/title_source", defaults.title_source),
                defaults.title_source,
                {"filename", "llm"},
            ),
            show_date=self._read_bool("document/show_date", defaults.show_date),
            beamer_title_page=self._read_bool(
                "document/beamer_title_page",
                defaults.beamer_title_page,
            ),
            model=self._read_str("model/model", defaults.model),
            base_url=self._read_str("model/base_url", defaults.base_url),
            api_key=self._read_str("model/api_key", defaults.api_key),
            api_key_source=api_key_source,
            prompt_preset=defaults.prompt_preset,
            extra_prompt=self._read_str("model/extra_prompt", defaults.extra_prompt),
            temperature=self._read_float("model/temperature", defaults.temperature),
            timeout_seconds=self._read_optional_float(
                "model/timeout_seconds",
                defaults.timeout_seconds,
            ),
            max_tokens=self._read_int("model/max_tokens", defaults.max_tokens),
            cache_enabled=self._read_bool("cache/enabled", defaults.cache_enabled),
            cache_directory=self._read_str("cache/directory", defaults.cache_directory),
            clear_cache=False,
            chunk_pages=self._read_int("runtime/chunk_pages", defaults.chunk_pages),
            prefetch_chunks=self._read_int("runtime/prefetch_chunks", defaults.prefetch_chunks),
            llm_max_concurrency=self._read_int(
                "runtime/llm_max_concurrency",
                defaults.llm_max_concurrency,
            ),
            llm_min_request_interval=self._read_float(
                "runtime/llm_min_request_interval",
                defaults.llm_min_request_interval,
            ),
            batch_workers=self._read_int("runtime/batch_workers", defaults.batch_workers),
            image_dpi=self._read_int("image/dpi", defaults.image_dpi),
            image_dpi_min=self._read_int("image/dpi_min", defaults.image_dpi_min),
            image_dpi_max=self._read_optional_int("image/dpi_max", defaults.image_dpi_max),
            image_format=_choice_value(
                self._read_str("image/format", defaults.image_format),
                defaults.image_format,
                {"auto", "png", "jpeg"},
            ),
            jpeg_quality=self._read_int("image/jpeg_quality", defaults.jpeg_quality),
            llm_retries=self._read_int("retry/retries", defaults.llm_retries),
            llm_retry_initial_delay=self._read_float(
                "retry/initial_delay",
                defaults.llm_retry_initial_delay,
            ),
            llm_retry_max_delay=self._read_float("retry/max_delay", defaults.llm_retry_max_delay),
            beamer_box_style=_choice_value(
                self._read_str("latex/beamer_box_style", defaults.beamer_box_style),
                defaults.beamer_box_style,
                {"block", "tcolorbox"},
            ),
            ctex_font_profile=_choice_value(
                self._read_str("latex/ctex_font_profile", defaults.ctex_font_profile),
                defaults.ctex_font_profile,
                {"default", "local"},
            ),
        )

    def save_conversion_settings(self, settings: GuiConversionSettings) -> None:
        """Save conversion settings and intentionally skip runtime-only state."""
        self._settings.setValue("schema_version", SCHEMA_VERSION)
        self._settings.setValue("conversion/output_kind", settings.output_kind.value)
        self._settings.setValue("conversion/confirm_overwrite", settings.confirm_overwrite)
        self._settings.setValue("input/batch_pattern", settings.batch_pattern)
        self._settings.setValue("document/pages", settings.pages)
        self._settings.setValue("document/document_class", settings.document_class)
        self._settings.setValue("document/structure_mode", settings.structure_mode)
        self._settings.setValue("document/structure_chunk_pages", settings.structure_chunk_pages)
        self._settings.setValue("document/structure_max_pages", settings.structure_max_pages)
        self._settings.setValue("document/manual_title", settings.manual_title)
        self._settings.setValue("document/title_source", settings.title_source)
        self._settings.setValue("document/show_date", settings.show_date)
        self._settings.setValue("document/beamer_title_page", settings.beamer_title_page)
        self._settings.setValue("model/model", settings.model)
        self._settings.setValue("model/base_url", settings.base_url)
        self._settings.setValue("model/api_key", settings.api_key)
        self._settings.setValue("model/api_key_source", settings.api_key_source.value)
        self._settings.setValue("model/extra_prompt", settings.extra_prompt)
        self._settings.setValue("model/temperature", settings.temperature)
        self._settings.setValue(
            "model/timeout_seconds",
            "" if settings.timeout_seconds is None else settings.timeout_seconds,
        )
        self._settings.setValue("model/max_tokens", settings.max_tokens)
        self._settings.setValue("cache/enabled", settings.cache_enabled)
        self._settings.setValue("cache/directory", settings.cache_directory)
        self._settings.setValue("runtime/chunk_pages", settings.chunk_pages)
        self._settings.setValue("runtime/prefetch_chunks", settings.prefetch_chunks)
        self._settings.setValue("runtime/llm_max_concurrency", settings.llm_max_concurrency)
        self._settings.setValue(
            "runtime/llm_min_request_interval",
            settings.llm_min_request_interval,
        )
        self._settings.setValue("runtime/batch_workers", settings.batch_workers)
        self._settings.setValue("image/dpi", settings.image_dpi)
        self._settings.setValue("image/dpi_min", settings.image_dpi_min)
        self._settings.setValue(
            "image/dpi_max",
            "" if settings.image_dpi_max is None else settings.image_dpi_max,
        )
        self._settings.setValue("image/format", settings.image_format)
        self._settings.setValue("image/jpeg_quality", settings.jpeg_quality)
        self._settings.setValue("retry/retries", settings.llm_retries)
        self._settings.setValue("retry/initial_delay", settings.llm_retry_initial_delay)
        self._settings.setValue("retry/max_delay", settings.llm_retry_max_delay)
        self._settings.setValue("latex/beamer_box_style", settings.beamer_box_style)
        self._settings.setValue("latex/ctex_font_profile", settings.ctex_font_profile)
        self._settings.sync()

    def load_path_memory(self) -> GuiPathMemory:
        """Load recently used dialog locations."""
        return GuiPathMemory(
            last_input_directory=self._read_str("paths/last_input_directory", ""),
            last_output_directory=self._read_str("paths/last_output_directory", ""),
            last_cache_directory=self._read_str("paths/last_cache_directory", ""),
            last_log_file_path=self._read_str(
                "paths/last_log_file_path",
                default_gui_log_file_path(),
            ),
        )

    def save_path_memory(self, path_memory: GuiPathMemory) -> None:
        """Save recently used dialog locations."""
        self._settings.setValue("paths/last_input_directory", path_memory.last_input_directory)
        self._settings.setValue("paths/last_output_directory", path_memory.last_output_directory)
        self._settings.setValue("paths/last_cache_directory", path_memory.last_cache_directory)
        self._settings.setValue("paths/last_log_file_path", path_memory.last_log_file_path)
        self._settings.sync()

    def load_display_preferences(self) -> GuiDisplayPreferences:
        """Load persisted theme and language preferences."""
        defaults = GuiDisplayPreferences()
        return GuiDisplayPreferences(
            theme=coerce_theme_mode(
                self._read_str("display/theme", defaults.theme.value),
                defaults.theme,
            ),
            language=coerce_language(
                self._read_str("display/language", defaults.language.value),
                defaults.language,
            ),
            font_point_size=self._read_font_point_size(
                "display/font_point_size",
                defaults.font_point_size,
            ),
        )

    def save_display_preferences(self, preferences: GuiDisplayPreferences) -> None:
        """Save theme and language preferences."""
        self._settings.setValue("schema_version", SCHEMA_VERSION)
        self._settings.setValue("display/theme", preferences.theme.value)
        self._settings.setValue("display/language", preferences.language.value)
        self._settings.setValue("display/font_point_size", preferences.font_point_size)
        self._settings.sync()

    def default_dialog_directory(self) -> str:
        """Return the default directory for first-time GUI file dialogs."""
        return self._default_directory_provider()

    def _read_str(self, key: str, default: str) -> str:
        value = self._settings.value(key, default)
        if value is None:
            return default
        return str(value)

    def _read_bool(self, key: str, default: bool) -> bool:
        return _coerce_bool(self._settings.value(key, default), default)

    def _read_int(self, key: str, default: int) -> int:
        return _coerce_int(self._settings.value(key, default), default)

    def _read_optional_int(self, key: str, default: int | None) -> int | None:
        value = self._settings.value(key, "" if default is None else default)
        if value in {None, ""}:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _read_float(self, key: str, default: float) -> float:
        return _coerce_float(self._settings.value(key, default), default)

    def _read_optional_float(self, key: str, default: float | None) -> float | None:
        value = self._settings.value(key, "" if default is None else default)
        if value in {None, ""}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _read_font_point_size(self, key: str, default: int) -> int:
        value = self._settings.value(key, default)
        try:
            point_size = int(value)
        except (TypeError, ValueError):
            return default
        coerced = coerce_font_point_size(point_size, default)
        return coerced if coerced == point_size else default


def system_default_dialog_directory() -> str:
    """Return a stable user directory for first-time file dialogs."""
    for location in (
        QStandardPaths.StandardLocation.DocumentsLocation,
        QStandardPaths.StandardLocation.HomeLocation,
    ):
        directory = QStandardPaths.writableLocation(location)
        if directory:
            return directory
    return ""


def parent_directory_text(path: str) -> str:
    """Return a parent directory string while preserving Windows-style paths."""
    text = path.strip()
    if not text:
        return ""
    parent = _pure_path(text).parent
    if str(parent) in {"", "."}:
        return ""
    return str(parent)


def join_dialog_path(directory: str, filename: str) -> str:
    """Join a dialog directory and file name without changing path flavor."""
    if not directory:
        return filename
    if _looks_windows_path(directory):
        return str(PureWindowsPath(directory) / filename)
    return str(PurePosixPath(directory) / filename)


def _pure_path(path: str) -> PurePath:
    if _looks_windows_path(path):
        return PureWindowsPath(path)
    return PurePosixPath(path)


def _looks_windows_path(path: str) -> bool:
    return "\\" in path or ":" in path


def _output_kind_value(raw: str, default: GuiOutputKind) -> GuiOutputKind:
    try:
        return GuiOutputKind(raw)
    except ValueError:
        return default


def _api_key_source_value(raw: str, default: GuiApiKeySource) -> GuiApiKeySource:
    try:
        return GuiApiKeySource(raw)
    except ValueError:
        return default


def _choice_value(raw: str, default: str, allowed: set[str]) -> str:
    return raw if raw in allowed else default


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
