"""UI-independent adapter for the previously tested locked-window reply engine."""
import io
import queue
import threading
import time
from types import SimpleNamespace
from app import App,validate_connection,local_budget,fingerprint
import vision


class EngineEvents(queue.Queue):
    def put(self,item,*args,**kwargs):
        super().put((time.monotonic(),item),*args,**kwargs)


class DesktopEngine:
    inspect=App.inspect
    PREVIEW_TTL=120

    def __init__(self,store):
        self.store=store;self.monitor=None;self.running=False;self.stop=threading.Event();self.awaiting=threading.Event()
        self.events=EngineEvents();self.history=[];self.pending=None;self.logs=[];self.waiting=None
        self.studio=SimpleNamespace(store=store,reply_cfg=self.reply_cfg)
        self.lock=threading.RLock();self.send_lock=threading.Lock();self.thread=None;self.preview_at=None;self.auto=False

    def halted(self):
        if App.halted(self):self.halt();return True
        return False

    def worker(self,cfg,key):
        try:App.worker(self,cfg,key)
        finally:
            with self.lock:
                self.running=False;self.pending=None;self.preview_at=None;self.waiting=None

    def busy(self):
        return self.running or self.send_lock.locked() or bool(self.thread and self.thread.is_alive())

    def reply_cfg(self,cfg,contact,text):
        result=dict(cfg)
        budget=None
        if cfg.get('provider') in ('ollama','lmstudio'):
            budget=min(1800,local_budget(cfg)-len(text.encode('utf-8'))-48)
            if budget<600:raise ValueError('当前消息过长，请提高本地上下文或缩短内容')
        result['prompt']=self.store.prompt(contact,text,cfg['prompt'],cfg.get('longer',True),cfg.get('proactive',True),max_bytes=budget) if cfg.get('persona_enabled',True) else cfg['prompt']
        return result

    def select(self,hwnd):
        with self.lock:
            if self.busy():raise ValueError('请先停止监听并等待当前操作退出')
            windows=vision.list_windows();selected=next((r for r in windows if r[0]==int(hwnd)),None)
            if not selected:raise ValueError('所选微信窗口已不存在')
            self.monitor={'hwnd':selected[0],'pid':selected[1],'input':None,'crop':None};self.pending=None;self.preview_at=None
            if hasattr(self,'last_rect'):del self.last_rect

    def screenshot(self):
        with self.lock:
            if not self.monitor:raise ValueError('请先选择微信窗口')
            image,rect=vision.capture_window(self.monitor['hwnd'],self.monitor['pid']);self.last_rect=rect
        output=io.BytesIO();image.save(output,format='PNG');return output.getvalue()

    def point(self,x,y):
        with self.lock:
            if self.busy():raise ValueError('请先停止监听并等待当前操作退出')
            if not self.monitor or not hasattr(self,'last_rect'):raise ValueError('请先获取窗口截图')
            vision.validate_window(self.monitor['hwnd'],self.monitor['pid'])
            if tuple(vision.client_rect(self.monitor['hwnd'])[2:])!=tuple(self.last_rect[2:]):raise ValueError('窗口尺寸已变化，请重新获取截图')
            size=list(self.last_rect[2:]);x,y=int(x),int(y)
            if not 0<x<size[0] or not 0<y<size[1]:raise ValueError('输入位置超出截图')
            self.monitor['input']={'size':size,'point':[x,y]}

    def start(self,cfg,key):
        with self.lock:
            if self.busy():raise ValueError('监听任务尚未退出')
            if not self.monitor:raise ValueError('请先选择微信窗口')
            if cfg.get('auto') and not self.monitor['input']:raise ValueError('自动发送前请在截图中确认输入框位置')
            key,_=validate_connection(cfg,key)
            while True:
                try:self.events.get_nowait()
                except queue.Empty:break
            self.stop.clear();self.awaiting.clear();self.pending=None;self.preview_at=None;self.waiting=None;self.running=True;self.auto=bool(cfg.get('auto'));self.history=[]
            self.thread=threading.Thread(target=self.worker,args=(dict(cfg,interval=3),key),daemon=True)
            try:self.thread.start()
            except Exception:self.running=False;self.stop.set();raise

    def halt(self):
        self.stop.set()
        with self.lock:self.pending=None;self.preview_at=None;self.awaiting.clear();self.waiting=None

    def _guard(self,expected,point=None):
        if self.halted():raise ValueError('监听已停止，取消发送')
        if not self.running or not self.monitor or expected.hwnd!=self.monitor['hwnd']:raise ValueError('预览不属于当前锁定窗口，取消发送')
        vision.validate_window(expected.hwnd,self.monitor['pid'])
        if point is not None:
            if not vision.foreground(expected.hwnd):raise ValueError('窗口焦点变化，取消发送')
            vision.ensure_point(expected.hwnd,point)
        if self.stop.is_set():raise ValueError('监听已停止，取消发送')

    @staticmethod
    def _unchanged(current,expected):
        title_ok=vision.header_same(current.header,expected.header) if expected.header else fingerprint(current.title)==fingerprint(expected.title)
        return title_ok and bool(current.message) and vision.message_equal(vision.Bubble('in',(),current.message),vision.Bubble('in',(),expected.message))

    def deliver(self,expected,text,answer):
        # Serialize all sends; every input operation rechecks the locked window.
        if not self.send_lock.acquire(blocking=False):raise ValueError('已有发送操作正在执行')
        try:
            self._guard(expected)
            if not self.monitor['input']:raise ValueError('请停止监听，在窗口预览中设置发送位置')
            if not isinstance(answer,str) or not answer.strip():raise ValueError('回复正文为空，取消发送')
            import pyautogui
            import pyperclip
            vision.activate(expected.hwnd);time.sleep(.3)
            self._guard(expected)
            if not vision.foreground(expected.hwnd):raise ValueError('无法激活锁定窗口，取消发送')
            current=self.inspect(expected.hwnd)
            if not self._unchanged(current,expected):raise ValueError('聊天或消息已变化，取消发送')
            point=current.input_point
            def action(fn,*args):
                self._guard(expected,point);fn(*args)
            action(pyautogui.click,*point);time.sleep(.25)
            previous=pyperclip.paste()
            try:
                pyperclip.copy('');action(pyautogui.hotkey,'ctrl','a');action(pyautogui.hotkey,'ctrl','c');time.sleep(.2)
                draft=pyperclip.paste();action(pyautogui.press,'right')
                if draft.strip():raise ValueError('输入框存在草稿，已停止，请清空后重试')
                self._guard(expected,point);pyperclip.copy(answer);action(pyautogui.hotkey,'ctrl','v');time.sleep(.35)
                current=self.inspect(expected.hwnd)
                if not self._unchanged(current,expected):raise ValueError('聊天或消息已变化，回复已留在草稿中，取消发送')
                action(pyautogui.press,'enter')
            finally:pyperclip.copy(previous)
            self.history.extend([{'role':'user','content':text},{'role':'assistant','content':answer}]);self.history=self.history[-12:]
            self.studio.store.remember_exchange(getattr(self,'active_record_contact',expected.title),text,answer)
            self.awaiting.clear();self.events.put(('sent',None))
        finally:self.send_lock.release()

    def send(self):
        with self.lock:
            if self.stop.is_set() or not self.running or not self.pending:raise ValueError('没有可发送的当前预览')
            if self.preview_at is None or time.monotonic()-self.preview_at>self.PREVIEW_TTL:
                self.halt();raise ValueError('回复预览已过期，请重新启动监听')
            expected,text,answer=self.pending;self.pending=None;self.preview_at=None
        try:self.deliver(expected,text,answer)
        except Exception:self.halt();raise

    def state(self):
        with self.lock:
            while True:
                try:created,(kind,value)=self.events.get_nowait()
                except queue.Empty:break
                if kind=='preview':
                    if self.running and not self.stop.is_set() and not self.auto:
                        self.pending=value;self.preview_at=created;self.logs.append('已生成回复预览')
                elif kind=='log':self.logs.append(value)
                elif kind=='model_wait':self.waiting=value if self.running and not self.stop.is_set() else None
                elif kind=='sent':self.pending=None;self.logs.append('已发送')
                elif kind=='done':self.running=False;self.pending=None;self.preview_at=None;self.waiting=None
            if self.stop.is_set():self.pending=None;self.preview_at=None;self.waiting=None
            if self.pending and time.monotonic()-self.preview_at>self.PREVIEW_TTL:self.halt();self.logs.append('回复预览已过期，监听已停止')
            self.logs=self.logs[-60:]
            return {'running':self.running,'selected':self.monitor['hwnd'] if self.monitor else None,'point':self.monitor.get('input') if self.monitor else None,
                    'preview':self.pending[2] if self.pending else '', 'logs':list(self.logs),'waiting':int(time.monotonic()-self.waiting) if self.waiting else None}
