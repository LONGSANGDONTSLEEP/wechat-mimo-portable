"""Unified desktop workspace for WeTrace exports, persona memory and OCR replies."""
import json
import os
from pathlib import Path
import subprocess
import threading
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
import urllib.request
import webbrowser
from chatdata import ChatStore,read_rows,normalize_rows,ALIASES

class StudioUI:
    def __init__(self,app,root_path):
        self.app=app; self.root=app.root; self.root_path=Path(root_path)
        self.store=ChatStore(self.root_path/'data'/'persona.sqlite3')
        self.pages={}; self.nav={}; self.tool_process=None; self.tool_log=None
        self.learn=tk.BooleanVar(value=self.store.get('learn',True))
        self.longer=tk.BooleanVar(value=self.store.get('longer',True))
        self.proactive=tk.BooleanVar(value=self.store.get('proactive',True))
        self.binding=tk.StringVar(value=self.store.get('binding','自动按聊天标题匹配'))
        self.lab_contact=tk.StringVar(); self.memory_contact=tk.StringVar()
        self.backend=tk.StringVar(value=str(self.root_path/'vendor'/'wetrace'/'wetrace.exe'))
        self._style()
        self.root.title('微信分身 Studio · 0.5.1')
        self.root.geometry('1180x850'); self.root.minsize(1040,760)
        shell=ttk.Frame(self.root); shell.pack(fill='both',expand=True)
        rail=tk.Frame(shell,bg='#152538',width=205); rail.pack(side='left',fill='y'); rail.pack_propagate(False)
        tk.Label(rail,text='微信分身',font=('Microsoft YaHei UI',22,'bold'),fg='white',bg='#152538').pack(anchor='w',padx=22,pady=(32,0))
        tk.Label(rail,text='STUDIO  /  本地记忆工作台',font=('Microsoft YaHei UI',9),fg='#9bb0c7',bg='#152538').pack(anchor='w',padx=23,pady=(6,30))
        body=ttk.Frame(shell,padding=(30,24)); body.pack(side='left',fill='both',expand=True)
        for name in ('总览','聊天记录','我的分身','联系人记忆','回复试验室','微信自动回复','模型与工具'):
            page=ttk.Frame(body); page.place(relx=0,rely=0,relwidth=1,relheight=1)
            self.pages[name]=page
            button=tk.Button(rail,text=name,anchor='w',relief='flat',bd=0,bg='#152538',fg='#c8d5e5',activebackground='#253d55',activeforeground='white',font=('Microsoft YaHei UI',12),padx=24,pady=13,command=lambda n=name:self.show(n))
            button.pack(fill='x',padx=10,pady=2); self.nav[name]=button
        tk.Label(rail,text='记录保存在本机\n支持 MiMo / 本地 Ollama',justify='left',font=('Microsoft YaHei UI',9),fg='#9bb0c7',bg='#152538',wraplength=170).pack(side='bottom',padx=20,pady=30)
        self.overview(); self.corpus(); self.persona(); self.memories(); self.laboratory(); self.automation(); self.settings()
        ttk.Label(self.root,textvariable=app.status,padding=(220,8),style='Hint.TLabel').pack(side='bottom',fill='x')
        self.refresh(); self.show('总览')

    def _style(self):
        style=ttk.Style(); style.theme_use('clam')
        style.configure('.',font=('Microsoft YaHei UI',10),background='#f4f7fb',foreground='#22344a')
        style.configure('TFrame',background='#f4f7fb'); style.configure('TLabel',background='#f4f7fb')
        style.configure('Title.TLabel',font=('Microsoft YaHei UI',23,'bold'),foreground='#16283f')
        style.configure('Hint.TLabel',foreground='#687b91',font=('Microsoft YaHei UI',10))
        style.configure('TButton',padding=(12,8),background='white',bordercolor='#d8e1ec')
        style.configure('Accent.TButton',background='#176bde',foreground='white',bordercolor='#176bde')
        style.map('Accent.TButton',background=[('active','#1258b8')])
        style.configure('Treeview',rowheight=30,fieldbackground='white',background='white')
        style.configure('Treeview.Heading',font=('Microsoft YaHei UI',10,'bold'),padding=8)
        style.configure('TEntry',padding=7,fieldbackground='white')

    def show(self,name):
        self.current_page=name
        self.pages[name].tkraise()
        for key,button in self.nav.items():button.configure(bg='#28476b' if key==name else '#152538',fg='white' if key==name else '#c8d5e5')

    def heading(self,page,title,hint):
        ttk.Label(page,text=title,style='Title.TLabel').pack(anchor='w')
        ttk.Label(page,text=hint,style='Hint.TLabel',wraplength=850).pack(anchor='w',pady=(8,22))

    def text(self,parent,height=8):
        outer=ttk.Frame(parent); outer.pack(fill='both',expand=True,pady=6)
        widget=tk.Text(outer,height=height,wrap='word',font=('Microsoft YaHei UI',10),bg='white',fg='#22344a',relief='flat',padx=14,pady=12)
        bar=ttk.Scrollbar(outer,command=widget.yview); widget.configure(yscrollcommand=bar.set)
        bar.pack(side='right',fill='y'); widget.pack(side='left',fill='both',expand=True)
        return widget

    def set_text(self,widget,text):
        widget.configure(state='normal'); widget.delete('1.0','end'); widget.insert('1.0',text); widget.configure(state='disabled')

    def run(self,work,done=None):
        def thread():
            try:
                value=work()
                if done:self.app.events.put(('ui',lambda v=value:done(v)))
            except Exception as exc:
                def fail(message=str(exc)):
                    self.app.status.set('操作未完成，请检查提示'); messagebox.showerror('操作未完成',message)
                self.app.events.put(('ui',fail))
        threading.Thread(target=thread,daemon=True).start()

    def overview(self):
        page=self.pages['总览']; self.heading(page,'让回复，更像你','导入 WeTrace 记录，确认本人身份，先在试验室检查，再接入微信。')
        row=ttk.Frame(page); row.pack(fill='x',pady=10); self.metrics={}
        for name,label in [('total','已导入消息'),('self','本人消息'),('contacts','联系人'),('unknown','待确认身份')]:
            card=tk.Frame(row,bg='white',padx=20,pady=20); card.pack(side='left',fill='both',expand=True,padx=(0,12))
            value=tk.StringVar(value='0'); self.metrics[name]=value
            tk.Label(card,textvariable=value,font=('Microsoft YaHei UI',27,'bold'),fg='#176bde',bg='white').pack(anchor='w')
            tk.Label(card,text=label,font=('Microsoft YaHei UI',10),fg='#657a94',bg='white').pack(anchor='w',pady=(8,0))
        for title,description,target in [
            ('01  导入你的历史','导入 JSON / CSV 或 WeTrace HTML 备份；先预览，再入库。','聊天记录'),
            ('02  建立你的分身','只用本人消息学习语气，给每位联系人保留独立记忆。','我的分身'),
            ('03  试着聊一聊','检查表达风格和引用的记忆，不直接发送到微信。','回复试验室'),
            ('04  接入当前聊天','锁定微信窗口，OCR 识别，默认预览后发送。','微信自动回复')]:
            card=ttk.LabelFrame(page,text=title,padding=16); card.pack(fill='x',pady=8)
            ttk.Label(card,text=description).pack(side='left'); ttk.Button(card,text='打开 →',command=lambda t=target:self.show(t)).pack(side='right')
        self.overview_note=ttk.Label(page,text='',style='Hint.TLabel',wraplength=850); self.overview_note.pack(anchor='w',pady=16)

    def corpus(self):
        page=self.pages['聊天记录']; self.heading(page,'聊天记录','支持你这份 WeTrace 的 window.CHAT_DATA HTML 导出；不会执行导出文件中的脚本。')
        row=ttk.Frame(page); row.pack(fill='x')
        ttk.Button(row,text='＋ 导入文件',style='Accent.TButton',command=self.import_files).pack(side='left',padx=(0,8))
        ttk.Button(row,text='导入 HTML 备份文件夹',command=self.import_folder).pack(side='left',padx=4)
        ttk.Button(row,text='启动 WeTrace 导出工具',command=self.start_wetrace).pack(side='left',padx=4)
        ttk.Button(row,text='导出所选记录',command=self.export_contact).pack(side='right')
        row=ttk.Frame(page); row.pack(fill='x',pady=14)
        self.contact_choice=ttk.Combobox(row,state='readonly'); self.contact_choice.pack(side='left',fill='x',expand=True)
        self.contact_choice.bind('<<ComboboxSelected>>',lambda e:self.show_messages())
        ttk.Button(row,text='刷新',command=self.refresh).pack(side='left',padx=6)
        columns=('time','sender','role','text')
        self.message_table=ttk.Treeview(page,columns=columns,show='headings',height=15)
        for key,label,width in [('time','时间',135),('sender','发送者',110),('role','身份',65),('text','内容',480)]:
            self.message_table.heading(key,text=label); self.message_table.column(key,width=width,stretch=key=='text')
        bar=ttk.Scrollbar(page,command=self.message_table.yview); self.message_table.configure(yscrollcommand=bar.set)
        bar.pack(side='right',fill='y'); self.message_table.pack(fill='both',expand=True)
        self.message_table.bind('<Double-1>',self.message_detail)
        ttk.Label(page,text='双击查看完整消息。未知身份的消息不会用于学习你的说话风格。',style='Hint.TLabel').pack(anchor='w',pady=10)

    def import_files(self):
        paths=filedialog.askopenfilenames(title='选择 WeTrace 导出文件',filetypes=[('聊天导出','*.json *.jsonl *.ndjson *.csv *.tsv *.html *.htm *.txt'),('所有文件','*.*')])
        if paths:self.load_import(paths)

    def import_folder(self):
        folder=filedialog.askdirectory(title='选择 WeTrace HTML 备份文件夹')
        if not folder:return
        paths=list(Path(folder).rglob('*.html'))
        if len(paths)>1500:return messagebox.showerror('文件过多','请按联系人分批导入，每次最多1500个HTML文件')
        self.load_import(paths)

    def load_import(self,paths):
        self.app.status.set('正在解析导出文件，仅在本机处理…')
        def load():
            loaded=[]; errors=[]
            for path in paths:
                try:loaded.append((str(path),read_rows(path)))
                except Exception as exc:errors.append(Path(path).name+'：'+str(exc))
            return loaded,errors
        self.run(load,self.import_preview)

    def import_preview(self,result):
        loaded,errors=result
        if not loaded:return messagebox.showerror('未找到消息','\n'.join(errors[:8]) or '没有可导入文件')
        dialog=tk.Toplevel(self.root); dialog.title('导入预览 · 确认本人身份'); dialog.geometry('1000x650'); dialog.transient(self.root); dialog.grab_set()
        frame=ttk.Frame(dialog,padding=20); frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='先确认谁是你，再开始学习',font=('Microsoft YaHei UI',17,'bold')).pack(anchor='w')
        ttk.Label(frame,text='有 isSelf / IsSender 标记时自动识别。没有标记时，填写你在导出记录中的准确称呼，以分号分隔。',wraplength=950).pack(anchor='w',pady=8)
        aliases=tk.StringVar(value=self.store.get('self_names',''))
        row=ttk.Frame(frame); row.pack(fill='x',pady=6); ttk.Label(row,text='我的称呼').pack(side='left'); ttk.Entry(row,textvariable=aliases).pack(side='left',fill='x',expand=True,padx=10)
        mapping={}; keys=sorted({str(k) for row in loaded[0][1][:100] for k in row})
        row=ttk.Frame(frame); row.pack(fill='x',pady=5)
        for key,label in [('text','消息列'),('sender','发送者列'),('self','本人标记列'),('contact','联系人列')]:
            ttk.Label(row,text=label).pack(side='left',padx=4)
            var=tk.StringVar(value='自动'); mapping[key]=var
            ttk.Combobox(row,textvariable=var,values=['自动']+keys,state='readonly',width=13).pack(side='left')
        summary=tk.StringVar(); ttk.Label(frame,textvariable=summary,foreground='#176bde').pack(anchor='w',pady=10)
        table=ttk.Treeview(frame,columns=('contact','sender','role','text'),show='headings')
        for key,label,width in [('contact','联系人',120),('sender','发送者',120),('role','身份',70),('text','内容',580)]:table.heading(key,text=label); table.column(key,width=width)
        table.pack(fill='both',expand=True)
        normalized=[]
        def recalc():
            normalized.clear(); selected={key:var.get() for key,var in mapping.items() if var.get()!='自动'}
            names=aliases.get().replace('；',';').split(';')
            for path,rows in loaded:
                p=Path(path); default=p.parent.name if p.stem.lower()=='index' else p.stem
                normalized.append((path,normalize_rows(rows,default,names,selected)))
            all_rows=[r for path,rows in normalized for r in rows]
            own=sum(r['role']=='self' for r in all_rows); unknown=sum(r['role']=='unknown' for r in all_rows)
            summary.set(f'{len(all_rows)} 条文本 · 本人 {own} 条 · 身份未确认 {unknown} 条 · 未解析文件 {len(errors)} 个')
            table.delete(*table.get_children())
            for r in all_rows[:120]:table.insert('', 'end', values=(r['contact'],r['sender'],{'self':'本人','other':'对方','unknown':'未知'}[r['role']],r['text'][:150]))
        def commit():
            recalc()
            total=sum(len(rows) for path,rows in normalized)
            if not total:return messagebox.showerror('没有可导入内容','请检查消息字段映射',parent=dialog)
            if not any(r['role']=='self' for path,rows in normalized for r in rows):
                if not messagebox.askyesno('未确认本人','这些记录没有确认的本人消息，暂不能学习你的说话风格。仍导入到记录库吗？',parent=dialog):return
            self.store.set('self_names',aliases.get()); batch=list(normalized)
            dialog.destroy(); self.app.status.set('正在写入本地记录库…')
            def done(count):
                self.refresh(); self.app.status.set(f'导入完成：新增 {count} 条，重复记录已跳过')
                if errors:messagebox.showinfo('部分文件未导入','\n'.join(errors[:8]))
            self.run(lambda:sum(self.store.import_rows(rows,path) for path,rows in batch),done)
        bottom=ttk.Frame(frame); bottom.pack(fill='x',pady=12)
        ttk.Button(bottom,text='更新预览',command=recalc).pack(side='left')
        ttk.Button(bottom,text='确认导入',style='Accent.TButton',command=commit).pack(side='right')
        recalc()

    def refresh(self):
        stats=self.store.stats()
        for key,var in self.metrics.items():var.set(str(stats[key]))
        contacts=[r['contact'] for r in self.store.contacts()]
        for combo in (self.contact_choice,self.memory_combo,self.lab_combo):combo['values']=contacts
        self.binding_combo['values']=['自动按聊天标题匹配']+contacts
        if contacts and not self.contact_choice.get():self.contact_choice.set(contacts[0])
        if contacts and not self.memory_contact.get():self.memory_contact.set(contacts[0])
        if contacts and not self.lab_contact.get():self.lab_contact.set(contacts[0])
        style=self.store.style(); self.set_text(self.style_view,style['summary']+'\n\n本人示例（仅供学习语气）：\n\n'+'\n\n'.join(style['examples']))
        self.overview_note.configure(text=style['summary'])
        self.show_messages(); self.load_memories()

    def show_messages(self):
        self.visible_rows=self.store.messages(self.contact_choice.get(),500)
        self.message_table.delete(*self.message_table.get_children())
        for index,row in enumerate(self.visible_rows):self.message_table.insert('', 'end',iid=str(index),values=(row['ts'],row['sender'],{'self':'本人','other':'对方','unknown':'未知'}[row['role']],row['content'][:200]))

    def message_detail(self,event=None):
        selected=self.message_table.selection()
        if selected:messagebox.showinfo('消息详情',self.visible_rows[int(selected[0])]['content'])

    def export_contact(self):
        contact=self.contact_choice.get()
        if not contact:return
        path=filedialog.asksaveasfilename(defaultextension='.json',filetypes=[('JSON','*.json')],title='导出所选联系人的本地记录')
        if not path:return
        records=self.store.messages(contact,1000000)
        rows=[dict(contact=r['contact'],sender=r['sender'],role=r['role'],time=r['ts'],content=r['content']) for r in records]
        Path(path).write_text(json.dumps({'messages':rows},ensure_ascii=False,indent=2),'utf-8'); self.app.status.set('记录已导出到所选位置')

    def persona(self):
        page=self.pages['我的分身']; self.heading(page,'我的分身','从已确认的本人消息中提取风格；这是风格示例与记忆增强，不是重新训练 MiMo 底层模型。')
        for label,var in [('使用我的聊天风格',self.learn),('回应更充分：接住话题，通常2至5句',self.longer),('更主动接话：自然追问，不机械反问',self.proactive)]:ttk.Checkbutton(page,text=label,variable=var,command=self.save_persona).pack(anchor='w',pady=6)
        ttk.Label(page,text='自动回复使用的记录联系人（标题与导出名称不同时可手动绑定）').pack(anchor='w',pady=(16,5))
        self.binding_combo=ttk.Combobox(page,textvariable=self.binding,state='readonly'); self.binding_combo.pack(fill='x'); self.binding_combo.bind('<<ComboboxSelected>>',lambda e:self.save_persona())
        self.style_view=self.text(page,12)
        ttk.Button(page,text='从本地记录重新提取风格',command=self.refresh).pack(anchor='w',pady=8)

    def save_persona(self):
        for key,value in [('learn',self.learn.get()),('longer',self.longer.get()),('proactive',self.proactive.get()),('binding',self.binding.get())]:self.store.set(key,value)
        self.app.status.set('分身设置已保存')

    def memories(self):
        page=self.pages['联系人记忆']; self.heading(page,'一位联系人，一份记忆','记忆按联系人隔离；只保存你明确添加的事实，发送成功后的对话另存为该联系人的近期上下文。')
        self.memory_combo=ttk.Combobox(page,textvariable=self.memory_contact,state='readonly'); self.memory_combo.pack(fill='x'); self.memory_combo.bind('<<ComboboxSelected>>',lambda e:self.load_memories())
        self.memory_list=tk.Listbox(page,font=('Microsoft YaHei UI',11),bg='white',relief='flat',height=11); self.memory_list.pack(fill='both',expand=True,pady=15)
        self.memory_input=tk.Text(page,height=4,font=('Microsoft YaHei UI',11),wrap='word',relief='flat',padx=12,pady=10); self.memory_input.pack(fill='x')
        row=ttk.Frame(page); row.pack(fill='x',pady=10)
        ttk.Button(row,text='添加已确认的记忆',style='Accent.TButton',command=self.add_memory).pack(side='left')
        ttk.Button(row,text='删除选中记忆',command=self.delete_memory).pack(side='left',padx=8)

    def load_memories(self):
        self.memory_rows=self.store.memory(self.memory_contact.get()); self.memory_list.delete(0,'end')
        for row in self.memory_rows:self.memory_list.insert('end',row['content'])

    def add_memory(self):
        try:self.store.add_memory(self.memory_contact.get(),self.memory_input.get('1.0','end'))
        except ValueError as exc:return messagebox.showerror('记忆',str(exc))
        self.memory_input.delete('1.0','end'); self.load_memories()

    def delete_memory(self):
        selected=self.memory_list.curselection()
        if selected:self.store.delete_memory(self.memory_rows[selected[0]]['id']); self.load_memories()

    def laboratory(self):
        page=self.pages['回复试验室']; self.heading(page,'先聊一聊，检查像不像你','这里不会操作微信。风格、记忆和相关片段交给选定的模型：支持 MiMo 或本地 Ollama。')
        self.lab_combo=ttk.Combobox(page,textvariable=self.lab_contact,state='readonly'); self.lab_combo.pack(fill='x')
        ttk.Label(page,text='对方说了什么').pack(anchor='w',pady=(15,4))
        self.lab_input=tk.Text(page,height=4,font=('Microsoft YaHei UI',11),relief='flat',wrap='word',padx=12,pady=10); self.lab_input.pack(fill='x')
        row=ttk.Frame(page); row.pack(fill='x',pady=12)
        self.lab_button=ttk.Button(row,text='生成我的风格回复',style='Accent.TButton',command=self.lab_reply); self.lab_button.pack(side='left')
        ttk.Button(row,text='查看本次使用的提示与记忆',command=self.inspect_prompt).pack(side='left',padx=8)
        self.lab_output=self.text(page,12)

    def reply_cfg(self,cfg,contact,query):
        copy=dict(cfg)
        if cfg.get('persona_enabled',True):copy['prompt']=self.store.prompt(contact,query,cfg['prompt'],cfg.get('longer',True),cfg.get('proactive',True))
        else:copy['prompt']=cfg['prompt']+('\n适当充分回应，可以自然接话追问。' if cfg.get('proactive',True) else '')
        return copy

    def inspect_prompt(self):
        cfg=self.reply_cfg(self.app.snapshot(),self.lab_contact.get(),self.lab_input.get('1.0','end').strip())
        dialog=tk.Toplevel(self.root); dialog.title('本次提交给模型的提示预览'); dialog.geometry('800x650')
        self.set_text(self.text(dialog,22),cfg['prompt'])

    def lab_reply(self):
        from app import completion,validate_connection
        query=self.lab_input.get('1.0','end').strip(); contact=self.lab_contact.get()
        if not query:return
        cfg=self.reply_cfg(self.app.snapshot(),contact,query)
        try:key,_=validate_connection(cfg,self.app.key.get())
        except ValueError as exc:return messagebox.showerror('模型配置',str(exc))
        self.lab_button.configure(state='disabled'); self.set_text(self.lab_output,'正在生成，不会发送到微信…')
        def work():
            try:return completion(cfg,key,query,self.store.recent(contact))
            except Exception as exc:return '生成失败：'+str(exc)
        def done(answer):self.set_text(self.lab_output,answer); self.lab_button.configure(state='normal')
        self.run(work,done)

    def automation(self):
        app=self.app; page=self.pages['微信自动回复']; self.heading(page,'微信自动回复','锁定一个窗口和当前聊天。先检查 OCR，再开启自动发送；这里会使用“我的分身”设置。')
        row=ttk.Frame(page); row.pack(fill='x')
        app.window_choice=ttk.Combobox(row,state='readonly'); app.window_choice.pack(side='left',fill='x',expand=True); app.window_choice.bind('<<ComboboxSelected>>',app.choose_window)
        ttk.Button(row,text='刷新窗口',command=app.refresh_windows).pack(side='left',padx=5)
        ttk.Button(row,text='窗口预览 / 发送位置',command=app.window_preview).pack(side='left')
        row=ttk.Frame(page); row.pack(fill='x',pady=12)
        ttk.Label(row,text='只回复此聊天（可留空）').pack(side='left')
        app.vars['title']=tk.StringVar(value=app.cfg.get('title','')); ttk.Entry(row,textvariable=app.vars['title']).pack(side='left',fill='x',expand=True,padx=10)
        app.auto=tk.BooleanVar(value=False); ttk.Checkbutton(page,text='自动发送（关闭时只生成预览）',variable=app.auto).pack(anchor='w',pady=5)
        row=ttk.Frame(page); row.pack(fill='x',pady=12)
        app.start_button=ttk.Button(row,text='开始监听',style='Accent.TButton',command=app.start); app.start_button.pack(side='left')
        for text,callback in [('停止 / Esc',app.halt),('仅测试识别',app.test_recognition),('发送当前预览',app.send_pending)]:ttk.Button(row,text=text,command=callback).pack(side='left',padx=5)
        app.output=self.text(page,16); app.output.configure(state='disabled')
        app.status=tk.StringVar(value='未启动'); ttk.Label(page,textvariable=app.status,style='Hint.TLabel',wraplength=850).pack(anchor='w',pady=8)

    def settings(self):
        app=self.app; outer=self.pages['模型与工具']
        canvas=tk.Canvas(outer,bg='#f4f7fb',highlightthickness=0)
        scrollbar=ttk.Scrollbar(outer,command=canvas.yview); canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right',fill='y'); canvas.pack(side='left',fill='both',expand=True)
        page=ttk.Frame(canvas); window=canvas.create_window(0,0,anchor='nw',window=page)
        page.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
        def wheel(event):
            if getattr(self,'current_page','')=='模型与工具' and not isinstance(event.widget,tk.Text):canvas.yview_scroll(-int(event.delta/120),'units')
        self.root.bind_all('<MouseWheel>',wheel,add='+')
        self.heading(page,'模型与工具','MiMo 云端 / Ollama、LM Studio 本地模型，复用同一份聊天风格和联系人记忆。')
        ttk.Label(page,text='模型服务').pack(anchor='w',pady=(4,4))
        app.vars['provider']=tk.StringVar(value=app.cfg.get('provider','mimo'))
        self.provider_choice=tk.StringVar(value={'ollama':'本机 Ollama','lmstudio':'本机 LM Studio'}.get(app.vars['provider'].get(),'小米 MiMo'))
        provider=ttk.Combobox(page,textvariable=self.provider_choice,values=['小米 MiMo','本机 Ollama','本机 LM Studio'],state='readonly'); provider.pack(fill='x'); provider.bind('<<ComboboxSelected>>',self.change_provider)
        for name,label in [('base_url','服务 Base URL'),('model','模型 ID（可读取本地服务列表）')]:
            ttk.Label(page,text=label).pack(anchor='w',pady=(8,4)); app.vars[name]=tk.StringVar(value=app.cfg[name])
            if name=='model':self.model_choice=ttk.Combobox(page,textvariable=app.vars[name]); self.model_choice.pack(fill='x')
            else:ttk.Entry(page,textvariable=app.vars[name]).pack(fill='x')
        ttk.Button(page,text='读取本地模型列表',command=self.load_ollama_models).pack(anchor='w',pady=5)
        ttk.Label(page,text='API Key（仅本次运行保存，不写入磁盘）').pack(anchor='w',pady=(12,4))
        app.key=tk.StringVar(value=os.environ.get('MIMO_API_KEY','')); self.key_entry=ttk.Entry(page,textvariable=app.key,show='•'); self.key_entry.pack(fill='x')
        if app.vars['provider'].get() in ('ollama','lmstudio'):self.key_entry.configure(state='disabled')
        row=ttk.Frame(page); row.pack(fill='x',pady=12)
        for text,url in [('按量付费','https://api.xiaomimimo.com/v1'),('Token Plan 中国','https://token-plan-cn.xiaomimimo.com/v1')]:ttk.Button(row,text=text,command=lambda u=url:self.cloud_preset(u)).pack(side='left',padx=(0,6))
        ttk.Button(row,text='测试模型连接',command=app.test_connection).pack(side='left')
        ttk.Label(page,text='补充回复要求（风格学习启用后，会叠加在分身提示中）').pack(anchor='w',pady=(8,4))
        app.prompt=tk.Text(page,height=4,font=('Microsoft YaHei UI',10),wrap='word',relief='flat',padx=12,pady=10); app.prompt.insert('1.0',app.cfg['prompt']); app.prompt.pack(fill='x')
        ttk.Button(page,text='保存模型配置',style='Accent.TButton',command=app.save).pack(anchor='w',pady=10)
        ttk.Separator(page).pack(fill='x',pady=12)
        ttk.Label(page,text='WeTrace 便携组件').pack(anchor='w'); ttk.Entry(page,textvariable=self.backend).pack(fill='x',pady=5)
        row=ttk.Frame(page); row.pack(fill='x',pady=6)
        ttk.Button(row,text='启动并打开 WeTrace',command=self.start_wetrace).pack(side='left')
        ttk.Button(row,text='选择已有 WeTrace.exe',command=self.choose_backend).pack(side='left',padx=6)
        ttk.Label(page,text='WeTrace 导出中心在独立工具窗口打开；导出后回到“聊天记录”导入。WeTrace 组件沿用原作者非商业许可证。',style='Hint.TLabel',wraplength=850).pack(anchor='w',pady=8)
        ttk.Label(page,text='本地记录库：'+str(self.store.path),style='Hint.TLabel',wraplength=850).pack(anchor='w',pady=8)

    def change_provider(self,event=None):
        provider={'本机 Ollama':'ollama','本机 LM Studio':'lmstudio'}.get(self.provider_choice.get(),'mimo')
        local=provider!='mimo'
        self.app.vars['provider'].set(provider)
        self.app.vars['base_url'].set({'ollama':'http://127.0.0.1:11434/v1','lmstudio':'http://127.0.0.1:1234/v1','mimo':'https://api.xiaomimimo.com/v1'}[provider])
        self.app.vars['model'].set('' if local else 'mimo-v2.6-pro')
        self.key_entry.configure(state='disabled' if local else 'normal')
        self.model_choice['values']=[]
        self.app.status.set('本地模式不使用 MiMo Key；请先启动模型服务，再读取模型列表' if local else '已切换到 MiMo，请检查 Key')

    def cloud_preset(self,url):
        self.provider_choice.set('小米 MiMo'); self.change_provider(); self.app.vars['base_url'].set(url)

    def load_ollama_models(self):
        provider=self.app.vars['provider'].get()
        if provider not in ('ollama','lmstudio'):return messagebox.showinfo('本地模型','请先选择本机 Ollama 或 LM Studio')
        base=self.app.vars['base_url'].get().strip()
        from urllib.parse import urlsplit,urlunsplit
        parsed=urlsplit(base)
        if parsed.scheme not in ('http','https') or not parsed.hostname:return messagebox.showerror('服务地址','请填写本地服务地址：Ollama 11434 / LM Studio 1234')
        url=urlunsplit((parsed.scheme,parsed.netloc,'/v1/models' if provider=='lmstudio' else '/api/tags','',''))
        def work():
            try:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url,timeout=5) as response:data=json.load(response)
            except Exception:raise ValueError('无法读取本地模型，请启动模型服务。Ollama 默认11434；LM Studio 默认1234，需开启 Developer 中的服务。') from None
            if provider=='lmstudio':return [row['id'] for row in data.get('data',[]) if row.get('id')]
            return [row['name'] for row in data.get('models',[]) if row.get('name') and not row.get('remote_host') and not row['name'].endswith((':cloud','-cloud'))]
        def done(models):
            self.model_choice['values']=models
            if models:
                if self.app.vars['model'].get() not in models:self.app.vars['model'].set(models[0])
                self.app.status.set(f'已读取 {len(models)} 个本地模型，请选择后测试连接')
            else:self.app.status.set('未找到模型，请先在本地服务中下载并加载模型；不自动下载')
        self.run(work,done)

    def choose_backend(self):
        path=filedialog.askopenfilename(title='选择 WeTrace.exe',filetypes=[('程序','*.exe')])
        if path:self.backend.set(path)

    def start_wetrace(self):
        path=Path(self.backend.get())
        if not path.is_file():return messagebox.showerror('缺少组件','请在模型与工具页选择已有WeTrace.exe，或使用完整便携ZIP')
        def ready():
            try:
                with urllib.request.urlopen('http://127.0.0.1:5200/',timeout=1) as response:return response.status==200
            except Exception:return False
        def work():
            if not ready():
                if self.tool_process and self.tool_process.poll() is None:return 'WeTrace 正在启动，请稍后再点一次'
                logs=self.root_path/'data'/'logs'; logs.mkdir(parents=True,exist_ok=True)
                self.tool_log=open(logs/'wetrace.log','ab')
                self.tool_process=subprocess.Popen([str(path)],cwd=str(path.parent),stdout=self.tool_log,stderr=self.tool_log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                import time
                for _ in range(30):
                    if ready():break
                    if self.tool_process.poll() is not None:raise ValueError('WeTrace启动失败，请查看data/logs/wetrace.log；纯净电脑可能需要安装组件目录中的VC运行库')
                    time.sleep(.3)
                else:raise ValueError('WeTrace尚未就绪，请查看data/logs/wetrace.log')
            edge_paths=[Path(os.environ.get(key,''))/'Microsoft'/'Edge'/'Application'/'msedge.exe' for key in ('ProgramFiles(x86)','ProgramFiles')]
            edge=next((candidate for candidate in edge_paths if candidate.is_file()),None)
            if edge:
                profile=self.root_path/'data'/'webview'
                subprocess.Popen([str(edge),'--app=http://127.0.0.1:5200/','--user-data-dir='+str(profile),'--no-first-run'])
            else:webbrowser.open('http://127.0.0.1:5200/')
            return 'WeTrace 导出中心已打开，完成导出后回到本 App 导入'
        self.app.status.set('正在启动 WeTrace…'); self.run(work,lambda message:self.app.status.set(message))
