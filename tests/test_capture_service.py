"""Capture API integration with synthetic data and no native process attachment."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.request

from web_service import Server, State
from web_selftest import empty_sqlite, encrypt_page
import workspace_backup


class CaptureServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.account = self.root / 'synthetic-account' / 'db_storage'
        self.account.mkdir(parents=True)
        self.other = self.root / 'other-synthetic-account' / 'db_storage'
        self.other.mkdir(parents=True)
        self.key = bytes(range(32))
        self.salt = b'fixture-salt1234'
        self.database = self.account / 'message_0.db'
        self.database.write_bytes(encrypt_page(empty_sqlite(), 1, self.key, self.salt))
        self.files = [{'path': str(self.database), 'name': self.database.name, 'encrypted': True}]
        self.plan = {'entry_rvas': [4096, 8192], 'module_name': 'Weixin.dll',
                     'module_path': 'C:/synthetic-installation/Weixin.dll', 'file_version': '4.1.15.fixture',
                     'target': {'pid': 12345, 'image': 'C:/synthetic-installation/Weixin.exe', 'created': 987654},
                     'private_marker': 'plan-internal-' + self.key.hex()}
        self.params = {'path': str(self.account), 'pid': 12345, 'version': 4, 'self_name': 'wxid_fixture'}
        self.state = State(self.root / 'workspace', Path(__file__).parent.parent / 'web')

    def event(self):
        value = threading.Event()
        self.addCleanup(value.set)
        return value

    def wait_job(self, identity):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            entry = self.state.jobs[identity]
            if entry['state'] not in ('running', 'pending'):
                return entry
            time.sleep(.005)
        self.fail('Synthetic background job did not finish within 5 seconds')

    def prepare(self):
        with patch('login_capture.preflight', return_value=(self.plan, self.files)):
            return self.state.api('/api/capture-preflight', self.params)

    def cache(self):
        self.state.database_scope(self.account, 4)
        self.state.keys[self.salt.hex()] = self.key
        self.state.capture_prepared = {'synthetic': 'prepared-value'}

    def assert_cache_empty(self):
        self.assertEqual(self.state.keys, {})
        self.assertIsNone(self.state.key_scope)
        self.assertIsNone(self.state.capture_prepared)

    def assert_no_secret(self, value):
        text = json.dumps(value, ensure_ascii=False, default=str)
        self.assertNotIn(self.key.hex(), text)
        self.assertNotIn(repr(self.key), text)
        self.assertNotIn(self.plan['private_marker'], text)

    def test_preflight_does_not_start_capture_and_returns_only_safe_metadata(self):
        with patch('login_capture.preflight', return_value=(self.plan, self.files)) as check, patch('login_capture.capture_keys') as capture, patch('login_capture.ensure_same_process') as identity:
            result = self.state.api('/api/capture-preflight', self.params)
        check.assert_called_once_with(12345, self.account.resolve(), 4)
        capture.assert_not_called()
        identity.assert_not_called()
        self.assertEqual(self.state.jobs, {})
        self.assertTrue(result['supported'])
        self.assertEqual(result['entry_count'], 2)
        self.assertEqual(result['databases'], {'encrypted': 1, 'total': 1})
        self.assertEqual(result['process']['pid'], 12345)
        self.assertIn('尚未安装采集器', result['note'])
        self.assertNotIn('plan', result)
        for internal in ('entry_rvas', 'module_path', 'target', 'private_marker'):
            self.assertNotIn(internal, json.dumps(result))
        self.assert_no_secret(result)
        self.assert_no_secret(self.state.api('/api/state', {}))

    def test_expired_or_mismatched_preflight_never_starts_capture(self):
        changes = ({'pid': 12346}, {'path': str(self.other)}, {'version': 3}, {})
        for change in changes:
            with self.subTest(change=change):
                self.prepare()
                if not change:
                    self.state.capture_prepared['expires'] = time.monotonic() - 1
                with patch('login_capture.capture_keys') as capture:
                    with self.assertRaises(ValueError):
                        self.state.api('/api/capture-import', dict(self.params, **change))
                capture.assert_not_called()
                self.assertEqual(self.state.jobs, {})

    def test_preflight_holds_the_job_lock_until_its_bound_plan_is_published(self):
        checking, release = self.event(), self.event()
        results, failures = [], []
        def inspect(*unused):
            checking.set()
            if not release.wait(3):
                raise AssertionError('Synthetic preflight release was not signaled')
            return self.plan, self.files
        def request():
            try:
                results.append(self.state.api('/api/capture-preflight', self.params))
            except Exception as error:
                failures.append(error)
        with patch('login_capture.preflight', side_effect=inspect):
            thread = threading.Thread(target=request)
            thread.start()
            try:
                self.assertTrue(checking.wait(2))
                acquired = self.state.job_lock.acquire(blocking=False)
                if acquired:
                    self.state.job_lock.release()
                self.assertFalse(acquired, 'Another job could race with the preflight account binding')
            finally:
                release.set()
                thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['supported'])
        self.assertEqual(self.state.capture_prepared['scope'], (os.path.normcase(str(self.account.resolve())), 4))

    def test_capture_hands_only_verified_cache_to_import_without_forwarding_a_manual_key(self):
        self.prepare()
        input_params = dict(self.params, key='a-manual-value-that-must-not-be-forwarded', auto_key=True, timeout=180)
        received = []
        def import_rows(params, progress):
            received.append(dict(params))
            self.assertEqual(self.state.keys, {self.salt.hex(): self.key})
            return {'inserted': 2, 'coverage': [{'database': 'message_0.db', 'messages': 2}], 'warnings': []}
        found = {self.salt.hex(): self.key}
        with patch('login_capture.capture_keys', return_value=found) as capture, patch('login_capture.ensure_same_process') as identity, patch.object(self.state, 'local_import', side_effect=import_rows):
            job = self.state.api('/api/capture-import', input_params)['job']
            entry = self.wait_job(job)
        self.assertEqual(entry['state'], 'success', entry['error'])
        identity.assert_called_once_with(12345, self.plan['target'])
        self.assertEqual(capture.call_args.args[:2], (12345, self.plan))
        self.assertEqual([{key: entry[key] for key in ('path', 'name', 'encrypted')} for entry in capture.call_args.args[2]], self.files)
        self.assertEqual(received, [{'path': str(self.account.resolve()), 'version': 4, 'self_name': 'wxid_fixture', 'auto_key': False}])
        self.assertEqual(entry['result']['capture'], {'authenticated': 1, 'databases': 1})
        self.assertEqual(entry['result']['inserted'], 2)
        self.assertIsNone(self.state.capture_prepared)
        self.assertEqual(found, {})
        self.assert_no_secret(entry)
        self.assert_no_secret(self.state.api('/api/state', {}))

    def test_capture_job_keeps_slot_until_helper_returns_after_its_cleanup(self):
        self.prepare()
        detaching, release = self.event(), self.event()
        importer = Mock(return_value={'inserted': 1, 'coverage': [], 'warnings': []})
        def capture(*args):
            try:
                return {self.salt.hex(): self.key}
            finally:
                detaching.set()
                if not release.wait(3):
                    raise AssertionError('Synthetic detach release was not signaled')
        with patch('login_capture.capture_keys', side_effect=capture), patch('login_capture.ensure_same_process'), patch.object(self.state, 'local_import', importer):
            job = self.state.api('/api/capture-import', self.params)['job']
            try:
                self.assertTrue(detaching.wait(2))
                self.assertEqual(self.state.jobs[job]['state'], 'running')
                importer.assert_not_called()
                with self.assertRaisesRegex(ValueError, '当前任务'):
                    self.state.job('another synthetic job', lambda progress: {})
            finally:
                release.set()
            self.assertEqual(self.wait_job(job)['state'], 'success')
        importer.assert_called_once()

    def test_error_cleanup_finishes_before_capture_job_becomes_terminal(self):
        self.prepare()
        cleaning, release = self.event(), self.event()
        clear = self.state.clear_database_keys
        def delayed_clear():
            cleaning.set()
            if not release.wait(3):
                raise AssertionError('Synthetic cleanup release was not signaled')
            clear()
        with patch('login_capture.capture_keys', side_effect=ValueError('synthetic capture failure')), patch('login_capture.ensure_same_process'), patch.object(self.state, 'clear_database_keys', side_effect=delayed_clear):
            job = self.state.api('/api/capture-import', self.params)['job']
            try:
                self.assertTrue(cleaning.wait(2))
                self.assertEqual(self.state.jobs[job]['state'], 'running')
                with self.assertRaisesRegex(ValueError, '当前任务'):
                    self.state.job('next synthetic job', lambda progress: {})
            finally:
                release.set()
            entry = self.wait_job(job)
        self.assertEqual(entry['state'], 'error')
        self.assertEqual(entry['error'], 'synthetic capture failure')
        self.assert_cache_empty()
        self.assert_no_secret(entry)

    def test_cancel_during_capture_prevents_import_even_when_helper_returns_material(self):
        self.prepare()
        entered, release = self.event(), self.event()
        found = {self.salt.hex(): self.key}
        def capture(*unused):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Synthetic capture release was not signaled')
            return found
        with patch('login_capture.capture_keys', side_effect=capture), patch('login_capture.ensure_same_process'), patch.object(self.state, 'local_import') as importer:
            job = self.state.api('/api/capture-import', self.params)['job']
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.state.api('/api/cancel', {'job': job}), {'requested': True})
            finally:
                release.set()
            entry = self.wait_job(job)
        importer.assert_not_called()
        self.assertEqual(entry['state'], 'error')
        self.assertIn('登录采集已停止', entry['error'])
        self.assertEqual(found, {})
        self.assert_cache_empty()
        self.assert_no_secret(entry)

    def test_database_cache_is_bound_to_canonical_account_path_and_format(self):
        self.cache()
        expected = (os.path.normcase(str(self.account.resolve())), 4)
        self.assertEqual(self.state.key_scope, expected)
        self.state.database_scope(self.account / '.', 4)
        self.assertEqual(self.state.keys, {self.salt.hex(): self.key})
        self.state.database_scope(self.other, 4)
        self.assertEqual(self.state.keys, {})
        self.assertIsNone(self.state.capture_prepared)
        self.assertNotEqual(self.state.key_scope, expected)
        self.state.keys[self.salt.hex()] = self.key
        self.state.database_scope(self.other, 3)
        self.assertEqual(self.state.keys, {})
        self.assertEqual(self.state.key_scope[1], 3)

    def test_active_capture_rejects_monitor_config_and_backup_mutations(self):
        self.state.jobs['capture'] = {'id': 'capture', 'label': '登录采集并导入', 'state': 'running', 'progress': 'synthetic capture'}
        original_config = dict(self.state.config)
        with patch.object(self.state.engine, 'start') as start:
            for path, params in (('/api/monitor-start', {'auto': True}), ('/api/config', {'model': 'must-not-save'}), ('/api/backup', {})):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError, '登录采集正在运行'):
                        self.state.api(path, params)
            start.assert_not_called()
        self.assertEqual(self.state.config, original_config)
        self.assertFalse((self.state.root / 'data' / 'exports').exists())
        self.assertEqual(self.state.api('/api/state', {})['jobs'][0]['state'], 'running')

    def test_cancel_targets_current_job_and_clears_database_cache(self):
        release, entered = self.event(), self.event()
        def work(progress):
            entered.set()
            release.wait(3)
            return {'synthetic': True}
        job = self.state.job('synthetic waiting job', work)['job']
        self.assertTrue(entered.wait(2))
        self.cache()
        try:
            self.assertEqual(self.state.api('/api/cancel', {'job': job}), {'requested': True})
            self.assertTrue(self.state.cancel.is_set())
            self.assert_cache_empty()
        finally:
            release.set()
        self.assertEqual(self.wait_job(job)['state'], 'partial')

    def test_stale_cancel_does_not_cancel_or_clear_cache_for_a_new_job(self):
        old = self.state.job('old synthetic job', lambda progress: {})['job']
        self.assertEqual(self.wait_job(old)['state'], 'success')
        release, entered = self.event(), self.event()
        def work(progress):
            entered.set()
            release.wait(3)
            return {}
        current = self.state.job('new synthetic job', work)['job']
        self.assertTrue(entered.wait(2))
        self.cache()
        try:
            self.assertEqual(self.state.api('/api/cancel', {'job': old}), {'requested': False})
            self.assertFalse(self.state.cancel.is_set())
            self.assertEqual(self.state.keys, {self.salt.hex(): self.key})
            self.assertEqual(self.state.jobs[current]['state'], 'running')
        finally:
            release.set()
        self.assertEqual(self.wait_job(current)['state'], 'success')

    def test_quit_clears_cache_and_prevents_new_jobs(self):
        self.cache()
        with patch.object(self.state.engine, 'halt') as halt:
            self.assertEqual(self.state.api('/api/quit', {}), {'ok': True})
        halt.assert_called_once()
        self.assertTrue(self.state.exit.is_set())
        self.assertTrue(self.state.cancel.is_set())
        self.assert_cache_empty()
        with self.assertRaisesRegex(ValueError, '正在退出'):
            self.state.job('must not start', lambda progress: {})

    def test_successful_restore_clears_capture_cache_and_provider_token(self):
        archive = self.root / 'synthetic-workspace.zip'
        workspace_backup.create(self.state.store, self.state.cfg(), archive)
        self.cache()
        self.state.key = 'synthetic-provider-token'
        server = Server(self.state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/upload?mode=restore&name=synthetic.zip&confirm=replace',
                                             data=archive.read_bytes(), headers={'X-Studio-Token': self.state.token, 'Content-Type': 'application/octet-stream'})
            with urllib.request.urlopen(request, timeout=5) as response:
                job = json.load(response)['job']
            entry = self.wait_job(job)
            self.assertEqual(entry['state'], 'success', entry['error'])
            self.assert_cache_empty()
            self.assertEqual(self.state.key, '')
            self.assert_no_secret(entry)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
