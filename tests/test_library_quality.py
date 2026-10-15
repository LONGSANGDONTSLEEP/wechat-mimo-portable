import hashlib
from pathlib import Path
import tempfile
import unittest

from chatdata import ChatStore,read_rows
from library import Library
from records import export_file


class LibraryQuality(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.library=Library(ChatStore(self.root/'records.sqlite3'))

    def test_mixed_times_sort_in_beijing_timezone(self):
        self.library.import_rows([
            dict(contact='甲',sender='我',time='2026-10-15 08:00:00',content='较早'),
            dict(contact='甲',sender='我',time='2026-10-15T00:30:00Z',content='较晚'),
            dict(contact='甲',sender='我',time='1792024200000',content='毫秒时间'),
        ],'sample')
        rows,_=self.library.rows()
        early=next(i for i,r in enumerate(rows) if r['content']=='较早')
        late=next(i for i,r in enumerate(rows) if r['content']=='较晚')
        self.assertLess(late,early)

    def test_media_links_by_content_hash_even_after_asset_import(self):
        data=b'synthetic image fixture'
        md5=hashlib.md5(data).hexdigest()
        self.library.import_rows([dict(contact='甲',sender='乙',time='2026-10-15',type=3,content=f'<img md5="{md5.upper()}"/>')],'sample')
        self.assertEqual(self.library.rows()[0][0]['assets'],[])
        image=self.root/'sample.png';image.write_bytes(data)
        identity=self.library.add_asset(image)
        rows,_=self.library.rows()
        self.assertEqual(rows[0]['assets'][0]['id'],identity)
        self.assertEqual(self.library.store.stats()['total'],0)
        self.assertEqual(self.library.add_asset(image,'renamed.jpg'),identity)
        self.assertEqual(len(list(self.library.root.iterdir())),1)

    def test_media_export_round_trip_and_identical_source_messages(self):
        rows=[dict(contact='甲',sender='我',isSelf=True,time='2026-10-15',type=3,content='<img/>',source_id=i,table='Msg_fixture') for i in (1,2)]
        self.assertEqual(self.library.import_rows(rows,'same-db'),2)
        self.assertEqual(self.library.import_rows(rows,'same-db'),0)
        exported,_=self.library.rows()
        for suffix in ('csv','html'):
            file=self.root/('export.'+suffix);export_file(file,exported[:1])
            destination=Library(ChatStore(self.root/(suffix+'.sqlite3')))
            destination.import_rows(read_rows(file),'roundtrip')
            result,total=destination.rows()
            self.assertEqual(total,1)
            self.assertEqual(result[0]['kind'],3)
            self.assertEqual(result[0]['role'],'self')
            self.assertEqual(destination.store.stats()['total'],0)


if __name__=='__main__':unittest.main()
