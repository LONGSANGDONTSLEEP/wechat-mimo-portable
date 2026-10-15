import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import app
from chatdata import ChatStore,normalize_rows
from studio_ui import StudioUI

class PersonaPipeline(unittest.TestCase):
    def test_import_to_model_and_contact_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ChatStore(Path(directory)/'memory.sqlite3')
            store.import_rows(normalize_rows([{'talkerName':'甲','senderName':'我','isSelf':True,'content':'哈哈，先聊聊你的想法','type':1}]),'fixture')
            store.add_memory('甲','甲喜欢周六骑车'); store.add_memory('乙','乙的秘密记忆')
            ui=StudioUI.__new__(StudioUI); ui.store=store
            cfg=dict(app.DEFAULT,persona_enabled=True,longer=True,proactive=True)
            generated=ui.reply_cfg(cfg,'甲','周末做什么')
            response=io.BytesIO(json.dumps({'choices':[{'message':{'content':'周六要不要一起骑车？'}}]}).encode())
            with patch('urllib.request.urlopen',return_value=response) as call:
                app.completion(generated,'sk-fake-test','周末做什么',[])
            system=json.loads(call.call_args.args[0].data)['messages'][0]['content']
            self.assertIn('哈哈，先聊聊你的想法',system)
            self.assertIn('甲喜欢周六骑车',system)
            self.assertNotIn('乙的秘密记忆',system)
            self.assertIn('2至5句',system)

    def test_disable_persona_does_not_send_style_examples(self):
        ui=StudioUI.__new__(StudioUI)
        cfg=dict(app.DEFAULT,persona_enabled=False,proactive=False)
        self.assertEqual(ui.reply_cfg(cfg,'甲','你好')['prompt'],cfg['prompt'])

    def test_retrieved_history_preserves_the_message_date(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ChatStore(Path(directory)/'memory.sqlite3')
            store.import_rows(normalize_rows([dict(contact='甲',sender='我',isSelf=True,time='2020-01-02',content='明天下午去公园骑车吧')]),'fixture')
            context=store.context('甲','去公园骑车吗')
            self.assertEqual(len(context),1)
            self.assertIn('2020-01-02',context[0])
            self.assertIn('明天下午',context[0])

if __name__=='__main__':unittest.main()
