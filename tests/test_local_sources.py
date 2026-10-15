from contextlib import closing
from pathlib import Path
import hashlib
import hmac
import json
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
import localdb
import wechat_keys
from chatdata import ChatStore
from library import Library
from web_selftest import empty_sqlite,encrypt_page
import workspace_backup

class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def test_plaintext_old_schema_and_read_only(self):
        path=self.root/'MSG0.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE MSG(localId INTEGER,StrTalker TEXT,IsSender INTEGER,CreateTime INTEGER,StrContent TEXT,Type INTEGER)')
            db.executemany('INSERT INTO MSG VALUES(?,?,?,?,?,?)',[(1,'甲',1,1792022400,'你好',1),(2,'甲',0,1792022500,'<msg><img/></msg>',3)]);db.commit()
        before=hashlib.sha256(path.read_bytes()).digest();rows=list(localdb.iter_messages(path))
        self.assertEqual(rows[0]['role'],'self');self.assertEqual(rows[1]['kind'],3)
        with closing(localdb.readonly(path)) as db:
            with self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM MSG')
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),before)
        store=ChatStore(self.root/'data'/'persona.sqlite3');library=Library(store)
        self.assertEqual(library.import_rows(rows,str(path)),2);self.assertEqual(library.import_rows(rows,str(path)),0)
        self.assertEqual(store.stats()['total'],1);self.assertEqual(library.rows()[1],2)

    def test_wechat_four_hash_names_compression_and_direction(self):
        import zstandard
        path=self.root/'message_0.db';table='Msg_'+hashlib.md5('friend'.encode()).hexdigest()
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE Name2Id(user_name TEXT)');db.executemany('INSERT INTO Name2Id VALUES(?)',[('self',),('friend',)])
            db.execute(f'CREATE TABLE "{table}"(local_id INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content BLOB,local_type INTEGER)')
            db.execute(f'INSERT INTO "{table}" VALUES(1,1,1792022400,?,1)',(zstandard.ZstdCompressor().compress('压缩消息'.encode()),));db.commit()
        rows=list(localdb.iter_messages(path,'self',{'friend':'朋友甲'}))
        self.assertEqual(rows[0]['text'],'压缩消息');self.assertEqual(rows[0]['contact'],'朋友甲');self.assertEqual(rows[0]['role'],'self')

    def test_sqlcipher_authentication_and_wrong_key_never_replaces_output(self):
        encrypted=self.root/'encrypted.db';plain=self.root/'plain.db';key=bytes(range(32));salt=b'fixture-salt1234'
        encrypted.write_bytes(encrypt_page(empty_sqlite(),1,key,salt));self.assertTrue(localdb.verify_key(encrypted.read_bytes(),key))
        localdb.decrypt_snapshot(encrypted,plain,key)
        self.assertEqual(plain.read_bytes(),empty_sqlite())
        before=plain.read_bytes()
        with self.assertRaises(ValueError):localdb.decrypt_snapshot(encrypted,plain,b'x'*32)
        self.assertEqual(plain.read_bytes(),before)
        damaged=bytearray(encrypted.read_bytes());damaged[300]^=1;encrypted.write_bytes(damaged)
        with self.assertRaises(ValueError):localdb.decrypt_snapshot(encrypted,plain,key)
        self.assertEqual(plain.read_bytes(),before)

    def test_wal_commits_checksums_and_uncommitted_tail(self):
        path=self.root/'file-wal';magic=0x377f0682;salt=b'12345678'
        header=struct.pack('>IIII',magic,3007000,4096,0)+salt
        state=localdb.wal_checksum(header);data=header+struct.pack('>II',*state)
        for page,size in ((1,1),(2,0)):
            payload=bytes([page])*4096;first=struct.pack('>II',page,size);state=localdb.wal_checksum(first+payload,state)
            data+=first+salt+struct.pack('>II',*state)+payload
        path.write_bytes(data);self.assertEqual(len(localdb.committed_wal(path)),1)
        damaged=bytearray(data);damaged[0]^=1;path.write_bytes(damaged)
        with self.assertRaises(ValueError):localdb.committed_wal(path)

    def write_wal(self,frames,big=False):
        salt=b'12345678';header=struct.pack('>IIII',0x377f0683 if big else 0x377f0682,3007000,4096,0)+salt
        checksum=localdb.wal_checksum(header,big=big);data=header+struct.pack('>II',*checksum)
        for number,dbsize,page in frames:
            prefix=struct.pack('>II',number,dbsize);checksum=localdb.wal_checksum(prefix+page,checksum,big)
            data+=prefix+salt+struct.pack('>II',*checksum)+page
        path=self.root/'fixture-wal';path.write_bytes(data);return path

    def test_reads_native_sqlite_wal_and_both_checksum_orders(self):
        path=self.root/'native.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE sample(value TEXT)')
            db.execute('INSERT INTO sample VALUES(?)',('synthetic-only',));db.commit()
            frames=localdb.committed_wal(str(path)+'-wal')
            self.assertGreaterEqual(len(frames),2)
            self.assertEqual(frames[-1][1],db.execute('PRAGMA page_count').fetchone()[0])
        for big in (False,True):
            wal=self.write_wal([(1,1,b'p'*4096)],big)
            self.assertEqual(localdb.committed_wal(wal),[(1,1,b'p'*4096)])
        wal.write_bytes(wal.read_bytes()+b'incomplete-frame')
        self.assertEqual(len(localdb.committed_wal(wal)),1)
        wal.write_bytes(b'truncated-header')
        with self.assertRaisesRegex(ValueError,'WAL头不完整'):localdb.committed_wal(wal)
        with self.assertRaises(ValueError):localdb.wal_checksum(b'1234')

    def test_encrypted_wal_uses_latest_commit_and_never_uncommitted_tail(self):
        key=bytes(range(32));salt=b'fixture-salt1234';base=bytearray(empty_sqlite());base[18:20]=b'\x02\x02'
        encrypted=self.root/'encrypted.db';output=self.root/'plain.db';encrypted.write_bytes(encrypt_page(bytes(base),1,key,salt))
        latest=bytearray(base);struct.pack_into('>I',latest,60,42)
        uncommitted=bytearray(latest);struct.pack_into('>I',uncommitted,60,99)
        wal=self.write_wal([(1,1,encrypt_page(bytes(latest),1,key,salt)),(1,0,encrypt_page(bytes(uncommitted),1,key,salt))])
        before=encrypted.read_bytes();localdb.decrypt_snapshot(encrypted,output,key,wal=wal)
        with closing(localdb.readonly(output)) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],42)
        self.assertEqual(output.read_bytes()[18:20],b'\x01\x01');self.assertEqual(encrypted.read_bytes(),before)
        previous=output.read_bytes();corrupt=bytearray(encrypt_page(bytes(latest),1,key,salt));corrupt[150]^=1
        wal=self.write_wal([(1,1,bytes(corrupt))])
        with self.assertRaisesRegex(ValueError,'认证失败'):localdb.decrypt_snapshot(encrypted,output,key,wal=wal)
        self.assertEqual(output.read_bytes(),previous);self.assertEqual(list(self.root.glob('plain.db.*.partial')),[])

    def test_wal_growth_cannot_create_a_sparse_incomplete_database(self):
        key=bytes(range(32));salt=b'fixture-salt1234';source=self.root/'encrypted.db';output=self.root/'plain.db'
        source.write_bytes(encrypt_page(empty_sqlite(),1,key,salt));output.write_bytes(b'original-output')
        wal=self.write_wal([(1000000,1000000,encrypt_page(bytes(4096),1000000,key,salt))])
        with self.assertRaisesRegex(ValueError,'提交大小超出'):localdb.decrypt_snapshot(source,output,key,wal=wal)
        self.assertEqual(output.read_bytes(),b'original-output')

    def test_sqlcipher_three_profile_and_checkpointed_wal_header(self):
        from Crypto.Cipher import AES
        material=bytes(range(32));salt=b'fixture-salt1234';key=localdb.derive_key(material,salt,3)
        page=bytearray(empty_sqlite());page[20]=48;page[18:20]=b'\x02\x02';struct.pack_into('>H',page,105,4048)
        iv=b'fixed-fixture-iv';cipher=AES.new(key,AES.MODE_CBC,iv).encrypt(bytes(page[16:-48]))
        mac_key=hashlib.pbkdf2_hmac('sha1',key,bytes(n^0x3a for n in salt),2,32)
        tag=hmac.new(mac_key,cipher+iv+struct.pack('<I',1),'sha1').digest()
        source=self.root/'old-encrypted.db';source.write_bytes(salt+cipher+iv+tag+bytes(12));output=self.root/'plain.db'
        self.assertTrue(localdb.verify_key(source.read_bytes(),key,3));self.assertFalse(localdb.verify_key(source.read_bytes(),key,4))
        localdb.decrypt_snapshot(source,output,key,3)
        with closing(localdb.readonly(output)) as db:self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
        self.assertEqual(output.read_bytes()[18:20],b'\x01\x01')
        with self.assertRaises(ValueError):localdb.derive_key(material,salt,2)

    def test_snapshot_cancel_preserves_previous_files_and_cleans_own_temporary(self):
        key=bytes(range(32));salt=b'fixture-salt1234';source=self.root/'encrypted.db';output=self.root/'plain.db'
        source.write_bytes(encrypt_page(empty_sqlite(),1,key,salt));output.write_bytes(b'previous-snapshot')
        unrelated=self.root/'plain.db.partial';unrelated.write_bytes(b'belongs-to-another-operation');calls=0
        def cancelled():
            nonlocal calls
            calls+=1;return calls>=2
        with self.assertRaisesRegex(ValueError,'已停止'):localdb.decrypt_snapshot(source,output,key,cancelled=cancelled)
        self.assertEqual(output.read_bytes(),b'previous-snapshot');self.assertEqual(unrelated.read_bytes(),b'belongs-to-another-operation')
        self.assertEqual(list(self.root.glob('plain.db.*.partial')),[])
        folder=self.root/'copies';folder.mkdir();existing=folder/source.name;existing.write_bytes(b'old-copy');calls=0
        with self.assertRaisesRegex(ValueError,'已停止'):localdb.stable_copy(source,folder,cancelled)
        self.assertEqual(existing.read_bytes(),b'old-copy');self.assertEqual(list(folder.glob('.snapshot-*')),[])
        with self.assertRaisesRegex(ValueError,'不能覆盖'):localdb.stable_copy(source,self.root)

    def test_copy_retries_when_checkpoint_removes_wal_and_rejects_hot_journal(self):
        source=self.root/'source.db';source.write_bytes(b'consistent-source');wal=Path(str(source)+'-wal');wal.write_bytes(b'checkpoint-in-progress')
        folder=self.root/'copies';folder.mkdir();old_wal=folder/'source.db-wal';old_wal.write_bytes(b'old-snapshot-wal');calls=0
        def checkpoint():
            nonlocal calls
            calls+=1
            if calls==2:wal.unlink()
            return False
        copied=localdb.stable_copy(source,folder,checkpoint)
        self.assertEqual(copied.read_bytes(),b'consistent-source');self.assertFalse(old_wal.exists())
        journal=Path(str(source)+'-journal');journal.write_bytes(b'\xd9\xd5\x05\xf9\x20\xa1\x63\xd7'+bytes(512))
        with self.assertRaisesRegex(ValueError,'回滚日志'):localdb.stable_copy(source,folder)
        self.assertEqual(copied.read_bytes(),b'consistent-source')
        journal.write_bytes(bytes(520));self.assertEqual(localdb.stable_copy(source,folder).read_bytes(),b'consistent-source')

    def test_explicit_name_ids_group_sender_and_unknown_direction(self):
        path=self.root/'group.db';group='group@chatroom';table='Msg_'+hashlib.md5(group.encode()).hexdigest()
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE Name2Id(id INTEGER,user_name TEXT)');db.executemany('INSERT INTO Name2Id VALUES(?,?)',[(17,'self'),(42,group),(63,'friend')])
            db.execute(f'CREATE TABLE "{table}"(id INTEGER,real_sender_id INTEGER,is_sender INTEGER,create_time INTEGER,message_content TEXT,local_type INTEGER)')
            db.executemany(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?)',[(1,17,None,1792022400,'my words',1),(2,63,0,1792022401,'friend:\ngroup words',1),(3,None,None,1792022402,'unknown direction',1)])
            db.commit()
        rows=list(localdb.iter_messages(path,'self',{group:'群聊','friend':'朋友'}))
        self.assertEqual(rows[0]['role'],'self');self.assertEqual(rows[0]['contact'],'群聊')
        self.assertEqual(rows[1]['sender'],'朋友');self.assertEqual(rows[1]['text'],'group words');self.assertEqual(rows[1]['role'],'other')
        self.assertEqual(rows[2]['role'],'unknown')
        with self.assertRaisesRegex(ValueError,'已停止'):list(localdb.iter_messages(path,cancelled=lambda:True))

    def test_compressed_message_has_a_real_output_limit(self):
        import zstandard
        large=zstandard.ZstdCompressor().compress(b'x'*(8*1024*1024+1))
        with self.assertRaisesRegex(ValueError,'超过8MB'):localdb.decode(large)
        unknown_size=zstandard.ZstdCompressor(write_content_size=False).compress(b'x'*(8*1024*1024+1))
        with self.assertRaisesRegex(ValueError,'超过8MB'):localdb.decode(unknown_size)

    def test_identical_contact_names_stay_in_separate_conversations(self):
        path=self.root/'contacts.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE MSG(localId INTEGER,StrTalker TEXT,Sender TEXT,IsSender INTEGER,CreateTime INTEGER,StrContent TEXT,Type INTEGER)')
            db.executemany('INSERT INTO MSG VALUES(?,?,?,?,?,?,?)',[(1,'friend_a','friend_a',0,1792022400,'first person',1),(2,'friend_b','friend_b',0,1792022401,'second person',1)]);db.commit()
        names={'friend_a':'同名备注','friend_b':'同名备注'};rows=list(localdb.iter_messages(path,contact_names=names))
        self.assertNotEqual(rows[0]['contact'],rows[1]['contact']);self.assertEqual(rows[0]['contact'],'同名备注（friend_a）')
        self.assertEqual(rows[0]['sender'],rows[0]['contact']);self.assertEqual(rows[1]['sender'],rows[1]['contact'])
        self.assertEqual(names,{'friend_a':'同名备注','friend_b':'同名备注'})
        store=ChatStore(self.root/'data'/'persona.sqlite3');library=Library(store);library.import_rows(rows,'duplicate-label-fixture')
        self.assertEqual(len(library.contacts()),2)

    def test_group_text_prefix_does_not_override_known_sender_or_self(self):
        path=self.root/'group-text.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE MSG(localId INTEGER,StrTalker TEXT,Sender TEXT,IsSender INTEGER,CreateTime INTEGER,StrContent TEXT,Type INTEGER)')
            db.executemany('INSERT INTO MSG VALUES(?,?,?,?,?,?,?)',[(1,'group@chatroom','friend',0,1792022400,'todo:\nkeep this text',1),(2,'group@chatroom',None,1,1792022401,'note:\nmy outgoing text',1),(3,'group@chatroom',None,0,1792022402,'friend:\nlegacy incoming',1)]);db.commit()
        rows=list(localdb.iter_messages(path))
        self.assertEqual(rows[0]['sender'],'friend');self.assertEqual(rows[0]['text'],'todo:\nkeep this text')
        self.assertEqual(rows[1]['role'],'self');self.assertEqual(rows[1]['text'],'note:\nmy outgoing text')
        self.assertEqual(rows[2]['sender'],'friend');self.assertEqual(rows[2]['text'],'legacy incoming')

    def test_key_scan_preflight_does_not_read_unrelated_processes(self):
        with patch('wechat_keys.related_processes',return_value=[]) as processes:
            with self.assertRaisesRegex(ValueError,'已停止'):wechat_keys.find_keys(123,[bytes(4096)],cancelled=lambda:True)
            processes.assert_not_called()
            self.assertEqual(wechat_keys.find_keys(123,[]),{});processes.assert_not_called()
            with self.assertRaisesRegex(ValueError,'首页不完整'):wechat_keys.find_keys(123,[b'incomplete'])
            processes.assert_not_called()
            with self.assertRaisesRegex(ValueError,'微信进程'):wechat_keys.find_keys(123,[bytes(4096)])
            processes.assert_called_once_with(0)

    def test_full_workspace_media_roundtrip_and_tamper_rejection(self):
        import zipfile
        store=ChatStore(self.root/'data'/'persona.sqlite3');library=Library(store)
        file=self.root/'sample.png';file.write_bytes(b'not-a-real-image-fixture');identity=library.add_asset(file)
        library.import_rows([{'contact':'甲','sender':'我','role':'self','time':'2026-10-15','content':'<msg/>','kind':3}],'fixture')
        archive=self.root/'workspace.zip';workspace_backup.create(store,{},archive)
        store.add_memory('甲','未备份记忆')
        cfg,counts,safety=workspace_backup.restore(store,{},archive,self.root/'config.json')
        self.assertTrue(safety.exists());self.assertEqual(store.memory('甲'),[]);self.assertEqual(library.rows()[1],1)
        self.assertEqual(library.asset(identity)[1].read_bytes(),b'not-a-real-image-fixture')
        with zipfile.ZipFile(archive) as z:entries={n:z.read(n) for n in z.namelist()}
        for name in entries:
            if name.startswith('media/'):entries[name]=b'bad'
        with zipfile.ZipFile(archive,'w') as z:
            for n,v in entries.items():z.writestr(n,v)
        with self.assertRaises(ValueError):workspace_backup.restore(store,{},archive,self.root/'config.json')
        self.assertEqual(library.asset(identity)[1].read_bytes(),b'not-a-real-image-fixture')

if __name__=='__main__':unittest.main()
