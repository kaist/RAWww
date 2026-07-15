## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Сочетания клавиш и их помощники, общие для настроек и рабочей области."""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QKeySequence


HOTKEY_DEFAULTS: dict[str, tuple[str, str]] = {
    "full_view": ("Полный просмотр", "F"),
    "open_in_editor": ("Открыть в редакторе", "E"),
    "grid": ("Сетка", "G"),
    "strip_collapse": ("Свернуть нижнюю панель (полный просмотр)", "Shift+Down"),
    "strip_expand": ("Развернуть нижнюю панель (полный просмотр)", "Shift+Up"),
    "refresh": ("Обновить", "Ctrl+R"),
    "fullscreen": ("Полный экран", "F11"),
    "quick_mark": ("Быстрая метка", "M"),
    "comment": ("Комментарий", "C"),
    "quick_copy": ("Быстрое копирование", "Shift+C"),
    "quick_move": ("Быстрое перемещение", "Shift+M"),
    **{f"rating_{rating}": (f"Рейтинг {rating}" if rating else "Сбросить рейтинг", str(rating)) for rating in range(6)},
    **{f"color_{index}": (label, f"Shift+{index}") for index, label in enumerate(("Сбросить цветовую метку", "Красная метка", "Жёлтая метка", "Зелёная метка", "Синяя метка", "Фиолетовая метка"))},
}


def _hotkey_sequence(settings: QSettings, identifier: str) -> QKeySequence:
    """Возвращает сохранённое сочетание; пустое значение означает «отключено»."""
    default = HOTKEY_DEFAULTS[identifier][1]
    key = f"hotkeys/{identifier}"
    if not settings.contains(key):
        return QKeySequence(default)
    return QKeySequence(settings.value(key, "", str))


def _uses_reserved_navigation_key(sequence: QKeySequence) -> bool:
    """Не позволяет назначить голые стрелки, Enter и Escape поверх навигации.

    Зарезервированы только клавиши без модификаторов. Сочетания вроде
    ``Ctrl+Up`` остаются доступными: обычному перемещению они не мешают.
    """
    reserved = {int(key) for key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape)}
    return any(sequence[index].toCombined() in reserved for index in range(sequence.count()))
