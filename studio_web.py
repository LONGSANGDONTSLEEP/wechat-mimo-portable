"""Portable local web application launcher."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from web_service import State,Server,VERSION

ROOT=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent
ASSETS=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'web'


def open_window(url,root):
    edge=next((Path(os.environ.get(name,''))/'Microsoft'/'Edge'/'Application'/'msedge.exe' for name in ('ProgramFiles(x86)','ProgramFiles') if (Path(os.environ.get(name,''))/'Microsoft'/'Edge'/'Application'/'msedge.exe').is_file()),None)
    if edge:
        subprocess.Popen([str(edge),'--app='+url,'--user-data-dir='+str(root/'data'/'browser'),'--no-first-run','--window-size=1440,980'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    else:webbrowser.open(url)


def main():
    import vision
    vision.enable_dpi_awareness()
    root=ROOT
    if '--data-root' in sys.argv:root=Path(sys.argv[sys.argv.index('--data-root')+1]).resolve()
    (root/'data').mkdir(parents=True,exist_ok=True)
    lock_file=(root/'data'/'instance.lock').open('a+b')
    if os.name=='nt':
        import msvcrt
        try:
            lock_file.seek(0);lock_file.write(b'0');lock_file.flush();lock_file.seek(0);msvcrt.locking(lock_file.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            try:
                info=json.loads((root/'data'/'running.json').read_text('utf-8'))
                if info['url'].startswith('http://127.0.0.1:'):open_window(info['url'],root)
            except Exception:pass
            return
    state=State(root,ASSETS);server=Server(state);threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}/#session={state.token}'
    (root/'data'/'running.json').write_text(json.dumps({'url':url,'pid':os.getpid()}),'utf-8')
    import tkinter as tk
    from tkinter import filedialog,messagebox
    window=tk.Tk();window.withdraw()
    open_window(url,root)
    def pump():
        try:
            while not state.dialogs.empty():
                request=state.dialogs.get()
                try:request['value']=filedialog.askdirectory(parent=window,title='选择本机文件夹') if request['kind']=='folder' else filedialog.askopenfilename(parent=window,title='选择已解密数据库',filetypes=[('数据库','*.db *.sqlite *.sqlite3'),('所有文件','*.*')])
                finally:request['event'].set()
            if state.engine.running:state.engine.halted()
            if state.exit.is_set() or time.monotonic()-state.last_seen>180:
                state.engine.halt();server.shutdown();(root/'data'/'running.json').unlink(missing_ok=True);window.destroy();return
            window.after(100,pump)
        except Exception as exc:
            messagebox.showerror('工作台',str(exc),parent=window);window.after(500,pump)
    window.after(100,pump)
    try:window.mainloop()
    finally:
        state.engine.halt();server.server_close();lock_file.close();(root/'data'/'running.json').unlink(missing_ok=True)


if __name__=='__main__':
    if '--self-test' in sys.argv:
        from web_selftest import run
        run(ROOT,ASSETS)
    elif '--serve-test' in sys.argv:
        # Isolated synthetic workspace for browser/UI tests, never actual user records.
        from web_selftest import seed
        with tempfile.TemporaryDirectory(prefix='studio-web-preview-') as temporary:
            state=State(Path(temporary),ASSETS);seed(state)
            server=Server(state,int(os.environ.get('STUDIO_TEST_PORT','18765')))
            state.token='self-test-session'
            print(f'http://127.0.0.1:{server.server_port}/#session=self-test-session',flush=True)
            threading.Thread(target=lambda:(state.exit.wait(),server.shutdown()),daemon=True).start();server.serve_forever()
    else:main()
