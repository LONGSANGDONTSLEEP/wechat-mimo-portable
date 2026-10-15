"""Opt-in, read-only key discovery restricted to a selected local WeChat PID.

Keys are validated against selected database salts and returned only in memory.
No injection, memory writes, debug privilege changes, or credential logging.
"""
import ctypes
import hashlib
import re
import struct
from ctypes import wintypes
from localdb import verify_key


def related_processes(pid):
    class Process(ctypes.Structure):
        _fields_=[('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),('th32ProcessID',wintypes.DWORD),('th32DefaultHeapID',ctypes.c_size_t),('th32ModuleID',wintypes.DWORD),('cntThreads',wintypes.DWORD),('th32ParentProcessID',wintypes.DWORD),('pcPriClassBase',wintypes.LONG),('dwFlags',wintypes.DWORD),('szExeFile',wintypes.WCHAR*260)]
    k=ctypes.windll.kernel32;k.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD];k.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    k.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Process)];k.Process32NextW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Process)];k.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=k.CreateToolhelp32Snapshot(2,0);entry=Process();entry.dwSize=ctypes.sizeof(entry);rows=[]
    try:
        more=k.Process32FirstW(handle,ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() in ('weixin.exe','wechat.exe'):rows.append((entry.th32ProcessID,entry.th32ParentProcessID))
            more=k.Process32NextW(handle,ctypes.byref(entry))
    finally:k.CloseHandle(handle)
    selected={int(pid)}
    changed=True
    while changed:
        changed=False
        for child,parent in rows:
            if parent in selected and child not in selected:selected.add(child);changed=True
            if child in selected and parent in {r[0] for r in rows} and parent not in selected:selected.add(parent);changed=True
    # Some WeChat database workers are started independently rather than as children.
    # Candidates are still limited to WeChat-named processes and validated salts.
    return sorted((selected|{child for child,parent in rows})-{0})


def find_keys(pid,pages,progress=lambda text:None,cancelled=lambda:False):
    class Region(ctypes.Structure):
        _fields_=[('BaseAddress',ctypes.c_void_p),('AllocationBase',ctypes.c_void_p),('AllocationProtect',wintypes.DWORD),
                  ('PartitionId',wintypes.WORD),('RegionSize',ctypes.c_size_t),('State',wintypes.DWORD),('Protect',wintypes.DWORD),('Type',wintypes.DWORD)]
    kernel=ctypes.windll.kernel32
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.VirtualQueryEx.argtypes=[wintypes.HANDLE,ctypes.c_void_p,ctypes.POINTER(Region),ctypes.c_size_t];kernel.VirtualQueryEx.restype=ctypes.c_size_t
    kernel.ReadProcessMemory.argtypes=[wintypes.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_size_t)]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x410,False,int(pid))
    if not handle:raise ValueError('无法只读访问所选微信进程，请确认属于当前用户且微信仍在运行')
    expected={page[:16].hex():page for page in pages};found={};seen=set();address=0;total=0;fallback=[];regions=[];salt_locations={}
    pattern=re.compile(rb'(?<![0-9a-fA-F])([0-9a-fA-F]{96}|[0-9a-fA-F]{64})(?![0-9a-fA-F])')
    wide=re.compile(rb'(?:[0-9a-fA-F]\x00){64}(?:(?:[0-9a-fA-F]\x00){32})?')
    try:
        while address<0x7fffffffffff and not cancelled():
            region=Region()
            if not kernel.VirtualQueryEx(handle,ctypes.c_void_p(address),ctypes.byref(region),ctypes.sizeof(region)):break
            base=int(region.BaseAddress or 0);size=int(region.RegionSize);address=base+size
            if not size:break
            if region.State!=0x1000 or region.Protect&0x101 or region.Type!=0x20000:continue
            regions.append((base,size))
            offset=0;tail=b''
            while offset<size and not cancelled():
                count=min(1024*1024,size-offset);buffer=ctypes.create_string_buffer(count);read=ctypes.c_size_t()
                kernel.ReadProcessMemory(handle,ctypes.c_void_p(base+offset),buffer,count,ctypes.byref(read))
                data_address=base+offset-len(tail);data=tail+buffer.raw[:read.value];tail=data[-200:];offset+=count;total+=read.value
                for salt in expected:
                    raw_salt=bytes.fromhex(salt)
                    for salt_buffer in (raw_salt,bytes(value^0x3a for value in raw_salt)):
                        position=data.find(salt_buffer)
                        while position>=0:
                            salt_locations[data_address+position]=salt;position=data.find(salt_buffer,position+1)
                values=[m[1].decode().lower() for m in pattern.finditer(data)]
                values.extend(m[0].replace(b'\x00',b'').decode().lower() for m in wide.finditer(data))
                for value in values:
                    if value in seen:continue
                    seen.add(value);key=bytes.fromhex(value[:64])
                    if len(set(key))<10:continue
                    candidates=[value[64:]] if len(value)==96 and value[64:] in expected else list(expected) if len(value)==64 else []
                    for salt in candidates:
                        if salt in found:continue
                        page=expected[salt]
                        if verify_key(page,key):found[salt]=key
                        elif len(value)==96:
                            derived=hashlib.pbkdf2_hmac('sha512',key,page[:16],256000,32)
                            if verify_key(page,derived):found[salt]=derived
                    if len(value)==64 and len(fallback)<256:fallback.append(key)
                if found.keys()>=expected.keys():return found
                if total//(32*1024*1024)!=(total-read.value)//(32*1024*1024):progress(f'已检查 {total//(1024*1024)} MB 微信可读内存，匹配 {len(found)}/{len(expected)} 个数据库')
                if total>2*1024*1024*1024:raise ValueError('已达到2GB扫描上限；此微信版本未找到可验证的密钥，可使用已有密钥或已解密库')
        # SQLCipher often retains binary buffers, not an SQL x'...' string.
        # Locate pointers to the selected salts, then test nearby key-buffer pointers.
        references={struct.pack('<Q',location):salt for location,salt in salt_locations.items()}
        binary_candidates=[];binary_seen=set()
        if references:
            progress(f'找到{len(references)}处数据库盐值，校验只读SQLCipher缓冲区引用…')
            reference_pattern=re.compile(b'|'.join(re.escape(raw) for raw in references))
            for base,size in regions:
                offset=0;tail=b''
                while offset<size and not cancelled():
                    count=min(1024*1024,size-offset);buffer=ctypes.create_string_buffer(count);read=ctypes.c_size_t()
                    kernel.ReadProcessMemory(handle,ctypes.c_void_p(base+offset),buffer,count,ctypes.byref(read))
                    data=tail+buffer.raw[:read.value];tail=data[-512:];offset+=count
                    for match in reference_pattern.finditer(data):
                        salt=references[match[0]]
                        if salt in found:continue
                        for position in range(max(0,match.start()-512)//8*8,min(len(data)-8,match.start()+520),8):
                            pointer=struct.unpack_from('<Q',data,position)[0]
                            if not 0x10000<pointer<0x7fffffffffff:continue
                            candidate=ctypes.create_string_buffer(384);got=ctypes.c_size_t()
                            kernel.ReadProcessMemory(handle,ctypes.c_void_p(pointer),candidate,384,ctypes.byref(got))
                            block=candidate.raw[:got.value]
                            def check(key):
                                if len(key)!=32 or len(set(key))<12 or (salt,key) in binary_seen:return
                                binary_seen.add((salt,key))
                                for variant in (key,b''.join(key[i:i+4][::-1] for i in range(0,32,4)),key[16:]+key[:16]):
                                    if verify_key(expected[salt],variant):found[salt]=variant;return
                                if len(binary_candidates)<96:binary_candidates.append((salt,key))
                            for key_offset in range(0,len(block)-31,8):check(block[key_offset:key_offset+32])
                            for key_offset in range(0,len(block)-7,8):
                                second=struct.unpack_from('<Q',block,key_offset)[0]
                                if not 0x10000<second<0x7fffffffffff:continue
                                raw=ctypes.create_string_buffer(32);got_key=ctypes.c_size_t()
                                kernel.ReadProcessMemory(handle,ctypes.c_void_p(second),raw,32,ctypes.byref(got_key))
                                if got_key.value==32:check(raw.raw)
                            if salt in found:break
                    if found.keys()>=expected.keys():return found
            for index,(salt,key) in enumerate(binary_candidates):
                if cancelled():break
                if salt in found:continue
                if index%10==0:progress(f'校验SQLCipher候选缓冲区 {index+1}/{len(binary_candidates)}')
                derived=hashlib.pbkdf2_hmac('sha512',key,expected[salt][:16],256000,32)
                if verify_key(expected[salt],derived):found[salt]=derived
        for index,key in enumerate(fallback):
            if cancelled():break
            if index%10==0:progress(f'校验候选密钥 {index+1}/{len(fallback)}，匹配 {len(found)}/{len(expected)} 个数据库')
            for salt,page in expected.items():
                if salt in found:continue
                derived=hashlib.pbkdf2_hmac('sha512',key,page[:16],256000,32)
                if verify_key(page,derived):found[salt]=derived
            if found.keys()>=expected.keys():break
        return found
    finally:kernel.CloseHandle(handle)
