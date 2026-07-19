## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Привязывает дочерние процессы приложения к времени жизни GUI на Windows."""

from __future__ import annotations

import ctypes
import os
import signal
import time
from ctypes import wintypes


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _BasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


_job_handle: int | None = None
_posix_guard_pid: int | None = None


def install_process_tree_guard() -> bool:
    """Гарантирует уничтожение всего дерева приложения после выхода GUI.

    Windows использует наследуемый Job Object. Linux и macOS — отдельную группу
    процессов и сторожа, который переживает GUI только для завершения группы.
    """
    if os.name == "nt":
        return _install_windows_process_tree_guard()
    if os.name == "posix":
        return _install_posix_process_tree_guard()
    return False


def _install_windows_process_tree_guard() -> bool:
    """Помещает GUI в Job Object, автоматически наследуемый его потомками."""
    global _job_handle
    if _job_handle is not None:
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return False
    limits = _ExtendedLimitInformation()
    limits.basic_limit_information.limit_flags = (
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
    )
    configured = kernel32.SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job, kernel32.GetCurrentProcess()
    )
    if not assigned:
        kernel32.CloseHandle(job)
        return False
    _job_handle = int(job)
    return True


def _install_posix_process_tree_guard() -> bool:
    """Запускает сторожа собственной группы процессов приложения."""
    global _posix_guard_pid
    if _posix_guard_pid is not None:
        return True
    if not hasattr(os, "fork") or not hasattr(os, "killpg"):
        return False
    try:
        # Не затрагиваем терминал и uv: в группу входят только GUI и процессы,
        # которые он создаст после установки защиты.
        if os.getpgrp() != os.getpid():
            os.setpgid(0, 0)
        group_id = os.getpgrp()
        parent_pid = os.getpid()
        guard_pid = os.fork()
    except OSError:
        return False
    if guard_pid:
        _posix_guard_pid = guard_pid
        return True

    # Сторож ничего не импортирует и не запускает: после раннего fork он только
    # ждёт переподчинения init, поэтому безопасен до создания потоков Qt.
    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while os.getppid() == parent_pid:
            time.sleep(0.25)
        try:
            os.killpg(group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
        time.sleep(1.0)
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    finally:
        os._exit(0)
