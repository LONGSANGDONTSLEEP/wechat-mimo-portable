import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from desktop_engine import DesktopEngine
import vision


class DesktopReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.store=Mock();self.engine=DesktopEngine(self.store)
        self.engine.monitor={'hwnd':1,'pid':10,'input':{'size':[600,500],'point':[300,450]},'crop':None}
        self.engine.running=True
        self.expected=vision.View(1,(10,20,600,500),'朋友','你好',(310,470),[],b'')
        self.patches=[patch('desktop_engine.App.halted',lambda engine:engine.stop.is_set()),patch('desktop_engine.vision.validate_window'),patch('desktop_engine.vision.ensure_point'),patch('desktop_engine.vision.activate'),patch('desktop_engine.vision.foreground',return_value=True),patch('desktop_engine.time.sleep')]
        for item in self.patches:item.start();self.addCleanup(item.stop)
        self.auto=Mock();self.clipboard=['原来的剪贴板']
        self.clip=SimpleNamespace(paste=lambda:self.clipboard[0],copy=lambda value:self.clipboard.__setitem__(0,value))
        module_patch=patch.dict(sys.modules,{'pyautogui':self.auto,'pyperclip':self.clip});module_patch.start();self.addCleanup(module_patch.stop)
        self.engine.inspect=Mock(return_value=self.expected)

    def test_stopped_queue_preview_cannot_return_or_send(self):
        self.engine.halt();self.engine.events.put(('preview',(self.expected,'你好','你好呀')))
        self.assertEqual(self.engine.state()['preview'],'')
        self.assertIsNone(self.engine.pending)
        with self.assertRaises(ValueError):self.engine.send()
        self.auto.click.assert_not_called()

    def test_expired_preview_stops_without_ui_input(self):
        self.engine.pending=(self.expected,'你好','你好呀');self.engine.preview_at=time.monotonic()-121
        with self.assertRaisesRegex(ValueError,'过期'):self.engine.send()
        self.assertTrue(self.engine.stop.is_set());self.auto.click.assert_not_called()

    def test_window_mismatch_and_stop_during_click_never_press_enter(self):
        bad=vision.View(2,self.expected.rect,'朋友','你好',self.expected.input_point,[])
        with self.assertRaisesRegex(ValueError,'窗口'):self.engine.deliver(bad,'你好','你好呀')
        self.auto.click.assert_not_called()
        self.auto.click.side_effect=lambda *args:self.engine.halt()
        with self.assertRaisesRegex(ValueError,'停止'):self.engine.deliver(self.expected,'你好','你好呀')
        self.auto.press.assert_not_called();self.assertEqual(self.clipboard[0],'原来的剪贴板')

    def test_new_incoming_after_paste_preserves_draft_without_sending(self):
        changed=vision.View(1,self.expected.rect,'朋友','又来一条',self.expected.input_point,[])
        self.engine.inspect.side_effect=[self.expected,changed]
        with self.assertRaisesRegex(ValueError,'消息已变化'):self.engine.deliver(self.expected,'你好','你好呀')
        self.assertNotIn(unittest.mock.call('enter'),self.auto.press.call_args_list)
        self.assertEqual(self.clipboard[0],'原来的剪贴板');self.store.remember_exchange.assert_not_called()

    def test_focus_loss_before_copy_aborts_and_restores_clipboard(self):
        foreground=[True]
        self.auto.hotkey.side_effect=lambda *keys:foreground.__setitem__(0,False)
        with patch('desktop_engine.vision.foreground',side_effect=lambda hwnd:foreground[0]):
            with self.assertRaisesRegex(ValueError,'焦点'):self.engine.deliver(self.expected,'你好','你好呀')
        self.auto.hotkey.assert_called_once_with('ctrl','a')
        self.assertEqual(self.clipboard[0],'原来的剪贴板');self.auto.press.assert_not_called()

    def test_success_and_duplicate_send_are_serialized(self):
        self.engine.deliver(self.expected,'你好','你好呀')
        self.auto.press.assert_any_call('enter');self.assertEqual(self.clipboard[0],'原来的剪贴板')
        self.store.remember_exchange.assert_called_once_with('朋友','你好','你好呀')
        self.engine.send_lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError,'正在执行'):self.engine.deliver(self.expected,'你好','你好呀')
            self.assertTrue(self.engine.busy())
        finally:self.engine.send_lock.release()

    def test_point_rejects_resize_and_selection_rejects_exiting_worker(self):
        self.engine.running=False;self.engine.last_rect=(0,0,600,500)
        with patch('desktop_engine.vision.client_rect',return_value=(0,0,700,500)):
            with self.assertRaisesRegex(ValueError,'尺寸'):self.engine.point(300,450)
        self.engine.thread=Mock();self.engine.thread.is_alive.return_value=True
        with self.assertRaisesRegex(ValueError,'退出'):self.engine.select(1)

    def test_start_uses_validated_key_and_drops_previous_events(self):
        self.engine.running=False;self.engine.events.put(('done',None));received=[]
        self.engine.worker=lambda cfg,key:received.append(key)
        with patch('desktop_engine.validate_connection',return_value=('normalized-token','endpoint')):
            self.engine.start({'auto':False},' raw-token ')
        self.engine.thread.join(timeout=1)
        self.assertEqual(received,['normalized-token']);self.assertTrue(self.engine.events.empty())


if __name__=='__main__':unittest.main()
