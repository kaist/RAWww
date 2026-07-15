## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Оформление приложения: шрифты, иконки и таблица стилей Qt."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygon,
)
from PySide6.QtWidgets import QApplication

from .runtime_paths import data_path


FOMANTIC_ICON_CODES = {
    "images": "\uf302", "grid": "\uf00a", "user": "\uf007", "brush": "\uf1fc", "media": "\uf87c",
    "sort": "\uf160", "search": "\uf002", "star": "\uf005", "ban": "\uf05e",
    "chevron-down": "\uf078", "chevron-up": "\uf077", "bookmark": "\uf02e",
    "step-forward": "\uf051", "keyboard": "\uf11c", "folder": "\uf07c",
    "filter": "\uf0b0", "lightbulb": "\uf0eb", "volume": "\uf028", "microphone": "\uf130", "close": "\uf00d",
    "plus": "\uf067", "trash": "\uf1f8",
    "expand": "\uf065", "zoom": "\uf00e", "zoom-out": "\uf010", "play": "\uf04b", "pause": "\uf04c", "film": "\uf008",
    "cloud": "\uf0c2", "sign-out": "\uf2f5", "lock": "\uf023", "sync": "\uf021",
    "download": "\uf56d", "eye": "\uf06e", "stop": "\uf04d",
    "link": "\uf0c1",
    "cog": "\uf013", "help": "\uf128", "magic": "\uf0d0", "wrench": "\uf0ad",
    "edit": "\uf044", "calendar": "\uf133", "clock": "\uf017", "camera": "\uf030",
    "file": "\uf15b", "arrow-right": "\uf061", "arrow-up": "\uf062", "level-up": "\uf3bf",
    "folder-plus": "\uf65e",
}

FOMANTIC_ICON_FAMILY = ""

def apply_theme(app: QApplication) -> None:
    """Загружает шрифты и применяет общую таблицу стилей ко всему приложению.

    Стили собраны здесь намеренно: геометрия виджетов остаётся в их классах, а
    цветовая кухня — в одном месте. Иначе поиск нужного оттенка серого быстро
    превращается в отдельную исследовательскую программу.
    """

    app.setStyle("Fusion")
    _load_viewer_fonts()
    interface_font = QFont(app.font())
    interface_font.setFamily("Lato")
    app.setFont(interface_font)
    app.setStyleSheet(
        """
        QMainWindow, QWidget {
            background: #1f1f1f;
            color: #d6d6d6;
            font-size: 12px;
        }
        QFrame#chromeTitleBar {
            background: #000000;
            border: 0;
            min-height: 38px;
        }
        QLabel#appIcon {
            color: #d0d0d0;
            font-size: 12px;
            font-weight: 700;
            min-width: 24px;
            qproperty-alignment: AlignCenter;
        }
        QTabBar#workspaceTabs {
            background: transparent;
        }
        QTabBar#workspaceTabs::tab {
            background: transparent;
            border: 0;
            border-radius: 0;
            color: #b5b5b5;
            margin: 0 1px;
            padding: 0 8px;
            font-size: 12px;
            font-weight: 400;
        }
        QTabBar#workspaceTabs::tab:hover:!selected {
            background: #171717;
            color: #e0e0e0;
        }
        QTabBar#workspaceTabs::tab:selected {
            background: transparent;
            color: #f2f2f2;
        }
        QTabBar#workspaceTabs::close-button:hover {
            background: #505050;
            border-radius: 7px;
        }
        QTabBar#workspaceTabs::close-button {
            background: transparent;
            border: 0;
            width: 16px;
            height: 16px;
        }
        QToolButton#titleAction {
            color: #c9c9c9;
            background: transparent;
            border: 0;
            border-radius: 5px;
            min-width: 26px;
            min-height: 30px;
            font-size: 16px;
        }
        QToolButton#titleAction:hover {
            background: #2b2b2b;
        }
        QToolButton#settingsTitleAction {
            color: #c9c9c9;
            background: transparent;
            border: 0;
            border-radius: 6px;
        }
        QToolButton#settingsTitleAction:hover { background: #2b2b2b; }
        QToolButton#windowControl {
            border: 0;
            border-radius: 0;
            min-width: 32px;
            min-height: 28px;
            font-size: 15px;
        }
        QToolButton#windowControl:hover {
            background: #303030;
        }
        QDialog#settingsDialog {
            background: #202020;
            border: 1px solid #4a4a4a;
        }
        QDialog#helpDialog {
            background: #202020;
            border: 1px solid #4a4a4a;
        }
        QLabel#helpDialogTitle {
            color: #f1f1f1;
            font-size: 20px;
            font-weight: 700;
        }
        QLabel#helpDialogHint {
            color: #aeb7c2;
            font-size: 12px;
            padding-bottom: 4px;
        }
        QTableWidget#helpHotkeysTable {
            background: #181818;
            alternate-background-color: #202020;
            border: 1px solid #3d3d3d;
            border-radius: 7px;
            color: #e5e5e5;
            gridline-color: #303030;
            outline: 0;
            font-size: 12px;
        }
        QTableWidget#helpHotkeysTable QHeaderView::section {
            background: #2b2b2b;
            border: 0;
            border-bottom: 1px solid #454545;
            color: #aeb7c2;
            font-weight: 600;
            padding: 7px 9px;
        }
        QTableWidget#helpHotkeysTable::item {
            padding: 6px 9px;
        }
        QPushButton#helpDialogCloseButton {
            min-width: 90px;
            min-height: 30px;
            border: 1px solid #4c4c4c;
            border-radius: 5px;
            background: #3a3a3a;
            color: #ededed;
        }
        QPushButton#helpDialogCloseButton:hover { background: #4a4a4a; }
        QMenu#helpPopup {
            background: #292929;
            border: 1px solid #5a5a5a;
            border-radius: 8px;
            padding: 0;
        }
        QWidget#helpPopupContent, QWidget#helpPopupContent QLabel {
            background: transparent;
        }
        QLabel#helpPopupTitle {
            color: #f2f2f2;
            font-size: 14px;
            font-weight: 700;
        }
        QPushButton#helpPopupPrimaryButton, QPushButton#helpPopupButton {
            min-height: 30px;
            padding: 4px 10px;
            border: 1px solid #4c4c4c;
            border-radius: 5px;
            color: #ffffff;
            text-align: left;
        }
        QPushButton#helpPopupPrimaryButton {
            background: #315b80;
            border-color: #79aaff;
            font-weight: 600;
        }
        QPushButton#helpPopupPrimaryButton:hover { background: #3d6f9d; }
        QPushButton#helpPopupButton { background: #3a3a3a; color: #ededed; }
        QPushButton#helpPopupButton:hover { background: #4a4a4a; }
        QDialog#quickTransferDialog {
            background: #1d2128;
            border: 1px solid #4a6380;
            border-radius: 10px;
        }
        QLabel#quickTransferTitle {
            color: #f4f8ff;
            font-size: 19px;
            font-weight: 700;
        }
        QLabel#quickTransferHint {
            color: #9eafc5;
            font-size: 12px;
            padding-bottom: 4px;
        }
        QListWidget#quickTransferDestinations {
            background: #15191f;
            border: 1px solid #334354;
            border-radius: 7px;
            color: #dce7f5;
            outline: 0;
            padding: 5px;
            font-size: 13px;
        }
        QListWidget#quickTransferDestinations::item {
            border-radius: 5px;
            min-height: 28px;
            padding: 4px 9px;
        }
        QListWidget#quickTransferDestinations::item:hover {
            background: #263545;
        }
        QListWidget#quickTransferDestinations::item:selected {
            background: #315e91;
            color: #ffffff;
        }
        QDialog#quickTransferDialog QProgressBar {
            background: #15191f;
            border: 1px solid #334354;
            border-radius: 5px;
            color: #e6f0ff;
            text-align: center;
            min-height: 18px;
        }
        QDialog#quickTransferDialog QProgressBar::chunk {
            background: #4b91d1;
            border-radius: 4px;
        }
        QLabel#settingsDialogTitle {
            color: #f1f1f1;
            font-size: 20px;
            font-weight: 700;
        }
        QTabWidget#settingsTabs::pane {
            background: transparent;
            border: 0;
        }
        QWidget#settingsTabPage {
            background: transparent;
        }
        QWidget#behaviorTabPage,
        QWidget#behaviorTabPage QLabel,
        QWidget#behaviorTabPage QCheckBox {
            background: transparent;
        }
        QFrame#externalEditorCard {
            background: #252525;
            border: 1px solid #3c3c3c;
            border-radius: 7px;
        }
        QLabel#externalEditorTitle {
            color: #eeeeee;
            font-size: 14px;
            font-weight: 700;
        }
        QLabel#externalEditorHint {
            color: #9d9d9d;
            font-size: 12px;
            padding-bottom: 2px;
        }
        QRadioButton#editorChoice {
            background: transparent;
            color: #dddddd;
            spacing: 8px;
            min-height: 22px;
        }
        QRadioButton#editorChoice::indicator {
            width: 14px;
            height: 14px;
            border: 1px solid #676767;
            border-radius: 7px;
            background: transparent;
        }
        QRadioButton#editorChoice::indicator:checked {
            background: #79aaff;
            border: 4px solid #252525;
        }
        QWidget#behaviorTabPage QLineEdit#editorExecutable {
            background: transparent;
            border: 0;
            border-bottom: 1px solid #555555;
            border-radius: 0;
            color: #dddddd;
            padding: 5px 2px;
        }
        QWidget#behaviorTabPage QLineEdit#editorExecutable:focus {
            border-bottom-color: #79aaff;
        }
        QWidget#behaviorTabPage QLineEdit#editorExecutable:disabled {
            color: #666666;
            border-bottom-color: #3b3b3b;
        }
        QWidget#behaviorTabPage QToolButton#editorBrowseButton {
            background: transparent;
            border: 1px solid #4b4b4b;
            color: #dddddd;
            border-radius: 4px;
            min-width: 30px;
            min-height: 28px;
        }
        QWidget#behaviorTabPage QToolButton#editorBrowseButton:hover {
            background: #333333;
            border-color: #79aaff;
        }
        QTabWidget#settingsTabs QTabBar::tab {
            background: #252525;
            border: 1px solid #3a3a3a;
            border-bottom: 0;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
            color: #969696;
            min-width: 92px;
            padding: 8px 13px;
        }
        QTabWidget#settingsTabs QTabBar::tab:selected {
            background: #202020;
            border-color: #4b4b4b;
            border-bottom-color: #202020;
            color: #f1f1f1;
            margin-bottom: -1px;
        }
        QTabWidget#settingsTabs QTabBar::tab:hover:!selected {
            background: #303030;
            color: #d8d8d8;
        }
        QLabel#settingsSectionTitle {
            color: #eeeeee;
            font-size: 14px;
            font-weight: 700;
        }
        QLabel#settingsHint {
            color: #9d9d9d;
            font-size: 12px;
            padding-bottom: 10px;
        }
        QDialog#settingsDialog QCheckBox {
            color: #dddddd;
            spacing: 9px;
            min-height: 28px;
        }
        QDialog#settingsDialog QCheckBox::indicator {
            width: 16px;
            height: 16px;
            border: 1px solid #676767;
            border-radius: 3px;
            background: #202020;
        }
        QDialog#settingsDialog QCheckBox::indicator:checked {
            background: #3f6db5;
            border-color: #79aaff;
        }
        QPushButton#settingsSecondaryButton, QPushButton#settingsPrimaryButton {
            min-width: 88px;
            min-height: 30px;
            border-radius: 5px;
            padding: 3px 12px;
            font-weight: 600;
        }
        QPushButton#settingsSecondaryButton {
            background: #2b2b2b;
            border: 1px solid #4b4b4b;
            color: #dddddd;
        }
        QPushButton#settingsSecondaryButton:hover { background: #363636; }
        QPushButton#settingsPrimaryButton {
            background: #315b92;
            border: 1px solid #79aaff;
            color: #ffffff;
        }
        QPushButton#settingsPrimaryButton:hover { background: #396bab; }
        QTreeView, QListWidget {
            background: #252525;
            border: 1px solid #161616;
            color: #d8d8d8;
            alternate-background-color: #202020;
            outline: 0;
        }
        QComboBox {
            background: #2b2b2b;
            border: 1px solid #151515;
            color: #d8d8d8;
            padding: 5px 8px;
        }
        QComboBox:hover {
            background: #333333;
        }
        QComboBox::drop-down {
            border: 0;
            width: 24px;
        }
        QComboBox QAbstractItemView {
            background: #252525;
            border: 1px solid #111111;
            selection-background-color: #3f6db5;
            color: #d8d8d8;
        }
        QToolButton#driveButton {
            background: #292929;
            border: 1px solid #3a3a3a;
            border-radius: 5px;
            color: #e8e8e8;
            font-weight: 600;
            min-height: 22px;
            padding: 0 3px;
        }
        QToolButton#driveButton:hover {
            background: #363636;
            border-color: #5689d6;
        }
        QToolButton#driveButton:checked {
            background: #315b92;
            border-color: #79aaff;
            color: white;
        }
        QWidget#directoryPanel {
            background: transparent;
        }
        QLabel#directoryTitle {
            background: transparent;
            color: #a9a9a9;
            font-size: 11px;
            font-weight: 600;
        }
        QToolButton#directoryAction {
            background: transparent;
            border: 1px solid transparent;
            border-radius: 4px;
            min-width: 18px;
            max-width: 18px;
            min-height: 24px;
            max-height: 24px;
            padding: 0;
        }
        QToolButton#directoryAction:hover {
            background: #303030;
            border-color: #4b4b4b;
        }
        QToolButton#directoryAction:pressed {
            background: #1b1b1b;
        }
        QToolButton#directoryAction:disabled {
            background: #202020;
            border-color: #121212;
        }
        QWidget#favoritesPanel {
            background: #202020;
            border-top: 1px solid #161616;
        }
        QLabel#favoritesTitle {
            background: transparent;
            color: #a9a9a9;
            font-size: 11px;
            font-weight: 600;
        }
        QToolButton#favoritesAdd, QToolButton#favoritesTrash {
            min-width: 22px;
            max-width: 22px;
            min-height: 20px;
            max-height: 20px;
            padding: 0;
            border: 1px solid transparent;
            border-radius: 3px;
            background: transparent;
        }
        QToolButton#favoritesAdd:hover {
            background: #363636;
            border-color: #4b4b4b;
        }
        QToolButton#favoritesTrash:hover, QToolButton#favoritesTrash[dropActive="true"] {
            background: #633536;
            border-color: #a65c5d;
        }
        QListWidget#favoritesList {
            background: transparent;
            border: 0;
            outline: 0;
            padding: 0 2px;
        }
        QListWidget#favoritesList::item {
            min-height: 22px;
            padding: 2px 4px;
            border-radius: 3px;
        }
        QListWidget#favoritesList::item:hover { background: #333333; }
        QListWidget#favoritesList::item:selected { background: #3f6db5; }
        QSplitter#favoritesSplitter::handle:vertical {
            height: 9px;
            background: #353535;
            border-top: 1px solid #4b4b4b;
            border-bottom: 1px solid #202020;
        }
        QSplitter#favoritesSplitter::handle:vertical:hover { background: #4b4b4b; }
        QSplitter#panelSplitter::handle:horizontal {
            width: 9px;
            background: #353535;
            border-left: 1px solid #4b4b4b;
            border-right: 1px solid #202020;
        }
        QSplitter#panelSplitter::handle:horizontal:hover { background: #4b4b4b; }
        QFrame#gridZoomControls {
            background: rgba(34, 37, 42, 0.78);
            border: 1px solid #4a4f56;
            border-radius: 7px;
        }
        QToolButton#gridZoomButton {
            background: rgba(67, 73, 81, 0.62);
            border: 1px solid #626872;
            border-radius: 5px;
            padding: 0;
        }
        QToolButton#gridZoomButton:hover { background: rgba(91, 99, 109, 0.78); border-color: #89919c; }
        QToolButton#gridZoomButton:pressed { background: rgba(45, 50, 57, 0.8); }
        QWidget#shotsyncPanel {
            background: transparent;
        }
        QLabel#shotsyncTitle {
            color: #f0f0f0;
            font-size: 15px;
            font-weight: 700;
        }
        QLabel#shotsyncHint {
            color: #8a8a8a;
            font-size: 12px;
        }
        QLabel#shotsyncError {
            color: #e2726e;
            font-size: 12px;
        }
        QLabel#shotsyncSection {
            color: #8a8a8a;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1px;
            padding: 2px 2px 0 2px;
        }
        QToolButton#shotsyncRefreshButton {
            background: transparent;
            border: 0;
            padding: 0;
        }
        QToolButton#shotsyncRefreshButton:hover {
            background: #3b3b3b;
            border-radius: 4px;
        }
        QLineEdit#shotsyncField {
            background: #202020;
            border: 1px solid #3a3a3a;
            border-radius: 6px;
            padding: 7px 9px;
            color: #ededed;
        }
        QLineEdit#shotsyncField:focus {
            border-color: #5689d6;
        }
        QPushButton#shotsyncPrimaryButton {
            background: #315b92;
            border: 1px solid #79aaff;
            border-radius: 6px;
            padding: 8px 12px;
            color: #ffffff;
            font-weight: 600;
        }
        QPushButton#shotsyncPrimaryButton:hover {
            background: #396bab;
        }
        QPushButton#shotsyncPrimaryButton:disabled {
            background: #2a2a2a;
            border-color: #3a3a3a;
            color: #8a8a8a;
        }
        QPushButton#shotsyncSendButton {
            min-height: 36px;
            max-height: 36px;
            background: #303030;
            border: 1px solid #626262;
            border-radius: 6px;
            color: #f0f0f0;
            font-family: Lato;
            font-size: 13px;
            font-weight: 700;
            padding: 5px 12px;
        }
        QPushButton#shotsyncSendButton:hover {
            background: #444444;
            border-color: #9a9a9a;
        }
        QPushButton#shotsyncSendButton:disabled {
            background: #242424;
            border-color: #3d3d3d;
            color: #777777;
        }
        QDialog#shotsyncUploadPopup {
            background: #202020;
            border: 1px solid #666666;
        }
        QLabel#shotsyncUploadPopupTitle {
            background: transparent;
            color: #f2f2f2;
            font-family: Lato;
            font-size: 18px;
            font-weight: 700;
        }
        QLabel#shotsyncUploadPopupHint {
            background: transparent;
            color: #a8a8a8;
            font-family: Lato;
            font-size: 12px;
        }
        QLineEdit#shotsyncUploadPopupField {
            min-height: 32px;
            background: #2b2b2b;
            border: 1px solid #555555;
            border-radius: 5px;
            color: #eeeeee;
            padding: 4px 9px;
        }
        QLineEdit#shotsyncUploadPopupField:focus {
            border-color: #999999;
        }
        QPushButton#shotsyncUploadPopupBrowse,
        QPushButton#shotsyncUploadPopupCancel,
        QPushButton#shotsyncUploadPopupSend {
            min-height: 32px;
            border-radius: 5px;
            padding: 4px 12px;
            font-family: Lato;
            font-weight: 600;
        }
        QPushButton#shotsyncUploadPopupBrowse,
        QPushButton#shotsyncUploadPopupCancel {
            background: #303030;
            border: 1px solid #5b5b5b;
            color: #dddddd;
        }
        QPushButton#shotsyncUploadPopupSend {
            background: #e0e0e0;
            border: 1px solid #f4f4f4;
            color: #171717;
        }
        QPushButton#shotsyncUploadPopupSend:disabled {
            background: #3a3a3a;
            border-color: #4c4c4c;
            color: #777777;
        }
        QCheckBox#shotsyncUploadPopupCheck {
            min-height: 28px;
            background: transparent;
            color: #d8d8d8;
            spacing: 8px;
            font-family: Lato;
            font-size: 12px;
        }
        QCheckBox#shotsyncUploadPopupCheck::indicator {
            width: 18px;
            height: 18px;
            background: #292929;
            border: 1px solid #777777;
            border-radius: 4px;
        }
        QCheckBox#shotsyncUploadPopupCheck::indicator:hover {
            border-color: #bdbdbd;
        }
        QCheckBox#shotsyncUploadPopupCheck::indicator:checked {
            background: #e0e0e0;
            border-color: #f2f2f2;
        }
        QLabel#shotsyncUploadStateTitle {
            background: transparent;
            color: #eeeeee;
            font-family: Lato;
            font-size: 20px;
            font-weight: 700;
        }
        QLabel#shotsyncUploadStateHint {
            background: transparent;
            color: #999999;
            font-family: Lato;
            font-size: 13px;
        }
        QWidget#shotsyncProfile {
            background: transparent;
            border: none;
        }
        QLabel#shotsyncProfileName {
            color: #f0f0f0;
            font-size: 13px;
            font-weight: 600;
        }
        QToolButton#shotsyncLogoutButton {
            background: transparent;
            border: none;
            border-radius: 5px;
            padding: 0;
            color: #d0d0d0;
            font-size: 18px;
        }
        QToolButton#shotsyncLogoutButton:hover {
            background: #3a3a3a;
        }
        QListWidget#shotsyncShootingList {
            background: #1c1c1c;
            border: 1px solid #2c2c2c;
            border-radius: 8px;
            padding: 5px;
        }
        QListWidget#shotsyncShootingList::item {
            padding: 0;
            margin: 0 0 7px 0;
            border: 0;
            background: transparent;
        }
        QWidget#shotsyncShootingCard {
            background: #242424;
            border: 1px solid #494949;
            border-radius: 10px;
        }
        QWidget#shotsyncShootingCard:hover {
            background: #303030;
            border-color: #777777;
        }
        QLabel#shotsyncShootingTitle {
            background: transparent;
            border: 0;
            color: #f2f4f8;
            font-family: Lato;
            font-size: 15px;
            font-weight: 700;
        }
        QToolButton#shotsyncViewerLink {
            background: transparent;
            border: 0;
            border-radius: 4px;
            color: #b9c5d6;
            font-size: 16px;
            padding: 0;
        }
        QToolButton#shotsyncViewerLink:hover {
            background: #424242;
            color: #ffffff;
        }
        QWidget#shotsyncShootingCard[currentShooting="true"] {
            border: 2px solid #dddddd;
        }
        QWidget#shotsyncShootingCard QLabel#shotsyncHint {
            background: transparent;
            border: 0;
            color: #a9b1bd;
            font-family: Lato;
            font-size: 12px;
        }
        QWidget#shotsyncShootingCard QPushButton {
            min-height: 21px;
            max-height: 21px;
            background: #303030;
            border: 1px solid #5c5c5c;
            border-radius: 4px;
            color: #eeeeee;
            font-family: Lato;
            font-size: 11px;
            font-weight: 600;
            padding: 1px 7px;
        }
        QWidget#shotsyncShootingCard QPushButton:hover {
            background: #424242;
            border-color: #969696;
        }
        QTreeView::item, QListWidget::item {
            padding: 6px;
        }
        QTreeView::item:selected, QListWidget::item:selected {
            background: #3f6db5;
            color: #ffffff;
        }
        QTreeView[treeFocused="false"]::item:selected {
            background: #30445d;
            color: #d2d8e0;
        }
        QListWidget::item:hover, QTreeView::item:hover {
            background: #333333;
        }
        QSplitter::handle {
            background: #111111;
        }
        QLabel {
            color: #d6d6d6;
        }
        QLabel#overlayLabel {
            background: #191919;
            border-bottom: 1px solid #111111;
            min-height: 30px;
            color: #cfcfcf;
        }
        QScrollBar:vertical, QScrollBar:horizontal {
            background: #202020;
            border: 0;
            width: 6px;
            height: 6px;
        }
        QScrollBar::handle {
            background: #555555;
            border-radius: 2px;
        }
        QScrollBar::handle:hover {
            background: #6a6a6a;
        }
        QScrollBar::add-line, QScrollBar::sub-line {
            width: 0;
            height: 0;
        }
        /* Visual language shared with the ShotSync /v viewer. */
        QWidget#viewerToolbar {
            min-height: 36px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #3d3d3d, stop:0.48 #303030, stop:1 #272727);
            border-bottom: 1px solid #111111;
            border-top: 1px solid #505050;
        }
        QWidget#viewerToolbar QComboBox, QWidget#viewerToolbar QLineEdit,
        QWidget#viewerToolbar QPushButton, QWidget#viewerToolbar QToolButton {
            min-height: 21px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #515151, stop:1 #404040);
            border: 1px solid #1b1b1b;
            border-radius: 2px;
            color: #ececec;
            padding: 1px 6px;
            font-size: 10px;
        }
        QWidget#viewerToolbar QLineEdit {
            background: #303030;
            color: #ededed;
            padding-left: 9px;
        }
        QWidget#viewerStatusPanel, QLabel#viewerStatusText {
            background: transparent;
            border: 0;
        }
        QLabel#viewerStatusText {
            color: #c4c4c4;
            font-size: 11px;
            padding: 0;
        }
        QLabel#viewerToast {
            background: #34573b;
            color: #f3fff3;
            border: 1px solid #5c9464;
            border-radius: 6px;
            padding: 8px 14px;
            font-weight: 600;
        }
        QProgressBar#viewerStatusProgress {
            min-height: 14px;
            max-height: 14px;
            border: 1px solid #595959;
            border-radius: 6px;
            background: #111111;
            color: #f0f0f0;
            font-size: 9px;
            padding: 0;
        }
        QProgressBar#viewerStatusProgress::chunk {
            border-radius: 5px;
            background: #707070;
        }
        QWidget#viewerFiltersPanel {
            min-height: 27px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 rgba(46, 46, 46, 0.96), stop:1 rgba(35, 35, 35, 0.96));
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 10px;
            padding: 0;
        }
        QWidget#viewerFiltersPanel QLabel {
            background: transparent;
            border: 0;
            padding: 0;
        }
        QWidget#viewerSearchBox {
            background: transparent;
            border: 0;
            padding: 0;
        }
        QWidget#viewerFiltersPanel QComboBox,
        QWidget#viewerFiltersPanel QLineEdit {
            min-height: 21px;
            max-height: 21px;
            font-size: 10px;
        }
        QFrame#faceFilterChip {
            background: #343434;
            border: 1px solid #5a5a5a;
            border-radius: 16px;
        }
        QFrame#faceFilterChip QLabel, QFrame#fullFaceFilterChip QLabel,
        QLabel#faceSetAvatar {
            background: transparent;
            border: none;
        }
        QToolButton#faceFilterClear {
            border: none;
            background: transparent;
            padding: 0;
            min-width: 0;
            min-height: 0;
        }
        QFrame#fullFaceFilterChip {
            background: #343434;
            border: 1px solid #5a5a5a;
            border-radius: 16px;
        }
        QToolButton#fullFaceFilterClear {
            border: none;
            background: transparent;
            padding: 0;
            min-width: 0;
            min-height: 0;
        }
        QWidget#viewerToolbar QToolButton#fullFaceFilterClear,
        QWidget#viewerToolbar QToolButton#fullFaceFilterClear:hover,
        QWidget#viewerToolbar QToolButton#fullFaceFilterClear:pressed {
            background: transparent;
            border: none;
            border-radius: 0;
            padding: 0;
            min-width: 0;
            min-height: 0;
        }
        QMenu#faceActionMenu { padding: 0; }
        QToolButton#faceActionButton {
            min-height: 30px;
            border: 1px solid #555;
            border-radius: 4px;
            padding: 3px 7px;
        }
        QToolButton#faceActionButton:hover { background: #3d3d3d; }
        QDialog#faceSetsDialog { background: #292929; }
        QLabel#faceSetsTitle { font-size: 16px; font-weight: 600; }
        QLabel#faceSetsToast {
            background: #34573b;
            color: #f3fff3;
            border: 1px solid #5c9464;
            border-radius: 6px;
            padding: 8px 12px;
            font-weight: 600;
        }
        QFrame#faceSetRow { background: transparent; border: none; }
        QWidget#viewerFiltersPanel QComboBox {
            combobox-popup: 0;
            padding-left: 5px;
            padding-right: 3px;
        }
        QWidget#viewerFiltersPanel QComboBox QAbstractItemView {
            background: #484848;
            border-color: #1b1b1b;
            color: #ececec;
            selection-background-color: #606060;
            selection-color: #ffffff;
            font-size: 10px;
            outline: 0;
        }
        QWidget#viewerFiltersPanel QComboBox QAbstractItemView::item {
            min-height: 21px;
            padding-left: 5px;
            padding-right: 5px;
        }
        QWidget#viewerFiltersPanel QComboBox QAbstractItemView::item:hover {
            background: #565656;
        }
        QWidget#viewerFiltersPanel QLineEdit {
            background: #303030;
            padding-left: 7px;
            padding-right: 7px;
            padding-top: 0;
            padding-bottom: 0;
        }
        QLineEdit#viewerSearchEdit QToolButton {
            min-height: 18px;
            max-height: 18px;
            margin-top: 3px;
            margin-bottom: -3px;
        }
        QWidget#viewerToolbar QComboBox:hover, QWidget#viewerToolbar QPushButton:hover,
        QWidget#viewerToolbar QToolButton:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #606060, stop:1 #4b4b4b);
            border-color: #707070;
        }
        QToolButton#toolbarAction {
            min-width: 52px;
            min-height: 40px;
            padding: 1px 4px;
            border: 1px solid #3b3b3b;
            border-radius: 4px;
            background: #303030;
            color: #e5e5e5;
            font-size: 10px;
        }
        QToolButton#toolbarAction:hover {
            background: #454545;
            border-color: #707070;
        }
        QToolButton#toolbarAction:disabled {
            color: #777777;
            background: #272727;
            border-color: #343434;
        }
        QMenu#toolbarPopup {
            background: #292929;
            border: 1px solid #5a5a5a;
            border-radius: 8px;
            padding: 0;
        }
        QWidget#toolbarPopupContent, QWidget#toolbarPopupContent QLabel, QWidget#toolbarPopupContent QCheckBox {
            background: transparent;
        }
        QLabel#toolbarPopupTitle {
            background: transparent;
            color: #f2f2f2;
            font-size: 14px;
            font-weight: 700;
        }
        QLabel#toolbarPopupHint {
            background: transparent;
            color: #b8c0ca;
            font-size: 11px;
        }
        QMenu#toolbarPopup QCheckBox {
            color: #dddddd;
            spacing: 9px;
            min-height: 28px;
        }
        QMenu#toolbarPopup QCheckBox::indicator {
            width: 16px;
            height: 16px;
            border: 1px solid #676767;
            border-radius: 3px;
            background: #202020;
        }
        QMenu#toolbarPopup QCheckBox::indicator:checked {
            background: #3f6db5;
            border-color: #79aaff;
        }
        QPushButton#toolbarPopupPrimaryButton, QPushButton#toolbarPopupUtilityButton {
            min-height: 30px;
            padding: 4px 10px;
            border: 1px solid #4c4c4c;
            border-radius: 5px;
            background: #3a3a3a;
            color: #ededed;
            text-align: left;
        }
        QPushButton#toolbarPopupPrimaryButton {
            background: #315b80;
            border-color: #79aaff;
            color: #ffffff;
            font-weight: 600;
        }
        QPushButton#toolbarPopupPrimaryButton:hover:!disabled { background: #3d6f9d; }
        QPushButton#toolbarPopupPrimaryButton:disabled {
            color: #777d84;
            background: #303030;
            border-color: #414141;
            font-weight: 400;
        }
        QPushButton#toolbarPopupUtilityButton:disabled {
            color: #777d84;
            background: #303030;
            border-color: #414141;
        }
        QDialog#batchRenameDialog {
            background: #242424;
            color: #e8e8e8;
        }
        QLabel#batchRenameTitle {
            background: transparent;
            color: #f4f4f4;
            font-size: 19px;
            font-weight: 700;
        }
        QLabel#batchRenameHint, QLabel#batchRenameTokens {
            color: #aeb8c4;
            font-size: 11px;
            background: transparent;
        }
        QLabel#batchRenameLabel { color: #d9dfe6; font-weight: 600; }
        QLineEdit#batchRenameTemplate {
            min-height: 31px;
            background: #181818;
            border: 1px solid #4c5967;
            border-radius: 5px;
            color: #f1f4f7;
            padding: 2px 9px;
            font-family: Consolas, monospace;
        }
        QLineEdit#batchRenameTemplate:focus { border-color: #79aaff; }
        QFrame#batchRenameConstructor {
            background: transparent;
            border: 0;
        }
        QFrame#batchRenameConstructor QLabel { background: transparent; color: #d5dce5; }
        QSpinBox#batchRenameSpin {
            min-height: 25px;
            background: #1d1f22;
            border: 1px solid #4b5662;
            border-radius: 4px;
            color: #e7edf4;
            padding: 0 5px;
        }
        QToolButton#batchRenameToken {
            min-height: 25px;
            padding: 0 6px;
            border: 1px solid #4a5662;
            border-radius: 4px;
            background: #353c44;
            color: #e2e8ef;
            font-size: 11px;
        }
        QToolButton#batchRenameToken:hover { background: #46515d; border-color: #718294; }
        QFrame#batchRenamePreview {
            background: transparent;
            border: 0;
        }
        QLabel#batchRenamePreviewTitle { color: #c3d3e4; font-weight: 700; background: transparent; }
        QListWidget#batchRenameList {
            background: #181818;
            border: 1px solid #30353a;
            border-radius: 4px;
            color: #d9dfe6;
            padding: 3px;
            font-family: Consolas, monospace;
        }
        QListWidget#batchRenameList::item { padding: 3px 5px; }
        QLabel#batchRenameValidation { background: transparent; color: #9eb7ce; min-height: 18px; }
        QLabel#batchRenameValidation[invalid="true"] { color: #f08b8b; }
        QProgressBar#batchRenameProgress {
            min-height: 20px;
            border: 1px solid #46576a;
            border-radius: 5px;
            background: #181818;
            color: #e7f0fb;
            text-align: center;
        }
        QProgressBar#batchRenameProgress::chunk { background: #386f9e; border-radius: 4px; }
        QPushButton#batchRenameSecondaryButton, QPushButton#batchRenamePrimaryButton {
            min-height: 31px;
            padding: 3px 13px;
            border-radius: 5px;
        }
        QPushButton#batchRenameSecondaryButton {
            background: #383838;
            border: 1px solid #565656;
            color: #e0e0e0;
        }
        QPushButton#batchRenameSecondaryButton:hover { background: #484848; }
        QPushButton#batchRenamePrimaryButton {
            background: #315b80;
            border: 1px solid #79aaff;
            color: #ffffff;
            font-weight: 600;
        }
        QPushButton#batchRenamePrimaryButton:hover { background: #3d6f9d; }
        QPushButton#batchRenamePrimaryButton:disabled { background: #303030; border-color: #464646; color: #777d84; }
        QDialog#batchResizeDialog { background: #242424; color: #e8e8e8; }
        QDialog#batchResizeDialog QLabel { background: transparent; }
        QLabel#batchResizeFieldLabel {
            min-width: 118px;
            color: #c9d3de;
            font-size: 12px;
            font-weight: 600;
        }
        QLineEdit#batchResizeOutput {
            min-height: 30px;
            background: #181818;
            border: 1px solid #4c5967;
            border-radius: 5px;
            color: #f1f4f7;
            padding: 2px 9px;
        }
        QLineEdit#batchResizeOutput:focus { border-color: #79aaff; }
        QToolButton#batchResizeBrowse {
            min-width: 30px;
            min-height: 30px;
            border: 1px solid #4a5662;
            border-radius: 5px;
            background: #353c44;
        }
        QToolButton#batchResizeBrowse:hover { background: #46515d; border-color: #718294; }
        QSpinBox#batchResizeSpin, QDoubleSpinBox#batchResizeSpin {
            min-height: 27px;
            background: #1d1f22;
            border: 1px solid #4b5662;
            border-radius: 4px;
            color: #e7edf4;
            padding: 0 5px;
        }
        QFrame#batchResizeOptions { background: transparent; border: 0; }
        QCheckBox#batchResizeOption {
            min-height: 28px;
            background: transparent;
            color: #e8edf2;
            font-size: 12px;
            font-weight: 600;
            spacing: 8px;
            font-family: Lato;
        }
        QCheckBox#batchResizeOption::indicator {
            width: 18px;
            height: 18px;
            background: #292929;
            border: 1px solid #777777;
            border-radius: 4px;
        }
        QCheckBox#batchResizeOption::indicator:hover { border-color: #bdbdbd; }
        QCheckBox#batchResizeOption::indicator:checked {
            background: #3f6db5;
            border-color: #79aaff;
        }
        QCheckBox#batchResizeOption::indicator:disabled {
            background: #242424;
            border-color: #454545;
        }
        QCheckBox#batchResizeOption:disabled, QLabel#batchResizeSettingLabel:disabled { background: transparent; color: #727982; }
        QLabel#batchResizeSettingLabel { color: #9fabb8; font-size: 11px; }
        QLabel#batchResizeStatus { min-height: 18px; color: #9eb7ce; font-size: 11px; }
        QProgressBar#batchResizeProgress {
            min-height: 20px;
            border: 1px solid #46576a;
            border-radius: 5px;
            background: #181818;
            color: #e7f0fb;
            text-align: center;
        }
        QProgressBar#batchResizeProgress::chunk { background: #386f9e; border-radius: 4px; }
        QPushButton#batchResizeSecondaryButton, QPushButton#batchResizePrimaryButton {
            min-height: 31px;
            padding: 3px 13px;
            border-radius: 5px;
        }
        QPushButton#batchResizeSecondaryButton {
            background: #383838;
            border: 1px solid #565656;
            color: #e0e0e0;
        }
        QPushButton#batchResizeSecondaryButton:hover { background: #484848; }
        QPushButton#batchResizePrimaryButton {
            background: #315b80;
            border: 1px solid #79aaff;
            color: #ffffff;
            font-weight: 600;
        }
        QPushButton#batchResizePrimaryButton:hover { background: #3d6f9d; }
        QPushButton#batchResizePrimaryButton:disabled { background: #303030; border-color: #464646; color: #777d84; }
        QWidget#viewerAiPanel {
            min-height: 30px;
            background: transparent;
            border-bottom: 1px solid #131313;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
        }
        QLabel#aiPanelTitle {
            color: #a8b0bd;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.7px;
            padding-right: 4px;
        }
        QToolButton#aiFilter {
            min-height: 21px;
            border: 1px solid #363636;
            border-radius: 10px;
            background: #242424;
            color: #c9c9c9;
            padding: 0 7px;
            font-size: 10px;
        }
        QToolButton#aiFilter:hover { background: #303030; color: #f0f0f0; }
        QToolButton#aiFilter:checked {
            background: #315b80;
            border-color: #79aaff;
            color: #ffffff;
        }
        QToolButton#shotFilter {
            min-height: 21px;
            border: 1px solid #363636;
            border-radius: 12px;
            background: #242424;
            color: #c9c9c9;
            padding: 0 7px;
            font-size: 10px;
        }
        QToolButton#shotFilter:hover { background: #303030; color: #f0f0f0; }
        QToolButton#shotFilter:checked {
            background: #315b80;
            border-color: #79aaff;
            color: #ffffff;
        }
        QProgressBar {
            border: 1px solid #171717;
            border-radius: 2px;
            background: #2a2a2a;
            color: #c9c9c9;
            text-align: center;
        }
        QProgressBar::chunk { background: #5284bd; }
        QListWidget#photoGrid {
            background: #666666;
            border: 0;
            color: #252525;
            padding: 3px;
        }
        QListWidget#photoGrid::item { background: transparent; padding: 0; }
        QListWidget#photoGrid::item:selected, QListWidget#photoGrid::item:hover {
            background: transparent;
            color: #252525;
        }
        QWidget#viewerMeta {
            min-height: 30px;
            max-height: 30px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #303030, stop:1 #272727);
            border-top: 1px solid #111111;
            border-bottom: 1px solid #454545;
        }
        QWidget#viewerMeta QPushButton, QWidget#viewerMeta QToolButton {
            color: #c9c9c9;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #4a4a4a, stop:1 #3c3c3c);
            border: 1px solid #171717;
            border-radius: 0;
            padding: 0 6px;
            font-size: 11px;
        }
        QWidget#viewerMeta QPushButton:hover, QWidget#viewerMeta QToolButton:hover {
            background: #505050;
            color: #f4f4f4;
        }
        QWidget#viewerMeta QLineEdit {
            background: #1f1f1f;
            color: #e1e1e1;
            border: 1px solid #111111;
            border-radius: 0;
            padding: 0 7px;
            font-size: 11px;
        }
        QTreeView {
            background: #252525;
            border: 0;
        }
        QFrame#fullView, QWidget#fullImageView { background: #1f1f1f; }
        QLabel#fullViewCounter {
            background: rgba(20, 22, 26, 0.78);
            border: 1px solid rgba(255, 255, 255, 0.18);
            border-radius: 4px;
            color: #f0f2f5;
            padding: 3px 7px;
            font-size: 12px;
        }
        QToolButton#fullViewMarkIndicator {
            border: 1px solid rgba(255, 255, 255, 0.24);
            border-radius: 22px;
            color: #ffffff;
            font-size: 13px;
            font-weight: 700;
        }
        QToolButton#fullViewMarkIndicator:hover {
            border-color: rgba(255, 255, 255, 0.6);
        }
        QLabel#overlayLabel {
            background: #252525;
            border: 0;
            border-bottom: 1px solid #111111;
            color: #9fa7b3;
            min-height: 28px;
        }
        QFrame#stripPanel {
            border-top: 1px solid #171717;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #373737, stop:1 #2a2a2a);
        }
        QWidget#stripHeader {
            min-height: 24px;
            background: transparent;
        }
        QToolButton#stripToggle {
            min-width: 46px;
            max-width: 46px;
            min-height: 16px;
            max-height: 16px;
            border: 1px solid #333333;
            border-radius: 8px;
            background: #181818;
            color: #f4f4f5;
            font-size: 13px;
        }
        QToolButton#stripToggle:hover { background: #242424; }
        QToolButton#fullQuickMark {
            min-width: 96px;
            max-width: 96px;
            min-height: 24px;
            max-height: 24px;
            border: 1px solid #1a1a1a;
            border-radius: 0;
            background: #3c3c3c;
        }
        QToolButton#fullQuickMark:hover { background: #505050; }
        QToolButton#fullAutoAdvance {
            min-width: 28px;
            max-width: 28px;
            min-height: 24px;
            max-height: 24px;
            border: 1px solid #1a1a1a;
            border-radius: 0;
            background: #3c3c3c;
        }
        QToolButton#fullAutoAdvance:hover { background: #505050; }
        QToolButton#fullAutoAdvance:checked {
            color: #9fc3f5;
            background: #38495f;
            border-color: #607fa8;
        }
        QToolButton#videoPlay {
            min-width: 28px;
            max-width: 28px;
            min-height: 22px;
            max-height: 22px;
            border: 1px solid #1a1a1a;
            border-radius: 3px;
            background: #3c3c3c;
        }
        QFrame#videoControls {
            background: #252525;
            border: 1px solid #121212;
            border-radius: 7px;
        }
        QToolButton#audioToggle {
            border: 1px solid rgba(255,255,255,0.14);
            border-radius: 24px;
            background: #161616;
            color: #f4f4f5;
        }
        QToolButton#audioToggle:hover { background: #303030; }
        QFrame#audioPanel {
            background: #171717;
            border: 1px solid #454545;
            border-radius: 10px;
        }
        QLabel#videoTime { min-width: 82px; background: transparent; color: #d5d5d5; font-size: 11px; font-weight: 600; }
        QSlider#videoSeek { background: transparent; }
        QSlider#videoSeek::groove:horizontal { height: 4px; background: #1b1b1b; border-radius: 2px; }
        QSlider#videoSeek::handle:horizontal { width: 10px; margin: -4px 0; background: #c8c8c8; border-radius: 5px; }
        QLineEdit#fullComment, QTextEdit#fullComment {
            min-width: 72px;
            max-width: 420px;
            min-height: 24px;
            max-height: 24px;
            background: #202020;
            color: #e1e1e1;
            border: 1px solid #111111;
            border-radius: 0;
            padding: 0 7px;
        }
        QListWidget#photoStrip {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #373737, stop:1 #2a2a2a);
            border: 0;
            outline: 0;
            padding: 1px 4px 2px 4px;
        }
        QListWidget#seriesStrip {
            background: transparent;
            border: 0;
            outline: 0;
            padding: 0;
        }
        QListWidget#photoStrip::item, QListWidget#seriesStrip::item,
        QListWidget#photoStrip::item:selected, QListWidget#seriesStrip::item:selected,
        QListWidget#photoStrip::item:hover, QListWidget#seriesStrip::item:hover {
            background: transparent;
            padding: 0;
        }
        QFrame#seriesPanel {
            min-width: 136px;
            max-width: 136px;
            border: 1px solid #383838;
            border-radius: 10px;
            background: #151515;
        }
        QToolButton#seriesNav {
            min-height: 26px;
            max-height: 26px;
            border: 1px solid #2f2f2f;
            border-radius: 6px;
            background: #383838;
            color: #ececec;
            font-size: 15px;
        }
        QToolButton#seriesNav:hover { background: #484848; }
        QToolButton#seriesNav:disabled { color: #666666; background: #292929; }
        QWidget#viewerMeta QToolButton#viewerColor {
            min-width: 23px;
            max-width: 23px;
            min-height: 22px;
            max-height: 22px;
            padding: 0;
            border: 1px solid #181818;
            border-left: 0;
            border-radius: 0;
            background: #4e4e4e;
            color: #b8b8b8;
            font-size: 10px;
        }
        QWidget#viewerRatingRow {
            min-width: 149px;
            max-width: 149px;
            min-height: 24px;
            max-height: 24px;
            border: 0;
            border-radius: 0;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #4a4a4a, stop:1 #3b3b3b);
        }
        QWidget#viewerMeta QPushButton#viewerRating {
            min-width: 24px;
            max-width: 24px;
            min-height: 24px;
            max-height: 24px;
            padding: 0;
            border: 0;
            border-left: 0;
            border-radius: 0;
            background: transparent;
            color: rgba(230, 230, 230, 0.22);
            font-size: 10px;
        }
        QWidget#viewerMeta QPushButton#viewerRating[ratingClear="true"] { border-left: 0; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="red"] { background: #7a5555; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="yellow"] { background: #7f7556; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="green"] { background: #5d7560; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="blue"] { background: #596b82; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="purple"] { background: #71607d; }
        QWidget#viewerMeta QToolButton#viewerColor:hover {
            border-color: #181818;
        }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="none"] {
            min-width: 22px;
            max-width: 22px;
            border-left: 1px solid #181818;
        }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="none"]:hover { background: #696969; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="red"]:hover { background: #a96a6a; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="yellow"]:hover { background: #aa9a65; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="green"]:hover { background: #719477; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="blue"]:hover { background: #708caa; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="purple"]:hover { background: #9175a2; }
        QWidget#viewerMeta QToolButton#viewerColor:checked {
            border-color: #181818;
        }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="none"]:checked {
            border-left-color: #181818;
        }
        QWidget#viewerMeta QPushButton#viewerRating:hover { background: rgba(255, 255, 255, 0.08); }
        QWidget#viewerMeta QPushButton#viewerRating:checked {
            color: #d8d8d8;
            background: rgba(255, 255, 255, 0.04);
        }
        QWidget#viewerMeta QPushButton#viewerRating[ratingClear="true"] {
            color: rgba(220, 220, 220, 0.42);
        }
        QWidget#viewerMeta QPushButton#viewerRating[ratingClear="true"]:checked {
            color: #c8c8c8;
        }
        QLabel#viewerExif {
            min-width: 0;
            background: transparent;
            border: 0;
            color: #9fa7b3;
            font-size: 11px;
        }
        QMenu QPushButton#quickMarkMenuItem {
            min-width: 170px;
            min-height: 26px;
            max-height: 26px;
            padding: 0;
            border: 0;
            border-radius: 0;
            background: transparent;
            text-align: left;
        }
        QMenu QPushButton#quickMarkMenuItem:hover { background: #454545; }
        QMenu QLabel#quickMarkMenuCheck,
        QMenu QLabel#quickMarkMenuValue {
            background: transparent;
            border: 0;
            color: #dedede;
            font-size: 13px;
        }
        """
    )

def _load_fomantic_icons() -> None:
    global FOMANTIC_ICON_FAMILY
    if FOMANTIC_ICON_FAMILY:
        return
    asset = data_path("assets") / "fomantic-icons.ttf"
    font_id = QFontDatabase.addApplicationFont(str(asset))
    if font_id >= 0:
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            FOMANTIC_ICON_FAMILY = families[0]

def _load_viewer_fonts() -> None:
    assets = data_path("assets")
    for filename in ("Lato-Regular.ttf", "Lato-Bold.ttf"):
        QFontDatabase.addApplicationFont(str(assets / filename))
    _load_fomantic_icons()

def _fomantic_icon(name: str, size: int = 18, color: str = "#d6d6d6") -> QIcon:
    """Рисует глиф из того же шрифта Fomantic, что использует веб-просмотрщик."""
    glyph = FOMANTIC_ICON_CODES.get(name, "")
    if not glyph or not FOMANTIC_ICON_FAMILY:
        return QIcon()
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    font = QFont(FOMANTIC_ICON_FAMILY)
    font.setPixelSize(size)
    painter.setFont(font)
    painter.setPen(QColor(color))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return QIcon(pixmap)

def _color_swatch_icon(color: str | None) -> QIcon:
    """Возвращает компактный цветной квадрат для выбора цветового фильтра."""
    pixmap = QPixmap(18, 18)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#8c8c8c"), 1))
    painter.setBrush(QColor(color) if color else QColor("#686868"))
    painter.drawRect(QRect(3, 3, 12, 12))
    painter.end()
    return QIcon(pixmap)

def _chrome_icon(kind: str) -> QIcon:
    """Рисует небольшие единообразные значки для элементов оконной рамки."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#d0d0d0"), 2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    if kind == "app":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2492c4"))
        painter.drawEllipse(QRect(4, 4, 24, 24))
        painter.setBrush(QColor("#f19b38"))
        painter.drawPolygon(QPolygon([QPoint(16, 5), QPoint(25, 10), QPoint(18, 16)]))
        painter.setBrush(QColor("#f5f5f5"))
        painter.drawEllipse(QRect(9, 9, 14, 14))
        painter.setBrush(QColor("#273746"))
        painter.drawEllipse(QRect(12, 12, 8, 8))
        painter.setBrush(QColor("#58b9dc"))
        painter.drawEllipse(QRect(14, 14, 4, 4))
    elif kind == "plus":
        painter.drawLine(10, 16, 22, 16)
        painter.drawLine(16, 10, 16, 22)
    elif kind == "minimize":
        painter.drawLine(9, 20, 23, 20)
    elif kind == "maximize":
        painter.drawRect(QRect(10, 10, 12, 12))
    elif kind == "close":
        painter.drawLine(11, 11, 21, 21)
        painter.drawLine(21, 11, 11, 21)
    elif kind == "settings":
        painter.drawEllipse(QRect(10, 10, 12, 12))
        painter.drawEllipse(QRect(14, 14, 4, 4))
        for start, end in (((16, 6), (16, 9)), ((16, 23), (16, 26)), ((6, 16), (9, 16)), ((23, 16), (26, 16))):
            painter.drawLine(*start, *end)
    painter.end()
    return QIcon(pixmap)

def _application_icon() -> QIcon:
    """Загружает основной значок приложения для Windows и заголовка окна."""
    return QIcon(str(data_path("assets") / "ctrlka-icon.ico"))

def _title_bar_icon() -> QIcon:
    """Загружает прозрачный логотип для собственной строки заголовка."""
    return QIcon(str(data_path("assets") / "ctrlka-mark.png"))
