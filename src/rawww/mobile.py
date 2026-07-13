"""Mobile (Android) shell: ShotSync "take a shooting for selection" only.

This is a deliberately small, single-window application that reuses the exact
same networking and storage as the desktop app — :class:`ShotSyncClient`
(login + shootings list), the process-wide :class:`ShotSyncHub` (shared socket
and preview downloader) and :class:`SelectionMarkSyncer` (durable mark upload) —
but replaces the desktop's tabbed, filesystem-oriented UI with four touch
screens:

    login -> shootings list -> thumbnail grid -> full-screen viewer

None of the desktop-only subsystems (AI, XMP, batch utilities, RAW/video
decoding, the filesystem browser) are imported here, so the Android build never
pulls in ``rawpy``/``onnxruntime``/ExifTool. Thumbnails are decoded from the
locally downloaded 1920px JPEG previews with Qt on a small thread pool.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QIcon, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .cache import FolderCache
from .shotsync_client import ShotSyncClient
from .shotsync_hub import shotsync_hub
from .shotsync_login import humanize_login_error
from .shotsync_selection import SelectionMarkSyncer

SHOTSYNC_BASE_URL = "https://shotsync.ru"
SETTINGS_NAME = "ctrlka"
APP_NAME = "Контролька"
THUMB_PX = 220

# Colour labels shipped by the server, in a stable display order.
COLOR_LABELS: list[tuple[str, str]] = [
    ("", "—"),
    ("red", "Красный"),
    ("yellow", "Жёлтый"),
    ("green", "Зелёный"),
    ("blue", "Синий"),
    ("purple", "Фиолетовый"),
]
_COLOR_HEX = {
    "red": "#e5484d",
    "yellow": "#f5d90a",
    "green": "#46a758",
    "blue": "#3b82f6",
    "purple": "#8e4ec6",
}


class _ThumbSignals(QObject):
    done = Signal(str, QImage)


class _ThumbTask(QRunnable):
    """Decode one JPEG preview to a thumbnail off the UI thread."""

    def __init__(self, name: str, path: Path, size: int) -> None:
        super().__init__()
        self._name = name
        self._path = path
        self._size = size
        self.signals = _ThumbSignals()

    def run(self) -> None:  # noqa: D401 - QRunnable entry point
        reader = QImageReader(str(self._path))
        reader.setAutoTransform(True)
        image = reader.read()
        if not image.isNull():
            image = image.scaled(
                self._size,
                self._size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.signals.done.emit(self._name, image)


class MobileWindow(QMainWindow):
    """The whole mobile app: sign in, pick a shooting, cull it, sync marks."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)

        self.client = ShotSyncClient(SHOTSYNC_BASE_URL, self)
        self.hub = shotsync_hub(SHOTSYNC_BASE_URL)
        self._pool = QThreadPool(self)

        self._folder: Path | None = None
        self._cache: FolderCache | None = None
        self._syncer: SelectionMarkSyncer | None = None
        self._names: list[str] = []
        self._details: dict[str, dict] = {}
        self._index = 0
        self._active_shooting = 0

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self._build_login_page()
        self._build_shootings_page()
        self._build_grid_page()
        self._build_viewer_page()

        self.client.loginSucceeded.connect(self._on_login_succeeded)
        self.client.loginFailed.connect(self._on_login_failed)
        self.client.sessionVerified.connect(lambda _user: self._show_shootings())
        self.client.sessionInvalid.connect(self._on_session_invalid)
        self.client.shootingsLoaded.connect(self._on_shootings_loaded)
        self.client.shootingsFailed.connect(self._on_shootings_failed)

        self.hub.downloader.progress.connect(self._on_download_progress)
        self.hub.downloader.finished.connect(self._on_download_finished)
        self.hub.downloader.failed.connect(self._on_download_failed)

        QTimer.singleShot(0, self._restore_session)

    # ----- persisted credential -----------------------------------------
    def _saved_key(self) -> str:
        return QSettings(SETTINGS_NAME, SETTINGS_NAME).value("shotsync/api_key", "", str)

    def _store_key(self, key: str) -> None:
        settings = QSettings(SETTINGS_NAME, SETTINGS_NAME)
        if key:
            settings.setValue("shotsync/api_key", key)
        else:
            settings.remove("shotsync/api_key")

    def _restore_session(self) -> None:
        key = self._saved_key()
        if key:
            self.client.set_api_key(key)
            self.hub.set_api_key(key)
            self.client.verify_session()
            self._show_shootings()
        else:
            self.stack.setCurrentWidget(self.login_page)

    # ----- login page ----------------------------------------------------
    def _build_login_page(self) -> None:
        self.login_page = QWidget()
        layout = QVBoxLayout(self.login_page)
        layout.setContentsMargins(32, 40, 32, 40)
        layout.setSpacing(14)
        layout.addStretch(1)
        title = QLabel("Вход в ShotSync")
        title.setObjectName("shotsyncTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        self.login_edit = QLineEdit()
        self.login_edit.setPlaceholderText("Логин или email")
        self.login_edit.returnPressed.connect(self._submit_login)
        layout.addWidget(self.login_edit)
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("Пароль")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.returnPressed.connect(self._submit_login)
        layout.addWidget(self.password_edit)
        self.login_error = QLabel()
        self.login_error.setObjectName("shotsyncError")
        self.login_error.setWordWrap(True)
        self.login_error.hide()
        layout.addWidget(self.login_error)
        self.login_button = QPushButton("Войти")
        self.login_button.setObjectName("settingsPrimaryButton")
        self.login_button.clicked.connect(self._submit_login)
        layout.addWidget(self.login_button)
        layout.addStretch(2)
        self.stack.addWidget(self.login_page)

    def _submit_login(self) -> None:
        login = self.login_edit.text().strip()
        password = self.password_edit.text()
        if not login or not password:
            self._show_login_error("Введите логин и пароль.")
            return
        self.login_button.setEnabled(False)
        self.login_button.setText("Входим…")
        self.login_error.hide()
        self.client.login(login, password)

    def _show_login_error(self, message: str) -> None:
        self.login_button.setEnabled(True)
        self.login_button.setText("Войти")
        self.login_error.setText(humanize_login_error(message))
        self.login_error.show()

    def _on_login_succeeded(self, _user: dict, key: str) -> None:
        self._store_key(key)
        self.hub.set_api_key(key)
        self.login_button.setEnabled(True)
        self.login_button.setText("Войти")
        self.password_edit.clear()
        self._show_shootings()

    def _on_login_failed(self, message: str) -> None:
        self._show_login_error(message)

    def _on_session_invalid(self, _message: str) -> None:
        self._store_key("")
        self.hub.set_api_key("")
        self.stack.setCurrentWidget(self.login_page)

    # ----- shootings page ------------------------------------------------
    def _build_shootings_page(self) -> None:
        self.shootings_page = QWidget()
        layout = QVBoxLayout(self.shootings_page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("Съёмки")
        title.setObjectName("shotsyncTitle")
        header.addWidget(title)
        header.addStretch(1)
        refresh = QPushButton("Обновить")
        refresh.setObjectName("settingsSecondaryButton")
        refresh.clicked.connect(self.client.fetch_shootings)
        header.addWidget(refresh)
        logout = QPushButton("Выйти")
        logout.setObjectName("settingsSecondaryButton")
        logout.clicked.connect(self._logout)
        header.addWidget(logout)
        layout.addLayout(header)
        self.shootings_list = QListWidget()
        self.shootings_list.itemActivated.connect(self._shooting_chosen)
        self.shootings_list.itemClicked.connect(self._shooting_chosen)
        layout.addWidget(self.shootings_list, 1)
        self.stack.addWidget(self.shootings_page)

    def _show_shootings(self) -> None:
        self.stack.setCurrentWidget(self.shootings_page)
        self.client.fetch_shootings()

    def _logout(self) -> None:
        self.client.logout()
        self._store_key("")
        self.hub.set_api_key("")
        self.shootings_list.clear()
        self.stack.setCurrentWidget(self.login_page)

    def _on_shootings_loaded(self, shootings: list) -> None:
        self.shootings_list.clear()
        for shooting in shootings:
            if not isinstance(shooting, dict):
                continue
            title = str(shooting.get("title") or "Без названия")
            count = shooting.get("photos_count") or shooting.get("photo_count")
            label = f"{title}  ·  {count} фото" if count else title
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, shooting)
            item.setSizeHint(QSize(0, 52))
            self.shootings_list.addItem(item)
        if self.shootings_list.count() == 0:
            self.shootings_list.addItem(QListWidgetItem("Нет доступных съёмок"))

    def _on_shootings_failed(self, message: str) -> None:
        QMessageBox.warning(self, "ShotSync", message)

    def _shooting_chosen(self, item: QListWidgetItem) -> None:
        shooting = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(shooting, dict):
            return
        shooting_id = int(shooting.get("id") or 0)
        if not shooting_id or self.hub.downloader.is_running(shooting_id):
            return
        title = str(shooting.get("title") or "Съёмка")
        self._active_shooting = shooting_id
        self.grid_title.setText(title)
        self.grid_list.clear()
        self.download_bar.setValue(0)
        self.download_bar.setVisible(True)
        self.download_status.setText("Получаем фотографии с сервера…")
        self.download_status.setVisible(True)
        self.stack.setCurrentWidget(self.grid_page)
        self.hub.downloader.start(shooting_id, title)

    # ----- grid page -----------------------------------------------------
    def _build_grid_page(self) -> None:
        self.grid_page = QWidget()
        layout = QVBoxLayout(self.grid_page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        back = QPushButton("‹ Съёмки")
        back.setObjectName("settingsSecondaryButton")
        back.clicked.connect(self._show_shootings)
        header.addWidget(back)
        self.grid_title = QLabel("")
        self.grid_title.setObjectName("shotsyncTitle")
        header.addWidget(self.grid_title, 1)
        layout.addLayout(header)
        self.download_status = QLabel("")
        self.download_status.setVisible(False)
        layout.addWidget(self.download_status)
        self.download_bar = QProgressBar()
        self.download_bar.setVisible(False)
        layout.addWidget(self.download_bar)
        self.grid_list = QListWidget()
        self.grid_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid_list.setMovement(QListWidget.Movement.Static)
        self.grid_list.setIconSize(QSize(THUMB_PX, THUMB_PX))
        self.grid_list.setGridSize(QSize(THUMB_PX + 16, THUMB_PX + 16))
        self.grid_list.setSpacing(6)
        self.grid_list.itemActivated.connect(self._thumb_chosen)
        self.grid_list.itemClicked.connect(self._thumb_chosen)
        layout.addWidget(self.grid_list, 1)
        self.stack.addWidget(self.grid_page)

    def _on_download_progress(self, shooting_id: int, done: int, total: int) -> None:
        if shooting_id != self._active_shooting:
            return
        self.download_bar.setMaximum(max(total, 1))
        self.download_bar.setValue(done)
        self.download_status.setText(
            f"Получено фотографий: {done} из {total}" if total else "Загружаем фотографии…"
        )

    def _on_download_failed(self, shooting_id: int, message: str) -> None:
        if shooting_id != self._active_shooting:
            return
        self.download_bar.setVisible(False)
        self.download_status.setText("")
        QMessageBox.warning(self, "ShotSync", f"Не удалось загрузить съёмку:\n{message}")
        self._show_shootings()

    def _on_download_finished(self, shooting_id: int, folder: str) -> None:
        if shooting_id != self._active_shooting:
            return
        self.download_bar.setVisible(False)
        self.download_status.setVisible(False)
        self._open_folder(Path(folder), shooting_id)

    def _open_folder(self, folder: Path, shooting_id: int) -> None:
        self._detach_syncer()
        if self._cache is not None:
            self._cache.close(flush=True)
            self._cache = None
        if not folder.is_dir():
            QMessageBox.warning(self, "ShotSync", "Папка съёмки не найдена.")
            self._show_shootings()
            return
        names = sorted(
            p.name for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}
        )
        self._folder = folder
        self._names = names
        self._cache = FolderCache(folder, live_names=set(names), load_from_disk=True)
        self._details = self._cache.load_photo_details(include_metadata=False)
        session = self._cache.shotsync_session()
        if session:
            self._syncer = SelectionMarkSyncer(self.hub, self._cache, session[0], self)
        self._populate_grid()

    def _populate_grid(self) -> None:
        self.grid_list.clear()
        assert self._folder is not None
        for name in self._names:
            item = QListWidgetItem(self._rating_prefix(name))
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setSizeHint(QSize(THUMB_PX + 16, THUMB_PX + 16))
            self.grid_list.addItem(item)
            task = _ThumbTask(name, self._folder / name, THUMB_PX)
            task.signals.done.connect(self._thumb_ready)
            self._pool.start(task)

    def _rating_prefix(self, name: str) -> str:
        detail = self._details.get(name) or {}
        rating = detail.get("rating") or 0
        color = detail.get("color_label") or ""
        stars = "★" * int(rating) if rating else ""
        dot = "●" if color else ""
        return f"{stars}{dot}".strip()

    def _thumb_ready(self, name: str, image: QImage) -> None:
        item = self._grid_item(name)
        if item is None or image.isNull():
            return
        item.setIcon(QIcon(QPixmap.fromImage(image)))

    def _grid_item(self, name: str) -> QListWidgetItem | None:
        for row in range(self.grid_list.count()):
            item = self.grid_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == name:
                return item
        return None

    def _thumb_chosen(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        if name in self._names:
            self._index = self._names.index(name)
            self._show_viewer()

    # ----- viewer page ---------------------------------------------------
    def _build_viewer_page(self) -> None:
        self.viewer_page = QWidget()
        layout = QVBoxLayout(self.viewer_page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        header = QHBoxLayout()
        back = QPushButton("‹ Сетка")
        back.setObjectName("settingsSecondaryButton")
        back.clicked.connect(self._back_to_grid)
        header.addWidget(back)
        self.viewer_name = QLabel("")
        self.viewer_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.viewer_name, 1)
        prev_btn = QPushButton("‹")
        prev_btn.setObjectName("settingsSecondaryButton")
        prev_btn.clicked.connect(lambda: self._step(-1))
        header.addWidget(prev_btn)
        next_btn = QPushButton("›")
        next_btn.setObjectName("settingsSecondaryButton")
        next_btn.clicked.connect(lambda: self._step(1))
        header.addWidget(next_btn)
        layout.addLayout(header)

        self.viewer_image = QLabel()
        self.viewer_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.viewer_image.setMinimumHeight(240)
        layout.addWidget(self.viewer_image, 1)

        ratings = QHBoxLayout()
        ratings.addWidget(QLabel("Оценка:"))
        self.rating_buttons: list[QPushButton] = []
        for value in range(0, 6):
            button = QPushButton("—" if value == 0 else "★" * value)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, v=value: self._set_rating(v))
            ratings.addWidget(button)
            self.rating_buttons.append(button)
        ratings.addStretch(1)
        layout.addLayout(ratings)

        colors = QHBoxLayout()
        colors.addWidget(QLabel("Цвет:"))
        self.color_buttons: list[tuple[str, QPushButton]] = []
        for value, label in COLOR_LABELS:
            button = QPushButton(label)
            button.setCheckable(True)
            hexcolor = _COLOR_HEX.get(value)
            if hexcolor:
                button.setStyleSheet(f"background-color: {hexcolor}; color: white;")
            button.clicked.connect(lambda _checked=False, v=value: self._set_color(v))
            colors.addWidget(button)
            self.color_buttons.append((value, button))
        colors.addStretch(1)
        layout.addLayout(colors)

        comment_row = QHBoxLayout()
        comment_row.addWidget(QLabel("Комментарий:"))
        self.comment_edit = QLineEdit()
        self.comment_edit.editingFinished.connect(self._commit_comment)
        comment_row.addWidget(self.comment_edit, 1)
        layout.addLayout(comment_row)

        self.pending_label = QLabel("")
        self.pending_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.pending_label)
        self.stack.addWidget(self.viewer_page)

    def _show_viewer(self) -> None:
        self.stack.setCurrentWidget(self.viewer_page)
        self._refresh_viewer()

    def _back_to_grid(self) -> None:
        self._commit_comment()
        name = self._current_name()
        if name is not None:
            item = self._grid_item(name)
            if item is not None:
                item.setText(self._rating_prefix(name))
        self.stack.setCurrentWidget(self.grid_page)

    def _current_name(self) -> str | None:
        if 0 <= self._index < len(self._names):
            return self._names[self._index]
        return None

    def _step(self, delta: int) -> None:
        self._commit_comment()
        new_index = self._index + delta
        if 0 <= new_index < len(self._names):
            self._index = new_index
            self._refresh_viewer()

    def _refresh_viewer(self) -> None:
        name = self._current_name()
        if name is None or self._folder is None:
            return
        self.viewer_name.setText(f"{name}  ({self._index + 1}/{len(self._names)})")
        reader = QImageReader(str(self._folder / name))
        reader.setAutoTransform(True)
        image = reader.read()
        if not image.isNull():
            target = self.viewer_image.size()
            pixmap = QPixmap.fromImage(image).scaled(
                target if target.width() > 1 else QSize(800, 600),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.viewer_image.setPixmap(pixmap)
        else:
            self.viewer_image.setText("Не удалось открыть изображение")
        self._sync_mark_controls(name)
        self._refresh_pending()

    def _sync_mark_controls(self, name: str) -> None:
        detail = self._details.get(name) or {}
        rating = int(detail.get("rating") or 0)
        color = detail.get("color_label") or ""
        for value, button in enumerate(self.rating_buttons):
            button.setChecked(value == rating)
        for value, button in self.color_buttons:
            button.setChecked(value == color)
        self.comment_edit.blockSignals(True)
        self.comment_edit.setText(detail.get("comment") or "")
        self.comment_edit.blockSignals(False)

    # ----- mark editing --------------------------------------------------
    def _detail_for(self, name: str) -> dict:
        return self._details.setdefault(
            name, {"rating": None, "color_label": "", "comment": ""}
        )

    def _set_rating(self, value: int) -> None:
        name = self._current_name()
        if name is None:
            return
        detail = self._detail_for(name)
        detail["rating"] = value or None
        for index, button in enumerate(self.rating_buttons):
            button.setChecked(index == value)
        self._persist(name, {"rating": True})

    def _set_color(self, value: str) -> None:
        name = self._current_name()
        if name is None:
            return
        detail = self._detail_for(name)
        detail["color_label"] = value
        for candidate, button in self.color_buttons:
            button.setChecked(candidate == value)
        self._persist(name, {"color_label": True})

    def _commit_comment(self) -> None:
        name = self._current_name()
        if name is None:
            return
        detail = self._detail_for(name)
        text = self.comment_edit.text()
        if text == (detail.get("comment") or ""):
            return
        detail["comment"] = text
        self._persist(name, {"comment": True})

    def _persist(self, name: str, changes: dict) -> None:
        if self._cache is None:
            return
        detail = self._detail_for(name)
        self._cache.store_photo_selection(
            name,
            rating=detail.get("rating"),
            color_label=detail.get("color_label") or "",
            comment=detail.get("comment") or "",
        )
        if self._syncer is not None:
            self._syncer.queue_mark(name, detail=dict(detail), changes=changes)
        self._refresh_pending()

    def _refresh_pending(self) -> None:
        if self._syncer is None:
            self.pending_label.setText("")
            return
        count = self._syncer.pending_count()
        self.pending_label.setText("Все метки отправлены" if count == 0 else f"Ожидают отправки: {count}")

    def _detach_syncer(self) -> None:
        if self._syncer is not None:
            self._syncer.detach()
            self._syncer.deleteLater()
            self._syncer = None

    def closeEvent(self, event) -> None:  # noqa: N802
        self._commit_comment()
        self._detach_syncer()
        if self._cache is not None:
            self._cache.close(flush=True)
            self._cache = None
        super().closeEvent(event)


def main() -> None:
    from .theme import _application_icon, apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(_application_icon())
    apply_theme(app)
    window = MobileWindow()
    window.showMaximized()
    sys.exit(app.exec())
