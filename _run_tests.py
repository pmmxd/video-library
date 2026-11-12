"""在 DSH 受限沙箱下跑测试：tempfile 建的目录无法访问，改为 os.makedirs。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp", "tests")
_COUNTER = [0]


class _TempDir:
    def __init__(self, *args, **kwargs):
        _COUNTER[0] += 1
        os.makedirs(_BASE, exist_ok=True)
        self.name = os.path.join(_BASE, f"case{_COUNTER[0]}")
        if os.path.isdir(self.name):
            shutil.rmtree(self.name, ignore_errors=True)
        os.makedirs(self.name)

    def cleanup(self) -> None:
        return

    def __enter__(self):
        return self.name

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.name, ignore_errors=True)
        return None


def _mkdtemp(*args, **kwargs):
    return _TempDir().name


tempfile.TemporaryDirectory = _TempDir
tempfile.mkdtemp = _mkdtemp

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
