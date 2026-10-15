import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import backup
from chatdata import ChatStore,normalize_rows
from records import all_rows

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.store=ChatStore(self.root/'data'/'persona.sqlite3')
        self.config={'provider':'lmstudio','model':'local-test','base_url':'http://localhost:1234/v1','prompt':'自然回复','auto':True,'context_size':4096,'api_key':'fake-secret'}
        self.store.import_rows(normalize_rows([dict(contact='甲',sender='我',isSelf=True,time='2026-10-15',content='备份消息')]),'fixture')
        self.store.add_memory('甲','周六骑车'); self.store.set('binding','甲'); self.store.set('learn',True)
        self.store.remember_exchange('甲','你好','你好呀')
        self.archive=self.root/'完整备份.zip'

    def test_restore_complete_state_and_keep_pre_restore_copy(self):
        counts=backup.create_backup(self.store,self.config,self.archive)
        self.assertEqual(counts,dict(messages=1,settings=2,memories=1,exchanges=1))
        with zipfile.ZipFile(self.archive) as z:
            config=json.loads(z.read('config.json')); self.assertNotIn('api_key',config);self.assertFalse(config['auto'])
        self.store.add_memory('甲','恢复前的新记忆'); self.store.set('learn',False)
        cfg,restored,safety=backup.restore_backup(self.store,self.config,self.archive,self.root/'config.json')
        self.assertEqual(restored,counts);self.assertTrue(self.store.get('learn'))
        self.assertEqual(self.store.memory('甲')[0]['content'],'周六骑车')
        self.assertEqual(len(self.store.memory('甲')),1);self.assertEqual(len(self.store.recent('甲')),2)
        self.assertTrue(safety.is_file()); self.assertFalse(cfg['auto'])
        folder=self.root/'check';folder.mkdir()
        _,old_counts=backup.unpack_checked(safety,folder)
        self.assertEqual(old_counts['memories'],2)

    def test_corrupt_and_unrelated_zip_leave_current_database_unchanged(self):
        backup.create_backup(self.store,self.config,self.archive)
        with zipfile.ZipFile(self.archive) as z:entries={name:z.read(name) for name in z.namelist()}
        entries['persona.sqlite3']=b'corrupt'
        with zipfile.ZipFile(self.archive,'w') as z:
            for name,data in entries.items():z.writestr(name,data)
        with self.assertRaises(ValueError):backup.restore_backup(self.store,self.config,self.archive,self.root/'config.json')
        self.assertEqual(all_rows(self.store)[0]['content'],'备份消息')
        self.assertFalse((self.root/'config.json').exists())
        with zipfile.ZipFile(self.archive,'w') as z:z.writestr('../escape','bad')
        with self.assertRaises(ValueError):backup.restore_backup(self.store,self.config,self.archive,self.root/'config.json')
        self.assertFalse((self.root/'escape').exists())

    def test_failed_database_replace_rolls_back_configuration(self):
        backup.create_backup(self.store,self.config,self.archive)
        config_path=self.root/'config.json'; config_path.write_text('{"model":"before"}','utf-8')
        original=backup.os.replace
        def replace(source,destination):
            if Path(destination)==self.store.path:raise PermissionError('simulated file lock')
            return original(source,destination)
        with patch('backup.os.replace',side_effect=replace):
            with self.assertRaises(PermissionError):backup.restore_backup(self.store,self.config,self.archive,config_path)
        self.assertEqual(json.loads(config_path.read_text('utf-8')),{'model':'before'})
        self.assertEqual(all_rows(self.store)[0]['content'],'备份消息')

if __name__=='__main__':unittest.main()
