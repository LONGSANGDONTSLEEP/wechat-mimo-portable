"""Frozen-EXE capture acceptance using only our own synthetic native fixture.

This module never enumerates or selects WeChat.  The only attachment PID comes
from a multiprocessing child that we start, and that child loads only a DLL
whose complete bytes are compared with our deterministic fixture builder.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import multiprocessing
import os
from pathlib import Path
import struct
import sys
import tempfile
import time


class AcceptanceFailure(Exception):
    """A fixed, non-sensitive error code, never an underlying exception text."""


def _send(connection, message):
    connection.send_bytes(json.dumps(message, ensure_ascii=True).encode('ascii'))


def fixture_worker(connection, fixture_path):
    """A spawned fixture target, with no console dependency in windowed builds."""
    try:
        from tests.capture_fixture import FIXTURE_KEY, build_fixture

        path = Path(fixture_path)
        reference = path.with_name('CaptureFixture.reference.dll')
        build_fixture(reference)
        try:
            if path.read_bytes() != reference.read_bytes():
                _send(connection, {'type': 'error', 'code': 'fixture_mismatch'})
                return
        finally:
            reference.unlink(missing_ok=True)
        dll = ctypes.WinDLL(str(path))
        function = dll.CaptureBlock
        function.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        function.restype = ctypes.c_int
        context = ctypes.create_string_buffer(256)
        _send(connection, {'type': 'loaded', 'pid': os.getpid()})
        while True:
            command = connection.recv_bytes(32)
            if command == b'STOP':
                _send(connection, {'type': 'stopped'})
                return
            if command not in (b'VALID', b'INVALID', b'PROBE'):
                _send(connection, {'type': 'error', 'code': 'fixture_protocol'})
                return
            pad = bytes(value ^ 0x36 for value in FIXTURE_KEY) + b'\x36' * 96
            if command != b'VALID':
                pad = pad[:-1] + b'\x35'
            block = ctypes.create_string_buffer(pad, 128)
            result = function(context, block, 1)
            response = {'type': 'returned', 'command': command.decode('ascii'),
                        'result': result}
            if command == b'PROBE':
                response['pristine'] = (
                    ctypes.string_at(ctypes.cast(function, ctypes.c_void_p), 32)
                    == b'\x90' * 32
                )
            _send(connection, response)
    except (EOFError, BrokenPipeError, OSError):
        # Native-loader and IPC failures may contain paths or sensitive bytes.
        try:
            _send(connection, {'type': 'error', 'code': 'fixture_io_failed'})
        except (EOFError, OSError):
            pass
    except Exception:
        try:
            _send(connection, {'type': 'error', 'code': 'fixture_failed'})
        except (EOFError, OSError):
            pass
    finally:
        connection.close()


def _receive(connection, seconds=10):
    try:
        if not connection.poll(seconds):
            raise AcceptanceFailure('fixture_timeout')
        value = json.loads(connection.recv_bytes(8192))
    except (EOFError, OSError, ValueError, UnicodeError):
        raise AcceptanceFailure('fixture_connection_failed') from None
    if not isinstance(value, dict):
        raise AcceptanceFailure('fixture_protocol')
    if value.get('type') == 'error':
        # The child owns this code namespace, but do not relay arbitrary values.
        code = value.get('code')
        if code not in ('fixture_mismatch', 'fixture_protocol', 'fixture_io_failed',
                        'fixture_failed'):
            code = 'fixture_failed'
        raise AcceptanceFailure(code)
    return value


def _command(connection, command):
    try:
        connection.send_bytes(command)
    except (OSError, EOFError):
        raise AcceptanceFailure('fixture_connection_failed') from None
    return _receive(connection)


def _wait_capture(capture, wanted, seconds=10):
    expires = time.monotonic() + seconds
    while time.monotonic() < expires:
        try:
            event = capture.next_event(timeout=.2)
        except Exception:
            raise AcceptanceFailure('capture_connection_failed') from None
        if event is None:
            continue
        kind, value = event
        if kind == 'error':
            # Only an explicitly enumerated safe code can enter the report.
            safe_codes = ('attach_denied', 'process_exited', 'module_changed',
                          'hook_failed', 'candidate_limit', 'agent_protocol',
                          'agent_error', 'capture_failed')
            raise AcceptanceFailure(value if value in safe_codes else 'capture_failed')
        if kind == wanted:
            return value
        if kind == 'stopped':
            raise AcceptanceFailure('capture_stopped_early')
    raise AcceptanceFailure('capture_timeout')


class _WorkerWatch:
    """A SYNCHRONIZE-only handle survives multiprocessing.Process.close()."""
    def __init__(self, pid):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                           wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x100000, False, int(pid))
        if not self.handle:
            raise AcceptanceFailure('capture_helper_unobservable')

    def exited(self):
        return self.kernel.WaitForSingleObject(self.handle, 1000) == 0

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run(root):
    """Run actual native capture, write a safe JSON report, and return that dict.

    The launcher must call multiprocessing.freeze_support() before dispatching
    here.  Spawn reuses the frozen executable; no installed Python is required.
    """
    report = {'ok': False, 'synthetic_only': True, 'wechat_attached': False,
              'frozen': bool(getattr(sys, 'frozen', False)), 'checks': []}
    current_check = 'environment'
    target = None
    connection = None
    watch = None
    temporary = None

    def passed(name):
        report['checks'].append({'name': name, 'ok': True})

    try:
        if os.name != 'nt' or struct.calcsize('P') != 8:
            raise AcceptanceFailure('windows_x64_required')
        import frida
        from capture_locator import resolve_image
        from login_capture import CaptureSession
        from tests.capture_fixture import (
            BLOCK_ENTRY_RVA, FIXTURE_KEY, IMAGE_SIZE, TABLE_RVA, build_fixture,
        )
        report['frida_version'] = str(frida.__version__)
        passed(current_check)

        current_check = 'synthetic_fixture'
        temporary = tempfile.TemporaryDirectory(prefix='studio-capture-selftest-')
        path = build_fixture(Path(temporary.name) / 'CaptureFixture.dll')
        plan = resolve_image(path, allow_test_fixture=True)
        if (plan.get('entry_rvas') != [BLOCK_ENTRY_RVA]
                or plan.get('table_rvas') != [TABLE_RVA]
                or plan.get('image_size') != IMAGE_SIZE
                or plan.get('test_fixture') is not True):
            raise AcceptanceFailure('fixture_plan_mismatch')
        passed(current_check)

        current_check = 'owned_target_started'
        context = multiprocessing.get_context('spawn')
        connection, child_connection = context.Pipe(duplex=True)
        target = context.Process(target=fixture_worker,
                                 args=(child_connection, str(path)), daemon=True)
        target.start()
        child_connection.close()
        loaded = _receive(connection)
        if (loaded.get('type') != 'loaded'
                or type(loaded.get('pid')) is not int
                or loaded['pid'] != target.pid):
            raise AcceptanceFailure('fixture_identity_mismatch')
        passed(current_check)

        current_check = 'native_hook_ready'
        with CaptureSession(target.pid, plan) as capture:
            _wait_capture(capture, 'ready')
            watch = _WorkerWatch(capture.process.pid)
            passed(current_check)

            current_check = 'invalid_pad_excluded'
            response = _command(connection, b'INVALID')
            if response != {'type': 'returned', 'command': 'INVALID', 'result': 1}:
                raise AcceptanceFailure('fixture_call_failed')
            expires = time.monotonic() + .7
            while time.monotonic() < expires:
                try:
                    event = capture.next_event(timeout=.1)
                except Exception:
                    raise AcceptanceFailure('capture_connection_failed') from None
                if event is not None:
                    if event[0] == 'candidate':
                        raise AcceptanceFailure('invalid_pad_accepted')
                    if event[0] in ('error', 'stopped'):
                        raise AcceptanceFailure('capture_failed')
            passed(current_check)

            current_check = 'binary_candidate_captured'
            response = _command(connection, b'VALID')
            if response != {'type': 'returned', 'command': 'VALID', 'result': 1}:
                raise AcceptanceFailure('fixture_call_failed')
            value = _wait_capture(capture, 'candidate')
            if not isinstance(value, bytes) or len(value) != 32 or value != FIXTURE_KEY:
                raise AcceptanceFailure('candidate_mismatch')
            # Only the result of comparison reaches the report, never the value.
            del value
            passed(current_check)
            current_check = 'cancel_detach'

        passed(current_check)
        current_check = 'capture_helper_exited'
        if watch is None or not watch.exited():
            raise AcceptanceFailure('capture_helper_still_running')
        passed(current_check)

        current_check = 'native_entry_restored'
        expires = time.monotonic() + 3
        while True:
            response = _command(connection, b'PROBE')
            if response.get('type') != 'returned' or response.get('result') != 1:
                raise AcceptanceFailure('fixture_probe_failed')
            if response.get('pristine') is True:
                break
            if time.monotonic() >= expires:
                raise AcceptanceFailure('native_entry_not_restored')
            time.sleep(.03)
        passed(current_check)

        current_check = 'owned_target_clean_exit'
        if _command(connection, b'STOP') != {'type': 'stopped'}:
            raise AcceptanceFailure('fixture_stop_failed')
        target.join(5)
        if target.is_alive() or target.exitcode != 0:
            raise AcceptanceFailure('fixture_exit_failed')
        passed(current_check)
        report['ok'] = True
    except AcceptanceFailure as failure:
        report['checks'].append({'name': current_check, 'ok': False,
                                 'error': str(failure)})
    except Exception:
        # Frida/native/loader exception text is intentionally not recorded.
        report['checks'].append({'name': current_check, 'ok': False,
                                 'error': 'capture_self_test_failed'})
    finally:
        if watch is not None:
            watch.close()
        if target is not None:
            try:
                if target.is_alive():
                    try:
                        connection.send_bytes(b'STOP')
                    except (OSError, EOFError, AttributeError):
                        pass
                    target.join(2)
                    if target.is_alive():
                        target.terminate()
                        target.join(3)
                if not target.is_alive():
                    target.close()
            except Exception:
                report['ok'] = False
                report['checks'].append({'name': 'owned_target_cleanup', 'ok': False,
                                         'error': 'fixture_cleanup_failed'})
        if connection is not None:
            connection.close()
        if temporary is not None:
            try:
                temporary.cleanup()
            except OSError:
                report['ok'] = False
                report['checks'].append({'name': 'temporary_cleanup', 'ok': False,
                                         'error': 'fixture_cleanup_failed'})
    destination = Path(root)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'capture-self-test.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
