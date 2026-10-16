import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request

from app import DEFAULT
from backup import clean_config
from configuration import load_config
from web_service import State,Server
import workspace_backup


class ConfigMigration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'config.json'

    def write(self,data):self.path.write_text(json.dumps(data,ensure_ascii=False),'utf-8')

    def test_old_tk_numeric_strings_open_real_state_endpoint(self):
        legacy=dict(DEFAULT,provider='lmstudio',model='qwen/qwen3.5-9b',base_url='http://127.0.0.1:1234/v1',context_size='8192',interval='3',prompt='保留我的中文要求',auto=True)
        self.write(legacy);original=self.path.read_bytes()
        state=State(self.root,Path(__file__).parent.parent/'web')
        server=Server(state);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/state',headers={'X-Studio-Token':state.token})
            with urllib.request.urlopen(request,timeout=5) as response:data=json.load(response)
            self.assertEqual(data['config']['context_size'],8192)
            self.assertEqual(data['config']['interval'],3)
            self.assertEqual(data['config']['prompt'],'保留我的中文要求')
            self.assertEqual(data['config_warnings'],[])
            self.assertFalse(state.config['auto'])
            self.assertEqual(self.path.read_bytes(),original)
        finally:server.shutdown();server.server_close();thread.join()

    def test_invalid_fields_do_not_prevent_access_to_records(self):
        for bad in (None,True,'oops',0,[],{},999999999):
            with self.subTest(value=bad):
                self.write(dict(context_size=bad,max_tokens='not a number',prompt='保留'))
                state=State(self.root,Path(__file__).parent.parent/'web')
                state.library.import_rows([dict(contact='甲',sender='我',content='已经导入的数据')],'fixture')
                snapshot=state.api('/api/state',{})
                self.assertEqual(snapshot['stats']['total'],1)
                self.assertEqual(snapshot['config']['context_size'],DEFAULT['context_size'])
                self.assertTrue(snapshot['config_warnings'])
                self.assertEqual(snapshot['config']['prompt'],'保留')
                state.api('/api/config',{'context_size':8192})
                self.assertEqual(state.api('/api/state',{})['config_warnings'],[])

    def test_bom_corrupt_and_nonobject_files_are_recoverable(self):
        self.path.write_text(json.dumps({'context_size':'16384','api_key':'FAKE_SECRET'}),'utf-8-sig')
        config,warnings=load_config(self.path,DEFAULT)
        self.assertEqual(config['context_size'],16384);self.assertNotIn('api_key',config)
        for raw in ('{broken','[]','null','"text"','123'):
            self.path.write_text(raw,'utf-8')
            config,warnings=load_config(self.path,DEFAULT)
            self.assertEqual(config['context_size'],DEFAULT['context_size']);self.assertTrue(warnings)
            self.assertEqual(self.path.read_text('utf-8'),raw)

    def test_legacy_backup_numbers_are_normalized_but_invalid_values_rejected(self):
        for cleaner in (clean_config,workspace_backup.clean_config):
            self.assertEqual(cleaner({'context_size':'4096','max_tokens':'768'})['max_tokens'],768)
            for invalid in ('4096.5','-1','1000000000',True,8192.5):
                with self.assertRaises(ValueError):cleaner({'context_size':invalid})

    def test_conflicting_output_budget_falls_back_without_losing_model(self):
        self.write(dict(context_size='2048',max_tokens='8192',model='local-model'))
        config,warnings=load_config(self.path,DEFAULT)
        self.assertEqual(config['model'],'local-model')
        self.assertLess(config['max_tokens'],config['context_size']-384)
        self.assertTrue(warnings)


if __name__=='__main__':unittest.main()
