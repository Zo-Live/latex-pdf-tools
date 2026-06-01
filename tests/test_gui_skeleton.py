"""Tests for the GUI application skeleton."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from pathlib import PurePosixPath
import runpy

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QSpinBox,
    QToolButton,
    QWidget,
)
from texbook.convert import LatexProjectResult  # noqa: E402
from texbook.document_class import DocumentClassMode  # noqa: E402
from texbook.gui.app import GUI_BASE_FONT_POINT_SIZE, create_application  # noqa: E402
from texbook.gui.core_adapter import GuiCoreAdapterError, build_core_conversion_bundle  # noqa: E402
from texbook.gui.display import GuiDisplayPreferences, GuiLanguage, GuiThemeMode  # noqa: E402
from texbook.gui.dialogs import AboutDialog, SettingsDialog  # noqa: E402
from texbook.gui.executor import (  # noqa: E402
    GuiCancellationToken,
    GuiOverwriteConfirmationRequest,
    GuiTaskCanceled,
    GuiTaskExecutor,
    GuiTaskExecutionError,
    GuiWritePolicy,
    run_gui_conversion_task,
    write_project_result,
    write_tex_result,
)
from texbook.gui.main_panel import ConversionMainPanel  # noqa: E402
from texbook.gui.main_window import MainWindow  # noqa: E402
from texbook.gui.persistence import (  # noqa: E402
    GuiPathMemory,
    GuiPersistentState,
    GuiSettingsStore,
)
from texbook.gui.resources import (  # noqa: E402
    APP_DISPLAY_NAME,
    APP_ORGANIZATION_NAME,
    APP_WINDOW_TITLE,
    resolve_app_icon_path,
)
from texbook.gui.selection import GuiInputKind, GuiInputSelection, GuiPathSelectionState  # noqa: E402
from texbook.gui.settings import (  # noqa: E402
    LEGACY_GUI_CACHE_DIRECTORY,
    GuiApiKeySource,
    GuiConversionMode,
    GuiConversionSettings,
    GuiOutputKind,
    default_gui_cache_directory,
    default_gui_log_file_path,
    validate_gui_settings,
)
from texbook.gui.theme import build_fluent_stylesheet  # noqa: E402
from texbook.gui.tasks import (  # noqa: E402
    GuiTaskRuntimeUpdate,
    GuiTaskStage,
    GuiTaskStatus,
    apply_progress_event,
    create_task_view_state,
    create_conversion_tasks,
    mark_task_completed,
    mark_task_failed,
    mark_task_running,
)
from texbook.gui.widgets import ChoiceGrid  # noqa: E402
from texbook.llm.scheduler import ProgressEvent  # noqa: E402
from texbook.llm.pipeline import LLMConversionResult  # noqa: E402
from texbook.output_options import BeamerBoxStyle, CtexFontProfile  # noqa: E402
from texbook.structure import StructureMode  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clear_default_gui_settings():
    settings = QSettings(APP_ORGANIZATION_NAME, APP_DISPLAY_NAME)
    settings.clear()
    settings.sync()
    yield
    settings.clear()
    settings.sync()


def _gui_settings_store(
    tmp_path: Path,
    *,
    default_directory: str = r"C:\Users\tester\Documents",
) -> GuiSettingsStore:
    ini_format = (
        QSettings.Format.IniFormat
        if hasattr(QSettings, "Format")
        else QSettings.IniFormat
    )
    settings = QSettings(str(tmp_path / "texbook-gui.ini"), ini_format)
    settings.clear()
    return GuiSettingsStore(
        settings=settings,
        default_directory_provider=lambda: default_directory,
    )


class _FakeConverter:
    def __init__(self, progress_reporter=None, *, cancel_during_convert: GuiCancellationToken | None = None):
        self.progress_reporter = progress_reporter
        self.cancel_during_convert = cancel_during_convert

    def convert(self, pdf_path: Path, *, pages: list[int] | None = None):
        if self.progress_reporter is not None:
            self.progress_reporter(
                ProgressEvent(
                    kind="stage_completed",
                    operation="chunk",
                    label=pdf_path.name,
                    metadata={"chunk_index": 1, "total_chunks": 1, "pages": pages or []},
                )
            )
        if self.cancel_during_convert is not None:
            self.cancel_during_convert.cancel()
        return LLMConversionResult(latex=f"% {pdf_path.name}\n", notes=["note"])

    def convert_project(self, pdf_path: Path, *, pages: list[int] | None = None):
        if self.progress_reporter is not None:
            self.progress_reporter(
                ProgressEvent(kind="stage_completed", operation="conversion", label=pdf_path.name)
            )
        return LatexProjectResult(
            files={
                PurePosixPath("main.tex"): "% main\n",
                PurePosixPath("chapters/chapter01.tex"): "% body\n",
            },
            entrypoint=PurePosixPath("main.tex"),
            notes=["project note"],
        )


class _FakeExecutor:
    def __init__(self, tasks, *, max_workers=1, parent=None):
        self.tasks = list(tasks)
        self.max_workers = max_workers
        self.parent = parent
        self.started = False
        self.canceled_task_ids = []
        self.task_started = _FakeSignal()
        self.task_progress = _FakeSignal()
        self.task_completed = _FakeSignal()
        self.task_failed = _FakeSignal()
        self.task_canceling = _FakeSignal()
        self.task_canceled = _FakeSignal()
        self.overwrite_confirmation_requested = _FakeSignal()
        self.all_finished = _FakeSignal()

    def start(self):
        self.started = True

    def cancel_task(self, task_id: str):
        self.canceled_task_ids.append(task_id)
        self.task_canceling.emit(task_id)

    def shutdown(self):
        pass

    def deleteLater(self):
        pass


class _FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class _FakeWheelEvent:
    def __init__(self):
        self.ignored = False

    def ignore(self):
        self.ignored = True


def _choice(panel: QWidget, object_name: str) -> ChoiceGrid:
    choices = panel.findChild(ChoiceGrid, object_name)
    assert choices is not None
    return choices


def _set_choice(panel: QWidget, object_name: str, value: str) -> None:
    _choice(panel, object_name).set_value(value)


def _choice_value(panel: QWidget, object_name: str) -> str:
    return _choice(panel, object_name).value()


def _fill_required_task_fields(
    panel: QWidget,
    *,
    model: str = "model",
    api_key: str = "key",
) -> None:
    panel.findChild(QLineEdit, "modelField").setText(model)
    panel.findChild(QLineEdit, "apiKeyField").setText(api_key)


def test_icon_resource_resolves_to_docs_icon():
    assert resolve_app_icon_path() == ROOT / "docs" / "icon.ico"


def test_icon_resource_resolves_from_pyinstaller_meipass(tmp_path, monkeypatch):
    meipass_root = tmp_path / "bundle"
    icon_path = meipass_root / "docs" / "icon.ico"
    icon_path.parent.mkdir(parents=True)
    icon_path.write_bytes(b"icon")
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass_root), raising=False)

    assert resolve_app_icon_path() == icon_path


def test_gui_import_does_not_create_qapplication():
    before = QApplication.instance()
    script_globals = runpy.run_path(str(ROOT / "src" / "texbook" / "gui" / "app.py"))

    assert "main" in script_globals
    assert QApplication.instance() is before


def test_create_application_sets_metadata():
    app = create_application(["texbook-gui-test"])

    assert app.applicationName() == APP_DISPLAY_NAME
    assert app.organizationName() == APP_ORGANIZATION_NAME
    assert not app.windowIcon().isNull()
    assert app.font().pointSize() == GUI_BASE_FONT_POINT_SIZE
    assert "Microsoft YaHei UI" in app.font().families()

    app.quit()


def test_gui_stylesheet_uses_application_font_without_small_body_px():
    stylesheet = build_fluent_stylesheet(GuiThemeMode.light)

    assert "font-size: 13px" not in stylesheet
    assert "font-size: 17pt" in stylesheet
    assert "font-size: 12pt" in stylesheet
    assert "min-height: 28px" in stylesheet


def test_gui_dark_stylesheet_covers_dialog_and_choice_groups():
    stylesheet = build_fluent_stylesheet(GuiThemeMode.dark)

    assert "QComboBox" not in stylesheet
    assert "background: #24272d" in stylesheet
    assert "color: #edf1f7" in stylesheet
    assert "QDialog#aboutDialog" in stylesheet
    assert "QDialog#settingsDialog" in stylesheet
    assert "QDialog QLabel" in stylesheet
    assert "QMessageBox QLabel" in stylesheet
    assert "QCheckBox {".replace(" ", "") not in ""  # keep parser quiet
    assert "QCheckBox" in stylesheet
    assert "padding-left: 2px" in stylesheet
    assert 'QWidget[choiceGroup="true"]:disabled QCheckBox' in stylesheet
    assert "QScrollArea#taskListScrollArea" in stylesheet
    assert "QWidget#taskRowsContainer" in stylesheet


def test_gui_stylesheet_sets_settings_scroll_background_for_both_themes():
    light = build_fluent_stylesheet(GuiThemeMode.light)
    dark = build_fluent_stylesheet(GuiThemeMode.dark)

    for stylesheet, background in [(light, "#f6f7f9"), (dark, "#17191d")]:
        assert "QScrollArea#settingsScrollArea" in stylesheet
        assert "QScrollArea#settingsScrollArea QWidget#settingsViewport" in stylesheet
        assert "QWidget#settingsPane," in stylesheet
        assert "QWidget#parametersPanel" in stylesheet
        assert f"background: {background}" in stylesheet


def test_main_window_has_basic_lifecycle_shell():
    app = create_application(["texbook-gui-test"])
    window = MainWindow()

    assert isinstance(window, QMainWindow)
    assert window.windowTitle() == APP_WINDOW_TITLE
    assert not window.windowIcon().isNull()
    assert isinstance(window.centralWidget(), ConversionMainPanel)
    assert window.menuBar().actions()
    assert window.statusBar().currentMessage() == "请选择 PDF 输入和产物目录"

    window.close()
    app.quit()


def test_main_window_about_action_opens_about_dialog(monkeypatch):
    app = create_application(["texbook-gui-test"])
    window = MainWindow()

    help_menu_action = window.menuBar().actions()[1]
    help_actions = help_menu_action.menu().actions()
    assert [action.text() for action in help_actions] == ["输出日志", "关于 TeXBook"]
    about_action = help_actions[1]

    assert about_action.isEnabled() is True
    assert about_action.text() == "关于 TeXBook"

    about_dialogs = []

    def fake_exec(self):
        about_dialogs.append(self)
        assert isinstance(self, AboutDialog)
        assert self.windowTitle() == "关于 TeXBook"
        assert not self.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
        assert not self.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint
        text = "\n".join(label.text() for label in self.findChildren(QLabel))
        assert "Windows 桌面应用" in text
        assert "WSLg" not in text
        assert "任务队列会展示阶段、进度、缓存命中、重试、失败原因和完成结果" in text
        assert "GUI 只" not in text
        assert "LaTeX 编译" not in text
        return 0

    monkeypatch.setattr(AboutDialog, "exec", fake_exec)

    about_action.trigger()

    assert len(about_dialogs) == 1
    assert about_dialogs[0].parent() is window

    window.close()
    app.quit()


def test_main_window_output_log_action_exports_session_log(tmp_path):
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    panel = window.centralWidget()
    assert isinstance(panel, ConversionMainPanel)
    export_path = tmp_path / "logs" / "session.jsonl"

    assert panel.findChild(QLineEdit, "logFilePathField").text() == default_gui_log_file_path()
    assert not export_path.exists()

    panel.set_path_memory(GuiPathMemory(last_log_file_path=str(export_path)))
    panel._diagnostic_log.record("manual_event", message="hello")
    window._export_log_action.trigger()

    assert export_path.exists()
    events = [json.loads(line) for line in export_path.read_text(encoding="utf-8").splitlines()]
    assert any(event["kind"] == "manual_event" and event["message"] == "hello" for event in events)
    assert any(event["kind"] == "log_exported" and event["path"] == str(export_path) for event in events)
    assert window.statusBar().currentMessage() == f"日志已输出：{export_path}"

    window.close()
    app.quit()


def test_conversion_panel_log_path_browse_uses_save_file_dialog(tmp_path, monkeypatch):
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()
    selected = tmp_path / "logs" / "texbook.jsonl"
    captured = []

    monkeypatch.setattr(
        "texbook.gui.main_panel.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: captured.append(args[2]) or (str(selected), ""),
    )

    panel.findChild(QToolButton, "logFileBrowseButton").click()

    assert captured == [default_gui_log_file_path()]
    assert panel.findChild(QLineEdit, "logFilePathField").text() == str(selected)
    assert panel.current_path_memory().last_log_file_path == str(selected)
    assert not selected.exists()

    panel.close()
    app.quit()


def test_main_window_closes_transient_dialogs_on_state_changes(monkeypatch):
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    dialog = SettingsDialog(window, preferences=GuiDisplayPreferences())
    closed = []
    monkeypatch.setattr(dialog, "close", lambda: closed.append(True) or True)
    window._open_dialogs.append(dialog)

    window.changeEvent(QEvent(QEvent.Type.WindowStateChange))

    assert closed == [True]

    app.quit()


def test_settings_dialog_updates_font_size_preference():
    app = create_application(["texbook-gui-test"])
    dialog = SettingsDialog(
        preferences=GuiDisplayPreferences(
            theme=GuiThemeMode.dark,
            language=GuiLanguage.zh_CN,
            font_point_size=13,
        )
    )

    assert dialog.windowTitle() == "GUI 设置"
    assert not dialog.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
    assert not dialog.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint
    assert dialog.findChild(QWidget, "settingsFontFamilyCombo") is None

    dialog.findChild(QSpinBox, "settingsFontSizeSpinBox").setValue(15)
    dialog._accept()
    preferences = dialog.selected_preferences()

    assert preferences.theme == GuiThemeMode.dark
    assert preferences.language == GuiLanguage.zh_CN
    assert preferences.font_point_size == 15

    dialog.close()
    app.quit()


def test_settings_dialog_reset_button_resets_only_display_font_size():
    app = create_application(["texbook-gui-test"])
    dialog = SettingsDialog(
        preferences=GuiDisplayPreferences(
            theme=GuiThemeMode.dark,
            language=GuiLanguage.zh_CN,
            font_point_size=15,
        )
    )

    dialog.findChild(QPushButton, "settingsResetButton").click()
    dialog._accept()
    preferences = dialog.selected_preferences()

    assert preferences.theme == GuiThemeMode.dark
    assert preferences.language == GuiLanguage.zh_CN
    assert preferences.font_point_size == GUI_BASE_FONT_POINT_SIZE

    dialog.close()
    app.quit()


def test_scroll_sensitive_controls_always_ignore_wheel_changes():
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()
    spin = panel.findChild(QSpinBox, "chunkPagesSpinBox")

    spin.setFocus()
    spin_event = _FakeWheelEvent()

    spin.wheelEvent(spin_event)

    assert spin_event.ignored is True
    assert spin.value() == 4

    spin.clearFocus()
    second_spin_event = _FakeWheelEvent()

    spin.wheelEvent(second_spin_event)

    assert second_spin_event.ignored is True
    assert spin.value() == 4

    panel.close()
    app.quit()


def test_clicking_non_field_widget_clears_control_focus():
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()
    label = panel.findChild(QLabel, "taskEmptyTitleLabel")

    class FakeFocusWidget:
        def __init__(self):
            self.cleared = False

        def clearFocus(self):
            self.cleared = True

    fake_focus = FakeFocusWidget()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("texbook.gui.main_panel.QApplication.focusWidget", lambda: fake_focus)

    panel.eventFilter(label, QEvent(QEvent.Type.MouseButtonPress))

    assert fake_focus.cleared is True

    monkeypatch.undo()

    panel.close()
    app.quit()


def test_main_window_settings_button_applies_font_size_preference(monkeypatch):
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    panel = window.centralWidget()
    assert isinstance(panel, ConversionMainPanel)

    monkeypatch.setattr(
        "texbook.gui.main_window.SettingsDialog.exec",
        lambda self: self.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        "texbook.gui.main_window.SettingsDialog.selected_preferences",
        lambda self: GuiDisplayPreferences(
            theme=GuiThemeMode.light,
            language=GuiLanguage.zh_CN,
            font_point_size=14,
        ),
    )

    panel.findChild(QToolButton, "settingsButton").click()

    assert panel.current_display_preferences().font_point_size == 14
    assert app.font().pointSize() == 14
    assert 'font-family: "Microsoft YaHei UI"' in window.styleSheet()

    window.close()
    app.quit()


def test_pyinstaller_spec_references_gui_entry_and_icon():
    spec_text = (ROOT / "packaging" / "texbook-gui.spec").read_text(encoding="utf-8")

    assert "ROOT = Path(SPECPATH).parent" in spec_text
    assert "src\" / \"texbook\" / \"gui\" / \"__main__.py" in spec_text
    assert "docs\" / \"icon.ico" in spec_text
    assert "datas=[(str(ICON_PATH), \"docs\")]" in spec_text
    assert 'name="TeXBook"' in spec_text
    assert "icon=str(ICON_PATH)" in spec_text
    assert "console=False" in spec_text


def test_pyinstaller_spec_does_not_bundle_latex_build_configuration():
    spec_text = (ROOT / "packaging" / "texbook-gui.spec").read_text(encoding="utf-8")

    assert ".latexmkrc" not in spec_text
    assert "post-build.sh" not in spec_text
    assert "latexmk" not in spec_text


def test_main_window_shows_conversion_tool_panel_immediately():
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    central = window.centralWidget()

    assert isinstance(central, ConversionMainPanel)
    assert central.objectName() == "conversionMainPanel"
    assert central.findChild(QWidget, "inputPanel") is not None
    assert central.findChild(QWidget, "outputPanel") is not None
    assert central.findChild(QWidget, "parametersPanel") is not None
    assert central.findChild(QWidget, "taskListPanel") is not None
    assert central.findChild(QWidget, "startButton") is not None
    assert central.findChild(QWidget, "addTaskButton") is not None
    assert central.findChild(QWidget, "emptyCentralWidget") is None

    window.close()
    app.quit()


def test_conversion_panel_exposes_future_task_control_points():
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()

    for object_name in [
        "pdfInputField",
        "pdfInputBrowseButton",
        "outputDirectoryField",
        "outputBrowseButton",
        "settingsScrollArea",
        "settingsViewport",
        "settingsPane",
        "inputTypeChoices",
        "outputKindChoices",
        "batchPatternField",
        "pageOptionsPanel",
        "pagesField",
        "manualTitleField",
        "titleSourceChoices",
        "documentClassChoices",
        "structureModeChoices",
        "structureChunkPagesSpinBox",
        "structureMaxPagesSpinBox",
        "beamerTitlePageCheckBox",
        "showDateCheckBox",
        "modelField",
        "baseUrlField",
        "apiKeyField",
        "apiKeySourceChoices",
        "promptPresetField",
        "extraPromptEdit",
        "cacheEnabledCheckBox",
        "cacheDirectoryField",
        "cacheBrowseButton",
        "clearCacheCheckBox",
        "chunkPagesSpinBox",
        "prefetchChunksSpinBox",
        "llmConcurrencySpinBox",
        "llmIntervalSpinBox",
        "batchWorkersSpinBox",
        "advancedOptionsPanel",
        "imageDpiSpinBox",
        "imageDpiMinSpinBox",
        "imageDpiMaxSpinBox",
        "imageFormatChoices",
        "jpegQualitySpinBox",
        "llmRetriesSpinBox",
        "llmRetryInitialDelaySpinBox",
        "llmRetryMaxDelaySpinBox",
        "timeoutSpinBox",
        "temperatureSpinBox",
        "maxTokensSpinBox",
        "beamerBoxStyleChoices",
        "ctexFontProfileChoices",
        "overallProgressBar",
        "taskEmptyStatusLabel",
        "taskListScrollArea",
        "taskRowsContainer",
    ]:
        assert panel.findChild(QWidget, object_name) is not None

    panel.close()
    app.quit()


def test_conversion_panel_uses_inline_choice_groups_instead_of_dropdowns():
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()

    for object_name in [
        "inputTypeChoices",
        "outputKindChoices",
        "titleSourceChoices",
        "documentClassChoices",
        "structureModeChoices",
        "apiKeySourceChoices",
        "imageFormatChoices",
        "beamerBoxStyleChoices",
        "ctexFontProfileChoices",
    ]:
        choices = _choice(panel, object_name)
        for option in choices.option_buttons():
            index = choices.layout().indexOf(option)
            row, column, _row_span, _column_span = choices.layout().getItemPosition(index)
            assert 0 <= column <= 2
            assert row >= 0
            assert isinstance(option, QCheckBox)

    assert len(_choice(panel, "documentClassChoices").option_buttons()) == 7
    assert panel.findChildren(ChoiceGrid)
    assert "Combo" not in "".join(child.objectName() for child in panel.findChildren(QWidget))

    panel.close()
    app.quit()


def test_choice_group_styleshift_leaves_indicator_left_padding():
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()

    stylesheet = panel.styleSheet()
    assert "padding-left: 2px" in stylesheet

    for checkbox in panel.findChildren(QCheckBox):
        checkbox.resize(checkbox.sizeHint())
        checkbox.ensurePolished()
        opt = QStyleOptionButton()
        checkbox.initStyleOption(opt)
        indicator = checkbox.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator,
            opt,
            checkbox,
        )
        assert indicator.x() >= 1

    panel.close()
    app.quit()


def test_conversion_panel_default_settings_match_cli_defaults():
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()

    settings = panel.current_settings()

    assert settings.conversion_mode == GuiConversionMode.tex_file
    assert settings.output_kind == GuiOutputKind.tex_file
    assert settings.confirm_overwrite is True
    assert settings.batch_pattern == "*.pdf"
    assert settings.pages == ""
    assert settings.document_class == "auto"
    assert settings.structure_mode == "auto"
    assert settings.structure_chunk_pages == 8
    assert settings.structure_max_pages == 32
    assert settings.manual_title == ""
    assert settings.title_source == "filename"
    assert settings.show_date is False
    assert settings.beamer_title_page is True
    assert settings.prompt_preset == "math"
    assert settings.temperature == 1.0
    assert settings.timeout_seconds is None
    assert settings.max_tokens == 128000
    assert settings.cache_enabled is True
    assert settings.cache_directory == default_gui_cache_directory()
    assert settings.cache_directory != LEGACY_GUI_CACHE_DIRECTORY
    assert settings.clear_cache is False
    assert settings.chunk_pages == 4
    assert settings.prefetch_chunks == 1
    assert settings.llm_max_concurrency == 1
    assert settings.llm_min_request_interval == 0.0
    assert settings.batch_workers == 1
    assert settings.image_dpi == 160
    assert settings.image_dpi_min == 100
    assert settings.image_dpi_max is None
    assert settings.image_format == "png"
    assert settings.jpeg_quality == 85
    assert settings.llm_retries == 2
    assert settings.llm_retry_initial_delay == 2.0
    assert settings.llm_retry_max_delay == 30.0
    assert settings.beamer_box_style == "block"
    assert settings.ctex_font_profile == "default"
    assert panel.validate_settings() == []
    preset_field = panel.findChild(QLineEdit, "promptPresetField")
    assert preset_field is not None
    assert preset_field.text() == "math"
    assert preset_field.isReadOnly() is True
    assert panel.findChild(QLineEdit, "cacheDirectoryField").text() == default_gui_cache_directory()
    assert (
        panel.findChild(QLineEdit, "cacheDirectoryField").placeholderText()
        == default_gui_cache_directory()
    )

    panel.close()
    app.quit()


def test_conversion_panel_settings_round_trip_preserves_all_fields():
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()
    panel.set_settings(
        GuiConversionSettings(
            conversion_mode=GuiConversionMode.project,
            confirm_overwrite=False,
            batch_pattern="slides-*.pdf",
            pages="1,3-5",
            document_class="ctexbeamer",
            structure_mode="llm",
            structure_chunk_pages=6,
            structure_max_pages=48,
            manual_title="手动标题",
            title_source="filename",
            show_date=True,
            beamer_title_page=False,
            model="vision-model",
            base_url="https://api.example/v1",
            api_key="secret",
            prompt_preset="custom-old",
            extra_prompt="只提取公式",
            temperature=0.7,
            timeout_seconds=120.0,
            max_tokens=64000,
            cache_enabled=True,
            cache_directory=r"D:\tex-cache",
            clear_cache=True,
            chunk_pages=3,
            prefetch_chunks=2,
            llm_max_concurrency=2,
            llm_min_request_interval=1.5,
            batch_workers=4,
            image_dpi=180,
            image_dpi_min=120,
            image_dpi_max=240,
            image_format="jpeg",
            jpeg_quality=92,
            llm_retries=4,
            llm_retry_initial_delay=3.5,
            llm_retry_max_delay=45.0,
            beamer_box_style="tcolorbox",
            ctex_font_profile="local",
        )
    )

    settings = panel.current_settings()

    assert settings.conversion_mode == GuiConversionMode.project
    assert settings.output_kind == GuiOutputKind.project
    assert settings.confirm_overwrite is False
    assert settings.batch_pattern == "slides-*.pdf"
    assert settings.pages == "1,3-5"
    assert settings.document_class == "ctexbeamer"
    assert settings.structure_mode == "llm"
    assert settings.structure_chunk_pages == 6
    assert settings.structure_max_pages == 48
    assert settings.manual_title == "手动标题"
    assert settings.title_source == "filename"
    assert settings.show_date is True
    assert settings.beamer_title_page is False
    assert settings.prompt_preset == "math"
    assert panel.findChild(QLineEdit, "promptPresetField").text() == "math"
    assert panel.findChild(QLineEdit, "promptPresetField").isReadOnly() is True
    assert settings.model == "vision-model"
    assert settings.base_url == "https://api.example/v1"
    assert settings.api_key == "secret"
    assert settings.api_key_source == GuiApiKeySource.direct
    assert settings.extra_prompt == "只提取公式"
    assert settings.temperature == 0.7
    assert settings.timeout_seconds == 120.0
    assert settings.max_tokens == 64000
    assert settings.cache_directory == r"D:\tex-cache"
    assert settings.clear_cache is True
    assert settings.chunk_pages == 3
    assert settings.prefetch_chunks == 2
    assert settings.llm_max_concurrency == 2
    assert settings.llm_min_request_interval == 1.5
    assert settings.batch_workers == 4
    assert settings.image_dpi == 180
    assert settings.image_dpi_min == 120
    assert settings.image_dpi_max == 240
    assert settings.image_format == "jpeg"
    assert settings.jpeg_quality == 92
    assert settings.llm_retries == 4
    assert settings.llm_retry_initial_delay == 3.5
    assert settings.llm_retry_max_delay == 45.0
    assert settings.beamer_box_style == "tcolorbox"
    assert settings.ctex_font_profile == "local"

    panel.close()
    app.quit()


def test_conversion_panel_supports_environment_api_key_source(monkeypatch):
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()

    _set_choice(panel, "apiKeySourceChoices", GuiApiKeySource.environment.value)
    panel.findChild(QLineEdit, "apiKeyField").setText("TEXBOOK_GUI_TEST_KEY")

    settings = panel.current_settings()

    assert settings.api_key_source == GuiApiKeySource.environment
    assert settings.api_key == "TEXBOOK_GUI_TEST_KEY"
    assert panel.findChild(QLineEdit, "apiKeyField").echoMode() == QLineEdit.EchoMode.Password
    assert "API Key 环境变量不存在：TEXBOOK_GUI_TEST_KEY。" in validate_gui_settings(settings)

    monkeypatch.setenv("TEXBOOK_GUI_TEST_KEY", "secret")

    assert validate_gui_settings(settings) == []

    panel.close()
    app.quit()


def test_conversion_panel_requires_environment_api_key_name_when_using_environment_source():
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()
    _set_choice(panel, "apiKeySourceChoices", GuiApiKeySource.environment.value)
    panel.findChild(QLineEdit, "apiKeyField").setText("")

    settings = panel.current_settings()

    assert "API Key 环境变量名不能为空。" in panel.validate_settings()
    with pytest.raises(GuiCoreAdapterError, match="API Key 环境变量名不能为空。"):
        build_core_conversion_bundle(settings, repo_root=ROOT)

    panel.close()
    app.quit()


def test_conversion_panel_validates_pages_and_title_conflict():
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    panel = window.centralWidget()

    panel.findChild(QLineEdit, "pagesField").setText("5-3")

    assert "页面范围格式无效" in panel.validate_settings()[0]
    assert window.statusBar().currentMessage().startswith("页面范围格式无效")

    panel.findChild(QLineEdit, "pagesField").setText("1,3-5")
    panel.findChild(QLineEdit, "manualTitleField").setText("手动标题")
    _set_choice(panel, "titleSourceChoices", "llm")

    assert panel.findChild(QLineEdit, "manualTitleField").isEnabled() is False
    assert "手动标题不能与 LLM 标题来源同时使用。" in panel.validate_settings()

    window.close()
    app.quit()


def test_conversion_panel_option_dependencies_follow_mode_cache_and_image_format():
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()

    assert _choice(panel, "structureModeChoices").isEnabled() is False
    assert panel.findChild(QSpinBox, "structureChunkPagesSpinBox").isEnabled() is False
    assert panel.findChild(QSpinBox, "batchWorkersSpinBox").isEnabled() is False
    assert panel.findChild(QLineEdit, "batchPatternField").isEnabled() is False

    _set_choice(panel, "outputKindChoices", GuiOutputKind.project.value)

    assert panel.current_settings().conversion_mode == GuiConversionMode.project
    assert _choice(panel, "structureModeChoices").isEnabled() is True
    assert panel.findChild(QSpinBox, "structureChunkPagesSpinBox").isEnabled() is True
    assert panel.findChild(QSpinBox, "batchWorkersSpinBox").isEnabled() is False

    _set_choice(panel, "inputTypeChoices", GuiInputKind.directory.value)

    assert panel.current_settings().conversion_mode == GuiConversionMode.project
    assert _choice(panel, "structureModeChoices").isEnabled() is True
    assert panel.findChild(QSpinBox, "batchWorkersSpinBox").isEnabled() is True
    assert panel.findChild(QLineEdit, "batchPatternField").isEnabled() is True

    panel.findChild(QCheckBox, "clearCacheCheckBox").setChecked(True)
    assert panel.current_settings().clear_cache is True

    panel.findChild(QCheckBox, "cacheEnabledCheckBox").setChecked(False)

    assert panel.findChild(QLineEdit, "cacheDirectoryField").isEnabled() is False
    assert panel.findChild(QToolButton, "cacheBrowseButton").isEnabled() is False
    assert panel.findChild(QCheckBox, "clearCacheCheckBox").isEnabled() is False
    assert panel.current_settings().clear_cache is False

    _set_choice(panel, "imageFormatChoices", "png")

    assert panel.findChild(QSpinBox, "jpegQualitySpinBox").isEnabled() is False

    _set_choice(panel, "imageFormatChoices", "jpeg")

    assert panel.findChild(QSpinBox, "jpegQualitySpinBox").isEnabled() is True

    panel.close()
    app.quit()


def test_beamer_title_page_option_is_editable_for_auto_and_beamer_document_classes():
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()
    checkbox = panel.findChild(QCheckBox, "beamerTitlePageCheckBox")
    assert checkbox is not None

    expected_enabled = {
        "auto": True,
        "ctexart": False,
        "ctexbook": False,
        "ctexbeamer": True,
        "article": False,
        "book": False,
        "beamer": True,
    }

    for document_class, enabled in expected_enabled.items():
        _set_choice(panel, "documentClassChoices", document_class)
        assert checkbox.isEnabled() is enabled

    panel.close()
    app.quit()


def test_conversion_panel_batch_pattern_only_validates_for_directory_input():
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()

    panel.findChild(QLineEdit, "batchPatternField").setText("")

    assert "目录批量匹配模式不能为空。" not in panel.validate_settings()
    _set_choice(panel, "inputTypeChoices", GuiInputKind.directory.value)

    assert "目录批量匹配模式不能为空。" in panel.validate_settings()
    _set_choice(panel, "inputTypeChoices", GuiInputKind.multiple_files.value)

    assert "目录批量匹配模式不能为空。" not in panel.validate_settings()

    panel.close()
    app.quit()


def test_conversion_panel_theme_and_language_switch_keep_stable_values():
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    panel = window.centralWidget()
    assert isinstance(panel, ConversionMainPanel)

    _set_choice(panel, "inputTypeChoices", GuiInputKind.directory.value)
    _set_choice(panel, "outputKindChoices", GuiOutputKind.project.value)
    _set_choice(panel, "apiKeySourceChoices", GuiApiKeySource.environment.value)
    assert panel.current_settings().output_kind == GuiOutputKind.project
    assert panel.current_settings().api_key_source == GuiApiKeySource.environment

    panel.findChild(QToolButton, "themeButton").click()

    assert panel.current_display_preferences().theme == GuiThemeMode.dark
    assert "#17191d" in window.styleSheet()
    assert panel.findChild(QToolButton, "themeButton").text() == "暗色"

    panel.findChild(QToolButton, "languageButton").click()

    assert panel.current_display_preferences().language == GuiLanguage.en_US
    assert window.windowTitle() == "TeXBook PDF to LaTeX"
    assert panel.findChild(QPushButton, "addTaskButton").text() == "Add Task"
    assert _choice_value(panel, "inputTypeChoices") == GuiInputKind.directory.value
    assert _choice_value(panel, "outputKindChoices") == GuiOutputKind.project.value
    assert _choice(panel, "inputTypeChoices").option_buttons()[2].text() == "Folder Batch"
    assert _choice(panel, "outputKindChoices").option_buttons()[1].text() == "Project Folder"
    assert panel.current_settings().output_kind == GuiOutputKind.project
    assert _choice(panel, "apiKeySourceChoices").option_buttons()[1].text() == "Environment variable"
    assert panel.current_settings().api_key_source == GuiApiKeySource.environment
    assert window.statusBar().currentMessage() == "API Key environment variable name cannot be empty."

    window.close()
    app.quit()


def test_reset_defaults_keeps_theme_language_paths_and_tasks(monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    panel = window.centralWidget()
    assert isinstance(panel, ConversionMainPanel)

    panel.set_display_preferences(
        GuiDisplayPreferences(
            theme=GuiThemeMode.dark,
            language=GuiLanguage.en_US,
            font_point_size=14,
        ),
        emit=True,
    )
    panel.set_path_memory(GuiPathMemory(last_input_directory=r"C:\books"))
    panel.findChild(QLineEdit, "pagesField").setText("1-2")
    _set_choice(panel, "outputKindChoices", GuiOutputKind.project.value)
    panel.set_input_selection(GuiInputSelection.from_single_file("a.pdf"))
    panel.set_output_directory("out/a.tex")
    _fill_required_task_fields(panel)
    panel.findChild(QPushButton, "addTaskButton").click()
    assert len(panel.task_view_states()) == 1

    panel.findChild(QToolButton, "resetDefaultsButton").click()

    preferences = panel.current_display_preferences()
    assert preferences.theme == GuiThemeMode.dark
    assert preferences.language == GuiLanguage.en_US
    assert preferences.font_point_size == 14
    assert panel.current_path_memory().last_input_directory == r"C:\books"
    assert len(panel.task_view_states()) == 1
    assert panel.current_settings().pages == ""
    assert panel.current_settings().output_kind == GuiOutputKind.tex_file
    assert panel.selection_state == GuiPathSelectionState()

    window.close()
    app.quit()


def test_conversion_panel_english_validation_and_task_text(monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    panel = window.centralWidget()
    assert isinstance(panel, ConversionMainPanel)
    panel.findChild(QToolButton, "languageButton").click()

    panel.findChild(QLineEdit, "pagesField").setText("5-3")

    assert panel.validate_settings()[0].startswith("Invalid page range")
    assert window.statusBar().currentMessage().startswith("Invalid page range")

    panel.findChild(QLineEdit, "pagesField").setText("")
    panel.set_input_selection(GuiInputSelection.from_multiple_files(["a.pdf", "b.pdf"]))
    panel.set_output_directory("out")
    _fill_required_task_fields(panel)
    panel.findChild(QPushButton, "addTaskButton").click()
    states = panel.task_view_states()

    assert panel.findChild(QLabel, f"taskStatusBadge_{states[0].task_id}").text() == "Pending"
    assert panel.findChild(QLabel, f"taskStageLabel_{states[0].task_id}").text() == "Stage: Waiting"
    assert panel.findChild(QLabel, f"taskCacheLabel_{states[0].task_id}").text() == "Cache hits 0"
    assert panel.findChild(QLabel, "taskEmptyStatusLabel").text() == "2 task(s) created"

    first_id = states[0].task_id
    panel.handle_task_progress(
        first_id,
        ProgressEvent(kind="cache_hit", operation="chunk", label="chunk 1/2"),
    )

    assert "Cache hit: chunk 1/2" in panel.findChild(
        QLabel,
        f"taskStageLabel_{first_id}",
    ).text()

    panel.update_task_state(
        first_id,
        GuiTaskRuntimeUpdate(
            status=GuiTaskStatus.completed,
            stage=GuiTaskStage.completed,
            progress=100,
            result="out/a.tex",
        ),
    )

    assert panel.findChild(QLabel, f"taskStatusBadge_{first_id}").text() == "Completed"
    assert panel.findChild(QLabel, f"taskResultLabel_{first_id}").text() == "Result: out/a.tex"

    window.close()
    app.quit()


def test_conversion_panel_cache_directory_browse_updates_settings(monkeypatch):
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()
    captured = []

    monkeypatch.setattr(
        "texbook.gui.main_panel.QFileDialog.getExistingDirectory",
        lambda *args, **kwargs: captured.append(args[2]) or r"D:\tex-cache",
    )

    panel.findChild(QToolButton, "cacheBrowseButton").click()

    assert captured == [default_gui_cache_directory()]
    assert panel.findChild(QLineEdit, "cacheDirectoryField").text() == r"D:\tex-cache"
    assert panel.current_settings().cache_directory == r"D:\tex-cache"

    panel.close()
    app.quit()


def test_gui_settings_store_round_trips_persistent_settings(tmp_path):
    store = _gui_settings_store(tmp_path)
    state = GuiPersistentState(
        settings=GuiConversionSettings(
            output_kind=GuiOutputKind.project,
            confirm_overwrite=False,
            batch_pattern="slides-*.pdf",
            pages="1,3-5",
            document_class="ctexbeamer",
            structure_mode="llm",
            structure_chunk_pages=6,
            structure_max_pages=48,
            manual_title="手动标题",
            title_source="filename",
            show_date=True,
            beamer_title_page=False,
            model="vision-model",
            base_url="https://api.example/v1",
            api_key="TEXBOOK_GUI_KEY",
            api_key_source=GuiApiKeySource.environment,
            prompt_preset="math",
            extra_prompt="只提取公式",
            temperature=0.7,
            timeout_seconds=120.0,
            max_tokens=64000,
            cache_enabled=True,
            cache_directory=r"D:\tex-cache",
            clear_cache=True,
            chunk_pages=3,
            prefetch_chunks=2,
            llm_max_concurrency=2,
            llm_min_request_interval=1.5,
            batch_workers=4,
            image_dpi=180,
            image_dpi_min=120,
            image_dpi_max=240,
            image_format="jpeg",
            jpeg_quality=92,
            llm_retries=4,
            llm_retry_initial_delay=3.5,
            llm_retry_max_delay=45.0,
            beamer_box_style="tcolorbox",
            ctex_font_profile="local",
        ),
        path_memory=GuiPathMemory(
            last_input_directory=r"C:\books",
            last_output_directory=r"D:\tex-output",
            last_cache_directory=r"D:\tex-cache",
            last_log_file_path=r"D:\tex-logs\texbook.jsonl",
        ),
    )

    store.save_state(state)
    loaded = store.load_state()

    assert loaded.settings.output_kind == GuiOutputKind.project
    assert loaded.settings.confirm_overwrite is False
    assert loaded.settings.batch_pattern == "slides-*.pdf"
    assert loaded.settings.pages == "1,3-5"
    assert loaded.settings.document_class == "ctexbeamer"
    assert loaded.settings.structure_mode == "llm"
    assert loaded.settings.api_key == "TEXBOOK_GUI_KEY"
    assert loaded.settings.api_key_source == GuiApiKeySource.environment
    assert loaded.settings.timeout_seconds == 120.0
    assert loaded.settings.image_dpi_max == 240
    assert loaded.settings.clear_cache is False
    assert loaded.settings.path_state == GuiPathSelectionState()
    assert loaded.path_memory.last_input_directory == r"C:\books"
    assert loaded.path_memory.last_output_directory == r"D:\tex-output"
    assert loaded.path_memory.last_cache_directory == r"D:\tex-cache"
    assert loaded.path_memory.last_log_file_path == r"D:\tex-logs\texbook.jsonl"


def test_gui_settings_store_migrates_legacy_default_cache_directory(tmp_path):
    store = _gui_settings_store(tmp_path)

    assert store.load_conversion_settings().cache_directory == default_gui_cache_directory()

    store._settings.setValue("cache/directory", LEGACY_GUI_CACHE_DIRECTORY)
    assert store.load_conversion_settings().cache_directory == default_gui_cache_directory()

    store._settings.setValue("cache/directory", r"D:\tex-cache")
    assert store.load_conversion_settings().cache_directory == r"D:\tex-cache"


def test_gui_settings_store_ignores_persisted_prompt_preset(tmp_path):
    store = _gui_settings_store(tmp_path)
    store._settings.setValue("model/prompt_preset", "custom-old")

    loaded = store.load_conversion_settings()

    assert loaded.prompt_preset == "math"


def test_gui_settings_store_round_trips_display_preferences(tmp_path):
    store = _gui_settings_store(tmp_path)
    state = GuiPersistentState(
        settings=GuiConversionSettings(),
        path_memory=GuiPathMemory(),
        display_preferences=GuiDisplayPreferences(
            theme=GuiThemeMode.dark,
            language=GuiLanguage.en_US,
            font_point_size=13,
        ),
    )

    store.save_state(state)
    loaded = store.load_state()

    assert loaded.display_preferences.theme == GuiThemeMode.dark
    assert loaded.display_preferences.language == GuiLanguage.en_US
    assert loaded.display_preferences.font_point_size == 13

    raw = store._settings
    raw.setValue("display/theme", "invalid")
    raw.setValue("display/language", "invalid")
    raw.setValue("display/font_family", "Ignored Legacy Font")
    raw.setValue("display/font_point_size", 4)

    loaded = store.load_state()

    assert loaded.display_preferences == GuiDisplayPreferences()


def test_gui_settings_store_falls_back_from_invalid_values(tmp_path):
    store = _gui_settings_store(tmp_path)
    raw = store._settings
    raw.setValue("conversion/output_kind", "invalid")
    raw.setValue("model/api_key_source", "invalid")
    raw.setValue("runtime/batch_workers", "bad")

    loaded = store.load_conversion_settings()

    assert loaded.output_kind == GuiOutputKind.tex_file
    assert loaded.api_key_source == GuiApiKeySource.direct
    assert loaded.batch_workers == 1


def test_gui_input_selection_accepts_windows_paths_and_filters_non_pdf():
    selection = GuiInputSelection.from_multiple_files(
        [
            r"C:\docs\a.pdf",
            r"C:\docs\ignore.txt",
            r"C:\docs\a.pdf",
            r"\\server\share\b.PDF",
        ]
    )

    assert selection.kind == GuiInputKind.multiple_files
    assert selection.paths == (r"C:\docs\a.pdf", r"\\server\share\b.PDF")
    assert selection.display_text() == r"C:\docs\a.pdf 等 2 个文件"


def test_conversion_panel_single_pdf_and_output_directory_enable_add_task(monkeypatch):
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()

    monkeypatch.setattr(
        "texbook.gui.main_panel.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: (r"C:\books\lecture.pdf", "PDF 文件 (*.pdf)"),
    )
    monkeypatch.setattr(
        "texbook.gui.main_panel.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (r"D:\tex-output\lecture.tex", "LaTeX 文件 (*.tex)"),
    )

    panel.findChild(QToolButton, "pdfInputBrowseButton").click()

    assert panel.selection_state.input_selection.kind == GuiInputKind.single_file
    assert panel.selection_state.input_selection.paths == (r"C:\books\lecture.pdf",)
    assert panel.findChild(QLineEdit, "pdfInputField").text() == r"C:\books\lecture.pdf"
    assert not panel.findChild(QPushButton, "addTaskButton").isEnabled()

    panel.findChild(QToolButton, "outputBrowseButton").click()

    assert panel.selection_state.output_directory == r"D:\tex-output\lecture.tex"
    assert panel.findChild(QLineEdit, "outputDirectoryField").text() == r"D:\tex-output\lecture.tex"
    assert not panel.findChild(QPushButton, "addTaskButton").isEnabled()

    _fill_required_task_fields(panel)

    assert panel.findChild(QPushButton, "addTaskButton").isEnabled()
    assert not panel.findChild(QPushButton, "startButton").isEnabled()

    panel.close()
    app.quit()


def test_conversion_panel_add_task_button_requires_model_key_and_batch_pattern():
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()
    add_button = panel.findChild(QPushButton, "addTaskButton")

    panel.set_input_selection(GuiInputSelection.from_single_file(r"C:\books\lecture.pdf"))
    panel.set_output_directory(r"D:\tex-output\lecture.tex")
    assert add_button.isEnabled() is False

    panel.findChild(QLineEdit, "modelField").setText("model")
    assert add_button.isEnabled() is False

    panel.findChild(QLineEdit, "apiKeyField").setText("key")
    assert add_button.isEnabled() is True

    panel.findChild(QLineEdit, "promptPresetField").setText("")
    assert panel.current_settings().prompt_preset == "math"
    assert add_button.isEnabled() is True
    panel.findChild(QLineEdit, "promptPresetField").setText("custom-old")
    assert panel.current_settings().prompt_preset == "math"
    assert add_button.isEnabled() is True

    _set_choice(panel, "apiKeySourceChoices", GuiApiKeySource.environment.value)
    panel.findChild(QLineEdit, "apiKeyField").setText("")
    assert add_button.isEnabled() is False

    panel.findChild(QLineEdit, "apiKeyField").setText("TEXBOOK_GUI_TEST_KEY")
    assert add_button.isEnabled() is True

    _set_choice(panel, "inputTypeChoices", GuiInputKind.directory.value)
    panel.set_input_selection(GuiInputSelection.from_directory(r"C:\books"))
    panel.findChild(QLineEdit, "batchPatternField").setText("")
    assert add_button.isEnabled() is False

    panel.findChild(QLineEdit, "batchPatternField").setText("*.pdf")
    assert add_button.isEnabled() is True

    panel.close()
    app.quit()


def test_conversion_panel_add_task_clears_input_and_output_paths(monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()

    panel.set_input_selection(GuiInputSelection.from_single_file(r"C:\books\lecture.pdf"))
    panel.set_output_directory(r"D:\tex-output\lecture.tex")
    panel.findChild(QLineEdit, "pagesField").setText("1-2")
    _fill_required_task_fields(panel)

    assert panel.current_path_memory().last_input_directory == r"C:\books"
    assert panel.current_path_memory().last_output_directory == r"D:\tex-output"

    panel.findChild(QPushButton, "addTaskButton").click()

    assert panel.findChild(QLineEdit, "pdfInputField").text() == ""
    assert panel.findChild(QLineEdit, "outputDirectoryField").text() == ""
    assert panel.findChild(QLineEdit, "pagesField").text() == ""
    assert panel.selection_state.input_selection.paths == ()
    assert panel.selection_state.output_directory == ""
    assert panel.current_path_memory().last_input_directory == r"C:\books"
    assert panel.current_path_memory().last_output_directory == r"D:\tex-output"
    assert panel.findChild(QPushButton, "addTaskButton").isEnabled() is False
    assert panel.findChild(QPushButton, "startButton").isEnabled() is True

    panel.close()
    app.quit()


def test_conversion_panel_uses_default_directory_for_first_input_dialog(monkeypatch):
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()
    captured = []

    monkeypatch.setattr(
        "texbook.gui.main_panel.system_default_dialog_directory",
        lambda: r"C:\Users\tester\Documents",
    )
    monkeypatch.setattr(
        "texbook.gui.main_panel.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: captured.append(args[2]) or ("", ""),
    )

    panel.findChild(QToolButton, "pdfInputBrowseButton").click()

    assert captured == [r"C:\Users\tester\Documents"]

    panel.close()
    app.quit()


def test_conversion_panel_remembers_input_and_output_dialog_directories(monkeypatch):
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()
    input_dirs = []
    output_dirs = []

    panel.set_input_selection(GuiInputSelection.from_single_file(r"C:\books\lecture.pdf"))
    panel.set_path_memory(
        panel.current_path_memory().remember_output_path(
            r"D:\tex-output",
            is_file=False,
        )
    )
    monkeypatch.setattr(
        "texbook.gui.main_panel.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: input_dirs.append(args[2]) or ("", ""),
    )
    monkeypatch.setattr(
        "texbook.gui.main_panel.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: output_dirs.append(args[2]) or ("", ""),
    )

    panel.findChild(QToolButton, "pdfInputBrowseButton").click()
    panel.findChild(QToolButton, "outputBrowseButton").click()

    assert input_dirs == [r"C:\books"]
    assert output_dirs == [r"D:\tex-output\lecture.tex"]

    panel.close()
    app.quit()


def test_conversion_panel_multiple_pdf_selection_is_filtered_deduped_and_summarized(
    monkeypatch,
):
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()
    _set_choice(panel, "inputTypeChoices", GuiInputKind.multiple_files.value)

    monkeypatch.setattr(
        "texbook.gui.main_panel.QFileDialog.getOpenFileNames",
        lambda *args, **kwargs: (
            [
                r"C:\docs\a.pdf",
                r"C:\docs\ignore.txt",
                r"C:\docs\a.pdf",
                r"\\server\share\b.PDF",
            ],
            "PDF 文件 (*.pdf)",
        ),
    )

    panel.findChild(QToolButton, "pdfInputBrowseButton").click()

    assert panel.selection_state.input_selection.kind == GuiInputKind.multiple_files
    assert panel.selection_state.input_selection.paths == (
        r"C:\docs\a.pdf",
        r"\\server\share\b.PDF",
    )
    assert panel.findChild(QLineEdit, "pdfInputField").text() == r"C:\docs\a.pdf 等 2 个文件"
    assert not panel.findChild(QPushButton, "addTaskButton").isEnabled()

    panel.close()
    app.quit()


def test_conversion_panel_directory_input_uses_directory_selection(monkeypatch):
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()
    _set_choice(panel, "inputTypeChoices", GuiInputKind.directory.value)

    monkeypatch.setattr(
        "texbook.gui.main_panel.QFileDialog.getExistingDirectory",
        lambda *args, **kwargs: r"\\server\share\pdfs",
    )

    panel.findChild(QToolButton, "pdfInputBrowseButton").click()

    assert panel.selection_state.input_selection.kind == GuiInputKind.directory
    assert panel.selection_state.input_selection.paths == (r"\\server\share\pdfs",)
    assert panel.findChild(QLineEdit, "pdfInputField").text() == r"\\server\share\pdfs"

    panel.close()
    app.quit()


def test_conversion_panel_switching_input_type_clears_previous_input_only():
    app = create_application(["texbook-gui-test"])
    panel = ConversionMainPanel()
    panel.set_input_selection(GuiInputSelection.from_single_file(r"C:\docs\a.pdf"))
    panel.set_output_directory(r"D:\tex-output")
    _fill_required_task_fields(panel)

    assert panel.findChild(QPushButton, "addTaskButton").isEnabled()

    _set_choice(panel, "inputTypeChoices", GuiInputKind.directory.value)

    assert panel.selection_state.input_selection.kind == GuiInputKind.directory
    assert panel.selection_state.input_selection.paths == ()
    assert panel.selection_state.output_directory == r"D:\tex-output"
    assert panel.findChild(QLineEdit, "pdfInputField").text() == ""
    assert not panel.findChild(QPushButton, "addTaskButton").isEnabled()

    panel.close()
    app.quit()


def test_main_window_status_bar_tracks_path_selection_state():
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    panel = window.centralWidget()

    assert window.statusBar().currentMessage() == "请选择 PDF 输入和产物目录"

    panel.set_input_selection(GuiInputSelection.from_single_file(r"C:\docs\a.pdf"))
    assert window.statusBar().currentMessage() == "已选择 PDF 输入，请选择产物目录"

    panel.set_output_directory(r"D:\tex-output")
    assert window.statusBar().currentMessage() == "已选择 PDF 输入和产物目录"

    window.close()
    app.quit()


def test_main_window_restores_and_saves_persistent_gui_state(tmp_path):
    store = _gui_settings_store(tmp_path)
    store.save_state(
        GuiPersistentState(
            settings=GuiConversionSettings(
                output_kind=GuiOutputKind.project,
                model="stored-model",
                api_key="stored-key",
                api_key_source=GuiApiKeySource.direct,
                pages="1-2",
                clear_cache=True,
            ),
            path_memory=GuiPathMemory(
                last_input_directory=r"C:\stored-input",
                last_output_directory=r"D:\stored-output",
            ),
        )
    )
    app = create_application(["texbook-gui-test"])
    window = MainWindow(settings_store=store)
    panel = window.centralWidget()

    assert panel.current_settings().output_kind == GuiOutputKind.project
    assert panel.current_settings().model == "stored-model"
    assert panel.current_settings().api_key == "stored-key"
    assert panel.current_settings().clear_cache is False
    assert panel.current_path_memory().last_input_directory == r"C:\stored-input"
    assert panel.task_view_states() == ()

    panel.set_input_selection(GuiInputSelection.from_single_file(r"C:\books\lecture.pdf"))
    _set_choice(panel, "outputKindChoices", GuiOutputKind.tex_file.value)
    panel.set_output_directory(r"D:\tex-output\lecture.tex")
    window.close()

    loaded = store.load_state()

    assert loaded.settings.path_state == GuiPathSelectionState()
    assert loaded.path_memory.last_input_directory == r"C:\books"
    assert loaded.path_memory.last_output_directory == r"D:\tex-output"

    app.quit()


def test_main_window_restores_and_saves_display_preferences(tmp_path):
    store = _gui_settings_store(tmp_path)
    store.save_state(
        GuiPersistentState(
            settings=GuiConversionSettings(),
            path_memory=GuiPathMemory(),
            display_preferences=GuiDisplayPreferences(
                theme=GuiThemeMode.dark,
                language=GuiLanguage.en_US,
            ),
        )
    )
    app = create_application(["texbook-gui-test"])
    window = MainWindow(settings_store=store)
    panel = window.centralWidget()
    assert isinstance(panel, ConversionMainPanel)

    assert window.windowTitle() == "TeXBook PDF to LaTeX"
    assert panel.current_display_preferences().theme == GuiThemeMode.dark
    assert panel.current_display_preferences().language == GuiLanguage.en_US
    assert panel.findChild(QPushButton, "addTaskButton").text() == "Add Task"
    assert "#17191d" in window.styleSheet()

    panel.findChild(QToolButton, "themeButton").click()
    panel.findChild(QToolButton, "languageButton").click()
    window.close()

    loaded = store.load_state()

    assert loaded.display_preferences.theme == GuiThemeMode.light
    assert loaded.display_preferences.language == GuiLanguage.zh_CN

    app.quit()


def test_gui_core_adapter_maps_settings_to_core_options(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "env-model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "env-key")
    settings = GuiConversionSettings(
        pages="1,3-4",
        document_class="ctexbeamer",
        structure_mode="llm",
        structure_chunk_pages=6,
        structure_max_pages=42,
        manual_title="手动标题",
        title_source="filename",
        show_date=True,
        beamer_title_page=False,
        prompt_preset="math",
        extra_prompt="只提取公式",
        temperature=0.5,
        timeout_seconds=30.0,
        max_tokens=64000,
        cache_enabled=True,
        cache_directory=str(tmp_path / "cache"),
        clear_cache=True,
        chunk_pages=3,
        prefetch_chunks=2,
        llm_max_concurrency=2,
        llm_min_request_interval=1.5,
        image_dpi=180,
        image_dpi_min=120,
        image_dpi_max=240,
        image_format="jpeg",
        jpeg_quality=92,
        llm_retries=4,
        llm_retry_initial_delay=3.0,
        llm_retry_max_delay=40.0,
        beamer_box_style="tcolorbox",
        ctex_font_profile="local",
    )

    bundle = build_core_conversion_bundle(settings, repo_root=ROOT)

    assert bundle.llm_config.model == "env-model"
    assert bundle.llm_config.api_key == "env-key"
    assert bundle.pages == [1, 3, 4]
    assert bundle.conversion_options.document_class == DocumentClassMode.ctexbeamer
    assert bundle.conversion_options.structure_options.mode == StructureMode.llm
    assert bundle.conversion_options.manual_title == "手动标题"
    assert bundle.conversion_options.cache_options.cache_dir == tmp_path / "cache"
    assert bundle.conversion_options.cache_options.clear is True
    assert bundle.conversion_options.image_options.image_format == "jpeg"
    assert bundle.conversion_options.output_options.beamer_box_style == BeamerBoxStyle.tcolorbox
    assert bundle.conversion_options.output_options.ctex_font_profile == CtexFontProfile.local
    assert bundle.conversion_options.output_options.beamer_title_page is False
    assert bundle.conversion_options.retry_options.retries == 4
    assert bundle.conversion_options.llm_max_concurrency == 2


def test_gui_core_adapter_resolves_api_key_from_named_environment_variable(monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "env-model")
    monkeypatch.setenv("TEXBOOK_GUI_API_KEY", "resolved-key")

    bundle = build_core_conversion_bundle(
        GuiConversionSettings(
            api_key="TEXBOOK_GUI_API_KEY",
            api_key_source=GuiApiKeySource.environment,
        ),
        repo_root=ROOT,
    )

    assert bundle.llm_config.api_key == "resolved-key"


def test_create_tasks_for_single_file_tex_and_project(monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    base = GuiConversionSettings(
        path_state=GuiPathSelectionState(
            input_selection=GuiInputSelection.from_single_file("lecture.pdf"),
            output_directory="out/lecture",
        )
    )

    tex_task = create_conversion_tasks(base, repo_root=ROOT)[0]
    project_task = create_conversion_tasks(
        GuiConversionSettings(
            path_state=base.path_state,
            output_kind=GuiOutputKind.project,
        ),
        repo_root=ROOT,
    )[0]

    assert tex_task.source_pdf == Path("lecture.pdf")
    assert tex_task.output_target.kind == GuiOutputKind.tex_file
    assert tex_task.output_target.path == Path("out/lecture.tex")
    assert project_task.output_target.kind == GuiOutputKind.project
    assert project_task.output_target.path == Path("out/lecture")
    assert tex_task.status == GuiTaskStatus.pending


def test_task_view_state_tracks_runtime_updates_and_progress_events(monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    task = create_conversion_tasks(
        GuiConversionSettings(
            path_state=GuiPathSelectionState(
                input_selection=GuiInputSelection.from_single_file("lecture.pdf"),
                output_directory="out/lecture",
            )
        ),
        repo_root=ROOT,
    )[0]

    state = create_task_view_state(task)
    assert state.status == GuiTaskStatus.pending
    assert state.stage == GuiTaskStage.waiting
    assert state.progress == 0

    mark_task_running(state)
    assert state.status == GuiTaskStatus.running
    assert state.progress == 5

    apply_progress_event(
        state,
        ProgressEvent(kind="stage_started", operation="chunk", label="chunk 1/2"),
    )
    assert state.stage == GuiTaskStage.chunk
    assert state.progress == 45

    apply_progress_event(
        state,
        ProgressEvent(
            kind="cache_hit",
            operation="chunk",
            label="chunk 1/2",
            metadata={"chunk_index": 1, "total_chunks": 2},
        ),
    )
    assert state.cache_hits == 1

    apply_progress_event(
        state,
        ProgressEvent(
            kind="retry_scheduled",
            operation="chunk",
            label="chunk 2/2",
            delay=0.5,
            error="timeout",
        ),
    )
    assert state.retries == 1

    apply_progress_event(
        state,
        ProgressEvent(
            kind="stage_completed",
            operation="chunk",
            label="chunk 2/2",
            metadata={"chunk_index": 2, "total_chunks": 2},
        ),
    )
    assert state.progress == 85

    mark_task_completed(state, result="out/lecture.tex", notes=("完成",))
    assert state.status == GuiTaskStatus.completed
    assert state.progress == 100
    assert state.result == "out/lecture.tex"

    mark_task_failed(state, "LLM failed")
    assert state.status == GuiTaskStatus.failed
    assert state.stage == GuiTaskStage.failed
    assert state.error == "LLM failed"


def test_gui_tex_write_confirms_existing_target(tmp_path):
    target = tmp_path / "out" / "lecture.tex"
    confirmations = []

    written = write_tex_result("% hello\n", target)

    assert written == target
    assert target.read_text(encoding="utf-8") == "% hello\n"

    written = write_tex_result(
        "% overwrite\n",
        target,
        policy=GuiWritePolicy(overwrite_confirmer=lambda request: confirmations.append(request) or True),
    )

    assert written == target
    assert target.read_text(encoding="utf-8") == "% overwrite\n"
    assert confirmations[0].summary == "覆盖已有 LaTeX 文件"


def test_gui_tex_write_keeps_existing_target_when_confirmation_is_rejected(tmp_path):
    target = tmp_path / "lecture.tex"
    target.write_text("% old\n", encoding="utf-8")

    try:
        write_tex_result(
            "% new\n",
            target,
            policy=GuiWritePolicy(overwrite_confirmer=lambda _request: False),
        )
    except GuiTaskExecutionError as exc:
        assert "用户取消覆盖" in str(exc)
    else:
        raise AssertionError("expected rejected overwrite to fail")

    assert target.read_text(encoding="utf-8") == "% old\n"


def test_gui_tex_write_silently_overwrites_when_confirmation_disabled(tmp_path):
    target = tmp_path / "lecture.tex"
    target.write_text("% old\n", encoding="utf-8")

    write_tex_result(
        "% new\n",
        target,
        policy=GuiWritePolicy(
            confirm_overwrite=False,
            overwrite_confirmer=lambda _request: (_ for _ in ()).throw(AssertionError("unexpected confirmation")),
        ),
    )

    assert target.read_text(encoding="utf-8") == "% new\n"


def test_gui_project_write_confirms_and_clears_nonempty_directory(tmp_path):
    project = LatexProjectResult(
        files={
            PurePosixPath("main.tex"): "% main\n",
            PurePosixPath("chapters/chapter01.tex"): "% body\n",
        },
        entrypoint=PurePosixPath("main.tex"),
        notes=["done"],
    )
    target = tmp_path / "project"
    old_nested = target / "old" / "stale.txt"

    entrypoint = write_project_result(project, target)

    assert entrypoint == target / "main.tex"
    assert (target / "main.tex").read_text(encoding="utf-8") == "% main\n"
    assert (target / "chapters" / "chapter01.tex").exists()

    old_nested.parent.mkdir()
    old_nested.write_text("old", encoding="utf-8")

    entrypoint = write_project_result(
        project,
        target,
        policy=GuiWritePolicy(overwrite_confirmer=lambda _request: True),
    )

    assert entrypoint == target / "main.tex"
    assert not old_nested.exists()
    assert (target / "chapters" / "chapter01.tex").read_text(encoding="utf-8") == "% body\n"


def test_gui_project_write_keeps_nonempty_directory_when_confirmation_is_rejected(tmp_path):
    project = LatexProjectResult(
        files={PurePosixPath("main.tex"): "% new\n"},
        entrypoint=PurePosixPath("main.tex"),
    )
    target = tmp_path / "project"
    target.mkdir()
    old = target / "old.txt"
    old.write_text("old", encoding="utf-8")

    try:
        write_project_result(
            project,
            target,
            policy=GuiWritePolicy(overwrite_confirmer=lambda _request: False),
        )
    except GuiTaskExecutionError as exc:
        assert "用户取消覆盖" in str(exc)
    else:
        raise AssertionError("expected rejected project overwrite to fail")

    assert old.read_text(encoding="utf-8") == "old"
    assert not (target / "main.tex").exists()


def test_gui_project_write_rejects_file_path_and_dangerous_directory(tmp_path):
    project = LatexProjectResult(
        files={PurePosixPath("main.tex"): "% main\n"},
        entrypoint=PurePosixPath("main.tex"),
    )
    file_target = tmp_path / "project"
    file_target.write_text("not a directory", encoding="utf-8")

    try:
        write_project_result(project, file_target)
    except GuiTaskExecutionError as exc:
        assert "不是目录" in str(exc)
    else:
        raise AssertionError("expected file project target to fail")

    dangerous = tmp_path / "repo"
    dangerous.mkdir()
    (dangerous / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    try:
        write_project_result(
            project,
            dangerous,
            policy=GuiWritePolicy(confirm_overwrite=False),
        )
    except GuiTaskExecutionError as exc:
        assert "疑似项目根目录" in str(exc)
    else:
        raise AssertionError("expected dangerous project target to fail")


def test_gui_project_write_rejects_package_subdirectory(tmp_path):
    project = LatexProjectResult(
        files={PurePosixPath("main.tex"): "% main\n"},
        entrypoint=PurePosixPath("main.tex"),
    )
    dangerous = ROOT / "src" / "texbook" / "__stage4_probe"
    dangerous.mkdir()
    (dangerous / "old.txt").write_text("old", encoding="utf-8")

    try:
        write_project_result(
            project,
            dangerous,
            policy=GuiWritePolicy(confirm_overwrite=False),
        )
    except GuiTaskExecutionError as exc:
        assert "危险项目目录" in str(exc)
    else:
        raise AssertionError("expected package subdirectory target to fail")
    finally:
        (dangerous / "old.txt").unlink(missing_ok=True)
        dangerous.rmdir()


def test_gui_project_write_rejects_empty_package_subdirectory(tmp_path):
    project = LatexProjectResult(
        files={PurePosixPath("main.tex"): "% main\n"},
        entrypoint=PurePosixPath("main.tex"),
    )
    dangerous = ROOT / "src" / "texbook" / "__stage4_probe_empty"
    dangerous.mkdir()

    try:
        with pytest.raises(GuiTaskExecutionError, match="危险项目目录"):
            write_project_result(
                project,
                dangerous,
                policy=GuiWritePolicy(confirm_overwrite=False),
            )
    finally:
        dangerous.rmdir()


def test_run_gui_conversion_task_writes_tex_and_forwards_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    task = create_conversion_tasks(
        GuiConversionSettings(
            path_state=GuiPathSelectionState(
                input_selection=GuiInputSelection.from_single_file("lecture.pdf"),
                output_directory=str(tmp_path / "lecture"),
            ),
            pages="1",
        ),
        repo_root=ROOT,
    )[0]
    events = []

    result = run_gui_conversion_task(
        task,
        progress_reporter=events.append,
        converter_factory=lambda _task, reporter: _FakeConverter(reporter),
    )

    assert result.result == str(tmp_path / "lecture.tex")
    assert result.notes == ("note",)
    assert (tmp_path / "lecture.tex").read_text(encoding="utf-8") == "% lecture.pdf\n"
    assert [event.kind for event in events] == ["stage_completed"]


def test_run_gui_conversion_task_uses_task_overwrite_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    target = tmp_path / "lecture.tex"
    target.write_text("% old\n", encoding="utf-8")
    task = create_conversion_tasks(
        GuiConversionSettings(
            path_state=GuiPathSelectionState(
                input_selection=GuiInputSelection.from_single_file("lecture.pdf"),
                output_directory=str(target),
            ),
            confirm_overwrite=True,
        ),
        repo_root=ROOT,
    )[0]
    confirmations = []

    result = run_gui_conversion_task(
        task,
        converter_factory=lambda _task, reporter: _FakeConverter(reporter),
        overwrite_confirmer=lambda request: confirmations.append(request) or True,
    )

    assert result.result == str(target)
    assert target.read_text(encoding="utf-8") == "% lecture.pdf\n"
    assert confirmations[0].task_id == task.task_id
    assert confirmations[0].task_label == "lecture.pdf"


def test_run_gui_conversion_task_silently_overwrites_when_confirmation_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    target = tmp_path / "lecture.tex"
    target.write_text("% old\n", encoding="utf-8")
    task = create_conversion_tasks(
        GuiConversionSettings(
            path_state=GuiPathSelectionState(
                input_selection=GuiInputSelection.from_single_file("lecture.pdf"),
                output_directory=str(target),
            ),
            confirm_overwrite=False,
        ),
        repo_root=ROOT,
    )[0]

    run_gui_conversion_task(
        task,
        converter_factory=lambda _task, reporter: _FakeConverter(reporter),
        overwrite_confirmer=lambda _request: (_ for _ in ()).throw(AssertionError("unexpected confirmation")),
    )

    assert target.read_text(encoding="utf-8") == "% lecture.pdf\n"


def test_gui_task_executor_fails_instead_of_hanging_without_confirmation_receiver(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    target = tmp_path / "lecture.tex"
    target.write_text("% old\n", encoding="utf-8")
    task = create_conversion_tasks(
        GuiConversionSettings(
            path_state=GuiPathSelectionState(
                input_selection=GuiInputSelection.from_single_file("lecture.pdf"),
                output_directory=str(target),
            ),
            confirm_overwrite=True,
        ),
        repo_root=ROOT,
    )[0]
    executor = GuiTaskExecutor(
        [task],
        converter_factory=lambda _task, reporter: _FakeConverter(reporter),
    )
    failures = []
    finished = []
    executor.task_failed.connect(lambda task_id, error: failures.append((task_id, error)))
    executor.all_finished.connect(lambda: finished.append(True))

    executor._run_task(task)

    assert failures == [(task.task_id, f"需要确认覆盖：{target}")]
    assert finished == []
    assert target.read_text(encoding="utf-8") == "% old\n"


def test_run_gui_conversion_task_writes_project_output(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    task = create_conversion_tasks(
        GuiConversionSettings(
            path_state=GuiPathSelectionState(
                input_selection=GuiInputSelection.from_single_file("lecture.pdf"),
                output_directory=str(tmp_path / "project"),
            ),
            output_kind=GuiOutputKind.project,
        ),
        repo_root=ROOT,
    )[0]

    result = run_gui_conversion_task(
        task,
        converter_factory=lambda _task, reporter: _FakeConverter(reporter),
    )

    assert result.result == str(tmp_path / "project" / "main.tex")
    assert result.notes == ("project note",)
    assert (tmp_path / "project" / "main.tex").exists()


def test_run_gui_conversion_task_can_cancel_before_write(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    token = GuiCancellationToken()
    task = create_conversion_tasks(
        GuiConversionSettings(
            path_state=GuiPathSelectionState(
                input_selection=GuiInputSelection.from_single_file("lecture.pdf"),
                output_directory=str(tmp_path / "lecture"),
            )
        ),
        repo_root=ROOT,
    )[0]

    try:
        run_gui_conversion_task(
            task,
            cancellation_token=token,
            converter_factory=lambda _task, reporter: _FakeConverter(
                reporter,
                cancel_during_convert=token,
            ),
        )
    except GuiTaskCanceled:
        pass
    else:
        raise AssertionError("expected cancellation before write")

    assert not (tmp_path / "lecture.tex").exists()


def test_conversion_panel_adds_visual_task_rows_and_updates_progress(monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    panel = window.centralWidget()
    assert isinstance(panel, ConversionMainPanel)

    panel.set_input_selection(GuiInputSelection.from_multiple_files(["a.pdf", "b.pdf"]))
    panel.set_output_directory("out")
    _fill_required_task_fields(panel)
    panel.findChild(QPushButton, "addTaskButton").click()

    states = panel.task_view_states()
    assert len(states) == 2
    assert panel.findChild(QWidget, f"taskRow_{states[0].task_id}") is not None
    assert panel.findChild(QLabel, f"taskStatusBadge_{states[0].task_id}").text() == "待处理"
    assert panel.findChild(QLabel, f"taskStageLabel_{states[0].task_id}").text() == "阶段：等待中"
    assert panel.findChild(QLabel, f"taskCacheLabel_{states[0].task_id}").text() == "缓存命中 0"
    assert panel.findChild(QLabel, f"taskRetryLabel_{states[0].task_id}").text() == "重试 0"
    assert panel.findChild(QLabel, "taskEmptyStatusLabel").text() == "已创建 2 个任务"
    assert panel.findChild(QToolButton, "clearTasksButton").isEnabled() is True
    assert panel.findChild(QPushButton, "startButton").isEnabled() is True

    first_id = states[0].task_id
    panel.handle_task_progress(
        first_id,
        ProgressEvent(
            kind="stage_completed",
            operation="chunk",
            label="chunk 1/2",
            metadata={"chunk_index": 1, "total_chunks": 2},
        ),
    )
    panel.handle_task_progress(
        first_id,
        ProgressEvent(kind="cache_hit", operation="chunk", label="chunk 1/2"),
    )
    panel.update_task_state(
        first_id,
        GuiTaskRuntimeUpdate(status=GuiTaskStatus.completed, stage=GuiTaskStage.completed, progress=100, result="out/a.tex"),
    )

    assert panel.findChild(QLabel, f"taskStatusBadge_{first_id}").text() == "完成"
    assert panel.findChild(QLabel, f"taskCacheLabel_{first_id}").text() == "缓存命中 1"
    assert panel.findChild(QLabel, f"taskResultLabel_{first_id}").text() == "完成结果：out/a.tex"
    assert panel.findChild(QProgressBar, "overallProgressBar").value() == 50

    panel.update_task_state(
        states[1].task_id,
        GuiTaskRuntimeUpdate(status=GuiTaskStatus.failed, stage=GuiTaskStage.failed, progress=40, error="LLM failed"),
    )
    assert panel.findChild(QLabel, f"taskStatusBadge_{states[1].task_id}").text() == "失败"
    assert panel.findChild(QLabel, f"taskResultLabel_{states[1].task_id}").text() == "失败原因：LLM failed"
    assert panel.findChild(QProgressBar, "overallProgressBar").value() == 70

    window.close()
    app.quit()


def test_conversion_panel_clear_tasks_button_empties_queue(monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    panel = window.centralWidget()
    assert isinstance(panel, ConversionMainPanel)

    panel.set_input_selection(GuiInputSelection.from_multiple_files(["a.pdf", "b.pdf"]))
    panel.set_output_directory("out")
    _fill_required_task_fields(panel)
    panel.findChild(QPushButton, "addTaskButton").click()
    assert len(panel.task_view_states()) == 2
    assert panel.findChild(QToolButton, "clearTasksButton").isEnabled() is True

    panel.findChild(QToolButton, "clearTasksButton").click()

    assert panel.task_view_states() == ()
    assert panel.findChild(QToolButton, "clearTasksButton").isEnabled() is False
    assert panel.findChild(QPushButton, "startButton").isEnabled() is False
    assert panel.findChild(QLabel, "taskEmptyStatusLabel").text() == "队列空闲"
    assert panel.findChild(QScrollArea, "taskListScrollArea").isVisible() is False

    window.close()
    app.quit()


def test_conversion_panel_start_button_runs_fake_executor(monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    panel = window.centralWidget()
    assert isinstance(panel, ConversionMainPanel)
    panel._executor_factory = _FakeExecutor

    panel.set_input_selection(GuiInputSelection.from_multiple_files(["a.pdf", "b.pdf"]))
    panel.set_output_directory("out")
    _fill_required_task_fields(panel)
    panel.findChild(QSpinBox, "batchWorkersSpinBox").setValue(2)
    panel.findChild(QPushButton, "addTaskButton").click()
    panel.findChild(QPushButton, "startButton").click()
    executor = panel._executor

    assert isinstance(executor, _FakeExecutor)
    assert executor.started is True
    assert executor.max_workers == 2
    assert panel.findChild(QPushButton, "startButton").isEnabled() is False

    first_id = panel.task_view_states()[0].task_id
    executor.task_started.emit(first_id)
    executor.task_progress.emit(
        first_id,
        ProgressEvent(
            kind="stage_completed",
            operation="chunk",
            label="chunk 1/1",
            metadata={"chunk_index": 1, "total_chunks": 1},
        ),
    )
    executor.task_completed.emit(first_id, "out/a.tex", ("done",))

    assert panel.findChild(QLabel, f"taskStatusBadge_{first_id}").text() == "完成"
    assert panel.findChild(QLabel, f"taskResultLabel_{first_id}").text() == "完成结果：out/a.tex"
    second_id = panel.task_view_states()[1].task_id
    executor.task_failed.emit(second_id, "LLM failed")
    assert panel.findChild(QLabel, f"taskStatusBadge_{second_id}").text() == "失败"
    assert any(event["kind"] == "task_failed" for event in panel._diagnostic_log.events)

    executor.all_finished.emit()
    assert panel._executor is None
    assert window.statusBar().currentMessage() == "后台转换已结束"

    window.close()
    app.quit()


def test_conversion_panel_cancels_pending_and_running_tasks(monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    panel = window.centralWidget()
    assert isinstance(panel, ConversionMainPanel)
    panel._executor_factory = _FakeExecutor

    panel.set_input_selection(GuiInputSelection.from_multiple_files(["a.pdf", "b.pdf"]))
    panel.set_output_directory("out")
    _fill_required_task_fields(panel)
    panel.findChild(QPushButton, "addTaskButton").click()

    first_id, second_id = [state.task_id for state in panel.task_view_states()]
    panel.cancel_task(first_id)

    assert panel.findChild(QLabel, f"taskStatusBadge_{first_id}").text() == "已取消"
    assert panel.findChild(QLabel, f"taskResultLabel_{first_id}").text() == "任务已取消"

    panel.findChild(QPushButton, "startButton").click()
    executor = panel._executor
    assert isinstance(executor, _FakeExecutor)
    executor.task_started.emit(second_id)
    panel.cancel_task(second_id)

    assert executor.canceled_task_ids == [second_id]
    assert panel.findChild(QLabel, f"taskStatusBadge_{second_id}").text() == "取消中"

    executor.task_canceled.emit(second_id)
    assert panel.findChild(QLabel, f"taskStatusBadge_{second_id}").text() == "已取消"
    assert panel.findChild(QPushButton, "startButton").isEnabled() is False
    log_kinds = [event["kind"] for event in panel._diagnostic_log.events]
    assert "task_cancel_requested" in log_kinds
    assert "task_canceling" in log_kinds
    assert "task_canceled" in log_kinds

    window.close()
    app.quit()


def test_conversion_panel_resolves_overwrite_confirmation(monkeypatch):
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    panel = window.centralWidget()
    assert isinstance(panel, ConversionMainPanel)

    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Yes)
    request = GuiOverwriteConfirmationRequest(
        task_id="task",
        task_label="lecture.pdf",
        target=Path("out/lecture.tex"),
        output_kind=GuiOutputKind.tex_file,
        summary="覆盖已有 LaTeX 文件",
        details="将替换目标 .tex 文件，同目录其它文件不会被清理。",
    )

    panel._handle_overwrite_confirmation_requested(request)

    assert request.wait() is True

    window.close()
    app.quit()


def test_conversion_panel_can_reject_overwrite_confirmation(monkeypatch):
    app = create_application(["texbook-gui-test"])
    window = MainWindow()
    panel = window.centralWidget()
    assert isinstance(panel, ConversionMainPanel)

    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.No)
    request = GuiOverwriteConfirmationRequest(
        task_id="task",
        task_label="lecture.pdf",
        target=Path("out/lecture.tex"),
        output_kind=GuiOutputKind.tex_file,
        summary="覆盖已有 LaTeX 文件",
        details="将替换目标 .tex 文件，同目录其它文件不会被清理。",
    )

    panel._handle_overwrite_confirmation_requested(request)

    assert request.wait() is False

    window.close()
    app.quit()


def test_create_tasks_supports_multiple_pdf_project_output(monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    settings = GuiConversionSettings(
        path_state=GuiPathSelectionState(
            input_selection=GuiInputSelection.from_multiple_files(["a.pdf", "b.PDF"]),
            output_directory="out",
        ),
        output_kind=GuiOutputKind.project,
        batch_workers=2,
    )

    tasks = create_conversion_tasks(settings, repo_root=ROOT)

    assert [task.source_pdf for task in tasks] == [Path("a.pdf"), Path("b.PDF")]
    assert [task.output_target.path for task in tasks] == [Path("out/a"), Path("out/b")]
    assert all(task.output_target.kind == GuiOutputKind.project for task in tasks)


def test_create_tasks_uses_windows_file_stems_for_native_paths(monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    settings = GuiConversionSettings(
        path_state=GuiPathSelectionState(
            input_selection=GuiInputSelection.from_multiple_files(
                [r"C:\books\a.pdf", r"\\server\share\b.PDF"]
            ),
            output_directory="out",
        ),
        output_kind=GuiOutputKind.tex_file,
    )

    tasks = create_conversion_tasks(settings, repo_root=ROOT)

    assert [task.label for task in tasks] == ["a.pdf", "b.PDF"]
    assert [task.output_target.path for task in tasks] == [Path("out/a.tex"), Path("out/b.tex")]


def test_create_tasks_expands_directory_pattern(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXBOOK_MODEL", "model")
    monkeypatch.setenv("TEXBOOK_API_KEY", "key")
    (tmp_path / "slides-2.pdf").write_text("", encoding="utf-8")
    (tmp_path / "slides-1.pdf").write_text("", encoding="utf-8")
    (tmp_path / "ignore.pdf").write_text("", encoding="utf-8")
    (tmp_path / "slides.txt").write_text("", encoding="utf-8")
    settings = GuiConversionSettings(
        path_state=GuiPathSelectionState(
            input_selection=GuiInputSelection.from_directory(str(tmp_path)),
            output_directory=str(tmp_path / "out"),
        ),
        output_kind=GuiOutputKind.tex_file,
        batch_pattern="slides-*.pdf",
    )

    tasks = create_conversion_tasks(settings, repo_root=ROOT)

    assert [task.source_pdf.name for task in tasks] == ["slides-1.pdf", "slides-2.pdf"]
    assert [task.output_target.path.name for task in tasks] == ["slides-1.tex", "slides-2.tex"]
