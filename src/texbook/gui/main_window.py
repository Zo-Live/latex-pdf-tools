"""Main window skeleton for the TexBook GUI."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize
from PySide6.QtGui import QAction, QCloseEvent, QHideEvent, QIcon, QShowEvent
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow

from texbook.gui.display import GuiDisplayPreferences, build_gui_font
from texbook.gui.dialogs import AboutDialog, SettingsDialog
from texbook.gui.i18n import tr
from texbook.gui.main_panel import ConversionMainPanel
from texbook.gui.persistence import GuiPersistentState, GuiSettingsStore
from texbook.gui.resources import APP_DISPLAY_NAME, resolve_app_icon_path
from texbook.gui.theme import build_fluent_stylesheet


class MainWindow(QMainWindow):
    """Empty application shell for later Fluent Design panels."""

    def __init__(self, *, settings_store: GuiSettingsStore | None = None) -> None:
        super().__init__()
        self._settings_store = settings_store or GuiSettingsStore()
        self._initial_state = self._settings_store.load_state()
        self._display_preferences = self._initial_state.display_preferences
        self._open_dialogs: list[QDialog] = []
        self._suppress_hide_event_cleanup = False
        self.setWindowTitle(self._tr("app.window_title"))
        self.setMinimumSize(QSize(960, 640))
        self.resize(QSize(1180, 760))

        icon_path = resolve_app_icon_path()
        if icon_path is not None:
            self.setWindowIcon(QIcon(str(icon_path)))

        self._setup_menu_bar()
        self._setup_status_bar()
        panel = ConversionMainPanel(
            self,
            display_preferences=self._display_preferences,
        )
        panel.display_preferences_changed.connect(self._apply_display_preferences)
        panel.settings_requested.connect(self._show_settings_dialog)
        panel.reset_defaults_requested.connect(self._reset_panel_defaults)
        self.setCentralWidget(panel)
        self._restore_gui_state(panel)
        self._apply_display_preferences(self._display_preferences)

    def _tr(self, key: str, **kwargs: object) -> str:
        return tr(self._display_preferences.language, key, **kwargs)

    def _setup_menu_bar(self) -> None:
        self._file_menu = self.menuBar().addMenu("")
        self._exit_action = QAction(self)
        self._exit_action.triggered.connect(self.close)
        self._file_menu.addAction(self._exit_action)

        self._help_menu = self.menuBar().addMenu("")
        self._export_log_action = QAction(self)
        self._export_log_action.triggered.connect(self._export_diagnostic_log)
        self._help_menu.addAction(self._export_log_action)
        self._about_action = QAction(self)
        self._about_action.triggered.connect(self._show_about_dialog)
        self._help_menu.addAction(self._about_action)
        self._retranslate_ui()

    def _setup_status_bar(self) -> None:
        self.statusBar().showMessage(self._tr("status.ready"))

    def _restore_gui_state(self, panel: ConversionMainPanel) -> None:
        panel.set_path_memory(self._initial_state.path_memory)
        panel.set_settings(self._initial_state.settings)

    def _apply_display_preferences(self, preferences: object) -> None:
        if not isinstance(preferences, GuiDisplayPreferences):
            return
        self._display_preferences = preferences
        self.setWindowTitle(self._tr("app.window_title"))
        app = QApplication.instance()
        if app is not None:
            app.setFont(
                build_gui_font(
                    preferences.font_point_size,
                    current_font=app.font(),
                )
            )
            app.setStyleSheet(
                build_fluent_stylesheet(
                    preferences.theme,
                    font_point_size=preferences.font_point_size,
                )
            )
        self.setFont(
            build_gui_font(
                preferences.font_point_size,
                current_font=self.font(),
            )
        )
        self.setStyleSheet(
            build_fluent_stylesheet(
                preferences.theme,
                font_point_size=preferences.font_point_size,
            )
        )
        self._retranslate_ui()

    def _retranslate_ui(self) -> None:
        self.setWindowTitle(self._tr("app.window_title"))
        self._file_menu.setTitle(self._tr("menu.file"))
        self._exit_action.setText(self._tr("menu.exit"))
        self._help_menu.setTitle(self._tr("menu.help"))
        self._export_log_action.setText(self._tr("menu.output_log"))
        self._about_action.setText(self._tr("menu.about", app_name=APP_DISPLAY_NAME))

    def _track_dialog(self, dialog: QDialog) -> int:
        self._open_dialogs.append(dialog)
        try:
            return dialog.exec()
        finally:
            if dialog in self._open_dialogs:
                self._open_dialogs.remove(dialog)
            dialog.deleteLater()

    def _show_about_dialog(self) -> None:
        dialog = AboutDialog(
            self,
            app_name=APP_DISPLAY_NAME,
            preferences=self._display_preferences,
        )
        self._track_dialog(dialog)

    def _export_diagnostic_log(self) -> None:
        panel = self.centralWidget()
        if isinstance(panel, ConversionMainPanel):
            panel.export_diagnostic_log()

    def _show_settings_dialog(self) -> None:
        panel = self.centralWidget()
        if not isinstance(panel, ConversionMainPanel):
            return
        dialog = SettingsDialog(self, preferences=self._display_preferences)
        if self._track_dialog(dialog) == QDialog.DialogCode.Accepted:
            panel.set_display_preferences(dialog.selected_preferences(), emit=True)

    def _reset_panel_defaults(self) -> None:
        panel = self.centralWidget()
        if not isinstance(panel, ConversionMainPanel):
            return
        panel.reset_to_default_configuration()

    def _save_gui_state(self) -> None:
        panel = self.centralWidget()
        if not isinstance(panel, ConversionMainPanel):
            return
        self._settings_store.save_state(
            GuiPersistentState(
                settings=panel.current_settings(),
                path_memory=panel.current_path_memory(),
                display_preferences=panel.current_display_preferences(),
            )
        )

    def _close_transient_dialogs(self) -> None:
        for dialog in list(self._open_dialogs):
            dialog.close()
        self._open_dialogs.clear()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() in {
            QEvent.Type.WindowStateChange,
        }:
            self._close_transient_dialogs()
        super().changeEvent(event)

    def hide(self) -> None:
        self._close_transient_dialogs()
        self._suppress_hide_event_cleanup = True
        super().hide()

    def hideEvent(self, event: QHideEvent) -> None:
        if not self._suppress_hide_event_cleanup:
            self._close_transient_dialogs()
        self._suppress_hide_event_cleanup = False
        super().hideEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        self._suppress_hide_event_cleanup = False
        super().showEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._close_transient_dialogs()
        self._save_gui_state()
        panel = self.centralWidget()
        close_executor = getattr(panel, "close_executor", None)
        if callable(close_executor):
            close_executor()
        super().closeEvent(event)
