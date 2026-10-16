import csv
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from chatdata import ChatStore,read_rows,CSV_ESCAPE_FIELD
from library import Library
from records import export_file
import backup
import workspace_backup


class DataIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.store=ChatStore(self.root/'data'/'persona.sqlite3');self.library=Library(self.store)

    def test_repeated_dateless_text_and_media_are_not_silently_collapsed(self):
        rows=[{'contact':'fixture','sender':'friend','content':'same message','isSelf':False,'type':kind} for kind in (1,1,3,3)]
        self.assertEqual(self.library.import_rows(rows,'dateless.json'),4)
        self.assertEqual(self.store.stats()['total'],2);self.assertEqual(self.library.rows('fixture')[1],4)
        self.assertEqual(self.library.import_rows(rows,'dateless.json'),0)

    def test_failed_media_insert_rolls_back_text_in_same_import(self):
        with self.store.db() as db:db.execute("CREATE TRIGGER test_media_failure BEFORE INSERT ON media_items BEGIN SELECT RAISE(ABORT,'injected media error'); END;")
        with self.assertRaises(sqlite3.DatabaseError):
            self.library.import_rows([{'contact':'fixture','content':'should rollback','type':1},{'contact':'fixture','content':'media failure','type':3}],'atomic.json')
        self.assertEqual(self.store.stats()['total'],0);self.assertEqual(self.library.rows()[1],0)

    def test_reimport_repairs_missing_and_corrupt_media_without_new_registry_row(self):
        source=self.root/'source.txt';source.write_bytes(b'original-bytes');identity=self.library.add_asset(source)
        managed=self.library.asset(identity)[1];managed.unlink()
        with self.assertRaisesRegex(ValueError,'缺失'):self.library.asset(identity)
        self.assertEqual(self.library.add_asset(source),identity);self.assertEqual(self.library.asset(identity)[1].read_bytes(),b'original-bytes')
        managed.write_bytes(b'corrupt');self.library.add_asset(source)
        self.assertEqual(self.library.asset(identity)[1].read_bytes(),b'original-bytes');self.assertEqual(len(self.library.assets()),1)

    def test_source_change_does_not_break_stored_hash_or_md5(self):
        source=self.root/'changing.txt';source.write_bytes(b'old!');original=Path.open;changed=[False]
        class ChangingReader:
            def __init__(self,stream):self.stream=stream
            def __enter__(self):return self
            def __exit__(self,*args):return self.stream.__exit__(*args)
            def read(self,*args):
                data=self.stream.read(*args)
                if data and not changed[0]:changed[0]=True;source.write_bytes(b'new!')
                return data
        def open_file(path,*args,**kwargs):
            stream=original(path,*args,**kwargs)
            return ChangingReader(stream) if path==source and args and args[0]=='rb' else stream
        with patch.object(Path,'open',open_file):identity=self.library.add_asset(source)
        metadata,managed=self.library.asset(identity);content=managed.read_bytes()
        self.assertTrue(changed[0]);self.assertEqual(content,b'old!');self.assertEqual(hashlib.sha256(content).hexdigest(),identity)
        with self.store.db() as db:md5=db.execute('SELECT md5 FROM assets WHERE id=?',(identity,)).fetchone()[0]
        self.assertEqual(md5,hashlib.md5(content).hexdigest())

    def test_failed_media_install_leaves_source_and_no_staging_files(self):
        source=self.root/'source.txt';source.write_bytes(b'keep-source')
        with patch('library.os.replace',side_effect=PermissionError('injected file lock')):
            with self.assertRaises(PermissionError):self.library.add_asset(source)
        self.assertEqual(source.read_bytes(),b'keep-source');self.assertEqual(self.library.assets(),[])
        self.assertEqual(list(self.library.root.glob('*.partial')),[])

    def test_csv_formula_protection_is_reversible_without_altering_real_apostrophes(self):
        rows=[{'contact':'=contact','sender':'@friend','role':'other','ts':'-100','content':text} for text in ('=literal','+literal','-literal','@literal',"'=genuine apostrophe",'normal')]
        path=self.root/'export.csv';export_file(path,rows);restored=read_rows(path)
        self.assertEqual([r['content'] for r in restored],[r['content'] for r in rows])
        self.assertTrue(all(r['contact']=='=contact' and r['sender']=='@friend' and r['time']=='-100' for r in restored))
        with path.open(encoding='utf-8-sig',newline='') as stream:
            escaped=list(csv.DictReader(stream))
        self.assertTrue(escaped[0]['content'].startswith("'="));self.assertIn('content',json.loads(escaped[0][CSV_ESCAPE_FIELD]))
        external=self.root/'external.csv';external.write_text('contact,sender,content\nfriend,other,\'=genuine apostrophe\n','utf-8')
        self.assertEqual(read_rows(external)[0]['content'],"'=genuine apostrophe")

    def test_legacy_backup_with_missing_media_is_rejected_before_replacing_data(self):
        source=self.root/'asset.txt';source.write_bytes(b'fixture');self.library.add_asset(source)
        legacy=self.root/'legacy.zip';backup.create_backup(self.store,{},legacy)
        target=ChatStore(self.root/'target'/'persona.sqlite3');Library(target);target.add_memory('fixture','keep current memory')
        with self.assertRaisesRegex(ValueError,'旧版备份不含媒体'):
            workspace_backup.restore(target,{},legacy,self.root/'target-config.json')
        self.assertEqual(target.memory('fixture')[0]['content'],'keep current memory')

    def test_workspace_backup_rejects_corrupt_media_and_keeps_prior_archive(self):
        source=self.root/'asset.txt';source.write_bytes(b'original');identity=self.library.add_asset(source)
        archive=self.root/'complete.zip';workspace_backup.create(self.store,{},archive);previous=archive.read_bytes()
        self.library.asset(identity)[1].write_bytes(b'corrupt!')
        with self.assertRaisesRegex(ValueError,'媒体内容与登记'):
            workspace_backup.create(self.store,{},archive)
        self.assertEqual(archive.read_bytes(),previous)
        self.assertEqual(self.library.asset(identity)[1].read_bytes(),b'corrupt!')

    def test_workspace_backup_rejects_wrong_asset_identity_size_and_md5(self):
        source=self.root/'asset.txt';source.write_bytes(b'fixture');identity=self.library.add_asset(source)
        for field,value,error in (('id','0'*64,'登记ID'),('size',999,'媒体内容与登记'),('md5','0'*32,'媒体内容与登记')):
            with self.subTest(field=field):
                with self.store.db() as db:
                    previous=db.execute('SELECT '+field+' FROM assets').fetchone()[0]
                    db.execute('UPDATE assets SET '+field+'=?',(value,))
                archive=self.root/(field+'.zip')
                with self.assertRaisesRegex(ValueError,error):workspace_backup.create(self.store,{},archive)
                self.assertFalse(archive.exists())
                with self.store.db() as db:db.execute('UPDATE assets SET '+field+'=?',(previous,))
        self.assertEqual(self.library.asset(identity)[0]['size'],7)

    def _rewrite_workspace_with_valid_container_checksums(self,archive,*,media_bytes=None,asset_update=None):
        # Model a self-consistent archive whose payload disagrees with the asset
        # registry. Outer ZIP and inner database hashes alone cannot catch this.
        with zipfile.ZipFile(archive) as bundle:files={name:bundle.read(name) for name in bundle.namelist()}
        if media_bytes is not None:
            name=next(name for name in files if name.startswith('media/'));files[name]=media_bytes
        if asset_update:
            with zipfile.ZipFile(io.BytesIO(files['core.zip'])) as core:core_files={name:core.read(name) for name in core.namelist()}
            database=self.root/'tampered.sqlite3';database.write_bytes(core_files['persona.sqlite3'])
            with closing(sqlite3.connect(database)) as db,db:db.execute('UPDATE assets SET '+asset_update[0]+'=?',(asset_update[1],))
            core_files['persona.sqlite3']=database.read_bytes()
            manifest=json.loads(core_files['manifest.json']);manifest['sha256']['persona.sqlite3']=hashlib.sha256(core_files['persona.sqlite3']).hexdigest()
            core_files['manifest.json']=json.dumps(manifest).encode()
            output=io.BytesIO()
            with zipfile.ZipFile(output,'w') as core:
                for name,data in core_files.items():core.writestr(name,data)
            files['core.zip']=output.getvalue()
        manifest=json.loads(files['workspace.json'])
        for name,data in files.items():
            if name!='workspace.json':manifest['files'][name]={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        files['workspace.json']=json.dumps(manifest).encode()
        with zipfile.ZipFile(archive,'w') as bundle:
            for name,data in files.items():bundle.writestr(name,data)

    def test_restore_rejects_corrupt_media_even_with_recomputed_manifest(self):
        source=self.root/'asset.txt';source.write_bytes(b'original');self.library.add_asset(source)
        archive=self.root/'complete.zip';workspace_backup.create(self.store,{},archive)
        self._rewrite_workspace_with_valid_container_checksums(archive,media_bytes=b'corrupt!')
        target=ChatStore(self.root/'target'/'persona.sqlite3');Library(target);target.add_memory('fixture','keep current memory')
        config_path=self.root/'target-config.json';config_path.write_text('{"model":"keep"}')
        with self.assertRaisesRegex(ValueError,'媒体内容与登记'):
            workspace_backup.restore(target,{},archive,config_path)
        self.assertEqual(target.memory('fixture')[0]['content'],'keep current memory')
        self.assertEqual(config_path.read_text(),'{"model":"keep"}');self.assertFalse((target.path.parent/'backups').exists())

    def test_restore_rejects_wrong_registered_asset_identity_size_and_md5(self):
        source=self.root/'asset.txt';source.write_bytes(b'fixture');self.library.add_asset(source)
        target=ChatStore(self.root/'target'/'persona.sqlite3');Library(target);target.add_memory('fixture','keep current memory')
        for field,value,error in (('id','0'*64,'登记ID'),('size',999,'媒体内容与登记'),('md5','0'*32,'媒体内容与登记')):
            with self.subTest(field=field):
                archive=self.root/(field+'.zip');workspace_backup.create(self.store,{},archive)
                self._rewrite_workspace_with_valid_container_checksums(archive,asset_update=(field,value))
                with self.assertRaisesRegex(ValueError,error):
                    workspace_backup.restore(target,{},archive,self.root/'target-config.json')
                self.assertEqual(target.memory('fixture')[0]['content'],'keep current memory')
                self.assertFalse((target.path.parent/'backups').exists())

    def test_legacy_restore_rejects_existing_but_corrupt_media(self):
        source=self.root/'asset.txt';source.write_bytes(b'original');identity=self.library.add_asset(source)
        legacy=self.root/'legacy.zip';backup.create_backup(self.store,{},legacy)
        self.store.add_memory('fixture','keep current memory')
        self.library.asset(identity)[1].write_bytes(b'corrupt!')
        with self.assertRaisesRegex(ValueError,'媒体内容与登记'):
            workspace_backup.restore(self.store,{},legacy,self.root/'config.json')
        self.assertEqual(self.store.memory('fixture')[0]['content'],'keep current memory')
        self.assertFalse((self.store.path.parent/'backups').exists())

    def test_workspace_backup_and_restore_apply_same_limits_and_keep_prior_output(self):
        archive=self.root/'backup.zip';workspace_backup.create(self.store,{},archive);before=archive.read_bytes()
        with patch('workspace_backup.MAX_WORKSPACE_BYTES',1):
            with self.assertRaisesRegex(ValueError,'8GB'):workspace_backup.create(self.store,{},archive)
            with self.assertRaisesRegex(ValueError,'8GB'):workspace_backup.restore(self.store,{},archive,self.root/'config.json')
        self.assertEqual(archive.read_bytes(),before)
        with patch('workspace_backup.MAX_MANIFEST_BYTES',1):
            with self.assertRaisesRegex(ValueError,'8MB'):workspace_backup.create(self.store,{},archive)
            with self.assertRaisesRegex(ValueError,'清单过大'):workspace_backup.restore(self.store,{},archive,self.root/'config.json')
        self.assertEqual(archive.read_bytes(),before)

    def test_invalid_json_settings_cannot_be_backed_up_as_valid_workspace(self):
        self.store.add_memory('fixture','keep current memory')
        with self.store.db() as db:db.execute('INSERT INTO settings VALUES(?,?)',('synthetic-invalid','{broken-json'))
        archive=self.root/'broken.zip'
        with self.assertRaisesRegex(ValueError,'设置损坏'):workspace_backup.create(self.store,{},archive)
        self.assertFalse(archive.exists());self.assertEqual(self.store.memory('fixture')[0]['content'],'keep current memory')


if __name__=='__main__':unittest.main()
