"""Synthetic-only end-to-end checks included in the portable executable."""
from contextlib import closing
from datetime import datetime,timedelta
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
import time
import urllib.request
import urllib.error
from unittest.mock import patch
from web_service import State,Server,VERSION
from toolbox import SHANGHAI
import localdb


def seed(state):
    now=datetime.now(SHANGHAI)
    samples=[('林间小路','朋友','other','明天下午去公园骑车吗？'),('林间小路','我','self','可以啊，下午三点怎么样'),('林间小路','朋友','other','好，东门见。记得带水。'),('林间小路','我','self','哈哈收到，到时候联系'),('周末小分队','小周','other','这周日一起吃饭吧，大家有空吗'),('周末小分队','我','self','我有空，找家安静点的就行'),('项目交流','同事','other','初稿我整理好了，明天再一起看一下'),('项目交流','我','self','行，明天上午聊，辛苦啦')]
    rows=[dict(contact=c,sender=s,role=r,time=(now-timedelta(minutes=(len(samples)-i)*8)).isoformat(),text=t,kind=1) for i,(c,s,r,t) in enumerate(samples)]
    state.library.import_rows(rows,'synthetic-demo');state.store.add_memory('林间小路','演示事实：习惯周末骑车')
    from PIL import Image,ImageDraw
    image=Image.new('RGB',(640,420),'#dfefe6');draw=ImageDraw.Draw(image)
    draw.ellipse((180,70,460,350),fill='#a6d9c5');draw.rectangle((280,170,360,320),fill='#539b83')
    path=state.root/'demo-image.png';image.save(path);state.library.add_asset(path,'演示图片.png');path.unlink()


def empty_sqlite():
    page=bytearray(4096);page[:16]=localdb.SQLITE_HEADER
    struct.pack_into('>H',page,16,4096);page[18:24]=bytes([1,1,80,64,32,32])
    for offset,value in ((24,1),(28,1),(44,4),(56,1),(92,1),(96,3049000)):struct.pack_into('>I',page,offset,value)
    page[100]=13;struct.pack_into('>H',page,105,4016)
    return bytes(page)


def encrypt_page(plain,number,key,salt):
    from Crypto.Cipher import AES
    iv=hashlib.sha256(b'fixture'+struct.pack('<I',number)).digest()[:16];start=16 if number==1 else 0
    cipher=AES.new(key,AES.MODE_CBC,iv).encrypt(plain[start:-80]);mac_key=hashlib.pbkdf2_hmac('sha512',key,bytes(x^0x3a for x in salt),2,32)
    tag=hmac.new(mac_key,cipher+iv+struct.pack('<I',number),'sha512').digest()
    return (salt if number==1 else b'')+cipher+iv+tag


def run(root,assets):
    with tempfile.TemporaryDirectory(prefix='studio-2-selftest-') as temporary:
        (Path(temporary)/'config.json').write_text(json.dumps(dict(context_size='8192',max_tokens='768',interval='3')),'utf-8-sig')
        state=State(Path(temporary),assets);seed(state);server=Server(state);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        assert state.config['context_size']==8192 and state.config['max_tokens']==768
        base=f'http://127.0.0.1:{server.server_port}'
        def request(path,data=None,auth=True):
            headers={'X-Studio-Token':state.token} if auth else {}
            if data is not None:headers['Content-Type']='application/json'
            req=urllib.request.Request(base+path,json.dumps(data).encode() if data is not None else None,headers)
            with urllib.request.urlopen(req,timeout=10) as response:return json.load(response)
        def wait(identity):
            deadline=time.monotonic()+10
            while state.jobs[identity]['state']=='running' and time.monotonic()<deadline:time.sleep(.03)
            assert state.jobs[identity]['state']=='success',state.jobs[identity]
            return state.jobs[identity]['result']
        try:
            try:request('/api/state',auth=False);raise AssertionError('missing authentication accepted')
            except urllib.error.HTTPError as exc:assert exc.code==403
            assert request('/api/state')['stats']['total']==8
            assert request('/api/messages?keyword='+urllib.parse.quote('公园'))['total']==1
            with patch('web_service.completion',return_value='演示回复，未调用真实模型。'):
                result=wait(request('/api/reply',{'contact':'林间小路','text':'明天几点见？'})['job'])
                assert result['text'].startswith('演示回复')
            with patch('web_service.completion',side_effect=ValueError('模拟模型错误')):
                identity=request('/api/reply',{'contact':'林间小路','text':'你好'})['job']
                deadline=time.monotonic()+5
                while state.jobs[identity]['state']=='running' and time.monotonic()<deadline:time.sleep(.03)
                assert state.jobs[identity]['state']=='error' and '模拟模型错误' in state.jobs[identity]['error']
            snapshot=Path(temporary)/'fixture-encrypted.db';decoded=Path(temporary)/'fixture-plain.db';key=bytes(range(32));salt=b'fixture-salt1234'
            snapshot.write_bytes(encrypt_page(empty_sqlite(),1,key,salt));localdb.decrypt_snapshot(snapshot,decoded,key)
            with closing(localdb.readonly(decoded)) as db:assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            from workspace_backup import create,restore
            archive=Path(temporary)/'backup.zip';create(state.store,state.cfg(),archive)
            state.store.add_memory('林间小路','不应保留的额外记忆')
            cfg,counts,safety=restore(state.store,state.cfg(),archive,Path(temporary)/'config.json')
            assert len(state.store.memory('林间小路'))==1 and len(state.library.assets())==1 and safety.exists()
            meta,file=state.library.asset(state.library.assets()[0]['id']);assert file.is_file()
            with urllib.request.urlopen(base+'/',timeout=5) as response:assert b'app.js' in response.read()
            from PIL import Image,ImageDraw,ImageFont
            import asyncio
            from app import recognize_image
            image=Image.new('RGB',(500,100),'white');ImageDraw.Draw(image).text((10,10),'你好 微信 测试',font=ImageFont.truetype('msyh.ttc',32),fill='black')
            assert '你好' in ''.join(asyncio.run(recognize_image(image)).text.split())
        finally:server.shutdown();server.server_close();thread.join()
    (Path(root)/'self-test.txt').write_text(f'SELF_TEST_OK {VERSION}: legacy config migration + authenticated HTTP + import/search + simulated AI success/failure + authenticated SQLCipher snapshot + media backup/restore + bundled OCR','utf-8')
