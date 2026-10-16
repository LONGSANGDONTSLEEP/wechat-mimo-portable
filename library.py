"""Unified text/non-text records and locally managed media assets."""
from pathlib import Path
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
from chatdata import normalize_rows,pick,ALIASES
from toolbox import parse_time


class Library:
    def __init__(self,store):
        self.store=store;self.root=store.path.parent/'media';self.root.mkdir(exist_ok=True)
        with store.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS media_items(id INTEGER PRIMARY KEY,contact TEXT,sender TEXT,role TEXT,ts TEXT,content TEXT,kind INTEGER,source TEXT,digest TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,name TEXT,mime TEXT,size INTEGER,filename TEXT,md5 TEXT);
            CREATE INDEX IF NOT EXISTS media_contact ON media_items(contact,id);
            CREATE INDEX IF NOT EXISTS assets_md5 ON assets(md5);''')

    def import_rows(self,rows,source,default='',self_names=()):
        text=[];media=[]
        for index,row in enumerate(rows):
            kind=str(row.get('kind',pick(row,ALIASES['type'],'1'))).lower()
            if kind in ('1','text','文本','0',''):
                normalized=normalize_rows([row],default,self_names)
                for item in normalized:item['row']=index
                if row.get('source_id') is not None:
                    for item in normalized:item['source_identity']=str(source)+'#'+str(row.get('table',''))+'#'+str(row['source_id'])
                text.extend(normalized);continue
            copy={key:pick(row,ALIASES[key],'') for key in ('contact','sender','time','self')};copy['role']=row.get('role','unknown');copy['type']=1
            copy['isSelf']=copy.pop('self')
            # Preserve XML and unsupported message bodies without training on them.
            body=pick(row,ALIASES['text'],'')
            if not isinstance(body,str):body=str(body)
            copy['content']='[媒体消息]'
            normalized=normalize_rows([copy],default,self_names)
            if not normalized:continue
            r=normalized[0];r['text']=body;r['kind']=int(kind) if kind.isdecimal() else 0
            r['row']=index
            if row.get('source_id') is not None:r['source_identity']=str(source)+'#'+str(row.get('table',''))+'#'+str(row['source_id'])
            media.append(r)
        with self.store.db() as db:
            inserted=self.store.import_rows(text,source,db)
            for row in media:
                identity=[row['contact'],row['sender'],row['time'],row['kind'],row['text']]
                if row.get('source_identity'):identity.append(row['source_identity'])
                if not row['time']:identity.extend([str(source),row['row']])
                digest=hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()
                inserted+=db.execute('INSERT OR IGNORE INTO media_items(contact,sender,role,ts,content,kind,source,digest) VALUES(?,?,?,?,?,?,?,?)',(row['contact'],row['sender'],row['role'],row['time'],row['text'],row['kind'],str(source),digest)).rowcount
                if row['role']!='unknown':db.execute('UPDATE media_items SET role=? WHERE digest=?',(row['role'],digest))
        return inserted

    def rows(self,contact='',keyword='',offset=0,limit=100):
        where=[];args=[]
        if contact:where.append('contact=?');args.append(contact)
        if keyword:where.append('(instr(content,?) OR instr(sender,?))');args.extend([keyword,keyword])
        clause=' WHERE '+' AND '.join(where) if where else ''
        union="SELECT id,contact,sender,role,ts,content,1 kind,source,'text' origin FROM messages UNION ALL SELECT id,contact,sender,role,ts,content,kind,source,'media' origin FROM media_items"
        with self.store.db() as db:
            total=db.execute('SELECT COUNT(*) FROM ('+union+')'+clause,args).fetchone()[0]
            def epoch(value):
                parsed=parse_time(value)
                return parsed.timestamp() if parsed else None
            db.create_function('studio_time',1,epoch,deterministic=True)
            order='studio_time(ts)'
            rows=[dict(r) for r in db.execute('SELECT * FROM ('+union+')'+clause+' ORDER BY '+order+' DESC,id DESC LIMIT ? OFFSET ?',args+[limit,offset])]
            hashes=set()
            for row in rows:
                row['assets']=[]
                if row['origin']=='media':hashes.update(re.findall(r'(?i)(?<![a-f0-9])[a-f0-9]{32}(?![a-f0-9])',row['content']))
            linked={}
            for digest in hashes:
                linked[digest.lower()]=[dict(r) for r in db.execute('SELECT id,name,mime,size FROM assets WHERE md5=?',(digest.lower(),))]
            for row in rows:
                if row['origin']=='media':
                    matched=re.findall(r'(?i)(?<![a-f0-9])[a-f0-9]{32}(?![a-f0-9])',row['content'])
                    row['assets']=list({a['id']:a for h in matched for a in linked.get(h.lower(),[])}.values())
        return rows,total

    def contacts(self):
        with self.store.db() as db:return [dict(r) for r in db.execute('SELECT contact,COUNT(*) total FROM (SELECT contact FROM messages UNION ALL SELECT contact FROM media_items) GROUP BY contact ORDER BY total DESC')]

    def add_asset(self,path,name=None):
        path=Path(path);name=Path(name or path.name).name
        if path.stat().st_size>512*1024*1024:raise ValueError('单个媒体文件超过512MB')
        sha=hashlib.sha256();md5=hashlib.md5();size=0;temporary=None
        try:
            # Hash exactly the bytes copied in one pass, even if the source file
            # changes. Unique staging files also permit concurrent equal uploads.
            with path.open('rb') as stream,tempfile.NamedTemporaryFile(dir=self.root,prefix='.asset-',suffix='.partial',delete=False) as target:
                temporary=Path(target.name)
                for block in iter(lambda:stream.read(1024*1024),b''):
                    size+=len(block)
                    if size>512*1024*1024:raise ValueError('单个媒体文件超过512MB')
                    sha.update(block);md5.update(block);target.write(block)
            identity=sha.hexdigest()
            extension=Path(name).suffix.lower()
            if extension not in ('.jpg','.jpeg','.png','.gif','.webp','.bmp','.mp4','.webm','.mp3','.wav','.ogg','.m4a','.silk','.dat','.pdf','.txt'):extension='.bin'
            with self.store.db() as db:
                current=db.execute('SELECT filename FROM assets WHERE id=?',(identity,)).fetchone()
                filename=current['filename'] if current else identity+extension
                if not isinstance(filename,str) or not re.fullmatch(r'[0-9a-f]{64}\.[a-z0-9]{1,6}',filename):raise ValueError('媒体登记路径无效，请检查备份')
                # Reimport repairs a missing/corrupted managed file; a registry
                # match alone must not be reported as a successful import.
                os.replace(temporary,self.root/filename)
                mime=mimetypes.guess_type(name)[0] or 'application/octet-stream'
                db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,?)',(identity,name,mime,size,filename,md5.hexdigest()))
                db.execute('UPDATE assets SET size=?,md5=? WHERE id=?',(size,md5.hexdigest(),identity))
            return identity
        finally:
            if temporary:temporary.unlink(missing_ok=True)

    def assets(self,search=''):
        with self.store.db() as db:return [dict(r) for r in db.execute('SELECT id,name,mime,size FROM assets WHERE instr(name,?)>0 ORDER BY rowid DESC LIMIT 500',(search,))]

    def asset(self,identity):
        with self.store.db() as db:row=db.execute('SELECT * FROM assets WHERE id=?',(identity,)).fetchone()
        if not row:raise ValueError('媒体不存在')
        filename=row['filename']
        if not isinstance(filename,str) or not re.fullmatch(r'[0-9a-f]{64}\.[a-z0-9]{1,6}',filename):raise ValueError('媒体登记路径无效，请检查备份')
        path=self.root/filename
        if not path.is_file():raise ValueError('媒体文件缺失，请从原文件重新导入后重试')
        return dict(row),path
