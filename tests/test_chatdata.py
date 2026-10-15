import json
from pathlib import Path
import tempfile
import unittest
from chatdata import ChatStore,read_rows,normalize_rows,redact

class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name); self.store=ChatStore(self.root/'memory.sqlite3')
    def tearDown(self):self.temp.cleanup()

    def test_actual_wetrace_html_schema(self):
        data=[{'talkerName':'测试联系人','senderName':'本人称呼','isSelf':True,'type':1,'content':'行啊，等下聊','createTime':'2026-01-01'},
              {'talkerName':'测试联系人','senderName':'朋友称呼','isSelf':False,'type':1,'content':'晚上去哪儿','createTime':'2026-01-02'},
              {'talkerName':'测试联系人','isSelf':False,'type':3,'content':'[图片]'}]
        path=self.root/'index.html'; path.write_text('<script>window.CHAT_DATA = '+json.dumps(data)+'; forbiddenFunction();</script>','utf-8')
        rows=normalize_rows(read_rows(path))
        self.assertEqual(len(rows),2); self.assertEqual([r['role'] for r in rows],['self','other'])
        self.assertEqual(rows[0]['sender'],'本人称呼'); self.assertEqual(rows[0]['contact'],'测试联系人')
        self.assertEqual(rows[0]['time'],'2026-01-01')

    def test_csv_multiline_and_explicit_identity(self):
        path=self.root/'export.csv'; path.write_text('Talker,Sender,IsSender,Content\n朋友,我,1,"第一行\n第二行"\n朋友,对方,0,你好\n','utf-8-sig')
        rows=normalize_rows(read_rows(path)); self.assertEqual(rows[0]['text'],'第一行\n第二行')
        self.assertEqual([r['role'] for r in rows],['self','other'])

    def test_unknown_senders_never_train_persona(self):
        rows=normalize_rows([{'sender':'某人','content':'不是本人的口头禅'}],'联系人')
        self.store.import_rows(rows,'fixture'); self.assertEqual(self.store.style()['count'],0)
        rows=normalize_rows([{'sender':'本人名','content':'我的口头禅'}],'联系人',['本人名'])
        self.store.import_rows(rows,'fixture2'); self.assertEqual(self.store.style()['examples'],['我的口头禅'])

    def test_reimport_dedup_and_identity_upgrade(self):
        raw=[{'sender':'我','content':'测试消息','time':'2026-01-01'}]
        self.assertEqual(self.store.import_rows(normalize_rows(raw,'甲'),'file'),1)
        self.assertEqual(self.store.import_rows(normalize_rows(raw,'甲',['我']),'file'),0)
        self.assertEqual(self.store.stats()['self'],1)

    def test_contact_context_isolation(self):
        self.store.import_rows(normalize_rows([{'sender':'我','content':'明天开会','time':'1','IsSender':1}],'甲'),'a')
        self.store.import_rows(normalize_rows([{'sender':'对方','content':'乙的秘密事实','time':'2','IsSender':0}],'乙'),'b')
        self.store.add_memory('甲','周五一起吃饭'); self.store.add_memory('乙','乙的私密记忆')
        prompt=self.store.prompt('甲','明天怎么安排')
        self.assertIn('周五一起吃饭',prompt); self.assertNotIn('乙的私密记忆',prompt); self.assertNotIn('乙的秘密事实',prompt)

    def test_ai_outputs_do_not_become_own_style(self):
        self.store.remember_exchange('甲','你好','AI生成的回复')
        self.assertEqual(self.store.style()['count'],0)
        self.assertEqual(self.store.recent('甲')[-1]['content'],'AI生成的回复')
        self.assertEqual(self.store.recent('乙'),[])

    def test_redact_common_private_values(self):
        value=redact('电话13800138000 邮箱test@example.com key sk-abcdefghijklmnop')
        self.assertNotIn('13800138000',value); self.assertNotIn('test@example.com',value); self.assertNotIn('sk-abcdefghijklmnop',value)

    def test_custom_field_mapping(self):
        rows=normalize_rows([{'foo':'消息','person':'本人名'}],'甲',['本人名'],{'text':'foo','sender':'person'})
        self.assertEqual(rows[0]['role'],'self'); self.assertEqual(rows[0]['text'],'消息')

    def test_retrieve_old_record_beyond_recent_window(self):
        rows=[{'sender':'对方','content':'周六去骑车','time':'first','IsSender':0}]
        rows.extend({'sender':'对方','content':'无关聊天'+str(index),'time':str(index),'IsSender':0} for index in range(650))
        self.store.import_rows(normalize_rows(rows,'甲'),'fixture')
        self.assertTrue(any('周六去骑车' in item for item in self.store.context('甲','骑车去哪里')))

if __name__=='__main__':unittest.main()
