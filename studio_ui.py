"""Independent desktop workspace for records, AI analysis, persona memory and OCR replies."""
import json
import os
import sys
from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
import urllib.request
from chatdata import ChatStore,read_rows,normalize_rows,ALIASES

class StudioUI:
    def __init__(self,app,root_path):
        self.app=app; self.root=app.root; self.root_path=Path(root_path)
        self.store=ChatStore(self.root_path/'data'/'persona.sqlite3')
        self.pages={}; self.nav={}; self.record_offset=0; self.jobs=0
        self.learn=tk.BooleanVar(value=self.store.get('learn',True))
        self.longer=tk.BooleanVar(value=self.store.get('longer',True))
        self.proactive=tk.BooleanVar(value=self.store.get('proactive',True))
        self.binding=tk.StringVar(value=self.store.get('binding','自动按聊天标题匹配'))
        self.lab_contact=tk.StringVar(); self.memory_contact=tk.StringVar()
        self._style()
        self.root.title('微信分身 Studio · 1.0.1')
        self.root.geometry('1180x850'); self.root.minsize(1040,760)
        shell=ttk.Frame(self.root); shell.pack(fill='both',expand=True)
        rail=tk.Frame(shell,bg='#152538',width=205); rail.pack(side='left',fill='y'); rail.pack_propagate(False)
        tk.Label(rail,text='微信分身',font=('Microsoft YaHei UI',22,'bold'),fg='white',bg='#152538').pack(anchor='w',padx=22,pady=(32,0))
        tk.Label(rail,text='STUDIO  /  本地记忆工作台',font=('Microsoft YaHei UI',9),fg='#9bb0c7',bg='#152538').pack(anchor='w',padx=23,pady=(6,30))
        body=ttk.Frame(shell,padding=(30,24)); body.pack(side='left',fill='both',expand=True)
        for name in ('总览','聊天记录','我的分身','联系人记忆','AI 工具箱','聊天统计','回复试验室','微信自动回复','模型与工具'):
            page=ttk.Frame(body); page.place(relx=0,rely=0,relwidth=1,relheight=1)
            self.pages[name]=page
            button=tk.Button(rail,text=name,anchor='w',relief='flat',bd=0,bg='#152538',fg='#c8d5e5',activebackground='#253d55',activeforeground='white',font=('Microsoft YaHei UI',12),padx=24,pady=9,command=lambda n=name:self.show(n))
            button.pack(fill='x',padx=10,pady=2); self.nav[name]=button
        tk.Label(rail,text='记录保存在本机\nMiMo / Ollama / LM Studio',justify='left',font=('Microsoft YaHei UI',9),fg='#9bb0c7',bg='#152538',wraplength=170).pack(side='bottom',padx=20,pady=30)
        self.overview(); self.corpus(); self.persona(); self.memories(); self.laboratory(); self.analysis_page(); self.statistics_page(); self.automation(); self.settings()
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
        self.jobs+=1
        def thread():
            try:
                value=work()
                def finish(v=value):
                    try:
                        if done:done(v)
                    finally:self.jobs-=1
                self.app.events.put(('ui',finish))
            except Exception as exc:
                def fail(message=str(exc)):
                    self.jobs-=1
                    self.app.status.set('操作未完成，请检查提示'); messagebox.showerror('操作未完成',message)
                self.app.events.put(('ui',fail))
        threading.Thread(target=thread,daemon=True).start()

    def overview(self):
        page=self.pages['总览']; self.heading(page,'让回复，更像你','记录管理、AI 分析、个人分身与窗口回复，一个 App 完成。')
        row=ttk.Frame(page); row.pack(fill='x',pady=10); self.metrics={}
        for name,label in [('total','已导入消息'),('self','本人消息'),('contacts','联系人'),('unknown','待确认身份')]:
            card=tk.Frame(row,bg='white',padx=20,pady=20); card.pack(side='left',fill='both',expand=True,padx=(0,12))
            value=tk.StringVar(value='0'); self.metrics[name]=value
            tk.Label(card,textvariable=value,font=('Microsoft YaHei UI',27,'bold'),fg='#176bde',bg='white').pack(anchor='w')
            tk.Label(card,text=label,font=('Microsoft YaHei UI',10),fg='#657a94',bg='white').pack(anchor='w',pady=(8,0))
        for title,description,target in [
            ('01  导入你的历史','导入 JSON / CSV 或 WeTrace HTML 备份；先预览，再入库。','聊天记录'),
            ('02  建立你的分身','只用本人消息学习语气，给每位联系人保留独立记忆。','我的分身'),
            ('03  回顾最近的聊天','按当前时间筛选，生成摘要、待办和关键信息。','AI 工具箱'),
            ('04  接入当前聊天','锁定微信窗口，OCR 识别，默认预览后发送。','微信自动回复')]:
            card=ttk.LabelFrame(page,text=title,padding=16); card.pack(fill='x',pady=8)
            ttk.Label(card,text=description).pack(side='left'); ttk.Button(card,text='打开 →',command=lambda t=target:self.show(t)).pack(side='right')
        self.overview_note=ttk.Label(page,text='',style='Hint.TLabel',wraplength=850); self.overview_note.pack(anchor='w',pady=16)

    def corpus(self):
        page=self.pages['聊天记录']; self.heading(page,'聊天记录','导入 JSON / CSV / HTML / TXT；支持 WeTrace 导出。搜索、浏览和再导出都在本 App 完成。')
        row=ttk.Frame(page); row.pack(fill='x')
        ttk.Button(row,text='＋ 导入文件',style='Accent.TButton',command=self.import_files).pack(side='left',padx=(0,8))
        ttk.Button(row,text='导入 HTML 备份文件夹',command=self.import_folder).pack(side='left',padx=4)
        ttk.Button(row,text='导出完整会话',command=self.export_contact).pack(side='right')
        row=ttk.Frame(page); row.pack(fill='x',pady=(8,0))
        ttk.Button(row,text='完整备份（记录＋记忆＋设置）',command=self.backup_all).pack(side='left')
        ttk.Button(row,text='恢复完整备份',command=self.restore_all).pack(side='left',padx=8)
        row=ttk.Frame(page); row.pack(fill='x',pady=14)
        self.contact_choice=ttk.Combobox(row,state='readonly'); self.contact_choice.pack(side='left',fill='x',expand=True)
        self.contact_choice.bind('<<ComboboxSelected>>',lambda e:self.reset_records())
        ttk.Button(row,text='刷新',command=self.refresh).pack(side='left',padx=6)
        row=ttk.Frame(page); row.pack(fill='x',pady=(0,10))
        self.search_var=tk.StringVar()
        search=ttk.Entry(row,textvariable=self.search_var); search.pack(side='left',fill='x',expand=True); search.bind('<Return>',lambda e:self.reset_records())
        ttk.Button(row,text='搜索',command=self.reset_records).pack(side='left',padx=5)
        ttk.Button(row,text='全部会话',command=self.all_contacts).pack(side='left')
        self.record_count=tk.StringVar(); ttk.Label(row,textvariable=self.record_count).pack(side='left',padx=12)
        row=ttk.Frame(page); row.pack(fill='x',pady=(0,8))
        self.prev_records=ttk.Button(row,text='上一页',command=lambda:self.page_records(-1)); self.prev_records.pack(side='left')
        self.next_records=ttk.Button(row,text='下一页',command=lambda:self.page_records(1)); self.next_records.pack(side='left',padx=6)
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
        dialog=tk.Toplevel(self.root); dialog.title('导入预览 · 确认本人身份'); dialog.geometry('1180x650'); dialog.transient(self.root); dialog.grab_set()
        frame=ttk.Frame(dialog,padding=20); frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='先确认谁是你，再开始学习',font=('Microsoft YaHei UI',17,'bold')).pack(anchor='w')
        ttk.Label(frame,text='有 isSelf / IsSender 标记时自动识别。没有标记时，填写你在导出记录中的准确称呼，以分号分隔。',wraplength=950).pack(anchor='w',pady=8)
        aliases=tk.StringVar(value=self.store.get('self_names',''))
        row=ttk.Frame(frame); row.pack(fill='x',pady=6); ttk.Label(row,text='我的称呼').pack(side='left'); ttk.Entry(row,textvariable=aliases).pack(side='left',fill='x',expand=True,padx=10)
        mapping={}; keys=sorted({str(k) for row in loaded[0][1][:100] for k in row})
        row=ttk.Frame(frame); row.pack(fill='x',pady=5)
        for key,label in [('text','消息列'),('sender','发送者列'),('self','本人标记列'),('contact','联系人列'),('time','时间列')]:
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
        for combo in (self.contact_choice,self.memory_combo,self.lab_combo,self.analysis_contact,self.stats_contact):combo['values']=contacts
        self.binding_combo['values']=['自动按聊天标题匹配']+contacts
        if contacts and not self.contact_choice.get():self.contact_choice.set(contacts[0])
        if contacts and not self.memory_contact.get():self.memory_contact.set(contacts[0])
        if contacts and not self.lab_contact.get():self.lab_contact.set(contacts[0])
        for combo in (self.analysis_contact,self.stats_contact):
            if contacts and not combo.get():combo.set(contacts[0])
        style=self.store.style(); self.set_text(self.style_view,style['summary']+'\n\n本人示例（仅供学习语气）：\n\n'+'\n\n'.join(style['examples']))
        self.overview_note.configure(text=style['summary'])
        self.show_messages(); self.load_memories()

    def all_contacts(self):
        self.contact_choice.set(''); self.reset_records()

    def reset_records(self):
        self.record_offset=0; self.show_messages()

    def page_records(self,direction):
        self.record_offset=max(0,self.record_offset+direction*200); self.show_messages()

    def show_messages(self):
        from records import query
        self.visible_rows,total=query(self.store,self.contact_choice.get(),self.search_var.get().strip(),self.record_offset)
        self.record_count.set(f'{total} 条 · 第 {self.record_offset//200+1} 页（每页200条）')
        self.prev_records.configure(state='normal' if self.record_offset else 'disabled')
        self.next_records.configure(state='normal' if self.record_offset+200<total else 'disabled')
        self.message_table.delete(*self.message_table.get_children())
        for index,row in enumerate(self.visible_rows):self.message_table.insert('', 'end',iid=str(index),values=(row['ts'],row['sender'],{'self':'本人','other':'对方','unknown':'未知'}[row['role']],row['content'][:200]))

    def message_detail(self,event=None):
        selected=self.message_table.selection()
        if selected:messagebox.showinfo('消息详情',self.visible_rows[int(selected[0])]['content'])

    def export_contact(self):
        from records import all_rows,export_file
        contact=self.contact_choice.get()
        path=filedialog.asksaveasfilename(defaultextension='.json',filetypes=[('JSON','*.json'),('CSV','*.csv'),('HTML','*.html')],title='导出完整会话（不受搜索和分页限制）')
        if not path:return
        self.run(lambda:export_file(path,all_rows(self.store,contact)),lambda count:self.app.status.set(f'已导出 {count} 条消息到所选位置'))

    def backup_all(self):
        from backup import create_backup
        path=filedialog.asksaveasfilename(title='完整备份：消息、记忆、风格设置、近期上下文和模型配置',defaultextension='.zip',filetypes=[('Studio完整备份','*.zip')])
        if not path:return
        config=self.app.snapshot()
        self.run(lambda:create_backup(self.store,config,path),lambda counts:messagebox.showinfo('备份完成',f"消息 {counts['messages']} 条、联系人记忆 {counts['memories']} 条、近期对话 {counts['exchanges']} 组。\n已包含风格设置和模型配置，不保存API Key。\n这是个人数据备份，请自行保管。"))

    def restore_all(self):
        from backup import restore_backup
        if self.app.running or self.jobs or getattr(self.app,'testing_connection',False):
            return messagebox.showinfo('请先停止任务','请停止微信监听，并等待导入、生成或模型测试完成后再恢复。')
        path=filedialog.askopenfilename(title='选择Studio完整备份ZIP（不是聊天导出文件）',filetypes=[('Studio完整备份','*.zip')])
        if not path:return
        if not messagebox.askyesno('恢复完整备份','恢复会替换当前记录、记忆和设置。程序会先校验备份，再自动保存一份当前数据。继续恢复吗？'):return
        config=self.app.snapshot()
        busy=tk.Toplevel(self.root); busy.title('恢复完整备份'); busy.transient(self.root); busy.grab_set(); busy.protocol('WM_DELETE_WINDOW',lambda:None)
        ttk.Label(busy,text='正在校验并恢复，请稍候…',padding=30).pack()
        def work():
            try:return True,restore_backup(self.store,config,path,self.root_path/'config.json')
            except Exception as exc:return False,str(exc)
        def done(result):
            busy.destroy()
            if not result[0]:return messagebox.showerror('未能恢复',result[1])
            from app import DEFAULT
            restored,counts,safety=result[1]
            self.app.cfg=dict(DEFAULT,**restored)
            for name,var in self.app.vars.items():var.set(str(self.app.cfg.get(name,'')))
            self.provider_choice.set({'ollama':'本机 Ollama','lmstudio':'本机 LM Studio'}.get(restored.get('provider'),'小米 MiMo'))
            self.key_entry.configure(state='disabled' if restored.get('provider') in ('ollama','lmstudio') else 'normal')
            self.app.key.set(''); self.app.auto.set(False)
            self.app.prompt.delete('1.0','end'); self.app.prompt.insert('1.0',self.app.cfg['prompt'])
            for key,var,default in [('learn',self.learn,True),('longer',self.longer,True),('proactive',self.proactive,True),('binding',self.binding,'自动按聊天标题匹配')]:var.set(self.store.get(key,default))
            self.contact_choice.set(''); self.lab_contact.set(''); self.memory_contact.set(''); self.analysis_contact.set(''); self.stats_contact.set('')
            self.record_offset=0; self.search_var.set(''); self.refresh()
            messagebox.showinfo('恢复完成',f"已恢复 {counts['messages']} 条消息和 {counts['memories']} 条记忆。\n恢复前备份：{safety}\nAPI Key需要重新填写，自动发送保持关闭。")
        self.run(work,done)

    def persona(self):
        page=self.pages['我的分身']; self.heading(page,'我的分身','从已确认的本人消息中提取风格；这是风格示例与记忆增强，不是修改模型参数。')
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
        page=self.pages['回复试验室']; self.heading(page,'先聊一聊，检查像不像你','这里不会操作微信。风格、记忆和相关片段交给选定的模型：支持 MiMo、LM Studio 或 Ollama。')
        self.lab_combo=ttk.Combobox(page,textvariable=self.lab_contact,state='readonly'); self.lab_combo.pack(fill='x')
        ttk.Label(page,text='对方说了什么').pack(anchor='w',pady=(15,4))
        self.lab_input=tk.Text(page,height=4,font=('Microsoft YaHei UI',11),relief='flat',wrap='word',padx=12,pady=10); self.lab_input.pack(fill='x')
        row=ttk.Frame(page); row.pack(fill='x',pady=12)
        self.lab_button=ttk.Button(row,text='生成我的风格回复',style='Accent.TButton',command=self.lab_reply); self.lab_button.pack(side='left')
        ttk.Button(row,text='查看本次使用的提示与记忆',command=self.inspect_prompt).pack(side='left',padx=8)
        self.lab_progress=tk.StringVar(value='本地模型会根据上下文预算选择参考资料和最近对话。')
        ttk.Label(page,textvariable=self.lab_progress,style='Hint.TLabel',wraplength=850).pack(anchor='w')
        self.lab_output=self.text(page,12)

    def analysis_page(self):
        from toolbox import RANGES,TASKS
        page=self.pages['AI 工具箱']
        self.heading(page,'AI 工具箱','分析已导入的记录。最近一周按现在向前7天计算；没有近期消息不会使用旧记录替代。')
        self.analysis_contact=ttk.Combobox(page,state='readonly'); self.analysis_contact.pack(fill='x')
        row=ttk.Frame(page); row.pack(fill='x',pady=12)
        self.analysis_range=tk.StringVar(value=next(iter(RANGES)))
        self.range_combo=ttk.Combobox(row,textvariable=self.analysis_range,values=list(RANGES)+['自定义日期'],state='readonly',width=22); self.range_combo.pack(side='left')
        self.date_start=tk.StringVar(); self.date_end=tk.StringVar()
        ttk.Label(row,text='从').pack(side='left',padx=(10,3))
        self.start_date_entry=ttk.Entry(row,textvariable=self.date_start,width=12); self.start_date_entry.pack(side='left')
        ttk.Label(row,text='至').pack(side='left',padx=3)
        self.end_date_entry=ttk.Entry(row,textvariable=self.date_end,width=12); self.end_date_entry.pack(side='left')
        self.start_date_entry.configure(state='disabled'); self.end_date_entry.configure(state='disabled')
        def change_range(event=None):
            state='normal' if self.analysis_range.get()=='自定义日期' else 'disabled'
            self.start_date_entry.configure(state=state); self.end_date_entry.configure(state=state)
            self.invalidate_analysis()
        self.range_combo.bind('<<ComboboxSelected>>',change_range)
        self.analysis_contact.bind('<<ComboboxSelected>>',lambda e:self.invalidate_analysis())
        self.date_start.trace_add('write',lambda *args:self.invalidate_analysis())
        self.date_end.trace_add('write',lambda *args:self.invalidate_analysis())
        ttk.Label(page,text='自定义格式 YYYY-MM-DD，包含结束当天；统一使用北京时间 UTC+8。',style='Hint.TLabel').pack(anchor='w')
        row=ttk.Frame(page); row.pack(fill='x',pady=12)
        self.analysis_task=ttk.Combobox(row,values=list(TASKS),state='readonly',width=16); self.analysis_task.set('对话摘要'); self.analysis_task.pack(side='left')
        ttk.Button(row,text='检查范围 / 预览消息',command=self.preview_analysis).pack(side='left',padx=8)
        self.analysis_button=ttk.Button(row,text='开始分析',style='Accent.TButton',command=self.generate_analysis); self.analysis_button.pack(side='left')
        self.analysis_cancel=threading.Event()
        ttk.Button(row,text='停止',command=self.stop_analysis).pack(side='left',padx=6)
        ttk.Button(row,text='导出结果',command=self.export_analysis).pack(side='right')
        self.analysis_meta=tk.StringVar(value='先选择会话和范围，再预览或分析。')
        ttk.Label(page,textvariable=self.analysis_meta,style='Hint.TLabel',wraplength=850).pack(anchor='w',pady=6)
        self.analysis_output=self.text(page,16)
        self.set_text(self.analysis_output,'支持 MiMo / LM Studio / Ollama。分析使用“模型与工具”中的服务配置。\n\n只提交所选日期范围内的消息；较长记录会分段分析后合并，进度会显示在这里。')

    def invalidate_analysis(self):
        if hasattr(self,'analysis_meta'):self.analysis_meta.set('筛选条件已改变，请重新检查范围。')

    def analysis_selection(self):
        from toolbox import select_messages
        # Capture all Tk values on the UI thread, before entering the worker.
        contact=self.analysis_contact.get(); choice=self.analysis_range.get()
        first=self.date_start.get(); last=self.date_end.get()
        return lambda:select_messages(self.store,contact,choice,first,last)

    def preview_analysis(self):
        from toolbox import describe,transcript
        work=self.analysis_selection()
        def done(selection):
            self.analysis_meta.set(describe(selection))
            dialog=tk.Toplevel(self.root); dialog.title('所选消息预览 · 未调用模型'); dialog.geometry('900x650')
            self.set_text(self.text(dialog,25),describe(selection)+'\n\n'+(transcript(selection) or '该时间范围无消息；不会回退到旧记录。'))
        self.run(work,done)

    def generate_analysis(self):
        from app import completion,validate_connection
        from toolbox import analyze,describe
        select=self.analysis_selection(); cfg=self.app.snapshot(); task=self.analysis_task.get()
        try:key,_=validate_connection(cfg,self.app.key.get())
        except ValueError as exc:return messagebox.showerror('模型配置',str(exc))
        self.analysis_cancel.clear()
        self.analysis_button.configure(state='disabled')
        self.set_text(self.analysis_output,'正在检查日期范围…')
        def progress(text):self.app.events.put(('ui',lambda value=text:self.set_text(self.analysis_output,value)))
        def work():
            try:
                selection=select()
                self.app.events.put(('ui',lambda value=describe(selection):self.analysis_meta.set(value)))
                return analyze(selection,task,cfg,key,completion,progress,self.analysis_cancel.is_set)
            except Exception as exc:return '分析未完成：'+str(exc)
        def done(result):
            self.set_text(self.analysis_output,result); self.analysis_button.configure(state='normal')
        self.run(work,done)

    def stop_analysis(self):
        self.analysis_cancel.set()
        self.app.status.set('已请求停止分析；当前模型请求结束后，不再提交后续片段。')

    def export_analysis(self):
        path=filedialog.asksaveasfilename(title='保存分析结果',defaultextension='.txt',filetypes=[('文本','*.txt'),('Markdown','*.md')])
        if path:
            try:Path(path).write_text(self.analysis_output.get('1.0','end').strip(),'utf-8')
            except OSError as exc:return messagebox.showerror('无法保存',str(exc))
            self.app.status.set('分析结果已保存')

    def statistics_page(self):
        page=self.pages['聊天统计']; self.heading(page,'聊天统计','本地计算文本消息数量和活跃时段，不调用模型；无法识别的日期单独列出。')
        row=ttk.Frame(page); row.pack(fill='x')
        self.stats_contact=ttk.Combobox(row,state='readonly'); self.stats_contact.pack(side='left',fill='x',expand=True)
        self.stats_contact.bind('<<ComboboxSelected>>',lambda e:self.load_statistics())
        ttk.Button(row,text='统计所选会话',command=self.load_statistics).pack(side='left',padx=6)
        ttk.Button(row,text='统计全部',command=lambda:self.load_statistics(True)).pack(side='left')
        self.stats_summary=tk.StringVar(value='选择会话后点击统计。')
        ttk.Label(page,textvariable=self.stats_summary,wraplength=850,style='Hint.TLabel').pack(anchor='w',pady=15)
        self.stats_chart=tk.Canvas(page,height=200,bg='white',highlightthickness=0); self.stats_chart.pack(fill='x')
        self.stats_text=self.text(page,9)

    def load_statistics(self,all_contacts=False):
        from records import statistics
        contact='' if all_contacts else self.stats_contact.get()
        def done(stats):
            self.stats_summary.set(f"{'全部会话' if not contact else contact} · {stats['total']} 条文本 · 本人 {stats['own']} 条 · 身份未知 {stats['unknown']} 条\n日期：{stats['first'] or '未知'} 至 {stats['last'] or '未知'}；日期无效 {stats['invalid']} 条")
            chart=self.stats_chart; chart.delete('all'); width=max(600,chart.winfo_width()); peak=max(stats['hours'].values(),default=1)
            for hour in range(24):
                count=stats['hours'][hour]; x=20+hour*(width-40)/24; bar=(width-40)/24-4; height=135*count/peak
                chart.create_rectangle(x,165-height,x+bar,165,fill='#176bde',outline='')
                chart.create_text(x+bar/2,181,text=str(hour),fill='#687b91')
                if count:chart.create_text(x+bar/2,155-height,text=str(count),font=('Microsoft YaHei UI',8))
            self.set_text(self.stats_text,'北京时间 0–23 时消息分布\n\n最活跃日期：\n'+'\n'.join(f'{day}：{count} 条' for day,count in stats['days'].most_common(10))+'\n\n发送者：\n'+'\n'.join(f'{sender}：{count} 条' for sender,count in stats['senders'].most_common(10)))
        self.run(lambda:statistics(self.store,contact),done)

    def reply_cfg(self,cfg,contact,query):
        copy=dict(cfg)
        budget=None
        if cfg.get('provider') in ('ollama','lmstudio'):
            from app import local_budget
            budget=min(1800,local_budget(cfg)-len(query.encode('utf-8'))-48)
            if budget<600:raise ValueError('当前消息过长，请缩短或提高本地上下文长度。')
        if cfg.get('persona_enabled',True):copy['prompt']=self.store.prompt(contact,query,cfg['prompt'],cfg.get('longer',True),cfg.get('proactive',True),max_bytes=budget)
        else:copy['prompt']=cfg['prompt']+('\n适当充分回应，可以自然接话追问。' if cfg.get('proactive',True) else '')
        return copy

    def inspect_prompt(self):
        from app import prepare_messages
        try:
            query=self.lab_input.get('1.0','end').strip(); contact=self.lab_contact.get()
            cfg=self.reply_cfg(self.app.snapshot(),contact,query)
            actual=prepare_messages(cfg,query,self.store.recent(contact))
        except ValueError as exc:return messagebox.showerror('提示预览',str(exc))
        dialog=tk.Toplevel(self.root); dialog.title('本次提交给模型的提示预览'); dialog.geometry('800x650')
        self.set_text(self.text(dialog,22),json.dumps(actual,ensure_ascii=False,indent=2))

    def lab_reply(self):
        from app import completion,validate_connection
        query=self.lab_input.get('1.0','end').strip(); contact=self.lab_contact.get()
        if not query:return
        try:
            cfg=self.reply_cfg(self.app.snapshot(),contact,query)
            key,_=validate_connection(cfg,self.app.key.get())
        except ValueError as exc:return messagebox.showerror('模型配置',str(exc))
        self.lab_button.configure(state='disabled'); self.set_text(self.lab_output,'正在生成，不会发送到微信…')
        import time
        started=time.monotonic(); self.lab_started=started
        def waiting():
            if self.lab_started==started and str(self.lab_button['state'])=='disabled':
                self.lab_progress.set(f'等待模型 {int(time.monotonic()-started)} 秒；本地首次加载可能较慢。')
                self.root.after(1000,waiting)
        waiting()
        def work():
            try:return completion(cfg,key,query,self.store.recent(contact))
            except Exception as exc:return '生成失败：'+str(exc)
        def done(answer):
            self.lab_started=None
            self.set_text(self.lab_output,answer); self.lab_button.configure(state='normal'); self.lab_progress.set('生成结束；失败原因会显示在下方。' if answer.startswith('生成失败：') else '已生成，未发送微信。')
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
        ttk.Label(page,text='本地上下文长度（与模型服务加载设置一致；默认4096，保守控制输入）').pack(anchor='w',pady=(8,4))
        app.vars['context_size']=tk.StringVar(value=str(app.cfg.get('context_size',4096)))
        ttk.Combobox(page,textvariable=app.vars['context_size'],values=['2048','4096','8192','16384','32768'],state='readonly').pack(fill='x')
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
        ttk.Label(page,text='独立便携版 · 记录导入、查看、导出和分析均由本 App 完成。无需 WeTrace 运行组件。',style='Hint.TLabel',wraplength=850).pack(anchor='w',pady=8)
        ttk.Button(page,text='开源许可证',command=self.show_licenses).pack(anchor='w',pady=6)
        ttk.Label(page,text='本地记录库：'+str(self.store.path),style='Hint.TLabel',wraplength=850).pack(anchor='w',pady=8)

    def show_licenses(self):
        base=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))
        files=[base/'LICENSE']+sorted((base/'THIRD_PARTY_LICENSES').rglob('*'))
        dialog=tk.Toplevel(self.root); dialog.title('本项目及第三方许可证'); dialog.geometry('900x650')
        self.set_text(self.text(dialog,25),'\n\n'.join(f'── {path.relative_to(base)} ──\n'+path.read_text('utf-8',errors='replace') for path in files if path.is_file()))

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
