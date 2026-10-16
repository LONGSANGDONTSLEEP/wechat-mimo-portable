"""User-initiated login-time capture; authenticate selected DBs before import."""
from pathlib import Path
import ctypes
from ctypes import wintypes
import importlib.util
import json
import multiprocessing
import os
import time
import localdb


ERRORS={
 'attach_denied':'Windows拒绝附加采集器，请检查工作台与微信是否由同一用户、以相同权限运行。',
 'process_exited':'所选微信进程已退出或更换，请刷新并重新选择；没有自动切换到其他账号。',
 'module_changed':'微信模块与预检结果不一致，请停止后重新检查当前微信版本。',
 'hook_failed':'当前微信版本的采集入口无法完整安装，尚未开始等待登录。',
 'candidate_limit':'候选数量达到本次采集上限，未将未验证的材料当成数据库密钥。',
 'agent_protocol':'采集子进程返回无效数据，采集已停止。',
 'agent_error':'采集模块运行失败，已停止采集；没有将内部数据写入错误日志。',
 'capture_failed':'采集器无法启动或连接所选微信进程，请重新检查环境。',
}


class CaptureSession:
    """An isolated, bounded helper. Production callers must provide target identity."""
    def __init__(self,pid,plan):self.pid=int(pid);self.plan=dict(plan);self.process=None;self.connection=None;self.closed=False
    def __enter__(self):
        from capture_worker import run_worker
        context=multiprocessing.get_context('spawn');parent,child=context.Pipe(duplex=True)
        self.connection=parent;self.process=context.Process(target=run_worker,args=(child,self.pid,self.plan),daemon=True)
        try:self.process.start()
        except Exception:parent.close();child.close();raise ValueError('无法启动本机采集辅助进程') from None
        child.close();return self
    def next_event(self,timeout=.2):
        if self.closed:return None
        try:available=self.connection.poll(timeout)
        except (EOFError,OSError):raise ValueError('采集辅助进程连接已关闭') from None
        if not available:
            if self.process.exitcode is not None:raise ValueError('采集辅助进程已退出，未取得完整结果')
            return None
        try:frame=self.connection.recv_bytes(65536)
        except (EOFError,OSError):raise ValueError('采集辅助进程连接已关闭') from None
        if len(frame)==33 and frame[:1]==b'K':return 'candidate',frame[1:]
        if frame[:1]!=b'J':raise ValueError(ERRORS['agent_protocol'])
        try:message=json.loads(frame[1:])
        except (ValueError,UnicodeError):raise ValueError(ERRORS['agent_protocol']) from None
        if not isinstance(message,dict):raise ValueError(ERRORS['agent_protocol'])
        kind=message.get('type')
        if kind=='ready' and type(message.get('entries')) is int and 0<message['entries']<=64:return kind,{'entries':message['entries']}
        if kind=='stopped':return kind,{}
        if kind=='error':return kind,message.get('code') if message.get('code') in ERRORS else 'capture_failed'
        raise ValueError(ERRORS['agent_protocol'])
    def close(self):
        if self.closed:return
        self.closed=True
        if not self.process:return
        try:
            if self.process.is_alive():
                try:self.connection.send_bytes(b'STOP')
                except (OSError,EOFError):pass
                # Drain while the helper detaches; never leave callbacks blocked on a full pipe.
                expires=time.monotonic()+4
                while self.process.is_alive() and time.monotonic()<expires:
                    try:
                        if self.connection.poll(.05):self.connection.recv_bytes(65536)
                    except (OSError,EOFError):break
                self.process.join(.1)
                if self.process.is_alive():self.process.terminate();self.process.join(3)
            else:self.process.join(.1)
        finally:
            self.connection.close()
            if not self.process.is_alive():self.process.close()
    def __exit__(self,*unused):self.close()


def process_identity(pid):
    from wechat_keys import process_status,_kernel,WECHAT_NAMES
    status=process_status(pid)
    if status.get('same_user') is not True:raise ValueError('无法确认微信与工作台属于同一用户，未准备采集')
    if not status.get('accessible'):raise ValueError(status.get('access_error') or '所选微信进程不可读取')
    if status.get('target_bits')!=64:raise ValueError('登录阶段采集目前只支持64位微信')
    kernel=_kernel();handle=kernel.OpenProcess(0x410,False,int(pid))
    if not handle:raise ValueError('微信进程已变化，无法确认采集目标')
    try:
        size=wintypes.DWORD(32768);buffer=ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)):raise ValueError('无法确认微信程序路径')
        image=Path(buffer.value).resolve()
        if image.name.lower() not in WECHAT_NAMES:raise ValueError('所选进程不属于微信')
        times=[wintypes.FILETIME() for _ in range(4)]
        kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4;kernel.GetProcessTimes.restype=wintypes.BOOL
        if not kernel.GetProcessTimes(handle,*[ctypes.byref(value) for value in times]):raise ValueError('无法确认微信进程创建时间')
        created=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
        return {'pid':int(pid),'image':str(image),'created':created}
    finally:kernel.CloseHandle(handle)


def ensure_same_process(pid,expected):
    current=process_identity(pid)
    if current['created']!=expected.get('created') or os.path.normcase(current['image'])!=os.path.normcase(expected.get('image','')):raise ValueError(ERRORS['process_exited'])
    return current


def wechat_module(pid):
    from wechat_keys import _kernel
    kernel=_kernel();handle=kernel.OpenProcess(0x410,False,int(pid))
    if not handle:raise ValueError('无法检查所选微信模块')
    try:
        api=ctypes.WinDLL('psapi',use_last_error=True)
        api.EnumProcessModulesEx.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.HMODULE),wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),wintypes.DWORD];api.EnumProcessModulesEx.restype=wintypes.BOOL
        api.GetModuleFileNameExW.argtypes=[wintypes.HANDLE,wintypes.HMODULE,wintypes.LPWSTR,wintypes.DWORD];api.GetModuleFileNameExW.restype=wintypes.DWORD
        modules=(wintypes.HMODULE*2048)();needed=wintypes.DWORD()
        if not api.EnumProcessModulesEx(handle,modules,ctypes.sizeof(modules),ctypes.byref(needed),3) or needed.value>ctypes.sizeof(modules):raise ValueError('无法完整检查微信模块，请选择有主窗口的微信进程')
        for module in modules[:needed.value//ctypes.sizeof(wintypes.HMODULE)]:
            path=ctypes.create_unicode_buffer(32768)
            if api.GetModuleFileNameExW(handle,module,path,len(path)) and Path(path.value).name.lower()=='weixin.dll':return Path(path.value).resolve()
        raise ValueError('所选进程尚未加载Weixin.dll，请选择微信主窗口进程并停在登录界面')
    finally:kernel.CloseHandle(handle)


def preflight(pid,root,version=4):
    if os.name!='nt':raise ValueError('登录阶段采集仅支持Windows')
    if version!=4:raise ValueError('登录阶段采集目前仅适配微信4格式')
    if importlib.util.find_spec('frida') is None:raise ValueError('此程序包未包含本机采集运行库，请使用包含采集模块的完整版本')
    identity=process_identity(pid);module=wechat_module(pid)
    # Restrict to the selected installation, including its version subdirectory.
    if not module.is_relative_to(Path(identity['image']).parent):raise ValueError('微信模块不属于所选安装目录，未准备采集')
    from capture_locator import resolve_image
    plan=resolve_image(module);plan['target']=identity
    ensure_same_process(pid,identity)
    files=selected_databases(root)
    encrypted=[entry for entry in files if entry['encrypted']]
    if not encrypted:raise ValueError('此来源没有加密消息库，可直接使用已解密数据库导入')
    return plan,encrypted


def selected_databases(root):
    import re
    root=Path(root);files=localdb.inventory(root)
    selected=[entry for entry in files if root.is_file() or entry['name'].lower() in ('contact.db','micromsg.db') or re.fullmatch(r'(?i)(?:biz_)?message_\d+\.db|msg\d*\.db',entry['name'])]
    return selected or files


def capture_keys(pid,plan,files,progress,cancelled,timeout=180,version=4):
    timeout=max(30,min(int(timeout),600));expected={};found={};seen=set();kdf_count=0
    for entry in files:
        with Path(entry['path']).open('rb') as stream:page=stream.read(4096)
        if len(page)!=4096:raise ValueError('数据库首页不完整，未启动采集')
        expected[page[:16].hex()]=page
    if not expected:raise ValueError('没有需要认证的加密数据库')
    created=time.monotonic();deadline=created+timeout;ready=False;last_match=None;last_progress=0
    progress('正在准备登录阶段采集器；请暂时不要扫码，等待就绪提示。')
    try:
        with CaptureSession(pid,plan) as capture:
            while time.monotonic()<deadline:
                if cancelled():raise ValueError('登录采集已停止，候选密钥未保存')
                if not ready and time.monotonic()-created>25:raise ValueError('采集器未能在25秒内就绪，请检查微信主进程或当前版本适配')
                event=capture.next_event(.15)
                if event:
                    kind,value=event
                    if kind=='ready':
                        ready=True;progress(f'采集已就绪（{value["entries"]}个入口），请在官方微信中手动登录；已登录时可打开相关聊天触发数据库访问。')
                    elif kind=='error':raise ValueError(ERRORS.get(value,ERRORS['capture_failed']))
                    elif kind=='stopped':raise ValueError('采集器已停止，尚未完成数据库验证')
                    elif kind=='candidate':
                        material=value
                        if not ready:raise ValueError(ERRORS['agent_protocol'])
                        if material in seen:continue
                        seen.add(material)
                        if len(seen)>1024:raise ValueError(ERRORS['candidate_limit'])
                        previous_count=len(found)
                        for salt,page in expected.items():
                            if salt not in found and localdb.verify_key(page,material,version):found[salt]=material
                        for salt,page in expected.items():
                            if salt in found:continue
                            if cancelled():raise ValueError('登录采集已停止，候选密钥未保存')
                            if time.monotonic()>=deadline:break
                            if kdf_count>=1024:raise ValueError('本次候选派生达到上限，未验证的数据库不算成功；请停止后重新采集')
                            kdf_count+=1;derived=localdb.derive_key(material,page[:16],version)
                            if localdb.verify_key(page,derived,version):found[salt]=derived
                        if len(found)>previous_count:
                            last_match=time.monotonic();progress(f'已认证 {len(found)}/{len(expected)} 个数据库密钥，正在等待其他库；不显示或保存密钥。')
                        if len(found)==len(expected):break
                if last_match is not None and time.monotonic()-last_match>10:break
                if ready and time.monotonic()-last_progress>5:
                    ensure_same_process(pid,plan['target']);last_progress=time.monotonic()
            progress('正在停止采集并解除观察，随后读取数据库只读快照。')
        if cancelled():raise ValueError('登录采集已停止，候选密钥未保存')
        if not found:raise ValueError('采集结束但没有密钥通过所选账号数据库认证。登录完成不等于取钥成功；请检查账号、登录时机与当前微信版本。')
        return dict(found)
    except Exception:
        found.clear();raise
    finally:seen.clear()
