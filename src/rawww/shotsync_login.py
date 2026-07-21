## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Диалог входа в ShotSync."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from .i18n import gettext as _


def humanize_login_error(raw: str) -> str:
    """Превращает техническую ошибку входа в короткое понятное сообщение."""
    if not raw:
        return _("Не удалось войти. Попробуйте ещё раз.")
    low = raw.lower()
    if any(key in low for key in ("invalid", "incorrect", "wrong", "password", "not found", "not exist", "no active")):
        return _("Неверный логин или пароль.")
    if any(key in low for key in ("connection", "timeout", "host", "network", "refused", "unreachable")):
        return _("Ошибка сети. Проверьте подключение к интернету.")
    if any(key in low for key in ("ssl", "tls", "certificate")):
        return _("Ошибка безопасного соединения (SSL).")
    if any(key in low for key in ("server", "500", "503", "unavailable")):
        return _("Сервер временно недоступен. Попробуйте позже.")
    return raw.rstrip(".") + "."


class ShotSyncLoginDialog(QDialog):
    """Общая переиспользуемая форма входа в ShotSync.

    Диалог пользуется переданным ``ShotSyncClient``, блокирует повторную отправку
    и показывает уже приведённые к человеческому виду ошибки. После закрытия
    ``reset`` готовит форму к новому сеансу, чтобы разные вкладки не плодили
    одинаковые окна авторизации.
    """

    loginSubmitted = Signal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("shotsyncLoginDialog")
        self.setWindowTitle(_("Вход в ShotSync"))
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 26)
        layout.setSpacing(14)
        title = QLabel(_("Вход в ShotSync"))
        title.setObjectName("shotsyncTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addSpacing(6)

        self.login_edit = QLineEdit()
        self.login_edit.setObjectName("shotsyncField")
        self.login_edit.setPlaceholderText(_("Логин или email"))
        self.login_edit.returnPressed.connect(self._submit)
        layout.addWidget(self.login_edit)
        self.password_edit = QLineEdit()
        self.password_edit.setObjectName("shotsyncField")
        self.password_edit.setPlaceholderText(_("Пароль"))
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.returnPressed.connect(self._submit)
        layout.addWidget(self.password_edit)
        self.error = QLabel()
        self.error.setObjectName("shotsyncError")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        cancel = QPushButton(_("Отмена"))
        cancel.setObjectName("settingsSecondaryButton")
        cancel.setFixedSize(120, 36)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.submit_button = QPushButton(_("Войти"))
        self.submit_button.setObjectName("settingsPrimaryButton")
        self.submit_button.setFixedSize(120, 36)
        self.submit_button.clicked.connect(self._submit)
        buttons.addWidget(self.submit_button)
        layout.addLayout(buttons)

    def _submit(self) -> None:
        login, password = self.login_edit.text().strip(), self.password_edit.text()
        if not login or not password:
            self.show_error(_("Введите логин и пароль."))
            return
        self.set_submitting(True)
        self.loginSubmitted.emit(login, password)

    def set_submitting(self, submitting: bool) -> None:
        self.submit_button.setEnabled(not submitting)
        self.submit_button.setText(_("Входим…") if submitting else _("Войти"))
        self.login_edit.setEnabled(not submitting)
        self.password_edit.setEnabled(not submitting)

    def show_error(self, error: str) -> None:
        self.set_submitting(False)
        self.error.setText(humanize_login_error(error))
        self.error.show()

    def login_succeeded(self) -> None:
        self.accept()

    def reset(self) -> None:
        """Возвращает переиспользуемую форму в состояние нового сеанса."""
        self.set_submitting(False)
        self.login_edit.clear()
        self.password_edit.clear()
        self.error.hide()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.login_edit.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
