from datetime import datetime,timedelta
from pathlib import Path
import tempfile
import unittest
from chatdata import ChatStore,normalize_rows
from toolbox import SHANGHAI,parse_time,bounds,select_messages,analyze,describe

class ToolboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=ChatStore(Path(self.tmp.name)/'records.sqlite3')
        self.now=datetime(2026,10,15,12,tzinfo=SHANGHAI)

    def put(self,when,content,contact='甲'):
        self.store.import_rows(normalize_rows([dict(contact=contact,time=when,sender='我',isSelf=True,content=content)]),'fixture')

    def test_recent_week_is_relative_to_now_never_first_or_latest_message(self):
        self.put('2020-01-01 12:00:00','旧消息')
        self.put('2026-10-08 11:59:59','边界外')
        self.put('2026-10-08 12:00:00','边界内')
        self.put('2026-10-15 12:00:00','现在')
        self.put('2026-10-16 12:00:00','未来')
        self.put('2026-10-14 12:00:00','另一联系人','乙')
        self.put('无日期','日期未知')
        result=select_messages(self.store,'甲','最近一周（7天）',now=self.now)
        self.assertEqual([r['content'] for r in result['rows']],['边界内','现在'])
        self.assertEqual(result['invalid'],1)
        self.assertIn('2026-10-08 12:00:00',describe(result))

    def test_empty_period_never_calls_model(self):
        self.put('2020-01-01','只有旧消息')
        selected=select_messages(self.store,'甲','最近一周（7天）',now=self.now)
        with self.assertRaisesRegex(ValueError,'没有消息'):
            analyze(selected,'对话摘要',{},'',lambda *a:self.fail('must not call model'))

    def test_seconds_milliseconds_and_utc_are_the_same_instant(self):
        expected=self.now
        for value in (str(expected.timestamp()),str(int(expected.timestamp()*1000)),'2026-10-15T04:00:00Z','2026/10/15 12:00:00','2026年10月15日 12:00:00'):
            self.assertEqual(parse_time(value),expected)
        self.assertIsNone(parse_time('garbage'))

    def test_custom_end_day_included_and_next_midnight_excluded(self):
        self.put('2026-10-15 23:59:59','结束当天')
        self.put('2026-10-16 00:00:00','隔天')
        result=select_messages(self.store,'甲','自定义日期','2026-10-15','2026-10-15',self.now)
        self.assertEqual([r['content'] for r in result['rows']],['结束当天'])
        with self.assertRaises(ValueError):bounds('自定义日期','2026-10-16','2026-10-15',self.now)

    def test_entire_corpus_and_analysis_batches_including_last_message(self):
        rows=[dict(contact='甲',sender='我',isSelf=True,time=(self.now-timedelta(minutes=i)).isoformat(),content=f'消息{i}，讨论周末安排') for i in range(605)]
        self.store.import_rows(normalize_rows(rows),'fixture')
        selected=select_messages(self.store,'甲','最近一周（7天）',now=self.now)
        self.assertEqual(len(selected['rows']),605)
        calls=[]
        def complete(cfg,key,text,history):
            calls.append(text); self.assertFalse(history); self.assertEqual(cfg['max_tokens'],1024)
            return '分段摘要'
        result=analyze(selected,'对话摘要',{},'',complete)
        text=''.join(calls)
        self.assertIn('消息604，讨论周末安排',text)
        self.assertIn('消息0，讨论周末安排',text)
        self.assertGreater(len(calls),2)
        self.assertIn('605 条文本消息',result)

    def test_cancel_stops_before_submitting(self):
        self.put('2026-10-15','消息')
        selected=select_messages(self.store,'甲','最近一周（7天）',now=self.now)
        with self.assertRaisesRegex(ValueError,'停止'):
            analyze(selected,'待办提取',{},'',lambda *a:self.fail(),cancelled=lambda:True)

if __name__=='__main__':unittest.main()
