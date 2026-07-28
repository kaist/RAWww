## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Отдельное окно пакетной ретуши и его лёгкий координатор воркера."""

from __future__ import annotations

import base64
import json
import sys
from time import monotonic
from pathlib import Path

from PySide6.QtCore import QProcess, QRect, QRectF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QGraphicsPixmapItem, QGraphicsScene,
    QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QProgressBar, QScrollArea, QSlider, QToolButton, QVBoxLayout, QWidget,
)

from .i18n import gettext as _
from .theme import _fomantic_icon
from .widgets import SettingsCheckBox

_FIELD_HEIGHT = 40
# Задержка дребезга предпросмотра: ползунок успевает доехать, а воркер не
# считает кадры, которые всё равно устареют.
_PREVIEW_DELAY = 350


class RetouchPreviewView(QGraphicsView):
    """Показывает превью вписанным или пиксель-в-пиксель со шторкой «до/после».

    Обработанный кадр лежит в сцене одним элементом, а оригинал того же размера
    дорисовывается поверх его левой части. Так шторка не меняет ни масштаб, ни
    положение сцены: двигается только линия раздела.
    """

    visibleRegionChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._item = QGraphicsPixmapItem()
        self._scene.addItem(self._item)
        self.setScene(self._scene)
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._fit_mode = True
        self._loading = False
        self._spinner_angle = 0
        self._before = QPixmap()
        self._wipe = .5
        self._dragging_wipe = False
        self._spinner = QTimer(self)
        self._spinner.setInterval(50)
        self._spinner.timeout.connect(self._advance_spinner)

    def has_preview(self) -> bool:
        return not self._item.pixmap().isNull()

    def show_frame(self, after: QPixmap, before: QPixmap, origin: tuple[int, int], full_size: tuple[int, int]) -> None:
        """Меняет содержимое, сохраняя масштаб и центр обзора при 100 %."""
        previous = self._scene.sceneRect()
        bars = (self.horizontalScrollBar().value(), self.verticalScrollBar().value())
        self._before = before
        self._item.setPixmap(after)
        if self._fit_mode:
            # Вписанный режим показывает уменьшенную копию кадра целиком, и её
            # пиксели — это и есть система координат сцены.
            self._item.setOffset(0, 0)
            self._scene.setSceneRect(self._item.boundingRect())
            self.fit()
        else:
            # При 100 % сцена — весь снимок, а обработан только видимый кусок:
            # он ложится на своё настоящее место, поэтому кадр не прыгает.
            self._item.setOffset(*origin)
            self._scene.setSceneRect(0, 0, *full_size)
            if self._scene.sceneRect() == previous:
                # Тот же снимок: возвращаем прокрутку ровно как была, иначе
                # округление центра уводит вид на пиксель при каждом ответе.
                self.horizontalScrollBar().setValue(bars[0])
                self.verticalScrollBar().setValue(bars[1])
        self.viewport().update()

    def set_full_canvas(self, size: tuple[int, int]) -> None:
        self._scene.setSceneRect(0, 0, *size)
        self.centerOn(size[0] / 2, size[1] / 2)

    def set_wipe(self, share: float) -> None:
        """Ставит шторку в долях ширины окна: 0 — только результат, 1 — оригинал."""
        self._wipe = min(1.0, max(0.0, share))
        self.viewport().update()

    def wipe(self) -> float:
        return self._wipe

    def _wipe_x(self) -> int:
        return round(self.viewport().width() * self._wipe)

    def visible_scene_rect(self) -> QRectF:
        return self.mapToScene(self.viewport().rect()).boundingRect()

    def set_loading(self, loading: bool) -> None:
        self._loading = loading
        if loading:
            self._spinner.start()
        else:
            self._spinner.stop()
        self.viewport().update()

    def _advance_spinner(self) -> None:
        self._spinner_angle = (self._spinner_angle + 24) % 360
        self.viewport().update()

    def set_fit_mode(self, enabled: bool) -> None:
        self._fit_mode = enabled
        if enabled:
            self.fit()
        else:
            self.resetTransform()
            self.centerOn(self._item)

    def fit(self) -> None:
        if self.has_preview():
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._fit_mode:
            self.fit()
        else:
            self.visibleRegionChanged.emit()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # Шторка живёт на самом фото, поэтому берёт нажатие раньше панорамы:
        # иначе вместо линии потянется сам кадр.
        if self._wipe_grabbed(event.position().x()):
            self._dragging_wipe = True
            self.setCursor(Qt.CursorShape.SplitHCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging_wipe:
            self.set_wipe(event.position().x() / max(1, self.viewport().width()))
            event.accept()
            return
        if self._wipe_grabbed(event.position().x()):
            self.viewport().setCursor(Qt.CursorShape.SplitHCursor)
        else:
            self.viewport().unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._dragging_wipe:
            self._dragging_wipe = False
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if not self._fit_mode:
            self.visibleRegionChanged.emit()

    def _wipe_grabbed(self, x: float) -> bool:
        return not self._before.isNull() and self.has_preview() and abs(x - self._wipe_x()) <= 10

    def drawForeground(self, painter: QPainter, rect) -> None:  # noqa: N802
        super().drawForeground(painter, rect)
        self._draw_wipe(painter)
        if not self._loading:
            return
        painter.save()
        painter.resetTransform()
        center = self.viewport().rect().center()
        spinner = QRect(center.x() - 18, center.y() - 18, 36, 36)
        pen = QPen(QColor(235, 235, 235), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(pen)
        painter.drawArc(spinner, self._spinner_angle * 16, 250 * 16)
        painter.restore()

    def _draw_wipe(self, painter: QPainter) -> None:
        """Рисует оригинал левее линии и саму линию с ручкой.

        Линия привязана к окну, а не к сцене: её место не едет при зуме и
        панораме, а сам оригинал всё равно ложится пиксель в пиксель на
        обработанный кадр.
        """
        if self._before.isNull() or not self.has_preview():
            return
        split = self._wipe_x()
        height = self.viewport().height()
        if split > 0:
            bounds = self._item.sceneBoundingRect()
            painter.save()
            painter.resetTransform()
            painter.setClipRect(QRect(0, 0, split, height), Qt.ClipOperation.IntersectClip)
            painter.setTransform(self.viewportTransform())
            painter.drawPixmap(bounds, self._before, QRectF(self._before.rect()))
            painter.restore()
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(255, 255, 255, 210), 1))
        painter.drawLine(split, 0, split, height)
        painter.setBrush(QColor(20, 20, 20, 190))
        painter.drawEllipse(QRectF(split - 11, height / 2 - 11, 22, 22))
        # Две встречные стрелки читаются как «тяни в любую сторону», а одна
        # полоска на кружке была похожа на кнопку свёртывания.
        painter.setPen(QPen(QColor(255, 255, 255, 230), 2))
        middle = round(height / 2)
        for side in (-1, 1):
            tip = split + side * 7
            painter.drawLine(tip, middle, tip - side * 4, middle)
            painter.drawLine(tip, middle, tip - side * 3, middle - 3)
            painter.drawLine(tip, middle, tip - side * 3, middle + 3)
        painter.restore()


class BatchRetouchDialog(QDialog):
    """Отдельное окно ретуши; UI не импортирует и не хранит ONNX-модели.

    Предпросмотр обслуживает один дочерний процесс, живущий вместе с окном: он
    держит распакованный кадр и маски кожи, поэтому движение ползунка стоит
    только цветовых этапов. Процесс завершается при закрытии окна и на время
    пакета, освобождая ONNX-сессии и их нативную память.
    """

    def __init__(self, paths: list[Path], current: Path, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self._paths = paths
        self._index = paths.index(current) if current in paths else 0
        self._settings = settings
        self._preview_process: QProcess | None = None
        self._batch_process: QProcess | None = None
        self._streams = {"preview": b"", "batch": b""}
        self._pending_previews = 0
        self._batch_running = False
        self._sliders: list[QSlider] = []
        self._before_preview = QPixmap()
        self._after_preview = QPixmap()
        self._batch_started_at = 0.0
        self._source_size: tuple[int, int] | None = None
        self._loaded_region = QRectF()
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(_PREVIEW_DELAY)
        self._preview_timer.timeout.connect(self._start_preview)
        self._region_timer = QTimer(self)
        self._region_timer.setSingleShot(True)
        self._region_timer.setInterval(180)
        self._region_timer.timeout.connect(self._queue_preview)

        self.setObjectName("batchRetouchDialog")
        self.setWindowTitle(_("Пакетная ретушь"))
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowMaximizeButtonHint | Qt.WindowType.WindowCloseButtonHint)
        self.resize(1500, 940)
        self._build_ui()
        self._update_navigation()
        QTimer.singleShot(0, self._queue_preview)

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(16)
        root.addWidget(self._build_panel())
        root.addWidget(self._build_preview(), 1)

    def _build_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("batchRetouchPanel")
        panel.setFixedWidth(330)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        title = QLabel(_("Пакетная ретушь"))
        title.setObjectName("batchRenameTitle")
        layout.addWidget(title)
        hint = QLabel(_("Модели работают в отдельном процессе и выгружаются вместе с окном."))
        hint.setObjectName("batchRenameHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Блоки настроек живут в прокрутке: их становится больше, а папка
        # результата и кнопка запуска обязаны оставаться видны на любом экране.
        stages = QWidget()
        stages.setObjectName("batchRetouchStages")
        inner = QVBoxLayout(stages)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(10)
        scroll = QScrollArea()
        scroll.setObjectName("batchRetouchScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(stages)
        layout.addWidget(scroll, 1)

        # Панель разбита на блоки в том же порядке, в каком идут этапы
        # обработки: сначала кожа, потом цвет всего кадра, последней — таблица.
        skin = self._group(inner, _("РЕТУШЬ КОЖИ"))
        self.tone = self._slider(skin, _("Выравнивание тона"), "retouch/tone_strength", 50)
        self.matte = self._slider(skin, _("Матирование"), "retouch/matte_strength", 0)
        self.dodge = self._slider(skin, _("Dodge & Burn"), "retouch/dodge_burn", 0)
        # Отдельный выключатель нейроретуши не нужен: ноль на ползунке и есть
        # выключенный этап, а воркер тогда не считает его вовсе.
        self.neural_strength = self._slider(skin, _("Нейроретушь"), "retouch/neural_strength", 50)

        colour = self._group(inner, _("ЦВЕТ"))
        self.brightness = self._slider(colour, _("Яркость"), "retouch/brightness", 0, minimum=-100)
        self.contrast = self._slider(colour, _("Контраст"), "retouch/contrast", 0, minimum=-100)
        self.saturation = self._slider(colour, _("Насыщенность"), "retouch/saturation", 0, minimum=-100)

        table = self._group(inner, _("ТАБЛИЦА LUT"))
        self.lut_enabled = SettingsCheckBox(_("Наложить таблицу .cube"))
        self.lut_enabled.setObjectName("batchResizeOption")
        self.lut_enabled.setChecked(self._settings.value("retouch/lut_enabled", False, bool))
        table.addWidget(self.lut_enabled)
        lut_row = QHBoxLayout()
        lut_row.setSpacing(8)
        self.lut_path = QLineEdit(self._settings.value("retouch/lut_path", "", str))
        self.lut_path.setObjectName("batchRetouchOutput")
        self.lut_path.setReadOnly(True)
        self.lut_path.setPlaceholderText(_("Файл не выбран"))
        self.lut_path.setFixedHeight(_FIELD_HEIGHT)
        self.lut_path.setCursorPosition(0)
        lut_row.addWidget(self.lut_path, 1)
        choose_lut = QToolButton()
        choose_lut.setObjectName("batchRetouchBrowse")
        choose_lut.setFixedSize(_FIELD_HEIGHT + 10, _FIELD_HEIGHT)
        choose_lut.setIcon(_fomantic_icon("folder", 18))
        choose_lut.setToolTip(_("Выбрать таблицу .cube"))
        choose_lut.clicked.connect(self._choose_lut)
        lut_row.addWidget(choose_lut)
        table.addLayout(lut_row)
        self.lut_strength = self._slider(table, _("Сила таблицы"), "retouch/lut_strength", 100)
        for widget in (self.lut_path, choose_lut, self.lut_strength):
            widget.setEnabled(self.lut_enabled.isChecked())
            self.lut_enabled.toggled.connect(widget.setEnabled)
        self.lut_enabled.toggled.connect(lambda checked: self._settings.setValue("retouch/lut_enabled", checked))
        self.lut_enabled.toggled.connect(self._queue_exact_preview)
        inner.addStretch(1)

        destination = QLabel(_("СОХРАНИТЬ В ПАПКУ"))
        destination.setObjectName("batchRetouchSectionLabel")
        layout.addWidget(destination)
        output_row = QHBoxLayout()
        output_row.setSpacing(8)
        self.output = QLineEdit(str(self._paths[0].parent / "retouched"))
        self.output.setObjectName("batchRetouchOutput")
        self.output.setReadOnly(True)
        self.output.setCursorPosition(0)
        # Поле и кнопка стоят в одну линию, а разные рамки и отступы стиля
        # легко дают разную высоту — задаём её явно для обоих.
        self.output.setFixedHeight(_FIELD_HEIGHT)
        output_row.addWidget(self.output, 1)
        browse = QToolButton()
        browse.setObjectName("batchRetouchBrowse")
        browse.setFixedSize(_FIELD_HEIGHT + 10, _FIELD_HEIGHT)
        browse.setIcon(_fomantic_icon("folder", 18))
        browse.setToolTip(_("Выбрать папку"))
        browse.clicked.connect(self._choose_output)
        output_row.addWidget(browse)
        layout.addLayout(output_row)

        self.status = QLabel()
        self.status.setObjectName("batchResizeStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setObjectName("batchResizeProgress")
        self.progress.hide()
        layout.addWidget(self.progress)
        self.batch = QPushButton(_("Обработать {count} фото").format(count=len(self._paths)))
        self.batch.setObjectName("batchResizePrimaryButton")
        self.batch.clicked.connect(self._start_batch)
        layout.addWidget(self.batch)
        close = QPushButton(_("Закрыть"))
        close.setObjectName("batchResizeSecondaryButton")
        close.clicked.connect(self.reject)
        layout.addWidget(close)
        return panel

    def _build_preview(self) -> QWidget:
        area = QWidget()
        layout = QVBoxLayout(area)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.preview = RetouchPreviewView()
        self.preview.visibleRegionChanged.connect(self._schedule_visible_region)
        layout.addWidget(self.preview, 1)

        # Навигация и масштаб живут поверх снимка: подпись с номером кадра не
        # нужна, а кнопки снаружи только отбирали высоту у самого фото.
        overlay = QFrame(self.preview.viewport())
        overlay.setObjectName("batchRetouchOverlay")
        overlay_row = QHBoxLayout(overlay)
        overlay_row.setContentsMargins(6, 6, 6, 6)
        overlay_row.setSpacing(6)
        self.previous = self._overlay_button("chevron-left", _("Предыдущая"))
        self.previous.clicked.connect(lambda: self._move(-1))
        self.next = self._overlay_button("chevron-right", _("Следующая"))
        self.next.clicked.connect(lambda: self._move(1))
        self.zoom_button = self._overlay_button("zoom", _("Пиксель в пиксель"))
        self.zoom_button.clicked.connect(lambda: self._set_preview_mode(not self._fit))
        for button in (self.previous, self.next, self.zoom_button):
            overlay_row.addWidget(button)
        overlay.adjustSize()
        overlay.move(12, 12)
        self._fit = True
        self.preview.setToolTip(_("Шторка сравнения: слева оригинал, справа результат"))
        return area

    @staticmethod
    def _overlay_button(icon: str, hint: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("batchRetouchOverlayButton")
        button.setIcon(_fomantic_icon(icon, 20))
        button.setIconSize(QSize(20, 20))
        button.setFixedSize(28, 28)
        button.setToolTip(hint)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _group(self, layout: QVBoxLayout, title: str) -> QVBoxLayout:
        """Добавляет блок настроек и отдаёт его внутреннюю раскладку."""
        group = QFrame()
        group.setObjectName("batchRetouchGroup")
        inner = QVBoxLayout(group)
        inner.setContentsMargins(12, 10, 12, 12)
        inner.setSpacing(8)
        caption = QLabel(title)
        caption.setObjectName("batchRetouchSectionLabel")
        inner.addWidget(caption)
        layout.addWidget(group)
        return inner

    def _slider(self, layout: QVBoxLayout, label: str, key: str, default: int, minimum: int = 0) -> QSlider:
        row = QHBoxLayout()
        row.setSpacing(6)
        caption = QLabel(label)
        caption.setObjectName("batchRetouchSliderLabel")
        row.addWidget(caption, 1)
        value = QLabel()
        value.setObjectName("batchRetouchSliderValue")
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(value)
        layout.addLayout(row)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setObjectName("batchRetouchSlider")
        slider.setRange(minimum, 100)
        slider.setValue(self._settings.value(key, default, int))
        sign = "+" if minimum < 0 else ""
        slider.valueChanged.connect(lambda number, text=value: text.setText(f"{number:{sign}} %"))
        slider.valueChanged.connect(lambda _number: self._queue_preview())
        slider.valueChanged.connect(lambda number, name=key: self._settings.setValue(name, number))
        slider.sliderReleased.connect(self._queue_exact_preview)
        slider.valueChanged.emit(slider.value())
        layout.addWidget(slider)
        self._sliders.append(slider)
        return slider

    def _options(self) -> dict:
        values = {
            "tone_strength": self.tone.value() / 100,
            "matte_strength": self.matte.value() / 100,
            "dodge_burn": self.dodge.value() / 100,
            "neural_retouch": self.neural_strength.value() > 0,
            "neural_strength": self.neural_strength.value() / 100,
            "brightness": self.brightness.value() / 100,
            "contrast": self.contrast.value() / 100,
            "saturation": self.saturation.value() / 100,
            "lut_path": self.lut_path.text() if self.lut_enabled.isChecked() else "",
            "lut_strength": self.lut_strength.value() / 100,
        }
        return values

    def _set_preview_mode(self, fit: bool) -> None:
        self._fit = fit
        self.zoom_button.setIcon(_fomantic_icon("zoom" if fit else "zoom-out", 20))
        self.zoom_button.setToolTip(_("Пиксель в пиксель") if fit else _("Вписать в окно"))
        self.preview.set_fit_mode(fit)
        if not fit and self._source_size is not None:
            self.preview.set_full_canvas(self._source_size)
        self._loaded_region = QRectF()
        self._queue_preview()

    def _schedule_visible_region(self) -> None:
        if not self._fit and not self._batch_running:
            visible = self.preview.visible_scene_rect()
            if self._loaded_region.isNull() or not self._loaded_region.contains(visible):
                self._region_timer.start()

    def _move(self, delta: int) -> None:
        self._index = max(0, min(len(self._paths) - 1, self._index + delta))
        self._update_navigation()
        self._queue_preview()

    def _update_navigation(self) -> None:
        self.previous.setEnabled(self._index > 0)
        self.next.setEnabled(self._index < len(self._paths) - 1)

    def _choose_lut(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Выбрать таблицу .cube"),
            str(Path(self.lut_path.text()).parent) if self.lut_path.text() else "",
            _("Таблицы LUT (*.cube)"),
        )
        if not path:
            return
        self.lut_path.setText(path)
        self.lut_path.setCursorPosition(0)
        self._settings.setValue("retouch/lut_path", path)
        self._queue_exact_preview()

    def _choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, _("Папка результата"), self.output.text())
        if path:
            self.output.setText(path)
            self.output.setCursorPosition(0)

    def _queue_preview(self) -> None:
        if not self._batch_running:
            self._preview_timer.start(_PREVIEW_DELAY)

    def _queue_exact_preview(self) -> None:
        # Отпущенный ползунок и выбор файла — законченное действие: ждать
        # задержку дребезга нечего. Интервал задаётся явно, иначе короткий
        # запуск остался бы интервалом таймера и для следующих запросов.
        if not self._batch_running:
            self._preview_timer.start(1)

    def _start_preview(self) -> None:
        if self._batch_running:
            return
        if any(slider.isSliderDown() for slider in self._sliders):
            # Пока палец на ползунке, воркеру ничего не отдаём: расчёт
            # всё равно устареет к следующему движению, а отпускание
            # само запросит точный вариант.
            return
        region = None
        if not self._fit and self._source_size is not None:
            visible = self.preview.visible_scene_rect()
            margin = 96
            x = max(0, round(visible.left()) - margin)
            y = max(0, round(visible.top()) - margin)
            right = min(self._source_size[0], round(visible.right()) + margin)
            bottom = min(self._source_size[1], round(visible.bottom()) + margin)
            region = (x, y, max(1, right - x), max(1, bottom - y))
        max_side = None if region is not None else max(1080, max(self.preview.viewport().width(), self.preview.viewport().height()) * 2)
        self.status.clear()
        self.preview.set_loading(True)
        task = {"source": str(self._paths[self._index]), "max_side": max_side}
        if region is not None:
            task["region"] = region
        self._pending_previews += 1
        self._send(self._ensure_preview_process(), {"settings": self._options(), "tasks": [task], "preview": True})

    def _ensure_preview_process(self) -> QProcess:
        """Держит один процесс предпросмотра: он кэширует кадр и маски кожи."""
        if self._preview_process is not None:
            return self._preview_process
        process = self._start_worker(preview=True)
        self._preview_process = process
        return process

    def _start_batch(self) -> None:
        output_text = self.output.text().strip()
        if not output_text:
            self.status.setText(_("Укажите папку результата."))
            return
        output = Path(output_text).expanduser()
        self._batch_running = True
        self._batch_started_at = monotonic()
        self.batch.setEnabled(False)
        self.progress.setRange(0, len(self._paths))
        self.progress.setValue(0)
        self.progress.show()
        self.status.setText(_("Запущена пакетная ретушь…"))
        # Пакет получает всю машину: процесс предпросмотра с его моделями и
        # кэшем кадра на это время закрывается.
        self._stop_preview_process()
        self.preview.set_loading(False)
        self._batch_process = self._start_worker(preview=False)
        self._send(self._batch_process, {"settings": self._options(), "tasks": self._batch_tasks(output), "preview": False}, last=True)

    def _batch_tasks(self, output: Path) -> list[dict]:
        """Подбирает новые JPEG-имена, не затирая RAW+JPEG-пары и старый экспорт."""
        used: set[str] = set()
        tasks = []
        for source in self._paths:
            candidate = output / f"{source.stem}.jpg"
            if candidate.name.casefold() in used or candidate.exists():
                candidate = output / f"{source.stem}_{source.suffix.lstrip('.').lower()}.jpg"
            number = 2
            while candidate.name.casefold() in used or candidate.exists():
                candidate = output / f"{source.stem}_{source.suffix.lstrip('.').lower()}_{number}.jpg"
                number += 1
            used.add(candidate.name.casefold())
            tasks.append({"source": str(source), "target": str(candidate), "max_side": None})
        return tasks

    def _start_worker(self, *, preview: bool) -> QProcess:
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(lambda p=process, is_preview=preview: self._read_events(p, is_preview))
        process.finished.connect(lambda _code, _status, p=process, is_preview=preview: self._finished(p, is_preview))
        process.errorOccurred.connect(lambda _error, p=process: self._worker_start_error(p))
        if getattr(sys, "frozen", False):
            process.start(sys.executable, ["--retouch-worker", "--stdin"])
        else:
            process.start(sys.executable, ["-m", "rawww.retouch_worker", "--stdin"])
        return process

    @staticmethod
    def _send(process: QProcess, job: dict, *, last: bool = False) -> None:
        """Передаёт задачу строкой JSON: воркер читает поток задач по одной."""
        process.write(json.dumps(job, ensure_ascii=False).encode("utf-8") + b"\n")
        if last:
            process.closeWriteChannel()

    def _stop_preview_process(self) -> None:
        process, self._preview_process = self._preview_process, None
        self._pending_previews = 0
        self._streams["preview"] = b""
        if process is not None:
            process.closeWriteChannel()
            if not process.waitForFinished(1500):
                process.kill()
                process.waitForFinished(1000)

    def _worker_start_error(self, process: QProcess) -> None:
        if process is self._preview_process:
            self._preview_process = None
            self._pending_previews = 0
            self.preview.set_loading(False)
        if process is self._preview_process or process is self._batch_process:
            self.status.setText(_("Не удалось запустить воркер ретуши."))

    def _read_events(self, process: QProcess, preview: bool) -> None:
        # Кадр предпросмотра — это сотни килобайт base64, и труба отдаёт его
        # кусками: собираем буфер и разбираем только целые строки.
        key = "preview" if preview else "batch"
        buffer = self._streams[key] + bytes(process.readAllStandardOutput())
        lines = buffer.split(b"\n")
        self._streams[key] = lines.pop()
        for raw in lines:
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            kind = event.get("event")
            if kind == "progress" and not preview:
                self._show_progress(event)
            elif kind == "error":
                self.status.setText(_("Ошибка ретуши: {error}").format(error=event.get("message", "")))
            elif kind == "preview" and preview:
                self._show_preview(event)
            elif kind == "finished" and preview:
                self._pending_previews = max(0, self._pending_previews - 1)
                if not self._pending_previews:
                    self._preview_done()

    def _show_progress(self, event: dict) -> None:
        done, total = int(event["done"]), int(event["total"])
        self.progress.setValue(done)
        elapsed = max(0.001, monotonic() - self._batch_started_at)
        remaining = round(elapsed / done * (total - done)) if done else 0
        suffix = _("≈ {s} с").format(s=remaining) if done < total else _("готово")
        self.progress.setFormat(_("Ретушь: {done}/{total}").format(done=done, total=total) + f" · {suffix}")

    def _show_preview(self, event: dict) -> None:
        pixmap = QPixmap()
        try:
            pixmap.loadFromData(base64.b64decode(event["jpeg"]), "JPG")
        except (KeyError, ValueError):
            return
        if pixmap.isNull():
            return
        self._after_preview = pixmap
        if "before" in event:
            # Оригинал воркер присылает один раз на кадр, дальше он не меняется.
            before = QPixmap()
            try:
                before.loadFromData(base64.b64decode(event["before"]), "JPG")
            except ValueError:
                before = QPixmap()
            if not before.isNull():
                self._before_preview = before
        try:
            origin = tuple(int(value) for value in event["origin"])
            full_size = tuple(int(value) for value in event["full_size"])
        except (KeyError, TypeError, ValueError):
            origin, full_size = (0, 0), (pixmap.width(), pixmap.height())
        self._source_size = full_size
        self.preview.show_frame(self._after_preview, self._before_preview, origin, full_size)
        if not self._fit:
            self._loaded_region = QRectF(origin[0], origin[1], pixmap.width(), pixmap.height())
        if event.get("exact") and not self._preview_timer.isActive():
            # Точный кадр пришёл и новых запросов нет: считать ответы дальше
            # незачем, иначе рассинхрон счётчика оставляет спиннер над готовым
            # предпросмотром.
            self._preview_done()

    def _preview_done(self) -> None:
        self._pending_previews = 0
        self.preview.set_loading(False)
        self.status.setText(_("Предпросмотр готов.") if self.preview.has_preview() else _("Не удалось подготовить предпросмотр."))

    def _finished(self, process: QProcess, preview: bool) -> None:
        if preview:
            if process is self._preview_process:
                self._preview_process = None
                self._pending_previews = 0
                self.preview.set_loading(False)
            return
        if process is not self._batch_process:
            return
        self._batch_process = None
        self._batch_running = False
        self.batch.setEnabled(True)
        self.status.setText(_("Пакетная ретушь завершена.") if process.exitCode() == 0 else _("Пакетная ретушь завершилась с ошибкой."))

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_preview_process()
        if self._batch_process is not None:
            self._batch_process.kill()
            self._batch_process.waitForFinished(1000)
        super().closeEvent(event)
