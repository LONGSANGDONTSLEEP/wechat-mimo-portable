from pathlib import Path
import tempfile
import unittest
from chatdata import ChatStore,normalize_rows,read_rows
from records import query,all_rows,export_file,statistics

class RecordsTests(unittest.TestCase):
    def test_search_pagination_export_round_trip_and_safe_html(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ChatStore(Path(directory)/'data.sqlite3')
            special='100% _ <script>alert(1)</script>\n下一行'
            store.import_rows(normalize_rows([dict(contact='甲',sender='我',isSelf=True,time='2026-10-14',content=special),dict(contact='乙',sender='对方',isSelf=False,time='bad',content='另一会话')]),'fixture')
            rows,total=query(store,keyword='% _')
            self.assertEqual(total,1);self.assertEqual(rows[0]['contact'],'甲')
            self.assertEqual(query(store,limit=1)[1],2)
            self.assertEqual(len(query(store,offset=1,limit=1)[0]),1)
            self.assertEqual(query(store,keyword="' OR 1=1 --")[1],0)
            for extension in ('json','csv','html'):
                path=Path(directory)/('export.'+extension)
                self.assertEqual(export_file(path,all_rows(store,'甲')),1)
                imported=normalize_rows(read_rows(path))
                self.assertEqual(imported[0]['text'],special)
                self.assertEqual(imported[0]['role'],'self')
                if extension=='html':self.assertNotIn('<script>alert(1)',path.read_text('utf-8'))
            stats=statistics(store)
            self.assertEqual(stats['total'],2);self.assertEqual(stats['invalid'],1)
            self.assertEqual(stats['days']['2026-10-14'],1)

if __name__=='__main__':unittest.main()
