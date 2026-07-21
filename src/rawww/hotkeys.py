## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Сочетания клавиш и их помощники, общие для настроек и рабочей области."""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QKeySequence

from .i18n import gettext as _


HOTKEY_DEFAULTS: dict[str, tuple[str, str]] = {
    "full_view": (_("Полный просмотр"), "F"),
    "open_in_editor": (_("Открыть в редакторе"), "E"),
    "grid": (_("Сетка"), "G"),
    "strip_collapse": (_("Свернуть нижнюю панель (полный просмотр)"), "Shift+Down"),
    "strip_expand": (_("Развернуть нижнюю панель (полный просмотр)"), "Shift+Up"),
    "refresh": (_("Обновить"), "Ctrl+R"),
    "fullscreen": (_("Полный экран"), "F11"),
    "quick_mark": (_("Быстрая метка"), "M"),
    "comment": (_("Комментарий"), "C"),
    "create_folder": (_("Создать новую папку"), "Ctrl+Shift+N"),
    "quick_copy": (_("Быстрое копирование"), "Shift+C"),
    "quick_move": (_("Быстрое перемещение"), "Shift+M"),
    "card_import": (_("Импорт с карты памяти"), "Ctrl+Shift+I"),
    **{f"rating_{rating}": (_("Рейтинг {n}").format(n=rating) if rating else _("Сбросить рейтинг"), str(rating)) for rating in range(6)},
    **{f"color_{index}": (label, f"Shift+{index}") for index, label in enumerate((_("Сбросить цветовую метку"), _("Красная метка"), _("Жёлтая метка"), _("Зелёная метка"), _("Синяя метка"), _("Фиолетовая метка")))},
}


# Эти сочетания обрабатываются навигацией и не могут быть переназначены.
# Справка показывает их рядом с пользовательскими настройками, чтобы не
# приходилось угадывать поведение полноэкранного просмотра и вкладок.
FIXED_HOTKEYS: tuple[tuple[str, str], ...] = (
    (_("Следующая вкладка"), "Ctrl+Right"),
    (_("Предыдущая вкладка"), "Ctrl+Left"),
    (_("Выйти из полного просмотра"), _("Esc или Enter")),
    (_("Следующая фотография (полный просмотр)"), _("Right или Space")),
    (_("Предыдущая фотография (полный просмотр)"), _("Left или Backspace")),
    (_("Включить или выключить масштаб (полный просмотр)"), "Z"),
    (_("Воспроизведение видео или аудио (полный просмотр)"), "Space"),
    (_("Переключить фокус между папками и сеткой"), "Tab"),
    (_("Открыть выбранную папку"), "Enter"),
    (_("Копировать выбранные файлы"), "Ctrl+C"),
    (_("Вырезать выбранные файлы"), "Ctrl+X"),
    (_("Вставить файлы"), "Ctrl+V"),
    (_("Снять выделение"), "Ctrl+D"),
    (_("Удалить выбранный файл или папку"), "Delete"),
)


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
