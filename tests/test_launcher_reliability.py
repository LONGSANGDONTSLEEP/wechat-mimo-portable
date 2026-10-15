import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from studio_web import should_exit,instance_url,cleanup_instance


class LauncherReliabilityTests(unittest.TestCase):
    def state(self):
        return SimpleNamespace(jobs={},job_lock=threading.Lock(),exit=threading.Event(),cancel=threading.Event(),engine=SimpleNamespace(halt=Mock(),send_lock=threading.Lock()),last_seen=0)

    def test_idle_timeout_waits_for_background_mutation(self):
        state=self.state();state.jobs={'backup':{'state':'running'}}
        self.assertFalse(should_exit(state,1000))
        state.jobs['backup']['state']='success';self.assertTrue(should_exit(state,1000))

    def test_requested_exit_cancels_and_waits_for_job_and_send(self):
        state=self.state();state.exit.set();state.jobs={'restore':{'state':'running'}}
        self.assertFalse(should_exit(state));self.assertTrue(state.cancel.is_set());state.engine.halt.assert_called()
        state.jobs.clear();state.engine.send_lock.acquire()
        try:self.assertFalse(should_exit(state))
        finally:state.engine.send_lock.release()
        self.assertTrue(should_exit(state))

    def test_instance_url_rejects_external_or_ambiguous_hosts(self):
        expected='http://127.0.0.1:12345/#session=secret'
        self.assertEqual(instance_url({'url':expected}),expected)
        for url in ('http://127.0.0.1:12345@example.com/#session=secret','http://127.0.0.1:12345.evil.test/#session=secret','http://127.0.0.1:12345/','https://127.0.0.1:12345/#session=secret','http://127.0.0.1:12345/other#session=secret'):
            self.assertIsNone(instance_url({'url':url}))

    def test_cleanup_only_removes_own_instance_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'running.json';path.write_text(json.dumps({'pid':22}),'utf-8')
            cleanup_instance(path,11);self.assertTrue(path.exists())
            cleanup_instance(path,22);self.assertFalse(path.exists())


if __name__=='__main__':unittest.main()
