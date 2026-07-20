## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Модальные диалоги, вынесенные из главного окна ради его душевного здоровья."""

from __future__ import annotations

import re
import sys

from PySide6.QtCore import QEvent, QKeyCombination, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QButtonGroup, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QRadioButton, QScrollArea, QSpinBox, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget, QTextEdit
from datetime import datetime
from pathlib import Path
from typing import Callable
from .hotkeys import FIXED_HOTKEYS, HOTKEY_DEFAULTS, _hotkey_sequence, _uses_reserved_navigation_key
from .error_log import clear_error_log, read_error_log
from .runtime_paths import filesystem_name_key
from .shotsync_client import ShotSyncClient
from .theme import _fomantic_icon
from .widgets import CodeCompletingLineEdit, CodeReplacementsEditor, SettingsCheckBox
from .version import __version__ as APP_VERSION


class HelpDialog(QDialog):
    """Показывает справку только для чтения и открывает ссылки с клавиатуры."""

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

        table = QTableWidget(len(HOTKEY_DEFAULTS) + len(FIXED_HOTKEYS), 2)
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
        for row, (label, sequence) in enumerate(FIXED_HOTKEYS, start=len(HOTKEY_DEFAULTS)):
            table.setItem(row, 0, QTableWidgetItem(label))
            table.setItem(row, 1, QTableWidgetItem(sequence))
        layout.addWidget(table, 1)

        close = QPushButton("Закрыть")
        close.setObjectName("helpDialogCloseButton")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)


class ErrorLogDialog(QDialog):
    """Показывает локальный stderr без необходимости искать файл в проводнике."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("errorLogDialog")
        self.setWindowTitle("Лог ошибок")
        self.setModal(True)
        self.resize(820, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(10)
        title = QLabel("Лог ошибок")
        title.setObjectName("settingsDialogTitle")
        layout.addWidget(title)
        hint = QLabel("Необработанные ошибки приложения и фоновых потоков. Лог остаётся только на этом компьютере.")
        hint.setObjectName("settingsHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.content = QTextEdit()
        self.content.setObjectName("errorLogContent")
        self.content.setReadOnly(True)
        self.content.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.content.setPlainText(read_error_log())
        layout.addWidget(self.content, 1)

        buttons = QHBoxLayout()
        clear = QPushButton("Очистить")
        clear.setObjectName("settingsSecondaryButton")
        clear.clicked.connect(self._clear)
        buttons.addWidget(clear)
        buttons.addStretch(1)
        copy = QPushButton("Копировать")
        copy.setObjectName("settingsSecondaryButton")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.content.toPlainText()))
        buttons.addWidget(copy)
        close = QPushButton("Закрыть")
        close.setObjectName("settingsPrimaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def _clear(self) -> None:
        """Очищает файл и виджет одним действием, чтобы они не расходились."""
        clear_error_log()
        self.content.clear()


class SettingsDialog(QDialog):
    """Собирает настройки интерфейса, горячих клавиш, AI, XMP и интеграций.

    Диалог читает исходные значения из ``QSettings``, даёт отредактировать их в
    тематических разделах и сохраняет согласованным набором после подтверждения.
    Внешние компоненты получают изменения уже через вызывающий код, поэтому
    половина интерфейса не успевает обновиться раньше второй.
    """

    def __init__(self, settings: QSettings, client: ShotSyncClient, changed: Callable[[list[dict]], None], login_requested: Callable[[], bool], update_requested: Callable[[], None], cache_size_provider: Callable[[], int], clear_cache_requested: Callable[[], None], parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.update_requested = update_requested
        self.cache_size_provider = cache_size_provider
        self.clear_cache_requested = clear_cache_requested
        self.setObjectName("settingsDialog")
        self.setWindowTitle("Настройки")
        self.setModal(True)
        screen = QApplication.primaryScreen()
        max_height = int(screen.availableGeometry().height() * 0.8) if screen else 540
        self.setMaximumHeight(max_height)
        self.resize(700, min(540, max_height))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)

        title = QLabel("Настройки")
        title.setObjectName("settingsDialogTitle")
        layout.addWidget(title)

        tabs = QTabWidget()
        tabs.setObjectName("settingsTabs")
        tabs.addTab(self._scrollable_settings_tab(self._behavior_tab()), "Поведение")
        tabs.addTab(self._scrollable_settings_tab(self._hotkeys_tab()), "Горячие клавиши")
        self.code_replacements_editor = CodeReplacementsEditor(client, settings, changed, login_requested)
        tabs.addTab(self._scrollable_settings_tab(self.code_replacements_editor), "Коды замен")
        tabs.addTab(self._scrollable_settings_tab(self._interface_tab()), "Интерфейс")
        tabs.addTab(self._scrollable_settings_tab(self._about_tab()), "О приложении")
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

    @staticmethod
    def _scrollable_settings_tab(content: QWidget) -> QScrollArea:
        """Даёт длинной вкладке прокрутку, не увеличивая окно за пределы экрана."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll

    def _behavior_tab(self) -> QWidget:
        """Собирает настройки поведения просмотра, файлов и фонового анализа."""
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
        self.delete_permanently_on_del = SettingsCheckBox(
            "Удалять сразу по DEL, без корзины (Shift+DEL — в корзину)"
        )
        self.delete_permanently_on_del.setChecked(
            self.settings.value("behavior/delete_permanently_on_del", False, bool)
        )
        layout.addWidget(self.delete_permanently_on_del)
        self.use_transfer_queue = SettingsCheckBox(
            "Выполнять копирование и перемещение последовательно"
        )
        self.use_transfer_queue.setChecked(
            self.settings.value("transfers/use_queue", True, bool)
        )
        layout.addWidget(self.use_transfer_queue)
        transfer_queue_hint = QLabel(
            "Новые файловые операции становятся в общую очередь. Если выключить, "
            "до трёх операций смогут выполняться одновременно."
        )
        transfer_queue_hint.setObjectName("settingsHint")
        transfer_queue_hint.setWordWrap(True)
        layout.addWidget(transfer_queue_hint)
        self.auto_rename_transfer_conflicts = SettingsCheckBox(
            "Не спрашивать при совпадении имён, сразу переименовывать"
        )
        self.auto_rename_transfer_conflicts.setChecked(
            self.settings.value("transfers/auto_rename_conflicts", True, bool)
        )
        layout.addWidget(self.auto_rename_transfer_conflicts)

        card_import_card = QFrame()
        card_import_card.setObjectName("externalEditorCard")
        card_import_layout = QVBoxLayout(card_import_card)
        card_import_layout.setContentsMargins(14, 13, 14, 14)
        card_import_layout.setSpacing(7)
        card_import_heading = QLabel("Импорт с карты памяти")
        card_import_heading.setObjectName("externalEditorTitle")
        card_import_layout.addWidget(card_import_heading)
        card_import_hint = QLabel(
            "Эти параметры применяются к каждому новому импорту с подключённой карты памяти."
        )
        card_import_hint.setObjectName("externalEditorHint")
        card_import_hint.setWordWrap(True)
        card_import_layout.addWidget(card_import_hint)
        self.card_import_flatten = SettingsCheckBox("Все файлы в одну папку")
        self.card_import_flatten.setChecked(self.settings.value("card_import/flatten", True, bool))
        card_import_layout.addWidget(self.card_import_flatten)
        self.card_import_delete_sources = SettingsCheckBox("Удалять исходные файлы с карты")
        self.card_import_delete_sources.setChecked(self.settings.value("card_import/delete_sources", True, bool))
        card_import_layout.addWidget(self.card_import_delete_sources)
        self.card_import_backup_enabled = SettingsCheckBox("Сделать резервную копию")
        self.card_import_backup_enabled.setChecked(self.settings.value("card_import/backup_enabled", False, bool))
        card_import_layout.addWidget(self.card_import_backup_enabled)
        backup_row = QHBoxLayout()
        backup_row.setContentsMargins(24, 0, 0, 0)
        backup_row.setSpacing(8)
        remembered_backup = self.settings.value("card_import/backup_destination", "", str)
        self.card_import_backup_destination = QLineEdit("" if remembered_backup in {"", "."} else remembered_backup)
        self.card_import_backup_destination.setObjectName("editorExecutable")
        self.card_import_backup_destination.setReadOnly(True)
        self.card_import_backup_destination.setPlaceholderText("Папка резервной копии")
        backup_row.addWidget(self.card_import_backup_destination, 1)
        self.card_import_backup_browse = QToolButton()
        self.card_import_backup_browse.setObjectName("editorBrowseButton")
        self.card_import_backup_browse.setIcon(_fomantic_icon("folder", 15, "#c9c9c9"))
        self.card_import_backup_browse.setIconSize(QSize(15, 15))
        self.card_import_backup_browse.setToolTip("Выбрать папку резервной копии")
        self.card_import_backup_browse.clicked.connect(self._choose_card_import_backup_directory)
        backup_row.addWidget(self.card_import_backup_browse)
        card_import_layout.addLayout(backup_row)
        self.card_import_backup_destination.setEnabled(self.card_import_backup_enabled.isChecked())
        self.card_import_backup_browse.setEnabled(self.card_import_backup_enabled.isChecked())
        self.card_import_backup_enabled.toggled.connect(self.card_import_backup_destination.setEnabled)
        self.card_import_backup_enabled.toggled.connect(self.card_import_backup_browse.setEnabled)
        layout.addWidget(card_import_card)

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
        self.editor_executable.setPlaceholderText("Путь к приложению или исполняемому файлу")
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
            self.default_app_button = QPushButton("Выбрать по умолчанию для JPG и RAW…")
            self.default_app_button.setObjectName("settingsPrimaryButton")
            self.default_app_button.clicked.connect(self._choose_default_photo_app)
            integration_layout.addWidget(self.default_app_button, 0, Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(integration_card)
            self._refresh_explorer_integration_button()
        layout.addStretch(1)
        self._update_editor_choice_state(self.custom_editor.isChecked())
        return tab

    def _update_editor_choice_state(self, use_custom: bool) -> None:
        self.custom_editor_controls.setVisible(use_custom)

    def _choose_card_import_backup_directory(self) -> None:
        """Выбирает постоянную папку для резервных копий импорта с карты."""
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Папка резервной копии",
            self.card_import_backup_destination.text(),
        )
        if chosen:
            self.card_import_backup_destination.setText(chosen)

    def _choose_editor_executable(self) -> None:
        if sys.platform == "win32":
            file_filter = "Программы (*.exe);;Все файлы (*)"
        elif sys.platform == "darwin":
            file_filter = "Приложения (*.app);;Все файлы (*)"
        else:
            file_filter = "Исполняемые файлы (*);;Все файлы (*)"
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите редактор",
            self.editor_executable.text().strip(),
            file_filter,
        )
        if path:
            self.editor_executable.setText(path)

    def _refresh_explorer_integration_button(self) -> None:
        from .windows_integration import is_registered

        if not getattr(sys, "frozen", False):
            self.explorer_integration_button.setText("Доступно в собранном приложении")
            self.explorer_integration_button.setEnabled(False)
            self.explorer_integration_button.setToolTip("Соберите приложение, чтобы Проводник запускал ctrlka.exe.")
            self.default_app_button.setEnabled(False)
            self.default_app_button.setToolTip("Доступно в собранном приложении Контрольки.")
            return
        self.explorer_integration_button.setEnabled(True)
        self.default_app_button.setEnabled(True)
        self.explorer_integration_button.setText("Удалить из Проводника" if is_registered() else "Добавить в Проводник")

    def _choose_default_photo_app(self) -> None:
        """Регистрирует поддерживаемые форматы и передаёт выбор приложению Windows."""
        from .windows_integration import open_default_apps_settings, register_default_app

        if not getattr(sys, "frozen", False):
            return
        try:
            register_default_app(Path(sys.executable))
            open_default_apps_settings()
        except OSError as exc:
            QMessageBox.warning(self, "Не удалось открыть настройки Windows", str(exc))

    def _toggle_explorer_integration(self) -> None:
        """Регистрирует или удаляет команды Контрольки в Проводнике Windows."""
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
        """Собирает редактор горячих клавиш и проверяет конфликты."""
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
        """Собирает настройки внешнего вида сетки и полноэкранного режима."""
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
        self.zoom_focus_face = SettingsCheckBox("Акцент на лице при зуме")
        self.zoom_focus_face.setChecked(
            self.settings.value("interface/zoom_focus_face", True, bool)
        )
        layout.addWidget(self.zoom_focus_face)
        layout.addStretch(1)
        return tab

    def _about_tab(self) -> QWidget:
        """Собирает сведения о версии, лицензии и полезные ссылки."""
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
        description = QLabel(
            "Бесплатное приложение для быстрого просмотра и отбора RAW и JPG. "
            "Оценки, цветовые метки, серии, поиск лиц, горячие клавиши и экспорт в XMP — "
            "чтобы быстрее перейти от съёмки к готовой работе."
        )
        description.setObjectName("settingsHint")
        description.setWordWrap(True)
        layout.addWidget(description)
        author = QLabel(
            "<b>Автор:</b> Игорь Заломский &lt;"
            "<a href=\"mailto:igor@zalomskij.ru\">igor@zalomskij.ru</a>&gt;<br>"
            "© 2026 Игорь Заломский. Лицензия GNU GPL v3 или более поздней версии.<br>"
            "<a href=\"https://shotsync.ru/ctrlka\">Контролька на ShotSync</a>"
            "<br><a href=\"https://github.com/kaist/RAWww\">Исходный код на GitHub</a>"
        )
        author.setObjectName("settingsHint")
        author.setWordWrap(True)
        author.setOpenExternalLinks(True)
        layout.addWidget(author)
        credits = QLabel(
            "<b>Определение закрытых глаз:</b> разметка лица "
            "<a href=\"https://github.com/deepinsight/insightface\">InsightFace</a> "
            "(модель 2d106det), по контуру век считается eye aspect ratio."
        )
        credits.setObjectName("settingsHint")
        credits.setWordWrap(True)
        credits.setOpenExternalLinks(True)
        layout.addWidget(credits)
        self.auto_update_check = SettingsCheckBox("Автоматически проверять обновления при запуске")
        self.auto_update_check.setChecked(self.settings.value("updates/auto_check", True, bool))
        layout.addWidget(self.auto_update_check)
        self.disable_usage_statistics = SettingsCheckBox("Не отправлять статистику использования")
        self.disable_usage_statistics.setChecked(
            self.settings.value("telemetry/disable_usage_statistics", False, bool)
        )
        layout.addWidget(self.disable_usage_statistics)
        check = QPushButton("Проверить обновления")
        check.setObjectName("settingsPrimaryButton")
        check.clicked.connect(lambda: self.update_requested())
        layout.addWidget(check, 0, Qt.AlignmentFlag.AlignLeft)
        error_log = QPushButton("Лог ошибок")
        error_log.setObjectName("settingsSecondaryButton")
        error_log.clicked.connect(lambda: ErrorLogDialog(self).exec())
        layout.addWidget(error_log, 0, Qt.AlignmentFlag.AlignLeft)
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
        """Меняет цифровые сочетания рейтинга, не затрагивая остальные клавиши."""
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
        """Проверяет значения и сохраняет настройки одним согласованным набором."""
        if not self.code_replacements_editor.commit_pending_code():
            return
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
        self.settings.setValue("behavior/delete_permanently_on_del", self.delete_permanently_on_del.isChecked())
        self.settings.setValue("transfers/use_queue", self.use_transfer_queue.isChecked())
        self.settings.setValue(
            "transfers/auto_rename_conflicts",
            self.auto_rename_transfer_conflicts.isChecked(),
        )
        self.settings.setValue("card_import/flatten", self.card_import_flatten.isChecked())
        self.settings.setValue("card_import/delete_sources", self.card_import_delete_sources.isChecked())
        self.settings.setValue("card_import/backup_enabled", self.card_import_backup_enabled.isChecked())
        self.settings.setValue(
            "card_import/backup_destination",
            self.card_import_backup_destination.text().strip(),
        )
        self.settings.setValue("ai/auto_after_previews", self.auto_ai_after_previews.isChecked())
        self.settings.setValue("interface/show_full_view_counter", self.show_full_view_counter.isChecked())
        self.settings.setValue(
            "interface/show_full_view_mark_indicator",
            self.show_full_view_mark_indicator.isChecked(),
        )
        self.settings.setValue("interface/zoom_focus_face", self.zoom_focus_face.isChecked())
        self.settings.setValue("editor/use_custom_executable", self.custom_editor.isChecked())
        self.settings.setValue("editor/executable", self.editor_executable.text().strip())
        self.settings.setValue("hotkeys/swap_rating_and_color", self.swap_rating_color.isChecked())
        self.settings.setValue("updates/auto_check", self.auto_update_check.isChecked())
        self.settings.setValue("telemetry/disable_usage_statistics", self.disable_usage_statistics.isChecked())
        for identifier, sequence in sequences.items():
            self.settings.setValue(f"hotkeys/{identifier}", sequence.toString(QKeySequence.SequenceFormat.PortableText))
        self.accept()


class QuickTransferDialog(QDialog):
    """Выбирает папку быстрого переноса с полноценным управлением клавиатурой.

    Список собирается из последней цели, открытых вкладок и истории. Диалог
    поддерживает поиск, стрелки и цифровые сочетания, а наружу возвращает только
    выбранный путь и режим копирования или перемещения. Файлы он не трогает:
    пользователь ещё может передумать, а ``Workspace`` проверит конфликты имён.
    """

    def __init__(self, operation: str, destinations: list[Path], hotkey: QKeySequence, accepted: Callable, parent=None) -> None:
        super().__init__(parent)
        self.hotkey, self._accepted = hotkey, accepted
        self._submitted = False
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
        self.destinations = QListWidget()
        self.destinations.setObjectName("quickTransferDestinations")
        self.destinations.installEventFilter(self)
        for number, destination in enumerate(destinations[:9], start=1):
            item = QListWidgetItem(f"{number}.  {destination}")
            item.setData(Qt.ItemDataRole.UserRole, destination)
            self.destinations.addItem(item)
        self.destinations.itemActivated.connect(lambda item: self._choose_item(item, True))
        layout.addWidget(self.destinations)
        self.repeat_shortcut = QShortcut(hotkey, self)
        self.repeat_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.repeat_shortcut.activated.connect(lambda: self._choose_selected(True))
        self.confirm_shortcuts: list[QShortcut] = []
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda: self._choose_selected(True))
            self.confirm_shortcuts.append(shortcut)
        self.escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.escape_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.escape_shortcut.activated.connect(self.reject)
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
        if item is None or self._submitted:
            return
        destination = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(destination, Path) and destination.is_dir():
            self._submitted = True
            self.destinations.setEnabled(False)
            self._accepted(destination, update_recent)
            self.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() in (
                Qt.KeyboardModifier.NoModifier,
                Qt.KeyboardModifier.KeypadModifier,
            )
        ):
            self._choose_selected(True)
            return
        if self._choose_number(event):
            return
        sequence = QKeySequence(QKeyCombination(event.modifiers(), Qt.Key(event.key())))
        if self.hotkey.matches(sequence) == QKeySequence.SequenceMatch.ExactMatch:
            self._choose_selected(True)
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.destinations and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._choose_selected(True)
                return True
            if self._choose_number(event):
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
    """Настраивает пакетное переименование и показывает будущие имена файлов.

    Предпросмотр строится в том же порядке, что видит пользователь в сетке,
    проверяет пустые имена и конфликты до изменения диска. Само переименование
    выполняет ``Workspace`` — диалог лишь возвращает проверенный план.
    """

    renameRequested = Signal(object)
    _preview_limit = 300
    _token_pattern = re.compile(
        r"\{counter(?::(\d+))?\}|\{(year|month|day|hour|minute|second|date|time|datetime)\}"
    )

    def __init__(self, paths: list[Path], details: dict[str, dict], settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.paths = paths
        self.details = details
        self.settings = settings
        self._renaming = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(180)
        self._preview_timer.timeout.connect(self._update_preview)
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
        self.template_edit.textEdited.connect(self._schedule_preview)
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
        self.counter_start.valueChanged.connect(self._schedule_preview)
        counter_row.addWidget(self.counter_start)
        self.counter_digits = QSpinBox()
        self.counter_digits.setObjectName("batchRenameSpin")
        self.counter_digits.setRange(1, 9)
        self.counter_digits.setValue(self.settings.value("batch_rename/counter_digits", 4, int))
        self.counter_digits.setSuffix(" цифры")
        self.counter_digits.valueChanged.connect(self._schedule_preview)
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
        self._schedule_preview()
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

    def set_cache_updating(self) -> None:
        """Объясняет паузу после файловой части, пока в фоне переносится SQLite-кэш."""
        self.rename_progress.setRange(0, 0)
        self.rename_progress.setFormat("Обновляю кэш…")

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
        """Пересчитывает таблицу будущих имён по текущему шаблону."""
        self._before_list.clear()
        self._after_list.clear()
        template = self.template_edit.text()
        candidates: dict[str, str] = {}
        errors: list[str] = []
        preview_before: list[str] = []
        preview_after: list[str] = []
        for index, path in enumerate(self.paths):
            try:
                stem = self._render_stem(template, path, index)
                name = f"{stem}{path.suffix}"
                self._validate_name(name)
            except ValueError as exc:
                name = "—"
                errors.append(str(exc))
            candidates[path.name] = name
            if index < self._preview_limit:
                preview_before.append(path.name)
                preview_after.append(name)
        if len(self.paths) > self._preview_limit:
            remainder = len(self.paths) - self._preview_limit
            preview_before.append(f"… ещё {remainder} файлов")
            preview_after.append(f"… ещё {remainder} новых имён")
        self._before_list.addItems(preview_before)
        self._after_list.addItems(preview_after)

        target_keys = {
            filesystem_name_key(name) for name in candidates.values() if name != "—"
        }
        if len(target_keys) != sum(name != "—" for name in candidates.values()):
            errors.append("Шаблон создаёт одинаковые имена файлов.")
        if self.paths:
            source_keys = {filesystem_name_key(path.name) for path in self.paths}
            try:
                existing_keys = {
                    filesystem_name_key(path.name) for path in self.paths[0].parent.iterdir()
                }
            except OSError:
                existing_keys = set()
            for name in candidates.values():
                key = filesystem_name_key(name)
                if name != "—" and key in existing_keys and key not in source_keys:
                    errors.append(f"Файл «{name}» уже существует в папке.")
                    break
        self._names = candidates if not errors else {}
        self.rename_button.setEnabled(bool(self._names) and any(old != new for old, new in self._names.items()))
        self.validation_label.setText(errors[0] if errors else f"Будет переименовано: {sum(old != new for old, new in candidates.items())} из {len(candidates)}")
        self.validation_label.setProperty("invalid", bool(errors))
        self.validation_label.style().unpolish(self.validation_label)
        self.validation_label.style().polish(self.validation_label)

    def _schedule_preview(self, *_args: object) -> None:
        """Откладывает тяжёлый пересчёт, пока пользователь продолжает вводить шаблон."""
        if not self._renaming:
            self._preview_timer.start()

    def _render_stem(self, template: str, path: Path, index: int) -> str:
        """Подставляет в шаблон имя, номер и EXIF-поля одного файла."""
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


class CardImportDialog(QDialog):
    """Собирает параметры импорта карты, не выполняя файловые операции сам."""

    def __init__(self, sources: list[Path | tuple[Path, str]], settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.sources = [
            source if isinstance(source, tuple) else (source, str(source))
            for source in sources
        ]
        self.settings = settings
        self.options: dict | None = None
        self.setObjectName("cardImportDialog")
        self.setWindowTitle("Импорт с карты памяти")
        self.setModal(True)
        self.setFixedWidth(590)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)

        title = QLabel("Импорт с карты памяти")
        title.setObjectName("cardImportTitle")
        layout.addWidget(title)

        source_label = QLabel("КАРТЫ ДЛЯ ИМПОРТА")
        source_label.setObjectName("cardImportSection")
        layout.addWidget(source_label)
        self.sources_list = QListWidget()
        self.sources_list.setObjectName("cardImportSources")
        self.sources_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        for source, label in self.sources:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, source)
            item.setSizeHint(QSize(0, 32))
            self.sources_list.addItem(item)
            check = SettingsCheckBox(label)
            check.setObjectName("cardImportSourceOption")
            check.setChecked(True)
            self.sources_list.setItemWidget(item, check)
        self.sources_list.setFixedHeight(10 + self.sources_list.count() * 32)
        layout.addWidget(self.sources_list)

        destination_box = QFrame()
        destination_box.setObjectName("cardImportDestination")
        destination_layout = QVBoxLayout(destination_box)
        destination_layout.setContentsMargins(12, 9, 12, 11)
        destination_layout.setSpacing(12)
        destination_row = QHBoxLayout()
        destination_row.setSpacing(8)
        destination_label = QLabel("Куда импортировать")
        destination_label.setObjectName("cardImportFieldLabel")
        destination_label.setFixedWidth(156)
        destination_row.addWidget(destination_label)
        self.destination_edit = QLineEdit(self.settings.value("card_import/destination", "", str))
        self.destination_edit.setObjectName("cardImportPath")
        self.destination_edit.setReadOnly(True)
        self.destination_edit.setFixedHeight(50)
        self.destination_edit.setPlaceholderText("Выберите основную папку")
        destination_row.addWidget(self.destination_edit, 1)
        destination_browse = QToolButton()
        destination_browse.setObjectName("cardImportBrowse")
        destination_browse.setFixedSize(QSize(50, 50))
        destination_browse.setIcon(_fomantic_icon("folder", 22))
        destination_browse.setToolTip("Выбрать папку")
        destination_browse.clicked.connect(lambda: self._choose_directory(self.destination_edit, "Папка для импорта"))
        destination_row.addWidget(destination_browse)
        destination_layout.addLayout(destination_row)

        mode_box = QFrame()
        mode_box.setObjectName("cardImportFolderMode")
        mode_layout = QVBoxLayout(mode_box)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_title = QLabel("КАТАЛОГ СЪЁМКИ")
        mode_title.setObjectName("cardImportSection")
        mode_layout.addWidget(mode_title)
        self.date_mode = QRadioButton("Создать каталог с датой съёмки")
        self.name_mode = QRadioButton("Создать каталог с названием съёмки")
        self.date_mode.setObjectName("cardImportDateMode")
        self.name_mode.setObjectName("cardImportNameMode")
        mode_group = QButtonGroup(self)
        mode_group.addButton(self.date_mode)
        mode_group.addButton(self.name_mode)
        mode_layout.addWidget(self.date_mode)
        mode_layout.addWidget(self.name_mode)
        self.shoot_name = QLineEdit(self.settings.value("card_import/shoot_name", "", str))
        self.shoot_name.setObjectName("cardImportShootName")
        self.shoot_name.setFixedHeight(54)
        self.shoot_name.setPlaceholderText("Название съёмки")
        mode_layout.addWidget(self.shoot_name)
        use_name = self.settings.value("card_import/folder_mode", "date", str) == "name"
        (self.name_mode if use_name else self.date_mode).setChecked(True)
        self.name_mode.toggled.connect(self.shoot_name.setEnabled)
        self.shoot_name.setEnabled(use_name)
        destination_layout.addWidget(mode_box)
        layout.addWidget(destination_box)

        self.status = QLabel()
        self.status.setObjectName("cardImportStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Отмена")
        cancel.setObjectName("settingsSecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        start = QPushButton("Начать")
        start.setObjectName("settingsPrimaryButton")
        start.clicked.connect(self._start)
        start.setDefault(True)
        buttons.addWidget(start)
        layout.addLayout(buttons)
        if use_name:
            self.shoot_name.setFocus(Qt.FocusReason.OtherFocusReason)
            self.shoot_name.selectAll()
        else:
            self.destination_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _choose_directory(self, edit: QLineEdit, title: str) -> None:
        chosen = QFileDialog.getExistingDirectory(self, title, edit.text())
        if chosen:
            edit.setText(chosen)

    def _start(self) -> None:
        destination_text = self.destination_edit.text().strip()
        if not destination_text:
            self.status.setText("Укажите основную папку для импорта.")
            self.destination_edit.setFocus()
            return
        destination = Path(destination_text).expanduser()
        shoot_name = self.shoot_name.text().strip()
        if self.name_mode.isChecked() and not shoot_name:
            self.status.setText("Введите название съёмки.")
            self.shoot_name.setFocus()
            return
        if shoot_name and (Path(shoot_name).name != shoot_name or shoot_name in {".", ".."}):
            self.status.setText("Название съёмки не должно содержать путь.")
            return
        backup_enabled = self.settings.value("card_import/backup_enabled", False, bool)
        backup_text = self.settings.value("card_import/backup_destination", "", str).strip()
        if backup_enabled and backup_text in {"", "."}:
            self.status.setText("Укажите папку резервной копии в Настройки → Поведение.")
            return
        backup = Path(backup_text).expanduser() if backup_text else None
        sources = [
            item.data(Qt.ItemDataRole.UserRole)
            for row in range(self.sources_list.count())
            if (item := self.sources_list.item(row))
            and isinstance(self.sources_list.itemWidget(item), SettingsCheckBox)
            and self.sources_list.itemWidget(item).isChecked()
        ]
        if not sources or not all(isinstance(source, Path) for source in sources):
            self.status.setText("Выберите хотя бы одну карту памяти.")
            return
        if backup_enabled and destination == backup:
            self.status.setText("Основная и резервная папки должны различаться.")
            return
        self.options = {
            "sources": sources, "destination": destination, "folder_mode": "name" if self.name_mode.isChecked() else "date",
            "shoot_name": shoot_name,
            "flatten": self.settings.value("card_import/flatten", True, bool),
            "delete_sources": self.settings.value("card_import/delete_sources", True, bool),
            "backup_enabled": backup_enabled,
            "backup_destination": backup,
        }
        for key, value in self.options.items():
            if key in {"destination", "folder_mode", "shoot_name"}:
                self.settings.setValue(f"card_import/{key}", str(value) if isinstance(value, Path) else value)
        self.accept()


class BatchResizeDialog(QDialog):
    """Настраивает пакетное изменение размера и заранее показывает результат.

    Диалог только собирает параметры и проверяет их согласованность. Файлы
    обрабатывает рабочая вкладка в отдельных процессах, поэтому окно не обязано
    героически зависать до конца всей съёмки.
    """

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
    """Настраивает пересжатие JPEG текущей папки с выбранным качеством."""

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
