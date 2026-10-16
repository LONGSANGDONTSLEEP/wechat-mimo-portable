"""Loopback-only desktop application API. No cloud hosting or external assets."""
from contextlib import closing
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs,unquote
import urllib.request
import urllib.error
from app import DEFAULT,completion,validate_connection,prepare_messages
from chatdata import ChatStore,read_rows
from library import Library
from desktop_engine import DesktopEngine
from configuration import load_config
import localdb
import workspace_backup
from toolbox import select_messages,describe,transcript,analyze

VERSION='2.0.0-preview.4'


class State:
    def __init__(self,root,assets):
        self.root=Path(root);self.assets=Path(assets);self.store=ChatStore(self.root/'data'/'persona.sqlite3');self.library=Library(self.store)
        self.engine=DesktopEngine(self.store);self.config,self.config_warnings=load_config(self.root/'config.json',DEFAULT)
        self.key='';self.token=secrets.token_urlsafe(32);self.jobs={};self.job_lock=threading.Lock();self.keys={};self.sources=self.store.get('local_sources',[])
        self.dialogs=queue.Queue();self.last_seen=time.monotonic();self.exit=threading.Event();self.cancel=threading.Event()
        self.downloads={};self.imports={}

    def cfg(self):
        return dict(self.config,local_auth=True,persona_enabled=self.store.get('learn',True),longer=self.store.get('longer',True),proactive=self.store.get('proactive',True),auto=False)

    def job(self,label,work):
        with self.job_lock:
            if self.exit.is_set():raise ValueError('工作台正在退出，不能启动新任务')
            if any(j['state'] in ('running','pending') for j in self.jobs.values()):raise ValueError('请先等待当前任务完成，或停止当前任务')
            while len(self.jobs)>=64:self.jobs.pop(next(iter(self.jobs)))
            identity=secrets.token_hex(8);entry={'id':identity,'label':label,'state':'running','progress':'正在准备…','result':None,'error':''};self.jobs[identity]=entry;self.cancel.clear()
        def progress(text):entry['progress']=str(text)
        def run():
            try:
                entry['result']=work(progress)
                partial=self.cancel.is_set() or isinstance(entry['result'],dict) and bool(entry['result'].get('warnings'))
                entry['state']='partial' if partial else 'success';entry['progress']='部分完成，请查看结果中的未完成项' if partial else '已完成'
            except Exception as exc:entry['error']=str(exc);entry['state']='error';entry['progress']='操作未完成'
        threading.Thread(target=run,daemon=True).start()
        return {'job':identity}

    def download(self,path,name=None):
        identity=secrets.token_hex(12);self.downloads[identity]=(Path(path),name or Path(path).name)
        return {'download':'/api/download/'+identity,'name':name or Path(path).name}

    def local_import(self,params,progress):
        if not str(params.get('path','')).strip():raise ValueError('请先选择数据库目录或文件')
        root=Path(params['path']).expanduser().resolve()
        if not root.exists():raise ValueError('所选数据库路径不存在，请重新选择账号的db_storage目录')
        try:version=int(params.get('version',4))
        except (TypeError,ValueError):raise ValueError('请选择微信3或微信4加密格式') from None
        if version not in (3,4):raise ValueError('请选择微信3或微信4加密格式')
        files=localdb.inventory(root);selected=[r for r in files if root.is_file() or r['name'].lower() in ('contact.db','micromsg.db') or __import__('re').match(r'(?i)(?:biz_)?message_\d+\.db|msg\d*\.db',r['name'])]
        if not selected:selected=files
        key_text=params.get('key','').strip();self_name=params.get('self_name','').strip();prepared=[];errors=[];count=0
        material=None
        if key_text:
            try:material=bytes.fromhex(key_text)
            except ValueError:raise ValueError('数据库密钥应为64位十六进制字符') from None
            if len(material)!=32:raise ValueError('数据库密钥应为64位十六进制字符')
        if not self_name:
            import re
            account=root.parent.name if root.name=='db_storage' else root.name
            match=re.fullmatch(r'(wxid_[A-Za-z0-9]+)_[A-Za-z0-9]{4}',account)
            if match:self_name=match[1]
        with tempfile.TemporaryDirectory(prefix='studio-source-') as temporary:
            folder=Path(temporary)
            encrypted=[entry for entry in selected if entry['encrypted']]
            if params.get('auto_key') and encrypted and not material:
                from wechat_keys import find_keys,related_processes,ProcessAccessError,ScanLimitError
                raw_pid=params.get('pid')
                try:pid=int(raw_pid) if raw_pid not in (None,'') else 0
                except (ValueError,TypeError):raise ValueError('请选择当前正在运行且可读取的微信进程') from None
                if pid not in related_processes(0):raise ValueError('请选择当前正在运行的微信进程')
                pages=[]
                for entry in encrypted:
                    with open(entry['path'],'rb') as stream:page=stream.read(4096)
                    if len(page)!=4096:raise ValueError(entry['name']+'：加密数据库首页不完整，未检查微信进程')
                    pages.append(page)
                checked=0;scan_errors=[]
                for process in related_processes(pid):
                    pending=[page for page in pages if page[:16].hex() not in self.keys]
                    if not pending or self.cancel.is_set():break
                    progress(f'检查微信关联进程 {process}…')
                    try:
                        self.keys.update(find_keys(process,pending,progress,self.cancel.is_set,version=version));checked+=1
                    except (ProcessAccessError,ScanLimitError) as exc:
                        if self.cancel.is_set():raise ValueError('读取已停止') from None
                        scan_errors.append(f'PID {process}：{exc}')
                unresolved=[page for page in pages if page[:16].hex() not in self.keys]
                if unresolved:
                    if not checked:
                        message='没有可完成检查的微信进程，未读取到密钥。'+('\n'+'\n'.join(scan_errors[:8]) if scan_errors else '')
                    else:message=f'已完成 {checked} 个微信进程检查，仍有 {len(unresolved)} 个数据库未匹配到密钥；当前微信版本的自动解密尚不可用。'
                    if len(encrypted)==len(selected):raise ValueError(message+'\n可改用已有密钥、已解密消息库或导出文件。')
                    errors.append(message)
            elif encrypted and not material and not self.keys and all(entry['encrypted'] for entry in selected):
                raise ValueError('所选数据库已加密。请提供已有数据库密钥，或使用已解密消息库/导出文件。自动取密钥仍在实验阶段。')
            for index,entry in enumerate(selected):
                if self.cancel.is_set():raise ValueError('读取已停止')
                progress(f"准备数据库 {index+1}/{len(selected)}：{entry['name']}")
                target=folder/(str(index)+'.db')
                try:
                    if entry['encrypted']:
                        with open(entry['path'],'rb') as stream:first=stream.read(4096)
                        key=self.keys.get(first[:16].hex())
                        if material:
                            candidate=material
                            if not localdb.verify_key(first,candidate,version):candidate=localdb.derive_key(candidate,first[:16],version)
                            key=candidate
                        if not key:raise ValueError('未取得可验证密钥，请使用本机读取密钥或已有数据库密钥')
                        copied=folder/('source-'+str(index));copied.mkdir();snapshot=localdb.stable_copy(entry['path'],copied,cancelled=self.cancel.is_set)
                        localdb.decrypt_snapshot(snapshot,target,key,version,str(snapshot)+'-wal',cancelled=self.cancel.is_set)
                    else:
                        with closing(localdb.readonly(entry['path'])) as source,closing(sqlite3.connect(target)) as destination:source.backup(destination)
                    prepared.append((entry['path'],target))
                except Exception as exc:errors.append(entry['name']+'：'+str(exc))
            names=localdb.contact_map([p for origin,p in prepared if Path(origin).name.lower() in ('contact.db','micromsg.db')])
            coverage=[]
            for origin,path in prepared:
                progress('读取消息：'+Path(origin).name);batch=[];read_count=0
                for row in localdb.iter_messages(path,self_name,names,cancelled=self.cancel.is_set):
                    if self.cancel.is_set():raise ValueError('读取已停止；已入库记录保留，可再次更新并去重')
                    batch.append(row);read_count+=1
                    if len(batch)>=500:count+=self.library.import_rows(batch,origin,self_names=[self_name] if self_name else []);batch=[]
                if batch:count+=self.library.import_rows(batch,origin,self_names=[self_name] if self_name else [])
                coverage.append({'database':Path(origin).name,'messages':read_count,'tables':len(localdb.inspect_schema(path))})
        if not prepared:raise ValueError('未能打开任何数据库。\n'+'\n'.join(errors[:12]))
        if not any(c['messages'] for c in coverage):raise ValueError('所选库没有可导入的聊天消息，可能是配置库、联系人库或空库。请选择包含message_*.db / MSG消息表的db_storage目录或聊天导出文件。'+('\n'+'\n'.join(errors[:5]) if errors else ''))
        source={'path':str(root),'self_name':self_name,'version':version}
        if source not in self.sources:self.sources.append(source)
        self.store.set('local_sources',self.sources)
        return {'inserted':count,'coverage':coverage,'warnings':errors,'note':'更新时重新读取只读快照，已存在消息自动去重；密钥仅本次运行保留。'}

    def api(self,path,p):
        if any(j['state']=='running' and '恢复' in j['label'] for j in self.jobs.values()) and path not in ('/api/state','/api/heartbeat','/api/cancel'):
            raise ValueError('工作区正在恢复，请稍后操作')
        if path=='/api/heartbeat':self.last_seen=time.monotonic();return {'ok':True}
        if path=='/api/state':
            self.last_seen=time.monotonic()
            from backup import clean_config
            config=clean_config(self.config);config.pop('auto',None)
            return {'version':VERSION,'stats':self.store.stats(),'contacts':self.library.contacts(),'config':config,'config_warnings':list(self.config_warnings),'key_set':bool(self.key),'jobs':list(self.jobs.values())[-12:],
                    'style':self.store.style(),'sources':self.store.get('local_sources',[]),'persona':{k:self.store.get(k,True) for k in ('learn','longer','proactive')},'engine':self.engine.state()}
        if path=='/api/messages':
            rows,total=self.library.rows(p.get('contact',''),p.get('keyword',''),max(0,int(p.get('offset',0))),100)
            return {'rows':rows,'total':total}
        if path=='/api/media':return self.library.assets(p.get('keyword',''))
        if path=='/api/config':
            from backup import clean_config
            updated=dict(self.config,**{k:v for k,v in p.items() if k in DEFAULT})
            updated=clean_config(updated)
            if updated.get('provider') in ('lmstudio','ollama'):
                from app import local_budget
                local_budget(dict(DEFAULT,**updated))
            if updated.get('provider',DEFAULT['provider'])!=self.config.get('provider') or updated.get('base_url','').rstrip('/')!=self.config.get('base_url','').rstrip('/'):self.key=''
            self.config=dict(DEFAULT,**updated)
            if 'key' in p:self.key=str(p['key']).strip()
            (self.root/'config.json').write_text(json.dumps(self.config,ensure_ascii=False,indent=2),'utf-8')
            self.config_warnings=[]
            return {'saved':True,'key_set':bool(self.key)}
        if path=='/api/models':
            provider=self.config.get('provider');base=urlsplit(self.config['base_url'])
            if provider not in ('lmstudio','ollama'):raise ValueError('请先选择本地模型服务并保存')
            if base.scheme not in ('http','https') or not base.hostname:raise ValueError('本地服务地址无效')
            endpoint='/api/tags' if provider=='ollama' else '/v1/models'
            from app import normalize_api_key
            token=normalize_api_key(self.key) if self.key else ''
            request=urllib.request.Request(f'{base.scheme}://{base.netloc}{endpoint}',headers={'Authorization':'Bearer '+token} if token else {})
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5) as r:data=json.load(r)
            except urllib.error.HTTPError as exc:
                if exc.code==401:raise ValueError('本地服务要求认证（HTTP 401）。请核对服务的 API Token 设置；未开启认证时可留空。') from None
                raise ValueError(f'读取本地模型失败：HTTP {exc.code}') from None
            except urllib.error.URLError:raise ValueError('无法连接本地模型服务，请先启动 LM Studio / Ollama 服务器并检查地址') from None
            except (ValueError,UnicodeDecodeError):raise ValueError('模型列表响应不是有效 JSON，请检查服务地址') from None
            return [r['id'] for r in data.get('data',[])] if provider=='lmstudio' else [r['name'] for r in data.get('models',[]) if not r.get('remote_host') and not r['name'].endswith((':cloud','-cloud'))]
        if path=='/api/test':
            cfg=dict(self.cfg(),prompt='只回复OK。',max_tokens=128);key=self.key
            return self.job('模型连接测试',lambda progress:{'text':completion(cfg,key,'你好',[])})
        if path=='/api/persona':
            for key in ('learn','longer','proactive'):
                if key in p:self.store.set(key,bool(p[key]))
            return {'saved':True}
        if path=='/api/memories':
            if p.get('add'):self.store.add_memory(p['contact'],p['add'])
            if p.get('delete'):self.store.delete_memory(int(p['delete']))
            return self.store.memory(p.get('contact',''))
        if path in ('/api/reply','/api/prompt'):
            text=str(p.get('text','')).strip();contact=p.get('contact','')
            if not text:raise ValueError('请输入对方的消息')
            cfg=self.engine.reply_cfg(self.cfg(),contact,text);history=self.store.recent(contact);key=self.key
            actual=prepare_messages(cfg,text,history)
            if path=='/api/prompt':return actual
            return self.job('生成回复预览',lambda progress:{'text':completion(cfg,key,text,history)})
        if path=='/api/analysis':
            choice=p.get('range','最近一周（7天）');selection=select_messages(self.store,p.get('contact',''),choice,p.get('start',''),p.get('end',''))
            if p.get('preview'):return {'description':describe(selection),'text':transcript(selection),'count':len(selection['rows'])}
            cfg=self.cfg();key=self.key;task=p.get('task','对话摘要')
            return self.job(task,lambda progress:{'text':analyze(selection,task,cfg,key,completion,progress,self.cancel.is_set)})
        if path=='/api/statistics':
            from records import statistics
            result=statistics(self.store,p.get('contact',''))
            return {k:dict(v) if hasattr(v,'most_common') else str(v) if k in ('first','last') and v else v for k,v in result.items()}
        if path=='/api/discover':return localdb.discover()
        if path=='/api/inventory':return localdb.inventory(p['path'])
        if path=='/api/local-import':return self.job('读取本机/已解密数据库',lambda progress:self.local_import(p,progress))
        if path=='/api/dialog':
            event=threading.Event();request={'kind':p.get('kind','folder'),'event':event,'value':None};self.dialogs.put(request)
            if not event.wait(180):raise ValueError('文件选择超时')
            return {'path':request['value'] or ''}
        if path=='/api/folder-media':
            folder=Path(p['path']).expanduser().resolve()
            if not folder.is_dir():raise ValueError('媒体文件夹不存在，请选择有效目录')
            def work(progress):
                count=0;errors=[]
                for file in folder.rglob('*'):
                    if self.cancel.is_set():break
                    if file.is_symlink() or not file.is_file() or file.suffix.lower() not in ('.jpg','.jpeg','.png','.gif','.webp','.bmp','.mp4','.webm','.mp3','.wav','.ogg','.m4a','.silk','.dat','.pdf'):continue
                    try:self.library.add_asset(file);count+=1
                    except Exception as exc:errors.append(file.name+'：'+str(exc))
                    progress(f'已索引 {count} 个媒体文件…')
                return {'imported':count,'warnings':errors[:30]}
            return self.job('导入本机媒体文件夹',work)
        if path=='/api/backup':
            folder=self.root/'data'/'exports';folder.mkdir(exist_ok=True);destination=folder/('Studio备份-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4)+'.zip');cfg=self.cfg()
            def work(progress):workspace_backup.create(self.store,cfg,destination);return self.download(destination)
            return self.job('完整备份（含媒体）',work)
        if path=='/api/export':
            from records import export_file,all_rows
            folder=self.root/'data'/'exports';folder.mkdir(exist_ok=True);extension=p.get('format','json')
            if extension not in ('json','csv','html'):raise ValueError('不支持的导出格式')
            destination=folder/('聊天导出-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4)+'.'+extension)
            def work(progress):
                rows,_=self.library.rows(p.get('contact',''),'',0,10000000)
                if extension=='json':destination.write_text(json.dumps({'messages':[dict(contact=r['contact'],sender=r['sender'],role=r['role'],time=r['ts'],content=r['content'],type=r['kind']) for r in rows]},ensure_ascii=False,indent=2),'utf-8')
                else:export_file(destination,rows)
                result=dict(self.download(destination),messages=len(rows),note='单个聊天导出文件回导上限150MB；媒体和整库迁移请使用完整备份。')
                if destination.stat().st_size>150*1024**2:result['warnings']=['导出文件超过150MB，当前不能直接回导。请使用完整备份迁移，或按联系人分别导出。']
                return result
            return self.job('导出会话',work)
        if path=='/api/cancel':self.cancel.set();return {'requested':True}
        if path=='/api/windows':
            import vision
            return [{'hwnd':h,'pid':pid,'title':title} for h,pid,title,kind in vision.list_windows()]
        if path=='/api/processes':
            from wechat_keys import related_processes,process_status
            import vision
            titles={pid:title for hwnd,pid,title,kind in vision.list_windows()}
            return [dict(process_status(pid),title=titles.get(pid,'微信后台进程')) for pid in related_processes(0)]
        if path=='/api/window-select':self.engine.select(p['hwnd']);return self.engine.state()
        if path=='/api/window-point':self.engine.point(p['x'],p['y']);return self.engine.state()
        if path=='/api/ocr-test':
            if not self.engine.monitor:raise ValueError('请先选择微信窗口，再测试识别')
            def work(progress):
                view=self.engine.inspect(self.engine.monitor['hwnd']);return {'title':view.title,'incoming':view.message,'messages':[{'side':r.side,'text':r.text} for r in view.messages]}
            return self.job('仅测试OCR',work)
        if path=='/api/monitor-start':self.engine.start(dict(self.cfg(),auto=bool(p.get('auto')),record_contact=p.get('contact',''),title=p.get('title','')),self.key);return self.engine.state()
        if path=='/api/monitor-stop':self.engine.halt();return self.engine.state()
        if path=='/api/send-preview':return self.job('发送当前预览',lambda progress:self.engine.send())
        if path=='/api/quit':self.engine.halt();self.exit.set();return {'ok':True}
        raise ValueError('未知功能接口')


class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,state,port=0):self.state=state;super().__init__(('127.0.0.1',port),Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def response(self,status,data,kind='application/json; charset=utf-8',headers=None):
        if not isinstance(data,bytes):data=json.dumps(data,ensure_ascii=False,default=str).encode('utf-8')
        self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'")
        for key,value in (headers or {}).items():self.send_header(key,value)
        self.end_headers();self.wfile.write(data)

    def auth(self):
        state=self.server.state;token=self.headers.get('X-Studio-Token','')
        if not token:
            from http.cookies import SimpleCookie
            try:token=SimpleCookie(self.headers.get('Cookie','')).get('studio').value
            except Exception:token=''
        origin=self.headers.get('Origin','')
        if origin and origin!=f'http://127.0.0.1:{self.server.server_port}':return False
        return secrets.compare_digest(token,state.token)

    def do_GET(self):
        path=urlsplit(self.path).path;state=self.server.state
        try:
            if path.startswith('/api/'):
                if not self.auth():return self.response(403,{'error':'会话无效，请从桌面程序重新打开工作台'})
                p={k:v[0] for k,v in parse_qs(urlsplit(self.path).query).items()}
                if path=='/api/screenshot':return self.response(200,state.engine.screenshot(),'image/png')
                if path.startswith('/api/asset/'):
                    meta,file=state.library.asset(path.rsplit('/',1)[-1]);return self.file(file,meta['mime'],meta['name'])
                if path.startswith('/api/download/'):
                    file,name=state.downloads[path.rsplit('/',1)[-1]];return self.file(file,'application/octet-stream',name,True)
                if path not in ('/api/state','/api/messages','/api/media','/api/statistics','/api/discover','/api/windows','/api/processes','/api/heartbeat','/api/memories'):raise ValueError('此操作必须使用POST')
                if path=='/api/memories':p={'contact':p.get('contact','')}
                return self.response(200,state.api(path,p))
            file=state.assets/({'/':'index.html'}.get(path,path.lstrip('/')))
            if file.resolve().parent!=state.assets.resolve() or not file.is_file():return self.response(404,{'error':'页面不存在'})
            import mimetypes
            return self.response(200,file.read_bytes(),(mimetypes.guess_type(file.name)[0] or 'text/plain')+'; charset=utf-8')
        except Exception as exc:self.response(400,{'error':str(exc)})

    def file(self,path,mime,name,attachment=False):
        from urllib.parse import quote
        size=path.stat().st_size;start=0;end=size-1;status=200;range_header=self.headers.get('Range','')
        if range_header:
            import re
            match=re.fullmatch(r'bytes=(\d+)-(\d*)',range_header)
            suffix=re.fullmatch(r'bytes=-(\d+)',range_header)
            if suffix:
                count=int(suffix[1]);start=max(0,size-count);status=206
                if not count or not size:return self.response(416,{'error':'媒体范围超出文件'},headers={'Content-Range':f'bytes */{size}'})
            elif match:
                start=int(match[1]);end=min(end,int(match[2])) if match[2] else end;status=206
            else:return self.response(416,{'error':'不支持的媒体范围'},headers={'Content-Range':f'bytes */{size}'})
            if start>end or start>=size:return self.response(416,{'error':'媒体范围超出文件'},headers={'Content-Range':f'bytes */{size}'})
        self.send_response(status);self.send_header('Content-Type',mime if mime.startswith(('image/','audio/','video/')) and mime!='image/svg+xml' else 'application/octet-stream')
        self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(max(0,end-start+1)));self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store')
        self.send_header('Content-Disposition',('attachment' if attachment else 'inline')+"; filename*=UTF-8''"+quote(name))
        if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        with path.open('rb') as stream:
            stream.seek(start);remaining=end-start+1
            while remaining>0:
                block=stream.read(min(1024*1024,remaining))
                if not block:break
                self.wfile.write(block);remaining-=len(block)

    def do_POST(self):
        state=self.server.state;path=urlsplit(self.path).path
        target=None
        if not self.auth():return self.response(403,{'error':'无效会话'})
        try:
            length=int(self.headers.get('Content-Length',0))
            upload_params=parse_qs(urlsplit(self.path).query)
            mode=upload_params.get('mode',['records'])[0]
            restoring=path=='/api/upload' and mode=='restore'
            maximum=9*1024**3 if restoring else (512 if mode=='media' else 150)*1024**2
            if not 0<=length<=maximum:raise ValueError('完整备份包超过9GB上传上限' if restoring else '媒体文件超过512MB' if mode=='media' else '聊天记录文件超过150MB，请按联系人或时间分批导入')
            if path=='/api/upload':
                if any(j['state']=='running' for j in state.jobs.values()):raise ValueError('请等待当前任务结束后上传文件')
                p=upload_params;name=Path(p.get('name',['upload.json'])[0]).name
                mode=p.get('mode',['records'])[0];folder=state.root/'data'/'uploads';folder.mkdir(exist_ok=True)
                if mode not in ('records','media','restore'):raise ValueError('不支持的上传类型')
                if mode=='restore':
                    if state.engine.busy():raise ValueError('请先停止微信监听，并等待当前发送结束')
                    if p.get('confirm',[''])[0]!='replace':raise ValueError('恢复前需要明确确认替换当前工作区')
                target=folder/(secrets.token_hex(8)+Path(name).suffix)
                remaining=length
                with target.open('wb') as stream:
                    while remaining:
                        block=self.rfile.read(min(1024*1024,remaining))
                        if not block:raise ValueError('上传中断')
                        stream.write(block);remaining-=len(block)
                if mode=='media':
                    try:result={'id':state.library.add_asset(target,name)}
                    finally:target.unlink(missing_ok=True)
                    return self.response(200,result)
                if mode=='restore':
                    if state.engine.busy():raise ValueError('请先停止微信监听，并等待当前发送结束')
                    if p.get('confirm',[''])[0]!='replace':raise ValueError('恢复前需要明确确认替换当前工作区')
                    def restore(progress):
                        try:
                            cfg,counts,safety=workspace_backup.restore(state.store,state.cfg(),target,state.root/'config.json')
                            state.config=dict(DEFAULT,**cfg);state.key='';state.keys.clear();state.library=Library(state.store);state.sources=state.store.get('local_sources',[])
                            return {'restored':counts,'safety':str(safety)}
                        finally:target.unlink(missing_ok=True)
                    return self.response(200,state.job('校验并恢复完整工作区',restore))
                rows=read_rows(target)
                preview=rows[:8];aliases=p.get('self',[''])[0].replace('；',';').split(';');contact=p.get('contact',[''])[0]
                if p.get('preview',[''])[0]=='1':
                    identity=secrets.token_hex(8);state.imports[identity]=(target,name)
                    return self.response(200,{'import_id':identity,'rows':preview,'count':len(rows),'fields':list(dict.fromkeys(k for row in rows[:100] for k in row))})
                try:count=state.library.import_rows(rows,name,contact,aliases)
                finally:target.unlink(missing_ok=True)
                return self.response(200,{'inserted':count})
            if length>1024*1024:raise ValueError('请求过大')
            p=json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(p,dict):raise ValueError('请求格式不正确')
            if path=='/api/session':return self.response(200,{'ok':True},headers={'Set-Cookie':f'studio={state.token}; HttpOnly; SameSite=Strict; Path=/'})
            if path=='/api/import-discard':
                entry=state.imports.pop(p.get('import_id',''),None)
                if entry:entry[0].unlink(missing_ok=True)
                return self.response(200,{'discarded':bool(entry)})
            if path=='/api/import-confirm':
                if any(j['state']=='running' for j in state.jobs.values()):raise ValueError('请等待当前任务结束后导入')
                entry=state.imports.pop(p.get('import_id',''),None)
                if not entry:raise ValueError('导入预览已过期，请重新选择文件')
                file,name=entry
                try:
                    rows=read_rows(file);mapping=p.get('mapping',{})
                    if mapping:
                        canonical={'text':'content','contact':'contact','sender':'sender','time':'time','self':'isSelf','type':'type'}
                        from chatdata import ALIASES
                        adapted=[]
                        for row in rows:
                            copy=dict(row)
                            for field,column in mapping.items():
                                if field in canonical and column:
                                    value=row.get(column,'')
                                    for alias in ALIASES.get(field,()):copy.pop(alias,None)
                                    copy[canonical[field]]=value
                            adapted.append(copy)
                        rows=adapted
                    count=state.library.import_rows(rows,name,p.get('contact',''),p.get('self','').replace('；',';').split(';'))
                finally:file.unlink(missing_ok=True)
                return self.response(200,{'inserted':count})
            return self.response(200,state.api(path,p))
        except Exception as exc:
            if target:target.unlink(missing_ok=True)
            return self.response(400,{'error':str(exc)})
