import asyncio
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import urllib.request
import urllib.error
import vision

ROOT = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
DEFAULT = dict(base_url='https://api.xiaomimimo.com/v1', model='mimo-v2.6-pro',
               prompt='你是微信聊天助手。自然、简短地用中文回复对方，不编造事实。只输出要发送的正文。',
               title='', auto=False, interval=3)

def fingerprint(text):
    return hashlib.sha256(''.join(text.split()).encode()).hexdigest()

def completion(cfg, key, text, history):
    payload = dict(model=cfg['model'], messages=[{'role':'system','content':cfg['prompt']}]+history[-12:]+[{'role':'user','content':text}], max_completion_tokens=2048, stream=False)
    url = cfg['base_url'].rstrip('/')+'/chat/completions'
    if not url.startswith('https://'):
        raise ValueError('接口地址必须使用 HTTPS')
    req = urllib.request.Request(url, json.dumps(payload).encode(), {'Content-Type':'application/json','api-key':key,'Authorization':'Bearer '+key})
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'MiMo HTTP {exc.code}，请检查 Key、地址、额度和模型') from None
    answer = result['choices'][0]['message']['content']
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError('模型未返回回复正文')
    if len(answer) > 1500:
        raise ValueError('回复超过1500字，请缩短提示词要求')
    return answer.strip()

async def recognize_image(image):
    import winocr
    return await winocr.recognize_pil(image, lang='zh-Hans')

class App:
    def __init__(self, root):
        self.root=root
        root.title('微信 MiMo 助手 · 全窗口 OCR 0.2.0')
        root.geometry('800x780')
        self.cfg=DEFAULT.copy()
        if (ROOT/'config.json').exists():
            try: self.cfg.update(json.loads((ROOT/'config.json').read_text('utf-8')))
            except Exception: messagebox.showwarning('配置', '配置无法读取，使用默认值')
        self.events=queue.Queue(); self.stop=threading.Event(); self.running=False
        self.history=[]; self.pending=None; self.seen=set(); self.vars={}; self.awaiting=threading.Event()
        frame=ttk.Frame(root,padding=16); frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='微信窗口截图识别 + MiMo 回复',font=('Microsoft YaHei UI',16,'bold')).pack(anchor='w')
        ttk.Label(frame,text='自动找到微信，识别未读聊天并生成回复；无需框选或校准。').pack(anchor='w',pady=8)
        for name,label in [('base_url','接口地址'),('model','模型'),('title','仅回复此聊天（可留空）')]:
            row=ttk.Frame(frame); row.pack(fill='x',pady=3)
            ttk.Label(row,text=label,width=26).pack(side='left')
            self.vars[name]=tk.StringVar(value=self.cfg[name]); ttk.Entry(row,textvariable=self.vars[name]).pack(side='left',fill='x',expand=True)
        row=ttk.Frame(frame); row.pack(fill='x',pady=3)
        ttk.Label(row,text='API Key（仅本次运行保留）',width=26).pack(side='left')
        self.key=tk.StringVar(value=os.environ.get('MIMO_API_KEY','')); ttk.Entry(row,textvariable=self.key,show='•').pack(side='left',fill='x',expand=True)
        row=ttk.Frame(frame); row.pack(fill='x',pady=6)
        for label,url in [('按量付费','https://api.xiaomimimo.com/v1'),('Token Plan 中国','https://token-plan-cn.xiaomimimo.com/v1')]:
            ttk.Button(row,text=label,command=lambda u=url:self.vars['base_url'].set(u)).pack(side='left',padx=3)
        ttk.Label(frame,text='回复提示词').pack(anchor='w')
        self.prompt=tk.Text(frame,height=3,wrap='word'); self.prompt.insert('1.0',self.cfg['prompt']); self.prompt.pack(fill='x')
        self.auto=tk.BooleanVar(value=False)
        ttk.Checkbutton(frame,text='开启自动发送（默认只生成预览）',variable=self.auto).pack(anchor='w')
        row=ttk.Frame(frame); row.pack(fill='x',pady=8)
        self.start_button=ttk.Button(row,text='开始监听',command=self.start); self.start_button.pack(side='left',padx=3)
        ttk.Button(row,text='停止 / Esc',command=self.halt).pack(side='left',padx=3)
        ttk.Button(row,text='发送当前预览',command=self.send_pending).pack(side='left',padx=3)
        ttk.Button(row,text='保存配置',command=self.save).pack(side='left',padx=3)
        ttk.Label(frame,text='识别与回复预览（不写入磁盘）').pack(anchor='w')
        self.output=tk.Text(frame,wrap='word',height=14,state='disabled'); self.output.pack(fill='both',expand=True)
        self.status=tk.StringVar(value='未启动'); ttk.Label(frame,textvariable=self.status).pack(anchor='w',pady=5)
        root.bind('<Escape>',lambda _:self.halt()); root.protocol('WM_DELETE_WINDOW',self.close)
        root.after(200,self.pump)

    def log(self,text):
        self.output.configure(state='normal'); self.output.insert('end',text+'\n'); self.output.see('end'); self.output.configure(state='disabled')

    def snapshot(self):
        cfg=dict(self.cfg); cfg.update({k:v.get().strip() for k,v in self.vars.items()})
        cfg['prompt']=self.prompt.get('1.0','end').strip(); cfg['auto']=self.auto.get()
        return cfg

    def save(self):
        self.cfg=self.snapshot(); self.cfg['auto']=False
        (ROOT/'config.json').write_text(json.dumps(self.cfg,ensure_ascii=False,indent=2),'utf-8')
        self.status.set('已保存配置；API Key 不保存')

    def start(self):
        if self.running:return
        cfg=self.snapshot(); key=self.key.get().strip()
        if not key:
            return messagebox.showerror('尚未配置','请填写 MiMo API Key')
        self.stop.clear(); self.awaiting.clear(); self.running=True; self.pending=None; self.seen=set(); self.history=[]
        self.start_button.configure(state='disabled'); self.status.set('监听中（Esc 全局停止；鼠标移到屏幕左上角也会停止）')
        threading.Thread(target=self.worker,args=(cfg,key),daemon=True).start()

    def halted(self):
        if ctypes.windll.user32.GetAsyncKeyState(0x1B)&0x8000:self.stop.set()
        return self.stop.is_set()

    def inspect(self,hwnd):
        import pyautogui
        if not vision.foreground(hwnd):
            vision.activate(hwnd); time.sleep(.4)
        if not vision.foreground(hwnd):raise ValueError('微信未处于前台，请点击微信后重新开始')
        rect=vision.window_rect(hwnd)
        image=pyautogui.screenshot(region=rect)
        result=asyncio.run(recognize_image(image))
        return vision.parse(image,vision.flatten(result),hwnd,rect)

    def worker(self,cfg,key):
        try:
            import pyautogui
            hwnd=vision.find_wechat(); vision.activate(hwnd); time.sleep(.5)
            view=self.inspect(hwnd)
            self.seen.add(fingerprint(view.title+'\n'+view.message))
            self.events.put(('log','已自动找到微信：'+view.title+'；当前旧消息已忽略。'))
            candidate=''; count=0
            visited={}; histories={}
            while not self.halted():
                if self.stop.wait(cfg['interval']):break
                if self.awaiting.is_set():continue
                view=self.inspect(hwnd)
                # Only switch between chats before generating a reply, never during preview/send.
                if view.unread and count==0:
                    target=next((p for p in view.unread if time.monotonic()-visited.get(p,0)>30),None)
                    if target:
                        visited[target]=time.monotonic(); pyautogui.click(*target); time.sleep(.7)
                        view=self.inspect(hwnd)
                if cfg['title'] and fingerprint(cfg['title'])!=fingerprint(view.title):
                    count=0; continue
                text=view.message
                if not text:continue
                token=fingerprint(view.title+'\n'+text)
                count=count+1 if token==candidate else 1; candidate=token
                if token in self.seen:count=0; continue
                if count<2:continue
                self.seen.add(token)
                self.events.put(('log','['+view.title+'] 识别：'+text))
                self.history=histories.setdefault(view.title,[])
                answer=completion(cfg,key,text,self.history)
                if self.halted():break
                if not cfg['auto']:self.awaiting.set()
                self.events.put(('preview',(view,text,answer)))
                if cfg['auto']:self.deliver(view,text,answer)
                histories[view.title]=self.history
                count=0
        except Exception as exc:self.events.put(('log','已停止：'+str(exc)))
        finally:self.stop.set(); self.events.put(('done',None))

    def deliver(self,expected,text,answer):
        import pyautogui
        import pyperclip
        if self.halted():return
        current=self.inspect(expected.hwnd)
        if fingerprint(current.title)!=fingerprint(expected.title) or fingerprint(current.message)!=fingerprint(text):
            raise ValueError('聊天或消息已变化，取消发送')
        pyautogui.click(*current.input_point); time.sleep(0.25)
        # Refuse to append to a user draft. Clipboard is restored after the check.
        previous=pyperclip.paste(); pyperclip.copy('')
        pyautogui.hotkey('ctrl','a'); pyautogui.hotkey('ctrl','c'); time.sleep(.2)
        draft=pyperclip.paste(); pyperclip.copy(previous)
        pyautogui.press('right')
        if draft.strip():raise ValueError('输入框存在草稿，已停止，请清空后重试')
        if self.halted():return
        pyperclip.copy(answer); pyautogui.hotkey('ctrl','v'); time.sleep(0.35)
        if self.halted():return
        current=self.inspect(expected.hwnd)
        if fingerprint(current.title)!=fingerprint(expected.title):raise ValueError('聊天已变化，取消发送')
        if self.halted():return
        pyautogui.press('enter')
        self.history.extend([{'role':'user','content':text},{'role':'assistant','content':answer}]); self.history=self.history[-12:]
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
            if kind=='log':self.log(value)
            elif kind=='preview':
                self.pending=value; self.log('回复预览：'+value[2])
            elif kind=='sent':self.pending=None; self.log('已发送')
            elif kind=='done':self.running=False; self.start_button.configure(state='normal'); self.status.set('已停止')
        self.root.after(200,self.pump)

    def close(self):self.halt(); self.root.destroy()

if __name__=='__main__':
    if '--self-test' in sys.argv:
        assert fingerprint('你好\n 世界')==fingerprint('你好 世界')
        import pyautogui, pyperclip, winocr
        from PIL import Image, ImageDraw, ImageFont
        window=tk.Tk(); instance=App(window); window.withdraw(); window.update(); window.destroy()
        sample=Image.new('RGB',(600,100),'white')
        ImageDraw.Draw(sample).text((10,10),'你好 微信 测试',font=ImageFont.truetype('msyh.ttc',32),fill='black')
        result=asyncio.run(recognize_image(sample))
        assert '你好' in ''.join(result.text.split()), repr(result.text)
        (ROOT/'self-test.txt').write_text('SELF_TEST_OK: GUI + Chinese OCR + imports','utf-8')
        sys.exit(0)
    App(tk.Tk()).root.mainloop()
