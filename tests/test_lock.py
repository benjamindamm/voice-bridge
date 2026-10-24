"""Tests für die Instanzsperre."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicebridge.lock import AlreadyRunningError, InstanceLock


class LockTests(unittest.TestCase):
    def test_zweite_sperre_scheitert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loop.lock"
            first = InstanceLock(path)
            first.acquire()
            with self.assertRaises(AlreadyRunningError) as ctx:
                InstanceLock(path).acquire()
            self.assertIn("bereits", str(ctx.exception))
            first.release()

            second = InstanceLock(path)
            second.acquire()
            second.release()

    def test_release_ist_wiederholbar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = InstanceLock(Path(tmp) / "loop.lock")
            lock.acquire()
            lock.release()
            lock.release()

    def test_pid_steht_in_der_datei(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loop.lock"
            lock = InstanceLock(path)
            lock.acquire()
            self.assertEqual(path.read_text().strip(), str(os.getpid()))
            lock.release()


if __name__ == "__main__":
    unittest.main()
