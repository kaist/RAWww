## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Переиспользуемые примитивы плавной прокрутки для виджетов приложения.

Здесь собрано всё, что делает прокрутку в интерфейсе плавной: инерционная
модель колеса «скорость + трение», автоскролл у края под курсором, анимация
доскролла к выделению и подмешиваемое поведение для списков/деревьев. Модуль
намеренно зависит только от Qt, чтобы им могли пользоваться и `app.py`, и
`dialogs.py` без кольцевых импортов.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import QAbstractItemView, QScrollArea


class MomentumScroller(QObject):
    """Инерционная прокрутка полосы через модель «скорость + трение».

    Колесо не задаёт новую анимацию на каждый щелчок, а добавляет скорость; таймер
    ~60 Гц плавно интегрирует позицию и гасит её трением. За счёт этого частые
    щелчки складываются в непрерывный разгон и мягкий «докат», а не в серию
    отдельных доводок с рывками между ними. Работает с любой полосой (верт./гориз.):
    при смене цели или после остановки состояние синхронизируется с реальным
    значением полосы, чтобы прокрутка не «прыгала».
    """

    _TICK_MS = 15
    _FRICTION = 0.9          # доля скорости, сохраняемая за тик
    _MIN_VELOCITY = 0.5      # ниже этого докат считаем законченным
    _IMPULSE = 0.13          # доля шага (стороны карточки) в скорость на щелчок
    _MAX_STEPS = 12          # потолок скорости в шагах, чтобы фляк не улетал

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._bar = None
        self._pos = 0.0
        self._velocity = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._tick)

    def flick(self, bar, units: float, step: int) -> None:
        """Добавляет импульс прокрутки: ``units`` — щелчки колеса (вниз > 0)."""
        step = max(1, step)
        if bar is not self._bar or not self._timer.isActive():
            self._bar = bar
            self._pos = float(bar.value())
        self._velocity += units * step * self._IMPULSE
        cap = step * self._MAX_STEPS
        self._velocity = max(-cap, min(cap, self._velocity))
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._velocity = 0.0

    def _tick(self) -> None:
        bar = self._bar
        if bar is None:
            self._timer.stop()
            return
        self._pos += self._velocity
        self._velocity *= self._FRICTION
        low, high = bar.minimum(), bar.maximum()
        value = int(round(self._pos))
        if value <= low:
            value, self._pos, self._velocity = low, float(low), 0.0
            self._timer.stop()
        elif value >= high:
            value, self._pos, self._velocity = high, float(high), 0.0
            self._timer.stop()
        elif abs(self._velocity) < self._MIN_VELOCITY:
            self._timer.stop()
        if value != bar.value():
            bar.setValue(value)
        else:
            self._pos = float(bar.value())


def kinetic_wheel_scroll(scroller: MomentumScroller, bar, event, step: int) -> bool:
    """Отдаёт колесо инерционному скроллеру, возвращает True, если обработал.

    Тачпады шлют ``pixelDelta`` и уже листают плавно попиксельно — их отдаём
    стандартной обработке, иначе точный жест превратился бы в «залипание».
    Модификаторы тоже не трогаем, чтобы не мешать привычным сценариям.
    """
    delta = event.angleDelta().y() or event.angleDelta().x()
    touchpad = not event.pixelDelta().isNull()
    if delta == 0 or touchpad or event.modifiers() != Qt.KeyboardModifier.NoModifier or bar.minimum() == bar.maximum():
        return False
    scroller.flick(bar, -delta / 120.0, step)
    return True


class EdgeAutoScroller(QObject):
    """Плавный автоскролл у края под курсором (перетаскивание файлов, drag-выделение).

    Заменяет ступенчатый автоскролл Qt, который дёргал позицию рывками раз в
    ~50 мс. Пока курсор в «горячей» полосе у края, задаётся целевая скорость (тем
    выше, чем ближе к краю), а таймер ~60 Гц плавно разгоняет и ведёт полосу; при
    уходе курсора из зоны или конце жеста скорость мягко гасится. Работает по
    любой оси через переданную полосу прокрутки, поэтому годится и для сетки, и
    для лент.
    """

    _TICK_MS = 15
    _MARGIN_FRAC = 0.16      # доля видимой стороны, считающаяся горячей зоной
    _MAX_FRAC = 0.045        # макс скорость как доля видимой стороны за тик
    _RAMP = 0.22             # сглаживание разгона/торможения
    _MIN_VELOCITY = 0.35     # ниже этого на затухании останавливаемся

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._bar = None
        self._pos = 0.0
        self._velocity = 0.0
        self._desired = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._tick)

    def update_edge(self, bar, coord: int, size: int) -> None:
        """Пересчитывает целевую скорость по позиции курсора вдоль видимой области."""
        if size <= 0:
            return
        if bar is not self._bar or not self._timer.isActive():
            self._bar = bar
            self._pos = float(bar.value())
        margin = max(8.0, size * self._MARGIN_FRAC)
        max_v = max(6.0, size * self._MAX_FRAC)
        if coord < margin:
            self._desired = -((margin - coord) / margin) * max_v
        elif coord > size - margin:
            self._desired = ((coord - (size - margin)) / margin) * max_v
        else:
            self._desired = 0.0
        if self._desired != 0.0 and not self._timer.isActive():
            self._pos = float(bar.value())
            self._timer.start()

    def release(self) -> None:
        """Курсор ушёл или жест закончился: цель — ноль, плавно тормозим."""
        self._desired = 0.0

    def stop(self) -> None:
        self._timer.stop()
        self._velocity = 0.0
        self._desired = 0.0

    def _tick(self) -> None:
        bar = self._bar
        if bar is None:
            self._timer.stop()
            return
        self._velocity += (self._desired - self._velocity) * self._RAMP
        if self._desired == 0.0 and abs(self._velocity) < self._MIN_VELOCITY:
            self.stop()
            return
        self._pos += self._velocity
        low, high = bar.minimum(), bar.maximum()
        value = int(round(self._pos))
        if value <= low:
            value, self._pos, self._velocity = low, float(low), 0.0
        elif value >= high:
            value, self._pos, self._velocity = high, float(high), 0.0
        if value != bar.value():
            bar.setValue(value)
        else:
            self._pos = float(bar.value())
        if self._desired == 0.0 and (value <= low or value >= high):
            self.stop()


def animate_scroll_to(base_scroll_to: Callable, anim: QPropertyAnimation, bar, index, hint) -> None:
    """Плавно доскролливает вид к элементу вместо мгновенного прыжка Qt.

    Штатный ``scrollTo`` при клавиатурной навигации мгновенно перекидывает полосу,
    чтобы удержать выделение на экране, — отсюда рывок. Даём базовой реализации
    (``base_scroll_to``) вычислить конечную позицию, затем возвращаем полосу на
    старт и анимируем к цели. Если элемент и так виден (позиция не меняется),
    ничего не делаем.
    """
    start = bar.value()
    base_scroll_to(index, hint)
    end = bar.value()
    if end == start:
        return
    bar.setValue(start)
    anim.stop()
    anim.setTargetObject(bar)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.start()


class SmoothScrollMixin:
    """Плавная прокрутка колесом и анимированный доскролл к выделению.

    Подмешивается к вертикальным спискам/деревьям, чтобы дать им ту же плавность,
    что у сетки и лент: колесо — модель «скорость + трение» (`MomentumScroller`),
    а доскролл к выделению при клавиатурной навигации — анимация вместо
    мгновенного прыжка. Подмешивающий класс должен вызвать `_init_smooth_scroll()`
    в своём конструкторе.
    """

    def _init_smooth_scroll(self) -> None:
        # Флаг до setVerticalScrollMode: Qt дёргает scrollTo уже из него.
        self._smooth_scroll_to = False
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._scroller = MomentumScroller(self)
        self._nav_anim = QPropertyAnimation(self)
        self._nav_anim.setPropertyName(b"value")
        self._nav_anim.setDuration(200)
        self._nav_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _wheel_step(self) -> int:
        step = self.sizeHintForRow(0)
        if step <= 0:
            step = max(48, self.viewport().height() // 6)
        return step

    def wheelEvent(self, event) -> None:  # noqa: N802
        if kinetic_wheel_scroll(self._scroller, self.verticalScrollBar(), event, self._wheel_step()):
            event.accept()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        self._smooth_scroll_to = True
        try:
            super().keyPressEvent(event)
        finally:
            self._smooth_scroll_to = False

    def scrollTo(self, index, hint=QAbstractItemView.ScrollHint.EnsureVisible) -> None:  # noqa: N802
        if self._smooth_scroll_to:
            self._scroller.stop()
            animate_scroll_to(super().scrollTo, self._nav_anim, self.verticalScrollBar(), index, hint)
        else:
            super().scrollTo(index, hint)


class _SmoothWheelFilter(QObject):
    """Event-filter: инерционное колесо для готового вида без его переписывания.

    Ставится на viewport существующего списка/таблицы, куда Qt доставляет события
    колеса, и переводит их в модель «скорость + трение». Хранит собственный
    `MomentumScroller`, живущий столько же, сколько сам вид.
    """

    def __init__(self, view: QAbstractItemView) -> None:
        super().__init__(view)
        self._view = view
        self._scroller = MomentumScroller(view)

    def _wheel_step(self) -> int:
        step = self._view.verticalScrollBar().singleStep()
        if step <= 0:
            step = max(48, self._view.viewport().height() // 6)
        return step

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Wheel and kinetic_wheel_scroll(
            self._scroller, self._view.verticalScrollBar(), event, self._wheel_step()
        ):
            return True
        return super().eventFilter(obj, event)


def enable_smooth_wheel(view: QAbstractItemView) -> None:
    """Включает инерционное колесо для существующего прокручиваемого вида.

    Не требует подкласса, поэтому годится для обычных `QListWidget`/`QTableWidget`,
    создаваемых на месте. Переводит вид на попиксельную прокрутку (иначе `setValue`
    двигал бы список поэлементно, и «докат» был бы ступенчатым) и ставит на его
    viewport event-filter с инерционной моделью колеса.
    """
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.viewport().installEventFilter(_SmoothWheelFilter(view))


class SmoothScrollArea(QScrollArea):
    """`QScrollArea` с инерционной прокруткой колесом (модель «скорость + трение»).

    Внешне и по логике — обычная область прокрутки; отличается лишь плавным
    «докатом» вместо ступенчатого шага колеса, как в сетке и списках. Годится для
    длинных вкладок настроек и прочего вертикально прокручиваемого содержимого.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scroller = MomentumScroller(self)

    def _wheel_step(self) -> int:
        step = self.verticalScrollBar().singleStep()
        if step <= 0:
            step = max(48, self.viewport().height() // 6)
        return step

    def wheelEvent(self, event) -> None:  # noqa: N802
        if kinetic_wheel_scroll(self._scroller, self.verticalScrollBar(), event, self._wheel_step()):
            event.accept()
        else:
            super().wheelEvent(event)
