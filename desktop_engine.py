"""UI-independent adapter for the previously tested locked-window reply engine."""
import io
import queue
import threading
import time
from types import SimpleNamespace
from app import App,validate_connection,local_budget
import vision


class DesktopEngine:
    inspect=App.inspect
    halted=App.halted
    deliver=App.deliver
    worker=App.worker

    def __init__(self,store):
        self.store=store;self.monitor=None;self.running=False;self.stop=threading.Event();self.awaiting=threading.Event()
        self.events=queue.Queue();self.history=[];self.pending=None;self.logs=[];self.waiting=None
        self.studio=SimpleNamespace(store=store,reply_cfg=self.reply_cfg)
        self.lock=threading.RLock()

    def reply_cfg(self,cfg,contact,text):
        result=dict(cfg)
        budget=None
        if cfg.get('provider') in ('ollama','lmstudio'):
            budget=min(1800,local_budget(cfg)-len(text.encode('utf-8'))-48)
            if budget<600:raise ValueError('当前消息过长，请提高本地上下文或缩短内容')
        result['prompt']=self.store.prompt(contact,text,cfg['prompt'],cfg.get('longer',True),cfg.get('proactive',True),max_bytes=budget) if cfg.get('persona_enabled',True) else cfg['prompt']
        return result

    def select(self,hwnd):
        if self.running:raise ValueError('请先停止监听')
        windows=vision.list_windows();selected=next((r for r in windows if r[0]==int(hwnd)),None)
        if not selected:raise ValueError('所选微信窗口已不存在')
        self.monitor={'hwnd':selected[0],'pid':selected[1],'input':None,'crop':None};self.pending=None
        if hasattr(self,'last_rect'):del self.last_rect

    def screenshot(self):
        if not self.monitor:raise ValueError('请先选择微信窗口')
        image,rect=vision.capture_window(self.monitor['hwnd'],self.monitor['pid']);self.last_rect=rect
        output=io.BytesIO();image.save(output,format='PNG');return output.getvalue()

    def point(self,x,y):
        if self.running:raise ValueError('请先停止监听')
        if not hasattr(self,'last_rect'):raise ValueError('请先获取窗口截图')
        size=list(self.last_rect[2:]);x,y=int(x),int(y)
        if not 0<x<size[0] or not 0<y<size[1]:raise ValueError('输入位置超出截图')
        self.monitor['input']={'size':size,'point':[x,y]}

    def start(self,cfg,key):
        if self.running:raise ValueError('监听任务尚未退出')
        if not self.monitor:raise ValueError('请先选择微信窗口')
        if cfg.get('auto') and not self.monitor['input']:raise ValueError('自动发送前请在截图中确认输入框位置')
        validate_connection(cfg,key);self.stop.clear();self.awaiting.clear();self.pending=None;self.running=True
        threading.Thread(target=self.worker,args=(dict(cfg,interval=3),key),daemon=True).start()

    def halt(self):self.stop.set();self.pending=None

    def send(self):
        if not self.running or not self.pending:raise ValueError('没有可发送的当前预览')
        expected,text,answer=self.pending;self.pending=None
        try:self.deliver(expected,text,answer)
        except Exception:self.halt();raise

    def state(self):
        with self.lock:
            while not self.events.empty():
                kind,value=self.events.get()
                if kind=='preview':self.pending=value;self.logs.append('已生成回复预览')
                elif kind=='log':self.logs.append(value)
                elif kind=='model_wait':self.waiting=value
                elif kind=='sent':self.pending=None;self.logs.append('已发送')
                elif kind=='done':self.running=False;self.pending=None;self.waiting=None
            self.logs=self.logs[-60:]
            return {'running':self.running,'selected':self.monitor['hwnd'] if self.monitor else None,'point':self.monitor.get('input') if self.monitor else None,
                    'preview':self.pending[2] if self.pending else '', 'logs':list(self.logs),'waiting':int(time.monotonic()-self.waiting) if self.waiting else None}
