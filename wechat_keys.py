"""Opt-in, read-only key discovery restricted to a selected local WeChat PID.

Keys are validated against selected database salts and returned only in memory.
No injection, memory writes, debug privilege changes, or credential logging.
"""
import ctypes
from functools import lru_cache
import ntpath
import re
import struct
from ctypes import wintypes
from localdb import verify_key,derive_key

WECHAT_NAMES=('weixin.exe','wechat.exe')
MAX_SCAN_BYTES=2*1024*1024*1024
MAX_KDF_ATTEMPTS=512
MAX_STRUCTURED_CANDIDATES=256


class ProcessAccessError(PermissionError):
    """A per-process read failure that callers may skip without hiding cancellation."""
    def __init__(self,pid,winerror,message):
        super().__init__(winerror,message)
        self.pid=pid;self.winerror=int(winerror or 0);self.message=message
    def __str__(self):return self.message


class ScanLimitError(ValueError):
    """The bounded scan is incomplete, and must not be reported as zero matches."""
    def __init__(self,pid,message=None):
        self.pid=pid
        super().__init__(message or f'PID {pid} 已达到2GB只读扫描预算（含复查）；尚未匹配的库不能自动读取')


def _msvc_string_pointers(data,pointer_size):
    """Locate 32-byte char strings using the public MSVC release ABI.

    basic_string<char> has a 16-byte storage union followed by size/capacity.
    A heap string can hold arbitrary bytes, so looking only for ASCII hex or
    salt references misses it. These are candidates, never trusted as keys.
    """
    if pointer_size not in (4,8):raise ValueError('不支持的指针宽度')
    integer='<I' if pointer_size==4 else '<Q';maximum=(1<<(pointer_size*8))-1
    for capacity in (47,63,32):
        marker=struct.pack(integer,32)+struct.pack(integer,capacity)
        offset=data.find(marker,16)
        while offset>=0:
            pointer=struct.unpack_from(integer,data,offset-16)[0]
            if 0x10000<pointer<maximum:yield pointer
            offset=data.find(marker,offset+1)


def _ordinary_text(data):
    """Reject ordinary string contents only from expensive derivation guesses.

    Direct page authentication still runs first, including for printable keys.
    Chinese UTF-8 strings are common heap contents but contain few ASCII bytes.
    """
    if sum(32<=value<=126 for value in data)>26:return True
    try:text=data.decode('utf-8')
    except UnicodeDecodeError:return False
    return len(text)>=8 and all(character.isprintable() or character.isspace() for character in text)


def _access_error(pid,code,stage='只读访问'):
    reasons={5:'Windows拒绝访问；可能是权限级别不同或进程受保护',6:'进程句柄已失效',
            87:'该PID无效或进程已退出',299:'内存页面在读取期间发生变化',
            50:'工具与微信的进程架构不兼容'}
    reason=reasons.get(code)
    if reason is None:reason='系统未返回详细原因' if not code else ctypes.FormatError(code).strip()
    return f'微信进程 PID {pid} 的{stage}失败（WinError {code}）：{reason}'


@lru_cache(maxsize=1)
def _kernel():
    # use_last_error preserves the failure from the exact WinAPI call. The
    # default ctypes.windll object does not maintain that thread-local copy.
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    declarations={
        'OpenProcess':([wintypes.DWORD,wintypes.BOOL,wintypes.DWORD],wintypes.HANDLE),
        'CloseHandle':([wintypes.HANDLE],wintypes.BOOL),
        'GetCurrentProcess':([],wintypes.HANDLE),
        'GetExitCodeProcess':([wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)],wintypes.BOOL),
        'QueryFullProcessImageNameW':([wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)],wintypes.BOOL),
        'VirtualQueryEx':([wintypes.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t],ctypes.c_size_t),
        'ReadProcessMemory':([wintypes.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_size_t)],wintypes.BOOL),
        'CreateToolhelp32Snapshot':([wintypes.DWORD,wintypes.DWORD],wintypes.HANDLE),
        'Process32FirstW':([wintypes.HANDLE,ctypes.c_void_p],wintypes.BOOL),
        'Process32NextW':([wintypes.HANDLE,ctypes.c_void_p],wintypes.BOOL),
    }
    for name,(args,result) in declarations.items():
        function=getattr(k,name);function.argtypes=args;function.restype=result
    if hasattr(k,'IsWow64Process2'):
        k.IsWow64Process2.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.WORD),ctypes.POINTER(wintypes.WORD)];k.IsWow64Process2.restype=wintypes.BOOL
    if hasattr(k,'IsWow64Process'):
        k.IsWow64Process.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)];k.IsWow64Process.restype=wintypes.BOOL
    return k


def _image_name(kernel,handle):
    buffer=ctypes.create_unicode_buffer(32768);size=wintypes.DWORD(len(buffer))
    if not kernel.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)):return ''
    return ntpath.basename(buffer.value).lower()


def _architecture(kernel,handle):
    if hasattr(kernel,'IsWow64Process2'):
        machine=wintypes.WORD();native=wintypes.WORD()
        if kernel.IsWow64Process2(handle,ctypes.byref(machine),ctypes.byref(native)):
            actual=machine.value or native.value;names={0x14c:('x86',32),0x1c4:('arm',32),0x8664:('x64',64),0xaa64:('arm64',64)}
            name,bits=names.get(actual,('unknown',None));return name,bits
    if hasattr(kernel,'IsWow64Process'):
        wow=wintypes.BOOL();caller_wow=wintypes.BOOL()
        if kernel.IsWow64Process(handle,ctypes.byref(wow)) and kernel.IsWow64Process(kernel.GetCurrentProcess(),ctypes.byref(caller_wow)):
            bits=32 if wow.value else 64 if caller_wow.value or ctypes.sizeof(ctypes.c_void_p)==8 else 32
            return ('x86' if bits==32 else 'x64'),bits
    return 'unknown',None


def _token_comparison(kernel,target):
    """Only boolean/level metadata leaves this helper; no SID or user name does."""
    result={'same_user':None,'caller_elevated':None,'target_elevated':None,
            'caller_integrity':None,'target_integrity':None,'token_error':0}
    adv=ctypes.WinDLL('advapi32',use_last_error=True)
    adv.OpenProcessToken.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE)];adv.OpenProcessToken.restype=wintypes.BOOL
    adv.GetTokenInformation.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)];adv.GetTokenInformation.restype=wintypes.BOOL
    adv.EqualSid.argtypes=[ctypes.c_void_p,ctypes.c_void_p];adv.EqualSid.restype=wintypes.BOOL
    adv.GetSidSubAuthorityCount.argtypes=[ctypes.c_void_p];adv.GetSidSubAuthorityCount.restype=ctypes.POINTER(ctypes.c_ubyte)
    adv.GetSidSubAuthority.argtypes=[ctypes.c_void_p,wintypes.DWORD];adv.GetSidSubAuthority.restype=ctypes.POINTER(wintypes.DWORD)
    class SidAttributes(ctypes.Structure):_fields_=[('sid',ctypes.c_void_p),('attributes',wintypes.DWORD)]
    tokens={};buffers={}
    def information(token,kind):
        size=wintypes.DWORD();adv.GetTokenInformation(token,kind,None,0,ctypes.byref(size))
        if not 0<size.value<=65536:result['token_error']=ctypes.get_last_error();return None
        buffer=ctypes.create_string_buffer(size.value)
        if adv.GetTokenInformation(token,kind,buffer,size.value,ctypes.byref(size)):return buffer
        result['token_error']=ctypes.get_last_error();return None
    try:
        for name,process in (('caller',kernel.GetCurrentProcess()),('target',target)):
            token=wintypes.HANDLE()
            if not adv.OpenProcessToken(process,0x0008,ctypes.byref(token)):
                result['token_error']=ctypes.get_last_error();continue
            tokens[name]=token
            elevation=information(token,20)
            if elevation:result[name+'_elevated']=bool(ctypes.cast(elevation,ctypes.POINTER(wintypes.DWORD)).contents.value)
            integrity=information(token,25)
            if integrity:
                sid=ctypes.cast(integrity,ctypes.POINTER(SidAttributes)).contents.sid;count=adv.GetSidSubAuthorityCount(sid)
                if count and count.contents.value:
                    level=adv.GetSidSubAuthority(sid,count.contents.value-1).contents.value
                    result[name+'_integrity']='system' if level>=0x4000 else 'high' if level>=0x3000 else 'medium' if level>=0x2000 else 'low'
            user=information(token,1)
            if user:buffers[name]=user
        if len(buffers)==2:
            left=ctypes.cast(buffers['caller'],ctypes.POINTER(SidAttributes)).contents.sid
            right=ctypes.cast(buffers['target'],ctypes.POINTER(SidAttributes)).contents.sid
            result['same_user']=bool(adv.EqualSid(left,right))
        return result
    finally:
        for token in tokens.values():kernel.CloseHandle(token)


def process_status(pid):
    """Inspect read permission and architecture without reading process memory."""
    row={'pid':None,'name':'','running':False,'is_wechat':False,'accessible':False,'access_error':'','winerror':0,
         'caller_bits':ctypes.sizeof(ctypes.c_void_p)*8,'target_bits':None,'architecture':'unknown',
         'same_user':None,'caller_elevated':None,'target_elevated':None,'caller_integrity':None,'target_integrity':None}
    try:pid=int(pid)
    except (TypeError,ValueError):pid=0
    row['pid']=pid
    if not 0<pid<=0xffffffff:
        row.update(winerror=87,access_error=_access_error(pid,87));return row
    kernel=_kernel();ctypes.set_last_error(0);query=kernel.OpenProcess(0x1000,False,pid)
    if not query:
        code=ctypes.get_last_error();row.update(winerror=code,access_error=_access_error(pid,code));return row
    try:
        row['name']=_image_name(kernel,query);name_error=ctypes.get_last_error() if not row['name'] else 0;row['is_wechat']=row['name'] in WECHAT_NAMES
        exit_code=wintypes.DWORD()
        exit_ok=kernel.GetExitCodeProcess(query,ctypes.byref(exit_code));exit_error=ctypes.get_last_error() if not exit_ok else 0
        row['running']=bool(exit_ok and exit_code.value==259)
        row['architecture'],row['target_bits']=_architecture(kernel,query);row.update(_token_comparison(kernel,query))
        if not row['running']:
            code=exit_error or 87;row.update(winerror=code,access_error=_access_error(pid,code,'查询进程运行状态'));return row
        if not row['name']:
            row.update(winerror=name_error,access_error=_access_error(pid,name_error,'查询进程名称'));return row
        if not row['is_wechat']:
            row.update(winerror=87,access_error=f'PID {pid} 不是正在运行的微信进程，未读取内存');return row
        if not row['target_bits'] or row['caller_bits']<row['target_bits']:
            row.update(winerror=50,access_error=f'PID {pid} 的进程架构无法由当前工具读取，请使用64位便携版（WinError 50）');return row
        if row['same_user'] is False:
            row.update(winerror=5,access_error=f'PID {pid} 不属于当前Windows用户，未读取内存（WinError 5）');return row
        ctypes.set_last_error(0);read=kernel.OpenProcess(0x410,False,pid)
        if read:
            kernel.CloseHandle(read);row['accessible']=True
        else:
            code=ctypes.get_last_error();row.update(winerror=code,access_error=_access_error(pid,code))
        return row
    finally:kernel.CloseHandle(query)


def related_processes(pid):
    class Process(ctypes.Structure):
        _fields_=[('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),('th32ProcessID',wintypes.DWORD),('th32DefaultHeapID',ctypes.c_size_t),('th32ModuleID',wintypes.DWORD),('cntThreads',wintypes.DWORD),('th32ParentProcessID',wintypes.DWORD),('pcPriClassBase',wintypes.LONG),('dwFlags',wintypes.DWORD),('szExeFile',wintypes.WCHAR*260)]
    k=_kernel()
    handle=k.CreateToolhelp32Snapshot(2,0);entry=Process();entry.dwSize=ctypes.sizeof(entry);rows=[]
    if handle in (None,0,ctypes.c_void_p(-1).value):
        code=ctypes.get_last_error();raise ProcessAccessError(0,code,_access_error(0,code,'列举微信进程'))
    try:
        more=k.Process32FirstW(handle,ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() in WECHAT_NAMES:rows.append((entry.th32ProcessID,entry.th32ParentProcessID))
            more=k.Process32NextW(handle,ctypes.byref(entry))
    finally:k.CloseHandle(handle)
    available={child for child,parent in rows}-{0}
    try:pid=int(pid)
    except (TypeError,ValueError):return []
    if not pid:return sorted(available)
    if pid not in available:return []
    # Keep scans within the selected WeChat process family; a separately
    # launched account is not implicitly authorized by this PID selection.
    family={pid};changed=True
    while changed:
        changed=False
        for child,parent in rows:
            if child in family and parent in available and parent not in family:family.add(parent);changed=True
            if parent in family and child not in family:family.add(child);changed=True
    return [pid]+sorted(family-{pid})


def find_keys(pid,pages,progress=lambda text:None,cancelled=lambda:False,version=4,diagnostics=None):
    def check_cancelled():
        if cancelled():raise ValueError('读取已停止；数据库密钥未写入磁盘')
    check_cancelled()
    if version not in (3,4):raise ValueError('仅支持SQLCipher 3/4加密配置')
    pages=list(pages)
    if any(not isinstance(page,bytes) or len(page)!=4096 for page in pages):raise ValueError('用于密钥校验的数据库首页不完整')
    if not pages:return {}
    try:pid=int(pid)
    except (TypeError,ValueError):raise ProcessAccessError(0,87,'请选择当前正在运行的微信进程（PID不能为空）') from None
    if pid not in related_processes(0):raise ProcessAccessError(pid,87,'请选择当前正在运行的微信进程；所选PID已失效')
    status=process_status(pid)
    if not status['accessible']:raise ProcessAccessError(pid,status['winerror'],status['access_error'])
    class Region(ctypes.Structure):
        _fields_=[('BaseAddress',ctypes.c_void_p),('AllocationBase',ctypes.c_void_p),('AllocationProtect',wintypes.DWORD)]+(
            [('PartitionId',wintypes.WORD)] if ctypes.sizeof(ctypes.c_void_p)==8 else [])+[
            ('RegionSize',ctypes.c_size_t),('State',wintypes.DWORD),('Protect',wintypes.DWORD),('Type',wintypes.DWORD)]
    kernel=_kernel()
    handle=kernel.OpenProcess(0x410,False,int(pid))
    if not handle:
        code=ctypes.get_last_error();raise ProcessAccessError(pid,code,_access_error(pid,code))
    expected={page[:16].hex():page for page in pages};found={};seen=set();address=0;total=0;fallback=[];regions=[];salt_locations={}
    structured=[];structured_seen=set();string_pointers=set();adjacent=[];adjacent_seen=set();kdf_results={}
    encoded=[];encoded_seen=set();direct_scan_complete=False;scan_complete=False
    pointer_size=status['target_bits']//8;pointer_format='<Q' if pointer_size==8 else '<I';maximum_address=(1<<status['target_bits'])-1
    budget=MAX_SCAN_BYTES;read_bytes=0;last_read_error=0
    def read_memory(location,count):
        nonlocal budget,read_bytes,last_read_error
        check_cancelled()
        if count>budget:raise ScanLimitError(pid)
        budget-=count;buffer=ctypes.create_string_buffer(count);read=ctypes.c_size_t()
        if not kernel.ReadProcessMemory(handle,ctypes.c_void_p(location),buffer,count,ctypes.byref(read)):last_read_error=ctypes.get_last_error()
        actual=min(read.value,count);read_bytes+=actual
        return buffer.raw[:actual]
    def derive_candidate(key,salt):
        check_cancelled();identity=(salt,key)
        if identity not in kdf_results:
            if len(kdf_results)>=MAX_KDF_ATTEMPTS:
                raise ScanLimitError(pid,f'PID {pid} 已达到{MAX_KDF_ATTEMPTS}次候选派生预算，尚未匹配的库不能自动读取')
            kdf_results[identity]=derive_key(key,expected[salt][:16],version)
        return kdf_results[identity]
    def direct_candidate(key,salts):
        if len(key)!=32 or len(set(key))<10:return False
        for salt in salts:
            check_cancelled()
            if salt not in found and verify_key(expected[salt],key,version):found[salt]=key
        return True
    pattern=re.compile(rb'(?<![0-9a-fA-F])([0-9a-fA-F]{96}|[0-9a-fA-F]{64})(?![0-9a-fA-F])')
    wide=re.compile(rb'(?:[0-9a-fA-F]\x00){64}(?:(?:[0-9a-fA-F]\x00){32})?')
    try:
        image=_image_name(kernel,handle)
        if not image:
            code=ctypes.get_last_error();raise ProcessAccessError(pid,code,_access_error(pid,code,'查询进程名称'))
        if image not in WECHAT_NAMES:raise ProcessAccessError(pid,87,f'PID {pid} 已不属于微信，未读取内存')
        while address<maximum_address and not cancelled():
            region=Region()
            if not kernel.VirtualQueryEx(handle,ctypes.c_void_p(address),ctypes.byref(region),ctypes.sizeof(region)):
                code=ctypes.get_last_error()
                if code not in (0,87):raise ProcessAccessError(pid,code,_access_error(pid,code,'枚举可读内存'))
                break
            base=int(region.BaseAddress or 0);size=int(region.RegionSize);address=base+size
            if not size or address<=base:break
            if region.State!=0x1000 or region.Protect&0x100 or (region.Protect&0xff) not in (2,4,8,0x20,0x40,0x80) or region.Type!=0x20000:continue
            regions.append((base,size))
            offset=0;tail=b''
            while offset<size and not cancelled():
                count=min(1024*1024,size-offset);block=read_memory(base+offset,count)
                data_address=base+offset-len(tail);data=tail+block;tail=data[-200:] if len(block)==count else b'';offset+=count;total+=len(block)
                for salt in expected:
                    raw_salt=bytes.fromhex(salt)
                    for salt_buffer in (raw_salt,bytes(value^0x3a for value in raw_salt)):
                        position=data.find(salt_buffer)
                        while position>=0:
                            salt_locations[data_address+position]=salt
                            if salt_buffer==raw_salt:
                                for key_start in (position-32,position+16):
                                    if key_start<0:continue
                                    key=data[key_start:key_start+32]
                                    if direct_candidate(key,[salt]) and salt not in found and (salt,key) not in adjacent_seen:
                                        adjacent_seen.add((salt,key))
                                        if len(adjacent)<96:adjacent.append((salt,key))
                            position=data.find(salt_buffer,position+1)
                # The raw 32-byte material may be in an unrelated heap string,
                # with no salt pointer anywhere near the string object.
                for pointer in _msvc_string_pointers(data,pointer_size):
                    check_cancelled()
                    if pointer in string_pointers:continue
                    string_pointers.add(pointer);key=read_memory(pointer,32)
                    if not direct_candidate(key,expected) or key in structured_seen:continue
                    if _ordinary_text(key):continue
                    structured_seen.add(key)
                    if len(structured)<MAX_STRUCTURED_CANDIDATES:structured.append(key)
                values=[m[1].decode().lower() for m in pattern.finditer(data)]
                values.extend(m[0].replace(b'\x00',b'').decode().lower() for m in wide.finditer(data))
                for value in values:
                    check_cancelled()
                    if value in seen:continue
                    seen.add(value);key=bytes.fromhex(value[:64])
                    if len(set(key))<10:continue
                    candidates=[value[64:]] if len(value)==96 and value[64:] in expected else list(expected) if len(value)==64 else []
                    for salt in candidates:
                        check_cancelled()
                        if salt in found:continue
                        page=expected[salt]
                        if verify_key(page,key,version):found[salt]=key
                        elif len(value)==96 and (salt,key) not in encoded_seen:
                            encoded_seen.add((salt,key))
                            if len(encoded)<96:encoded.append((salt,key))
                    if len(value)==64 and len(fallback)<256:fallback.append(key)
                if found.keys()>=expected.keys():return found
                if total//(32*1024*1024)!=(total-len(block))//(32*1024*1024):progress(f'PID {pid} 已检查 {total//(1024*1024)} MB 微信可读内存，匹配 {len(found)}/{len(expected)} 个数据库')
        # SQLCipher often retains binary buffers, not an SQL x'...' string.
        # Locate pointers to the selected salts, then test nearby key-buffer pointers.
        # Finish every direct authentication path before doing costly KDF work:
        # a heap full of unrelated strings must not starve this second pass.
        references={struct.pack(pointer_format,location):salt for location,salt in salt_locations.items() if location<=maximum_address}
        binary_candidates=[];binary_seen=set()
        if references:
            progress(f'找到{len(references)}处数据库盐值，校验只读SQLCipher缓冲区引用…')
            reference_pattern=re.compile(b'|'.join(re.escape(raw) for raw in references))
            for base,size in regions:
                offset=0;tail=b''
                while offset<size and not cancelled():
                    count=min(1024*1024,size-offset);block=read_memory(base+offset,count)
                    data=tail+block;tail=data[-512:] if len(block)==count else b'';offset+=count
                    for match in reference_pattern.finditer(data):
                        check_cancelled()
                        salt=references[match[0]]
                        if salt in found:continue
                        for position in range(max(0,match.start()-512)//pointer_size*pointer_size,min(len(data)-pointer_size+1,match.start()+520),pointer_size):
                            check_cancelled()
                            pointer=struct.unpack_from(pointer_format,data,position)[0]
                            if not 0x10000<pointer<maximum_address:continue
                            block=read_memory(pointer,384)
                            def check(key):
                                if len(key)!=32 or len(set(key))<12 or (salt,key) in binary_seen:return
                                binary_seen.add((salt,key))
                                for variant in (key,b''.join(key[i:i+4][::-1] for i in range(0,32,4)),key[16:]+key[:16]):
                                    if verify_key(expected[salt],variant,version):found[salt]=variant;return
                                if len(binary_candidates)<96:binary_candidates.append((salt,key))
                            for key_offset in range(0,len(block)-31,pointer_size):check(block[key_offset:key_offset+32])
                            for key_offset in range(0,len(block)-pointer_size+1,pointer_size):
                                check_cancelled()
                                second=struct.unpack_from(pointer_format,block,key_offset)[0]
                                if not 0x10000<second<maximum_address:continue
                                raw=read_memory(second,32)
                                if len(raw)==32:check(raw)
                            if salt in found:break
                    if found.keys()>=expected.keys():return found
        direct_scan_complete=True
        # Explicit key+salt strings and buffers beside a selected salt have a
        # stronger relationship than arbitrary 32-byte heap containers.
        for label,candidates in (('已关联盐值',encoded+adjacent+binary_candidates),
                                 ('32字节容器',[(salt,key) for key in structured for salt in expected]),
                                 ('十六进制',[(salt,key) for key in fallback for salt in expected])):
            for index,(salt,key) in enumerate(candidates):
                check_cancelled()
                if salt in found:continue
                if index%10==0:progress(f'校验{label}候选 {index+1}/{len(candidates)}，匹配 {len(found)}/{len(expected)} 个数据库')
                derived=derive_candidate(key,salt)
                if verify_key(expected[salt],derived,version):found[salt]=derived
                if found.keys()>=expected.keys():return found
        check_cancelled()
        exit_code=wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle,ctypes.byref(exit_code)) or exit_code.value!=259:
            raise ProcessAccessError(pid,87,f'PID {pid} 在读取过程中已退出，未将这次扫描视为完成（WinError 87）')
        if not read_bytes:raise ProcessAccessError(pid,last_read_error,f'PID {pid} 已取得只读句柄，但没有成功读取任何可用私有内存页；'+_access_error(pid,last_read_error,'读取内存页'))
        scan_complete=True
        progress(f'PID {pid} 扫描完成：读取 {read_bytes//(1024*1024)} MB，匹配 {len(found)}/{len(expected)} 个数据库')
        return found
    finally:
        if diagnostics is not None:
            diagnostics.update(pid=pid,requested_bytes=MAX_SCAN_BYTES-budget,read_bytes=read_bytes,
                               scan_byte_limit=MAX_SCAN_BYTES,kdf_attempts=len(kdf_results),kdf_limit=MAX_KDF_ATTEMPTS,
                               structured_candidates=len(structured),structured_discarded=max(0,len(structured_seen)-len(structured)),
                               adjacent_candidates=len(adjacent),encoded_candidates=len(encoded),salt_locations=len(salt_locations),
                               direct_scan_complete=direct_scan_complete,scan_complete=scan_complete,
                               matched=len(found),expected=len(expected))
        kernel.CloseHandle(handle)
