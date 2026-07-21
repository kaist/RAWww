## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rawww import i18n


class _MemorySettings:
    """Настройки в памяти с сигнатурой ``value``, как у QSettings."""

    def __init__(self, stored: dict[str, str] | None = None) -> None:
        self._stored = stored or {}

    def value(self, key, default=None, type=None):  # noqa: A002 - имитируем QSettings
        return self._stored.get(key, default)


class SupportedLanguagesTest(unittest.TestCase):
    def test_supported_codes_match_declared_languages(self) -> None:
        self.assertEqual(i18n.supported_codes(), {"ru", "en", "de", "zh"})

    def test_russian_is_the_source_language(self) -> None:
        self.assertEqual(i18n.SUPPORTED_LANGUAGES[0][0], "ru")


class SystemLanguageTest(unittest.TestCase):
    def _with_system_name(self, name: str) -> str:
        fake_locale = SimpleNamespace(system=lambda: SimpleNamespace(name=lambda: name))
        with patch("PySide6.QtCore.QLocale", fake_locale):
            return i18n._system_language()

    def test_supported_system_language_is_used(self) -> None:
        self.assertEqual(self._with_system_name("de_DE"), "de")
        self.assertEqual(self._with_system_name("zh_CN"), "zh")
        self.assertEqual(self._with_system_name("ru_RU"), "ru")

    def test_unsupported_system_language_falls_back_to_english(self) -> None:
        self.assertEqual(self._with_system_name("fr_FR"), "en")
        self.assertEqual(self._with_system_name(""), "en")


class StoredLanguageTest(unittest.TestCase):
    def test_explicit_code_is_returned(self) -> None:
        with patch.object(i18n, "_settings", return_value=_MemorySettings({i18n.LANGUAGE_SETTING_KEY: "de"})):
            self.assertEqual(i18n.stored_language(), "de")

    def test_system_preference_is_returned(self) -> None:
        with patch.object(i18n, "_settings", return_value=_MemorySettings()):
            self.assertEqual(i18n.stored_language(), i18n.SYSTEM_LANGUAGE)

    def test_unknown_stored_value_falls_back_to_system(self) -> None:
        with patch.object(i18n, "_settings", return_value=_MemorySettings({i18n.LANGUAGE_SETTING_KEY: "xx"})):
            self.assertEqual(i18n.stored_language(), i18n.SYSTEM_LANGUAGE)

    def test_settings_failure_falls_back_to_system(self) -> None:
        def _boom() -> None:
            raise RuntimeError("settings unavailable")

        with patch.object(i18n, "_settings", side_effect=_boom):
            self.assertEqual(i18n.stored_language(), i18n.SYSTEM_LANGUAGE)


class ResolveLanguageTest(unittest.TestCase):
    def test_explicit_supported_code_is_kept(self) -> None:
        self.assertEqual(i18n.resolve_language("de"), "de")

    def test_system_choice_uses_system_language(self) -> None:
        with patch.object(i18n, "_system_language", return_value="zh"):
            self.assertEqual(i18n.resolve_language(i18n.SYSTEM_LANGUAGE), "zh")

    def test_unsupported_choice_uses_system_language(self) -> None:
        with patch.object(i18n, "_system_language", return_value="en"):
            self.assertEqual(i18n.resolve_language("xx"), "en")


class ActivateTest(unittest.TestCase):
    def tearDown(self) -> None:
        i18n.activate("ru")

    def test_activate_english_translates_ui_string(self) -> None:
        self.assertEqual(i18n.activate("en"), "en")
        self.assertEqual(i18n.gettext("Настройки"), "Settings")

    def test_activate_german_translates_format_template(self) -> None:
        i18n.activate("de")
        self.assertEqual(i18n.gettext("Рейтинг {n}").format(n=3), "Bewertung 3")

    def test_activate_russian_returns_source_string(self) -> None:
        i18n.activate("ru")
        self.assertEqual(i18n.gettext("Настройки"), "Настройки")

    def test_unknown_message_returns_source_string(self) -> None:
        i18n.activate("en")
        self.assertEqual(i18n.gettext("Не переведённая строка"), "Не переведённая строка")

    def test_missing_catalog_falls_back_to_source(self) -> None:
        # Каталога для несуществующего языка нет: fallback=True возвращает msgid.
        with patch.object(i18n, "resolve_language", return_value="xx"):
            self.assertEqual(i18n.activate("xx"), "xx")
            self.assertEqual(i18n.gettext("Настройки"), "Настройки")


class QtTranslationNameTest(unittest.TestCase):
    def test_chinese_maps_to_qt_locale_name(self) -> None:
        self.assertEqual(i18n.qt_translation_name("zh"), "zh_CN")

    def test_other_languages_keep_their_code(self) -> None:
        self.assertEqual(i18n.qt_translation_name("de"), "de")


if __name__ == "__main__":
    unittest.main()
