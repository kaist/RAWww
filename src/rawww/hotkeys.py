"""Keyboard shortcut definitions and helpers, shared by the settings UI and the workspace."""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QKeySequence


HOTKEY_DEFAULTS: dict[str, tuple[str, str]] = {
    "full_view": ("Полный просмотр", "F"),
    "open_in_editor": ("Открыть в редакторе", "E"),
    "grid": ("Сетка", "G"),
    "strip_collapse": ("Свернуть нижнюю панель (полный просмотр)", "Ctrl+Down"),
    "strip_expand": ("Развернуть нижнюю панель (полный просмотр)", "Ctrl+Up"),
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
    """Return a saved shortcut; an explicitly blank value disables it."""
    default = HOTKEY_DEFAULTS[identifier][1]
    key = f"hotkeys/{identifier}"
    if not settings.contains(key):
        return QKeySequence(default)
    return QKeySequence(settings.value(key, "", str))


def _uses_reserved_navigation_key(sequence: QKeySequence) -> bool:
    """Bare arrows, Enter and Escape always retain their navigation behaviour.

    Only the unmodified keys are reserved; combinations such as ``Ctrl+Up``
    stay assignable because they never collide with plain navigation.
    """
    reserved = {int(key) for key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape)}
    return any(sequence[index].toCombined() in reserved for index in range(sequence.count()))
