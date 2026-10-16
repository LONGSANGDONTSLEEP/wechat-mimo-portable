import asyncio
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import urllib.request
import urllib.error
import urllib.parse
import vision
import recognition
from types import SimpleNamespace

_ocr_engine=None
_ocr_lock=threading.Lock()

ROOT = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
DEFAULT = dict(provider='mimo',base_url='https://api.xiaomimimo.com/v1', model='mimo-v2.6-pro',
               prompt='自然接住对方的话题，不编造经历或承诺。只输出要发送的正文。',
               title='', auto=False, interval=3,context_size=4096,max_tokens=768,reasoning='none')

def local_budget(cfg):
    try:size=int(cfg.get('context_size',4096)); output=int(cfg.get('max_tokens',768))
    except (ValueError,TypeError):raise ValueError('本地上下文长度必须为整数') from None
    if not 2048<=size<=131072 or not 1<=output<size-384:raise ValueError('请检查本地上下文长度和回复长度设置')
    # UTF-8 byte count is deliberately conservative, not an exact tokenizer count.
    return size-output-384

def prepare_messages(cfg,text,history):
    base=[{'role':'system','content':cfg['prompt']}]
    current={'role':'user','content':text}
    if cfg.get('provider') not in ('ollama','lmstudio'):return base+history[-12:]+[current]
    budget=local_budget(cfg)
    used=sum(len(m['content'].encode('utf-8'))+24 for m in base+[current])
    if used>budget:raise ValueError('当前消息或补充提示超出本地上下文预算。请缩短内容，或在模型服务加载更大上下文后同步修改“本地上下文长度”。')
    retained=[]
    pairs=[history[i:i+2] for i in range(0,len(history),2)]
    for pair in reversed(pairs[-6:]):
        cost=sum(len(m['content'].encode('utf-8'))+24 for m in pair)
        if used+cost>budget:break
        retained=pair+retained; used+=cost
    return base+retained+[current]

def fingerprint(text):
    return hashlib.sha256(''.join(text.split()).encode()).hexdigest()

def normalize_api_key(value):
    key=value.strip().lstrip('\ufeff')
    if key.lower().startswith('bearer '):key=key[7:].strip()
    if len(key)>1 and key[0]==key[-1] and key[0] in ('"',"'"):key=key[1:-1].strip()
    if not key:raise ValueError('请填写小米控制台复制的 API Key')
    if not key.isascii():raise ValueError('API Key 含中文、全角或不可见字符，无法放入请求头。请清空后从小米控制台重新复制纯 Key，不要复制说明文字。')
    if any(ord(c)<33 or ord(c)>126 for c in key):raise ValueError('API Key 内含空格、换行或控制字符，请重新复制完整 Key')
    return key

def validate_connection(cfg,key):
    base=cfg['base_url'].strip().rstrip('/')
    parsed=urllib.parse.urlsplit(base)
    local=cfg.get('provider','mimo') in ('ollama','lmstudio')
    if not base.isascii() or parsed.scheme not in (('http','https') if local else ('https',)) or not parsed.hostname:
        raise ValueError('接口地址应为英文 HTTPS 地址，请点击按量付费或 Token Plan 按钮恢复')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('接口地址只能填写 Base URL，不能包含账号、密钥或查询参数')
    if not cfg['model'].strip() or not cfg['model'].isascii():raise ValueError('请选择本地服务中的准确模型 ID' if local else '模型名称应为英文模型 ID，例如 mimo-v2.6-pro')
    if local:
        if parsed.path.rstrip('/') in ('','/api'):base=urllib.parse.urlunsplit((parsed.scheme,parsed.netloc,'/v1','',''))
        return normalize_api_key(key) if cfg.get('local_auth') and key.strip() else '',base+'/chat/completions'
    key=normalize_api_key(key)
    return key,base+'/chat/completions'

def completion(cfg, key, text, history):
    key,url=validate_connection(cfg,key)
    local=cfg.get('provider','mimo') in ('ollama','lmstudio')
    payload = dict(model=cfg['model'].strip(), messages=prepare_messages(cfg,text,history), stream=False)
    payload['max_tokens' if local else 'max_completion_tokens']=int(cfg.get('max_tokens',768 if local else 2048))
    if local and cfg.get('reasoning')=='none':payload['reasoning_effort']='none'
    headers={'Content-Type':'application/json; charset=utf-8'}
    if local and key:headers['Authorization']='Bearer '+key
    if not local:headers.update({'api-key':key,'Authorization':'Bearer '+key})
    req = urllib.request.Request(url, json.dumps(payload,ensure_ascii=False).encode('utf-8'),headers)
    try:
        open_request=urllib.request.build_opener(urllib.request.ProxyHandler({})).open if local else urllib.request.urlopen
        with open_request(req, timeout=300 if local else 90) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        if local and exc.code==401:raise RuntimeError('本地模型服务 HTTP 401：服务器要求 API Token。请核对 LM Studio 的认证设置；Token只需在本次运行填写。') from None
        detail=''
        try:
            error=json.loads(exc.read(8192)); error=error.get('error',error)
            detail=str(error.get('message',error) if isinstance(error,dict) else error)[:350]
        except Exception:pass
        from chatdata import redact
        if local and 'no models loaded' in detail.lower():raise RuntimeError('本地服务已连接，但没有加载聊天模型。请在 LM Studio 的 Model Server 点击 Load Model，加载后读取模型列表并选择准确ID。') from None
        raise RuntimeError((f'本地模型服务 HTTP {exc.code}' if local else f'MiMo HTTP {exc.code}')+'：'+(redact(detail) or '请检查服务地址、模型名称与认证配置')) from None
    except UnicodeEncodeError:
        raise ValueError('请求头含非英文字符，尚未成功调用 MiMo。请重新复制 Key；若仍失败，请检查系统代理配置。') from None
    except urllib.error.URLError:
        raise RuntimeError('无法连接本地模型服务，请先启动服务并检查端口（Ollama 11434 / LM Studio 1234）' if local else '无法连接 MiMo，请检查网络、代理和接口地址') from None
    except TimeoutError:raise RuntimeError('模型请求超时；请检查模型是否加载、关闭思考模式，或减少上下文后重试') from None
    except (json.JSONDecodeError,UnicodeDecodeError):raise ValueError('服务返回的不是有效JSON，请检查服务地址是否为模型API地址') from None
    try:choice=result['choices'][0]; message=choice['message']; answer=message.get('content')
    except (KeyError,TypeError,IndexError,AttributeError):raise ValueError('模型返回格式不正确，缺少choices/message；请检查模型服务兼容接口') from None
    if choice.get('finish_reason')=='length':raise ValueError('模型达到输出上限，结果已截断，未作为回复使用。请关闭思考模式、简化要求后重试。')
    if choice.get('finish_reason') not in (None,'stop') or message.get('tool_calls'):raise ValueError('模型未正常完成纯文本回复（过滤或工具调用），未发送微信')
    if local and isinstance(answer,str):
        answer=re.sub(r'<think>.*?</think>','',answer,flags=re.S).strip()
        if '</think>' in answer:answer=answer.rsplit('</think>',1)[-1].strip()
        if '<think>' in answer:raise ValueError('本地模型只返回了未结束的思考内容，请换用聊天模型或检查模型配置')
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError('模型只返回思考内容，没有回复正文；请在本地模型设置关闭思考模式后重试' if message.get('reasoning_content') or message.get('reasoning') else '模型未返回回复正文')
    if len(answer) > 1500:
        raise ValueError('回复超过1500字，请缩短提示词要求')
    return answer.strip()

async def recognize_image(image):
    global _ocr_engine
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    with _ocr_lock:
        if _ocr_engine is None:_ocr_engine=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=2)
        result,_=_ocr_engine(np.asarray(image.convert('RGB')))
    rows=[]
    for box,text,confidence in result or []:
        if float(confidence)>=.60:rows.append((min(p[1] for p in box),min(p[0] for p in box),text))
    return SimpleNamespace(text='\n'.join(row[2] for row in sorted(rows)))

class App:
    def __init__(self, root,data_root=None):
        self.root=root; self.data_root=Path(data_root or ROOT)
        root.title('微信分身 Studio · 1.0.1')
        root.geometry('800x780')
        self.cfg=DEFAULT.copy()
        if (self.data_root/'config.json').exists():
            try: self.cfg.update(json.loads((self.data_root/'config.json').read_text('utf-8')))
            except Exception: messagebox.showwarning('配置', '配置无法读取，使用默认值')
        if self.cfg['prompt']=='你是微信聊天助手。自然、简短地用中文回复对方，不编造事实。只输出要发送的正文。':self.cfg['prompt']=DEFAULT['prompt']
        self.events=queue.Queue(); self.stop=threading.Event(); self.running=False; self.testing_connection=False
        self.history=[]; self.pending=None; self.seen=set(); self.vars={}; self.awaiting=threading.Event()
        self.windows=[]; self.monitor=None; self.preview_dialog=None; self.generating_since=None
        from studio_ui import StudioUI
        self.studio=StudioUI(self,self.data_root)
        root.bind('<Escape>',lambda _:self.halt()); root.protocol('WM_DELETE_WINDOW',self.close)
        root.after(200,self.pump)
        self.refresh_windows()

    def refresh_windows(self):
        if self.running:return
        self.windows=vision.list_windows()
        self.window_choice['values']=[f'{title} · PID {pid} · {hwnd:X}' for hwnd,pid,title,kind in self.windows]
        self.monitor=None; self.window_choice.set('')
        if len(self.windows)==1:self.window_choice.current(0); self.choose_window()
        self.status.set('请选择微信窗口，可包含微信独立聊天窗口' if self.windows else '未找到微信窗口，请打开微信后刷新')

    def choose_window(self,event=None):
        if self.running:return
        index=self.window_choice.current()
        if index<0:return
        hwnd,pid,title,kind=self.windows[index]
        self.monitor={'hwnd':hwnd,'pid':pid,'input':None,'crop':None}
        self.status.set('已锁定：'+title+'；打开预览检查截图，可设置输入框位置')

    def window_preview(self):
        if self.running:return messagebox.showinfo('窗口预览','请先停止监听')
        if not self.monitor:return messagebox.showinfo('选择窗口','请先选择一个微信窗口')
        try:image,rect=vision.capture_window(self.monitor['hwnd'],self.monitor['pid'])
        except Exception as exc:return messagebox.showerror('窗口截图',str(exc))
        from PIL import ImageTk,ImageDraw
        display=image.copy()
        try:
            user=ctypes.windll.user32; user.GetDpiForWindow.argtypes=[ctypes.c_void_p]
            factor=max(96,user.GetDpiForWindow(self.monitor['hwnd']))/96
            logical,split,top=recognition.layout(image,factor)
            painter=ImageDraw.Draw(display)
            for body in vision.bubbles(logical,split,top):
                box=tuple(round(v*factor) for v in body.box)
                color='red' if body.side=='out' else '#00a0ff'
                painter.rectangle(box,outline=color,width=3)
                painter.text((box[0],box[1]),'OUT' if body.side=='out' else 'IN',fill=color)
        except ValueError:pass
        dialog=tk.Toplevel(self.root); dialog.title('所选窗口预览：点击输入框设置发送位置，或拖选对方消息区域')
        self.preview_dialog=dialog
        scale=min(1,1000/image.width,650/image.height)
        mode=tk.StringVar(value='input')
        ttk.Label(dialog,text='此图只来自锁定窗口。发送前需在图中点击输入框空白处确认一次。').pack(anchor='w',padx=8,pady=6)
        controls=ttk.Frame(dialog); controls.pack(fill='x')
        ttk.Radiobutton(controls,text='点击设置输入框',variable=mode,value='input').pack(side='left')
        ttk.Radiobutton(controls,text='拖选对方消息区域（可选，改善识别）',variable=mode,value='crop').pack(side='left')
        canvas=tk.Canvas(dialog,width=int(image.width*scale),height=int(image.height*scale)); canvas.pack()
        canvas.preview_image=ImageTk.PhotoImage(display.resize((int(image.width*scale),int(image.height*scale))))
        canvas.create_image(0,0,anchor='nw',image=canvas.preview_image)
        label=ttk.Label(dialog,text='未设置发送位置'); label.pack(anchor='w',padx=8,pady=6)
        origin=[]; box=[]
        def down(e):
            if mode.get()=='input':
                px,py=int(e.x/scale),int(e.y/scale)
                if not 0<px<image.width or not 0<py<image.height:return
                self.monitor['input']={'size':list(rect[2:]),'point':[px,py]}
                canvas.delete('input'); canvas.create_oval(e.x-6,e.y-6,e.x+6,e.y+6,outline='red',width=3,tags='input')
                label.configure(text='发送位置已确认；窗口移动无需重设，尺寸变化需重新确认。')
            else:
                origin[:]=[e.x,e.y]; canvas.delete('crop'); box[:]=[canvas.create_rectangle(e.x,e.y,e.x,e.y,outline='blue',width=2,tags='crop')]
        def move(e):
            if mode.get()=='crop' and box:canvas.coords(box[0],*origin,e.x,e.y)
        def up(e):
            if mode.get()!='crop' or not origin:return
            endx=max(0,min(e.x,int(image.width*scale)-1)); endy=max(0,min(e.y,int(image.height*scale)-1))
            left,top=min(origin[0],endx)/scale,min(origin[1],endy)/scale
            width,height=abs(origin[0]-endx)/scale,abs(origin[1]-endy)/scale
            if width<20 or height<20:return
            self.monitor['crop']={'size':list(rect[2:]),'rect':[int(left),int(top),int(width),int(height)]}
            label.configure(text='只识别蓝框内对方消息；请勿包含自己的回复或输入框。')
        canvas.bind('<Button-1>',down); canvas.bind('<B1-Motion>',move); canvas.bind('<ButtonRelease-1>',up)
        def clear():self.monitor['crop']=None; canvas.delete('crop'); label.configure(text='已恢复全窗口识别')
        ttk.Button(dialog,text='恢复全窗口识别',command=clear).pack(side='left',padx=8,pady=6)
        ttk.Button(dialog,text='完成',command=dialog.destroy).pack(side='right',padx=8,pady=6)
        dialog.transient(self.root); dialog.grab_set()

    def log(self,text):
        self.output.configure(state='normal'); self.output.insert('end',text+'\n'); self.output.see('end'); self.output.configure(state='disabled')

    def snapshot(self):
        cfg=dict(self.cfg); cfg.update({k:v.get().strip() for k,v in self.vars.items()})
        cfg['prompt']=self.prompt.get('1.0','end').strip(); cfg['auto']=self.auto.get()
        cfg.update(persona_enabled=self.studio.learn.get(),longer=self.studio.longer.get(),proactive=self.studio.proactive.get(),record_contact=self.studio.binding.get())
        return cfg

    def save(self):
        self.cfg=self.snapshot(); self.cfg['auto']=False
        (self.data_root/'config.json').write_text(json.dumps(self.cfg,ensure_ascii=False,indent=2),'utf-8')
        self.status.set('已保存配置；API Key 不保存')

    def start(self):
        if self.running:return
        cfg=self.snapshot(); key=self.key.get().strip()
        try:key,_=validate_connection(cfg,key)
        except ValueError as exc:return messagebox.showerror('模型配置需要修正',str(exc))
        if not self.monitor:return messagebox.showerror('尚未选择窗口','请在窗口列表选择微信窗口')
        if cfg['auto'] and not self.monitor['input']:
            return messagebox.showerror('确认发送位置','请先在窗口预览图上点击输入框位置')
        self.stop.clear(); self.awaiting.clear(); self.running=True; self.pending=None; self.seen=set(); self.history=[]
        self.start_button.configure(state='disabled'); self.status.set('监听中（Esc 全局停止；鼠标移到屏幕左上角也会停止）')
        self.window_choice.configure(state='disabled')
        threading.Thread(target=self.worker,args=(cfg,key),daemon=True).start()

    def test_connection(self):
        if self.running:return messagebox.showinfo('测试连接','请先停止监听')
        if self.testing_connection:return
        cfg=dict(self.snapshot(),prompt='你是模型连接测试助手，只输出OK。',max_tokens=128)
        try:key,_=validate_connection(cfg,self.key.get())
        except ValueError as exc:return messagebox.showerror('模型配置需要修正',str(exc))
        provider={'ollama':'Ollama','lmstudio':'LM Studio'}.get(cfg.get('provider'),'MiMo')
        self.testing_connection=True; self.status.set('正在测试 '+provider+' 连接，不操作微信')
        def test():
            try:
                completion(cfg,key,'请只回复OK',[])
                self.events.put(('connection_done',(True,provider+' 连接成功：模型已返回正文（未发送微信）')))
            except Exception as exc:self.events.put(('connection_done',(False,provider+' 连接测试失败：'+str(exc))))
        threading.Thread(target=test,daemon=True).start()

    def halted(self):
        if ctypes.windll.user32.GetAsyncKeyState(0x1B)&0x8000:self.stop.set()
        return self.stop.is_set()

    def inspect(self,hwnd):
        image,rect=vision.capture_window(hwnd,self.monitor['pid'])
        user=ctypes.windll.user32
        user.GetDpiForWindow.argtypes=[ctypes.c_void_p]
        dpi=max(96,user.GetDpiForWindow(hwnd)); factor=dpi/96
        crop_rect=None
        if self.monitor['crop']:
            crop=self.monitor['crop']
            if crop['size']!=list(rect[2:]):raise ValueError('窗口尺寸变化，请重新确认消息区域')
            crop_rect=crop['rect']
        view=asyncio.run(recognition.analyze(image,hwnd,rect,recognize_image,factor,crop_rect))
        view.input_point=vision.confirmed_point(rect,self.monitor['input']) if self.monitor['input'] else None
        return view

    def test_recognition(self):
        if self.running:return messagebox.showinfo('测试识别','请先停止监听')
        if not self.monitor:return messagebox.showinfo('测试识别','请先选择微信窗口')
        def test():
            try:
                view=self.inspect(self.monitor['hwnd'])
                self.events.put(('log','识别测试（未调用AI、未发送）：标题='+repr(view.title)))
                for body in view.messages:self.events.put(('log',('对方：'+body.text) if body.side=='in' else '自己：绿色气泡已排除'))
                self.events.put(('log','当前待回复：'+(view.message or '无（最新为自己发送，或无新文字）')))
            except Exception as exc:self.events.put(('log','识别测试失败：'+str(exc)))
        threading.Thread(target=test,daemon=True).start()

    def worker(self,cfg,key):
        try:
            hwnd=self.monitor['hwnd']
            view=self.inspect(hwnd)
            if not view.title:raise ValueError('聊天标题未能可靠识别，请先点“仅测试识别”检查')
            locked_title=view.title; locked_header=view.header
            record_contact=cfg.get('record_contact','')
            if not record_contact or record_contact=='自动按聊天标题匹配':record_contact=locked_title
            self.active_record_contact=record_contact
            self.history=self.studio.store.recent(record_contact)
            tracker=vision.MessageTracker(view.messages)
            self.events.put(('log','已锁定微信：'+view.title+'；当前旧消息已忽略。'))
            candidate=''; count=0
            header_changes=0
            while not self.halted():
                if self.stop.wait(cfg['interval']):break
                if self.awaiting.is_set():continue
                view=self.inspect(hwnd)
                if not vision.header_same(view.header,locked_header):
                    header_changes+=1; count=0
                    if header_changes>=3 and view.title and fingerprint(view.title)!=fingerprint(locked_title):
                        raise ValueError('连续三次确认聊天标题变化，监听已停止')
                    self.events.put(('log','标题图像暂不稳定，暂停本帧，不发送'))
                    continue
                header_changes=0; view.title=locked_title
                if cfg['title'] and not vision.title_equal(cfg['title'],locked_title):
                    count=0; continue
                token=fingerprint(repr([(b.side,b.text) for b in view.messages]))
                count=count+1 if token==candidate else 1; candidate=token
                if count<2:continue
                text=tracker.observe(view.messages)
                if not text:continue
                if len(''.join(text.split()))>=12 and any(vision.message_equal(vision.Bubble('in',(),text),vision.Bubble('in',(),turn['content'])) for turn in self.history if turn['role']=='assistant'):
                    self.events.put(('log','已排除与本程序历史回复相同的文字')); continue
                self.events.put(('log','['+view.title+'] 识别：'+text))
                reply_cfg=self.studio.reply_cfg(cfg,record_contact,text)
                self.events.put(('model_wait',time.monotonic()))
                self.events.put(('log','正在请求模型生成回复；本地模式会按上下文预算选择参考资料。'))
                try:answer=completion(reply_cfg,key,text,self.history)
                finally:self.events.put(('model_wait',None))
                if self.halted():break
                if not cfg['auto']:self.awaiting.set()
                self.events.put(('preview',(view,text,answer)))
                if cfg['auto']:self.deliver(view,text,answer)
                count=0
        except Exception as exc:self.events.put(('log','已停止：'+str(exc)))
        finally:self.stop.set(); self.events.put(('done',None))

    def deliver(self,expected,text,answer):
        import pyautogui
        import pyperclip
        if self.halted():return
        if not self.monitor['input']:raise ValueError('请停止监听，在窗口预览中设置发送位置')
        vision.validate_window(expected.hwnd,self.monitor['pid'])
        vision.activate(expected.hwnd); time.sleep(.3)
        if not vision.foreground(expected.hwnd):raise ValueError('无法激活锁定窗口，取消发送')
        current=self.inspect(expected.hwnd)
        title_ok=vision.header_same(current.header,expected.header) if expected.header else fingerprint(current.title)==fingerprint(expected.title)
        if not title_ok or not current.message or not vision.message_equal(vision.Bubble('in',(),current.message),vision.Bubble('in',(),expected.message)):
            raise ValueError('聊天或消息已变化，取消发送')
        vision.ensure_point(expected.hwnd,current.input_point)
        pyautogui.click(*current.input_point); time.sleep(0.25)
        # Refuse to append to a user draft. Clipboard is restored after the check.
        previous=pyperclip.paste(); pyperclip.copy('')
        if not vision.foreground(expected.hwnd):raise ValueError('窗口焦点变化，已停止')
        pyautogui.hotkey('ctrl','a'); pyautogui.hotkey('ctrl','c'); time.sleep(.2)
        draft=pyperclip.paste(); pyperclip.copy(previous)
        pyautogui.press('right')
        if draft.strip():raise ValueError('输入框存在草稿，已停止，请清空后重试')
        if self.halted():return
        vision.ensure_point(expected.hwnd,current.input_point)
        if not vision.foreground(expected.hwnd):raise ValueError('窗口焦点变化，已停止')
        pyperclip.copy(answer); pyautogui.hotkey('ctrl','v'); time.sleep(0.35)
        if self.halted():return
        current=self.inspect(expected.hwnd)
        title_ok=vision.header_same(current.header,expected.header) if expected.header else fingerprint(current.title)==fingerprint(expected.title)
        if not title_ok:raise ValueError('聊天标题图像已变化，取消发送')
        if self.halted():return
        if not vision.foreground(expected.hwnd):raise ValueError('窗口焦点变化，取消发送')
        pyautogui.press('enter')
        self.history.extend([{'role':'user','content':text},{'role':'assistant','content':answer}]); self.history=self.history[-12:]
        if hasattr(self,'studio'):self.studio.store.remember_exchange(getattr(self,'active_record_contact',expected.title),text,answer)
        self.awaiting.clear(); self.events.put(('sent',None))

    def send_pending(self):
        if not self.pending:return
        view,text,answer=self.pending
        if self.auto.get():return
        if self.stop.is_set():return messagebox.showinfo('发送','监听已停止，请重新启动后生成预览')
        self.pending=None
        def send():
            try:self.deliver(view,text,answer)
            except Exception as exc:self.events.put(('log','发送失败：'+str(exc))); self.stop.set()
        threading.Thread(target=send,daemon=True).start()

    def halt(self):
        self.stop.set(); self.pending=None; self.status.set('停止中，等待当前操作退出')

    def pump(self):
        if self.running:self.halted()
        while not self.events.empty():
            kind,value=self.events.get()
            if kind=='ui':
                try:value()
                except Exception as exc:self.status.set('界面操作失败：'+str(exc)); self.log('界面操作失败：'+str(exc))
            elif kind=='log':self.log(value)
            elif kind=='model_wait':self.generating_since=value
            elif kind=='preview':
                self.pending=value; self.log('回复预览：'+value[2])
            elif kind=='sent':self.pending=None; self.log('已发送')
            elif kind=='connection_done':
                self.testing_connection=False; self.status.set(value[1]); self.log(value[1])
                (messagebox.showinfo if value[0] else messagebox.showerror)('模型连接测试',value[1])
            elif kind=='done':self.running=False; self.start_button.configure(state='normal'); self.window_choice.configure(state='readonly'); self.status.set('已停止')
        if self.running and self.generating_since is not None:
            self.status.set(f'模型生成中，已等待 {int(time.monotonic()-self.generating_since)} 秒；停止后会丢弃本次结果。')
        self.root.after(200,self.pump)

    def close(self):
        self.halt(); self.studio.analysis_cancel.set(); self.root.destroy()

if __name__=='__main__':
    vision.enable_dpi_awareness()
    if '--check-image' in sys.argv:
        from PIL import Image
        image=Image.open(sys.argv[sys.argv.index('--check-image')+1]).convert('RGB')
        if '--box' in sys.argv:
            x,y,w,h=map(int,sys.argv[sys.argv.index('--box')+1].split(',')); image=image.crop((x,y,x+w,y+h))
        view=asyncio.run(recognition.analyze(image,0,(0,0,image.width,image.height),recognize_image))
        report={'version':'1.0.1','title':view.title,'reply_input':view.message,'messages':[{'side':b.side,'text':b.text} for b in view.messages]}
        (ROOT/'ocr-check.json').write_text(json.dumps(report,ensure_ascii=True,indent=2),'utf-8')
        sys.exit(0)
    if '--self-test' in sys.argv:
        assert fingerprint('你好\n 世界')==fingerprint('你好 世界')
        import pyautogui, pyperclip
        from PIL import Image, ImageDraw, ImageFont
        import tempfile
        from chatdata import normalize_rows
        temporary=tempfile.TemporaryDirectory(prefix='wechat-studio-test-')
        window=tk.Tk(); instance=App(window,Path(temporary.name))
        fixture=[{'talkerName':'演示联系人','senderName':'我','isSelf':True,'content':text,'type':1,'createTime':str(index)} for index,text in enumerate(('行啊，等下聊','哈哈，确实有点意思','你先说说怎么想的'))]
        fixture.append({'talkerName':'演示联系人','senderName':'朋友','isSelf':False,'content':'周末有什么安排','type':1,'createTime':'4'})
        instance.studio.store.import_rows(normalize_rows(fixture),'self-test'); instance.studio.refresh(); window.update()
        from backup import create_backup,restore_backup
        instance.studio.store.add_memory('演示联系人','演示记忆：周末骑车')
        instance.studio.store.set('learn',True)
        instance.studio.store.remember_exchange('演示联系人','演示问候','演示回复')
        test_backup=Path(temporary.name)/'self-test-backup.zip'
        counts=create_backup(instance.studio.store,DEFAULT,test_backup)
        instance.studio.store.add_memory('演示联系人','恢复后应消失的演示记忆')
        restored,restored_counts,safety=restore_backup(instance.studio.store,DEFAULT,test_backup,Path(temporary.name)/'config.json')
        assert counts==restored_counts and len(instance.studio.store.memory('演示联系人'))==1
        assert len(instance.studio.store.recent('演示联系人'))==2 and safety.is_file()
        # Exercise real Tk callbacks and worker/UI queue with simulated model outcomes.
        from unittest.mock import patch
        instance.vars['provider'].set('lmstudio'); instance.vars['base_url'].set('http://127.0.0.1:1234/v1'); instance.vars['model'].set('self-test-model')
        instance.studio.lab_input.insert('1.0','你好，周末有什么安排？')
        def finish_ui_job():
            deadline=time.monotonic()+5
            while instance.studio.jobs and time.monotonic()<deadline:window.update(); time.sleep(.05)
            assert instance.studio.jobs==0, 'UI worker did not finish'
        with patch('app.completion',side_effect=ValueError('模型输出截断（演示错误）')):
            instance.studio.lab_reply(); finish_ui_job()
            assert '生成失败：' in instance.studio.lab_output.get('1.0','end')
            assert str(instance.studio.lab_button['state'])=='normal'
        with patch('app.completion',return_value='演示回复：周末可以聊聊骑车安排。'):
            instance.studio.lab_reply(); finish_ui_job()
            assert '演示回复：' in instance.studio.lab_output.get('1.0','end')
            assert str(instance.studio.lab_button['state'])=='normal'
        user=ctypes.windll.user32
        user.GetAncestor.argtypes=[ctypes.c_void_p,ctypes.c_uint]; user.GetAncestor.restype=ctypes.c_void_p
        hwnd=user.GetAncestor(window.winfo_id(),2)
        captured,bounds=vision.capture_window(hwnd,os.getpid())
        assert captured.size==bounds[2:], (captured.size,bounds)
        captured.save(ROOT/'studio-self-test.png')
        for name,filename in [('模型与工具','settings'),('回复试验室','lab'),('微信自动回复','automation'),('聊天记录','records'),('AI 工具箱','toolbox'),('聊天统计','statistics')]:
            instance.studio.show(name); window.update()
            page_image,_=vision.capture_window(hwnd,os.getpid())
            page_image.save(ROOT/('ui-self-test-'+filename+'.png'))
        window.withdraw(); window.update(); window.destroy()
        temporary.cleanup()
        sample=Image.new('RGB',(600,100),'white')
        ImageDraw.Draw(sample).text((10,10),'你好 微信 测试',font=ImageFont.truetype('msyh.ttc',32),fill='black')
        result=asyncio.run(recognize_image(sample))
        assert '你好' in ''.join(result.text.split()), repr(result.text)
        (ROOT/'self-test.txt').write_text('SELF_TEST_OK 1.0.1: Studio pages + complete backup/restore + real Tk lab success/failure callbacks (simulated model) + isolated demo corpus + locked HWND capture + bundled RapidOCR model','utf-8')
        sys.exit(0)
    App(tk.Tk()).root.mainloop()
