## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Нативное контекстное меню Проводника для одного локального файла Windows."""

from __future__ import annotations

import ctypes
import sys
import uuid
from pathlib import Path


class ShellMenuError(OSError):
    """Ошибка получения или исполнения меню оболочки Windows."""


if sys.platform == "win32":
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def parse(cls, value: str) -> "_GUID":
            raw = uuid.UUID(value).bytes_le
            return cls.from_buffer_copy(raw)


    class _CMINVOKECOMMANDINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.DWORD),
            ("hwnd", wintypes.HWND),
            ("lpVerb", ctypes.c_void_p),
            ("lpParameters", ctypes.c_char_p),
            ("lpDirectory", ctypes.c_char_p),
            ("nShow", ctypes.c_int),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
        ]


    _IID_ISHELL_FOLDER = _GUID.parse("000214E6-0000-0000-C000-000000000046")
    _IID_ICONTEXT_MENU = _GUID.parse("000214E4-0000-0000-C000-000000000046")


def _failed(result: int) -> bool:
    """HRESULT отрицателен, если вызов COM завершился ошибкой."""
    return ctypes.c_int32(result).value < 0


def _check(result: int, operation: str) -> None:
    if _failed(result):
        code = ctypes.c_uint32(result).value
        raise ShellMenuError(code, f"{operation}: HRESULT 0x{code:08X}")


def _com_method(pointer: ctypes.c_void_p, index: int, result_type, *argument_types):
    """Возвращает метод COM по индексу vtable без зависимости от pywin32."""
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *argument_types)
    return prototype(table[index])


def show_file_context_menu(path: Path, window_handle: int, x: int, y: int) -> bool:
    """Показывает меню Проводника и исполняет выбранную команду.

    Возвращает ``False``, если пользователь закрыл меню без выбора. Функция
    вызывается только из главного UI-потока: Shell расширения ожидают STA COM и
    нередко сами создают окна.
    """
    if sys.platform != "win32":
        raise ShellMenuError("Нативное меню Проводника доступно только в Windows")

    target = Path(path)
    if not target.is_file():
        raise ShellMenuError(f"Файл недоступен: {target}")

    # WinDLL оставляет HRESULT вызывающему коду; OleDLL превратил бы
    # RPC_E_CHANGED_MODE в исключение раньше, чем мы успеем его обработать.
    ole32 = ctypes.WinDLL("ole32")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    shell32.SHParseDisplayName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    shell32.SHParseDisplayName.restype = ctypes.c_long
    shell32.SHBindToParent.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHBindToParent.restype = ctypes.c_long

    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.TrackPopupMenuEx.argtypes = [
        wintypes.HMENU,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        ctypes.c_void_p,
    ]
    user32.TrackPopupMenuEx.restype = wintypes.UINT

    initialized = False
    item_id = ctypes.c_void_p()
    parent_folder = ctypes.c_void_p()
    context_menu = ctypes.c_void_p()
    popup = None
    try:
        init_result = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        # RPC_E_CHANGED_MODE означает, что Qt уже инициализировал COM иначе.
        if ctypes.c_uint32(init_result).value != 0x80010106:
            _check(init_result, "CoInitializeEx")
            initialized = True

        _check(
            shell32.SHParseDisplayName(str(target), None, ctypes.byref(item_id), 0, None),
            "SHParseDisplayName",
        )
        child_id = ctypes.c_void_p()
        _check(
            shell32.SHBindToParent(
                item_id,
                ctypes.byref(_IID_ISHELL_FOLDER),
                ctypes.byref(parent_folder),
                ctypes.byref(child_id),
            ),
            "SHBindToParent",
        )

        get_ui_object = _com_method(
            parent_folder,
            10,
            ctypes.c_long,
            wintypes.HWND,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(_GUID),
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(ctypes.c_void_p),
        )
        children = (ctypes.c_void_p * 1)(child_id.value)
        _check(
            get_ui_object(
                parent_folder,
                wintypes.HWND(window_handle),
                1,
                children,
                ctypes.byref(_IID_ICONTEXT_MENU),
                None,
                ctypes.byref(context_menu),
            ),
            "IShellFolder.GetUIObjectOf",
        )

        popup = user32.CreatePopupMenu()
        if not popup:
            raise ctypes.WinError(ctypes.get_last_error())
        first_command = 1
        query_menu = _com_method(
            context_menu,
            3,
            ctypes.c_long,
            wintypes.HMENU,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        )
        _check(query_menu(context_menu, popup, 0, first_command, 0x7FFF, 0), "IContextMenu.QueryContextMenu")

        hwnd = wintypes.HWND(window_handle)
        user32.SetForegroundWindow(hwnd)
        command = user32.TrackPopupMenuEx(popup, 0x0102, x, y, hwnd, None)
        if not command:
            return False

        invoke = _CMINVOKECOMMANDINFO()
        invoke.cbSize = ctypes.sizeof(invoke)
        invoke.hwnd = hwnd
        invoke.lpVerb = command - first_command
        invoke.nShow = 1  # SW_SHOWNORMAL
        invoke_command = _com_method(
            context_menu,
            4,
            ctypes.c_long,
            ctypes.POINTER(_CMINVOKECOMMANDINFO),
        )
        _check(invoke_command(context_menu, ctypes.byref(invoke)), "IContextMenu.InvokeCommand")
        user32.PostMessageW(hwnd, 0, 0, 0)
        return True
    finally:
        if popup:
            user32.DestroyMenu(popup)
        for pointer in (context_menu, parent_folder):
            if pointer.value:
                release = _com_method(pointer, 2, wintypes.ULONG)
                release(pointer)
        if item_id.value:
            ole32.CoTaskMemFree(item_id)
        if initialized:
            ole32.CoUninitialize()
