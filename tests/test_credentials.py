import io
import json
import http.client
import unittest
from unittest.mock import patch
import app

class CredentialTests(unittest.TestCase):
    def test_reproduce_reported_latin1_error(self):
        connection=http.client.HTTPConnection('example.invalid')
        connection.putrequest('POST','/')
        with self.assertRaises(UnicodeEncodeError) as raised:
            connection.putheader('api-key','中'*51)
        self.assertEqual(raised.exception.encoding,'latin-1')
        self.assertEqual((raised.exception.start,raised.exception.end),(0,51))

    def test_non_ascii_key_rejected_without_transport_or_secret_in_error(self):
        for key in ('中文Key不要泄露','sk-test\u200bvalue','ｓｋ－ｆｕｌｌｗｉｄｔｈ'):
            with patch('urllib.request.urlopen') as transport:
                with self.assertRaises(ValueError) as raised:app.completion(app.DEFAULT,key,'你好',[])
                transport.assert_not_called()
                self.assertNotIn(key,str(raised.exception))

    def test_whitespace_inside_key_rejected(self):
        for key in ('sk-one two','sk-one\ntwo','sk-one\ttwo'):
            with self.assertRaises(ValueError):app.normalize_api_key(key)

    def test_paste_wrapper_normalized(self):
        self.assertEqual(app.normalize_api_key('  Bearer sk-fake-test  '),'sk-fake-test')
        self.assertEqual(app.normalize_api_key('"sk-fake-test"'),'sk-fake-test')

    def test_chinese_request_body_is_utf8_and_headers_are_ascii(self):
        response=io.BytesIO(json.dumps({'choices':[{'message':{'content':'连接成功'}}]}).encode())
        with patch('urllib.request.urlopen',return_value=response) as transport:
            self.assertEqual(app.completion(app.DEFAULT,'sk-fake-test','你好啊',[]),'连接成功')
        request=transport.call_args.args[0]
        self.assertEqual(json.loads(request.data.decode('utf-8'))['messages'][-1]['content'],'你好啊')
        for name,value in request.header_items():value.encode('latin-1')

if __name__=='__main__':unittest.main()
