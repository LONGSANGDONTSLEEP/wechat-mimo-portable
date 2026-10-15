import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import app
from chatdata import ChatStore,normalize_rows
from studio_ui import StudioUI
from toolbox import select_messages,analyze,SHANGHAI
from datetime import datetime

class AiFailures(unittest.TestCase):
    def call(self,response):
        cfg=dict(app.DEFAULT,provider='lmstudio',base_url='http://localhost:1234/v1')
        opener=SimpleNamespace(open=lambda *a,**k:io.BytesIO(json.dumps(response).encode()))
        with patch('urllib.request.build_opener',return_value=opener):return app.completion(cfg,'','你好',[])

    def test_truncation_and_reasoning_are_not_sent_as_replies(self):
        with self.assertRaisesRegex(ValueError,'截断'):self.call({'choices':[{'message':{'content':'一半的回复'},'finish_reason':'length'}]})
        with self.assertRaisesRegex(ValueError,'思考'):self.call({'choices':[{'message':{'content':None,'reasoning_content':'思考正文'},'finish_reason':'stop'}]})
        self.assertEqual(self.call({'choices':[{'message':{'content':'隐藏的思考</think>你好呀'},'finish_reason':'stop'}]}),'你好呀')
        with self.assertRaisesRegex(ValueError,'格式'):self.call({'error':'wrong endpoint'})

    def test_http_error_shows_service_context_error(self):
        error=urllib.error.HTTPError('https://example.invalid',400,'bad',{},io.BytesIO(b'{"error":{"message":"context length exceeded"}}'))
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaisesRegex(RuntimeError,'context length exceeded'):app.completion(app.DEFAULT,'fake','hi',[])

    def test_local_persona_and_history_stay_in_budget_keep_current_message(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ChatStore(Path(directory)/'records.sqlite3')
            store.import_rows(normalize_rows([dict(contact='甲',sender='我',isSelf=True,time=str(i),content='哈哈，这个安排挺好的'+str(i)) for i in range(20)]),'fixture')
            for i in range(10):store.add_memory('甲','已确认的记忆'+str(i)+'很长的说明'*90)
            store.add_memory('乙','不能出现在甲的资料里')
            ui=StudioUI.__new__(StudioUI);ui.store=store
            cfg=ui.reply_cfg(dict(app.DEFAULT,provider='lmstudio'),'甲','今晚吃什么？')
            history=[dict(role=role,content='很长的旧对话'*400) for _ in range(6) for role in ('user','assistant')]
            messages=app.prepare_messages(cfg,'今晚吃什么？',history)
            self.assertLessEqual(sum(len(m['content'].encode('utf-8'))+24 for m in messages),app.local_budget(cfg))
            self.assertEqual(messages[-1]['content'],'今晚吃什么？')
            self.assertNotIn('不能出现在甲的资料里',cfg['prompt'])
            with self.assertRaisesRegex(ValueError,'过长'):ui.reply_cfg(dict(app.DEFAULT,provider='lmstudio'),'甲','长消息'*2000)

    def test_local_toolbox_chunks_also_obey_request_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ChatStore(Path(directory)/'records.sqlite3')
            store.import_rows(normalize_rows([dict(contact='甲',sender='我',isSelf=True,time='2026-10-15',content='中文长消息。'*800)]),'fixture')
            selection=select_messages(store,'甲','最近一周（7天）',now=datetime(2026,10,15,12,tzinfo=SHANGHAI))
            calls=[]
            def complete(cfg,key,text,history):
                messages=app.prepare_messages(cfg,text,history)
                self.assertLessEqual(sum(len(m['content'].encode('utf-8'))+24 for m in messages),app.local_budget(cfg))
                calls.append(text); return '短摘要'
            result=analyze(selection,'对话摘要',dict(app.DEFAULT,provider='lmstudio'),'',complete)
            self.assertGreater(len(calls),2);self.assertIn('短摘要',result)

if __name__=='__main__':unittest.main()
