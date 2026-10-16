"""Synthetic authentication/lifecycle checks; never attach to a real process."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import localdb
import login_capture
from web_selftest import empty_sqlite, encrypt_page


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now


class ScriptedCapture:
    """A deterministic event source in place of the native capture helper."""
    def __init__(self, clock, events, idle_step=1):
        self.clock = clock
        self.events = list(events)
        self.idle_step = idle_step
        self.entered = False
        self.closed = False
        self.exit_error = None
        self.polls = 0

    def __enter__(self):
        self.entered = True
        return self

    def next_event(self, timeout=.15):
        self.polls += 1
        if self.polls > 2000:
            raise AssertionError('Capture loop exceeded the deterministic fixture budget')
        delay, event = self.events.pop(0) if self.events else (self.idle_step, None)
        self.clock.now += delay
        if isinstance(event, Exception):
            raise event
        return event

    def __exit__(self, kind, error, traceback):
        self.closed = True
        self.exit_error = error


class LoginCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = FakeClock()
        self.progress = []
        self.plan = {'target': {'pid': 12345, 'image': 'C:/synthetic/Weixin.exe', 'created': 987654}}
        self.ready = ('ready', {'entries': 2})
        self.key_a = bytes(range(32))
        self.key_b = bytes(range(32, 64))
        self.salt_a = b'fixture-salt1234'
        self.salt_b = b'another-salt1234'

    def database(self, name='message_0.db', key=None, salt=None):
        key = self.key_a if key is None else key
        salt = self.salt_a if salt is None else salt
        self.assertEqual(len(salt), 16)
        path = self.root / name
        page = encrypt_page(empty_sqlite(), 1, key, salt)
        path.write_bytes(page)
        self.assertTrue(localdb.verify_key(page, key))
        return {'path': str(path), 'name': name, 'encrypted': True}

    def run_capture(self, files, events, *, cancelled=lambda: False, timeout=180, idle_step=1, identity=None):
        session = ScriptedCapture(self.clock, events, idle_step)
        factory = Mock(return_value=session)
        identity = identity if identity is not None else Mock(return_value=self.plan['target'])
        self.session = session
        self.factory = factory
        with patch('login_capture.CaptureSession', factory), patch('login_capture.time.monotonic', self.clock.monotonic), patch('login_capture.ensure_same_process', identity):
            return login_capture.capture_keys(12345, self.plan, files, self.progress.append, cancelled, timeout=timeout)

    def assert_no_material_in_text(self, *texts):
        combined = '\n'.join([*self.progress, *(str(text) for text in texts)])
        for secret in (self.key_a, self.key_b):
            self.assertNotIn(secret.hex(), combined)
            self.assertNotIn(repr(secret), combined)

    def test_ready_deadline_fails_before_waiting_for_login_and_closes_session(self):
        file = self.database()
        with self.assertRaisesRegex(ValueError, '25秒内就绪') as failure:
            self.run_capture([file], [(26, None)])
        self.assertTrue(self.session.entered)
        self.assertTrue(self.session.closed)
        self.assertLess(self.clock.now, 130)
        self.assertFalse(any('采集已就绪' in line for line in self.progress))
        self.assert_no_material_in_text(failure.exception)

    def test_cancel_before_ready_closes_without_emitting_ready_or_result(self):
        with self.assertRaisesRegex(ValueError, '登录采集已停止') as failure:
            self.run_capture([self.database()], [], cancelled=lambda: True)
        self.assertTrue(self.session.closed)
        self.assertEqual(self.session.polls, 0)
        self.assertFalse(any('采集已就绪' in line for line in self.progress))
        self.assert_no_material_in_text(failure.exception)

    def test_ready_then_timeout_without_authentication_is_failure(self):
        with self.assertRaisesRegex(ValueError, '没有密钥通过所选账号数据库认证') as failure:
            self.run_capture([self.database()], [(1, self.ready)], timeout=30, idle_step=6)
        self.assertTrue(self.session.closed)
        self.assertTrue(any('采集已就绪' in line for line in self.progress))
        self.assertFalse(any('已认证 1/' in line for line in self.progress))
        self.assert_no_material_in_text(failure.exception)

    def test_wrong_candidate_is_neither_returned_nor_reported_as_authenticated(self):
        wrong = bytes([225]) * 32
        self.assertEqual(len(wrong), 32)
        with patch('login_capture.localdb.derive_key', return_value=wrong):
            with self.assertRaisesRegex(ValueError, '没有密钥通过所选账号数据库认证') as failure:
                self.run_capture([self.database()], [(1, self.ready), (1, ('candidate', wrong))], timeout=30, idle_step=6)
        self.assertTrue(self.session.closed)
        self.assertFalse(any('已认证 1/' in line for line in self.progress))
        self.assertNotIn(wrong.hex(), '\n'.join(self.progress) + str(failure.exception))
        self.assert_no_material_in_text(failure.exception)

    def test_direct_candidate_is_authenticated_without_unnecessary_derivation(self):
        file = self.database()
        original = Path(file['path']).read_bytes()
        with patch('login_capture.localdb.derive_key') as derive:
            found = self.run_capture([file], [(1, self.ready), (1, ('candidate', self.key_a))])
        derive.assert_not_called()
        self.assertEqual(found, {self.salt_a.hex(): self.key_a})
        self.assertEqual(Path(file['path']).read_bytes(), original)
        self.assertTrue(self.session.closed)
        self.assert_no_material_in_text()

    def test_candidate_before_ready_is_rejected_even_when_it_authenticates_the_database(self):
        with patch('login_capture.localdb.derive_key') as derive:
            with self.assertRaises(ValueError) as failure:
                self.run_capture([self.database()], [(1, ('candidate', self.key_a)), (1, self.ready)])
        self.assertEqual(str(failure.exception), login_capture.ERRORS['agent_protocol'])
        derive.assert_not_called()
        self.assertTrue(self.session.closed)
        self.assertFalse(any('已认证' in line for line in self.progress))
        self.assertFalse(any('采集已就绪' in line for line in self.progress))
        self.assert_no_material_in_text(failure.exception)

    def test_material_is_derived_against_the_selected_database_salt(self):
        material = bytes(reversed(range(32)))
        derived = localdb.derive_key(material, self.salt_a)
        file = self.database(key=derived)
        original_derive = localdb.derive_key
        with patch('login_capture.localdb.derive_key', wraps=original_derive) as derive:
            found = self.run_capture([file], [(1, self.ready), (1, ('candidate', material))])
        derive.assert_called_once_with(material, self.salt_a, 4)
        self.assertEqual(found, {self.salt_a.hex(): derived})
        self.assertTrue(self.session.closed)
        self.assertNotIn(material.hex(), '\n'.join(self.progress))
        self.assertNotIn(derived.hex(), '\n'.join(self.progress))

    def test_multiple_database_salts_require_independent_authentication(self):
        first = self.database()
        second = self.database('message_1.db', self.key_b, self.salt_b)
        found = self.run_capture([first, second], [(1, self.ready), (1, ('candidate', self.key_a)), (1, ('candidate', self.key_b))])
        self.assertEqual(found, {self.salt_a.hex(): self.key_a, self.salt_b.hex(): self.key_b})
        self.assertTrue(self.session.closed)
        self.assertTrue(any('已认证 2/2' in line for line in self.progress))
        self.assert_no_material_in_text()

    def test_one_candidate_can_be_direct_for_one_salt_and_derived_for_another(self):
        first = self.database()
        derived = localdb.derive_key(self.key_a, self.salt_b)
        second = self.database('message_1.db', derived, self.salt_b)
        original_derive = localdb.derive_key
        with patch('login_capture.localdb.derive_key', wraps=original_derive) as derive:
            found = self.run_capture([first, second], [(1, self.ready), (1, ('candidate', self.key_a))])
        self.assertEqual(found, {self.salt_a.hex(): self.key_a, self.salt_b.hex(): derived})
        derive.assert_called_once_with(self.key_a, self.salt_b, 4)
        self.assertTrue(self.session.closed)
        self.assertTrue(any('已认证 2/2' in line for line in self.progress))
        self.assertNotIn(derived.hex(), '\n'.join(self.progress))
        self.assert_no_material_in_text()

    def test_duplicate_candidates_are_not_rederived(self):
        wrong = bytes([255]) * 32
        original_derive = localdb.derive_key
        with patch('login_capture.localdb.derive_key', wraps=original_derive) as derive:
            found = self.run_capture([self.database()], [(1, self.ready), (1, ('candidate', wrong)), (1, ('candidate', wrong)), (1, ('candidate', self.key_a))])
        self.assertEqual(found, {self.salt_a.hex(): self.key_a})
        derive.assert_called_once_with(wrong, self.salt_a, 4)
        self.assertTrue(self.session.closed)

    def test_changed_process_identity_stops_before_accepting_later_candidates(self):
        identity = Mock(side_effect=ValueError(login_capture.ERRORS['process_exited']))
        with self.assertRaisesRegex(ValueError, '没有自动切换到其他账号') as failure:
            self.run_capture([self.database()], [(1, self.ready), (1, ('candidate', self.key_a))], identity=identity)
        identity.assert_called_once_with(12345, self.plan['target'])
        self.assertTrue(self.session.closed)
        self.assertEqual(self.session.polls, 1)
        self.assert_no_material_in_text(failure.exception)

    def test_partial_authentication_exits_after_grace_period_with_only_verified_keys(self):
        first = self.database()
        second = self.database('message_1.db', self.key_b, self.salt_b)
        found = self.run_capture([first, second], [(1, self.ready), (1, ('candidate', self.key_a))], idle_step=6)
        self.assertEqual(found, {self.salt_a.hex(): self.key_a})
        self.assertLess(self.clock.now, 140)
        self.assertTrue(self.session.closed)
        self.assertFalse(any('已认证 2/2' in line for line in self.progress))
        self.assert_no_material_in_text()

    def test_cancel_after_partial_authentication_discards_the_partial_result(self):
        first = self.database()
        second = self.database('message_1.db', self.key_b, self.salt_b)
        with self.assertRaisesRegex(ValueError, '登录采集已停止') as failure:
            self.run_capture([first, second], [(1, self.ready), (1, ('candidate', self.key_a))], cancelled=lambda: self.clock.now >= 103)
        self.assertTrue(self.session.closed)
        self.assertTrue(any('已认证 1/2' in line for line in self.progress))
        self.assert_no_material_in_text(failure.exception)

    def test_worker_error_after_partial_authentication_is_failure_not_partial_success(self):
        first = self.database()
        second = self.database('message_1.db', self.key_b, self.salt_b)
        with self.assertRaisesRegex(ValueError, '采集入口无法完整安装') as failure:
            self.run_capture([first, second], [(1, self.ready), (1, ('candidate', self.key_a)), (1, ('error', 'hook_failed'))])
        self.assertTrue(self.session.closed)
        self.assert_no_material_in_text(failure.exception)

    def test_unknown_worker_error_is_sanitized_instead_of_echoing_internal_values(self):
        secret_message = 'internal key=' + self.key_a.hex()
        with self.assertRaises(ValueError) as failure:
            self.run_capture([self.database()], [(1, ('error', secret_message))])
        self.assertEqual(str(failure.exception), login_capture.ERRORS['capture_failed'])
        self.assertTrue(self.session.closed)
        self.assert_no_material_in_text(failure.exception)

    def test_incomplete_or_empty_database_selection_never_starts_helper(self):
        file = self.database()
        Path(file['path']).write_bytes(b'not-a-full-page')
        with patch('login_capture.CaptureSession') as factory:
            with self.assertRaisesRegex(ValueError, '首页不完整'):
                login_capture.capture_keys(12345, self.plan, [file], self.progress.append, lambda: False)
            with self.assertRaisesRegex(ValueError, '没有需要认证'):
                login_capture.capture_keys(12345, self.plan, [], self.progress.append, lambda: False)
        factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
