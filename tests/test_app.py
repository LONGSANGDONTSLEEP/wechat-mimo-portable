import io
import json
import unittest
from unittest.mock import patch
import app
import vision
from PIL import Image,ImageDraw

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

    def test_unrecognized_layout_rejected(self):
        with self.assertRaises(ValueError):vision.divider(Image.new('RGB',(1000,700),'white'))

    def test_full_window_layout_and_unread(self):
        image=Image.new('RGB',(1000,700),(245,245,245)); draw=ImageDraw.Draw(image)
        draw.rectangle((0,0,319,699),fill=(235,235,235))
        draw.line((320,0,320,699),fill=(190,190,190))
        draw.rectangle((321,510,999,699),fill='white')
        draw.line((321,510,999,510),fill=(190,190,190))
        draw.ellipse((110,145,125,160),fill=(240,40,40))
        lines=[vision.Text('测试联系人',350,45,100,20),vision.Text('你好',380,220,45,20),vision.Text('自己的回复',850,300,80,20)]
        view=vision.parse(image,lines,1,(100,100,1000,700))
        self.assertEqual(view.title,'测试联系人')
        self.assertEqual(view.message,'你好')
        self.assertEqual(len(view.unread),1)
        self.assertGreater(view.input_point[1],610)

    def test_dedupe(self):
        self.assertEqual(app.fingerprint('你 好\n'),app.fingerprint('你好'))
        self.assertNotEqual(app.fingerprint('你好'),app.fingerprint('您好'))

if __name__=='__main__':unittest.main()
