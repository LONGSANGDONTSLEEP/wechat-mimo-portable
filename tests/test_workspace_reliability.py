from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from chatdata import ChatStore
from library import Library
import backup
import workspace_backup


class WorkspaceReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.store=ChatStore(self.root/'data'/'persona.sqlite3');self.library=Library(self.store)
        self.config=self.root/'config.json';self.config.write_text('{"model":"current","max_tokens":555}',encoding='utf-8')
        self.cfg={'model':'archived','max_tokens':1234,'reasoning':'none','api_key':'never-back-up-secret'}
        asset=self.root/'fixture.png';asset.write_bytes(b'archived-media')
        self.identity=self.library.add_asset(asset);self.archive=self.root/'backup.zip'
        self.store.add_memory('甲','归档记忆');workspace_backup.create(self.store,self.cfg,self.archive)
        self.store.add_memory('甲','当前记忆');self.old_config=self.config.read_bytes()
        second=self.root/'current.png';second.write_bytes(b'current-media');self.current_identity=self.library.add_asset(second)

    def unchanged(self):
        self.assertEqual([row['content'] for row in self.store.memory('甲')],['归档记忆','当前记忆'])
        self.assertEqual(self.config.read_bytes(),self.old_config)
        self.assertEqual(self.library.asset(self.identity)[1].read_bytes(),b'archived-media')
        self.assertEqual(self.library.asset(self.current_identity)[1].read_bytes(),b'current-media')

    def test_create_never_overwrites_database_config_or_managed_media(self):
        before=self.store.path.read_bytes()
        for destination in (self.store.path,self.config,self.library.asset(self.identity)[1]):
            with self.assertRaisesRegex(ValueError,'覆盖'):workspace_backup.create(self.store,self.cfg,destination)
        self.assertEqual(self.store.path.read_bytes(),before);self.unchanged()

    def test_restore_failure_at_each_mutation_keeps_original_workspace(self):
        original=workspace_backup.os.replace
        for stage in ('move-old-media','install-new-media','install-db','install-config'):
            with self.subTest(stage=stage):
                def replace(source,destination):
                    source=Path(source);destination=Path(destination)
                    should_fail=(stage=='move-old-media' and destination.name=='old-media' or
                                 stage=='install-new-media' and source.name=='media' and source.parent.name=='payload' or
                                 stage=='install-db' and source.parent.name=='checked' and destination==self.store.path or
                                 stage=='install-config' and source.name=='new-config')
                    if should_fail:raise PermissionError('injected-'+stage)
                    return original(source,destination)
                with patch('workspace_backup.os.replace',side_effect=replace):
                    with self.assertRaises(PermissionError):workspace_backup.restore(self.store,{},self.archive,self.config)
                self.unchanged()

    def test_extended_model_config_survives_and_secrets_do_not(self):
        with zipfile.ZipFile(self.archive) as archive:
            manifest=json.loads(archive.read('workspace.json'))
            self.assertNotIn('api_key',manifest['config']);self.assertNotIn('never-back-up-secret',archive.read('workspace.json').decode())
        cfg,_,_=workspace_backup.restore(self.store,{},self.archive,self.config)
        self.assertEqual(cfg['max_tokens'],1234);self.assertEqual(cfg['reasoning'],'none');self.assertFalse(cfg['auto'])

    def test_legacy_restore_returns_full_media_safety_backup(self):
        legacy=self.root/'legacy.zip';backup.create_backup(self.store,{},legacy)
        _,_,safety=workspace_backup.restore(self.store,{},legacy,self.config)
        with zipfile.ZipFile(safety) as archive:
            self.assertIn('workspace.json',archive.namelist())
            self.assertEqual(sum(name.startswith('media/') for name in archive.namelist()),2)

    def test_bad_manifest_schema_fails_before_mutation(self):
        with zipfile.ZipFile(self.archive) as archive:entries={name:archive.read(name) for name in archive.namelist()}
        for bad in ([],{'format':'wechat-studio-workspace','version':2,'files':[]},{'format':'wechat-studio-workspace','version':2,'files':{}}):
            with self.subTest(manifest=bad):
                entries['workspace.json']=json.dumps(bad)
                with zipfile.ZipFile(self.archive,'w') as archive:
                    for name,data in entries.items():archive.writestr(name,data)
                with self.assertRaises(ValueError):workspace_backup.restore(self.store,{},self.archive,self.config)
                self.unchanged()

    def test_failed_rollback_retains_restorable_complete_safety_archive(self):
        original=workspace_backup.os.replace
        def replace(source,destination):
            source=Path(source)
            if source.name in ('new-config','old-db'):raise PermissionError('injected-disk-error')
            return original(source,destination)
        with patch('workspace_backup.os.replace',side_effect=replace):
            with self.assertRaisesRegex(RuntimeError,'完整安全备份'):workspace_backup.restore(self.store,{},self.archive,self.config)
        safety=next((self.store.path.parent/'backups').glob('*.zip'))
        recovered=ChatStore(self.root/'recovered'/'persona.sqlite3');recovered_library=Library(recovered)
        workspace_backup.restore(recovered,{},safety,self.root/'recovered-config.json')
        self.assertEqual(len(recovered.memory('甲')),2)
        self.assertEqual(recovered_library.asset(self.current_identity)[1].read_bytes(),b'current-media')


if __name__=='__main__':unittest.main()
