"""Portable local web application launcher."""
import multiprocessing
# PyInstaller worker dispatch must happen before importing the application or UI.
if __name__=='__main__':multiprocessing.freeze_support()
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from urllib.parse import urlsplit,parse_qs
from web_service import State,Server,VERSION

ROOT=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent
ASSETS=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'web'


def instance_url(info):
    """Only reopen the loopback session URL written by this launcher."""
    if not isinstance(info,dict) or not isinstance(info.get('url'),str):return None
    try:
        parsed=urlsplit(info['url']);token=parse_qs(parsed.fragment).get('session',[])
        if parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and parsed.port and not parsed.username and not parsed.password and parsed.path=='/' and not parsed.query and len(token)==1 and token[0]:return info['url']
    except ValueError:pass
    return None


def active_jobs(state):
    with state.job_lock:return any(job.get('state') in ('running','pending') for job in state.jobs.values())


def should_exit(state,now=None):
    active=active_jobs(state)
    if state.exit.is_set():
        # Complete atomic backup/restore operations before releasing the workspace.
        state.cancel.set();state.engine.halt()
        return not active and not state.engine.send_lock.locked()
    return not active and not state.engine.send_lock.locked() and (time.monotonic() if now is None else now)-state.last_seen>180


def cleanup_instance(path,pid):
    """Never remove another process's metadata after a failed launch."""
    try:
        if json.loads(path.read_text('utf-8')).get('pid')==pid:path.unlink(missing_ok=True)
    except (ValueError,OSError,AttributeError):pass


def open_window(url,root):
    edge=next((Path(os.environ.get(name,''))/'Microsoft'/'Edge'/'Application'/'msedge.exe' for name in ('ProgramFiles(x86)','ProgramFiles') if (Path(os.environ.get(name,''))/'Microsoft'/'Edge'/'Application'/'msedge.exe').is_file()),None)
    if edge:
        subprocess.Popen([str(edge),'--app='+url,'--user-data-dir='+str(root/'data'/'browser'),'--no-first-run','--window-size=1440,980'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    else:webbrowser.open(url)


def main():
    import vision
    vision.enable_dpi_awareness()
    root=ROOT
    if '--data-root' in sys.argv:
        index=sys.argv.index('--data-root')+1
        if index>=len(sys.argv):raise ValueError('--data-root 缺少数据目录')
        root=Path(sys.argv[index]).expanduser().resolve()
    (root/'data').mkdir(parents=True,exist_ok=True)
    lock_file=(root/'data'/'instance.lock').open('a+b')
    if os.name=='nt':
        import msvcrt
        try:
            lock_file.seek(0);lock_file.write(b'0');lock_file.flush();lock_file.seek(0);msvcrt.locking(lock_file.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            try:
                info=json.loads((root/'data'/'running.json').read_text('utf-8'))
                url=instance_url(info)
                if url:open_window(url,root)
            except Exception:pass
            return
    state=None;server=None;server_thread=None;window=None;metadata=root/'data'/'running.json'
    try:
        state=State(root,ASSETS);server=Server(state)
        server_thread=threading.Thread(target=server.serve_forever,daemon=True);server_thread.start()
        url=f'http://127.0.0.1:{server.server_port}/#session={state.token}'
        metadata.write_text(json.dumps({'url':url,'pid':os.getpid()}),'utf-8')
        import tkinter as tk
        from tkinter import filedialog,messagebox
        window=tk.Tk();window.withdraw()
        open_window(url,root)
        def pump():
            try:
                while not state.dialogs.empty():
                    request=state.dialogs.get()
                    try:
                        request['value']='' if state.exit.is_set() else filedialog.askdirectory(parent=window,title='选择本机文件夹') if request['kind']=='folder' else filedialog.askopenfilename(parent=window,title='选择已解密数据库',filetypes=[('数据库','*.db *.sqlite *.sqlite3'),('所有文件','*.*')])
                    finally:request['event'].set()
                if state.engine.running:state.engine.halted()
                if should_exit(state):
                    state.engine.halt();window.quit();return
                window.after(100,pump)
            except Exception as exc:
                messagebox.showerror('工作台',str(exc),parent=window);window.after(500,pump)
        window.after(100,pump)
        window.mainloop()
    finally:
        if state:
            state.exit.set();state.cancel.set();state.engine.halt()
            while not state.dialogs.empty():
                request=state.dialogs.get();request['value']='';request['event'].set()
        if server:
            if server_thread and server_thread.is_alive():server.shutdown();server_thread.join(timeout=2)
            server.server_close()
        if state:
            # Request handlers are stopped; finish mutations that were already accepted.
            while active_jobs(state) or state.engine.send_lock.locked():time.sleep(.05)
            state.clear_database_keys()
        if window:
            try:window.destroy()
            except Exception:pass
        cleanup_instance(metadata,os.getpid());lock_file.close()


if __name__=='__main__':
    if '--capture-self-test' in sys.argv:
        from capture_acceptance import run
        raise SystemExit(0 if run(ROOT)['ok'] else 1)
    elif '--self-test' in sys.argv:
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
