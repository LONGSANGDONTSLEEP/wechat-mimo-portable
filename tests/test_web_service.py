import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
from urllib.parse import quote
from web_service import State,Server

class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state=State(Path(self.tmp.name),Path(__file__).parent.parent/'web')
        self.server=Server(self.state);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(self.stop)

    def stop(self):self.server.shutdown();self.server.server_close();self.thread.join()

    def request(self,path,body=None,headers=None,auth=True,raw=False):
        headers=dict(headers or {})
        if auth:headers['X-Studio-Token']=self.state.token
        if body is not None and not isinstance(body,bytes):body=json.dumps(body).encode();headers['Content-Type']='application/json'
        req=urllib.request.Request(self.base+path,body,headers)
        with urllib.request.urlopen(req,timeout=5) as r:return (r.status,r.headers,r.read()) if raw else json.load(r)

    def wait(self,identity):
        deadline=time.monotonic()+5
        while self.state.jobs[identity]['state']=='running' and time.monotonic()<deadline:time.sleep(.02)
        return self.state.jobs[identity]

    def test_provider_switch_clears_previous_token(self):
        self.request('/api/config',{'key':'fake-cloud-token'})
        self.request('/api/config',{'provider':'lmstudio','base_url':'http://127.0.0.1:1234/v1','model':'test'})
        self.assertFalse(self.state.key)
        self.request('/api/config',{'key':'fake-local-token'})
        self.assertTrue(self.state.cfg()['local_auth'])
        self.request('/api/config',{'provider':'mimo'})
        self.assertFalse(self.state.key)
        self.assertNotIn('fake-local-token',(Path(self.tmp.name)/'config.json').read_text('utf-8'))

    def test_auth_origin_and_methods(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:self.request('/api/state',auth=False)
        self.assertEqual(exc.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError) as exc:self.request('/api/config',{},headers={'Origin':'https://example.invalid'})
        self.assertEqual(exc.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/config?provider=mimo')
        self.assertEqual(self.request('/api/state')['stats']['total'],0)

    def test_preview_discard_failed_upload_and_expired_confirmation(self):
        preview=self.request('/api/upload?name=test.json&preview=1',b'[{"content":"test","contact":"fixture"}]')
        file=self.state.imports[preview['import_id']][0]
        self.assertTrue(file.exists())
        self.assertTrue(self.request('/api/import-discard',{'import_id':preview['import_id']})['discarded'])
        self.assertFalse(file.exists())
        self.assertFalse(self.request('/api/import-discard',{'import_id':preview['import_id']})['discarded'])
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/import-confirm',{'import_id':preview['import_id']})
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/upload?name=broken.json',b'invalid json')
        self.assertFalse(list((Path(self.tmp.name)/'data'/'uploads').iterdir()))

    def test_model_address_change_clears_token_and_invalid_limits_do_not_save(self):
        self.request('/api/config',{'provider':'lmstudio','base_url':'http://localhost:1234/v1','key':'local-fixture'})
        self.request('/api/config',{'base_url':'http://localhost:1235/v1'})
        self.assertFalse(self.state.key)
        for value in (0,8193,True,128.5):
            with self.assertRaises(urllib.error.HTTPError):self.request('/api/config',{'max_tokens':value})
        self.request('/api/config',{'max_tokens':1024,'reasoning':'none'})
        self.assertEqual(self.request('/api/state')['config']['max_tokens'],1024)

    def test_missing_media_folder_and_ocr_window_fail_clearly(self):
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/folder-media',{'path':str(Path(self.tmp.name)/'missing')})
        with self.assertRaises(urllib.error.HTTPError) as result:self.request('/api/ocr-test',{})
        self.assertIn('请先选择微信窗口',result.exception.read().decode())

    def test_import_field_mapping_media_identity_and_reimport_dedup(self):
        raw=json.dumps([{'内容列':'你好','姓名':'我','会话列':'甲','发送':1,'日期':'2026-10-15'},{'内容列':'<msg/>','姓名':'我','会话列':'甲','发送':1,'日期':'2026-10-15','Type':3}],ensure_ascii=False).encode()
        for expected in (2,0):
            preview=self.request('/api/upload?name=test.json&preview=1',raw)
            result=self.request('/api/import-confirm',{'import_id':preview['import_id'],'mapping':{'text':'内容列','sender':'姓名','contact':'会话列','self':'发送','time':'日期'}})
            self.assertEqual(result['inserted'],expected)
        rows=self.request('/api/messages?contact='+quote('甲'))
        self.assertEqual(rows['total'],2);self.assertEqual({r['role'] for r in rows['rows']},{'self'})
        self.assertEqual(self.request('/api/state')['stats']['total'],1)

    def test_media_range_and_backup_include_bytes(self):
        result=self.request('/api/upload?name=clip.mp4&mode=media',b'0123456789')
        status,headers,data=self.request('/api/asset/'+result['id'],headers={'Range':'bytes=3-5'},raw=True)
        self.assertEqual(status,206);self.assertEqual(data,b'345');self.assertEqual(headers['Content-Range'],'bytes 3-5/10')
        status,headers,data=self.request('/api/asset/'+result['id'],headers={'Range':'bytes=-3'},raw=True)
        self.assertEqual(status,206);self.assertEqual(data,b'789');self.assertEqual(headers['Content-Range'],'bytes 7-9/10')
        task=self.request('/api/backup',{})['job'];entry=self.wait(task)
        self.assertEqual(entry['state'],'success')
        status,headers,data=self.request(entry['result']['download'],raw=True)
        self.assertGreater(len(data),100);self.assertEqual(status,200)

    def test_keys_not_in_config_or_state_and_model_error_is_visible(self):
        self.request('/api/config',{'provider':'lmstudio','base_url':'http://127.0.0.1:1234/v1','model':'fake-model','key':'FAKE_SECRET_KEY'})
        self.assertNotIn('FAKE_SECRET_KEY',(Path(self.tmp.name)/'config.json').read_text('utf-8'))
        self.assertNotIn('FAKE_SECRET_KEY',json.dumps(self.request('/api/state')))
        with patch('web_service.completion',side_effect=ValueError('模拟模型错误')):
            entry=self.wait(self.request('/api/reply',{'contact':'甲','text':'你好'})['job'])
            self.assertEqual(entry['state'],'error');self.assertIn('模拟模型错误',entry['error'])

    def test_local_plain_db_job_and_empty_analysis(self):
        import sqlite3
        from contextlib import closing
        path=Path(self.tmp.name)/'sample.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE MSG(StrTalker TEXT,IsSender INTEGER,CreateTime TEXT,StrContent TEXT,Type INTEGER)')
            db.execute("INSERT INTO MSG VALUES('甲',1,'2020-01-01','旧记录',1)");db.commit()
        entry=self.wait(self.request('/api/local-import',{'path':str(path)})['job'])
        self.assertEqual(entry['state'],'success');self.assertEqual(entry['result']['inserted'],1)
        with patch('web_service.completion') as call:
            entry=self.wait(self.request('/api/analysis',{'contact':'甲','range':'最近一周（7天）'})['job'])
            self.assertEqual(entry['state'],'error');call.assert_not_called()

    def test_plain_db_needs_no_pid_even_when_experimental_flag_is_left_on(self):
        import sqlite3
        path=Path(self.tmp.name)/'plain.db'
        db=sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE MSG(StrTalker TEXT,IsSender INTEGER,CreateTime TEXT,StrContent TEXT,Type INTEGER)')
            db.execute("INSERT INTO MSG VALUES('fixture',0,'2026-10-16','plain fixture',1)");db.commit()
        finally:db.close()
        with patch('wechat_keys.find_keys') as scan:
            entry=self.wait(self.request('/api/local-import',{'path':str(path),'auto_key':True,'pid':''})['job'])
            self.assertEqual(entry['state'],'success');scan.assert_not_called()

    def test_encrypted_import_rejects_empty_pid_and_keeps_other_processes_after_denial(self):
        import wechat_keys
        path=Path(self.tmp.name)/'message_0.db';path.write_bytes(b'not-sqlite'+bytes(4096-10))
        params={'path':str(path),'auto_key':True,'pid':''}
        with patch('wechat_keys.related_processes',return_value=[1,2]),patch('wechat_keys.find_keys') as scan:
            entry=self.wait(self.request('/api/local-import',params)['job'])
            self.assertEqual(entry['state'],'error');self.assertIn('请选择',entry['error']);scan.assert_not_called()
        denied=wechat_keys.ProcessAccessError(1,5,'WinError 5: access denied')
        with patch('wechat_keys.related_processes',return_value=[1,2]),patch('wechat_keys.find_keys',side_effect=[denied,{}]) as scan:
            entry=self.wait(self.request('/api/local-import',dict(params,pid=1))['job'])
            self.assertEqual(scan.call_count,2)
            self.assertEqual(entry['state'],'error');self.assertIn('当前微信版本的自动解密尚不可用',entry['error'])

    def test_encrypted_key_and_nonmessage_source_errors_are_actionable(self):
        path=Path(self.tmp.name)/'message_0.db';path.write_bytes(b'not-sqlite'+bytes(4096-10))
        for params,expected in (({'key':'xyz'},'64位'),({'pid':''},'已加密')):
            entry=self.wait(self.request('/api/local-import',dict(path=str(path),**params))['job'])
            self.assertIn(expected,entry['error'])
        import sqlite3
        config=Path(self.tmp.name)/'config.db';db=sqlite3.connect(config)
        try:db.execute('CREATE TABLE settings(name,value)');db.commit()
        finally:db.close()
        entry=self.wait(self.request('/api/local-import',{'path':str(config)})['job'])
        self.assertIn('可能是配置库',entry['error'])

    def test_downloads_from_same_second_do_not_overwrite_previous_exports(self):
        self.state.library.import_rows([{'contact':'first','sender':'x','role':'other','content':'first fixture'},
                                       {'contact':'second','sender':'y','role':'other','content':'second fixture'}],'fixture')
        with patch('web_service.time.strftime',return_value='fixed-second'):
            first=self.wait(self.request('/api/export',{'format':'json','contact':'first'})['job'])['result']
            second=self.wait(self.request('/api/export',{'format':'json','contact':'second'})['job'])['result']
            self.assertNotEqual(first['name'],second['name'])
            old=self.request(first['download'],raw=True)[2]
            self.assertIn(b'first fixture',old);self.assertNotIn(b'second fixture',old)
            backups=[self.wait(self.request('/api/backup',{})['job'])['result'] for _ in range(2)]
            self.assertNotEqual(backups[0]['name'],backups[1]['name'])

if __name__=='__main__':unittest.main()
