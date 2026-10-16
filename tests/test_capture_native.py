"""Real Frida acceptance against only our own harmless, synthetic child DLL."""

from __future__ import annotations

import importlib.util
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests.capture_fixture import (
    BLOCK_ENTRY_RVA, FIXTURE_KEY, IMAGE_SIZE, SHA512_TABLE, TABLE_RVA,
    build_fixture,
)


class FixtureStructureTests(unittest.TestCase):
    def test_fixture_is_deterministic_import_free_and_has_exact_unwind_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            first = build_fixture(Path(directory) / 'CaptureFixture.dll').read_bytes()
            second = build_fixture(Path(directory) / 'again.dll').read_bytes()
        self.assertEqual(first, second)
        self.assertEqual(first[:2], b'MZ')
        self.assertEqual(first[0x80:0x84], b'PE\0\0')
        self.assertEqual(struct.unpack_from('<H', first, 0x84)[0], 0x8664)
        self.assertEqual(struct.unpack_from('<H', first, 0x98)[0], 0x20B)
        self.assertEqual(struct.unpack_from('<I', first, 0x98 + 56)[0], IMAGE_SIZE)
        self.assertEqual(first[0x600:0x600 + 640], SHA512_TABLE)
        self.assertEqual(struct.unpack_from('<II', first, 0x98 + 120), (0, 0))
        entry, end, unwind = struct.unpack_from('<III', first, 0xA00 + 12)
        self.assertEqual((entry, end, unwind), (BLOCK_ENTRY_RVA, 0x106D, 0x4004))
        self.assertEqual(first[0x440:0x460], b'\x90' * 32)
        displacement = struct.unpack_from('<i', first, 0x463)[0]
        self.assertEqual(0x1067 + displacement, TABLE_RVA)
        self.assertEqual(first[0xC04:0xC08], b'\x01\x00\x00\x00')


@unittest.skipUnless(
    os.name == 'nt' and struct.calcsize('P') == 8
    and importlib.util.find_spec('frida') is not None,
    'requires Windows x64 and Frida; never attaches to a user application',
)
class NativeCaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = build_fixture(Path(self.directory.name) / 'CaptureFixture.dll')
        child_script = Path(__file__).with_name('capture_fixture.py')
        # Windows venv python.exe can be a redirector which spawns a different
        # PID.  The stdlib-only dummy uses the underlying interpreter so the
        # Popen PID is exactly the process we own and later attach to.
        child_python = getattr(sys, '_base_executable', sys.executable)
        self.child = subprocess.Popen(
            [child_python, '-u', str(child_script), '--child', str(self.fixture)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding='utf-8', errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        self.addCleanup(self._stop_child)
        self.lines = queue.Queue()

        def read_lines():
            for line in self.child.stdout:
                try:
                    self.lines.put(json.loads(line))
                except (ValueError, TypeError):
                    self.lines.put({'kind': 'bad_child_output'})
            self.lines.put({'kind': 'eof'})

        self.reader = threading.Thread(target=read_lines, daemon=True)
        self.reader.start()
        ready = self._child_line()
        self.assertEqual(ready['kind'], 'loaded', ready)
        self.assertEqual(ready['pid'], self.child.pid)
        self.assertEqual(Path(ready['module']), self.fixture)

    def _child_line(self):
        try:
            return self.lines.get(timeout=10)
        except queue.Empty:
            self.fail('owned synthetic child did not respond within 10 seconds')

    def _command(self, command):
        self.child.stdin.write(json.dumps({'command': command}) + '\n')
        self.child.stdin.flush()
        return self._child_line()

    def _stop_child(self):
        if self.child.poll() is None:
            try:
                self.child.stdin.write('{"command":"stop"}\n')
                self.child.stdin.flush()
                self.child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.child.terminate()
                self.child.wait(timeout=5)
        for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
            stream.close()

    def _wait_capture_event(self, capture, wanted, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            event = capture.next_event(timeout=.2)
            if event is None:
                continue
            kind, value = event
            if kind == 'error':
                self.fail(f'synthetic capture reported error: {value!r}')
            if kind == wanted:
                return value
        self.fail(f'capture did not produce {wanted!r} within {seconds} seconds')

    def _watch_worker(self, capture):
        """Keep only a synchronization handle to our own capture helper."""
        worker_pid = capture.process.pid
        self.assertNotEqual(worker_pid, self.child.pid)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x100000, False, worker_pid)  # SYNCHRONIZE only
        self.assertTrue(handle, 'could not observe our own capture helper')
        self.addCleanup(kernel.CloseHandle, handle)
        return kernel, handle

    def _assert_detached(self, worker):
        kernel, handle = worker
        self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0,
                         'our capture helper did not exit after close')
        # Abrupt helper death can require a short asynchronous Frida transport
        # cleanup. Every intermediate probe must still be callable and return 1.
        deadline = time.monotonic() + 3
        while True:
            probe = self._command('probe')
            self.assertEqual(probe.get('result'), 1, probe)
            if probe.get('entry_pristine'):
                break
            if time.monotonic() >= deadline:
                self.fail('fixture entry was not restored after helper exit')
            time.sleep(.03)
        self.assertIsNone(self.child.poll())

    def test_real_hook_captures_only_synthetic_ipad_and_detaches_cleanly(self):
        # Imports remain inside the test: the fixture builder can be checked
        # before the production locator/session implementations are ready.
        from capture_locator import resolve_image
        from login_capture import CaptureSession

        plan = resolve_image(self.fixture, allow_test_fixture=True)
        self.assertIn(TABLE_RVA, plan['table_rvas'])
        self.assertIn(BLOCK_ENTRY_RVA, plan['entry_rvas'])
        self.assertEqual(plan['image_size'], IMAGE_SIZE)
        with CaptureSession(self.child.pid, plan) as capture:
            self._wait_capture_event(capture, 'ready')
            worker = self._watch_worker(capture)
            self.assertEqual(self._command('invalid'),
                             {'kind': 'returned', 'command': 'invalid', 'result': 1})
            deadline = time.monotonic() + .7
            while time.monotonic() < deadline:
                event = capture.next_event(timeout=.1)
                if event:
                    self.assertNotIn(event[0], ('candidate', 'error'), event)
            self.assertEqual(self._command('valid'),
                             {'kind': 'returned', 'command': 'valid', 'result': 1})
            self.assertEqual(self._wait_capture_event(capture, 'candidate'), FIXTURE_KEY)
        # A second real native call after context-manager detach proves the
        # owned process is alive and its function remains executable.
        self._assert_detached(worker)
        self.assertEqual(self._command('valid'),
                         {'kind': 'returned', 'command': 'valid', 'result': 1})
        self.assertIsNone(self.child.poll())
        self.assertEqual(self._command('stop'), {'kind': 'stopped'})
        self.assertEqual(self.child.wait(timeout=5), 0)

    def test_context_exception_detaches_and_restores_owned_fixture(self):
        from capture_locator import resolve_image
        from login_capture import CaptureSession

        plan = resolve_image(self.fixture, allow_test_fixture=True)
        with self.assertRaisesRegex(ValueError, 'controlled fixture exception'):
            with CaptureSession(self.child.pid, plan) as capture:
                self._wait_capture_event(capture, 'ready')
                worker = self._watch_worker(capture)
                raise ValueError('controlled fixture exception')
        self._assert_detached(worker)
        self.assertEqual(self._command('stop'), {'kind': 'stopped'})
        self.assertEqual(self.child.wait(timeout=5), 0)

    def test_terminated_owned_helper_restores_fixture_and_leaves_child_alive(self):
        from capture_locator import resolve_image
        from login_capture import CaptureSession

        plan = resolve_image(self.fixture, allow_test_fixture=True)
        with CaptureSession(self.child.pid, plan) as capture:
            self._wait_capture_event(capture, 'ready')
            worker = self._watch_worker(capture)
            # Kill only the exact helper this context manager created. The
            # synthetic target and every real user application remain alive.
            capture.process.terminate()
            capture.process.join(3)
        self._assert_detached(worker)
        self.assertEqual(self._command('stop'), {'kind': 'stopped'})
        self.assertEqual(self.child.wait(timeout=5), 0)


if __name__ == '__main__':
    unittest.main()
