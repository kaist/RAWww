## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Окно удаления объектов: кисть и навигация, без моделей и файловых операций."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from PySide6.QtCore import QPointF, QProcess, QRectF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from .i18n import gettext as _
from .theme import _fomantic_icon
from .widgets import SettingsCheckBox


class _BusyIndicator(QWidget):
    """Небольшой индикатор панели; таймер работает лишь пока индикатор виден."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(24,24)
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._advance)

    def _advance(self) -> None:
        self.angle = (self.angle+24) % 360
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#79aaff"), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(QRectF(3,3,18,18), self.angle*16, 250*16)


class InpaintView(QGraphicsView):
    """Владеет экранным изображением и векторными штрихами в координатах фото.

    Маску растеризует воркер. Панорама и зум не меняют её координаты; цвет
    штриха служит только подсказкой и не влияет на выделение для модели.
    """

    maskChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        scene = QGraphicsScene(self)
        self.setScene(scene)
        self.item = QGraphicsPixmapItem()
        scene.addItem(self.item)
        self.image = QImage()
        self.strokes: list[dict] = []
        self._paths: list[tuple[QPainterPath, QColor, float]] = []
        self.diameter = 40.0
        self.editable = False
        self._space = False
        self._panning = False
        self._drawing = False
        self._cursor = QPointF(-1000, -1000)
        self._last_mouse = QPointF()
        self._zoom = 1.0
        self.setBackgroundBrush(QColor("#161616"))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setCursor(Qt.CursorShape.BlankCursor)

    def show_image(self, image: QImage, *, reset: bool) -> None:
        """Смена фото вписывает его в окно, результат сохраняет экранный масштаб.

        Результат LaMa приходит полным, а до него пользователь рисует на draft.
        Оставить прежний transform означало бы внезапно увеличить фотографию во
        столько раз, во сколько draft был меньше оригинала.
        """
        previous = self.image.size()
        previous_center = self.mapToScene(self.viewport().rect().center())
        self.image = image
        self.item.setPixmap(QPixmap.fromImage(image))
        self.scene().setSceneRect(QRectF(image.rect()))
        self.clear_mask()
        if reset:
            self.fit()
        elif previous.isValid() and previous.width() and previous.height():
            factor = previous.width() / image.width()
            self.scale(factor, factor)
            self.centerOn(
                previous_center.x() * image.width() / previous.width(),
                previous_center.y() * image.height() / previous.height(),
            )

    def clear_mask(self) -> None:
        """Удаляет только выделение, не обработанные пиксели."""
        self.strokes.clear()
        self._paths.clear()
        self._drawing = False
        self.viewport().update()
        self.maskChanged.emit()

    def undo_stroke(self) -> None:
        """Убирает последний штрих до запуска обработки."""
        if self.strokes:
            self.strokes.pop()
            self._paths.pop()
            self._drawing = False
            self.viewport().update()
            self.maskChanged.emit()

    def _fit_scale(self) -> float:
        return min(max(1, self.viewport().width()-2)/max(1,self.image.width()),
                   max(1, self.viewport().height()-2)/max(1,self.image.height()))

    def fit(self) -> None:
        """Вписанный масштаб — нижняя граница приближения."""
        self._zoom = 1
        self.resetTransform()
        self.scale(self._fit_scale(), self._fit_scale())
        self.centerOn(self.item)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self.image.isNull():
            return
        before = self.mapToScene(event.position().toPoint())
        zoom = max(1.0, min(32.0, self._zoom * 1.2**(event.angleDelta().y()/120)))
        self.scale(zoom/self._zoom, zoom/self._zoom)
        self._zoom = zoom
        if zoom == 1:
            self.fit()
        else:
            after = self.mapToScene(event.position().toPoint())
            center = self.mapToScene(self.viewport().rect().center())
            self.centerOn(center + before - after)
        self._cursor = event.position()
        self.viewport().update()
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._zoom == 1:
            self.fit()
        else:
            scale = self._fit_scale()*self._zoom
            current = self.transform().m11()
            self.scale(scale/current, scale/current)

    def brush_colour(self, point: QPointF) -> QColor:
        """Выбирает наиболее далёкий цвет из жёлтого, синего, красного и зелёного."""
        palette = [QColor("#ffe100"), QColor("#1677ff"), QColor("#ff3045"), QColor("#18ef75")]
        if self.image.isNull():
            return palette[0]
        x = min(self.image.width()-1, max(0, round(point.x())))
        y = min(self.image.height()-1, max(0, round(point.y())))
        pixel = self.image.pixelColor(x, y)
        return max(palette, key=lambda c: sum((a-b)**2 for a,b in zip(c.getRgb()[:3], pixel.getRgb()[:3])))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus()
        self._cursor = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            if self._space:
                self._panning = True
                self._last_mouse = event.position()
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            elif self.editable:
                point = self.mapToScene(event.position().toPoint())
                if self.sceneRect().contains(point):
                    width = self.diameter / self.transform().m11()
                    self.strokes.append({"diameter": width, "points": [[point.x(), point.y()]]})
                    path = QPainterPath(point)
                    path.lineTo(point + QPointF(.001, 0))
                    self._paths.append((path, self.brush_colour(point), width))
                    self._drawing = True
                    self.maskChanged.emit()
            event.accept()
            self.viewport().update()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._cursor = event.position()
        if self._panning:
            delta = event.position() - self._last_mouse
            self._last_mouse = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value()-round(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value()-round(delta.y()))
        elif self._drawing and self.editable:
            point = self.mapToScene(event.position().toPoint())
            self.strokes[-1]["points"].append([point.x(), point.y()])
            self._paths[-1][0].lineTo(point)
        self.viewport().update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drawing = self._panning = False
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if self._space else Qt.CursorShape.BlankCursor)
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Space:
            self._space = True
            self._drawing = False
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        elif event.key() in (Qt.Key.Key_BracketLeft, Qt.Key.Key_BracketRight) or event.text() in ("[", "]", "х", "ъ"):
            smaller = event.key() == Qt.Key.Key_BracketLeft or event.text() in ("[", "х")
            self.diameter = min(500, max(3, self.diameter*(1/1.2 if smaller else 1.2)))
            self.viewport().update()
        elif event.key() == Qt.Key.Key_Z and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if self.editable:
                self.undo_stroke()
        else:
            # Стрелки принадлежат окну, а не прокрутке QGraphicsView.
            event.ignore()
            return
        event.accept()

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space = self._panning = False
            self.viewport().setCursor(Qt.CursorShape.BlankCursor)
        event.accept()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._space = self._panning = self._drawing = False
        self.viewport().setCursor(Qt.CursorShape.BlankCursor)
        super().focusOutEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._cursor = QPointF(-1000, -1000)
        self.viewport().update()
        super().leaveEvent(event)

    def drawForeground(self, painter: QPainter, rect) -> None:  # noqa: N802
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(self.sceneRect())
        for path, colour, width in self._paths:
            tint = QColor(colour)
            tint.setAlpha(115)
            painter.setPen(QPen(tint, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)
        painter.restore()
        if self._space or not self.editable:
            return
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = self.brush_colour(self.mapToScene(self._cursor.toPoint()))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for pen in (QPen(QColor(0,0,0,210), 2), QPen(colour, 1)):
            painter.setPen(pen)
            painter.drawEllipse(self._cursor, self.diameter/2, self.diameter/2)
        painter.restore()


class InpaintDialog(QDialog):
    """Координирует окно и процесс; ни ONNX, ни оригиналы на диск UI не пишет.

    Ревизии ведутся по каждому пути, включая уже покинутые кадры с автозаписью.
    Закрытие ждёт записи асинхронно и лишь затем завершает процесс с моделью.
    """

    def __init__(self, paths: list[Path], current: Path, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.paths = [str(p) for p in paths]
        self.settings = settings
        self.index = self.paths.index(str(current)) if str(current) in self.paths else 0
        self.request = 0
        self.revisions: dict[str, int] = {}
        self.saved: dict[str, int] = {}
        self.pending: dict[str, set[int]] = {}
        self._ready = False
        self._model_phase = "loading"
        self._downloaded = 0
        self._download_total: int | None = None
        self._loading = False
        self._processing = False
        self._displayed_path = None
        self._closing = False
        self._closed = False
        self._after_save = None
        self._buffer = bytearray()
        self._header = None
        self._stderr = b""
        self.setObjectName("inpaintDialog")
        self.setWindowTitle(_("Удаление объектов"))
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowMaximizeButtonHint | Qt.WindowType.WindowCloseButtonHint)
        self.resize(1400, 900)
        root = QVBoxLayout(self)
        root.setContentsMargins(12,12,12,12)
        root.setSpacing(10)
        panel = QFrame(self)
        panel.setObjectName("batchRetouchPanel")
        bar = QHBoxLayout(panel)
        bar.setContentsMargins(12,10,12,10)
        bar.setSpacing(10)
        root.addWidget(panel)
        navigation = QFrame(panel)
        navigation.setObjectName("batchRetouchOverlay")
        navigation_row = QHBoxLayout(navigation)
        navigation_row.setContentsMargins(5,5,5,5)
        navigation_row.setSpacing(4)
        self.previous = self._icon_button("chevron-left", _("Предыдущее фото"), lambda: self.navigate(-1))
        self.next = self._icon_button("chevron-right", _("Следующее фото"), lambda: self.navigate(1))
        navigation_row.addWidget(self.previous)
        navigation_row.addWidget(self.next)
        bar.addWidget(navigation)
        information = QVBoxLayout()
        information.setSpacing(2)
        self.counter = QLabel()
        self.counter.setObjectName("batchRetouchSliderLabel")
        self.counter.setMaximumWidth(320)
        information.addWidget(self.counter)
        self.status = QLabel()
        self.status.setObjectName("batchResizeStatus")
        information.addWidget(self.status)
        bar.addLayout(information, 1)
        self.download_progress = QProgressBar()
        self.download_progress.setObjectName("batchProgress")
        self.download_progress.setFixedWidth(150)
        self.download_progress.setTextVisible(False)
        self.download_progress.hide()
        bar.addWidget(self.download_progress)
        self.spinner = _BusyIndicator()
        bar.addWidget(self.spinner)
        self.autosave = SettingsCheckBox(_("Автосохранение"))
        self.autosave.setObjectName("batchResizeOption")
        self.autosave.setChecked(self.settings.value("inpaint/autosave", True, bool))
        self.autosave.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bar.addWidget(self.autosave)
        self.save_button = QPushButton(_("Сохранить"))
        self.apply_button = QPushButton(_("Удалить объекты (Enter)"))
        self.clear_button = QPushButton(_("Очистить маску"))
        for button in (self.clear_button, self.apply_button, self.save_button):
            button.setObjectName("batchResizePrimaryButton" if button is self.apply_button else "batchResizeSecondaryButton")
            button.setFixedHeight(40)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setAutoDefault(False)
            button.setIconSize(QSize(18, 18))
            bar.addWidget(button)
        self.apply_button.setIcon(_fomantic_icon("magic", 18, "#ffffff"))
        self.clear_button.setIcon(_fomantic_icon("close", 16))
        self.save_button.setIcon(_fomantic_icon("save", 18))
        self.view = InpaintView(self)
        root.addWidget(self.view, 1)
        self.view.maskChanged.connect(self._mask_changed)
        self.clear_button.clicked.connect(self.view.clear_mask)
        self.apply_button.clicked.connect(self.apply)
        self.save_button.clicked.connect(self.save)
        self.autosave.toggled.connect(self._autosave_changed)
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        # QAction с контекстом дочерних виджетов не зависит от того, у холста,
        # флажка или кнопки сейчас фокус: Ctrl+S всегда означает сохранение.
        save_action = QAction(self)
        save_action.setShortcut(QKeySequence("Ctrl+S"))
        save_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        save_action.triggered.connect(self.save)
        self.addAction(save_action)
        self._update()
        QTimer.singleShot(0, self._start)

    @property
    def path(self) -> str:
        return self.paths[self.index]

    def _icon_button(self, icon, text, callback) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("batchRetouchOverlayButton")
        button.setIcon(_fomantic_icon(icon, 24))
        button.setIconSize(QSize(24,24))
        button.setToolTip(text)
        button.setAccessibleName(text)
        button.setFixedSize(36,36)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(callback)
        return button

    def _start(self) -> None:
        if self._closed or self._closing:
            return
        args = ["--inpaint-worker"] if getattr(sys, "frozen", False) else ["-m", "rawww.inpaint_worker"]
        self.process.start(sys.executable, args)
        self.view.setFocus()

    def _send(self, command: str, **values) -> None:
        self.process.write(json.dumps({"command": command, **values}, ensure_ascii=False).encode("utf-8")+b"\n")

    def _neighbors(self) -> list[str]:
        return [self.paths[i] for delta in (1,-1,2,-2) if 0 <= (i := self.index+delta) < len(self.paths)]

    def _open(self) -> None:
        self.request += 1
        self._loading = True
        self._displayed_path = None
        self.view.clear_mask()
        self._send("open", path=self.path, request=self.request, neighbors=self._neighbors())
        self._update()

    def _mask_changed(self) -> None:
        """Первый штрих запускает полный декодер, пока пользователь рисует маску."""
        self._update()
        if self._ready and not self._loading and self.view.strokes:
            self._send("prepare", path=self.path)

    def _dirty(self, path: str) -> bool:
        return self.revisions.get(path, 0) > self.saved.get(path, 0)

    def _update(self) -> None:
        active = self._ready and not self._closing and not self._closed
        idle = (active and not self._loading and not self._processing and not self._after_save
                and self._displayed_path == self.path and self.path in self.revisions)
        self.view.editable = idle
        self.previous.setEnabled(active and not self._processing and not self._after_save and self.index > 0)
        self.next.setEnabled(active and not self._processing and not self._after_save and self.index+1 < len(self.paths))
        self.apply_button.setEnabled(idle and bool(self.view.strokes))
        self.clear_button.setEnabled(idle and bool(self.view.strokes))
        saving = any(self.pending.values())
        self.save_button.setVisible(not self.autosave.isChecked() or self._dirty(self.path))
        self.save_button.setEnabled(idle and self._dirty(self.path) and self.revisions[self.path] not in self.pending.get(self.path, set()))
        self.autosave.setEnabled(not self._closing)
        downloading = not self._ready and self._model_phase == "downloading"
        self.download_progress.setVisible(downloading)
        if downloading:
            if self._download_total:
                self.download_progress.setRange(0, self._download_total)
                self.download_progress.setValue(self._downloaded)
            else:
                self.download_progress.setRange(0, 0)
        self.spinner.setVisible(not self._ready or self._loading or self._processing or saving or self._closing)
        self.view.viewport().setCursor(Qt.CursorShape.BlankCursor if idle else Qt.CursorShape.ArrowCursor)
        if self._closing:
            text = _("Завершение сохранения…") if saving else _("Закрытие…")
        elif downloading and self._download_total:
            done = f"{self._downloaded / 1024 / 1024:.0f} MiB"
            total = f"{self._download_total / 1024 / 1024:.0f} MiB"
            text = _("Скачивание модели: {done} из {total}").format(done=done, total=total)
        elif downloading:
            text = _("Скачивание модели…")
        elif not self._ready:
            text = _("Загрузка модели…")
        elif self._processing:
            text = _("Удаление объектов…")
        elif self._loading:
            text = _("Загрузка изображения…")
        elif saving:
            text = _("Сохранение в фоне…")
        elif self._dirty(self.path):
            text = _("Есть несохранённые изменения")
        else:
            text = _("Готово")
        self.status.setText(text)
        caption = f"{self.index+1} / {len(self.paths)} · {Path(self.path).name}"
        self.counter.setText(self.counter.fontMetrics().elidedText(caption, Qt.TextElideMode.ElideMiddle, 310))
        self.counter.setToolTip(self.path)

    def apply(self) -> None:
        """Фиксирует маску для конкретной ревизии; повторный Enter не дублирует задачу."""
        if not self.view.editable or not self.view.strokes:
            return
        self._processing = True
        self._send("apply", path=self.path, request=self.request, revision=self.revisions[self.path],
                   strokes=self.view.strokes, draft_size=[self.view.image.width(), self.view.image.height()],
                   neighbors=self._neighbors())
        self._update()

    def save(self, path: str | None = None) -> None:
        """Сразу отмечает запись, чтобы навигация не ждала ответа процесса."""
        if not self._ready or self._processing or self._closing:
            return
        path = path if isinstance(path, str) else self.path
        if not self._dirty(path):
            return
        revision = self.revisions[path]
        pending = self.pending.setdefault(path, set())
        if revision not in pending:
            pending.add(revision)
            self._send("save", path=path, revision=revision)
        self._update()

    def _autosave_changed(self, checked: bool) -> None:
        """Запоминает выбор сразу, чтобы новое окно не меняло стратегию записи."""
        self.settings.setValue("inpaint/autosave", checked)
        if checked and not self._processing:
            self.save()
        self._update()

    def _confirm_leave(self, continuation) -> bool:
        """Маска и незаписанный результат требуют явного решения перед уходом."""
        dirty = self._dirty(self.path)
        pending_current = self.revisions.get(self.path, 0) in self.pending.get(self.path, set())
        if not self.view.strokes and (not dirty or pending_current):
            return True
        buttons = QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel
        if dirty and self._ready and not self._processing:
            buttons |= QMessageBox.StandardButton.Save
        choice = QMessageBox.warning(self, _("Несохранённые изменения"),
            _("Есть несохранённый результат или нарисованная маска. Сохранить результат, отбросить изменения или остаться?"),
            buttons, QMessageBox.StandardButton.Cancel)
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            self._after_save = continuation
            self.save()
            return False
        if self.pending.get(self.path):
            # Уже принятая запись завершится; можно отбросить лишь новую маску.
            if not pending_current:
                self._after_save = lambda: (self._discard_current(), continuation())
                return False
        else:
            self._discard_current()
        self.view.clear_mask()
        return True

    def _discard_current(self) -> None:
        self._send("discard", path=self.path)
        self.revisions.pop(self.path, None)
        self.saved.pop(self.path, None)
        self.view.clear_mask()

    def navigate(self, delta: int) -> None:
        if self._processing or self._closing or self._after_save or not self._ready:
            return
        index = self.index + delta
        if not 0 <= index < len(self.paths):
            return

        def go() -> None:
            self.index = index
            self._open()
        if self._confirm_leave(go):
            go()

    def _read_stderr(self) -> None:
        self._stderr = (self._stderr + bytes(self.process.readAllStandardError()))[-8000:]

    def _read(self) -> None:
        """Принимает кадры без PNG/JPEG-декодирования в главном потоке."""
        self._buffer.extend(bytes(self.process.readAllStandardOutput()))
        while not self._closed:
            if self._header is None:
                end = self._buffer.find(b"\n")
                if end < 0:
                    return
                self._header = json.loads(self._buffer[:end])
                del self._buffer[:end+1]
            length = self._header["bytes"]
            if len(self._buffer) < length:
                return
            payload = bytes(self._buffer[:length]) if length else b""
            del self._buffer[:length]
            header, self._header = self._header, None
            self._event(header, payload)

    def _event(self, event: dict, payload: bytes = b"") -> None:
        kind = event["event"]
        path = event.get("path", "")
        if kind == "downloading":
            self._model_phase = "downloading"
            self._downloaded = event.get("downloaded", 0)
            self._download_total = event.get("total") or None
        elif kind == "loading_model":
            self._model_phase = "loading"
        elif kind == "ready":
            self._ready = True
            self._open()
        elif kind in {"frame", "result"}:
            if event["request"] != self.request or path != self.path or self._closing:
                return
            self.revisions[path] = event["revision"]
            self.saved[path] = max(self.saved.get(path, 0), event["saved_revision"])
            image = QImage(payload, event["width"], event["height"], event["width"]*3, QImage.Format.Format_RGB888).copy()
            self._loading = self._processing = False
            self._displayed_path = path
            self.view.show_image(image, reset=kind == "frame")
            if kind == "result" and self.autosave.isChecked():
                self.save(path)
        elif kind in {"saved", "save_error"}:
            self.pending.setdefault(path, set()).discard(event["revision"])
            if kind == "saved":
                self.saved[path] = max(self.saved.get(path, 0), event["revision"])
                if self._after_save and not self.pending.get(self.path):
                    continuation, self._after_save = self._after_save, None
                    self.view.clear_mask()
                    continuation()
                if self._closing and not any(self.pending.values()):
                    self._stop_process()
            else:
                self._closing = False
                self._after_save = None
                self._show_error(event, _("Не удалось сохранить изображение"))
        elif kind in {"error", "model_error"}:
            if kind == "error" and (event["request"] != self.request or path != self.path):
                return
            self._loading = self._processing = False
            self._show_error(event, _("Не удалось обработать изображение") if kind == "error" else _("Не удалось загрузить модель"))
        self._update()

    def _show_error(self, event: dict, title: str) -> None:
        code = event.get("error", "")
        if code == "unsupported_image":
            message = _("Поддерживаются одиночные 8-битные JPEG, PNG, WebP и TIFF. RAW, многокадровые и другие цветовые режимы недоступны.")
        elif code == "external_change":
            message = _("Файл изменён другой программой. Сохранение отменено, чтобы не перезаписать новую версию.")
        else:
            message = str(code)
        QMessageBox.warning(self, title, f"{Path(event.get('path', '')).name}\n{message}")

    def _process_error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart and not self._closing:
            self._ready = False
            self.spinner.hide()
            self.status.setText(_("Не удалось загрузить модель"))
            QMessageBox.warning(self, _("Не удалось загрузить модель"), self.process.errorString())

    def _finished(self, *args) -> None:
        if self._closing:
            self._closed = True
            self._buffer.clear()
            self.view.image = QImage()
            self.view.item.setPixmap(QPixmap())
            super().accept()
        elif not self._closed:
            self._ready = False
            self._loading = self._processing = False
            self.pending.clear()
            self._update()
            self.spinner.hide()
            self.status.setText(_("Процесс обработки завершился. Закройте и откройте утилиту снова."))

    def _stop_process(self) -> None:
        """Вызывается после записей: освобождение модели не задерживает Qt event loop."""
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self._finished()
        else:
            self.process.kill()

    def _begin_close(self) -> None:
        # Не теряем неудачную автозапись уже покинутого кадра. Сначала возвращаем
        # его владельцу на экран, где доступны повторная запись и явный отказ.
        failed = next((p for p in self.revisions if p != self.path and self._dirty(p)
                       and self.revisions[p] not in self.pending.get(p, set())), None)
        if failed and self._ready:
            self.index = self.paths.index(failed)
            self._open()
            return
        self._closing = True
        self.view.editable = False
        self._update()
        if not any(self.pending.values()):
            self._stop_process()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._closed:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        if self._confirm_leave(self._begin_close):
            self._begin_close()

    def reject(self) -> None:
        self.close()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Left:
            self.navigate(-1)
        elif key == Qt.Key.Key_Right:
            self.navigate(1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.apply()
        elif key == Qt.Key.Key_S and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.save()
        elif key == Qt.Key.Key_Escape and self.view.strokes and not self._processing:
            self.view.clear_mask()
        else:
            super().keyPressEvent(event)
            return
        event.accept()
