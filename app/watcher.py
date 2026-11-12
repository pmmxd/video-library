from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes

from PySide6.QtCore import QThread, Signal


class LibraryWatcher(QThread):
    library_changed = Signal(int)

    def __init__(self, library_id: int, root: str, parent=None):
        super().__init__(parent)
        self.library_id = library_id
        self.root = root
        self._thread_handle: int | None = None
        self._directory_handle: int | None = None
        self._handle_lock = threading.Lock()

    def stop(self) -> None:
        self.requestInterruption()
        with self._handle_lock:
            thread_handle = self._thread_handle
            directory_handle = self._directory_handle
        if os.name == "nt":
            if thread_handle:
                ctypes.windll.kernel32.CancelSynchronousIo(thread_handle)
            if directory_handle:
                ctypes.windll.kernel32.CancelIoEx(directory_handle, None)

    def run(self) -> None:
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
        kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.ReadDirectoryChangesW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.LPDWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]

        directory = kernel32.CreateFileW(
            self.root,
            0x0001,
            0x0007,
            None,
            3,
            0x02000000,
            None,
        )
        if directory == wintypes.HANDLE(-1).value:
            return
        with self._handle_lock:
            self._directory_handle = directory
        thread_handle = kernel32.OpenThread(0x0001, False, kernel32.GetCurrentThreadId())
        if thread_handle:
            with self._handle_lock:
                self._thread_handle = thread_handle

        buffer = ctypes.create_string_buffer(64 * 1024)
        returned = wintypes.DWORD()
        filter_flags = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x40
        last_notice = 0.0
        try:
            while not self.isInterruptionRequested():
                ok = kernel32.ReadDirectoryChangesW(
                    directory,
                    buffer,
                    64 * 1024,
                    True,
                    filter_flags,
                    ctypes.byref(returned),
                    None,
                    None,
                )
                if not ok:
                    break
                now = time.monotonic()
                if now - last_notice >= 0.5:
                    last_notice = now
                    self.library_changed.emit(self.library_id)
        finally:
            kernel32.CloseHandle(directory)
            with self._handle_lock:
                self._directory_handle = None
                if thread_handle:
                    self._thread_handle = None
            if thread_handle:
                kernel32.CloseHandle(thread_handle)
