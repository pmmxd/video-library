from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import secrets
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter

ITERATIONS = 260_000
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_LOGOFF = 0x5
NOTIFY_FOR_THIS_SESSION = 0


def register_session_lock_notification(window_handle: int) -> bool:
    if os.name != "nt":
        return False
    try:
        register = ctypes.windll.wtsapi32.WTSRegisterSessionNotification
        register.argtypes = (wintypes.HWND, wintypes.DWORD)
        register.restype = wintypes.BOOL
        return bool(
            register(wintypes.HWND(window_handle), NOTIFY_FOR_THIS_SESSION)
        )
    except (AttributeError, OSError):
        return False


def unregister_session_lock_notification(window_handle: int) -> None:
    if os.name != "nt":
        return
    try:
        unregister = ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification
        unregister.argtypes = (wintypes.HWND,)
        unregister.restype = wintypes.BOOL
        unregister(wintypes.HWND(window_handle))
    except (AttributeError, OSError):
        pass


class WindowsSessionLockFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, event_type, message):
        try:
            event_name = bytes(event_type)
        except (TypeError, ValueError):
            event_name = b""
        if event_name == b"windows_generic_MSG":
            try:
                msg = wintypes.MSG.from_address(int(message))
                if (
                    msg.message == WM_WTSSESSION_CHANGE
                    and int(msg.wParam)
                    in {WTS_SESSION_LOCK, WTS_SESSION_LOGOFF}
                ):
                    self._callback()
            except (TypeError, ValueError):
                pass
        return False, 0


def hide_directory(path: Path) -> None:
    if os.name != "nt" or not path.exists():
        return
    get_attributes = ctypes.windll.kernel32.GetFileAttributesW
    set_attributes = ctypes.windll.kernel32.SetFileAttributesW
    attributes = get_attributes(str(path))
    if attributes == 0xFFFFFFFF:
        return
    set_attributes(str(path), attributes | 0x2)


def create_password_record(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        ITERATIONS,
    )
    return json.dumps(
        {
            "version": 1,
            "iterations": ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
            "hash": base64.b64encode(digest).decode("ascii"),
        },
        separators=(",", ":"),
    )


def verify_password(password: str, record: str | None) -> bool:
    if not record:
        return False
    try:
        payload = json.loads(record)
        salt = base64.b64decode(payload["salt"])
        expected = base64.b64decode(payload["hash"])
        iterations = int(payload["iterations"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)
