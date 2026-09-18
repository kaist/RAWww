## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Окно пакетного редактора: кисть и навигация, без моделей и файловых операций."""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys

from PySide6.QtCore import QPointF, QProcess, QRectF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
    QComboBox, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
    QToolButton, QVBoxLayout, QWidget,
)

from .i18n import gettext as _
from .theme import _fomantic_icon, _orientation_icon
from .widgets import SettingsCheckBox


_AUTOSAVE_DEBOUNCE_MS = 750
_ROTATE_STEP_DEGREES = .5
_ROTATE_QUARTER_TURN_DEGREES = 90


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


class InpaintHelpDialog(QDialog):
    """Показывает только сочетания пакетного редактора, не смешивая их с главным окном."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("helpDialog")
        self.setWindowTitle(_("Справка по горячим клавишам"))
        self.setModal(True)
        self.resize(500, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(10)
        title = QLabel(_("Горячие клавиши"))
        title.setObjectName("helpDialogTitle")
        layout.addWidget(title)
        rows = (
            (_("Предыдущее фото"), "Left"),
            (_("Следующее фото"), "Right"),
            (_("Кадрирование (C)"), "C"),
            (_("Изменить размер рамки"), "Ctrl+↑ / ↓"),
            (_("Переместить рамку"), "Shift+← / → / ↑ / ↓"),
            (_("Автогоризонт"), "H"),
            (_("Повернуть на 0,5°"), "< и >"),
            (_("Повернуть на 90°"), "Ctrl+< и >"),
            (_("Сбросить (R)"), "R"),
            (_("Назад (Ctrl+Z)"), "Ctrl+Z"),
            (_("Вперёд (Ctrl+Y)"), "Ctrl+Y"),
            (_("Сохранить"), "Ctrl+S"),
            (_("Удалить объекты (Enter)"), "Enter"),
            (_("Применить кадрирование"), _("Enter в режиме кадрирования")),
            (_("Очистить маску"), "Esc"),
        )
        table = QTableWidget(len(rows), 2, self)
        table.setObjectName("helpHotkeysTable")
        table.setHorizontalHeaderLabels((_("Действие"), _("Сочетание")))
        table.verticalHeader().hide()
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnWidth(0, 310)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        for row, (label, shortcut) in enumerate(rows):
            table.setItem(row, 0, QTableWidgetItem(label))
            table.setItem(row, 1, QTableWidgetItem(shortcut))
        layout.addWidget(table, 1)
        close = QPushButton(_("Закрыть"))
        close.setObjectName("helpDialogCloseButton")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)


class InpaintView(QGraphicsView):
    """Владеет экранным изображением и векторными штрихами в координатах фото.

    Маску растеризует воркер. Панорама и зум не меняют её координаты; цвет
    штриха служит только подсказкой и не влияет на выделение для модели.
    """

    maskChanged = Signal()
    cropChanged = Signal()
    editorShortcut = Signal(str)
    resized = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        scene = QGraphicsScene(self)
        self.setScene(scene)
        self.item = QGraphicsPixmapItem()
        scene.addItem(self.item)
        self.image = QImage()
        self.strokes: list[dict] = []
        self._paths: list[tuple[QPainterPath, QColor, float]] = []
        self._mask_pending = False
        self._mask_pulse_phase = 0.0
        self._mask_pulse_timer = QTimer(self)
        self._mask_pulse_timer.setInterval(40)
        self._mask_pulse_timer.timeout.connect(self._pulse_mask)
        self.diameter = 40.0
        self.editable = False
        self.crop_active = False
        self.crop_box = QRectF()
        self.crop_ratio: float | None = None
        self.crop_locked = True
        self.crop_vertical = False
        self._crop_drag = False
        self._crop_resize_handle: str | None = None
        self._crop_drag_start = QPointF()
        self._crop_drag_box = QRectF()
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

    def _update_crop_canvas(self) -> None:
        """Расширяет холст рамкой и вписывает фото вместе с пустым полем."""
        bounds = QRectF(self.image.rect()).united(self.crop_box)
        self.scene().setSceneRect(bounds)

    def _update_cursor(self, point: QPointF | None = None) -> None:
        """Не смешивает курсоры кисти, панорамирования и перемещения рамки."""
        if self._space:
            shape = Qt.CursorShape.ClosedHandCursor if self._panning else Qt.CursorShape.OpenHandCursor
        elif self._crop_drag:
            shape = Qt.CursorShape.ClosedHandCursor
        elif self.crop_active:
            scene_point = point if point is not None else self.mapToScene(self._cursor.toPoint())
            handle = self._crop_handle_at(scene_point)
            cursors = {
                "top_left": Qt.CursorShape.SizeFDiagCursor, "bottom_right": Qt.CursorShape.SizeFDiagCursor,
                "top_right": Qt.CursorShape.SizeBDiagCursor, "bottom_left": Qt.CursorShape.SizeBDiagCursor,
                "left": Qt.CursorShape.SizeHorCursor, "right": Qt.CursorShape.SizeHorCursor,
                "top": Qt.CursorShape.SizeVerCursor, "bottom": Qt.CursorShape.SizeVerCursor,
            }
            shape = cursors.get(handle, Qt.CursorShape.OpenHandCursor if self.crop_box.contains(scene_point) else Qt.CursorShape.ArrowCursor)
        else:
            shape = Qt.CursorShape.BlankCursor if self.editable else Qt.CursorShape.ArrowCursor
        self.viewport().setCursor(shape)

    def _crop_handle_at(self, point: QPointF) -> str | None:
        """Находит ближайший маркер в экранно-постоянной зоне захвата."""
        if self.image.isNull():
            return None
        radius = 10 / max(.001, self.transform().m11())
        handles = {
            "top_left": self.crop_box.topLeft(), "top_right": self.crop_box.topRight(),
            "bottom_left": self.crop_box.bottomLeft(), "bottom_right": self.crop_box.bottomRight(),
            "top": QPointF(self.crop_box.center().x(), self.crop_box.top()),
            "bottom": QPointF(self.crop_box.center().x(), self.crop_box.bottom()),
            "left": QPointF(self.crop_box.left(), self.crop_box.center().y()),
            "right": QPointF(self.crop_box.right(), self.crop_box.center().y()),
        }
        return next((name for name, handle in handles.items()
                     if abs(handle.x() - point.x()) <= radius and abs(handle.y() - point.y()) <= radius), None)

    def _resize_crop(self, point: QPointF) -> None:
        """Меняет рамку за выбранный маркер, сохраняя активное соотношение сторон."""
        handle = self._crop_resize_handle
        if not handle:
            return
        original = self._crop_drag_box
        ratio = self.crop_ratio or self.image.width() / self.image.height()
        if self.crop_ratio is not None and self.crop_vertical:
            ratio = 1 / ratio
        minimum = 2.0
        if handle in {"left", "top_left", "bottom_left"}:
            width = max(minimum, original.right() - point.x())
            left = original.right() - width
        elif handle in {"right", "top_right", "bottom_right"}:
            width = max(minimum, point.x() - original.left())
            left = original.left()
        else:
            width = original.width()
            left = original.left()
        if handle in {"top", "top_left", "top_right"}:
            height = max(minimum, original.bottom() - point.y())
            top = original.bottom() - height
        elif handle in {"bottom", "bottom_left", "bottom_right"}:
            height = max(minimum, point.y() - original.top())
            top = original.top()
        else:
            height = original.height()
            top = original.top()
        if not self.crop_locked:
            box = QRectF(left, top, width, height)
        elif handle in {"left", "right"}:
            height = width / ratio
            top = original.center().y() - height / 2
        elif handle in {"top", "bottom"}:
            width = height * ratio
            left = original.center().x() - width / 2
        else:
            # Угловой маркер выбирает ведущую ось, чтобы рамка не дёргалась по диагонали.
            if abs(width / original.width() - 1) >= abs(height / original.height() - 1):
                height = width / ratio
                if "top" in handle:
                    top = original.bottom() - height
            else:
                width = height * ratio
                if "left" in handle:
                    left = original.right() - width
        if self.crop_locked:
            box = QRectF(left, top, width, height)
        bounds = QRectF(self.image.rect())
        box = box.intersected(bounds)
        self.crop_box = box
        self._update_crop_canvas()
        self.cropChanged.emit()

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
        self.reset_crop()
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

    def reset_crop(self) -> None:
        """Возвращает рамку к полному кадру после загрузки либо применения кропа."""
        self.crop_box = QRectF(self.image.rect())
        self.crop_ratio = None
        self.scene().setSceneRect(QRectF(self.image.rect()))
        self.viewport().update()
        self.cropChanged.emit()

    def set_crop_ratio(self, ratio: float | None, vertical: bool = False) -> None:
        """Вписывает выбранное соотношение в снимок, сохраняя центр прежней рамки."""
        self.crop_ratio = ratio
        self.crop_vertical = vertical
        if ratio is None or self.image.isNull():
            self.crop_box = QRectF(self.image.rect())
        else:
            target = 1 / ratio if vertical else ratio
            bounds = QRectF(self.image.rect())
            width = bounds.width()
            height = width / target
            if height > bounds.height():
                height = bounds.height()
                width = height * target
            center = self.crop_box.center() if self.crop_box.isValid() else bounds.center()
            left = min(max(bounds.left(), center.x() - width / 2), bounds.right() - width)
            top = min(max(bounds.top(), center.y() - height / 2), bounds.bottom() - height)
            self.crop_box = QRectF(left, top, width, height)
        self.viewport().update()

    def crop_by_edge(self, key: Qt.Key, fraction: float = .01) -> bool:
        """Масштабирует рамку от центра: Ctrl+↑ расширяет, Ctrl+↓ уменьшает."""
        if self.image.isNull():
            return False
        if key not in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            return False
        box = QRectF(self.crop_box)
        minimum = 2.0
        dx, dy = self.image.width() * fraction, self.image.height() * fraction
        ratio = self.crop_ratio or self.image.width() / self.image.height()
        if self.crop_ratio is not None and self.crop_vertical:
            ratio = 1 / ratio
        direction = 1 if key == Qt.Key.Key_Up else -1
        center = box.center()
        bounds = QRectF(self.image.rect())
        max_width = 2 * min(center.x() - bounds.left(), bounds.right() - center.x())
        max_height = 2 * min(center.y() - bounds.top(), bounds.bottom() - center.y())
        if self.crop_locked:
            height = min(max_height, max(minimum, box.height() + direction * dy))
            width = min(max_width, height * ratio)
            height = width / ratio
        else:
            width = min(max_width, max(minimum, box.width() + direction * dx))
            height = min(max_height, max(minimum, box.height() + direction * dy))
        box = QRectF(center.x() - width / 2, center.y() - height / 2, width, height)
        self.crop_box = box
        self.viewport().update()
        self.cropChanged.emit()
        return True

    def move_crop(self, key: Qt.Key, fraction: float = .005) -> bool:
        """Переносит рамку, удерживая её целиком внутри исходной фотографии."""
        if self.image.isNull():
            return False
        dx = self.image.width() * fraction if key in (Qt.Key.Key_Left, Qt.Key.Key_Right) else 0
        dy = self.image.height() * fraction if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) else 0
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            dx, dy = -dx, -dy
        if not dx and not dy:
            return False
        box = QRectF(self.crop_box)
        box.translate(dx, dy)
        bounds = QRectF(self.image.rect())
        box.translate(max(0, bounds.left() - box.left()) + min(0, bounds.right() - box.right()),
                      max(0, bounds.top() - box.top()) + min(0, bounds.bottom() - box.bottom()))
        self.crop_box = box
        self._update_crop_canvas()
        self.viewport().update()
        self.cropChanged.emit()
        return True

    def has_crop(self) -> bool:
        """Отличает фактическую обрезку от исходной рамки с учётом дробных координат."""
        return not self.crop_box.toAlignedRect().contains(self.image.rect()) or not self.image.rect().contains(self.crop_box.toAlignedRect())

    def clear_mask(self) -> None:
        """Удаляет только выделение, не обработанные пиксели."""
        self.strokes.clear()
        self._paths.clear()
        self.set_mask_pending(False)
        self._drawing = False
        self.viewport().update()
        self.maskChanged.emit()

    def set_mask_pending(self, pending: bool) -> None:
        """Мерцает принятой маской, пока фоновая задача ещё не вернула результат."""
        self._mask_pending = pending
        self._mask_pulse_phase = 0.0
        if pending:
            self._mask_pulse_timer.start()
        else:
            self._mask_pulse_timer.stop()
        self.viewport().update()

    def _pulse_mask(self) -> None:
        """Перерисовывает только оверлей маски: таймер не меняет данные выделения."""
        self._mask_pulse_phase = (self._mask_pulse_phase + .11) % (math.tau)
        self.viewport().update()

    def undo_stroke(self) -> None:
        """Убирает последний штрих до запуска обработки."""
        if self.strokes:
            self.strokes.pop()
            self._paths.pop()
            self._drawing = False
            self.viewport().update()
            self.maskChanged.emit()

    def _fit_scale(self) -> float:
        bounds = self.sceneRect()
        return min(max(1, self.viewport().width()-2)/max(1, bounds.width()),
                   max(1, self.viewport().height()-2)/max(1, bounds.height()))

    def fit(self) -> None:
        """Вписанный масштаб — нижняя граница приближения."""
        self._zoom = 1
        self.resetTransform()
        self.scale(self._fit_scale(), self._fit_scale())
        self.centerOn(self.sceneRect().center())

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
        self.resized.emit()

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
            elif self.crop_active:
                point = self.mapToScene(event.position().toPoint())
                self._crop_resize_handle = self._crop_handle_at(point)
                self._crop_drag = self._crop_resize_handle is None and self.crop_box.contains(point)
                self._crop_drag_start = point
                self._crop_drag_box = QRectF(self.crop_box)
                self._update_cursor(point)
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
        elif self._crop_resize_handle:
            self._resize_crop(self.mapToScene(event.position().toPoint()))
        elif self._crop_drag:
            point = self.mapToScene(event.position().toPoint())
            self.crop_box = QRectF(self._crop_drag_box)
            delta = point - self._crop_drag_start
            bounds = QRectF(self.image.rect())
            self.crop_box.translate(delta)
            self.crop_box.translate(max(0, bounds.left() - self.crop_box.left()) + min(0, bounds.right() - self.crop_box.right()),
                                    max(0, bounds.top() - self.crop_box.top()) + min(0, bounds.bottom() - self.crop_box.bottom()))
            self._update_crop_canvas()
            self.cropChanged.emit()
        elif self._drawing and self.editable:
            point = self.mapToScene(event.position().toPoint())
            self.strokes[-1]["points"].append([point.x(), point.y()])
            self._paths[-1][0].lineTo(point)
        self._update_cursor(self.mapToScene(event.position().toPoint()))
        self.viewport().update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drawing = self._panning = self._crop_drag = False
        self._crop_resize_handle = None
        self._update_cursor(self.mapToScene(event.position().toPoint()))
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key, modifiers = event.key(), event.modifiers()
        plain = not modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
        less = key in (Qt.Key.Key_Comma, Qt.Key.Key_Less) or event.text() in (",", "б")
        greater = key in (Qt.Key.Key_Period, Qt.Key.Key_Greater) or event.text() in (".", "ю")
        if modifiers == Qt.KeyboardModifier.ControlModifier and less:
            self.editorShortcut.emit("rotate_left_90")
        elif modifiers == Qt.KeyboardModifier.ControlModifier and greater:
            self.editorShortcut.emit("rotate_right_90")
        elif plain and key == Qt.Key.Key_C:
            self.editorShortcut.emit("crop")
        elif plain and less and not modifiers & Qt.KeyboardModifier.ShiftModifier:
            self.editorShortcut.emit("rotate_left")
        elif plain and greater and not modifiers & Qt.KeyboardModifier.ShiftModifier:
            self.editorShortcut.emit("rotate_right")
        elif plain and key == Qt.Key.Key_R:
            self.editorShortcut.emit("reset")
        elif key == Qt.Key.Key_Space:
            self._space = True
            self._drawing = False
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        elif event.key() in (Qt.Key.Key_BracketLeft, Qt.Key.Key_BracketRight) or event.text() in ("[", "]", "х", "ъ"):
            smaller = event.key() == Qt.Key.Key_BracketLeft or event.text() in ("[", "х")
            self.diameter = min(500, max(3, self.diameter*(1/1.2 if smaller else 1.2)))
            self.viewport().update()
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
        if self.crop_active and not self.image.isNull():
            painter.save()
            painter.setBrush(QColor(0, 0, 0, 115))
            painter.setPen(Qt.PenStyle.NoPen)
            outer = QPainterPath()
            outer.addRect(self.sceneRect())
            inner = QPainterPath()
            inner.addRect(self.crop_box)
            painter.drawPath(outer.subtracted(inner))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            scale = max(.001, self.transform().m11())
            pen = QPen(QColor("#ffffff"), max(1.0, 1.25 / scale), Qt.PenStyle.DashLine)
            pen.setDashPattern([5 / scale, 4 / scale])
            painter.setPen(pen)
            painter.drawRect(self.crop_box)
            handle = 8 / scale
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#ffffff"))
            for marker in (self.crop_box.topLeft(), self.crop_box.topRight(),
                           self.crop_box.bottomLeft(), self.crop_box.bottomRight(),
                           QPointF(self.crop_box.center().x(), self.crop_box.top()),
                           QPointF(self.crop_box.center().x(), self.crop_box.bottom()),
                           QPointF(self.crop_box.left(), self.crop_box.center().y()),
                           QPointF(self.crop_box.right(), self.crop_box.center().y())):
                painter.drawRect(QRectF(marker.x() - handle / 2, marker.y() - handle / 2, handle, handle))
            painter.restore()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(self.sceneRect())
        for path, colour, width in self._paths:
            tint = QColor(colour)
            if self._mask_pending:
                wave = (math.sin(self._mask_pulse_phase) + 1) / 2
                hue = tint.hsvHue() if tint.hsvHue() >= 0 else 200
                tint.setHsv((hue + round(28 * wave)) % 360, max(150, tint.hsvSaturation()),
                            min(255, tint.value() + round(24 * wave)), round(72 + 98 * wave))
            else:
                tint.setAlpha(115)
            painter.setPen(QPen(tint, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)
        painter.restore()
        if self._space or not self.editable or self.crop_active:
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
    """Координирует пакетный редактор и процесс без ONNX и записи в UI.

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
        # LaMa загружается заранее, хотя отдельный режим удаления в этом окне скрыт.
        self._models = {"inpaint": "pending", "horizon": "pending"}
        self._model_errors: dict[str, str] = {}
        quality = str(self.settings.value("inpaint/quality", "sd"))
        self._inpaint_quality = quality if quality in {"sd", "hd"} else "sd"
        self._loading = False
        self._processing = False
        self._processing_tool: str | None = None
        self._background_inpaint: dict[str, int] = {}
        self._displayed_path = None
        self._closing = False
        self._closed = False
        self._after_save = None
        self._autosave_path: str | None = None
        self._rotation_angles: dict[str, float] = {}
        self._crop_vertical = False
        self._history_available: dict[str, tuple[bool, bool]] = {}
        self._buffer = bytearray()
        self._header = None
        self._stderr = b""
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(_AUTOSAVE_DEBOUNCE_MS)
        self._autosave_timer.timeout.connect(self._flush_autosave)
        self.setObjectName("inpaintDialog")
        self.setWindowTitle(_("Пакетный редактор"))
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowMaximizeButtonHint | Qt.WindowType.WindowCloseButtonHint)
        self.resize(1400, 900)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        panel = QFrame(self)
        panel.setObjectName("batchRetouchPanel")
        bar = QHBoxLayout(panel)
        bar.setContentsMargins(6, 5, 6, 5)
        bar.setSpacing(6)
        root.addWidget(panel)
        navigation = QFrame(panel)
        navigation.setObjectName("inpaintToolbarGroup")
        navigation_row = QHBoxLayout(navigation)
        navigation_row.setContentsMargins(2, 2, 2, 2)
        navigation_row.setSpacing(1)
        self.previous = self._icon_button("chevron-left", _("Предыдущее фото"), lambda: self.navigate(-1))
        self.next = self._icon_button("chevron-right", _("Следующее фото"), lambda: self.navigate(1))
        navigation_row.addWidget(self.previous)
        navigation_row.addWidget(self.next)
        bar.addWidget(navigation)
        information_panel = QWidget(panel)
        information_panel.setObjectName("inpaintInformation")
        information_row = QHBoxLayout(information_panel)
        information_row.setContentsMargins(0, 0, 0, 0)
        information_row.setSpacing(6)
        information = QVBoxLayout()
        information.setSpacing(2)
        self.counter = QLabel()
        self.counter.setObjectName("batchRetouchSliderLabel")
        self.counter.setMaximumWidth(320)
        information.addWidget(self.counter)
        self.status = QLabel()
        self.status.setObjectName("batchResizeStatus")
        information.addWidget(self.status)
        information_row.addLayout(information)
        self.spinner = _BusyIndicator()
        information_row.addWidget(self.spinner)
        information_row.addStretch()
        bar.addWidget(information_panel, 1)
        self.download_progress = QProgressBar()
        self.download_progress.setObjectName("batchProgress")
        self.download_progress.setFixedWidth(150)
        self.download_progress.setTextVisible(False)
        self.download_progress.hide()
        bar.addWidget(self.download_progress)
        self.autosave = SettingsCheckBox(_("Автосохранение"))
        self.autosave.setObjectName("batchResizeOption")
        self.autosave.setChecked(self.settings.value("inpaint/autosave", True, bool))
        self.autosave.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bar.addWidget(self.autosave)
        history = self._toolbar_group(panel)
        self.undo_button = self._toolbar_button("undo", _("Назад (Ctrl+Z)"))
        self.redo_button = self._toolbar_button("redo", _("Вперёд (Ctrl+Y)"))
        self.reset_button = self._toolbar_button("sync", _("Сбросить (R)"))
        for button in (self.undo_button, self.redo_button, self.reset_button):
            history.layout().addWidget(button)
        bar.addWidget(history)
        editing = self._toolbar_group(panel)
        self.clear_button = self._toolbar_button("close", _("Очистить маску"))
        self.apply_button = self._toolbar_button("magic", _("Удалить объекты (Enter)"), primary=True)
        self.crop_toggle = self._toolbar_button("crop", _("Кадрирование (C)"))
        self.crop_toggle.setCheckable(True)
        self.horizon_button = self._toolbar_button("ruler-horizontal", _("Автогоризонт"))
        for button in (self.clear_button, self.apply_button, self.crop_toggle, self.horizon_button):
            editing.layout().addWidget(button)
        bar.addWidget(editing)
        quality = self._toolbar_group(panel)
        self.quality_group = QButtonGroup(self)
        self.quality_group.setExclusive(True)
        self.sd_quality_button = self._quality_button(
            "SD", _("Стандартное качество — LaMa 512×512"), "sd", quality,
        )
        self.hd_quality_button = self._quality_button(
            "HD", _("Высокое качество — LaMa 1024×1024"), "hd", quality,
        )
        for button in (self.sd_quality_button, self.hd_quality_button):
            quality.layout().addWidget(button)
            self.quality_group.addButton(button)
        (self.hd_quality_button if self._inpaint_quality == "hd" else self.sd_quality_button).setChecked(True)
        self.quality_group.buttonClicked.connect(self._quality_changed)
        bar.addWidget(quality)
        self.save_group = self._toolbar_group(panel)
        self.save_button = self._toolbar_button("save", _("Сохранить"), primary=True)
        self.save_group.layout().addWidget(self.save_button)
        bar.addWidget(self.save_group)
        self.help_button = self._toolbar_button("help", _("Справка по горячим клавишам"))
        self.help_button.clicked.connect(lambda: InpaintHelpDialog(self).exec())
        bar.addWidget(self.help_button)
        self.view = InpaintView(self)
        root.addWidget(self.view, 1)
        self.crop_controls = QFrame(self.view.viewport())
        self.crop_controls.setObjectName("batchRetouchOverlay")
        crop_row = QHBoxLayout(self.crop_controls)
        crop_row.setContentsMargins(4, 4, 4, 4)
        crop_row.setSpacing(3)
        self.crop_ratio = QComboBox(self.crop_controls)
        for text, ratio in ((_('Исходный'), None), ("16:9", 16 / 9), ("3:2", 3 / 2), ("4:3", 4 / 3), ("1:1", 1.0)):
            self.crop_ratio.addItem(text, ratio)
        self.crop_ratio.setObjectName("inpaintCropRatio")
        self.crop_ratio.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.crop_ratio.setToolTip(_("Соотношение:"))
        self.crop_ratio.currentIndexChanged.connect(
            lambda index: self._set_crop_ratio(self.crop_ratio.itemData(index))
        )
        crop_row.addWidget(self.crop_ratio)
        self.crop_lock_button = self._toolbar_button("lock", _("Сохранять пропорции"), parent=self.crop_controls)
        self.crop_lock_button.setCheckable(True)
        self.crop_lock_button.setChecked(True)
        self.crop_lock_button.toggled.connect(self._set_crop_lock)
        crop_row.addWidget(self.crop_lock_button)
        self.crop_vertical_button = QToolButton(self.crop_controls)
        self.crop_vertical_button.setObjectName("inpaintToolbarButton")
        self.crop_vertical_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.crop_vertical_button.clicked.connect(self._toggle_crop_vertical)
        crop_row.addWidget(self.crop_vertical_button)
        self._refresh_crop_orientation_button()
        self.crop_reset_button = self._toolbar_button("sync", _("Сбросить (R)"), parent=self.crop_controls)
        self.crop_reset_button.clicked.connect(self._reset_crop)
        crop_row.addWidget(self.crop_reset_button)
        self.crop_apply_button = self._toolbar_button("check", _("Применить кадрирование"), primary=True, parent=self.crop_controls)
        self.crop_apply_button.clicked.connect(self.apply_crop)
        crop_row.addWidget(self.crop_apply_button)
        self.crop_controls.hide()
        self.view.resized.connect(self._position_crop_controls)
        self.view.maskChanged.connect(self._mask_changed)
        self.view.cropChanged.connect(self._update)
        self.clear_button.clicked.connect(self.view.clear_mask)
        self.apply_button.clicked.connect(self.apply)
        self.horizon_button.clicked.connect(self.straighten)
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        self.reset_button.clicked.connect(self.reset_current)
        self.save_button.clicked.connect(self.save)
        self.autosave.toggled.connect(self._autosave_changed)
        self.crop_toggle.toggled.connect(self._toggle_crop)
        self.view.editorShortcut.connect(self._editor_shortcut)
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
        for shortcut, callback in ((QKeySequence("Ctrl+Z"), self.undo), (QKeySequence("Ctrl+Y"), self.redo)):
            action = QAction(self)
            action.setShortcut(shortcut)
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(callback)
            self.addAction(action)
        for shortcut, callback in (
            (QKeySequence(Qt.Key.Key_C), lambda: self.crop_toggle.setChecked(not self.crop_toggle.isChecked())),
            (QKeySequence(Qt.Key.Key_H), self.straighten),
            (QKeySequence(Qt.Key.Key_Comma), lambda: self.rotate(-_ROTATE_STEP_DEGREES)),
            (QKeySequence(Qt.Key.Key_Period), lambda: self.rotate(_ROTATE_STEP_DEGREES)),
            (QKeySequence("Ctrl+,"), lambda: self.rotate(-_ROTATE_QUARTER_TURN_DEGREES)),
            (QKeySequence("Ctrl+."), lambda: self.rotate(_ROTATE_QUARTER_TURN_DEGREES)),
            (QKeySequence(Qt.Key.Key_R), self.reset_current),
        ):
            action = QAction(self)
            action.setShortcut(shortcut)
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(callback)
            self.addAction(action)
        self._update()
        QTimer.singleShot(0, self._start)

    @property
    def path(self) -> str:
        return self.paths[self.index]

    def _toolbar_group(self, parent: QWidget) -> QFrame:
        """Создаёт компактную рамку для близких по смыслу команд верхней панели."""
        group = QFrame(parent)
        group.setObjectName("inpaintToolbarGroup")
        layout = QHBoxLayout(group)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(1)
        return group

    def _toolbar_button(
        self, icon: str, text: str, *, primary: bool = False, parent: QWidget | None = None,
    ) -> QToolButton:
        """Возвращает доступную кнопку-иконку, не раздувающую панель подписью."""
        button = QToolButton(parent or self)
        button.setObjectName("inpaintToolbarPrimaryButton" if primary else "inpaintToolbarButton")
        button.setIcon(_fomantic_icon(icon, 20, "#ffffff" if primary else "#dfe6ef"))
        button.setIconSize(QSize(20, 20))
        button.setToolTip(text)
        button.setAccessibleName(text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _quality_button(self, text: str, tooltip: str, quality: str, parent: QWidget) -> QToolButton:
        """Создаёт текстовый сегмент выбора качества без отдельной настройки."""
        button = QToolButton(parent)
        button.setObjectName("inpaintToolbarButton")
        button.setText(text)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setProperty("quality", quality)
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _quality_changed(self, button: QToolButton) -> None:
        """Запрашивает другую LaMa; воркер выгружает старую сессию до загрузки новой."""
        quality = str(button.property("quality"))
        if quality == self._inpaint_quality:
            return
        self._inpaint_quality = quality
        self.settings.setValue("inpaint/quality", quality)
        self._models["inpaint"] = "pending"
        self._model_errors.pop("inpaint", None)
        self._downloaded = 0
        self._download_total = None
        self._send("set_inpaint_quality", quality=quality)
        self._update()

    def _icon_button(self, icon, text, callback) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("inpaintToolbarButton")
        button.setIcon(_fomantic_icon(icon, 20))
        button.setIconSize(QSize(20, 20))
        button.setToolTip(text)
        button.setAccessibleName(text)
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

    def _toggle_crop(self, checked: bool) -> None:
        """Показывает рамку только в явном режиме, не отменяя уже набранную обрезку."""
        self.view.crop_active = checked
        self.crop_controls.setVisible(checked)
        if checked:
            self._position_crop_controls()
        self.view._update_cursor()
        self.view.viewport().update()

    def _editor_shortcut(self, command: str) -> None:
        """Принимает клавиши холста напрямую, чтобы раскладка не съедала C, < и >."""
        if command == "crop":
            self.crop_toggle.setChecked(not self.crop_toggle.isChecked())
        elif command == "rotate_left":
            self.rotate(-_ROTATE_STEP_DEGREES)
        elif command == "rotate_right":
            self.rotate(_ROTATE_STEP_DEGREES)
        elif command == "rotate_left_90":
            self.rotate(-_ROTATE_QUARTER_TURN_DEGREES)
        elif command == "rotate_right_90":
            self.rotate(_ROTATE_QUARTER_TURN_DEGREES)
        elif command == "reset":
            self.reset_current()

    def _set_crop_ratio(self, ratio: float | None) -> None:
        """Применяет пропорцию, считая «Исходный» пропорцией самого снимка."""
        vertical = self._crop_vertical
        if ratio is None and not self.view.image.isNull():
            vertical = self.view.image.height() > self.view.image.width()
        elif ratio is not None and not self.view.image.isNull() and not self._crop_vertical:
            vertical = self.view.image.height() > self.view.image.width()
        self._crop_vertical = vertical
        self.view.set_crop_ratio(self._crop_ratio_value(ratio), vertical)
        self._refresh_crop_orientation_button()

    def _crop_ratio_value(self, ratio: float | None) -> float | None:
        """Возвращает длинную сторону исходного кадра для пункта «Исходный»."""
        if ratio is not None or self.view.image.isNull():
            return ratio
        width, height = self.view.image.width(), self.view.image.height()
        return max(width / height, height / width)

    def _toggle_crop_vertical(self) -> None:
        """Меняет направление установленного соотношения сторон одной кнопкой."""
        self._crop_vertical = not self._crop_vertical
        ratio = self._crop_ratio_value(self.crop_ratio.currentData())
        self.view.set_crop_ratio(ratio, self._crop_vertical)
        self._refresh_crop_orientation_button()

    def _set_crop_lock(self, checked: bool) -> None:
        """Разрешает свободный размер рамки только после явного снятия замка."""
        self.view.crop_locked = checked
        self.crop_lock_button.setIcon(_fomantic_icon("lock" if checked else "unlock", 20, "#dfe6ef"))

    def _refresh_crop_orientation_button(self) -> None:
        """Показывает текущую ориентацию той же пиктограммой, что и сетка снимков."""
        self.crop_vertical_button.setIcon(_orientation_icon(self._crop_vertical, 20, "#dfe6ef"))
        self.crop_vertical_button.setIconSize(QSize(20, 20))
        self.crop_vertical_button.setToolTip(
            _("Вертикальная ориентация — нажмите для горизонтальной")
            if self._crop_vertical else _("Горизонтальная ориентация — нажмите для вертикальной")
        )
        self.crop_vertical_button.setAccessibleName(self.crop_vertical_button.toolTip())

    def _reset_crop(self) -> None:
        """Возвращает рамку, пропорцию и ориентацию кадрирования к исходному кадру."""
        self.crop_ratio.setCurrentIndex(0)
        self._set_crop_ratio(None)

    def _position_crop_controls(self) -> None:
        """Держит плашку кадрирования в левом верхнем углу области фотографии."""
        self.crop_controls.adjustSize()
        self.crop_controls.move(8, 8)
        self.crop_controls.raise_()

    def apply_crop(self) -> None:
        """Передаёт полную рамку воркеру; LaMa вызывается лишь для выступающих краёв."""
        if not self.view.editable or not self.view.has_crop():
            return
        self._processing = True
        self._processing_tool = "crop"
        box = self.view.crop_box
        self._send("crop", path=self.path, request=self.request, revision=self.revisions[self.path],
                   box=[box.left(), box.top(), box.right(), box.bottom()],
                   draft_size=[self.view.image.width(), self.view.image.height()],
                   neighbors=self._neighbors())
        self._update()

    def rotate(self, degrees: float) -> None:
        """Меняет абсолютный угол от базового кадра, не пересчитывая прошлый поворот."""
        if not self.view.editable:
            return
        target_angle = self._rotation_angles.get(self.path, 0.0) + degrees
        self._processing = True
        self._processing_tool = "rotate"
        self._send("rotate", path=self.path, request=self.request, revision=self.revisions[self.path],
                   degrees=target_angle, draft_size=[self.view.image.width(), self.view.image.height()],
                   neighbors=self._neighbors())
        self._update()

    def _dirty(self, path: str) -> bool:
        return self.revisions.get(path, 0) > self.saved.get(path, 0)

    def _update(self) -> None:
        active = not self._closing and not self._closed
        inpaint_running_here = self.path in self._background_inpaint
        idle = (active and not self._loading and not self._processing and not inpaint_running_here and not self._after_save
                and self._displayed_path == self.path and self.path in self.revisions)
        inpaint_ready = self._models["inpaint"] == "ready"
        horizon_ready = self._models["horizon"] == "ready"
        self.view.editable = idle and inpaint_ready
        self.previous.setEnabled(active and not self._processing and not self._after_save and self.index > 0)
        self.next.setEnabled(active and not self._processing and not self._after_save and self.index+1 < len(self.paths))
        self.horizon_button.setEnabled(idle and horizon_ready)
        can_undo, can_redo = self._history_available.get(self.path, (False, False))
        self.undo_button.setEnabled(idle and can_undo)
        self.redo_button.setEnabled(idle and can_redo)
        self.reset_button.setEnabled(idle)
        self.apply_button.setEnabled(idle and inpaint_ready and bool(self.view.strokes))
        self.crop_toggle.setEnabled(idle)
        self.crop_apply_button.setEnabled(idle and self.view.has_crop())
        self.crop_vertical_button.setEnabled(idle)
        self.crop_lock_button.setEnabled(idle)
        self.crop_reset_button.setEnabled(idle and self.view.has_crop())
        self.crop_ratio.setEnabled(idle)
        self.clear_button.setEnabled(idle and inpaint_ready and bool(self.view.strokes))
        quality_enabled = (active and self._ready and not self._processing and not self._background_inpaint
                           and self._models["inpaint"] in {"ready", "error"})
        self.sd_quality_button.setEnabled(quality_enabled)
        self.hd_quality_button.setEnabled(quality_enabled)
        saving = any(self.pending.values())
        self.save_group.setVisible(not self.autosave.isChecked())
        self.save_button.setEnabled(idle and self._dirty(self.path) and self.revisions[self.path] not in self.pending.get(self.path, set()))
        self.autosave.setEnabled(not self._closing)
        downloading = any(state == "downloading" for state in self._models.values())
        models_pending = any(state in {"pending", "downloading", "loading"} for state in self._models.values())
        self.download_progress.setVisible(downloading)
        if downloading:
            if self._download_total:
                self.download_progress.setRange(0, self._download_total)
                self.download_progress.setValue(self._downloaded)
            else:
                self.download_progress.setRange(0, 0)
        self.spinner.setVisible(not self._ready or self._loading or self._processing or bool(self._background_inpaint)
                                or saving or self._closing or models_pending)
        self.view._update_cursor()
        if self._closing:
            text = _("Завершение сохранения…") if saving else _("Закрытие…")
        elif not self._ready:
            text = _("Загрузка модели…")
            text = _("Запускаем ИИ-модели…")
        elif self._loading:
            text = _("Загрузка изображения…")
        elif self._processing:
            text = (_("Выравнивание горизонта…") if self._processing_tool == "horizon" else
                    _("Поворот изображения…") if self._processing_tool == "rotate" else
                    _("Кадрирование…") if self._processing_tool == "crop" else
                    _("Удаление объектов…"))
        elif self._background_inpaint:
            text = _("Удаление объектов в фоне…")
        elif downloading and self._download_total:
            done = f"{self._downloaded / 1024 / 1024:.0f} MiB"
            total = f"{self._download_total / 1024 / 1024:.0f} MiB"
            text = _("Загрузка моделей в фоне: {done} из {total}").format(done=done, total=total)
        elif models_pending:
            text = _("Запускаем ИИ-модели…")
        elif self._model_errors:
            text = _("Не удалось загрузить часть моделей")
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
        path = self.path
        self._background_inpaint[path] = self.request
        self.view.set_mask_pending(True)
        self._send("apply", path=path, request=self.request, revision=self.revisions[path],
                   strokes=self.view.strokes, draft_size=[self.view.image.width(), self.view.image.height()],
                   neighbors=self._neighbors())
        self._update()

    def straighten(self) -> None:
        """Просит воркер предложить небольшой поворот, не записывая файл сразу."""
        if not self.view.editable:
            return
        self._processing = True
        self._processing_tool = "horizon"
        self._send("straighten", path=self.path, request=self.request,
                   draft_size=[self.view.image.width(), self.view.image.height()],
                   neighbors=self._neighbors())
        self._update()

    def reset_current(self) -> None:
        """Сбрасывает все правки текущего кадра к его состоянию при открытии."""
        if not self.view.editable:
            return
        self._processing = True
        self._processing_tool = "reset"
        self._send("reset", path=self.path, request=self.request,
                   draft_size=[self.view.image.width(), self.view.image.height()],
                   neighbors=self._neighbors())
        self._update()

    def _history_command(self, command: str) -> None:
        """Передаёт отмену или повтор воркеру, где хранятся точные состояния кадров."""
        if not self.view.editable:
            return
        self._processing = True
        self._processing_tool = command
        self._send(command, path=self.path, request=self.request,
                   draft_size=[self.view.image.width(), self.view.image.height()],
                   neighbors=self._neighbors())
        self._update()

    def undo(self) -> None:
        """Отменяет последнее изменение текущего кадра."""
        self._history_command("undo")

    def redo(self) -> None:
        """Повторяет последнее отменённое изменение текущего кадра."""
        self._history_command("redo")

    def save(self, path: str | None = None) -> None:
        """Сразу отмечает запись, чтобы навигация не ждала ответа процесса."""
        if not self._ready or self._processing or self._closing:
            return
        path = path if isinstance(path, str) else self.path
        if self._autosave_path == path:
            self._autosave_timer.stop()
            self._autosave_path = None
        if not self._dirty(path):
            return
        revision = self.revisions[path]
        pending = self.pending.setdefault(path, set())
        if revision not in pending:
            pending.add(revision)
            self._send("save", path=path, revision=revision)
        self._update()

    def _queue_autosave(self, path: str) -> None:
        """Откладывает фоновую запись, пока пользователь продолжает править кадр."""
        if not self.autosave.isChecked() or not self._dirty(path) or self._closing:
            return
        self._autosave_path = path
        self._autosave_timer.start()
        self._update()

    def _flush_autosave(self) -> None:
        """Сохраняет последнюю ревизию, а не каждое промежуточное нажатие клавиши."""
        path, self._autosave_path = self._autosave_path, None
        if path and self.autosave.isChecked() and self._dirty(path):
            self.save(path)

    def _autosave_changed(self, checked: bool) -> None:
        """Запоминает выбор сразу, чтобы новое окно не меняло стратегию записи."""
        self.settings.setValue("inpaint/autosave", checked)
        if checked and not self._processing:
            self._queue_autosave(self.path)
        elif not checked:
            self._autosave_timer.stop()
            self._autosave_path = None
        self._update()

    def _confirm_leave(self, continuation) -> bool:
        """Маска и незаписанный результат требуют явного решения перед уходом."""
        if self.path in self._background_inpaint:
            # Маска уже принадлежит фоновой задаче и будет очищена при открытии
            # следующего кадра; повторный вопрос здесь означал бы ложный «отказ».
            return True
        dirty = self._dirty(self.path)
        pending_current = self.revisions.get(self.path, 0) in self.pending.get(self.path, set())
        if dirty and not pending_current and self.autosave.isChecked() and self._ready and not self._processing:
            self._autosave_timer.stop()
            self._autosave_path = None
            self._after_save = continuation
            self.save()
            return False
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
        if self._processing or self._closing or self._after_save:
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
        if kind == "model_downloading":
            model = event["model"]
            if model == "inpaint" and event.get("quality", self._inpaint_quality) != self._inpaint_quality:
                return
            self._models[model] = "downloading"
            self._downloaded = event.get("downloaded", 0)
            self._download_total = event.get("total") or None
        elif kind == "model_loading":
            if event["model"] == "inpaint" and event.get("quality", self._inpaint_quality) != self._inpaint_quality:
                return
            self._models[event["model"]] = "loading"
        elif kind == "model_unloading":
            self._models[event["model"]] = "loading"
        elif kind == "model_ready":
            if event["model"] == "inpaint" and event.get("quality", self._inpaint_quality) != self._inpaint_quality:
                return
            self._models[event["model"]] = "ready"
        elif kind == "downloading":
            self._model_phase = "downloading"
            self._downloaded = event.get("downloaded", 0)
            self._download_total = event.get("total") or None
        elif kind == "loading_model":
            self._model_phase = "loading"
        elif kind == "ready":
            self._ready = True
            self._send("set_inpaint_quality", quality=self._inpaint_quality)
            self._open()
        elif kind in {"frame", "result"}:
            background_result = (kind == "result" and self._background_inpaint.get(path) == event["request"])
            if not background_result and (event["request"] != self.request or path != self.path or self._closing):
                return
            self.revisions[path] = event["revision"]
            self.saved[path] = max(self.saved.get(path, 0), event["saved_revision"])
            self._history_available[path] = (bool(event.get("can_undo", False)), bool(event.get("can_redo", False)))
            if "rotation_angle" in event:
                self._rotation_angles[path] = float(event["rotation_angle"])
            if background_result:
                self._background_inpaint.pop(path, None)
            if path == self.path and not self._closing:
                image = QImage(payload, event["width"], event["height"], event["width"]*3, QImage.Format.Format_RGB888).copy()
                self._loading = self._processing = False
                self._processing_tool = None
                self._displayed_path = path
                self.view.show_image(image, reset=kind == "frame")
                self._set_crop_ratio(self.crop_ratio.currentData())
            if kind == "result":
                self._queue_autosave(path)
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
        elif kind == "model_error":
            if (event.get("model", "inpaint") == "inpaint"
                    and event.get("quality", self._inpaint_quality) != self._inpaint_quality):
                return
            self._models[event.get("model", "inpaint")] = "error"
            self._model_errors[event.get("model", "inpaint")] = event.get("error", "")
        elif kind == "error":
            background_error = self._background_inpaint.get(path) == event["request"]
            if background_error:
                self._background_inpaint.pop(path, None)
            if not background_error and (event["request"] != self.request or path != self.path):
                return
            if path == self.path:
                if background_error:
                    self.view.set_mask_pending(False)
                self._loading = self._processing = False
                self._processing_tool = None
                self._show_error(event, _("Не удалось обработать изображение"))
        self._update()

    def _show_error(self, event: dict, title: str) -> None:
        code = event.get("error", "")
        if code == "unsupported_image":
            message = _("Поддерживаются одиночные 8-битные JPEG, PNG, WebP и TIFF. RAW, многокадровые и другие цветовые режимы недоступны.")
        elif code == "external_change":
            message = _("Файл изменён другой программой. Сохранение отменено, чтобы не перезаписать новую версию.")
        elif code.startswith("horizon_angle_out_of_range:"):
            angle = code.partition(":")[2]
            message = _("Модель предлагает поворот на {angle}°. Автогоризонт применяет только небольшие коррекции до 15°.").format(angle=angle)
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
        modifiers = event.modifiers()
        plain = not modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
        less_key = key in (Qt.Key.Key_Comma, Qt.Key.Key_Less) or event.text() in (",", "б")
        greater_key = key in (Qt.Key.Key_Period, Qt.Key.Key_Greater) or event.text() in (".", "ю")
        less = less_key and modifiers == Qt.KeyboardModifier.NoModifier
        greater = greater_key and modifiers == Qt.KeyboardModifier.NoModifier
        if plain and (less or greater):
            self.rotate(-_ROTATE_STEP_DEGREES if less else _ROTATE_STEP_DEGREES)
        elif modifiers == Qt.KeyboardModifier.ControlModifier and (less_key or greater_key):
            self.rotate(-_ROTATE_QUARTER_TURN_DEGREES if less_key else _ROTATE_QUARTER_TURN_DEGREES)
        elif key == Qt.Key.Key_C and modifiers == Qt.KeyboardModifier.NoModifier:
            self.crop_toggle.setChecked(not self.crop_toggle.isChecked())
        elif key == Qt.Key.Key_H and modifiers == Qt.KeyboardModifier.NoModifier:
            self.straighten()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down) and modifiers & Qt.KeyboardModifier.ControlModifier:
            # Qt повторяет keyPress при удержании, поэтому каждый шаг остаётся 1 %
            # оригинала, а удержание даёт ожидаемое плавное кадрирование.
            if self.view.crop_by_edge(key):
                if not self.crop_toggle.isChecked():
                    self.crop_toggle.setChecked(True)
                self._update()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down) and modifiers & Qt.KeyboardModifier.ShiftModifier:
            if self.view.move_crop(key):
                if not self.crop_toggle.isChecked():
                    self.crop_toggle.setChecked(True)
                self._update()
        elif key == Qt.Key.Key_Left:
            self.navigate(-1)
        elif key == Qt.Key.Key_Right:
            self.navigate(1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            (self.apply_crop if self.crop_toggle.isChecked() else self.apply)()
        elif key == Qt.Key.Key_S and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.save()
        elif key == Qt.Key.Key_Escape and self.view.strokes and not self._processing:
            self.view.clear_mask()
        else:
            super().keyPressEvent(event)
            return
        event.accept()
