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
               title='', auto=False, interval=3)

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
        return '',base+'/chat/completions'
    key=normalize_api_key(key)
    return key,base+'/chat/completions'

def completion(cfg, key, text, history):
    key,url=validate_connection(cfg,key)
    local=cfg.get('provider','mimo') in ('ollama','lmstudio')
    payload = dict(model=cfg['model'], messages=[{'role':'system','content':cfg['prompt']}]+history[-12:]+[{'role':'user','content':text}], stream=False)
    payload['max_tokens' if local else 'max_completion_tokens']=2048
    headers={'Content-Type':'application/json; charset=utf-8'}
    if not local:headers.update({'api-key':key,'Authorization':'Bearer '+key})
    req = urllib.request.Request(url, json.dumps(payload,ensure_ascii=False).encode('utf-8'),headers)
    try:
        open_request=urllib.request.build_opener(urllib.request.ProxyHandler({})).open if local else urllib.request.urlopen
        with open_request(req, timeout=300 if local else 90) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'本地模型服务 HTTP {exc.code}，请检查服务地址与本地模型名称' if local else f'MiMo HTTP {exc.code}，请检查 Key、地址、额度和模型') from None
    except UnicodeEncodeError:
        raise ValueError('请求头含非英文字符，尚未成功调用 MiMo。请重新复制 Key；若仍失败，请检查系统代理配置。') from None
    except urllib.error.URLError:
        raise RuntimeError('无法连接本地模型服务，请先启动服务并检查端口（Ollama 11434 / LM Studio 1234）' if local else '无法连接 MiMo，请检查网络、代理和接口地址') from None
    answer = result['choices'][0]['message']['content']
    if local and isinstance(answer,str):
        answer=re.sub(r'<think>.*?</think>','',answer,flags=re.S).strip()
        if '<think>' in answer:raise ValueError('本地模型只返回了未结束的思考内容，请换用聊天模型或检查模型配置')
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError('模型未返回回复正文')
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
        self.root=root
        root.title('微信分身 Studio · 0.5.1')
        root.geometry('800x780')
        self.cfg=DEFAULT.copy()
        if (ROOT/'config.json').exists():
            try: self.cfg.update(json.loads((ROOT/'config.json').read_text('utf-8')))
            except Exception: messagebox.showwarning('配置', '配置无法读取，使用默认值')
        if self.cfg['prompt']=='你是微信聊天助手。自然、简短地用中文回复对方，不编造事实。只输出要发送的正文。':self.cfg['prompt']=DEFAULT['prompt']
        self.events=queue.Queue(); self.stop=threading.Event(); self.running=False
        self.history=[]; self.pending=None; self.seen=set(); self.vars={}; self.awaiting=threading.Event()
        self.windows=[]; self.monitor=None; self.preview_dialog=None
        from studio_ui import StudioUI
        self.studio=StudioUI(self,data_root or ROOT)
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
        (ROOT/'config.json').write_text(json.dumps(self.cfg,ensure_ascii=False,indent=2),'utf-8')
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
        cfg=self.snapshot()
        try:key,_=validate_connection(cfg,self.key.get())
        except ValueError as exc:return messagebox.showerror('模型配置需要修正',str(exc))
        provider={'ollama':'Ollama','lmstudio':'LM Studio'}.get(cfg.get('provider'),'MiMo')
        self.status.set('正在测试 '+provider+' 连接，不操作微信')
        def test():
            try:
                completion(cfg,key,'请只回复OK',[])
                self.events.put(('log',provider+' 连接成功：模型已返回正文（未发送微信）'))
            except Exception as exc:self.events.put(('log',provider+' 连接测试失败：'+str(exc)))
            finally:self.events.put(('connection_done',None))
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
                answer=completion(reply_cfg,key,text,self.history)
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
            if kind=='ui':value()
            elif kind=='log':self.log(value)
            elif kind=='preview':
                self.pending=value; self.log('回复预览：'+value[2])
            elif kind=='sent':self.pending=None; self.log('已发送')
            elif kind=='connection_done':self.status.set('模型连接测试结束，详情见微信自动回复页日志')
            elif kind=='done':self.running=False; self.start_button.configure(state='normal'); self.window_choice.configure(state='readonly'); self.status.set('已停止')
        self.root.after(200,self.pump)

    def close(self):self.halt(); self.root.destroy()

if __name__=='__main__':
    vision.enable_dpi_awareness()
    if '--check-image' in sys.argv:
        from PIL import Image
        image=Image.open(sys.argv[sys.argv.index('--check-image')+1]).convert('RGB')
        if '--box' in sys.argv:
            x,y,w,h=map(int,sys.argv[sys.argv.index('--box')+1].split(',')); image=image.crop((x,y,x+w,y+h))
        view=asyncio.run(recognition.analyze(image,0,(0,0,image.width,image.height),recognize_image))
        report={'version':'0.5.1','title':view.title,'reply_input':view.message,'messages':[{'side':b.side,'text':b.text} for b in view.messages]}
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
        user=ctypes.windll.user32
        user.GetAncestor.argtypes=[ctypes.c_void_p,ctypes.c_uint]; user.GetAncestor.restype=ctypes.c_void_p
        hwnd=user.GetAncestor(window.winfo_id(),2)
        captured,bounds=vision.capture_window(hwnd,os.getpid())
        assert captured.size==bounds[2:], (captured.size,bounds)
        captured.save(ROOT/'studio-self-test.png')
        for name,filename in [('模型与工具','settings'),('回复试验室','lab'),('微信自动回复','automation'),('聊天记录','records')]:
            instance.studio.show(name); window.update()
            page_image,_=vision.capture_window(hwnd,os.getpid())
            page_image.save(ROOT/('ui-self-test-'+filename+'.png'))
        window.withdraw(); window.update(); window.destroy()
        temporary.cleanup()
        sample=Image.new('RGB',(600,100),'white')
        ImageDraw.Draw(sample).text((10,10),'你好 微信 测试',font=ImageFont.truetype('msyh.ttc',32),fill='black')
        result=asyncio.run(recognize_image(sample))
        assert '你好' in ''.join(result.text.split()), repr(result.text)
        (ROOT/'self-test.txt').write_text('SELF_TEST_OK 0.5.1: Studio pages + isolated demo corpus + locked HWND capture + bundled RapidOCR model','utf-8')
        sys.exit(0)
    App(tk.Tk()).root.mainloop()
