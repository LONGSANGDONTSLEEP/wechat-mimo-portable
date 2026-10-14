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

    def test_locked_point_tracks_move_but_rejects_resize(self):
        saved={'size':[1000,700],'point':[450,580]}
        self.assertEqual(vision.confirmed_point((100,200,1000,700),saved),(550,780))
        self.assertEqual(vision.confirmed_point((-100,50,1000,700),saved),(350,630))
        with self.assertRaises(ValueError):vision.confirmed_point((0,0,900,700),saved)
        with self.assertRaises(ValueError):vision.confirmed_point((0,0,1000,700),None)

    def test_wrong_chat_prevents_any_click(self):
        import threading
        instance=app.App.__new__(app.App)
        instance.monitor={'pid':1,'input':{'size':[1000,700],'point':[500,600]}}
        instance.halted=lambda:False
        before=vision.View(10,(0,0,1000,700),'目标','消息',(500,600),[])
        changed=vision.View(10,(0,0,1000,700),'其他人','消息',(500,600),[])
        instance.inspect=lambda _:changed
        with patch('vision.validate_window'),patch('vision.activate'),patch('vision.foreground',return_value=True),patch('pyautogui.click') as click,patch('app.time.sleep'):
            with self.assertRaises(ValueError):instance.deliver(before,'消息','回复')
            click.assert_not_called()

    def test_obscured_point_prevents_any_click(self):
        instance=app.App.__new__(app.App)
        instance.monitor={'pid':1,'input':{'size':[1000,700],'point':[500,600]}}
        instance.halted=lambda:False
        view=vision.View(10,(0,0,1000,700),'目标','消息',(500,600),[])
        instance.inspect=lambda _:view
        with patch('vision.validate_window'),patch('vision.activate'),patch('vision.foreground',return_value=True),patch('vision.ensure_point',side_effect=ValueError('遮挡')),patch('pyautogui.click') as click,patch('app.time.sleep'):
            with self.assertRaises(ValueError):instance.deliver(view,'消息','回复')
            click.assert_not_called()

if __name__=='__main__':unittest.main()
