## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Боковая панель ShotSync в интерфейсе приложения."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QSize, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QPainter, QPainterPath, QPixmap, QTransform
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .i18n import gettext as _

IconProvider = Callable[..., QIcon]
SHOTSYNC_BASE_URL = "https://shotsync.ru"


def _rounded_avatar(image: QImage, size: int = 40) -> QPixmap:
    """Обрезает аватар по кругу для заголовка профиля."""
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
    """Показывает авторизацию, профиль и список съёмок ShotSync в боковой панели.

    Виджет переключается между проверкой сессии, формой входа и содержимым
    профиля. Для каждой съёмки он рисует состояние локальной копии, приёма и
    доступные действия. Сам панель ничего не загружает и не удаляет: она испускает
    сигналы с данными карточки, а сетевую и файловую работу выполняет ``Workspace``.

    Списки ID передаются отдельно, потому что серверное состояние, локальная
    папка и текущая операция меняются независимо. После каждого изменения
    карточки пересобираются целиком — элементов немного, зато логика не хранит
    полдюжины полуживых виджетов на одну съёмку.
    """

    loginRequested = Signal()
    logoutRequested = Signal()
    refreshRequested = Signal()
    shootingActivated = Signal(dict)  # пользователь открыл карточку съёмки
    receiveRequested = Signal(dict)  # включить или выключить приём оригиналов
    selectRequested = Signal(dict)  # скачать съёмку для локального отбора
    removeLocalRequested = Signal(dict)  # удалить только локальную копию
    deleteServerRequested = Signal(dict)  # удалить только съёмку на сервере
    getMarksForRequested = Signal(dict)  # получить метки в локальную папку
    sendFolderRequested = Signal()       # загрузите открытую папку как новую съемку
    getMarksRequested = Signal()         # метки для открытой папки ShotSync

    def __init__(self, icon_provider: IconProvider | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icon_provider = icon_provider
        self._avatar_size = 40
        self._shootings: list[dict] = []
        self._receiving_ids: set[int] = set()
        self._local_ids: set[int] = set()
        self._offline_ids: set[int] = set()
        self._shooting_modes: dict[int, str] = {}
        self._current_shooting_id: int | None = None
        self._refresh_angle = 0
        self._refresh_base_icon = QIcon()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(45)
        self._refresh_timer.timeout.connect(self._rotate_refresh_icon)
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

    def _icon(self, name: str, size: int = 14, color: str = "#d6d6d6") -> QIcon:
        if self._icon_provider is None:
            return QIcon()
        try:
            return self._icon_provider(name, size, color)
        except TypeError:
            return self._icon_provider(name)

    def _build_checking_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 24, 16, 16)
        layout.addStretch(1)
        label = QLabel(_("Проверяем вход в ShotSync…"))
        label.setObjectName("shotsyncHint")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch(2)
        return page

    def _build_login_page(self) -> QWidget:
        """Собирает форму входа и область сообщения об ошибке."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 22, 16, 16)
        layout.setSpacing(10)

        title = QLabel(_("Вход в ShotSync"))
        title.setObjectName("shotsyncTitle")
        layout.addWidget(title)

        subtitle = QLabel(_("Войдите, чтобы открыть свои съёмки с shotsync.ru"))
        subtitle.setObjectName("shotsyncHint")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.login_error = QLabel()
        self.login_error.setObjectName("shotsyncError")
        self.login_error.setWordWrap(True)
        self.login_error.hide()
        layout.addWidget(self.login_error)

        self.submit_button = QPushButton(_("Войти в ShotSync"))
        self.submit_button.setObjectName("shotsyncPrimaryButton")
        self.submit_button.clicked.connect(self.loginRequested)
        layout.addWidget(self.submit_button)

        layout.addStretch(1)
        return page

    def _build_logged_in_page(self) -> QWidget:
        """Собирает профиль, действия папки и прокручиваемый список съёмок."""
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
        logout_icon = self._icon("sign-out", 15, "#d0d0d0")
        self.logout_button.setIcon(logout_icon)
        self.logout_button.setIconSize(QSize(18, 18))
        self.logout_button.setFixedSize(32, 32)
        if logout_icon.isNull():
            self.logout_button.setText("⇥")
            self.logout_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        else:
            self.logout_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.logout_button.setToolTip(_("Выйти из ShotSync"))
        self.logout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.logout_button.clicked.connect(self._confirm_logout)
        header_layout.addWidget(self.logout_button, 0, Qt.AlignmentFlag.AlignVCenter)

        layout.addWidget(header)

        self.send_folder_button = QPushButton(_("Отправить на ShotSync"))
        self.send_folder_button.setObjectName("shotsyncSendButton")
        self.send_folder_button.setIcon(self._icon("plus", 17, "#e0e0e0"))
        self.send_folder_button.setIconSize(QSize(17, 17))
        self.send_folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_folder_button.clicked.connect(self.sendFolderRequested)
        layout.addWidget(self.send_folder_button)

        section_row = QHBoxLayout()
        section_row.setContentsMargins(2, 2, 2, 0)
        section_row.setSpacing(5)
        section = QLabel(_("СЪЁМКИ НА СЕРВЕРЕ"))
        section.setObjectName("shotsyncSection")
        section_row.addWidget(section)
        self.refresh_shootings_button = QToolButton()
        self.refresh_shootings_button.setObjectName("shotsyncRefreshButton")
        self.refresh_shootings_button.setIcon(self._icon("sync", 11, "#a8a8a8"))
        self._refresh_base_icon = self.refresh_shootings_button.icon()
        self.refresh_shootings_button.setIconSize(QSize(11, 11))
        self.refresh_shootings_button.setFixedSize(18, 18)
        self.refresh_shootings_button.setToolTip(_("Обновить список съёмок"))
        self.refresh_shootings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_shootings_button.clicked.connect(self.refreshRequested)
        section_row.addWidget(self.refresh_shootings_button)
        section_row.addStretch(1)
        layout.addLayout(section_row)

        self.shooting_status = QLabel()
        self.shooting_status.setObjectName("shotsyncHint")
        self.shooting_status.setWordWrap(True)
        self.shooting_status.hide()
        layout.addWidget(self.shooting_status)

        self.shooting_list = QListWidget()
        self.shooting_list.setObjectName("shotsyncShootingList")
        self.shooting_list.setUniformItemSizes(False)
        self.shooting_list.itemClicked.connect(self._emit_activated)
        layout.addWidget(self.shooting_list, 1)

        return page

    def set_folder_actions(self, *, can_send: bool, is_session: bool) -> None:
        """Обновляет доступность действий с учётом открытой папки."""
        if hasattr(self, "send_folder_button"):
            self.send_folder_button.setEnabled(can_send)

    def _confirm_logout(self) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle(_("Выход из ShotSync"))
        msg.setText(_("Вы уверены, что хотите выйти?"))
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        msg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        msg.button(QMessageBox.StandardButton.Yes).setText(_("Выйти"))
        msg.button(QMessageBox.StandardButton.Cancel).setText(_("Отмена"))
        if msg.exec() == QMessageBox.StandardButton.Yes:
            self.logoutRequested.emit()

    def _emit_activated(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict):
            self.shootingActivated.emit(data)

    def show_checking(self) -> None:
        self.stack.setCurrentIndex(0)

    def show_login(self, error: str = "") -> None:
        self.set_submitting(False)
        if error:
            self.show_login_error(error)
        else:
            self.login_error.hide()
        self.stack.setCurrentIndex(1)

    def show_login_error(self, message: str) -> None:
        self.set_submitting(False)
        self.login_error.setText(_humanize_login_error(message))
        self.login_error.show()

    def set_submitting(self, submitting: bool) -> None:
        self.submit_button.setEnabled(not submitting)
        self.submit_button.setText(_("Входим…") if submitting else _("Войти в ShotSync"))

    def show_logged_in(self, user: dict) -> None:
        name = user.get("display_name") or user.get("name") or user.get("login") or _("Профиль")
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
        self.set_refreshing(True)

    def set_refreshing(self, refreshing: bool) -> None:
        if not hasattr(self, "refresh_shootings_button"):
            return
        self.refresh_shootings_button.setEnabled(not refreshing)
        if refreshing:
            self._refresh_timer.start()
        else:
            self._refresh_timer.stop()
            self.refresh_shootings_button.setIcon(self._refresh_base_icon)

    def _rotate_refresh_icon(self) -> None:
        if self._refresh_base_icon.isNull():
            return
        self._refresh_angle = (self._refresh_angle + 18) % 360
        pixmap = self._refresh_base_icon.pixmap(22, 22).transformed(
            QTransform().rotate(self._refresh_angle), Qt.TransformationMode.SmoothTransformation
        )
        self.refresh_shootings_button.setIcon(QIcon(pixmap))

    def set_shootings_error(self, message: str) -> None:
        self.set_refreshing(False)
        self.shooting_status.setText(message or _("Не удалось загрузить съёмки."))
        self.shooting_status.show()

    def set_shootings(self, shootings: list) -> None:
        self.set_refreshing(False)
        self._shootings = [s for s in shootings if isinstance(s, dict)]
        self._render_shootings()

    def set_receiving_ids(self, ids) -> None:
        """Запоминает принимаемые съёмки и обновляет их карточки."""
        self._receiving_ids = {int(i) for i in ids}
        self._render_shootings()

    def set_local_ids(self, ids) -> None:
        """Отмечает съёмки, для которых уже существует локальная папка."""
        self._local_ids = {int(i) for i in ids}
        self._render_shootings()

    def set_offline_ids(self, ids) -> None:
        """Отмечает карточки, доступные сейчас только из локального кэша."""
        self._offline_ids = {int(i) for i in ids}
        self._render_shootings()

    def set_shooting_modes(self, modes: dict[int, str]) -> None:
        """Задаёт происхождение локальных папок: отправка или скачанный отбор."""
        self._shooting_modes = {int(shooting_id): str(mode) for shooting_id, mode in modes.items()}
        self._render_shootings()

    def set_current_shooting_id(self, shooting_id: int | None) -> None:
        self._current_shooting_id = int(shooting_id) if shooting_id else None
        self._render_shootings()

    def _render_shootings(self) -> None:
        """Полностью перестраивает список карточек по текущим наборам состояний."""
        self.shooting_list.clear()
        if not self._shootings:
            self.shooting_status.setText(_("Пока нет ни одной съёмки."))
            self.shooting_status.show()
            return
        self.shooting_status.hide()
        for shooting in self._shootings:
            title = shooting.get("title") or _("Без названия")
            photo_count = shooting.get("photo_count") or 0
            status = _status_label(shooting.get("status"))
            shooting_id = int(shooting.get("id") or 0)
            receiving = shooting_id in self._receiving_ids
            local = shooting_id in self._local_ids
            offline = shooting_id in self._offline_ids
            mode = self._shooting_modes.get(shooting_id, "")
            is_current = shooting_id == self._current_shooting_id
            parts = [status, _("{n} фото").format(n=photo_count)]
            if offline:
                parts.append(_("офлайн"))
            if receiving:
                parts.append(_("● приём: слежение включено"))
            elif mode == "uploaded":
                parts.append(_("отправлена на отбор"))
            elif mode == "selection_copy":
                parts.append(_("взята на отбор"))
            details = " · ".join(part for part in parts if part)
            item = QListWidgetItem(f"{title}\n{details}")
            item.setData(Qt.ItemDataRole.UserRole, shooting)
            item.setToolTip(_("Открыть папку") if (receiving or local) else title)
            item.setSizeHint(QSize(0, self._card_height(shooting, receiving, mode)))
            self.shooting_list.addItem(item)
            self.shooting_list.setItemWidget(item, self._shooting_card(shooting, receiving, local, offline, mode, is_current))

    @staticmethod
    def _card_height(shooting: dict, receiving: bool, mode: str) -> int:
        """Оценивает высоту карточки по заголовку, описанию и числу действий."""
        title = str(shooting.get("title") or _("Без названия"))
        title_lines = max(1, (len(title) + 27) // 28)
        description_length = {
            "uploaded": 78,
            "selection_copy": 64,
        }.get(mode, 62 if receiving else 30)
        detail_lines = max(1, (description_length + 37) // 38)
        action_count = 1 if (receiving or mode == "selection_copy") else 2
        return min(210, max(132, 44 + title_lines * 19 + detail_lines * 16 + action_count * 27))

    def _shooting_card(self, shooting: dict, receiving: bool, local: bool, offline: bool, mode: str, is_current: bool) -> QWidget:
        """Собирает карточку съёмки с действиями для её текущего состояния."""
        card = QWidget()
        card.setObjectName("shotsyncShootingCard")
        card.setProperty("currentShooting", is_current)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        title = QLabel(str(shooting.get("title") or _("Без названия")))
        title.setObjectName("shotsyncShootingTitle")
        title.setWordWrap(True)
        title_row.addWidget(title, 1)

        viewer_url = str(shooting.get("viewer_url") or "").strip()
        if viewer_url:
            if viewer_url.startswith("/"):
                viewer_url = f"{SHOTSYNC_BASE_URL}{viewer_url}"
            viewer_button = QToolButton()
            viewer_button.setObjectName("shotsyncViewerLink")
            viewer_icon = self._icon("link", 15, "#b9c5d6")
            viewer_button.setIcon(viewer_icon)
            viewer_button.setIconSize(QSize(15, 15))
            viewer_button.setFixedSize(24, 24)
            viewer_button.setToolTip(_("Открыть во вьювере ShotSync в браузере"))
            viewer_button.setCursor(Qt.CursorShape.PointingHandCursor)
            if viewer_icon.isNull():
                viewer_button.setText("🔗")
            viewer_button.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl(viewer_url))
            )
            title_row.addWidget(viewer_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(title_row)
        photo_count = shooting.get("photo_count") or 0
        state = _("Слежение: новые фото будут загружаться в выбранную папку.") if receiving else {
            "uploaded": _("Ваша папка отправлена на отбор. Метки можно получить с сервера."),
            "selection_copy": _("Локальная копия съёмки, взятая с сервера для отбора."),
        }.get(mode, _("Съёмка хранится на сервере."))
        if offline:
            state = _("Офлайн · работаем с локальной копией; изменения отправятся при подключении.")
        details = QLabel(_("{n} фото · {state}").format(n=photo_count, state=state))
        details.setObjectName("shotsyncHint")
        details.setWordWrap(True)
        layout.addWidget(details)
        actions = QVBoxLayout()
        actions.setSpacing(6)
        def action_button(label: str, icon: str) -> QPushButton:
            button = QPushButton(label)
            button.setIcon(self._icon(icon, 12, "#e0e0e0"))
            button.setIconSize(QSize(12, 12))
            return button

        if mode == "uploaded" and not receiving:
            marks_button = action_button(_("Получить метки"), "sync")
            marks_button.clicked.connect(lambda: self.getMarksForRequested.emit(shooting))
            actions.addWidget(marks_button)
        if mode == "uploaded" and not receiving:
            delete_button = action_button(_("Удалить с сервера"), "trash")
            delete_button.clicked.connect(lambda: self.deleteServerRequested.emit(shooting))
            actions.addWidget(delete_button)
        if mode == "selection_copy" and not receiving:
            remove_button = action_button(_("Удалить локально"), "trash")
            remove_button.clicked.connect(lambda: self.removeLocalRequested.emit(shooting))
            actions.addWidget(remove_button)
        if not mode and not receiving:
            select_button = action_button(_("Взять на отбор"), "download")
            select_button.clicked.connect(lambda: self.selectRequested.emit(shooting))
            actions.addWidget(select_button)
        if receiving:
            watch_button = action_button(_("Остановить отслеживание"), "stop")
            watch_button.clicked.connect(lambda: self.receiveRequested.emit(shooting))
            actions.addWidget(watch_button)
        elif not mode:
            watch_button = action_button(_("Получать оригиналы"), "eye")
            watch_button.clicked.connect(lambda: self.receiveRequested.emit(shooting))
            actions.addWidget(watch_button)
        layout.addLayout(actions)
        return card

def _humanize_login_error(raw: str) -> str:
    """Переводит ответ сервера или сетевую ошибку в понятное русское сообщение."""
    if not raw:
        return _("Не удалось войти. Попробуйте ещё раз.")
    low = raw.lower()
    if any(k in low for k in ("invalid", "incorrect", "wrong", "неверн", "not found", "not exist",
                               "no active", "does not exist")):
        return _("Неверный логин или пароль.")
    if any(k in low for k in ("password", "пароль")):
        return _("Неверный логин или пароль.")
    if any(k in low for k in ("login", "логин", "email", "user")):
        return _("Пользователь с таким логином не найден.")
    if any(k in low for k in ("connection", "timeout", "host", "network", "refused",
                               "unreachable", "соединен", "подключен", "сеть", "недоступ")):
        return _("Ошибка сети. Проверьте подключение к интернету.")
    if any(k in low for k in ("ssl", "tls", "certificate")):
        return _("Ошибка безопасного соединения (SSL).")
    if any(k in low for k in ("server", "500", "503", "unavailable")):
        return _("Сервер временно недоступен. Попробуйте позже.")
    return raw.rstrip(".") + "."


def _status_label(status: str | None) -> str:
    return {
        "active": _("Активна"),
        "scheduled": _("Запланирована"),
        "finished": _("Завершена"),
        "archived": _("В архиве"),
    }.get(status or "", "")
