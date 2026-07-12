"""Sidebar panel for the ShotSync integration.

The panel lives in the left navigation column and swaps between three states:

* a short "checking" state while a stored key is validated,
* a login form (login + password) when the user is signed out,
* the signed-in view with the profile header and the list of shootings.

All networking happens in :mod:`rawww.shotsync_client`; this widget only emits
intent signals (``loginSubmitted``, ``logoutRequested``, ``refreshRequested``)
and renders whatever state the app hands back to it.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

IconProvider = Callable[..., QIcon]


def _rounded_avatar(image: QImage, size: int = 40) -> QPixmap:
    """Return a circular avatar pixmap for the profile header."""
    scaled = image.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0, 0, size, size)
    painter.setClipPath(path)
    x = (size - scaled.width()) // 2
    y = (size - scaled.height()) // 2
    painter.drawImage(x, y, scaled)
    painter.end()
    return pixmap


class ShotSyncPanel(QWidget):
    """ShotSync navigation panel shown in place of the folder tree."""

    loginSubmitted = Signal(str, str)   # login, password
    logoutRequested = Signal()
    refreshRequested = Signal()
    shootingActivated = Signal(dict)    # emitted on double-click (future use)
    receiveRequested = Signal(dict)     # toggle live "receive photos" for a shooting
    selectRequested = Signal(dict)      # download a shooting locally for selection

    def __init__(self, icon_provider: IconProvider | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icon_provider = icon_provider
        self._avatar_size = 40
        self._shootings: list[dict] = []
        self._receiving_ids: set[int] = set()
        self.setObjectName("shotsyncPanel")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        self.stack.addWidget(self._build_checking_page())
        self.stack.addWidget(self._build_login_page())
        self.stack.addWidget(self._build_logged_in_page())
        self.stack.setCurrentIndex(0)

    # ----- icon helper ---------------------------------------------------
    def _icon(self, name: str, size: int = 14, color: str = "#d6d6d6") -> QIcon:
        if self._icon_provider is None:
            return QIcon()
        try:
            return self._icon_provider(name, size, color)
        except TypeError:
            return self._icon_provider(name)

    # ----- page builders -------------------------------------------------
    def _build_checking_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 24, 16, 16)
        layout.addStretch(1)
        label = QLabel("Проверяем вход в ShotSync…")
        label.setObjectName("shotsyncHint")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch(2)
        return page

    def _build_login_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 22, 16, 16)
        layout.setSpacing(10)

        title = QLabel("Вход в ShotSync")
        title.setObjectName("shotsyncTitle")
        layout.addWidget(title)

        subtitle = QLabel("Войдите, чтобы открыть свои съёмки с shotsync.ru")
        subtitle.setObjectName("shotsyncHint")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.login_edit = QLineEdit()
        self.login_edit.setObjectName("shotsyncField")
        self.login_edit.setPlaceholderText("Логин или email")
        self.login_edit.returnPressed.connect(self._submit)
        layout.addWidget(self.login_edit)

        self.password_edit = QLineEdit()
        self.password_edit.setObjectName("shotsyncField")
        self.password_edit.setPlaceholderText("Пароль")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.returnPressed.connect(self._submit)
        layout.addWidget(self.password_edit)

        self.login_error = QLabel()
        self.login_error.setObjectName("shotsyncError")
        self.login_error.setWordWrap(True)
        self.login_error.hide()
        layout.addWidget(self.login_error)

        self.submit_button = QPushButton("Войти")
        self.submit_button.setObjectName("shotsyncPrimaryButton")
        self.submit_button.clicked.connect(self._submit)
        layout.addWidget(self.submit_button)

        layout.addStretch(1)
        return page

    def _build_logged_in_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header = QWidget()
        header.setObjectName("shotsyncProfile")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 6, 6, 6)
        header_layout.setSpacing(8)

        self.avatar_label = QLabel()
        self.avatar_label.setObjectName("shotsyncAvatar")
        self.avatar_label.setFixedSize(self._avatar_size, self._avatar_size)
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(self.avatar_label)

        self.profile_name = QLabel()
        self.profile_name.setObjectName("shotsyncProfileName")
        self.profile_name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.profile_name.setWordWrap(True)
        header_layout.addWidget(self.profile_name, 1)

        self.logout_button = QToolButton()
        self.logout_button.setObjectName("shotsyncLogoutButton")
        self.logout_button.setIcon(self._icon("sign-out", 15, "#8a8a8a"))
        self.logout_button.setToolTip("Выйти из ShotSync")
        self.logout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.logout_button.clicked.connect(self._confirm_logout)
        header_layout.addWidget(self.logout_button, 0, Qt.AlignmentFlag.AlignVCenter)

        layout.addWidget(header)

        section = QLabel("СЪЁМКИ")
        section.setObjectName("shotsyncSection")
        layout.addWidget(section)

        self.shooting_status = QLabel()
        self.shooting_status.setObjectName("shotsyncHint")
        self.shooting_status.setWordWrap(True)
        self.shooting_status.hide()
        layout.addWidget(self.shooting_status)

        self.shooting_list = QListWidget()
        self.shooting_list.setObjectName("shotsyncShootingList")
        self.shooting_list.setUniformItemSizes(False)
        self.shooting_list.itemDoubleClicked.connect(self._emit_activated)
        self.shooting_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.shooting_list.customContextMenuRequested.connect(self._show_shooting_menu)
        layout.addWidget(self.shooting_list, 1)

        return page

    # ----- interaction ---------------------------------------------------
    def _submit(self) -> None:
        if not self.submit_button.isEnabled():
            return
        login = self.login_edit.text().strip()
        password = self.password_edit.text()
        if not login or not password:
            self.show_login_error("Введите логин и пароль.")
            return
        self.set_submitting(True)
        self.loginSubmitted.emit(login, password)

    def _confirm_logout(self) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("Выход из ShotSync")
        msg.setText("Вы уверены, что хотите выйти?")
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        msg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        msg.button(QMessageBox.StandardButton.Yes).setText("Выйти")
        msg.button(QMessageBox.StandardButton.Cancel).setText("Отмена")
        if msg.exec() == QMessageBox.StandardButton.Yes:
            self.logoutRequested.emit()

    def _emit_activated(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict):
            self.shootingActivated.emit(data)

    # ----- state transitions --------------------------------------------
    def show_checking(self) -> None:
        self.stack.setCurrentIndex(0)

    def show_login(self, error: str = "") -> None:
        self.set_submitting(False)
        self.password_edit.clear()
        if error:
            self.show_login_error(error)
        else:
            self.login_error.hide()
        self.stack.setCurrentIndex(1)
        self.login_edit.setFocus()

    def show_login_error(self, message: str) -> None:
        self.set_submitting(False)
        self.login_error.setText(_humanize_login_error(message))
        self.login_error.show()

    def set_submitting(self, submitting: bool) -> None:
        self.submit_button.setEnabled(not submitting)
        self.submit_button.setText("Входим…" if submitting else "Войти")
        self.login_edit.setEnabled(not submitting)
        self.password_edit.setEnabled(not submitting)

    def show_logged_in(self, user: dict) -> None:
        name = user.get("display_name") or user.get("name") or user.get("login") or "Профиль"
        self.profile_name.setText(name)
        self._set_placeholder_avatar()
        self.stack.setCurrentIndex(2)

    def _set_placeholder_avatar(self) -> None:
        icon = self._icon("user", 20, "#8fa3bd")
        if icon.isNull():
            self.avatar_label.clear()
        else:
            self.avatar_label.setPixmap(icon.pixmap(self._avatar_size, self._avatar_size))

    def set_avatar(self, image: QImage) -> None:
        if image.isNull():
            return
        self.avatar_label.setPixmap(_rounded_avatar(image, self._avatar_size))

    def set_shootings_loading(self) -> None:
        self.shooting_status.setText("Загружаем съёмки…")
        self.shooting_status.show()

    def set_shootings_error(self, message: str) -> None:
        self.shooting_status.setText(message or "Не удалось загрузить съёмки.")
        self.shooting_status.show()

    def set_shootings(self, shootings: list) -> None:
        self._shootings = [s for s in shootings if isinstance(s, dict)]
        self._render_shootings()

    def set_receiving_ids(self, ids) -> None:
        """Update which shootings are being received and repaint the list."""
        self._receiving_ids = {int(i) for i in ids}
        self._render_shootings()

    def _render_shootings(self) -> None:
        self.shooting_list.clear()
        if not self._shootings:
            self.shooting_status.setText("Пока нет ни одной съёмки.")
            self.shooting_status.show()
            return
        self.shooting_status.hide()
        for shooting in self._shootings:
            title = shooting.get("title") or "Без названия"
            photo_count = shooting.get("photo_count") or 0
            status = _status_label(shooting.get("status"))
            receiving = int(shooting.get("id") or 0) in self._receiving_ids
            parts = [status, f"{photo_count} фото"]
            if receiving:
                parts.append("● приём")
            details = " · ".join(part for part in parts if part)
            item = QListWidgetItem(f"{title}\n{details}")
            item.setData(Qt.ItemDataRole.UserRole, shooting)
            item.setToolTip("Приём включён" if receiving else title)
            self.shooting_list.addItem(item)

    def _show_shooting_menu(self, pos) -> None:
        item = self.shooting_list.itemAt(pos)
        if item is None:
            return
        shooting = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(shooting, dict):
            return
        receiving = int(shooting.get("id") or 0) in self._receiving_ids
        menu = QMenu(self)
        receive_label = "Остановить приём фото" if receiving else "Получать новые фото…"
        receive_action = menu.addAction(receive_label)
        select_action = menu.addAction("Взять на отбор…")
        chosen = menu.exec(self.shooting_list.mapToGlobal(pos))
        if chosen is receive_action:
            self.receiveRequested.emit(shooting)
        elif chosen is select_action:
            self.selectRequested.emit(shooting)


def _humanize_login_error(raw: str) -> str:
    """Convert a raw server/network error into a human-readable Russian message."""
    if not raw:
        return "Не удалось войти. Попробуйте ещё раз."
    low = raw.lower()
    # Server-side auth errors
    if any(k in low for k in ("invalid", "incorrect", "wrong", "неверн", "not found", "not exist",
                               "no active", "does not exist")):
        return "Неверный логин или пароль."
    if any(k in low for k in ("password", "пароль")):
        return "Неверный логин или пароль."
    if any(k in low for k in ("login", "логин", "email", "user")):
        return "Пользователь с таким логином не найден."
    # Network / connection errors
    if any(k in low for k in ("connection", "timeout", "host", "network", "refused",
                               "unreachable", "соединен", "подключен", "сеть", "недоступ")):
        return "Ошибка сети. Проверьте подключение к интернету."
    if any(k in low for k in ("ssl", "tls", "certificate")):
        return "Ошибка безопасного соединения (SSL)."
    if any(k in low for k in ("server", "500", "503", "unavailable")):
        return "Сервер временно недоступен. Попробуйте позже."
    # Fall back to the raw message but trim any trailing punctuation excess
    return raw.rstrip(".") + "."


def _status_label(status: str | None) -> str:
    return {
        "active": "Активна",
        "scheduled": "Запланирована",
        "finished": "Завершена",
        "archived": "В архиве",
    }.get(status or "", "")
