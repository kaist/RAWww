## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Небольшие переиспользуемые виджеты для главного окна и диалогов."""

from __future__ import annotations

import re

from PySide6.QtCore import QEvent, QRect, QSettings, QStringListModel, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QComboBox, QCompleter, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QStyle, QStyleOptionButton, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget
from typing import Callable
from uuid import uuid4
from .shotsync_client import ShotSyncClient
from .theme import _fomantic_icon
from .i18n import gettext as _


class SettingsCheckBox(QCheckBox):
    """Флажок настроек с явно нарисованной галочкой в выбранном состоянии."""

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if not self.isChecked():
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        indicator = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#ffffff"), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(indicator.left() + 3, indicator.center().y(), indicator.left() + 7, indicator.bottom() - 4)
        painter.drawLine(indicator.left() + 7, indicator.bottom() - 4, indicator.right() - 3, indicator.top() + 4)
        painter.end()


class CodeReplacementsEditor(QWidget):
    """Редактирует локальные и серверные наборы кодов замены.

    Виджет встроен в настройки и управляет полным жизненным циклом наборов:
    загрузкой, созданием, переименованием, удалением, импортом и экспортом.
    При наличии авторизации изменения отправляются в ShotSync, без сети остаются
    в ``QSettings``. После каждой успешной операции вызывается ``changed``, чтобы
    открытые редакторы комментариев сразу увидели новый словарь.
    """

    def __init__(self, client: ShotSyncClient, settings: QSettings, changed: Callable[[list[dict]], None], login_requested: Callable[[], bool], parent=None) -> None:
        super().__init__(parent)
        self.client, self.settings, self._changed = client, settings, changed
        self._login_requested = login_requested
        self.client.loginSucceeded.connect(self._authentication_succeeded)
        self.setObjectName("codeReplacementsEditor")
        self.sets: list[dict] = []
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.set_combo = QComboBox()
        self.set_combo.currentIndexChanged.connect(self._set_changed)
        toolbar.addWidget(self.set_combo, 1)
        for text, handler, tip in (
            (_("+ Набор"), self._create_set, _("Создать набор")),
            (_("Переименовать"), self._rename_set, _("Переименовать набор")),
            (_("Удалить"), self._delete_set, _("Удалить набор")),
            (_("Импорт…"), self._import_codes, _("Импорт CSV, TSV или XLSX")),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([_("Код"), _("Значение"), ""])
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(2, 44)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(1, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.itemChanged.connect(self._update_code)
        layout.addWidget(self.table, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.login_hint = QLabel(_("Для синхронизации кодов замен, авторизуйтесь на shotsync."))
        self.login_hint.setObjectName("shotsyncHint")
        self.login_hint.setWordWrap(True)
        self.login_button = QPushButton(_("Войти"))
        self.login_button.setObjectName("settingsPrimaryButton")
        self.login_button.setFixedSize(66, 28)
        self.login_button.setToolTip(_("Войти в ShotSync"))
        self.login_button.clicked.connect(self._login)
        login_row = QHBoxLayout()
        login_row.setSpacing(8)
        login_row.addWidget(self.login_hint)
        login_row.addWidget(self.login_button)
        login_row.addStretch(1)
        layout.addLayout(login_row)
        self._load()

    def _active_set(self) -> dict | None:
        index = self.set_combo.currentIndex()
        return self.sets[index] if 0 <= index < len(self.sets) else None

    def _load(self, *, select_id: int | None = None, focus_new: bool = False) -> None:
        """Загружает наборы с сервера или из настроек и обновляет выбор."""
        if not self.client.has_key():
            self.sets = self._local_sets()
            self._ensure_default_local_set()
            self._apply_loaded_sets(select_id=select_id, focus_new=focus_new)
            self.status.clear()
            self.login_hint.show()
            self.login_button.show()
            return
        self.status.setText(_("Синхронизация с ShotSync…"))
        def done(ok: bool, data: dict, error: str) -> None:
            if not ok:
                self.status.setText(error)
                return
            self.sets = [item for item in data.get("sets", []) if isinstance(item, dict)]
            if not self.sets:
                self.client.request_json(
                    "/api/users/code-replacements/",
                    lambda ok, data, error: self._load(select_id=data.get("set", {}).get("id")) if ok else self.status.setText(error),
                    method="POST",
                    payload={"name": _("По умолчанию")},
                )
                return
            self._apply_loaded_sets(select_id=select_id, focus_new=focus_new)
            self.status.setText(_("Синхронизировано"))
            self.login_hint.hide()
            self.login_button.hide()
        self.client.request_json("/api/users/code-replacements/", done)

    def _apply_loaded_sets(self, *, select_id: int | None, focus_new: bool) -> None:
        wanted = select_id if select_id is not None else self.settings.value("code_replacements/active_set_id", 0, int)
        self.set_combo.blockSignals(True)
        self.set_combo.clear()
        chosen = 0
        for index, item in enumerate(self.sets):
            self.set_combo.addItem(str(item.get("name") or _("Без названия")))
            if item.get("id") == wanted:
                chosen = index
        self.set_combo.setCurrentIndex(chosen if self.sets else -1)
        self.set_combo.blockSignals(False)
        self._set_changed()
        if focus_new:
            QTimer.singleShot(0, self._focus_new_code)

    def _local_sets(self) -> list[dict]:
        return [item for item in self.settings.value("code_replacements/local_sets", [], list) if isinstance(item, dict)]

    def _save_local_sets(self) -> None:
        self.settings.setValue("code_replacements/local_sets", self.sets)
        self.settings.sync()
        self._changed(self.sets)

    def _ensure_default_local_set(self) -> None:
        """Создаёт первый локальный набор, чтобы редактор не встречал пустотой."""
        if self.sets:
            return
        self.sets = [{"id": -1, "name": _("По умолчанию"), "codes": []}]
        self._save_local_sets()

    def _login(self) -> None:
        self._login_requested()

    def _authentication_succeeded(self, _user: dict, _key: str) -> None:
        """Обновляет открытый редактор сразу после успешного общего входа."""
        if self.isVisible():
            self._load()

    def _set_changed(self) -> None:
        active = self._active_set()
        if active:
            self.settings.setValue("code_replacements/active_set_id", int(active["id"]))
        self._render_table()
        self._changed(self.sets)

    def _render_table(self) -> None:
        """Перестраивает строки кодов активного набора и подключает редакторы."""
        active = self._active_set()
        codes = active.get("codes", []) if active else []
        self.table.blockSignals(True)
        self.table.setRowCount(len(codes) + (1 if active else 0))
        for row, entry in enumerate(codes):
            self.table.setItem(row, 0, QTableWidgetItem(str(entry.get("code") or "")))
            self.table.setItem(row, 1, QTableWidgetItem(str(entry.get("value") or "")))
            remove = QToolButton()
            remove.setIcon(_fomantic_icon("trash", 12))
            remove.setToolTip(_("Удалить код"))
            remove.clicked.connect(lambda _checked=False, code_id=entry.get("id"): self._delete_code(code_id))
            self.table.setCellWidget(row, 2, remove)
        if active:
            row = len(codes)
            code = QLineEdit()
            code.setPlaceholderText(_("Код"))
            code.setMaxLength(80)
            value = QLineEdit()
            value.setPlaceholderText(_("Значение"))
            code.returnPressed.connect(lambda: value.setFocus())
            code.installEventFilter(self)
            value.installEventFilter(self)
            self.table.setCellWidget(row, 0, code)
            self.table.setCellWidget(row, 1, value)
        self.table.blockSignals(False)

    def _focus_new_code(self) -> None:
        field = self.table.cellWidget(self.table.rowCount() - 1, 0)
        if isinstance(field, QLineEdit):
            field.setFocus()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """Повторяет переход Tab из веб-редактора в последней строке таблицы."""
        if event.type() != QEvent.Type.KeyPress or watched not in (self.table.cellWidget(self.table.rowCount() - 1, 0), self.table.cellWidget(self.table.rowCount() - 1, 1)):
            return super().eventFilter(watched, event)
        code = self.table.cellWidget(self.table.rowCount() - 1, 0)
        value = self.table.cellWidget(self.table.rowCount() - 1, 1)
        if not isinstance(code, QLineEdit) or not isinstance(value, QLineEdit):
            return super().eventFilter(watched, event)
        if watched is code and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Return, Qt.Key.Key_Enter) and code.text().strip():
            value.setFocus(); event.accept(); return True
        if watched is value and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Return, Qt.Key.Key_Enter) and code.text().strip() and value.text().strip():
            self._add_code(code.text(), value.text()); event.accept(); return True
        return super().eventFilter(watched, event)

    def commit_pending_code(self) -> bool:
        if self.table.rowCount() == 0:
            return True
        row = self.table.rowCount() - 1
        code = self.table.cellWidget(row, 0)
        value = self.table.cellWidget(row, 1)
        if not isinstance(code, QLineEdit) or not isinstance(value, QLineEdit):
            return True
        code_text = code.text().strip()
        value_text = value.text().strip()
        if not code_text and not value_text:
            return True
        if not code_text or not value_text:
            self.status.setText(_("Заполните код и значение."))
            return False
        self._add_code(code_text, value_text)
        return True

    def _create_set(self) -> None:
        name, ok = QInputDialog.getText(self, _("Новый набор"), _("Название:"))
        if not ok or not name.strip(): return
        if not self.client.has_key():
            local_ids = [int(item.get("id") or 0) for item in self.sets]
            new_set = {"id": min([0, *local_ids]) - 1, "name": name.strip(), "codes": []}
            self.sets.append(new_set)
            self._save_local_sets()
            self._apply_loaded_sets(select_id=new_set["id"], focus_new=True)
            return
        self.client.request_json("/api/users/code-replacements/", lambda ok, data, error: self._load(select_id=data.get("set", {}).get("id") if ok else None), method="POST", payload={"name": name.strip()})

    def _rename_set(self) -> None:
        active = self._active_set()
        if not active: return
        name, ok = QInputDialog.getText(self, _("Переименовать набор"), _("Название:"), text=str(active.get("name") or ""))
        if not ok or not name.strip(): return
        if not self.client.has_key():
            active["name"] = name.strip()
            self._save_local_sets()
            self._render_table()
            return
        self.client.request_json(f"/api/users/code-replacements/{active['id']}/", lambda ok, _data, error: self._load(select_id=active["id"]) if ok else self.status.setText(error), method="POST", payload={"name": name.strip()})

    def _delete_set(self) -> None:
        active = self._active_set()
        if not active:
            return
        confirm = QMessageBox(QMessageBox.Icon.Warning, _("Удалить набор"), _("Удалить набор и все его коды?"), parent=self)
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        confirm.button(QMessageBox.StandardButton.Yes).setText(_("Удалить"))
        confirm.button(QMessageBox.StandardButton.Cancel).setText(_("Отмена"))
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return
        if not self.client.has_key():
            if len(self.sets) == 1:
                self.status.setText(_("Нужен хотя бы один набор кодов."))
                return
            self.sets.remove(active)
            self._save_local_sets()
            self._apply_loaded_sets(select_id=None, focus_new=False)
            return
        self.client.request_json(f"/api/users/code-replacements/{active['id']}/delete/", lambda ok, _data, error: self._load() if ok else self.status.setText(error), method="POST")

    def _add_code(self, code: str, value: str) -> None:
        active = self._active_set(); code, value = code.strip(), value.strip()
        if not active or not code or not value: return
        if not self.client.has_key():
            active.setdefault("codes", []).append({"id": uuid4().hex, "code": code, "value": value})
            self._save_local_sets()
            self._render_table()
            self._focus_new_code()
            return
        def done(ok: bool, _data: dict, error: str) -> None:
            if ok:
                self._load(select_id=active["id"], focus_new=True)
            else: self.status.setText(error)
        self.client.request_json(f"/api/users/code-replacements/{active['id']}/codes/", done, method="POST", payload={"code": code, "value": value})

    def _delete_code(self, code_id: object) -> None:
        active = self._active_set()
        if not active or not code_id: return
        if not self.client.has_key():
            active["codes"] = [entry for entry in active.get("codes", []) if entry.get("id") != code_id]
            self._save_local_sets()
            self._render_table()
            return
        self.client.request_json(f"/api/users/code-replacements/{active['id']}/codes/{code_id}/delete/", lambda ok, _data, error: self._load(select_id=active["id"]) if ok else self.status.setText(error), method="POST")

    def _update_code(self, item: QTableWidgetItem) -> None:
        active = self._active_set()
        if not active or item.column() not in (0, 1): return
        codes = active.get("codes", [])
        if item.row() >= len(codes): return
        entry = codes[item.row()]
        code = self.table.item(item.row(), 0).text().strip()
        value = self.table.item(item.row(), 1).text().strip()
        if not code or not value or (code == entry.get("code") and value == entry.get("value")): return
        if not self.client.has_key():
            entry["code"], entry["value"] = code, value
            self._save_local_sets()
            return
        self.client.request_json(
            f"/api/users/code-replacements/{active['id']}/codes/{entry['id']}/",
            lambda ok, _data, error: self._load(select_id=active["id"]) if ok else self.status.setText(error),
            method="POST", payload={"code": code, "value": value},
        )

    def _import_codes(self) -> None:
        active = self._active_set()
        if not active: return
        path, _filter = QFileDialog.getOpenFileName(self, _("Импорт кодов"), "", _("Таблицы (*.csv *.tsv *.xlsx)"))
        if not path: return
        self.status.setText(_("Импорт…"))
        def done(ok: bool, data: dict, error: str) -> None:
            if ok:
                self.status.setText(_("Импортировано: {imported}, пропущено: {skipped}").format(imported=data.get('imported', 0), skipped=data.get('skipped', 0)))
                self._load(select_id=active["id"])
            else: self.status.setText(error)
        self.client.upload_file(f"/api/users/code-replacements/{active['id']}/import/", path, done)


class CodeCompletingLineEdit(QLineEdit):
    """Однострочный комментарий с дополнением кодов вида ``{code}``.

    Внутри хранится исходная строка, совместимая с ShotSync, а поверх неё
    рисуются понятные плашки со значениями. Стандартный ``QLineEdit`` остаётся
    моделью ввода, поэтому клавиатура, выделение и буфер обмена работают как
    обычно; подсказки лишь помогают вставить код и ничего не подменяют тайком.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._codes: list[dict] = []
        self._lookup: dict[str, str] = {}
        self._raw = ""
        self._showing_preview = False
        self._opener = "{"
        self._start = 0
        self._labels: dict[str, str] = {}
        self._model = QStringListModel(self)
        self._completer = QCompleter(self._model, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.setCompleter(self._completer)
        self.textEdited.connect(self._remember_raw)
        self.textEdited.connect(self._offer_codes)
        self._completer.activated.connect(self._insert_code)
        self._suggestion_popup = QListWidget(self)
        self._suggestion_popup.setWindowFlags(Qt.WindowType.ToolTip)
        self._suggestion_popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._suggestion_popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._suggestion_popup.setObjectName("codeSuggestionPopup")
        self._suggestion_popup.itemClicked.connect(lambda item: self._insert_code(item.text()))
        self._suggestion_popup.hide()

    def set_codes(self, sets: list[dict], active_id: int) -> None:
        self._codes = [entry for group in sets if not active_id or group.get("id") == active_id for entry in group.get("codes", []) if isinstance(entry, dict)]
        self._lookup = {str(entry.get("code") or ""): str(entry.get("value") or "") for entry in self._codes}
        self.setToolTip(self._raw)
        self.update()

    def text(self) -> str:  # noqa: N802 — имя совпадает с API QLineEdit
        return self._raw

    def setText(self, text: str) -> None:  # noqa: N802 — имя совпадает с API QLineEdit
        self._raw = str(text or "")
        self._showing_preview = False
        super().setText(self._raw)
        self.setToolTip(self._raw)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._raw = super().text()
        super().focusOutEvent(event)
        self.update()

    def _remember_raw(self, value: str) -> None:
        self._raw = value
        self.setToolTip(value)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        """Рисует плашки замен поверх исходного текста ``QLineEdit``.

        Само поле остаётся моделью редактирования и сохраняет обычное поведение
        клавиатуры, буфера обмена и автодополнения. Исходный маркер лишь скрыт
        под плашкой; его содержимое при этом не меняется.
        """
        super().paintEvent(event)
        if not self._raw:
            return
        painter = QPainter(self)
        content = self.contentsRect().adjusted(10, 1, -10, -1)
        painter.fillRect(content, self.palette().base())
        painter.setFont(self.font())
        metrics = painter.fontMetrics()
        x, baseline = content.left(), content.top() + (content.height() + metrics.ascent() - metrics.descent()) // 2
        last = 0
        raw_cursor = self.cursorPosition()
        cursor_x: int | None = None
        token_re = re.compile(r"\{([^}]+)\}|\\([^\\]+)\\|=([^=]+)=|@([\w]+)|#([\w]+)")
        for match in token_re.finditer(self._raw):
            plain = self._raw[last:match.start()]
            painter.setPen(self.palette().text().color())
            painter.drawText(x, baseline, plain)
            if cursor_x is None and last <= raw_cursor <= match.start():
                cursor_x = x + metrics.horizontalAdvance(self._raw[last:raw_cursor])
            x += metrics.horizontalAdvance(plain)
            tag = match.group(5) is not None
            code = next(value for value in match.groups() if value is not None)
            value = match.group(0) if tag else self._lookup.get(code)
            if value is None:
                painter.drawText(x, baseline, match.group(0))
                x += metrics.horizontalAdvance(match.group(0))
            else:
                width = metrics.horizontalAdvance(value) + 10
                chip = QRect(x, content.top() + 2, width, max(18, content.height() - 4))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#765b9a") if tag else QColor("#3867a8"))
                painter.drawRoundedRect(chip, 4, 4)
                painter.setPen(QColor("#f7fbff"))
                painter.drawText(chip.adjusted(5, 0, -5, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, value)
                x += width + 2
            if cursor_x is None and match.start() < raw_cursor <= match.end():
                cursor_x = x
            last = match.end()
        tail = self._raw[last:]
        painter.setPen(self.palette().text().color())
        painter.drawText(x, baseline, tail)
        if cursor_x is None:
            cursor_x = x + metrics.horizontalAdvance(self._raw[last:raw_cursor])
        if self.hasFocus() and self.cursorPosition() >= 0:
            painter.setPen(QPen(QColor("#f4f7fb"), 1))
            painter.drawLine(cursor_x, content.top() + 4, cursor_x, content.bottom() - 4)
        painter.end()

    def _offer_codes(self, _text: str) -> None:
        before = self.text()[:self.cursorPosition()]
        candidates = [(before.rfind(mark), mark) for mark in ("{", "\\", "=", "@")]
        start, opener = max(candidates)
        if start < 0: self._suggestion_popup.hide(); return
        fragment = before[start + 1:]
        if "\n" in fragment or (opener == "@" and not fragment.replace("_", "a").isalnum()):
            self._suggestion_popup.hide(); return
        if opener != "@" and ("}" if opener == "{" else opener) in fragment:
            self._completer.popup().hide(); return
        self._start, self._opener = start, opener
        self._labels = {f"{entry.get('code', '')} — {entry.get('value', '')}": str(entry.get("code") or "") for entry in self._codes}
        labels = [label for label in self._labels if fragment.casefold() in label.casefold()]
        self._model.setStringList(labels)
        self._completer.setCompletionPrefix(fragment)
        if labels:
            self._completer.complete(self.cursorRect())

    def _insert_code(self, label: str) -> None:
        code = self._labels.get(label)
        if not code: return
        closer = "}" if self._opener == "{" else ("" if self._opener == "@" else self._opener)
        end = self.cursorPosition()
        insertion = f"{self._opener}{code}{closer}"
        self.setText(self.text()[:self._start] + insertion + self.text()[end:])
        self.setCursorPosition(self._start + len(insertion))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """При редактировании удаляет видимую плашку замены только целиком."""
        key = event.key()
        if key not in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            super().keyPressEvent(event)
            return
        raw = self.text()
        start = self.selectionStart()
        end = self.cursorPosition()
        if start >= 0:
            end = start + len(self.selectedText())
        else:
            start = end
        if start == end:
            for match in re.finditer(r"\{[^}]+\}|\\[^\\]+\\|=[^=]+=|@[\w]+|#[\w]+", raw):
                if key == Qt.Key.Key_Backspace and match.start() < start <= match.end():
                    start, end = match.start(), match.end()
                    break
                if key == Qt.Key.Key_Delete and match.start() <= start < match.end():
                    start, end = match.start(), match.end()
                    break
            else:
                super().keyPressEvent(event)
                return
        else:
            for match in re.finditer(r"\{[^}]+\}|\\[^\\]+\\|=[^=]+=|@[\w]+|#[\w]+", raw):
                if match.start() < end and match.end() > start:
                    start, end = min(start, match.start()), max(end, match.end())
        updated = raw[:start] + raw[end:]
        self.setText(updated)
        self.setCursorPosition(start)
        self.textEdited.emit(updated)
