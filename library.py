"""Unified text/non-text records and locally managed media assets."""
from pathlib import Path
import hashlib
import json
import mimetypes
import os
import shutil
from chatdata import normalize_rows,pick,ALIASES
from toolbox import parse_time


class Library:
    def __init__(self,store):
        self.store=store;self.root=store.path.parent/'media';self.root.mkdir(exist_ok=True)
        with store.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS media_items(id INTEGER PRIMARY KEY,contact TEXT,sender TEXT,role TEXT,ts TEXT,content TEXT,kind INTEGER,source TEXT,digest TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,name TEXT,mime TEXT,size INTEGER,filename TEXT,md5 TEXT);
            CREATE INDEX IF NOT EXISTS media_contact ON media_items(contact,id);''')

    def import_rows(self,rows,source,default='',self_names=()):
        text=[];media=[]
        for index,row in enumerate(rows):
            kind=str(row.get('kind',pick(row,ALIASES['type'],'1'))).lower()
            if kind in ('1','text','文本','0',''):
                normalized=normalize_rows([row],default,self_names)
                if row.get('source_id'):
                    for item in normalized:item['source_identity']=str(source)+'#'+row.get('table','')+'#'+row['source_id']
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
            media.append(r)
        inserted=self.store.import_rows(text,source)
        with self.store.db() as db:
            for row in media:
                digest=hashlib.sha256(json.dumps([row['contact'],row['sender'],row['time'],row['kind'],row['text']],ensure_ascii=False).encode()).hexdigest()
                inserted+=db.execute('INSERT OR IGNORE INTO media_items(contact,sender,role,ts,content,kind,source,digest) VALUES(?,?,?,?,?,?,?,?)',(row['contact'],row['sender'],row['role'],row['time'],row['text'],row['kind'],str(source),digest)).rowcount
        return inserted

    def rows(self,contact='',keyword='',offset=0,limit=100):
        where=[];args=[]
        if contact:where.append('contact=?');args.append(contact)
        if keyword:where.append('(instr(content,?) OR instr(sender,?))');args.extend([keyword,keyword])
        clause=' WHERE '+' AND '.join(where) if where else ''
        union="SELECT id,contact,sender,role,ts,content,1 kind,source,'text' origin FROM messages UNION ALL SELECT id,contact,sender,role,ts,content,kind,source,'media' origin FROM media_items"
        with self.store.db() as db:
            total=db.execute('SELECT COUNT(*) FROM ('+union+')'+clause,args).fetchone()[0]
            order="CASE WHEN ts NOT GLOB '*[^0-9.]*' AND ts<>'' THEN CAST(ts AS REAL)/(CASE WHEN CAST(ts AS REAL)>100000000000 THEN 1000 ELSE 1 END) ELSE CAST(strftime('%s',replace(ts,'/','-')) AS REAL) END"
            rows=[dict(r) for r in db.execute('SELECT * FROM ('+union+')'+clause+' ORDER BY '+order+' DESC,id DESC LIMIT ? OFFSET ?',args+[limit,offset])]
        return rows,total

    def contacts(self):
        with self.store.db() as db:return [dict(r) for r in db.execute('SELECT contact,COUNT(*) total FROM (SELECT contact FROM messages UNION ALL SELECT contact FROM media_items) GROUP BY contact ORDER BY total DESC')]

    def add_asset(self,path,name=None):
        path=Path(path);name=Path(name or path.name).name
        if path.stat().st_size>512*1024*1024:raise ValueError('单个媒体文件超过512MB')
        sha=hashlib.sha256();md5=hashlib.md5()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):sha.update(block);md5.update(block)
        identity=sha.hexdigest();extension=Path(name).suffix.lower()
        if extension not in ('.jpg','.jpeg','.png','.gif','.webp','.bmp','.mp4','.webm','.mp3','.wav','.ogg','.m4a','.silk','.dat','.pdf','.txt'):extension='.bin'
        filename=identity+extension;destination=self.root/filename
        if not destination.exists():
            temporary=self.root/(filename+'.partial');shutil.copyfile(path,temporary);os.replace(temporary,destination)
        mime=mimetypes.guess_type(name)[0] or 'application/octet-stream'
        with self.store.db() as db:db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,?)',(identity,name,mime,destination.stat().st_size,filename,md5.hexdigest()))
        return identity

    def assets(self,search=''):
        with self.store.db() as db:return [dict(r) for r in db.execute('SELECT id,name,mime,size FROM assets WHERE instr(name,?)>0 ORDER BY rowid DESC LIMIT 500',(search,))]

    def asset(self,identity):
        with self.store.db() as db:row=db.execute('SELECT * FROM assets WHERE id=?',(identity,)).fetchone()
        if not row:raise ValueError('媒体不存在')
        return dict(row),self.root/row['filename']
