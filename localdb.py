"""Read-only WeChat/SQLite source adapter, with authenticated SQLCipher snapshots."""
from contextlib import closing
from pathlib import Path
import hashlib
import hmac
import os
import re
import sqlite3
import struct
import tempfile
import time

SQLITE_HEADER=b'SQLite format 3\x00'
PAGE_SIZE=4096


def _cipher_profile(version):
    if version==4:return 'sha512',80,256000
    if version==3:return 'sha1',48,64000
    raise ValueError('仅支持微信使用的4096字节SQLCipher 3/4加密配置')


def derive_key(material,salt,version=4):
    """Derive an AES key from the 32-byte WeChat key material, in memory only."""
    digest,reserve,iterations=_cipher_profile(version)
    if not isinstance(material,bytes) or len(material)!=32 or len(salt)!=16:raise ValueError('数据库密钥材料或盐值长度不正确')
    return hashlib.pbkdf2_hmac(digest,material,salt,iterations,32)


def _check_cancelled(cancelled):
    if cancelled():raise ValueError('读取已停止')


def quote(name):return '"'+name.replace('"','""')+'"'


def discover():
    home=Path.home(); roots=[home/'Documents'/'xwechat_files',home/'xwechat_files',home/'Documents'/'WeChat Files']
    for drive in ('C:','D:','E:','F:'):
        for suffix in ('xwechat_files','WeChat Files','微信文件/xwechat_files','Weixin/xwechat_files'):
            roots.append(Path(drive+'/')/suffix)
    config=Path(os.environ.get('APPDATA',''))/'Tencent'/'xwechat'/'All Users'/'config'
    if config.is_dir():
        for ini in config.glob('*.ini'):
            if ini.stat().st_size>4096:continue
            raw=ini.read_bytes()
            for encoding in ('utf-8-sig','utf-16-le','gb18030'):
                try:text=raw.decode(encoding).strip('\x00\r\n ')
                except UnicodeError:continue
                if re.match(r'^[A-Za-z]:[\\/]',text):roots.extend([Path(text),Path(text)/'xwechat_files'])
    accounts=[]; seen=set()
    for root in roots:
        try:
            candidates=[root]+[p for p in root.iterdir() if p.is_dir()]
            for candidate in candidates:
                data=candidate/'db_storage'
                if not data.is_dir() and (candidate/'Msg').is_dir():data=candidate/'Msg'
                if data.is_dir() and str(data.resolve()) not in seen:
                    seen.add(str(data.resolve()));accounts.append({'account':candidate.name,'path':str(data),'files':len(list(data.rglob('*.db')))})
        except (PermissionError,OSError):continue
    return accounts


def inventory(root):
    root=Path(root)
    files=[root] if root.is_file() else sorted(root.rglob('*.db'))
    result=[]
    for path in files:
        if path.is_symlink() or not path.is_file():continue
        with path.open('rb') as stream:header=stream.read(16)
        result.append({'path':str(path),'name':path.name,'size':path.stat().st_size,'encrypted':header!=SQLITE_HEADER,
                       'wal':Path(str(path)+'-wal').exists()})
    if not result:raise ValueError('所选目录中没有数据库文件，请选择账号的db_storage目录或数据库文件')
    return result


def verify_key(page,key,version=4):
    digest,reserve,iterations=_cipher_profile(version)
    if not isinstance(key,bytes) or not isinstance(page,bytes) or len(key)!=32 or len(page)!=PAGE_SIZE:return False
    mac_key=hashlib.pbkdf2_hmac(digest,key,bytes(x^0x3a for x in page[:16]),2,32)
    expected=hmac.new(mac_key,page[16:4096-reserve+16]+struct.pack('<I',1),digest).digest()
    return hmac.compare_digest(expected,page[4096-reserve+16:4096-reserve+16+len(expected)])


def wal_checksum(data,state=(0,0),big=False):
    if len(data)%8:raise ValueError('WAL校验数据长度必须是8的倍数')
    words=struct.unpack(('>' if big else '<')+str(len(data)//4)+'I',data);a,b=state
    for i in range(0,len(words),2):a=(a+words[i]+b)&0xffffffff;b=(b+words[i+1]+a)&0xffffffff
    return a,b


def committed_wal(path,cancelled=lambda:False):
    """Validate rolling WAL checksums and ignore uncommitted/trailing frames."""
    path=Path(path)
    _check_cancelled(cancelled)
    if not path.exists() or path.stat().st_size==0:return []
    if path.stat().st_size<32:raise ValueError('WAL头不完整，请重新获取快照')
    with path.open('rb') as stream:
        header=stream.read(32);magic,version,size=struct.unpack('>III',header[:12])
        if magic not in (0x377f0682,0x377f0683) or version!=3007000 or size!=PAGE_SIZE:raise ValueError('不支持的WAL格式，未忽略最新消息')
        state=wal_checksum(header[:24],big=bool(magic&1))
        if state!=struct.unpack('>II',header[24:32]):raise ValueError('WAL头校验失败，请重新获取快照')
        frames=[];last_commit=0
        while True:
            _check_cancelled(cancelled)
            frame=stream.read(24+size)
            if len(frame)!=24+size:break
            if frame[8:16]!=header[16:24]:break
            calculated=wal_checksum(frame[:8]+frame[24:],state,bool(magic&1))
            if calculated!=struct.unpack('>II',frame[16:24]):break
            state=calculated;number,dbsize=struct.unpack('>II',frame[:8])
            if not number or number>100000000:raise ValueError('WAL页号异常')
            frames.append((number,dbsize,frame[24:]))
            if dbsize:last_commit=len(frames)
        return frames[:last_commit]


def stable_copy(source,folder,cancelled=lambda:False):
    source=Path(source);folder=Path(folder);destination=folder/source.name
    if source.resolve()==destination.resolve():raise ValueError('数据库快照不能覆盖微信原文件')
    wal=Path(str(source)+'-wal');target_wal=Path(str(destination)+'-wal')
    def check_journal():
        try:
            with Path(str(source)+'-journal').open('rb') as journal:magic=journal.read(8)
        except FileNotFoundError:return
        if magic==b'\xd9\xd5\x05\xf9\x20\xa1\x63\xd7':
            raise ValueError('数据库存在待恢复的回滚日志，请先正常打开并关闭微信后重试')
    def state(path):
        try:s=path.stat();return s.st_size,s.st_mtime_ns
        except FileNotFoundError:return None
    with tempfile.TemporaryDirectory(prefix='.snapshot-',dir=folder) as scratch:
        staged=Path(scratch)/source.name;staged_wal=Path(str(staged)+'-wal')
        for attempt in range(4):
            _check_cancelled(cancelled)
            check_journal()
            before=(state(source),state(wal))
            def copy_file(origin,target):
                with origin.open('rb') as reader,target.open('wb') as writer:
                    while True:
                        _check_cancelled(cancelled)
                        chunk=reader.read(1024*1024)
                        if not chunk:break
                        writer.write(chunk)
            try:
                copy_file(source,staged)
                if before[1]:copy_file(wal,staged_wal)
                else:staged_wal.unlink(missing_ok=True)
            except FileNotFoundError:
                # A checkpoint can remove the WAL between stat and open.
                if not source.exists():raise ValueError('源数据库已被移动或删除') from None
                continue
            _check_cancelled(cancelled)
            check_journal()
            if before==(state(source),state(wal)):
                os.replace(staged,destination)
                if before[1]:os.replace(staged_wal,target_wal)
                else:target_wal.unlink(missing_ok=True)
                return destination
            time.sleep(.2)
    raise ValueError('微信正在持续写入，无法获得稳定快照；请暂停聊天后重试')


def decrypt_snapshot(source,destination,key,version=4,wal=None,cancelled=lambda:False):
    from Crypto.Cipher import AES
    source=Path(source); destination=Path(destination)
    if source.resolve()==destination.resolve():raise ValueError('解密副本不能覆盖微信原文件')
    if isinstance(key,str):
        try:key=bytes.fromhex(key.strip())
        except ValueError:raise ValueError('数据库密钥应为64位十六进制原始密钥') from None
    digest,reserve,iterations=_cipher_profile(version)
    _check_cancelled(cancelled)
    size=source.stat().st_size
    if size%4096:raise ValueError('数据库页不完整，当前快照不能安全读取')
    with source.open('rb') as stream:
        first=stream.read(4096)
        if not verify_key(first,key,version):raise ValueError('密钥或数据库加密版本不匹配，未读取消息')
        mac_key=hashlib.pbkdf2_hmac(digest,key,bytes(x^0x3a for x in first[:16]),2,32)
        stream.seek(0)
        # An interrupted old run (or another destination) must never have its
        # partial file silently overwritten by this operation.
        descriptor,name=tempfile.mkstemp(prefix=destination.name+'.',suffix='.partial',dir=destination.parent)
        os.close(descriptor);temporary=Path(name)
        try:
            with temporary.open('wb') as target:
                for index in range(1,size//4096+1):
                    _check_cancelled(cancelled)
                    page=stream.read(4096); start=16 if index==1 else 0
                    iv=page[-reserve:-reserve+16]
                    tag=hmac.new(mac_key,page[start:4096-reserve+16]+struct.pack('<I',index),digest).digest()
                    if not hmac.compare_digest(tag,page[4096-reserve+16:4096-reserve+16+len(tag)]):raise ValueError(f'第{index}页校验失败，源文件可能变化，请停止微信写入后重试')
                    plain=AES.new(key,AES.MODE_CBC,iv).decrypt(page[start:-reserve])
                    target.write((SQLITE_HEADER if index==1 else b'')+plain+b'\x00'*reserve)
            frames=committed_wal(wal,cancelled) if wal else []
            if frames:
                final_size=frames[-1][1];base_size=size//4096
                extra={number for number,dbsize,page in frames if base_size<number<=final_size}
                # New database pages must be supplied by the WAL. Exclude the
                # SQLite lock-byte page, which is deliberately never written.
                lock_page=1073741824//4096+1
                missing_lock=int(base_size<lock_page<=final_size and lock_page not in extra)
                if final_size>base_size and len(extra)!=final_size-base_size-missing_lock:
                    raise ValueError('WAL提交大小超出已验证页面范围，未生成不完整快照')
                with temporary.open('r+b') as target:
                    for number,dbsize,page in frames:
                        _check_cancelled(cancelled)
                        start=16 if number==1 else 0
                        tag=hmac.new(mac_key,page[start:4096-reserve+16]+struct.pack('<I',number),digest).digest()
                        if not hmac.compare_digest(tag,page[4096-reserve+16:4096-reserve+16+len(tag)]):raise ValueError('WAL消息页认证失败，未返回不完整记录')
                        plain=AES.new(key,AES.MODE_CBC,page[-reserve:-reserve+16]).decrypt(page[start:-reserve])
                        if number>final_size:continue
                        target.seek((number-1)*4096);target.write((SQLITE_HEADER if number==1 else b'')+plain+b'\x00'*reserve)
                    target.truncate(final_size*4096)
            # The materialized snapshot never has pending WAL, including a
            # fully checkpointed source whose encrypted header still says WAL.
            with temporary.open('r+b') as target:target.seek(18);target.write(b'\x01\x01')
            _check_cancelled(cancelled)
            with closing(sqlite3.connect(temporary)) as check:
                check.set_progress_handler(lambda:int(bool(cancelled())),10000)
                try:
                    if check.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('解密副本完整性检查失败')
                except sqlite3.OperationalError:
                    _check_cancelled(cancelled);raise
            _check_cancelled(cancelled);os.replace(temporary,destination)
        finally:temporary.unlink(missing_ok=True)


def readonly(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    db.execute('PRAGMA query_only=ON'); db.execute('PRAGMA trusted_schema=OFF'); db.row_factory=sqlite3.Row
    return db


def decode(value):
    if value is None:return ''
    if isinstance(value,bytes):
        if value.startswith(b'\x28\xb5\x2f\xfd'):
            import zstandard
            # max_output_size alone does not bound frames declaring their size.
            # A bounded stream also covers unknown-size and concatenated frames.
            import io
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(value)) as reader:value=reader.read(8*1024*1024+1)
            if len(value)>8*1024*1024:raise ValueError('单条压缩消息超过8MB，已停止读取该库')
        return value.decode('utf-8',errors='replace')
    return str(value)


def inspect_schema(path):
    with closing(readonly(path)) as db:
        return [{'table':r[0],'columns':[c[1] for c in db.execute('PRAGMA table_info('+quote(r[0])+')')]} for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


def iter_messages(path,self_name='',contact_names=None,cancelled=lambda:False):
    """Signature-based parser for WeChat 3 MSG, WeChat 4 Msg_hash and generic exports."""
    contact_names=contact_names or {}
    # The current library uses the visible contact label as its conversation
    # key. Preserve separate people when remarks/nicknames happen to coincide.
    counts={}
    for display in contact_names.values():counts[display]=counts.get(display,0)+1
    contact_names={name:(display if counts[display]==1 else f'{display}（{name}）') for name,display in contact_names.items()}
    with closing(readonly(path)) as db:
        schemas=inspect_schema(path); names={}; hashes={hashlib.md5(name.encode()).hexdigest():name for name in contact_names}
        for schema in schemas:
            _check_cancelled(cancelled)
            if schema['table'].lower()=='name2id':
                cols={c.lower():c for c in schema['columns']}; user=cols.get('user_name') or cols.get('username')
                if user:
                    identity=next((cols[key] for key in ('id','local_id','user_id') if key in cols),None)
                    for row in db.execute('SELECT '+(quote(identity) if identity else 'rowid')+','+quote(user)+' FROM '+quote(schema['table'])):
                        _check_cancelled(cancelled)
                        name=decode(row[1]); names[str(row[0])]=name; hashes[hashlib.md5(name.encode()).hexdigest()]=name
        def pick(cols,*options):return next((cols[o.lower()] for o in options if o.lower() in cols),None)
        for schema in schemas:
            _check_cancelled(cancelled)
            cols={c.lower():c for c in schema['columns']}; table=schema['table']
            body=pick(cols,'message_content','strContent','content','content_','msg_content')
            when=pick(cols,'create_time','createTime','timestamp','time')
            if not body or not when:continue
            talker=pick(cols,'strTalker','talker','talker_name','contact','chat_name','user_name')
            sender=pick(cols,'sender','sender_name','real_sender_id','sender_id')
            direction=pick(cols,'isSender','is_sender','is_send','isSelf')
            kind=pick(cols,'local_type','type','msg_type'); identity=pick(cols,'local_id','localId','meslocalid','id')
            default=hashes.get(table[4:].lower(),table) if table.lower().startswith('msg_') else table
            for raw in db.execute('SELECT * FROM '+quote(table)):
                _check_cancelled(cancelled)
                text=decode(raw[body]); contact=decode(raw[talker]) if talker else default
                user=decode(raw[sender]) if sender else ''
                user=names.get(user,user); contact=names.get(contact,contact)
                role='unknown'
                if direction:
                    flag=decode(raw[direction]).strip().lower()
                    if flag in ('1','true','yes','self','me'):role='self'
                    elif flag in ('0','false','no','other'):role='other'
                if contact.endswith('@chatroom') and ':\n' in text:
                    prefix,body_text=text.split(':\n',1)
                    # A WeChat 4 row already names the sender. An ordinary
                    # message such as "todo:\n..." is not a sender envelope.
                    if re.fullmatch(r'[A-Za-z0-9_@.\-]{3,}',prefix) and (user==prefix or (not user and role!='self')):user=prefix;text=body_text
                if role=='unknown' and self_name and user:role='self' if user==self_name else 'other'
                if not user:user='我' if role=='self' else contact
                try:message_type=int(raw[kind] or 1)&0xffffffff if kind else 1
                except (ValueError,TypeError):raise ValueError(f'数据库表{table}的消息类型格式无法识别') from None
                yield dict(contact=contact_names.get(contact,contact),sender=contact_names.get(user,user),role=role,
                           time=decode(raw[when]),text=text,kind=message_type,source_id=decode(raw[identity]) if identity else '',table=table)


def contact_map(paths):
    result={}
    for path in paths:
        with closing(readonly(path)) as db:
            for schema in inspect_schema(path):
                cols={c.lower():c for c in schema['columns']}
                user=cols.get('username') or cols.get('user_name'); names=[cols[k] for k in ('remark','remark_name','nick_name','nickname') if k in cols]
                if user and names:
                    for row in db.execute('SELECT '+','.join(quote(c) for c in [user]+names)+' FROM '+quote(schema['table'])):
                        display=next((decode(value) for value in row[1:] if value),'')
                        if row[0] and display:result[decode(row[0])]=display
    return result
