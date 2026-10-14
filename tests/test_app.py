import io
import json
import unittest
from unittest.mock import patch
import app

class Tests(unittest.TestCase):
    def test_mimo_protocol(self):
        cfg=app.DEFAULT.copy()
        with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps({'choices':[{'message':{'content':'你好'}}]}).encode())) as call:
            self.assertEqual(app.completion(cfg,'FAKE_TEST_KEY','消息',[]),'你好')
        req=call.call_args.args[0]
        self.assertEqual(req.full_url,'https://api.xiaomimimo.com/v1/chat/completions')
        self.assertEqual(req.get_header('Api-key'),'FAKE_TEST_KEY')
        self.assertEqual(json.loads(req.data)['messages'][-1]['content'],'消息')

    def test_empty_reply_rejected(self):
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'{"choices":[{"message":{"content":""}}]}')):
            with self.assertRaises(ValueError):app.completion(app.DEFAULT,'fake','x',[])

    def test_title_mismatch_blocks(self):
        instance=app.App.__new__(app.App)
        with patch('app.ocr',return_value='另一个人'):
            with self.assertRaises(ValueError):instance.check_title({'regions':{'header':[]},'title':'目标'})

    def test_dedupe(self):
        self.assertEqual(app.fingerprint('你 好\n'),app.fingerprint('你好'))
        self.assertNotEqual(app.fingerprint('你好'),app.fingerprint('您好'))

if __name__=='__main__':unittest.main()
