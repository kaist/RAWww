"""Modal dialogs split out of app.py."""

from __future__ import annotations

import re
import sys

from PySide6.QtCore import QEvent, QKeyCombination, QSettings, QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QButtonGroup, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QRadioButton, QSpinBox, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget
from datetime import datetime
from pathlib import Path
from typing import Callable
from .hotkeys import HOTKEY_DEFAULTS, _hotkey_sequence, _uses_reserved_navigation_key
from .shotsync_client import ShotSyncClient
from .theme import _fomantic_icon
from .widgets import CodeCompletingLineEdit, CodeReplacementsEditor, SettingsCheckBox
from .version import __version__ as APP_VERSION


class HelpDialog(QDialog):
    """A read-only, current shortcut reference opened from the title bar."""

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("helpDialog")
        self.setWindowTitle("Справка по горячим клавишам")
        self.setModal(True)
        self.resize(620, 590)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(10)

        title = QLabel("Горячие клавиши")
        title.setObjectName("helpDialogTitle")
        layout.addWidget(title)
        hint = QLabel(
            "Сочетания ниже показывают текущие настройки приложения. "
            "Их можно переназначить в разделе «Настройки → Горячие клавиши»."
        )
        hint.setObjectName("helpDialogHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        table = QTableWidget(len(HOTKEY_DEFAULTS), 2)
        table.setObjectName("helpHotkeysTable")
        table.setHorizontalHeaderLabels(("Действие", "Сочетание"))
        table.verticalHeader().hide()
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnWidth(0, 350)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        for row, (identifier, (label, _default)) in enumerate(HOTKEY_DEFAULTS.items()):
            table.setItem(row, 0, QTableWidgetItem(label))
            sequence = _hotkey_sequence(settings, identifier)
            table.setItem(row, 1, QTableWidgetItem(sequence.toString() or "Не назначено"))
        layout.addWidget(table, 1)

        close = QPushButton("Закрыть")
        close.setObjectName("helpDialogCloseButton")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)


class SettingsDialog(QDialog):
    """Application settings presented in the same visual language as the shell."""

    def __init__(self, settings: QSettings, client: ShotSyncClient, changed: Callable[[list[dict]], None], login_requested: Callable[[], bool], update_requested: Callable[[], None], cache_size_provider: Callable[[], int], clear_cache_requested: Callable[[], None], parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.update_requested = update_requested
        self.cache_size_provider = cache_size_provider
        self.clear_cache_requested = clear_cache_requested
        self.setObjectName("settingsDialog")
        self.setWindowTitle("Настройки")
        self.setModal(True)
        self.resize(700, 540)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)

        title = QLabel("Настройки")
        title.setObjectName("settingsDialogTitle")
        layout.addWidget(title)

        tabs = QTabWidget()
        tabs.setObjectName("settingsTabs")
        tabs.addTab(self._behavior_tab(), "Поведение")
        tabs.addTab(self._hotkeys_tab(), "Горячие клавиши")
        self.code_replacements_editor = CodeReplacementsEditor(client, settings, changed, login_requested)
        tabs.addTab(self.code_replacements_editor, "Коды замен")
        tabs.addTab(self._interface_tab(), "Интерфейс")
        tabs.addTab(self._about_tab(), "О приложении")
        layout.addWidget(tabs, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Отмена")
        cancel.setObjectName("settingsSecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        apply = QPushButton("Готово")
        apply.setObjectName("settingsPrimaryButton")
        apply.clicked.connect(self._save)
        buttons.addWidget(apply)
        layout.addLayout(buttons)

    def _behavior_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("behaviorTabPage")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 20, 18, 18)
        layout.setSpacing(8)
        heading = QLabel("Рабочее пространство")
        heading.setObjectName("settingsSectionTitle")
        layout.addWidget(heading)
        hint = QLabel("Выберите, что Контролька будет восстанавливать при следующем запуске.")
        hint.setObjectName("settingsHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.restore_workspaces = SettingsCheckBox("Восстанавливать открытые вкладки")
        self.restore_workspaces.setChecked(self.settings.value("restore_workspaces", True, bool))
        layout.addWidget(self.restore_workspaces)
        self.delete_without_confirmation = SettingsCheckBox("Удалять без подтверждения по DEL или Shift+DEL")
        self.delete_without_confirmation.setChecked(
            self.settings.value("behavior/delete_without_confirmation", False, bool)
        )
        layout.addWidget(self.delete_without_confirmation)
        self.auto_ai_after_previews = SettingsCheckBox("Всегда запускать AI после превью")
        self.auto_ai_after_previews.setChecked(
            self.settings.value("ai/auto_after_previews", False, bool)
        )
        auto_ai_hint = QLabel(
            "Как только миниатюры в папке готовы, автоматически запускается анализ "
            "серий и лиц. Срабатывает и при добавлении новых фотографий."
        )
        auto_ai_hint.setObjectName("settingsHint")
        auto_ai_hint.setWordWrap(True)
        layout.addWidget(self.auto_ai_after_previews)
        layout.addWidget(auto_ai_hint)

        cache_card = QFrame()
        cache_card.setObjectName("externalEditorCard")
        cache_layout = QVBoxLayout(cache_card)
        cache_layout.setContentsMargins(14, 13, 14, 14)
        cache_layout.setSpacing(7)
        cache_heading = QLabel("Кэш")
        cache_heading.setObjectName("externalEditorTitle")
        cache_layout.addWidget(cache_heading)
        cache_hint = QLabel("Кэш содержит миниатюры и результаты анализа фотографий. Очистка не удаляет исходные файлы.")
        cache_hint.setObjectName("externalEditorHint")
        cache_hint.setWordWrap(True)
        cache_layout.addWidget(cache_hint)
        self.cache_size_label = QLabel()
        self.cache_size_label.setObjectName("settingsHint")
        cache_layout.addWidget(self.cache_size_label)
        clear_cache_button = QPushButton("Очистить кэш")
        clear_cache_button.setObjectName("settingsPrimaryButton")
        clear_cache_button.clicked.connect(self._clear_cache)
        cache_layout.addWidget(clear_cache_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(cache_card)
        self._refresh_cache_size()

        editor_card = QFrame()
        editor_card.setObjectName("externalEditorCard")
        editor_layout = QVBoxLayout(editor_card)
        editor_layout.setContentsMargins(14, 13, 14, 14)
        editor_layout.setSpacing(7)
        editor_heading = QLabel("Внешний редактор")
        editor_heading.setObjectName("externalEditorTitle")
        editor_layout.addWidget(editor_heading)
        editor_hint = QLabel("Выберите приложение, в котором открываются файлы по клавише E.")
        editor_hint.setObjectName("externalEditorHint")
        editor_hint.setWordWrap(True)
        editor_layout.addWidget(editor_hint)

        self.photoshop_editor = QRadioButton("Adobe Photoshop")
        self.photoshop_editor.setObjectName("editorChoice")
        self.custom_editor = QRadioButton("Другой редактор")
        self.custom_editor.setObjectName("editorChoice")
        self.editor_choices = QButtonGroup(self)
        self.editor_choices.addButton(self.photoshop_editor)
        self.editor_choices.addButton(self.custom_editor)
        has_custom_path = bool(self.settings.value("editor/executable", "", str).strip())
        use_custom = self.settings.value("editor/use_custom_executable", has_custom_path, bool)
        (self.custom_editor if use_custom else self.photoshop_editor).setChecked(True)
        self.custom_editor.toggled.connect(self._update_editor_choice_state)
        choices_row = QHBoxLayout()
        choices_row.setSpacing(24)
        choices_row.addWidget(self.photoshop_editor)
        choices_row.addWidget(self.custom_editor)
        choices_row.addStretch(1)
        editor_layout.addLayout(choices_row)

        self.custom_editor_controls = QWidget()
        self.custom_editor_controls.setObjectName("customEditorControls")
        editor_row = QHBoxLayout()
        editor_row.setContentsMargins(24, 0, 0, 0)
        editor_row.setSpacing(8)
        self.editor_executable = QLineEdit(self.settings.value("editor/executable", "", str))
        self.editor_executable.setObjectName("editorExecutable")
        self.editor_executable.setPlaceholderText("Путь к исполняемому файлу")
        self.editor_executable.setClearButtonEnabled(True)
        editor_row.addWidget(self.editor_executable, 1)
        self.choose_editor = QToolButton()
        self.choose_editor.setObjectName("editorBrowseButton")
        self.choose_editor.setIcon(_fomantic_icon("folder", 15, "#c9c9c9"))
        self.choose_editor.setIconSize(QSize(15, 15))
        self.choose_editor.setToolTip("Выбрать исполняемый файл")
        self.choose_editor.clicked.connect(self._choose_editor_executable)
        editor_row.addWidget(self.choose_editor)
        self.custom_editor_controls.setLayout(editor_row)
        editor_layout.addWidget(self.custom_editor_controls)
        layout.addWidget(editor_card)

        if sys.platform == "win32":
            integration_card = QFrame()
            integration_card.setObjectName("externalEditorCard")
            integration_layout = QVBoxLayout(integration_card)
            integration_layout.setContentsMargins(14, 13, 14, 14)
            integration_layout.setSpacing(7)
            integration_heading = QLabel("Интеграция с Проводником")
            integration_heading.setObjectName("externalEditorTitle")
            integration_layout.addWidget(integration_heading)
            integration_hint = QLabel(
                "Добавляет «Открыть в Контрольке» для поддерживаемых файлов и папок. "
                "Программа просмотра по умолчанию не меняется."
            )
            integration_hint.setObjectName("externalEditorHint")
            integration_hint.setWordWrap(True)
            integration_layout.addWidget(integration_hint)
            self.explorer_integration_button = QPushButton()
            self.explorer_integration_button.setObjectName("settingsSecondaryButton")
            self.explorer_integration_button.clicked.connect(self._toggle_explorer_integration)
            integration_layout.addWidget(self.explorer_integration_button, 0, Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(integration_card)
            self._refresh_explorer_integration_button()
        layout.addStretch(1)
        self._update_editor_choice_state(self.custom_editor.isChecked())
        return tab

    def _update_editor_choice_state(self, use_custom: bool) -> None:
        self.custom_editor_controls.setVisible(use_custom)

    def _choose_editor_executable(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите редактор",
            self.editor_executable.text().strip(),
            "Программы (*.exe);;Все файлы (*)",
        )
        if path:
            self.editor_executable.setText(path)

    def _refresh_explorer_integration_button(self) -> None:
        from .windows_integration import is_registered

        if not getattr(sys, "frozen", False):
            self.explorer_integration_button.setText("Доступно в собранном приложении")
            self.explorer_integration_button.setEnabled(False)
            self.explorer_integration_button.setToolTip("Соберите приложение, чтобы Проводник запускал ctrlka.exe.")
            return
        self.explorer_integration_button.setEnabled(True)
        self.explorer_integration_button.setText("Удалить из Проводника" if is_registered() else "Добавить в Проводник")

    def _toggle_explorer_integration(self) -> None:
        from .windows_integration import is_registered, register, unregister

        if not getattr(sys, "frozen", False):
            return

        if is_registered():
            if QMessageBox.question(
                self,
                "Удалить интеграцию?",
                "Убрать команду «Открыть в Контрольке» из Проводника?",
            ) != QMessageBox.StandardButton.Yes:
                return
            try:
                unregister()
            except OSError as exc:
                QMessageBox.warning(self, "Не удалось изменить Проводник", str(exc))
                return
            self._refresh_explorer_integration_button()
            return

        executable = Path(sys.executable)
        try:
            register(executable)
        except OSError as exc:
            QMessageBox.warning(self, "Не удалось изменить Проводник", str(exc))
            return
        self._refresh_explorer_integration_button()

    def _hotkeys_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("settingsTabPage")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 20, 18, 18)
        layout.setSpacing(10)
        heading = QLabel("Горячие клавиши")
        heading.setObjectName("settingsSectionTitle")
        layout.addWidget(heading)
        hint = QLabel("Нажмите новое сочетание в поле. Стрелки, Enter и Esc зарезервированы для навигации.")
        hint.setObjectName("settingsHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.swap_rating_color = SettingsCheckBox("Цветовые метки — цифры без Shift")
        self.swap_rating_color.blockSignals(True)
        self.swap_rating_color.setChecked(self.settings.value("hotkeys/swap_rating_and_color", False, bool))
        self.swap_rating_color.blockSignals(False)
        self.swap_rating_color.toggled.connect(self._set_rating_color_scheme)
        layout.addWidget(self.swap_rating_color)

        table = QTableWidget(len(HOTKEY_DEFAULTS), 2)
        table.setObjectName("hotkeysTable")
        table.setHorizontalHeaderLabels(("Действие", "Сочетание"))
        table.verticalHeader().hide()
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnWidth(0, 260)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.hotkey_edits: dict[str, QKeySequenceEdit] = {}
        for row, (identifier, (label, _default)) in enumerate(HOTKEY_DEFAULTS.items()):
            table.setItem(row, 0, QTableWidgetItem(label))
            editor = QKeySequenceEdit(_hotkey_sequence(self.settings, identifier))
            editor.setMaximumSequenceLength(1)
            table.setCellWidget(row, 1, editor)
            self.hotkey_edits[identifier] = editor
        layout.addWidget(table, 1)
        restore = QPushButton("Вернуть сочетания по умолчанию")
        restore.setObjectName("settingsSecondaryButton")
        restore.clicked.connect(self._restore_default_hotkeys)
        layout.addWidget(restore, 0, Qt.AlignmentFlag.AlignLeft)
        return tab

    def _restore_default_hotkeys(self) -> None:
        self.swap_rating_color.setChecked(False)
        for identifier, editor in self.hotkey_edits.items():
            editor.setKeySequence(QKeySequence(HOTKEY_DEFAULTS[identifier][1]))

    def _interface_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("settingsTabPage")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 20, 18, 18)
        layout.setSpacing(8)
        heading = QLabel("Интерфейс")
        heading.setObjectName("settingsSectionTitle")
        layout.addWidget(heading)
        hint = QLabel("Настройте элементы, которые показываются поверх изображения в полном просмотре.")
        hint.setObjectName("settingsHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.show_full_view_counter = SettingsCheckBox("Показывать счетчик файлов в полном просмотре")
        self.show_full_view_counter.setChecked(
            self.settings.value("interface/show_full_view_counter", True, bool)
        )
        layout.addWidget(self.show_full_view_counter)
        self.show_full_view_mark_indicator = SettingsCheckBox(
            "Показывать индикатор меток в полном просмотре"
        )
        self.show_full_view_mark_indicator.setChecked(
            self.settings.value("interface/show_full_view_mark_indicator", True, bool)
        )
        layout.addWidget(self.show_full_view_mark_indicator)
        self.mark_indicator_position_control = QWidget()
        position_layout = QHBoxLayout(self.mark_indicator_position_control)
        position_layout.setContentsMargins(24, 0, 0, 0)
        position_layout.setSpacing(8)
        position_layout.addWidget(QLabel("Положение индикатора:"))
        self.full_view_mark_indicator_position = QComboBox()
        self.full_view_mark_indicator_position.addItem("Снизу справа", "bottom")
        self.full_view_mark_indicator_position.addItem("Сверху справа", "top")
        saved_position = self.settings.value(
            "interface/full_view_mark_indicator_position", "bottom", str
        )
        self.full_view_mark_indicator_position.setCurrentIndex(
            max(0, self.full_view_mark_indicator_position.findData(saved_position))
        )
        position_layout.addWidget(self.full_view_mark_indicator_position)
        position_layout.addStretch(1)
        self.mark_indicator_position_control.setEnabled(self.show_full_view_mark_indicator.isChecked())
        self.show_full_view_mark_indicator.toggled.connect(self.mark_indicator_position_control.setEnabled)
        layout.addWidget(self.mark_indicator_position_control)
        self.zoom_focus_face = SettingsCheckBox("Акцент на лице при зуме")
        self.zoom_focus_face.setChecked(
            self.settings.value("interface/zoom_focus_face", True, bool)
        )
        layout.addWidget(self.zoom_focus_face)
        layout.addStretch(1)
        return tab

    def _about_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("settingsTabPage")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 20, 18, 18)
        layout.setSpacing(10)
        title = QLabel("Контролька")
        title.setObjectName("settingsSectionTitle")
        layout.addWidget(title)
        version = QLabel(f"Версия {APP_VERSION}")
        version.setObjectName("settingsHint")
        layout.addWidget(version)
        description = QLabel("Рабочее пространство для просмотра, отбора и подготовки фотоматериалов.")
        description.setObjectName("settingsHint")
        description.setWordWrap(True)
        layout.addWidget(description)
        self.auto_update_check = SettingsCheckBox("Автоматически проверять обновления при запуске")
        self.auto_update_check.setChecked(self.settings.value("updates/auto_check", True, bool))
        layout.addWidget(self.auto_update_check)
        check = QPushButton("Проверить обновления")
        check.setObjectName("settingsPrimaryButton")
        check.clicked.connect(lambda: self.update_requested())
        layout.addWidget(check, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return tab

    def _refresh_cache_size(self) -> None:
        self.cache_size_label.setText(f"Размер: {self._format_size(self.cache_size_provider())}")

    def _clear_cache(self) -> None:
        confirm = QMessageBox.question(
            self, "Очистить кэш", "Удалить все миниатюры и результаты анализа?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.clear_cache_requested()
            self._refresh_cache_size()

    @staticmethod
    def _format_size(size: int) -> str:
        value = float(size)
        for unit in ("Б", "КБ", "МБ", "ГБ"):
            if value < 1024 or unit == "ГБ":
                return f"{value:.1f} {unit}" if unit != "Б" else f"{int(value)} {unit}"
            value /= 1024

    def _set_rating_color_scheme(self, color_on_plain_digits: bool) -> None:
        """Switch all number pairs together, leaving other custom keys alone."""
        for number in range(6):
            rating = f"rating_{number}"
            color = f"color_{number}"
            self.hotkey_edits[rating].setKeySequence(QKeySequence(f"Shift+{number}" if color_on_plain_digits else str(number)))
            self.hotkey_edits[color].setKeySequence(QKeySequence(str(number) if color_on_plain_digits else f"Shift+{number}"))

    @staticmethod
    def _placeholder_tab(title_text: str, description: str) -> QWidget:
        tab = QWidget()
        tab.setObjectName("settingsTabPage")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 20, 18, 18)
        layout.setSpacing(8)
        title = QLabel(title_text)
        title.setObjectName("settingsSectionTitle")
        layout.addWidget(title)
        hint = QLabel(description)
        hint.setObjectName("settingsHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        return tab

    def _save(self) -> None:
        sequences = {identifier: editor.keySequence() for identifier, editor in self.hotkey_edits.items()}
        if any(_uses_reserved_navigation_key(sequence) for sequence in sequences.values()):
            QMessageBox.warning(self, "Горячие клавиши", "Стрелки, Enter и Esc нельзя назначать на другие действия.")
            return
        assigned: dict[str, str] = {}
        for identifier, sequence in sequences.items():
            text = sequence.toString(QKeySequence.SequenceFormat.PortableText)
            if text and text in assigned:
                QMessageBox.warning(self, "Горячие клавиши", f"Сочетание {text} уже назначено действию «{HOTKEY_DEFAULTS[assigned[text]][0]}».")
                return
            if text:
                assigned[text] = identifier
        self.settings.setValue("restore_workspaces", self.restore_workspaces.isChecked())
        self.settings.setValue("behavior/delete_without_confirmation", self.delete_without_confirmation.isChecked())
        self.settings.setValue("ai/auto_after_previews", self.auto_ai_after_previews.isChecked())
        self.settings.setValue("interface/show_full_view_counter", self.show_full_view_counter.isChecked())
        self.settings.setValue(
            "interface/show_full_view_mark_indicator",
            self.show_full_view_mark_indicator.isChecked(),
        )
        self.settings.setValue(
            "interface/full_view_mark_indicator_position",
            self.full_view_mark_indicator_position.currentData(),
        )
        self.settings.setValue("interface/zoom_focus_face", self.zoom_focus_face.isChecked())
        self.settings.setValue("editor/use_custom_executable", self.custom_editor.isChecked())
        self.settings.setValue("editor/executable", self.editor_executable.text().strip())
        self.settings.setValue("hotkeys/swap_rating_and_color", self.swap_rating_color.isChecked())
        self.settings.setValue("updates/auto_check", self.auto_update_check.isChecked())
        for identifier, sequence in sequences.items():
            self.settings.setValue(f"hotkeys/{identifier}", sequence.toString(QKeySequence.SequenceFormat.PortableText))
        self.accept()


class QuickTransferDialog(QDialog):
    """A small destination picker designed to be operated without a mouse."""

    def __init__(self, operation: str, destinations: list[Path], hotkey: QKeySequence, accepted: Callable, parent=None) -> None:
        super().__init__(parent)
        self.hotkey, self._accepted = hotkey, accepted
        self.setObjectName("quickTransferDialog")
        self.setWindowTitle(f"Быстрое {operation}")
        self.setModal(True)
        self.setFixedWidth(680)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        title = QLabel(f"Куда {operation} выделенные файлы?")
        title.setObjectName("quickTransferTitle")
        layout.addWidget(title)
        hint = QLabel(f"↑/↓ — выбрать · повторите {hotkey.toString()} или Enter — выполнить · 1–9 — выполнить сразу")
        hint.setObjectName("quickTransferHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.destinations = QListWidget()
        self.destinations.setObjectName("quickTransferDestinations")
        self.destinations.installEventFilter(self)
        for number, destination in enumerate(destinations[:9], start=1):
            item = QListWidgetItem(f"{number}.  {destination}")
            item.setData(Qt.ItemDataRole.UserRole, destination)
            self.destinations.addItem(item)
        # QListWidget consumes Enter before the dialog sees it, so handle the
        # list's activation signal as well as the dialog-level shortcut.
        self.destinations.itemActivated.connect(lambda item: self._choose_item(item, True))
        layout.addWidget(self.destinations)
        self.repeat_shortcut = QShortcut(hotkey, self)
        self.repeat_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.repeat_shortcut.activated.connect(lambda: self._choose_selected(True))
        buttons = QHBoxLayout()
        add_path = QPushButton("Добавить путь…")
        add_path.setObjectName("settingsSecondaryButton")
        add_path.setAutoDefault(False)
        add_path.clicked.connect(self._add_path)
        buttons.addWidget(add_path)
        buttons.addStretch(1)
        cancel = QPushButton("Отмена")
        cancel.setObjectName("settingsSecondaryButton")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        choose = QPushButton(operation.capitalize())
        choose.setObjectName("settingsPrimaryButton")
        choose.setAutoDefault(False)
        choose.setDefault(False)
        choose.clicked.connect(lambda: self._choose_selected(True))
        buttons.addWidget(choose)
        layout.addLayout(buttons)
        if self.destinations.count():
            self.destinations.setCurrentRow(0)
        self.destinations.setFocus(Qt.FocusReason.OtherFocusReason)

    def _add_path(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Выберите папку назначения")
        if not path:
            return
        destination = Path(path)
        for row in range(self.destinations.count()):
            if self.destinations.item(row).data(Qt.ItemDataRole.UserRole) == destination:
                self.destinations.setCurrentRow(row)
                return
        if self.destinations.count() >= 9:
            QMessageBox.information(self, "Быстрое перемещение", "Можно сохранить не более 9 путей.")
            return
        item = QListWidgetItem(f"{self.destinations.count() + 1}.  {destination}")
        item.setData(Qt.ItemDataRole.UserRole, destination)
        self.destinations.addItem(item)
        self.destinations.setCurrentItem(item)

    def _choose_selected(self, update_recent: bool) -> None:
        self._choose_item(self.destinations.currentItem(), update_recent)

    def _choose_item(self, item: QListWidgetItem | None, update_recent: bool) -> None:
        if item is None:
            return
        destination = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(destination, Path) and destination.is_dir():
            self.destinations.setEnabled(False)
            self.progress.setValue(0)
            self.progress.show()
            self._accepted(destination, update_recent, self._set_progress)
            self.accept()

    def _set_progress(self, completed: int, total: int) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(completed)
        self.progress.setFormat(f"{completed} из {total}")
        QApplication.processEvents()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._choose_number(event):
            return
        sequence = QKeySequence(QKeyCombination(event.modifiers(), Qt.Key(event.key())))
        if self.hotkey.matches(sequence) == QKeySequence.SequenceMatch.ExactMatch:
            self._choose_selected(True)
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.destinations and event.type() == QEvent.Type.KeyPress and self._choose_number(event):
            return True
        return super().eventFilter(watched, event)

    def _choose_number(self, event) -> bool:
        number = event.key() - int(Qt.Key.Key_0)
        if not (1 <= number <= 9 and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            return False
        if number <= self.destinations.count():
            self.destinations.setCurrentRow(number - 1)
            self._choose_selected(False)
        return True


class BatchRenameDialog(QDialog):
    """Preview a filename template against the current, already sorted photo view."""

    renameRequested = Signal(object)
    _token_pattern = re.compile(
        r"\{counter(?::(\d+))?\}|\{(year|month|day|hour|minute|second|date|time|datetime)\}"
    )

    def __init__(self, paths: list[Path], details: dict[str, dict], settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.paths = paths
        self.details = details
        self.settings = settings
        self._renaming = False
        self.setObjectName("batchRenameDialog")
        self.setWindowTitle("Групповое переименование")
        self.setModal(True)
        self.resize(880, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        title = QLabel("Групповое переименование")
        title.setObjectName("batchRenameTitle")
        layout.addWidget(title)
        hint = QLabel("Файлы идут в том же порядке, что и текущий список. Расширение каждого файла сохраняется.")
        hint.setObjectName("batchRenameHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        template_row = QHBoxLayout()
        template_icon = QLabel()
        template_icon.setPixmap(_fomantic_icon("edit", 16, "#a8b0bd").pixmap(QSize(16, 16)))
        template_row.addWidget(template_icon)
        template_label = QLabel("Шаблон")
        template_label.setObjectName("batchRenameLabel")
        template_row.addWidget(template_label)
        self.template_edit = CodeCompletingLineEdit()
        self.template_edit.setObjectName("batchRenameTemplate")
        self.template_edit.setText(self.settings.value("batch_rename/template", "IMG_{counter:04}", str))
        self.template_edit.set_codes([{"codes": [
            {"code": "year", "value": "Год"}, {"code": "month", "value": "Месяц"},
            {"code": "day", "value": "День"}, {"code": "hour", "value": "Час"},
            {"code": "minute", "value": "Минута"}, {"code": "second", "value": "Секунда"},
            {"code": "counter:04", "value": "Счётчик (0001)"},
        ]}], 0)
        self.template_edit.setPlaceholderText("Например: свадьба_{counter:04}")
        self.template_edit.textEdited.connect(lambda _text: self._update_preview())
        template_row.addWidget(self.template_edit, 1)
        layout.addLayout(template_row)

        constructor = QFrame()
        constructor.setObjectName("batchRenameConstructor")
        constructor_layout = QVBoxLayout(constructor)
        constructor_layout.setContentsMargins(10, 7, 10, 7)
        constructor_layout.setSpacing(6)
        counter_row = QHBoxLayout()
        counter_row.setSpacing(6)
        counter_icon = QLabel()
        counter_icon.setPixmap(_fomantic_icon("sort", 14, "#a8b0bd").pixmap(QSize(14, 14)))
        counter_row.addWidget(counter_icon)
        counter_row.addWidget(QLabel("Счётчик"))
        self.counter_start = QSpinBox()
        self.counter_start.setObjectName("batchRenameSpin")
        self.counter_start.setRange(0, 999_999_999)
        self.counter_start.setValue(self.settings.value("batch_rename/counter_start", 1, int))
        self.counter_start.setPrefix("с ")
        self.counter_start.valueChanged.connect(self._update_preview)
        counter_row.addWidget(self.counter_start)
        self.counter_digits = QSpinBox()
        self.counter_digits.setObjectName("batchRenameSpin")
        self.counter_digits.setRange(1, 9)
        self.counter_digits.setValue(self.settings.value("batch_rename/counter_digits", 4, int))
        self.counter_digits.setSuffix(" цифры")
        self.counter_digits.valueChanged.connect(self._update_preview)
        counter_row.addWidget(self.counter_digits)
        add_counter = self._token_button("sort", "Счётчик", self._insert_counter)
        counter_row.addWidget(add_counter)
        counter_row.addStretch(1)
        constructor_layout.addLayout(counter_row)
        date_time_row = QHBoxLayout()
        date_time_row.setSpacing(6)
        for icon, label, token in (
            ("calendar", "Год", "{year}"), ("calendar", "Месяц", "{month}"), ("calendar", "День", "{day}"),
        ):
            date_time_row.addWidget(self._token_button(icon, label, lambda _checked=False, value=token: self._insert_token(value)))
        date_time_row.addSpacing(8)
        for icon, label, token in (
            ("clock", "Час", "{hour}"), ("clock", "Минута", "{minute}"), ("clock", "Секунда", "{second}"),
        ):
            date_time_row.addWidget(self._token_button(icon, label, lambda _checked=False, value=token: self._insert_token(value)))
        date_time_row.addStretch(1)
        constructor_layout.addLayout(date_time_row)
        layout.addWidget(constructor)

        tokens = QLabel("Введите { в поле шаблона, чтобы выбрать подстановку. Дата и время берутся из EXIF, а при его отсутствии — из файла.")
        tokens.setObjectName("batchRenameTokens")
        layout.addWidget(tokens)

        lists = QSplitter(Qt.Orientation.Horizontal)
        self._before_list = QListWidget()
        self._after_list = QListWidget()
        self._before_list.verticalScrollBar().valueChanged.connect(
            self._after_list.verticalScrollBar().setValue
        )
        self._after_list.verticalScrollBar().valueChanged.connect(
            self._before_list.verticalScrollBar().setValue
        )
        before_box = self._preview_box("До переименования", "file", self._before_list)
        after_box = self._preview_box("Станет", "arrow-right", self._after_list)
        lists.addWidget(before_box)
        lists.addWidget(after_box)
        lists.setSizes([420, 420])
        layout.addWidget(lists, 1)
        self.validation_label = QLabel()
        self.validation_label.setObjectName("batchRenameValidation")
        self.validation_label.setWordWrap(True)
        layout.addWidget(self.validation_label)
        self.rename_progress = QProgressBar()
        self.rename_progress.setObjectName("batchRenameProgress")
        self.rename_progress.setTextVisible(True)
        self.rename_progress.hide()
        layout.addWidget(self.rename_progress)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setObjectName("batchRenameSecondaryButton")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.rename_button = QPushButton("Переименовать")
        self.rename_button.setObjectName("batchRenamePrimaryButton")
        self.rename_button.setIcon(_fomantic_icon("edit", 14, "#ffffff"))
        self.rename_button.clicked.connect(self._request_rename)
        buttons.addWidget(self.rename_button)
        layout.addLayout(buttons)
        self._names: dict[str, str] = {}
        self._update_preview()

    def _preview_box(self, title_text: str, icon: str, target: QListWidget) -> QWidget:
        box = QFrame()
        box.setObjectName("batchRenamePreview")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(2, 0, 2, 0)
        header_layout.setSpacing(5)
        icon_label = QLabel()
        icon_label.setPixmap(_fomantic_icon(icon, 14, "#a8b0bd").pixmap(QSize(14, 14)))
        header_layout.addWidget(icon_label)
        title = QLabel(title_text)
        title.setObjectName("batchRenamePreviewTitle")
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        layout.addWidget(header)
        target.setObjectName("batchRenameList")
        target.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        layout.addWidget(target, 1)
        return box

    def _token_button(self, icon: str, text: str, callback: Callable) -> QToolButton:
        button = QToolButton()
        button.setObjectName("batchRenameToken")
        button.setIcon(_fomantic_icon(icon, 13, "#d6dce4"))
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.clicked.connect(callback)
        return button

    def _insert_counter(self) -> None:
        self._insert_token(f"{{counter:0{self.counter_digits.value()}}}")

    def _insert_token(self, token: str) -> None:
        raw = self.template_edit.text()
        position = self.template_edit.cursorPosition()
        self.template_edit.setText(raw[:position] + token + raw[position:])
        self.template_edit.setCursorPosition(position + len(token))
        self._update_preview()
        self.template_edit.setFocus()

    def names(self) -> dict[str, str]:
        return dict(self._names)

    def _request_rename(self) -> None:
        if self._names:
            self.renameRequested.emit(dict(self._names))

    def set_renaming(self, total: int) -> None:
        self._renaming = True
        self.template_edit.setEnabled(False)
        self.counter_start.setEnabled(False)
        self.counter_digits.setEnabled(False)
        self.rename_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.rename_progress.setRange(0, max(1, total))
        self.rename_progress.setValue(0)
        self.rename_progress.setFormat("Переименование: 0/%m")
        self.rename_progress.show()

    def update_rename_progress(self, completed: int, total: int) -> None:
        self.rename_progress.setRange(0, max(1, total))
        self.rename_progress.setValue(completed)
        self.rename_progress.setFormat(f"Переименование: {completed}/{total}")
        QApplication.processEvents()

    def rename_failed(self, message: str) -> None:
        self._renaming = False
        self.rename_progress.hide()
        self.template_edit.setEnabled(True)
        self.counter_start.setEnabled(True)
        self.counter_digits.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self._update_preview()
        self.validation_label.setText(message)
        self.validation_label.setProperty("invalid", True)
        self.validation_label.style().unpolish(self.validation_label)
        self.validation_label.style().polish(self.validation_label)

    def reject(self) -> None:
        if not self._renaming:
            super().reject()

    def accept(self) -> None:
        self.settings.setValue("batch_rename/template", self.template_edit.text())
        self.settings.setValue("batch_rename/counter_start", self.counter_start.value())
        self.settings.setValue("batch_rename/counter_digits", self.counter_digits.value())
        super().accept()

    def _update_preview(self) -> None:
        self._before_list.clear()
        self._after_list.clear()
        template = self.template_edit.text()
        candidates: dict[str, str] = {}
        errors: list[str] = []
        for index, path in enumerate(self.paths):
            self._before_list.addItem(path.name)
            try:
                stem = self._render_stem(template, path, index)
                name = f"{stem}{path.suffix}"
                self._validate_name(name)
            except ValueError as exc:
                name = "—"
                errors.append(str(exc))
            candidates[path.name] = name
            self._after_list.addItem(name)
        target_keys = [name.casefold() for name in candidates.values() if name != "—"]
        if len(target_keys) != len(set(target_keys)):
            errors.append("Шаблон создаёт одинаковые имена файлов.")
        for name in candidates.values():
            if name == "—":
                continue
            target = self.paths[0].parent / name
            if target.exists() and not any(target.samefile(source) for source in self.paths):
                errors.append(f"Файл «{name}» уже существует в папке.")
                break
        self._names = candidates if not errors else {}
        self.rename_button.setEnabled(bool(self._names) and any(old != new for old, new in self._names.items()))
        self.validation_label.setText(errors[0] if errors else f"Будет переименовано: {sum(old != new for old, new in candidates.items())} из {len(candidates)}")
        self.validation_label.setProperty("invalid", bool(errors))
        self.validation_label.style().unpolish(self.validation_label)
        self.validation_label.style().polish(self.validation_label)

    def _render_stem(self, template: str, path: Path, index: int) -> str:
        detail = self.details.get(path.name, {})
        raw_datetime = detail.get("original_datetime")
        try:
            captured = datetime.fromisoformat(str(raw_datetime)) if raw_datetime else None
        except (TypeError, ValueError):
            captured = None
        def replace(match: re.Match) -> str:
            width, token = match.groups()
            if width is not None:
                return f"{self.counter_start.value() + index:0{int(width)}d}"
            if match.group(0) == "{counter}":
                return str(self.counter_start.value() + index)
            if captured is None:
                raise ValueError(f"У файла «{path.name}» нет даты и времени съёмки в EXIF.")
            return {
                "year": captured.strftime("%Y"),
                "month": captured.strftime("%m"),
                "day": captured.strftime("%d"),
                "hour": captured.strftime("%H"),
                "minute": captured.strftime("%M"),
                "second": captured.strftime("%S"),
                "date": captured.strftime("%Y-%m-%d"),
                "time": captured.strftime("%H-%M-%S"),
                "datetime": captured.strftime("%Y-%m-%d_%H-%M-%S"),
            }[token]

        return self._safe_stem(self._token_pattern.sub(replace, template))

    @staticmethod
    def _safe_stem(value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().rstrip(". ")
        return cleaned

    @staticmethod
    def _validate_name(name: str) -> None:
        if not name or name in {".", ".."} or name.rstrip(". ") != name:
            raise ValueError("Шаблон не создаёт корректное имя файла.")
        stem = Path(name).stem.upper()
        if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise ValueError("Шаблон создаёт зарезервированное имя Windows.")


class BatchResizeDialog(QDialog):
    startRequested = Signal(object)

    def __init__(self, source_dir: Path, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setObjectName("batchResizeDialog")
        self.setWindowTitle("Групповой резайс")
        self.setModal(True)
        self.resize(570, 370)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        title = QLabel("Групповой резайс")
        title.setObjectName("batchRenameTitle")
        layout.addWidget(title)
        hint = QLabel("Экспортирует текущий отсортированный список в JPEG. RAW-файлы используют встроенное превью, если оно есть.")
        hint.setObjectName("batchRenameHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        folder_row = QHBoxLayout()
        folder_label = QLabel("Папка экспорта")
        folder_label.setObjectName("batchResizeFieldLabel")
        folder_row.addWidget(folder_label)
        self.output_edit = QLineEdit(str(source_dir / "resized"))
        self.output_edit.setObjectName("batchResizeOutput")
        folder_row.addWidget(self.output_edit, 1)
        browse = QToolButton()
        browse.setObjectName("batchResizeBrowse")
        browse.setIcon(_fomantic_icon("folder", 15))
        browse.setToolTip("Выбрать папку")
        browse.clicked.connect(self._choose_output_folder)
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)

        size_row = QHBoxLayout()
        size_label = QLabel("Большая сторона")
        size_label.setObjectName("batchResizeFieldLabel")
        size_row.addWidget(size_label)
        self.max_side = QSpinBox()
        self.max_side.setObjectName("batchResizeSpin")
        self.max_side.setRange(64, 20_000)
        self.max_side.setValue(self.settings.value("batch_resize/max_side", 1920, int))
        self.max_side.setSuffix(" px")
        size_row.addWidget(self.max_side)
        size_row.addStretch(1)
        layout.addLayout(size_row)

        options = QFrame()
        options.setObjectName("batchResizeOptions")
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(2, 3, 2, 3)
        self.sharpen = SettingsCheckBox("Шарп")
        self.unsharp = SettingsCheckBox("Unsharp Mask")
        self.keep_exif = SettingsCheckBox("Сохранить EXIF")
        for option in (self.sharpen, self.unsharp, self.keep_exif):
            option.setObjectName("batchResizeOption")
        self.sharpen.setChecked(self.settings.value("batch_resize/sharpen", False, bool))
        self.unsharp.setChecked(self.settings.value("batch_resize/unsharp", True, bool))
        self.keep_exif.setChecked(self.settings.value("batch_resize/keep_exif", True, bool))
        options_layout.addWidget(self.sharpen)
        sharpen_settings = QHBoxLayout()
        sharpen_settings.setContentsMargins(24, 0, 0, 0)
        sharpen_strength_label = QLabel("Сила")
        sharpen_strength_label.setObjectName("batchResizeSettingLabel")
        sharpen_settings.addWidget(sharpen_strength_label)
        self.sharpen_amount = QSpinBox()
        self.sharpen_amount.setObjectName("batchResizeSpin")
        self.sharpen_amount.setRange(0, 500)
        self.sharpen_amount.setValue(self.settings.value("batch_resize/sharpen_amount", 125, int))
        self.sharpen_amount.setSuffix(" %")
        sharpen_settings.addWidget(self.sharpen_amount)
        sharpen_settings.addStretch(1)
        options_layout.addLayout(sharpen_settings)
        options_layout.addWidget(self.unsharp)
        unsharp_settings = QHBoxLayout()
        unsharp_settings.setContentsMargins(24, 0, 0, 0)
        unsharp_radius_label = QLabel("Радиус")
        unsharp_radius_label.setObjectName("batchResizeSettingLabel")
        unsharp_settings.addWidget(unsharp_radius_label)
        self.unsharp_radius = QDoubleSpinBox()
        self.unsharp_radius.setObjectName("batchResizeSpin")
        self.unsharp_radius.setRange(0.1, 10.0)
        self.unsharp_radius.setSingleStep(0.1)
        self.unsharp_radius.setValue(self.settings.value("batch_resize/unsharp_radius", 0.3, float))
        unsharp_settings.addWidget(self.unsharp_radius)
        unsharp_strength_label = QLabel("Сила")
        unsharp_strength_label.setObjectName("batchResizeSettingLabel")
        unsharp_settings.addWidget(unsharp_strength_label)
        self.unsharp_amount = QSpinBox()
        self.unsharp_amount.setObjectName("batchResizeSpin")
        self.unsharp_amount.setRange(1, 500)
        self.unsharp_amount.setValue(self.settings.value("batch_resize/unsharp_amount", 220, int))
        self.unsharp_amount.setSuffix(" %")
        unsharp_settings.addWidget(self.unsharp_amount)
        unsharp_threshold_label = QLabel("Порог")
        unsharp_threshold_label.setObjectName("batchResizeSettingLabel")
        unsharp_settings.addWidget(unsharp_threshold_label)
        self.unsharp_threshold = QSpinBox()
        self.unsharp_threshold.setObjectName("batchResizeSpin")
        self.unsharp_threshold.setRange(0, 255)
        self.unsharp_threshold.setValue(self.settings.value("batch_resize/unsharp_threshold", 4, int))
        unsharp_settings.addWidget(self.unsharp_threshold)
        unsharp_settings.addStretch(1)
        options_layout.addLayout(unsharp_settings)
        options_layout.addWidget(self.keep_exif)
        self.sharpen.toggled.connect(self.sharpen_amount.setEnabled)
        self.unsharp.toggled.connect(self.unsharp_radius.setEnabled)
        self.unsharp.toggled.connect(self.unsharp_amount.setEnabled)
        self.unsharp.toggled.connect(self.unsharp_threshold.setEnabled)
        self.sharpen_amount.setEnabled(self.sharpen.isChecked())
        self.unsharp_radius.setEnabled(self.unsharp.isChecked())
        self.unsharp_amount.setEnabled(self.unsharp.isChecked())
        self.unsharp_threshold.setEnabled(self.unsharp.isChecked())
        layout.addWidget(options)
        layout.addStretch(1)
        self.status = QLabel()
        self.status.setObjectName("batchResizeStatus")
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setObjectName("batchResizeProgress")
        self.progress.setFixedHeight(0)
        self.progress.hide()
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setObjectName("batchResizeSecondaryButton")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.start_button = QPushButton("Старт")
        self.start_button.setObjectName("batchResizePrimaryButton")
        self.start_button.setIcon(_fomantic_icon("play", 13, "#ffffff"))
        self.start_button.clicked.connect(self._start)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)

    def _choose_output_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Папка для экспорта", self.output_edit.text())
        if chosen:
            self.output_edit.setText(chosen)

    def _start(self) -> None:
        output_text = self.output_edit.text().strip()
        if not output_text:
            self.status.setText("Укажите папку для экспорта.")
            return
        output = Path(output_text).expanduser()
        for key, value in {
            "max_side": self.max_side.value(), "sharpen": self.sharpen.isChecked(),
            "sharpen_amount": self.sharpen_amount.value(), "unsharp": self.unsharp.isChecked(),
            "unsharp_radius": self.unsharp_radius.value(), "unsharp_amount": self.unsharp_amount.value(),
            "unsharp_threshold": self.unsharp_threshold.value(), "keep_exif": self.keep_exif.isChecked(),
        }.items():
            self.settings.setValue(f"batch_resize/{key}", value)
        self.startRequested.emit({
            "output_dir": output, "max_side": self.max_side.value(), "sharpen": self.sharpen.isChecked(),
            "sharpen_amount": self.sharpen_amount.value(), "unsharp": self.unsharp.isChecked(),
            "unsharp_radius": self.unsharp_radius.value(), "unsharp_amount": self.unsharp_amount.value(),
            "unsharp_threshold": self.unsharp_threshold.value(), "keep_exif": self.keep_exif.isChecked(),
        })

    def set_running(self, total: int) -> None:
        for widget in (self.output_edit, self.max_side, self.sharpen, self.sharpen_amount, self.unsharp, self.unsharp_radius, self.unsharp_amount, self.unsharp_threshold, self.keep_exif, self.start_button, self.cancel_button):
            widget.setEnabled(False)
        self.progress.setRange(0, total)
        self.progress.setValue(0)
        self.progress.setFixedHeight(20)
        self.progress.show()

    def update_progress(self, value: int, total: int) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(value)
        self.progress.setFormat(f"Экспорт: {value}/{total}")
        QApplication.processEvents()


class ShrinkJpegDialog(QDialog):
    """Re-compress every JPEG in the current folder in place at a chosen quality."""

    startRequested = Signal(object)

    def __init__(self, source_dir: Path, count: int, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setObjectName("shrinkJpegDialog")
        self.setWindowTitle("Уменьшить JPG")
        self.setModal(True)
        self.resize(520, 300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        title = QLabel("Уменьшить JPG")
        title.setObjectName("batchRenameTitle")
        layout.addWidget(title)
        hint = QLabel(
            f"Пересохранит все JPG-файлы в папке «{source_dir.name}» ({count} шт.) "
            "с выбранным качеством, поверх исходников без подтверждения."
        )
        hint.setObjectName("batchRenameHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        quality_row = QHBoxLayout()
        quality_label = QLabel("Качество")
        quality_label.setObjectName("batchResizeFieldLabel")
        quality_row.addWidget(quality_label)
        self.quality = QSpinBox()
        self.quality.setObjectName("batchResizeSpin")
        self.quality.setRange(1, 100)
        self.quality.setValue(self.settings.value("shrink_jpeg/quality", 85, int))
        self.quality.setSuffix(" %")
        quality_row.addWidget(self.quality)
        quality_row.addStretch(1)
        layout.addLayout(quality_row)

        options = QFrame()
        options.setObjectName("batchResizeOptions")
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(2, 3, 2, 3)
        self.keep_exif = SettingsCheckBox("Сохранить EXIF")
        self.keep_exif.setObjectName("batchResizeOption")
        self.keep_exif.setChecked(self.settings.value("shrink_jpeg/keep_exif", True, bool))
        options_layout.addWidget(self.keep_exif)
        layout.addWidget(options)
        layout.addStretch(1)

        self.status = QLabel()
        self.status.setObjectName("batchResizeStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setObjectName("batchResizeProgress")
        self.progress.setFixedHeight(0)
        self.progress.hide()
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setObjectName("batchResizeSecondaryButton")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.start_button = QPushButton("Старт")
        self.start_button.setObjectName("batchResizePrimaryButton")
        self.start_button.setIcon(_fomantic_icon("play", 13, "#ffffff"))
        self.start_button.clicked.connect(self._start)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)

    def _start(self) -> None:
        self.settings.setValue("shrink_jpeg/quality", self.quality.value())
        self.settings.setValue("shrink_jpeg/keep_exif", self.keep_exif.isChecked())
        self.startRequested.emit({
            "quality": self.quality.value(),
            "keep_exif": self.keep_exif.isChecked(),
        })

    def set_running(self, total: int) -> None:
        for widget in (self.quality, self.keep_exif, self.start_button, self.cancel_button):
            widget.setEnabled(False)
        self.progress.setRange(0, total)
        self.progress.setValue(0)
        self.progress.setFixedHeight(20)
        self.progress.show()

    def update_progress(self, value: int, total: int) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(value)
        self.progress.setFormat(f"Сжатие: {value}/{total}")
        QApplication.processEvents()
