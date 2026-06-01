"""Main conversion panel for the TexBook GUI."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QAbstractSpinBox,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from texbook.diagnostics import DiagnosticLog
from texbook.gui.executor import GuiOverwriteConfirmationRequest, GuiTaskExecutor
from texbook.gui.display import (
    DEFAULT_GUI_FONT_POINT_SIZE,
    GuiDisplayPreferences,
    GuiLanguage,
    GuiThemeMode,
    build_gui_font,
)
from texbook.gui.dialogs import AboutDialog, SettingsDialog
from texbook.gui.i18n import tr
from texbook.gui.persistence import (
    GuiPathMemory,
    join_dialog_path,
    system_default_dialog_directory,
)
from texbook.gui.resources import APP_DISPLAY_NAME
from texbook.gui.selection import (
    GuiInputKind,
    GuiInputSelection,
    GuiPathSelectionState,
)
from texbook.gui.settings import (
    GuiApiKeySource,
    GuiConversionMode,
    GuiOutputKind,
    GuiConversionSettings,
    default_gui_log_file_path,
    validate_gui_settings,
)
from texbook.gui.tasks import (
    GuiTaskCreationError,
    GuiTaskRuntimeUpdate,
    GuiTaskStatus,
    GuiTaskViewState,
    TERMINAL_TASK_STATUSES,
    apply_progress_event,
    apply_task_update,
    create_conversion_tasks,
    create_task_view_state,
    mark_task_canceling,
    mark_task_canceled,
    mark_task_completed,
    mark_task_failed,
    mark_task_running,
    task_recent_event_text,
    task_stage_label,
    task_status_label,
)
from texbook.gui.widgets import (
    ChoiceGrid,
    FocusWheelDoubleSpinBox,
    FocusWheelSpinBox,
    InlineField,
    MetricPill,
    OptionGrid,
    SectionPanel,
)
from texbook.gui.theme import build_fluent_stylesheet
from texbook.llm.scheduler import ProgressEvent
from texbook.llm.presets import DEFAULT_PROMPT_PRESET_NAME


_INPUT_KIND_ITEMS = (
    (GuiInputKind.single_file.value, "option.input.single_file"),
    (GuiInputKind.multiple_files.value, "option.input.multiple_files"),
    (GuiInputKind.directory.value, "option.input.directory"),
)

_OUTPUT_KIND_ITEMS = (
    (GuiOutputKind.tex_file.value, "option.output.tex_file"),
    (GuiOutputKind.project.value, "option.output.project"),
)

_API_KEY_SOURCE_ITEMS = (
    (GuiApiKeySource.direct.value, "option.api_key.direct"),
    (GuiApiKeySource.environment.value, "option.api_key.environment"),
)


class ConversionMainPanel(QWidget):
    """Fluent Design main panel shown when the GUI opens."""

    display_preferences_changed = Signal(object)
    settings_requested = Signal()
    reset_defaults_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        display_preferences: GuiDisplayPreferences | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("conversionMainPanel")
        self._display_preferences = display_preferences or GuiDisplayPreferences()
        self.selection_state = GuiPathSelectionState()
        self.path_memory = GuiPathMemory()
        self.tasks = []
        self._task_states: dict[str, GuiTaskViewState] = {}
        self._task_rows: dict[str, QFrame] = {}
        self._executor: GuiTaskExecutor | None = None
        self._executor_factory = GuiTaskExecutor
        self._diagnostic_log = DiagnosticLog()
        self._sections: dict[str, SectionPanel] = {}
        self._row_labels: dict[str, QLabel] = {}
        self._metric_pills: dict[GuiTaskStatus, MetricPill] = {}
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(14)

        layout.addWidget(self._create_command_bar())

        content = QHBoxLayout()
        content.setSpacing(14)
        content.addWidget(self._create_settings_scroll(), 3)
        content.addWidget(self._create_task_list_panel(), 2)
        layout.addLayout(content, 1)

        self._connect_selection_controls()
        self._apply_theme()
        self._retranslate_ui()
        self._refresh_path_state()

    @property
    def _language(self) -> GuiLanguage:
        return self._display_preferences.language

    @property
    def _theme(self) -> GuiThemeMode:
        return self._display_preferences.theme

    def _tr(self, key: str, **kwargs: object) -> str:
        return tr(self._language, key, **kwargs)

    def _create_command_bar(self) -> QFrame:
        command_bar = QFrame(self)
        command_bar.setObjectName("topCommandBar")
        layout = QHBoxLayout(command_bar)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        title_group = QVBoxLayout()
        title_group.setSpacing(2)

        title = QLabel(APP_DISPLAY_NAME, command_bar)
        title.setObjectName("appTitleLabel")
        title_group.addWidget(title)

        subtitle = QLabel(command_bar)
        subtitle.setObjectName("appSubtitleLabel")
        self.subtitle_label = subtitle
        title_group.addWidget(subtitle)

        layout.addLayout(title_group, 1)
        self.theme_button = self._make_tool_button(
            "themeButton",
            "",
            QStyle.StandardPixmap.SP_DesktopIcon,
        )
        self.theme_button.setEnabled(True)
        layout.addWidget(self.theme_button)
        self.language_button = self._make_tool_button(
            "languageButton",
            "",
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
        )
        self.language_button.setEnabled(True)
        layout.addWidget(self.language_button)

        self.settings_button = self._make_tool_button(
            "settingsButton",
            "",
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
        )
        self.settings_button.setEnabled(True)
        layout.addWidget(self.settings_button)

        self.reset_defaults_button = self._make_tool_button(
            "resetDefaultsButton",
            "",
            QStyle.StandardPixmap.SP_BrowserReload,
        )
        self.reset_defaults_button.setEnabled(True)
        layout.addWidget(self.reset_defaults_button)

        add_task = QPushButton(command_bar)
        add_task.setObjectName("addTaskButton")
        add_task.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder))
        add_task.setEnabled(False)
        self.add_task_button = add_task
        layout.addWidget(add_task)

        start = QPushButton(command_bar)
        start.setObjectName("startButton")
        start.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        start.setEnabled(False)
        self.start_button = start
        layout.addWidget(start)

        return command_bar

    def _create_settings_scroll(self) -> QScrollArea:
        scroll = QScrollArea(self)
        scroll.setObjectName("settingsScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setObjectName("settingsViewport")

        pane = QWidget(scroll)
        pane.setObjectName("settingsPane")
        pane.setMinimumWidth(560)
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        layout.addWidget(self._create_input_panel())
        layout.addWidget(self._create_output_panel())
        parameters = QWidget(pane)
        parameters.setObjectName("parametersPanel")
        parameters_layout = QVBoxLayout(parameters)
        parameters_layout.setContentsMargins(0, 0, 0, 0)
        parameters_layout.setSpacing(12)
        parameters_layout.addWidget(self._create_page_document_panel())
        parameters_layout.addWidget(self._create_document_panel())
        parameters_layout.addWidget(self._create_model_panel())
        parameters_layout.addWidget(self._create_cache_panel())
        parameters_layout.addWidget(self._create_log_panel())
        parameters_layout.addWidget(self._create_advanced_panel())
        layout.addWidget(parameters)

        layout.addStretch(1)
        scroll.setWidget(pane)
        return scroll

    def _register_section(self, key: str, panel: SectionPanel) -> SectionPanel:
        self._sections[key] = panel
        return panel

    def _add_grid_row(
        self,
        grid: OptionGrid,
        key: str,
        control: QWidget,
    ) -> None:
        self._row_labels[key] = grid.add_row("", control)

    def _set_choice_items(
        self,
        choices: ChoiceGrid,
        items: tuple[tuple[str, str], ...],
        current_value: str | None = None,
    ) -> None:
        value = current_value if current_value is not None else choices.value()
        blocker = QSignalBlocker(choices)
        choices.set_items(
            tuple((item_value, self._tr(label_key)) for item_value, label_key in items),
            current_value=value,
        )
        del blocker

    def _set_plain_choice_items(
        self,
        choices: ChoiceGrid,
        values: tuple[str, ...],
        current_value: str | None = None,
    ) -> None:
        value = current_value if current_value is not None else choices.value()
        blocker = QSignalBlocker(choices)
        choices.set_items(tuple((item, item) for item in values), current_value=value)
        del blocker

    def _create_input_panel(self) -> SectionPanel:
        panel = self._register_section(
            "section.input",
            SectionPanel("", object_name="inputPanel", parent=self),
        )
        grid = OptionGrid(parent=panel)

        pdf_input = QLineEdit(panel)
        pdf_input.setObjectName("pdfInputField")
        pdf_input.setReadOnly(True)
        self.pdf_input_field = pdf_input
        browse = self._make_icon_button(
            "pdfInputBrowseButton",
            QStyle.StandardPixmap.SP_DialogOpenButton,
            "",
        )
        self.pdf_input_browse_button = browse
        self._add_grid_row(grid, "field.pdf_input", InlineField(pdf_input, browse, parent=panel))

        input_type = ChoiceGrid(panel)
        input_type.setObjectName("inputTypeChoices")
        self._set_choice_items(input_type, _INPUT_KIND_ITEMS, GuiInputKind.single_file.value)
        self.input_type_choices = input_type
        self._add_grid_row(grid, "field.input_type", input_type)

        batch_pattern = QLineEdit(panel)
        batch_pattern.setObjectName("batchPatternField")
        batch_pattern.setPlaceholderText("*.pdf")
        batch_pattern.setText("*.pdf")
        self.batch_pattern_field = batch_pattern
        self._add_grid_row(grid, "field.batch_pattern", batch_pattern)

        panel.body_layout.addWidget(grid)
        return panel

    def _create_output_panel(self) -> SectionPanel:
        panel = self._register_section(
            "section.output",
            SectionPanel("", object_name="outputPanel", parent=self),
        )
        grid = OptionGrid(parent=panel)

        output_kind = ChoiceGrid(panel)
        output_kind.setObjectName("outputKindChoices")
        self._set_choice_items(output_kind, _OUTPUT_KIND_ITEMS, GuiOutputKind.tex_file.value)
        self.output_kind_choices = output_kind
        self._add_grid_row(grid, "field.output_kind", output_kind)

        output_dir = QLineEdit(panel)
        output_dir.setObjectName("outputDirectoryField")
        output_dir.setReadOnly(True)
        self.output_directory_field = output_dir
        browse = self._make_icon_button(
            "outputBrowseButton",
            QStyle.StandardPixmap.SP_DirOpenIcon,
            "",
        )
        self.output_browse_button = browse
        self._add_grid_row(grid, "field.output_target", InlineField(output_dir, browse, parent=panel))

        overwrite = QCheckBox(panel)
        overwrite.setObjectName("confirmOverwriteCheckBox")
        overwrite.setChecked(True)
        self.confirm_overwrite_checkbox = overwrite
        self._add_grid_row(grid, "field.write_policy", overwrite)

        panel.body_layout.addWidget(grid)
        return panel

    def _create_page_document_panel(self) -> SectionPanel:
        panel = self._register_section(
            "section.page",
            SectionPanel("", object_name="pageOptionsPanel", parent=self),
        )
        grid = OptionGrid(parent=panel)

        pages = QLineEdit(panel)
        pages.setObjectName("pagesField")
        self.pages_field = pages
        self._add_grid_row(grid, "field.pages", pages)

        manual_title = QLineEdit(panel)
        manual_title.setObjectName("manualTitleField")
        self.manual_title_field = manual_title
        self._add_grid_row(grid, "field.manual_title", manual_title)

        title_source = ChoiceGrid(panel)
        title_source.setObjectName("titleSourceChoices")
        self._set_plain_choice_items(title_source, ("filename", "llm"), "filename")
        self.title_source_choices = title_source
        self._add_grid_row(grid, "field.title_source", title_source)

        show_date = QCheckBox(panel)
        show_date.setObjectName("showDateCheckBox")
        self.show_date_checkbox = show_date
        self._add_grid_row(grid, "field.date", show_date)

        panel.body_layout.addWidget(grid)
        return panel

    def _create_document_panel(self) -> SectionPanel:
        panel = self._register_section(
            "section.document",
            SectionPanel("", object_name="documentOptionsPanel", parent=self),
        )
        grid = OptionGrid(parent=panel)

        document_class = ChoiceGrid(panel)
        document_class.setObjectName("documentClassChoices")
        self._set_plain_choice_items(
            document_class,
            ("auto", "ctexart", "ctexbook", "ctexbeamer", "article", "book", "beamer"),
            "auto",
        )
        self.document_class_choices = document_class
        self._add_grid_row(grid, "field.document_class", document_class)

        structure = ChoiceGrid(panel)
        structure.setObjectName("structureModeChoices")
        self._set_plain_choice_items(structure, ("auto", "off", "local", "llm"), "auto")
        self.structure_mode_choices = structure
        self._add_grid_row(grid, "field.structure", structure)

        structure_chunk_pages = self._make_spin_box("structureChunkPagesSpinBox", 1, 128, 8)
        self.structure_chunk_pages_spin_box = structure_chunk_pages
        self._add_grid_row(grid, "field.structure_chunk_pages", structure_chunk_pages)

        structure_max_pages = self._make_spin_box("structureMaxPagesSpinBox", 1, 4096, 32)
        self.structure_max_pages_spin_box = structure_max_pages
        self._add_grid_row(grid, "field.structure_max_pages", structure_max_pages)

        title_page = QCheckBox(panel)
        title_page.setObjectName("beamerTitlePageCheckBox")
        title_page.setChecked(True)
        self.beamer_title_page_checkbox = title_page
        self._add_grid_row(grid, "field.title_page", title_page)

        panel.body_layout.addWidget(grid)
        return panel

    def _create_model_panel(self) -> SectionPanel:
        panel = self._register_section(
            "section.model",
            SectionPanel("", object_name="modelOptionsPanel", parent=self),
        )
        grid = OptionGrid(parent=panel)

        model = QLineEdit(panel)
        model.setObjectName("modelField")
        model.setPlaceholderText("TEXBOOK_MODEL")
        self.model_field = model
        self._add_grid_row(grid, "field.model", model)

        base_url = QLineEdit(panel)
        base_url.setObjectName("baseUrlField")
        base_url.setPlaceholderText("TEXBOOK_BASE_URL")
        self.base_url_field = base_url
        grid.add_row("Base URL", base_url)

        api_key = QLineEdit(panel)
        api_key.setObjectName("apiKeyField")
        api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_field = api_key

        api_key_source = ChoiceGrid(panel)
        api_key_source.setObjectName("apiKeySourceChoices")
        self._set_choice_items(api_key_source, _API_KEY_SOURCE_ITEMS, GuiApiKeySource.direct.value)
        self.api_key_source_choices = api_key_source
        self._add_grid_row(grid, "field.key_source", api_key_source)
        grid.add_row("API Key", api_key)

        preset = QLineEdit(panel)
        preset.setObjectName("promptPresetField")
        preset.setText(DEFAULT_PROMPT_PRESET_NAME)
        preset.setReadOnly(True)
        self.prompt_preset_field = preset
        self._add_grid_row(grid, "field.prompt_preset", preset)

        extra_prompt = QTextEdit(panel)
        extra_prompt.setObjectName("extraPromptEdit")
        self.extra_prompt_edit = extra_prompt
        self._add_grid_row(grid, "field.extra_prompt", extra_prompt)

        panel.body_layout.addWidget(grid)
        return panel

    def _create_cache_panel(self) -> SectionPanel:
        panel = self._register_section(
            "section.cache",
            SectionPanel("", object_name="cacheConcurrencyPanel", parent=self),
        )
        grid = OptionGrid(parent=panel)

        cache_enabled = QCheckBox(panel)
        cache_enabled.setObjectName("cacheEnabledCheckBox")
        cache_enabled.setChecked(True)
        self.cache_enabled_checkbox = cache_enabled
        self._add_grid_row(grid, "field.cache", cache_enabled)

        default_cache_directory = GuiConversionSettings().cache_directory
        cache_dir = QLineEdit(panel)
        cache_dir.setObjectName("cacheDirectoryField")
        cache_dir.setPlaceholderText(default_cache_directory)
        cache_dir.setText(default_cache_directory)
        self.cache_directory_field = cache_dir
        browse = self._make_icon_button(
            "cacheBrowseButton",
            QStyle.StandardPixmap.SP_DirOpenIcon,
            "",
        )
        self.cache_browse_button = browse
        self._add_grid_row(grid, "field.cache_directory", InlineField(cache_dir, browse, parent=panel))

        clear_cache = QCheckBox(panel)
        clear_cache.setObjectName("clearCacheCheckBox")
        self.clear_cache_checkbox = clear_cache
        self._add_grid_row(grid, "field.clear_cache", clear_cache)

        chunk_pages = self._make_spin_box("chunkPagesSpinBox", 1, 64, 4)
        self.chunk_pages_spin_box = chunk_pages
        self._add_grid_row(grid, "field.chunk_pages", chunk_pages)

        prefetch = self._make_spin_box("prefetchChunksSpinBox", 0, 16, 1)
        self.prefetch_chunks_spin_box = prefetch
        self._add_grid_row(grid, "field.prefetch", prefetch)

        llm_concurrency = self._make_spin_box("llmConcurrencySpinBox", 1, 16, 1)
        self.llm_concurrency_spin_box = llm_concurrency
        self._add_grid_row(grid, "field.llm_concurrency", llm_concurrency)

        llm_interval = FocusWheelDoubleSpinBox(panel)
        llm_interval.setObjectName("llmIntervalSpinBox")
        llm_interval.setRange(0.0, 600.0)
        llm_interval.setDecimals(1)
        llm_interval.setSingleStep(0.5)
        llm_interval.setValue(0.0)
        llm_interval.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self.llm_min_request_interval_spin_box = llm_interval
        self._add_grid_row(grid, "field.request_interval", llm_interval)

        batch_workers = self._make_spin_box("batchWorkersSpinBox", 1, 16, 1)
        self.batch_workers_spin_box = batch_workers
        self._add_grid_row(grid, "field.batch_workers", batch_workers)

        panel.body_layout.addWidget(grid)
        return panel

    def _create_log_panel(self) -> SectionPanel:
        panel = self._register_section(
            "section.log",
            SectionPanel("", object_name="diagnosticLogPanel", parent=self),
        )
        grid = OptionGrid(parent=panel)

        log_path = QLineEdit(panel)
        log_path.setObjectName("logFilePathField")
        log_path.setReadOnly(True)
        log_path.setText(default_gui_log_file_path())
        self.log_file_path_field = log_path
        browse = self._make_icon_button(
            "logFileBrowseButton",
            QStyle.StandardPixmap.SP_DialogSaveButton,
            "",
        )
        self.log_file_browse_button = browse
        self._add_grid_row(grid, "field.log_file", InlineField(log_path, browse, parent=panel))

        panel.body_layout.addWidget(grid)
        return panel

    def _create_advanced_panel(self) -> SectionPanel:
        panel = self._register_section(
            "section.advanced",
            SectionPanel("", object_name="advancedOptionsPanel", parent=self),
        )
        grid = OptionGrid(parent=panel)

        image_dpi = self._make_spin_box("imageDpiSpinBox", 72, 600, 160)
        self.image_dpi_spin_box = image_dpi
        self._add_grid_row(grid, "field.image_dpi", image_dpi)

        image_dpi_min = self._make_spin_box("imageDpiMinSpinBox", 1, 600, 100)
        self.image_dpi_min_spin_box = image_dpi_min
        self._add_grid_row(grid, "field.image_dpi_min", image_dpi_min)

        image_dpi_max = self._make_spin_box("imageDpiMaxSpinBox", 0, 600, 0)
        self.image_dpi_max_spin_box = image_dpi_max
        self._add_grid_row(grid, "field.image_dpi_max", image_dpi_max)

        image_format = ChoiceGrid(panel)
        image_format.setObjectName("imageFormatChoices")
        self._set_plain_choice_items(image_format, ("auto", "png", "jpeg"), "png")
        self.image_format_choices = image_format
        self._add_grid_row(grid, "field.image_format", image_format)

        jpeg_quality = self._make_spin_box("jpegQualitySpinBox", 1, 100, 85)
        self.jpeg_quality_spin_box = jpeg_quality
        self._add_grid_row(grid, "field.jpeg_quality", jpeg_quality)

        retries = self._make_spin_box("llmRetriesSpinBox", 0, 10, 2)
        self.llm_retries_spin_box = retries
        self._add_grid_row(grid, "field.retries", retries)

        retry_initial_delay = FocusWheelDoubleSpinBox(panel)
        retry_initial_delay.setObjectName("llmRetryInitialDelaySpinBox")
        retry_initial_delay.setRange(0.0, 600.0)
        retry_initial_delay.setDecimals(1)
        retry_initial_delay.setSingleStep(0.5)
        retry_initial_delay.setValue(2.0)
        retry_initial_delay.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self.llm_retry_initial_delay_spin_box = retry_initial_delay
        self._add_grid_row(grid, "field.retry_initial_delay", retry_initial_delay)

        retry_max_delay = FocusWheelDoubleSpinBox(panel)
        retry_max_delay.setObjectName("llmRetryMaxDelaySpinBox")
        retry_max_delay.setRange(0.0, 600.0)
        retry_max_delay.setDecimals(1)
        retry_max_delay.setSingleStep(0.5)
        retry_max_delay.setValue(30.0)
        retry_max_delay.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self.llm_retry_max_delay_spin_box = retry_max_delay
        self._add_grid_row(grid, "field.retry_max_delay", retry_max_delay)

        timeout = FocusWheelDoubleSpinBox(panel)
        timeout.setObjectName("timeoutSpinBox")
        timeout.setRange(0.0, 6000.0)
        timeout.setDecimals(1)
        timeout.setSingleStep(1.0)
        timeout.setValue(0.0)
        timeout.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self.timeout_spin_box = timeout
        self._add_grid_row(grid, "field.timeout", timeout)

        temperature = FocusWheelDoubleSpinBox(panel)
        temperature.setObjectName("temperatureSpinBox")
        temperature.setRange(0.0, 2.0)
        temperature.setDecimals(2)
        temperature.setSingleStep(0.05)
        temperature.setValue(1.0)
        self.temperature_spin_box = temperature
        grid.add_row("Temperature", temperature)

        max_tokens = self._make_spin_box("maxTokensSpinBox", 1, 512000, 128000)
        self.max_tokens_spin_box = max_tokens
        grid.add_row("Max tokens", max_tokens)

        beamer_style = ChoiceGrid(panel)
        beamer_style.setObjectName("beamerBoxStyleChoices")
        self._set_plain_choice_items(beamer_style, ("block", "tcolorbox"), "block")
        self.beamer_box_style_choices = beamer_style
        self._add_grid_row(grid, "field.beamer_box", beamer_style)

        ctex_font = ChoiceGrid(panel)
        ctex_font.setObjectName("ctexFontProfileChoices")
        self._set_plain_choice_items(ctex_font, ("default", "local"), "default")
        self.ctex_font_profile_choices = ctex_font
        self._add_grid_row(grid, "field.ctex_font", ctex_font)

        panel.body_layout.addWidget(grid)
        return panel

    def _create_task_list_panel(self) -> SectionPanel:
        panel = self._register_section(
            "section.tasks",
            SectionPanel("", object_name="taskListPanel", parent=self),
        )
        panel.setMinimumWidth(330)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        metrics = QGridLayout()
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setHorizontalSpacing(8)
        metrics.setVerticalSpacing(8)
        self.task_metric_labels = {}
        for index, (_label, value, key) in enumerate(
            [
                ("待处理", "0", GuiTaskStatus.pending),
                ("运行中", "0", GuiTaskStatus.running),
                ("取消中", "0", GuiTaskStatus.canceling),
                ("已取消", "0", GuiTaskStatus.canceled),
                ("完成", "0", GuiTaskStatus.completed),
                ("失败", "0", GuiTaskStatus.failed),
            ]
        ):
            metric = MetricPill("", value, parent=panel)
            self._metric_pills[key] = metric
            self.task_metric_labels[key] = metric.value_label
            metrics.addWidget(metric, index // 2, index % 2)
        panel.body_layout.addLayout(metrics)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addStretch(1)
        self.clear_tasks_button = self._make_icon_button(
            "clearTasksButton",
            QStyle.StandardPixmap.SP_TrashIcon,
            self._tr("button.clear_tasks.tooltip"),
        )
        actions.addWidget(self.clear_tasks_button)
        panel.body_layout.addLayout(actions)

        body = QWidget(panel)
        body.setObjectName("taskListBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)
        self.task_list_body_layout = body_layout

        empty = QFrame(body)
        empty.setObjectName("taskEmptyState")
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(18, 28, 18, 28)
        empty_layout.setSpacing(8)

        icon = QLabel("+", empty)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setProperty("sectionTitle", True)
        empty_layout.addWidget(icon)

        title = QLabel(empty)
        title.setObjectName("taskEmptyTitleLabel")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setProperty("sectionTitle", True)
        empty_layout.addWidget(title)

        status = QLabel(empty)
        status.setObjectName("taskEmptyStatusLabel")
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status.setProperty("muted", True)
        empty_layout.addWidget(status)
        self.task_empty_state = empty

        body_layout.addWidget(empty)

        scroll = QScrollArea(body)
        scroll.setObjectName("taskListScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVisible(False)
        scroll.viewport().setObjectName("taskListViewport")
        self.task_list_scroll_area = scroll

        rows = QWidget(scroll)
        rows.setObjectName("taskRowsContainer")
        rows_layout = QVBoxLayout(rows)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(8)
        rows_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.task_rows_container = rows
        self.task_rows_layout = rows_layout
        scroll.setWidget(rows)
        body_layout.addWidget(scroll, 1)

        progress = QProgressBar(body)
        progress.setObjectName("overallProgressBar")
        progress.setRange(0, 100)
        progress.setValue(0)
        progress.setTextVisible(False)
        self.overall_progress_bar = progress
        body_layout.addWidget(progress)
        body_layout.addStretch(1)

        panel.body_layout.addWidget(body, 1)
        return panel

    def _make_tool_button(
        self,
        object_name: str,
        text: str,
        standard_pixmap: QStyle.StandardPixmap,
    ) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(object_name)
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setIcon(self.style().standardIcon(standard_pixmap))
        button.setEnabled(False)
        return button

    def _make_icon_button(
        self,
        object_name: str,
        standard_pixmap: QStyle.StandardPixmap,
        tooltip: str,
    ) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(object_name)
        button.setIcon(self.style().standardIcon(standard_pixmap))
        button.setToolTip(tooltip)
        button.setFixedWidth(34)
        return button

    def _make_spin_box(self, object_name: str, minimum: int, maximum: int, value: int) -> QSpinBox:
        spin_box = FocusWheelSpinBox(self)
        spin_box.setObjectName(object_name)
        spin_box.setRange(minimum, maximum)
        spin_box.setValue(value)
        spin_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        return spin_box

    def _connect_selection_controls(self) -> None:
        self.pdf_input_browse_button.clicked.connect(self._browse_pdf_input)
        self.output_browse_button.clicked.connect(self._browse_output_directory)
        self.cache_browse_button.clicked.connect(self._browse_cache_directory)
        self.log_file_browse_button.clicked.connect(self._browse_log_file)
        self.theme_button.clicked.connect(self.toggle_theme)
        self.language_button.clicked.connect(self.toggle_language)
        self.settings_button.clicked.connect(
            lambda _checked=False: self.settings_requested.emit()
        )
        self.reset_defaults_button.clicked.connect(
            lambda _checked=False: self.reset_defaults_requested.emit()
        )
        self.clear_tasks_button.clicked.connect(self.clear_task_queue)
        self.input_type_choices.value_changed.connect(self._change_input_kind)
        self.output_kind_choices.value_changed.connect(self._sync_gui_state)
        self.batch_pattern_field.textChanged.connect(self._sync_gui_state)
        self.add_task_button.clicked.connect(self.add_current_tasks)
        self.start_button.clicked.connect(self.start_pending_tasks)
        self.title_source_choices.value_changed.connect(self._sync_gui_state)
        self.structure_mode_choices.value_changed.connect(self._sync_gui_state)
        self.document_class_choices.value_changed.connect(self._sync_gui_state)
        self.cache_enabled_checkbox.toggled.connect(self._sync_cache_controls)
        self.image_format_choices.value_changed.connect(self._sync_image_controls)
        self.cache_directory_field.textChanged.connect(self._sync_gui_state)
        self.pages_field.textChanged.connect(self._sync_gui_state)
        self.manual_title_field.textChanged.connect(self._sync_gui_state)
        self.model_field.textChanged.connect(self._sync_gui_state)
        self.base_url_field.textChanged.connect(self._sync_gui_state)
        self.api_key_field.textChanged.connect(self._sync_gui_state)
        self.api_key_source_choices.value_changed.connect(self._sync_api_key_controls)
        self.extra_prompt_edit.textChanged.connect(self._sync_gui_state)
        self.confirm_overwrite_checkbox.toggled.connect(self._sync_gui_state)
        self.show_date_checkbox.toggled.connect(self._sync_gui_state)
        self.beamer_title_page_checkbox.toggled.connect(self._sync_gui_state)
        self.clear_cache_checkbox.toggled.connect(self._sync_gui_state)
        self.chunk_pages_spin_box.valueChanged.connect(self._sync_gui_state)
        self.prefetch_chunks_spin_box.valueChanged.connect(self._sync_gui_state)
        self.llm_concurrency_spin_box.valueChanged.connect(self._sync_gui_state)
        self.llm_min_request_interval_spin_box.valueChanged.connect(self._sync_gui_state)
        self.batch_workers_spin_box.valueChanged.connect(self._sync_gui_state)
        self.structure_chunk_pages_spin_box.valueChanged.connect(self._sync_gui_state)
        self.structure_max_pages_spin_box.valueChanged.connect(self._sync_gui_state)
        self.image_dpi_spin_box.valueChanged.connect(self._sync_gui_state)
        self.image_dpi_min_spin_box.valueChanged.connect(self._sync_gui_state)
        self.image_dpi_max_spin_box.valueChanged.connect(self._sync_gui_state)
        self.jpeg_quality_spin_box.valueChanged.connect(self._sync_gui_state)
        self.llm_retries_spin_box.valueChanged.connect(self._sync_gui_state)
        self.llm_retry_initial_delay_spin_box.valueChanged.connect(self._sync_gui_state)
        self.llm_retry_max_delay_spin_box.valueChanged.connect(self._sync_gui_state)
        self.timeout_spin_box.valueChanged.connect(self._sync_gui_state)
        self.temperature_spin_box.valueChanged.connect(self._sync_gui_state)
        self.max_tokens_spin_box.valueChanged.connect(self._sync_gui_state)
        self.beamer_box_style_choices.value_changed.connect(self._sync_gui_state)
        self.ctex_font_profile_choices.value_changed.connect(self._sync_gui_state)

        self._sync_image_controls()
        self._sync_cache_controls()
        self._sync_api_key_controls()
        self._sync_gui_state()

    def current_display_preferences(self) -> GuiDisplayPreferences:
        """Return current theme and language preferences."""
        return self._display_preferences

    def eventFilter(self, watched: object, event: object) -> bool:
        if isinstance(event, QEvent) and event.type() == QEvent.Type.MouseButtonPress:
            widget = watched if isinstance(watched, QWidget) else None
            if widget is not None and widget.focusPolicy() == Qt.FocusPolicy.NoFocus:
                focus_widget = QApplication.focusWidget()
                if focus_widget is not None:
                    focus_widget.clearFocus()
        return super().eventFilter(watched, event)

    def set_display_preferences(
        self,
        preferences: GuiDisplayPreferences,
        *,
        emit: bool = False,
    ) -> None:
        """Apply theme and language preferences to the current panel."""
        self._display_preferences = preferences
        self._apply_theme()
        self._retranslate_ui()
        self._refresh_path_state()
        if emit:
            self.display_preferences_changed.emit(preferences)

    def toggle_theme(self) -> None:
        """Switch between light and dark themes."""
        next_theme = (
            GuiThemeMode.dark if self._theme == GuiThemeMode.light else GuiThemeMode.light
        )
        self.set_display_preferences(
            GuiDisplayPreferences(
                theme=next_theme,
                language=self._language,
                font_point_size=self._display_preferences.font_point_size,
            ),
            emit=True,
        )

    def toggle_language(self) -> None:
        """Switch between Chinese and English interface text."""
        next_language = (
            GuiLanguage.en_US if self._language == GuiLanguage.zh_CN else GuiLanguage.zh_CN
        )
        self.set_display_preferences(
            GuiDisplayPreferences(
                theme=self._theme,
                language=next_language,
                font_point_size=self._display_preferences.font_point_size,
            ),
            emit=True,
        )

    def _apply_theme(self) -> None:
        self.setFont(
            build_gui_font(
                self._display_preferences.font_point_size or DEFAULT_GUI_FONT_POINT_SIZE,
                current_font=self.font(),
            )
        )
        self.setStyleSheet(
            build_fluent_stylesheet(
                self._theme,
                font_point_size=self._display_preferences.font_point_size,
            )
        )

    def _retranslate_ui(self) -> None:
        self.subtitle_label.setText(self._tr("command.subtitle"))
        self.theme_button.setText(
            self._tr("command.theme.dark")
            if self._theme == GuiThemeMode.dark
            else self._tr("command.theme.light")
        )
        self.theme_button.setToolTip(
            self._tr("command.theme.switch_to_light")
            if self._theme == GuiThemeMode.dark
            else self._tr("command.theme.switch_to_dark")
        )
        self.language_button.setText(
            self._tr("command.language.en")
            if self._language == GuiLanguage.en_US
            else self._tr("command.language.zh")
        )
        self.language_button.setToolTip(
            self._tr("command.language.switch_to_zh")
            if self._language == GuiLanguage.en_US
            else self._tr("command.language.switch_to_en")
        )
        self.settings_button.setText(self._tr("command.settings"))
        self.settings_button.setToolTip(self._tr("command.settings.tooltip"))
        self.reset_defaults_button.setText(self._tr("command.reset_defaults"))
        self.reset_defaults_button.setToolTip(self._tr("command.reset_defaults.tooltip"))
        self.add_task_button.setText(self._tr("button.add_task"))
        self.add_task_button.setToolTip(self._tr("button.add_task.tooltip"))
        self.start_button.setText(self._tr("button.start"))
        self.start_button.setToolTip(self._tr("button.start.tooltip"))
        self.clear_tasks_button.setToolTip(self._tr("button.clear_tasks.tooltip"))

        for key, section in self._sections.items():
            section.set_title(self._tr(key))
        for key, label in self._row_labels.items():
            label.setText(self._tr(key))

        self.pdf_input_field.setPlaceholderText(self._tr("placeholder.pdf_input"))
        self.output_directory_field.setPlaceholderText(self._tr("placeholder.output_target"))
        self.pages_field.setPlaceholderText(self._tr("placeholder.pages"))
        self.manual_title_field.setPlaceholderText(self._tr("placeholder.manual_title"))
        self.extra_prompt_edit.setPlaceholderText(self._tr("placeholder.extra_prompt"))
        self.image_dpi_max_spin_box.setSpecialValueText(self._tr("special.default_dpi"))
        self.timeout_spin_box.setSpecialValueText(self._tr("special.no_timeout"))

        self.pdf_input_browse_button.setToolTip(self._tr("dialog.pdf_file.title"))
        self.output_browse_button.setToolTip(self._tr("field.output_target"))
        self.cache_browse_button.setToolTip(self._tr("dialog.cache_directory.title"))
        self.log_file_browse_button.setToolTip(self._tr("dialog.log_file.title"))
        self.confirm_overwrite_checkbox.setText(self._tr("checkbox.confirm_overwrite"))
        self.show_date_checkbox.setText(self._tr("checkbox.show_date"))
        self.beamer_title_page_checkbox.setText(self._tr("checkbox.beamer_title_page"))
        self.cache_enabled_checkbox.setText(self._tr("checkbox.cache_enabled"))
        self.clear_cache_checkbox.setText(self._tr("checkbox.clear_cache"))

        self._set_choice_items(self.input_type_choices, _INPUT_KIND_ITEMS)
        self._set_choice_items(self.output_kind_choices, _OUTPUT_KIND_ITEMS)
        self._set_choice_items(self.api_key_source_choices, _API_KEY_SOURCE_ITEMS)
        self._sync_api_key_controls()

        for status, metric in self._metric_pills.items():
            metric.set_label(self._tr(f"task.metric.{status.value}"))
        empty_title = self.findChild(QLabel, "taskEmptyTitleLabel")
        if empty_title is not None:
            empty_title.setText(self._tr("task.empty.title"))
        self._refresh_task_summary()

    def _browse_pdf_input(self) -> None:
        kind = self._current_input_kind()
        if kind == GuiInputKind.single_file:
            path, _selected_filter = QFileDialog.getOpenFileName(
                self,
                self._tr("dialog.pdf_file.title"),
                self._input_dialog_directory(),
                self._tr("dialog.filter.pdf"),
            )
            if path:
                self.set_input_selection(GuiInputSelection.from_single_file(path))
            return

        if kind == GuiInputKind.multiple_files:
            paths, _selected_filter = QFileDialog.getOpenFileNames(
                self,
                self._tr("dialog.pdf_files.title"),
                self._input_dialog_directory(),
                self._tr("dialog.filter.pdf"),
            )
            if paths:
                self.set_input_selection(GuiInputSelection.from_multiple_files(paths))
            return

        directory = QFileDialog.getExistingDirectory(
            self,
            self._tr("dialog.pdf_directory.title"),
            self._input_dialog_directory(),
        )
        if directory:
            self.set_input_selection(GuiInputSelection.from_directory(directory))

    def _browse_output_directory(self) -> None:
        if self._current_input_kind() == GuiInputKind.single_file and self._current_output_kind() == GuiOutputKind.tex_file:
            path, _selected_filter = QFileDialog.getSaveFileName(
                self,
                self._tr("dialog.output_tex.title"),
                self._output_tex_dialog_path(),
                self._tr("dialog.filter.tex"),
            )
            if path:
                self.set_output_directory(path)
            return

        directory = QFileDialog.getExistingDirectory(
            self,
            self._tr("dialog.output_directory.title"),
            self._output_dialog_directory(),
        )
        if directory:
            self.set_output_directory(directory)

    def _browse_cache_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            self._tr("dialog.cache_directory.title"),
            self._cache_dialog_directory(),
        )
        if directory:
            self.path_memory = self.path_memory.remember_cache_directory(directory)
            self.cache_directory_field.setText(directory)

    def _browse_log_file(self) -> None:
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            self._tr("dialog.log_file.title"),
            self._log_file_dialog_path(),
            self._tr("dialog.filter.log"),
        )
        if path:
            self.path_memory = self.path_memory.remember_log_file_path(path)
            self.log_file_path_field.setText(path)

    def _change_input_kind(self, _text: str) -> None:
        self.set_input_selection(GuiInputSelection.empty(self._current_input_kind()))

    def set_input_selection(self, selection: GuiInputSelection) -> None:
        """Set the current PDF input selection and refresh dependent controls."""
        self.path_memory = self.path_memory.remember_input_selection(selection)
        self.selection_state = GuiPathSelectionState(
            input_selection=selection,
            output_directory=self.selection_state.output_directory,
        )
        self._refresh_path_state()

    def set_output_directory(self, directory: str) -> None:
        """Set the current output directory and refresh dependent controls."""
        self.path_memory = self.path_memory.remember_output_path(
            directory,
            is_file=self._output_target_is_single_tex_file(),
        )
        self.selection_state = GuiPathSelectionState(
            input_selection=self.selection_state.input_selection,
            output_directory=directory,
        )
        self._refresh_path_state()

    def set_path_memory(self, path_memory: GuiPathMemory) -> None:
        """Replace dialog path memory restored from persistent settings."""
        self.path_memory = path_memory
        self._refresh_log_file_path_field()

    def current_path_memory(self) -> GuiPathMemory:
        """Return current dialog path memory."""
        return self.path_memory.remember_log_file_path(self._current_log_file_path())

    def _refresh_path_state(self, *, publish_status: bool = True) -> None:
        self._refresh_log_file_path_field()
        self.pdf_input_field.setText(self.selection_state.input_selection.display_text(self._language))
        self.output_directory_field.setText(self.selection_state.output_directory)
        self._refresh_mode_controls()
        self._refresh_action_state()
        if publish_status:
            self._publish_status_message(self._status_message())

    def _status_message(self) -> str:
        has_input = self.selection_state.input_selection.is_valid
        has_output = bool(self.selection_state.output_directory)
        errors = validate_gui_settings(self.current_settings(), language=self._language)
        if errors:
            return errors[0]
        if has_input and has_output:
            return self._tr("status.paths.ready")
        if has_input:
            return self._tr("status.paths.need_output")
        if has_output:
            return self._tr("status.paths.need_input")
        return self._tr("status.paths.empty")

    def _publish_status_message(self, message: str) -> None:
        window = self.window()
        status_bar = getattr(window, "statusBar", None)
        if callable(status_bar):
            status_bar().showMessage(message)

    def _input_dialog_directory(self) -> str:
        return self.path_memory.last_input_directory or system_default_dialog_directory()

    def _output_dialog_directory(self) -> str:
        return self.path_memory.last_output_directory or system_default_dialog_directory()

    def _cache_dialog_directory(self) -> str:
        cache_directory = self.cache_directory_field.text().strip()
        if cache_directory:
            return cache_directory
        return (
            self.path_memory.last_cache_directory
            or system_default_dialog_directory()
        )

    def _log_file_dialog_path(self) -> str:
        return self._current_log_file_path()

    def _current_log_file_path(self) -> str:
        return (
            self.log_file_path_field.text().strip()
            or self.path_memory.last_log_file_path
            or default_gui_log_file_path()
        )

    def _refresh_log_file_path_field(self) -> None:
        path = self.path_memory.last_log_file_path or default_gui_log_file_path()
        if self.log_file_path_field.text() != path:
            self.log_file_path_field.setText(path)

    def _output_tex_dialog_path(self) -> str:
        directory = self._output_dialog_directory()
        input_paths = self.selection_state.input_selection.paths
        filename = "output.tex"
        if input_paths:
            stem = self._path_stem(input_paths[0])
            if stem:
                filename = f"{stem}.tex"
        return join_dialog_path(directory, filename)

    def _output_target_is_single_tex_file(self) -> bool:
        return (
            self._current_input_kind() == GuiInputKind.single_file
            and self._current_output_kind() == GuiOutputKind.tex_file
        )

    def _path_stem(self, path: str) -> str:
        return path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].rsplit(".", 1)[0]

    def _current_input_kind(self) -> GuiInputKind:
        return GuiInputKind(self.input_type_choices.value())

    def _current_conversion_mode(self) -> GuiConversionMode:
        return self._current_output_kind()

    def _current_output_kind(self) -> GuiOutputKind:
        return GuiOutputKind(self.output_kind_choices.value())

    def _current_api_key_source(self) -> GuiApiKeySource:
        return GuiApiKeySource(self.api_key_source_choices.value())

    def _set_conversion_mode(self, mode: GuiConversionMode) -> None:
        self.output_kind_choices.set_value(mode.value)
        self._sync_gui_state()

    def _refresh_mode_controls(self) -> None:
        batch_input = self._current_input_kind() != GuiInputKind.single_file
        self.batch_workers_spin_box.setEnabled(batch_input)
        self.batch_pattern_field.setEnabled(self._current_input_kind() == GuiInputKind.directory)

        project_mode = self._current_output_kind() == GuiOutputKind.project
        self.structure_mode_choices.setEnabled(project_mode)
        self.structure_chunk_pages_spin_box.setEnabled(project_mode)
        self.structure_max_pages_spin_box.setEnabled(project_mode)

    def _sync_cache_controls(self) -> None:
        enabled = self.cache_enabled_checkbox.isChecked()
        self.cache_directory_field.setEnabled(enabled)
        self.cache_browse_button.setEnabled(enabled)
        if not enabled:
            self.clear_cache_checkbox.setChecked(False)
        self.clear_cache_checkbox.setEnabled(enabled)
        self._sync_gui_state()

    def _sync_image_controls(self) -> None:
        image_format = self.image_format_choices.value()
        is_jpeg = image_format == "jpeg"
        self.jpeg_quality_spin_box.setEnabled(is_jpeg or image_format == "auto")
        self._sync_gui_state()

    def _sync_api_key_controls(self) -> None:
        if self._current_api_key_source() == GuiApiKeySource.environment:
            self.api_key_field.setPlaceholderText(self._tr("placeholder.api_key_env"))
        else:
            self.api_key_field.setPlaceholderText(self._tr("placeholder.api_key_direct"))
        self.api_key_field.setEchoMode(QLineEdit.EchoMode.Password)
        self._sync_gui_state()

    def _sync_gui_state(self) -> None:
        self._refresh_action_state()
        self._publish_status_message(self._status_message())

    def _refresh_action_state(self) -> None:
        is_running = self._executor is not None
        self.add_task_button.setEnabled(
            not is_running
            and self._has_required_task_fields()
        )
        self.clear_tasks_button.setEnabled(not is_running and bool(self._task_states))
        self.start_button.setEnabled(
            not is_running
            and any(state.status == GuiTaskStatus.pending for state in self._task_states.values())
        )
        single_input = self._current_input_kind() == GuiInputKind.single_file
        self.manual_title_field.setEnabled(single_input and self.title_source_choices.value() != "llm")
        self.show_date_checkbox.setEnabled(True)
        self.beamer_title_page_checkbox.setEnabled(
            self.document_class_choices.value() in {"auto", "beamer", "ctexbeamer"}
        )

        project_mode = self._current_output_kind() == GuiOutputKind.project
        self.structure_mode_choices.setEnabled(project_mode)
        self.structure_chunk_pages_spin_box.setEnabled(project_mode)
        self.structure_max_pages_spin_box.setEnabled(project_mode)
        self.batch_workers_spin_box.setEnabled(self._current_input_kind() != GuiInputKind.single_file)
        self.batch_pattern_field.setEnabled(self._current_input_kind() == GuiInputKind.directory)

    def _has_required_task_fields(self) -> bool:
        if not self.selection_state.can_add_task:
            return False
        if self._current_input_kind() == GuiInputKind.directory and not self.batch_pattern_field.text().strip():
            return False
        return all(
            field.text().strip()
            for field in (
                self.model_field,
                self.api_key_field,
            )
        )

    def open_settings_dialog(self) -> GuiDisplayPreferences | None:
        dialog = SettingsDialog(self, preferences=self._display_preferences)
        if dialog.exec() == dialog.DialogCode.Accepted:
            return dialog.selected_preferences()
        return None

    def open_about_dialog(self) -> None:
        dialog = AboutDialog(self, app_name=APP_DISPLAY_NAME, preferences=self._display_preferences)
        dialog.exec()

    def reset_to_default_configuration(self) -> None:
        self.reset_settings()

    def current_settings(self) -> GuiConversionSettings:
        return GuiConversionSettings(
            path_state=self.selection_state,
            output_kind=self._current_output_kind(),
            confirm_overwrite=self.confirm_overwrite_checkbox.isChecked(),
            batch_pattern=self.batch_pattern_field.text(),
            pages=self.pages_field.text(),
            document_class=self.document_class_choices.value(),
            structure_mode=self.structure_mode_choices.value(),
            structure_chunk_pages=self.structure_chunk_pages_spin_box.value(),
            structure_max_pages=self.structure_max_pages_spin_box.value(),
            manual_title=self.manual_title_field.text(),
            title_source=self.title_source_choices.value(),
            show_date=self.show_date_checkbox.isChecked(),
            beamer_title_page=self.beamer_title_page_checkbox.isChecked(),
            model=self.model_field.text(),
            base_url=self.base_url_field.text(),
            api_key=self.api_key_field.text(),
            api_key_source=self._current_api_key_source(),
            prompt_preset=DEFAULT_PROMPT_PRESET_NAME,
            extra_prompt=self.extra_prompt_edit.toPlainText(),
            temperature=self.temperature_spin_box.value(),
            timeout_seconds=None if self.timeout_spin_box.value() == 0 else self.timeout_spin_box.value(),
            max_tokens=self.max_tokens_spin_box.value(),
            cache_enabled=self.cache_enabled_checkbox.isChecked(),
            cache_directory=self.cache_directory_field.text(),
            clear_cache=self.clear_cache_checkbox.isChecked(),
            chunk_pages=self.chunk_pages_spin_box.value(),
            prefetch_chunks=self.prefetch_chunks_spin_box.value(),
            llm_max_concurrency=self.llm_concurrency_spin_box.value(),
            llm_min_request_interval=self.llm_min_request_interval_spin_box.value(),
            batch_workers=self.batch_workers_spin_box.value(),
            image_dpi=self.image_dpi_spin_box.value(),
            image_dpi_min=self.image_dpi_min_spin_box.value(),
            image_dpi_max=None if self.image_dpi_max_spin_box.value() == 0 else self.image_dpi_max_spin_box.value(),
            image_format=self.image_format_choices.value(),
            jpeg_quality=self.jpeg_quality_spin_box.value(),
            llm_retries=self.llm_retries_spin_box.value(),
            llm_retry_initial_delay=self.llm_retry_initial_delay_spin_box.value(),
            llm_retry_max_delay=self.llm_retry_max_delay_spin_box.value(),
            beamer_box_style=self.beamer_box_style_choices.value(),
            ctex_font_profile=self.ctex_font_profile_choices.value(),
        )

    def set_settings(self, settings: GuiConversionSettings) -> None:
        self.selection_state = settings.path_state
        self.input_type_choices.set_value(settings.path_state.input_selection.kind.value)
        self.output_kind_choices.set_value(settings.output_kind.value)
        self.confirm_overwrite_checkbox.setChecked(settings.confirm_overwrite)
        self.batch_pattern_field.setText(settings.batch_pattern)
        self.pages_field.setText(settings.pages)
        self.document_class_choices.set_value(settings.document_class)
        self.structure_mode_choices.set_value(settings.structure_mode)
        self.structure_chunk_pages_spin_box.setValue(settings.structure_chunk_pages)
        self.structure_max_pages_spin_box.setValue(settings.structure_max_pages)
        self.manual_title_field.setText(settings.manual_title)
        self.title_source_choices.set_value(settings.title_source)
        self.show_date_checkbox.setChecked(settings.show_date)
        self.beamer_title_page_checkbox.setChecked(settings.beamer_title_page)
        self.model_field.setText(settings.model)
        self.base_url_field.setText(settings.base_url)
        self.api_key_field.setText(settings.api_key)
        self.api_key_source_choices.set_value(settings.api_key_source.value)
        self.prompt_preset_field.setText(DEFAULT_PROMPT_PRESET_NAME)
        self.extra_prompt_edit.setPlainText(settings.extra_prompt)
        self.temperature_spin_box.setValue(settings.temperature)
        self.timeout_spin_box.setValue(0.0 if settings.timeout_seconds is None else settings.timeout_seconds)
        self.max_tokens_spin_box.setValue(settings.max_tokens)
        self.cache_enabled_checkbox.setChecked(settings.cache_enabled)
        self.cache_directory_field.setText(settings.cache_directory)
        self.clear_cache_checkbox.setChecked(settings.clear_cache)
        self.chunk_pages_spin_box.setValue(settings.chunk_pages)
        self.prefetch_chunks_spin_box.setValue(settings.prefetch_chunks)
        self.llm_concurrency_spin_box.setValue(settings.llm_max_concurrency)
        self.llm_min_request_interval_spin_box.setValue(settings.llm_min_request_interval)
        self.batch_workers_spin_box.setValue(settings.batch_workers)
        self.image_dpi_spin_box.setValue(settings.image_dpi)
        self.image_dpi_min_spin_box.setValue(settings.image_dpi_min)
        self.image_dpi_max_spin_box.setValue(0 if settings.image_dpi_max is None else settings.image_dpi_max)
        self.image_format_choices.set_value(settings.image_format)
        self.jpeg_quality_spin_box.setValue(settings.jpeg_quality)
        self.llm_retries_spin_box.setValue(settings.llm_retries)
        self.llm_retry_initial_delay_spin_box.setValue(settings.llm_retry_initial_delay)
        self.llm_retry_max_delay_spin_box.setValue(settings.llm_retry_max_delay)
        self.beamer_box_style_choices.set_value(settings.beamer_box_style)
        self.ctex_font_profile_choices.set_value(settings.ctex_font_profile)
        self._sync_api_key_controls()
        self._sync_cache_controls()
        self._sync_image_controls()
        self._refresh_path_state()

    def reset_settings(self) -> None:
        current_preferences = self._display_preferences
        self.set_settings(GuiConversionSettings())
        self._display_preferences = current_preferences
        self._apply_theme()
        self._retranslate_ui()
        self._refresh_path_state()

    def validate_settings(self) -> list[str]:
        return validate_gui_settings(self.current_settings(), language=self._language)

    def export_diagnostic_log(self) -> None:
        """Export the current in-memory diagnostic log to the selected path."""
        path = self._current_log_file_path()
        try:
            exported = self._diagnostic_log.export(path)
        except OSError as exc:
            self._diagnostic_log.record(
                "log_export_failed",
                path=path,
                error=str(exc),
                exception_type=exc.__class__.__name__,
            )
            self._publish_status_message(
                self._tr("status.log_export_failed", error=str(exc))
            )
            return
        self.path_memory = self.path_memory.remember_log_file_path(str(exported))
        self.log_file_path_field.setText(str(exported))
        self._publish_status_message(self._tr("status.log_exported", path=exported))

    def add_current_tasks(self) -> None:
        """Create pending in-memory tasks from current settings."""
        if self._executor is not None:
            self._publish_status_message(self._tr("status.running_cannot_add"))
            return
        try:
            new_tasks = create_conversion_tasks(
                self.current_settings(),
                language=self._language,
            )
        except GuiTaskCreationError as exc:
            self._publish_status_message(str(exc))
            return
        self.tasks.extend(new_tasks)
        for task in new_tasks:
            self._task_states[task.task_id] = create_task_view_state(task)
            self._diagnostic_log.record(
                "task_added",
                task_id=task.task_id,
                pdf=task.source_pdf,
                target=task.output_target.path,
                output_kind=task.output_target.kind.value,
            )
        self._clear_path_selection_after_task_creation()
        self._refresh_path_state(publish_status=False)
        self._refresh_task_summary()
        self._publish_status_message(self._tr("status.tasks_added", count=len(new_tasks)))

    def start_pending_tasks(self) -> None:
        """Start background execution for all pending tasks."""
        if self._executor is not None:
            self._publish_status_message(self._tr("status.already_running"))
            return
        pending_tasks = [
            task
            for task in self.tasks
            if task.task_id in self._task_states
            and self._task_states[task.task_id].status == GuiTaskStatus.pending
        ]
        if not pending_tasks:
            self._publish_status_message(self._tr("status.no_pending_tasks"))
            self._refresh_action_state()
            return

        self._diagnostic_log.record(
            "tasks_start_requested",
            task_count=len(pending_tasks),
        )
        max_workers = self.current_settings().batch_workers
        executor = self._executor_factory(pending_tasks, max_workers=max_workers, parent=self)
        self._executor = executor
        executor.task_started.connect(self._handle_executor_task_started)
        executor.task_progress.connect(self._handle_executor_task_progress)
        executor.task_completed.connect(self._handle_executor_task_completed)
        executor.task_failed.connect(self._handle_executor_task_failed)
        executor.task_canceling.connect(self._handle_executor_task_canceling)
        executor.task_canceled.connect(self._handle_executor_task_canceled)
        executor.overwrite_confirmation_requested.connect(self._handle_overwrite_confirmation_requested)
        executor.all_finished.connect(self._handle_executor_finished)
        self._publish_status_message(self._tr("status.starting_tasks", count=len(pending_tasks)))
        self._refresh_action_state()
        executor.start()

    def cancel_task(self, task_id: str) -> None:
        """Cancel one pending or running task."""
        state = self._task_states.get(task_id)
        if state is None:
            raise ValueError(self._tr("status.unknown_task", task_id=task_id))
        if state.status in TERMINAL_TASK_STATUSES:
            return
        self._diagnostic_log.record(
            "task_cancel_requested",
            task_id=task_id,
            pdf=state.task.source_pdf,
        )
        if state.status == GuiTaskStatus.pending:
            if self._executor is not None:
                self._executor.cancel_task(task_id)
            mark_task_canceled(state, language=self._language)
            self._diagnostic_log.record(
                "task_canceled",
                task_id=task_id,
                pdf=state.task.source_pdf,
            )
            self._refresh_task_summary()
            self._publish_status_message(self._tr("status.pending_canceled"))
            return
        mark_task_canceling(state, language=self._language)
        if self._executor is not None:
            self._executor.cancel_task(task_id)
        self._refresh_task_summary()
        self._publish_status_message(self._tr("status.canceling"))

    def clear_task_queue(self) -> None:
        """Clear all queued tasks while the executor is idle."""
        if self._executor is not None:
            return
        if not self.tasks and not self._task_states:
            return
        self._diagnostic_log.record("task_queue_cleared", task_count=len(self._task_states))
        self.tasks.clear()
        self._task_states.clear()
        self._refresh_task_summary()
        self._publish_status_message(self._tr("status.tasks_cleared"))

    def task_view_states(self) -> tuple[GuiTaskViewState, ...]:
        """Return current task display states in queue order."""
        return tuple(
            self._task_states[task.task_id]
            for task in self.tasks
            if task.task_id in self._task_states
        )

    def update_task_state(self, task_id: str, update: GuiTaskRuntimeUpdate) -> None:
        """Apply a display update to one queued task."""
        state = self._task_states.get(task_id)
        if state is None:
            raise ValueError(self._tr("status.unknown_task", task_id=task_id))
        apply_task_update(state, update)
        self._refresh_task_summary()

    def handle_task_progress(self, task_id: str, event: ProgressEvent) -> None:
        """Merge one core progress event into one queued task."""
        state = self._task_states.get(task_id)
        if state is None:
            raise ValueError(self._tr("status.unknown_task", task_id=task_id))
        self._diagnostic_log.record_progress(
            event,
            task_id=task_id,
            pdf=state.task.source_pdf,
        )
        apply_progress_event(state, event, language=self._language)
        self._refresh_task_summary()

    def _handle_executor_task_started(self, task_id: str) -> None:
        state = self._task_states.get(task_id)
        if state is None or state.status == GuiTaskStatus.canceled:
            return
        self._diagnostic_log.record(
            "task_started",
            task_id=task_id,
            pdf=state.task.source_pdf,
        )
        mark_task_running(state, language=self._language)
        self._refresh_task_summary()
        self._publish_status_message(self._tr("status.converting", label=state.label))

    def _handle_executor_task_progress(self, task_id: str, event: object) -> None:
        if isinstance(event, ProgressEvent):
            self.handle_task_progress(task_id, event)

    def _handle_executor_task_completed(self, task_id: str, result: str, notes: object) -> None:
        state = self._task_states.get(task_id)
        if state is None:
            return
        resolved_notes = tuple(notes) if isinstance(notes, tuple) else ()
        self._diagnostic_log.record(
            "task_completed",
            task_id=task_id,
            pdf=state.task.source_pdf,
            target=result,
        )
        self._diagnostic_log.record(
            "write_completed",
            task_id=task_id,
            pdf=state.task.source_pdf,
            path=result,
            output_kind=state.task.output_target.kind.value,
        )
        mark_task_completed(
            state,
            result=result,
            notes=resolved_notes,
            language=self._language,
        )
        self._refresh_task_summary()
        self._publish_status_message(self._tr("status.completed", label=state.label))

    def _handle_executor_task_failed(self, task_id: str, error: str) -> None:
        state = self._task_states.get(task_id)
        if state is None:
            return
        self._diagnostic_log.record(
            "task_failed",
            task_id=task_id,
            pdf=state.task.source_pdf,
            error=error,
        )
        mark_task_failed(state, error)
        self._refresh_task_summary()
        self._publish_status_message(self._tr("status.failed", label=state.label))

    def _handle_executor_task_canceling(self, task_id: str) -> None:
        state = self._task_states.get(task_id)
        if state is None or state.status in TERMINAL_TASK_STATUSES:
            return
        self._diagnostic_log.record(
            "task_canceling",
            task_id=task_id,
            pdf=state.task.source_pdf,
        )
        mark_task_canceling(state, language=self._language)
        self._refresh_task_summary()

    def _handle_executor_task_canceled(self, task_id: str) -> None:
        state = self._task_states.get(task_id)
        if state is None or state.status == GuiTaskStatus.canceled:
            return
        self._diagnostic_log.record(
            "task_canceled",
            task_id=task_id,
            pdf=state.task.source_pdf,
        )
        mark_task_canceled(state, language=self._language)
        self._refresh_task_summary()
        self._publish_status_message(self._tr("status.canceled", label=state.label))

    def _handle_overwrite_confirmation_requested(self, request: object) -> None:
        if not isinstance(request, GuiOverwriteConfirmationRequest):
            return
        self._diagnostic_log.record(
            "overwrite_requested",
            task_id=request.task_id,
            path=request.target,
            output_kind=request.output_kind.value,
        )
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle(self._tr("dialog.overwrite.title"))
        task_label = request.task_label or request.task_id or self._tr("dialog.overwrite.task_fallback")
        message.setText(f"{request.summary}：{task_label}")
        message.setInformativeText(
            self._tr(
                "dialog.overwrite.informative",
                details=request.details,
                target=request.target,
            )
        )
        message.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        message.setDefaultButton(QMessageBox.StandardButton.No)
        message.button(QMessageBox.StandardButton.Yes).setText(self._tr("dialog.overwrite.yes"))
        message.button(QMessageBox.StandardButton.No).setText(self._tr("dialog.overwrite.no"))
        approved = message.exec() == QMessageBox.StandardButton.Yes
        self._diagnostic_log.record(
            "overwrite_approved" if approved else "overwrite_rejected",
            task_id=request.task_id,
            path=request.target,
            output_kind=request.output_kind.value,
        )
        request.resolve(approved)

    def _handle_executor_finished(self) -> None:
        if self._executor is not None:
            self._executor.deleteLater()
            self._executor = None
        self._diagnostic_log.record("executor_finished")
        self._refresh_task_summary()
        self._publish_status_message(self._tr("status.finished"))

    def _refresh_task_summary(self) -> None:
        counts = {status: 0 for status in GuiTaskStatus}
        states = list(self.task_view_states())
        for state in states:
            counts[state.status] += 1
        for status, label in self.task_metric_labels.items():
            label.setText(str(counts[status]))
        empty_status = self.findChild(QLabel, "taskEmptyStatusLabel")
        if empty_status is not None:
            total = len(states)
            empty_status.setText(
                self._tr("task.empty.status")
                if total == 0
                else self._tr("task.empty.created", count=total)
            )
        self._refresh_task_rows(states)
        self._refresh_overall_progress(states)
        self._refresh_action_state()

    def _refresh_task_rows(self, states: list[GuiTaskViewState]) -> None:
        active_ids = {state.task_id for state in states}
        for task_id, row in list(self._task_rows.items()):
            if task_id not in active_ids:
                self.task_rows_layout.removeWidget(row)
                row.deleteLater()
                del self._task_rows[task_id]

        for state in states:
            row = self._task_rows.get(state.task_id)
            if row is None:
                row = self._create_task_row(state)
                self._task_rows[state.task_id] = row
                self.task_rows_layout.addWidget(row)
            self._update_task_row(row, state)

        has_tasks = bool(states)
        self.task_empty_state.setVisible(not has_tasks)
        self.task_list_scroll_area.setVisible(has_tasks)

    def _create_task_row(self, state: GuiTaskViewState) -> QFrame:
        row = QFrame(self.task_rows_container)
        row.setObjectName(f"taskRow_{state.task_id}")
        row.setProperty("taskRow", True)
        layout = QVBoxLayout(row)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        title = QLabel(state.label, row)
        title.setObjectName(f"taskTitleLabel_{state.task_id}")
        title.setProperty("sectionTitle", True)
        title.setWordWrap(True)
        header.addWidget(title, 1)

        status = QLabel(row)
        status.setObjectName(f"taskStatusBadge_{state.task_id}")
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status.setProperty("taskStatus", state.status.value)
        header.addWidget(status)

        cancel = self._make_icon_button(
            f"taskCancelButton_{state.task_id}",
            QStyle.StandardPixmap.SP_DialogCancelButton,
            self._tr("task.cancel.tooltip"),
        )
        cancel.clicked.connect(lambda _checked=False, task_id=state.task_id: self.cancel_task(task_id))
        header.addWidget(cancel)
        layout.addLayout(header)

        target = QLabel(row)
        target.setObjectName(f"taskTargetLabel_{state.task_id}")
        target.setProperty("muted", True)
        target.setWordWrap(True)
        layout.addWidget(target)

        stage = QLabel(row)
        stage.setObjectName(f"taskStageLabel_{state.task_id}")
        stage.setProperty("muted", True)
        stage.setWordWrap(True)
        layout.addWidget(stage)

        progress = QProgressBar(row)
        progress.setObjectName(f"taskProgressBar_{state.task_id}")
        progress.setRange(0, 100)
        progress.setTextVisible(True)
        layout.addWidget(progress)

        metrics = QHBoxLayout()
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setSpacing(10)
        cache = QLabel(row)
        cache.setObjectName(f"taskCacheLabel_{state.task_id}")
        cache.setProperty("muted", True)
        metrics.addWidget(cache)
        retry = QLabel(row)
        retry.setObjectName(f"taskRetryLabel_{state.task_id}")
        retry.setProperty("muted", True)
        metrics.addWidget(retry)
        metrics.addStretch(1)
        layout.addLayout(metrics)

        result = QLabel(row)
        result.setObjectName(f"taskResultLabel_{state.task_id}")
        result.setProperty("muted", True)
        result.setWordWrap(True)
        layout.addWidget(result)

        return row

    def _update_task_row(self, row: QFrame, state: GuiTaskViewState) -> None:
        status = row.findChild(QLabel, f"taskStatusBadge_{state.task_id}")
        if status is not None:
            status.setText(task_status_label(state.status, self._language))
            status.setProperty("taskStatus", state.status.value)
            status.style().unpolish(status)
            status.style().polish(status)

        cancel = row.findChild(QToolButton, f"taskCancelButton_{state.task_id}")
        if cancel is not None:
            active = state.status not in TERMINAL_TASK_STATUSES
            cancel.setEnabled(active)
            cancel.setVisible(active)

        target = row.findChild(QLabel, f"taskTargetLabel_{state.task_id}")
        if target is not None:
            target.setText(self._task_target_text(state))

        stage = row.findChild(QLabel, f"taskStageLabel_{state.task_id}")
        if stage is not None:
            detail = self._tr(
                "task.stage.prefix",
                stage=task_stage_label(state.stage, self._language),
            )
            recent_event = task_recent_event_text(state, self._language)
            if recent_event:
                detail = f"{detail} · {recent_event}"
            stage.setText(detail)

        progress = row.findChild(QProgressBar, f"taskProgressBar_{state.task_id}")
        if progress is not None:
            progress.setValue(state.progress)
            progress.setFormat(f"{state.progress}%")

        cache = row.findChild(QLabel, f"taskCacheLabel_{state.task_id}")
        if cache is not None:
            cache.setText(self._tr("task.cache_hits", count=state.cache_hits))

        retry = row.findChild(QLabel, f"taskRetryLabel_{state.task_id}")
        if retry is not None:
            retry.setText(self._tr("task.retries", count=state.retries))

        result = row.findChild(QLabel, f"taskResultLabel_{state.task_id}")
        if result is not None:
            result.setText(self._task_result_text(state))

    def _refresh_overall_progress(self, states: list[GuiTaskViewState]) -> None:
        if not states:
            self.overall_progress_bar.setValue(0)
            return
        average = round(sum(state.progress for state in states) / len(states))
        self.overall_progress_bar.setValue(max(0, min(100, average)))

    def _task_target_text(self, state: GuiTaskViewState) -> str:
        key = (
            "task.target.project"
            if state.task.output_target.kind == GuiOutputKind.project
            else "task.target.tex_file"
        )
        return self._tr(key, path=state.task.output_target.path)

    def _task_result_text(self, state: GuiTaskViewState) -> str:
        if state.status == GuiTaskStatus.failed:
            return self._tr(
                "task.result.failed",
                error=state.error or self._tr("task.result.unknown_error"),
            )
        if state.status == GuiTaskStatus.canceled:
            return self._tr("task.result.canceled")
        if state.status == GuiTaskStatus.completed:
            return self._tr(
                "task.result.completed",
                result=state.result or state.task.output_target.path,
            )
        if state.notes:
            return "；".join(state.notes)
        return self._tr("task.result.waiting")

    def close_executor(self) -> None:
        """Request executor shutdown when the owning window is closing."""
        if self._executor is not None:
            self._executor.shutdown()
            self._executor.deleteLater()
            self._executor = None

    def _clear_path_selection_after_task_creation(self) -> None:
        """Reset active input/output selection and page range after queue creation."""
        self.pages_field.clear()
        self.selection_state = GuiPathSelectionState(
            input_selection=GuiInputSelection.empty(self._current_input_kind()),
            output_directory="",
        )
