## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Отдельное окно пакетной ретуши и его лёгкий координатор воркера."""

from __future__ import annotations

import base64
import json
import sys
from time import monotonic
from pathlib import Path

from PySide6.QtCore import QProcess, QRect, QRectF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QGraphicsPixmapItem, QGraphicsScene,
    QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QProgressBar, QSlider, QToolButton, QVBoxLayout, QWidget,
)

from .i18n import gettext as _
from .theme import _fomantic_icon
from .widgets import SettingsCheckBox


class RetouchPreviewView(QGraphicsView):
    """Показывает готовое превью вписанным или пиксель-в-пиксель с панорамой."""

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
        self._spinner = QTimer(self)
        self._spinner.setInterval(50)
        self._spinner.timeout.connect(self._advance_spinner)

    def set_preview(self, pixmap: QPixmap) -> None:
        self._item.setPixmap(pixmap)
        self._item.setOffset(0, 0)
        self._scene.setSceneRect(self._item.boundingRect())
        if self._fit_mode:
            self.fit()

    def set_region_preview(self, pixmap: QPixmap, origin: tuple[int, int], full_size: tuple[int, int]) -> None:
        """Кладёт обработанный фрагмент на его настоящее место в сцене 100 %."""
        self._item.setPixmap(pixmap)
        self._item.setOffset(*origin)
        self._scene.setSceneRect(0, 0, *full_size)

    def set_full_canvas(self, size: tuple[int, int]) -> None:
        self._scene.setSceneRect(0, 0, *size)
        self.centerOn(size[0] / 2, size[1] / 2)

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
        if not self._item.pixmap().isNull():
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._fit_mode:
            self.fit()
        else:
            self.visibleRegionChanged.emit()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        if not self._fit_mode:
            self.visibleRegionChanged.emit()

    def drawForeground(self, painter: QPainter, _rect) -> None:  # noqa: N802
        super().drawForeground(painter, _rect)
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


class BatchRetouchDialog(QDialog):
    """Отдельное окно ретуши; UI не импортирует и не хранит ONNX-модели.

    Каждый preview и весь пакет запускаются одноразовым процессом. После его
    нормального завершения QProcess освобождает системный процесс, а вместе с ним
    ONNX-сессии и выделенную ими память.
    """

    def __init__(self, paths: list[Path], current: Path, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self._paths = paths
        self._index = paths.index(current) if current in paths else 0
        self._settings = settings
        self._process: QProcess | None = None
        self._batch_running = False
        self._before_preview = QPixmap()
        self._after_preview = QPixmap()
        self._show_before = False
        self._batch_started_at = 0.0
        self._source_size: tuple[int, int] | None = None
        self._loaded_region = QRectF()
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(350)
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
        panel = QFrame()
        panel.setObjectName("batchRetouchPanel")
        panel.setFixedWidth(310)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        title = QLabel(_("Пакетная ретушь"))
        title.setObjectName("batchRenameTitle")
        layout.addWidget(title)
        hint = QLabel(_("Модели запускаются в отдельном процессе и выгружаются после каждого предпросмотра и пакета."))
        hint.setObjectName("batchRenameHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.tone = self._slider(layout, _("Выравнивание тона кожи"), "retouch/tone_strength", 50)
        self.dodge = self._slider(layout, _("Dodge & Burn"), "retouch/dodge_burn", 0)
        self.neural = SettingsCheckBox(_("Ретушь кожи"))
        self.neural.setChecked(self._settings.value("retouch/neural_retouch", True, bool))
        layout.addWidget(self.neural)
        self.neural_strength = self._slider(layout, _("Сила ретуши кожи"), "retouch/neural_strength", 50)
        self.neural.toggled.connect(self.neural_strength.setEnabled)
        self.neural.toggled.connect(self._queue_preview)
        self.neural.toggled.connect(lambda checked: self._settings.setValue("retouch/neural_retouch", checked))
        self.neural_strength.setEnabled(self.neural.isChecked())

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel(_("Папка результата")))
        self.output = QLineEdit(str(self._paths[0].parent / "retouched"))
        output_row.addWidget(self.output, 1)
        browse = QToolButton()
        browse.setIcon(_fomantic_icon("folder", 15))
        browse.setToolTip(_("Выбрать папку"))
        browse.clicked.connect(self._choose_output)
        output_row.addWidget(browse)
        layout.addLayout(output_row)
        layout.addStretch(1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        self.batch = QPushButton(_("Обработать {count} фото").format(count=len(self._paths)))
        self.batch.setObjectName("batchResizePrimaryButton")
        self.batch.clicked.connect(self._start_batch)
        layout.addWidget(self.batch)
        close = QPushButton(_("Закрыть"))
        close.clicked.connect(self.reject)
        layout.addWidget(close)
        root.addWidget(panel)

        preview_area = QWidget()
        preview_layout = QVBoxLayout(preview_area)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.previous = QPushButton(_("Предыдущая"))
        self.previous.clicked.connect(lambda: self._move(-1))
        self.next = QPushButton(_("Следующая"))
        self.next.clicked.connect(lambda: self._move(1))
        self.caption = QLabel()
        self.fit_button = QPushButton(_("Вписать"))
        self.fit_button.setCheckable(True)
        self.fit_button.setChecked(True)
        self.full_button = QPushButton("100 %")
        self.full_button.setCheckable(True)
        self.fit_button.clicked.connect(lambda: self._set_preview_mode(True))
        self.full_button.clicked.connect(lambda: self._set_preview_mode(False))
        self.before_button = QPushButton(_("До"))
        self.before_button.setCheckable(True)
        self.before_button.toggled.connect(self._toggle_before)
        toolbar.addWidget(self.previous)
        toolbar.addWidget(self.next)
        toolbar.addWidget(self.caption, 1)
        toolbar.addWidget(self.before_button)
        toolbar.addWidget(self.fit_button)
        toolbar.addWidget(self.full_button)
        preview_layout.addLayout(toolbar)
        self.preview = RetouchPreviewView()
        self.preview.visibleRegionChanged.connect(self._schedule_visible_region)
        preview_layout.addWidget(self.preview, 1)
        root.addWidget(preview_area, 1)

    def _slider(self, layout: QVBoxLayout, label: str, key: str, default: int) -> QSlider:
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        value = QLabel()
        row.addWidget(value)
        layout.addLayout(row)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(self._settings.value(key, default, int))
        slider.valueChanged.connect(lambda number, text=value: text.setText(f"{number} %"))
        slider.valueChanged.connect(lambda _number: self._queue_preview())
        slider.valueChanged.connect(lambda number, name=key: self._settings.setValue(name, number))
        slider.sliderReleased.connect(self._queue_exact_preview)
        slider.valueChanged.emit(slider.value())
        layout.addWidget(slider)
        return slider

    def _options(self) -> dict:
        values = {
            "tone_strength": self.tone.value() / 100,
            "dodge_burn": self.dodge.value() / 100,
            "neural_retouch": self.neural.isChecked(),
            "neural_strength": self.neural_strength.value() / 100,
        }
        for key, value in values.items():
            self._settings.setValue(f"retouch/{key}", value)
        return values

    def _set_preview_mode(self, fit: bool) -> None:
        self.fit_button.setChecked(fit)
        self.full_button.setChecked(not fit)
        self.preview.set_fit_mode(fit)
        if not fit and self._source_size is not None:
            self.preview.set_full_canvas(self._source_size)
        self._loaded_region = QRectF()
        self._queue_preview()

    def _schedule_visible_region(self) -> None:
        if not self.fit_button.isChecked() and not self._batch_running:
            visible = self.preview.visible_scene_rect()
            if self._loaded_region.isNull() or not self._loaded_region.contains(visible):
                self._region_timer.start()

    def _toggle_before(self, checked: bool) -> None:
        self._show_before = checked
        self._show_active_preview()

    def _show_active_preview(self) -> None:
        pixmap = self._before_preview if self._show_before else self._after_preview
        if not pixmap.isNull():
            self.preview.set_preview(pixmap)

    def _move(self, delta: int) -> None:
        self._index = max(0, min(len(self._paths) - 1, self._index + delta))
        self._update_navigation()
        self._queue_preview()

    def _update_navigation(self) -> None:
        self.previous.setEnabled(self._index > 0)
        self.next.setEnabled(self._index < len(self._paths) - 1)
        self.caption.setText(_("{current} из {total}: {name}").format(current=self._index + 1, total=len(self._paths), name=self._paths[self._index].name))

    def _choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, _("Папка результата"), self.output.text())
        if path:
            self.output.setText(path)

    def _queue_preview(self) -> None:
        if not self._batch_running:
            self._preview_timer.start()

    def _queue_exact_preview(self) -> None:
        if not self._batch_running:
            self._preview_timer.start(1)

    def _start_preview(self) -> None:
        if self._batch_running:
            return
        if self._process is not None:
            self._process.kill()
        region = None
        if self.full_button.isChecked() and self._source_size is not None:
            visible = self.preview.visible_scene_rect()
            margin = 96
            x = max(0, round(visible.left()) - margin)
            y = max(0, round(visible.top()) - margin)
            right = min(self._source_size[0], round(visible.right()) + margin)
            bottom = min(self._source_size[1], round(visible.bottom()) + margin)
            region = (x, y, max(1, right - x), max(1, bottom - y))
        max_side = None if region is not None else max(1080, max(self.preview.viewport().width(), self.preview.viewport().height()) * 2)
        fast = any(slider.isSliderDown() for slider in (self.tone, self.dodge, self.neural_strength))
        self.status.clear()
        self.preview.set_loading(True)
        task = {"source": str(self._paths[self._index]), "max_side": max_side}
        if region is not None:
            task["region"] = region
        self._launch([task], preview=True, fast=fast)

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
        tasks = self._batch_tasks(output)
        self._launch(tasks, preview=False)

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

    def _launch(self, tasks: list[dict], *, preview: bool, fast: bool = False) -> None:
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(lambda p=process, is_preview=preview: self._read_events(p, is_preview))
        process.finished.connect(lambda _code, _status, p=process, is_preview=preview: self._finished(p, is_preview))
        options = self._options()
        if fast:
            # Пока палец на ползунке, не тратим секунды на нейроретушь: точный
            # вариант автоматически построится сразу после отпускания.
            options["neural_retouch"] = False
        payload = json.dumps({"settings": options, "tasks": tasks, "preview": preview}, ensure_ascii=False).encode("utf-8")
        process.started.connect(lambda p=process, value=payload: self._send_job(p, value))
        process.errorOccurred.connect(lambda _error: self._worker_start_error(process))
        self._process = process
        if getattr(sys, "frozen", False):
            process.start(sys.executable, ["--retouch-worker", "--stdin"])
        else:
            process.start(sys.executable, ["-m", "rawww.retouch_worker", "--stdin"])

    @staticmethod
    def _send_job(process: QProcess, payload: bytes) -> None:
        """Передаёт задачу потоком, не создавая файла и не останавливая Qt."""
        process.write(payload)
        process.closeWriteChannel()

    def _worker_start_error(self, process: QProcess) -> None:
        if process is self._process:
            self.status.setText(_("Не удалось запустить воркер ретуши."))

    def _read_events(self, process: QProcess, preview: bool) -> None:
        for raw in bytes(process.readAllStandardOutput()).splitlines():
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if event.get("event") == "progress" and not preview:
                done, total = int(event["done"]), int(event["total"])
                self.progress.setValue(done)
                elapsed = max(0.001, monotonic() - self._batch_started_at)
                remaining = round(elapsed / done * (total - done)) if done else 0
                suffix = _("≈ {s} с").format(s=remaining) if done < total else _("готово")
                self.progress.setFormat(_("Ретушь: {done}/{total}").format(done=done, total=total) + f" · {suffix}")
            elif event.get("event") == "error":
                self.status.setText(_("Ошибка ретуши: {error}").format(error=event.get("message", "")))
            elif event.get("event") == "preview" and preview:
                try:
                    pixmap = QPixmap()
                    pixmap.loadFromData(base64.b64decode(event["jpeg"]), "JPG")
                except (KeyError, ValueError):
                    continue
                if not pixmap.isNull():
                    self._after_preview = pixmap
                    try:
                        before = QPixmap()
                        before.loadFromData(base64.b64decode(event["before"]), "JPG")
                        self._before_preview = before
                    except (KeyError, ValueError):
                        pass
                    try:
                        origin = tuple(int(value) for value in event["origin"])
                        full_size = tuple(int(value) for value in event["full_size"])
                    except (KeyError, TypeError, ValueError):
                        origin = (0, 0)
                        full_size = (pixmap.width(), pixmap.height())
                    self._source_size = full_size
                    if self.full_button.isChecked() and full_size != (pixmap.width(), pixmap.height()):
                        active = self._before_preview if self._show_before else self._after_preview
                        self.preview.set_region_preview(active, origin, full_size)
                        self._loaded_region = QRectF(origin[0], origin[1], pixmap.width(), pixmap.height())
                    else:
                        self._show_active_preview()

    def _finished(self, process: QProcess, preview: bool) -> None:
        if process is not self._process:
            return
        self._process = None
        if preview:
            self.preview.set_loading(False)
            if process.exitCode() == 0 and not self.preview._item.pixmap().isNull():
                self.status.setText(_("Предпросмотр готов."))
                return
            self.status.setText(_("Не удалось подготовить предпросмотр."))
            return
        self._batch_running = False
        self.batch.setEnabled(True)
        if process.exitCode() == 0:
            self.status.setText(_("Пакетная ретушь завершена."))
        else:
            self.status.setText(_("Пакетная ретушь завершилась с ошибкой."))

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._process is not None:
            self._process.kill()
            self._process.waitForFinished(1000)
        super().closeEvent(event)
    visibleRegionChanged = Signal()
