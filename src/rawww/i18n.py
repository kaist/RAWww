## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Мультиязычность приложения: выбор языка и загрузка каталогов gettext.

Русский — исходный язык интерфейса, поэтому его строки служат ``msgid`` и
хранятся прямо в коде. Переводы лежат в ``locale/<код>/LC_MESSAGES/rawww.mo`` и
подключаются через :func:`activate`.

Модуль активируется до импорта тяжёлых Qt-модулей (см. ``__init__.main``),
чтобы строковые константы уровня модуля успели получить перевод выбранного
языка. Язык применяется по перезапуску приложения, поэтому «заморозка» перевода
на время сессии — ожидаемое поведение, а не ограничение.
"""

from __future__ import annotations

import gettext as _gettext
from pathlib import Path

from .runtime_paths import PORTABLE, data_path, work_path

#: Имя домена gettext и файлов каталога (``rawww.po`` / ``rawww.mo``).
DOMAIN = "rawww"

#: Дублирует ``app.SETTINGS_NAME``: язык читается до импорта ``app``.
SETTINGS_NAME = "ctrlka"

#: Ключ настройки с выбранным языком; ``SYSTEM_LANGUAGE`` — «как в системе».
LANGUAGE_SETTING_KEY = "interface/language"
SYSTEM_LANGUAGE = "system"

#: Поддерживаемые языки: код gettext/QLocale и подпись для меню настроек.
SUPPORTED_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("ru", "Русский"),
    ("en", "English"),
    ("de", "Deutsch"),
    ("zh", "中文"),
)

#: Язык, на который откатываемся, если системный не поддерживается.
FALLBACK_LANGUAGE = "en"

#: Соответствие наших кодов именам каталогов Qt (``qtbase_<name>.qm``).
#: Для китайского Qt использует ``zh_CN``, а не короткий ``zh``.
QT_TRANSLATION_NAMES: dict[str, str] = {
    "ru": "ru",
    "en": "en",
    "de": "de",
    "zh": "zh_CN",
}

# Текущий перевод и его язык. NullTranslations возвращает исходный русский msgid,
# поэтому приложение работоспособно даже без единого каталога.
_current: _gettext.NullTranslations = _gettext.NullTranslations()
_active_language = "ru"


def gettext(message: str) -> str:
    """Возвращает перевод строки для активного языка (или исходную строку)."""
    return _current.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    """Возвращает форму множественного числа для активного языка."""
    return _current.ngettext(singular, plural, n)


def locale_directory() -> Path:
    """Каталог с переводами; в собранной версии лежит в ``data/locale``."""
    return data_path("locale")


def supported_codes() -> frozenset[str]:
    """Множество кодов языков, для которых есть перевод или исходные строки."""
    return frozenset(code for code, _name in SUPPORTED_LANGUAGES)


def _system_language() -> str:
    """Определяет язык системы и сводит его к поддерживаемому или откату."""
    from PySide6.QtCore import QLocale

    name = QLocale.system().name()
    code = name.split("_", 1)[0].lower() if name else ""
    return code if code in supported_codes() else FALLBACK_LANGUAGE


def _settings():
    """Открывает те же настройки, что и приложение, ещё до создания QApplication."""
    from PySide6.QtCore import QSettings

    if PORTABLE:
        settings_path = work_path() / "settings"
        return QSettings(
            str(settings_path / f"{SETTINGS_NAME}.ini"),
            QSettings.Format.IniFormat,
        )
    return QSettings(SETTINGS_NAME, SETTINGS_NAME)


def stored_language() -> str:
    """Возвращает сохранённый выбор языка или ``SYSTEM_LANGUAGE`` по умолчанию."""
    try:
        value = _settings().value(LANGUAGE_SETTING_KEY, SYSTEM_LANGUAGE, str)
    except Exception:
        # Настройки могут быть недоступны на самом раннем старте; язык системы —
        # безопасный вариант, а ошибку чтения не стоит превращать в сбой запуска.
        return SYSTEM_LANGUAGE
    if value == SYSTEM_LANGUAGE or value in supported_codes():
        return value
    return SYSTEM_LANGUAGE


def resolve_language(choice: str) -> str:
    """Сводит выбор (в т.ч. ``system``) к конкретному коду поддерживаемого языка."""
    if choice == SYSTEM_LANGUAGE or choice not in supported_codes():
        return _system_language()
    return choice


def active_language() -> str:
    """Код языка, который сейчас применён к интерфейсу."""
    return _active_language


def qt_translation_name(language: str | None = None) -> str:
    """Имя каталога Qt для языка: ``qtbase_<имя>.qm`` подключает диалоги Qt."""
    code = language if language is not None else _active_language
    return QT_TRANSLATION_NAMES.get(code, code)


def activate(language: str | None = None) -> str:
    """Загружает каталог перевода и делает его активным, возвращает его код.

    Без аргумента язык берётся из настроек. Для русского каталога нет: исходные
    строки уже на русском, поэтому используется ``NullTranslations``.
    """
    global _current, _active_language
    choice = language if language is not None else stored_language()
    resolved = resolve_language(choice)
    _current = _gettext.translation(
        DOMAIN,
        localedir=str(locale_directory()),
        languages=[resolved],
        fallback=True,
    )
    _active_language = resolved
    return resolved
