from contextlib import closing
from pathlib import Path
import hashlib
import json
import sqlite3
import struct
import tempfile
import unittest
import localdb
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
