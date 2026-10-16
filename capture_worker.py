"""Isolated local Frida session. No key files, console output or network server.

HMAC-pad observation is adapted from freanocank/wechat-decrypt (MIT), commit
f0b3f9f95dda2d954ee3dbe1d139fb1944a5ecfd. See THIRD_PARTY_LICENSES.
Addresses come from our PE/unwind locator, not the upstream CC heuristic.
"""
import json
import queue
import time


def agent_source(plan):
    config={key:plan[key] for key in ('module_name','module_path','image_size','entry_rvas','entry_signatures')}
    return 'const cfg='+json.dumps(config)+';\n'+r'''
'use strict';
const installed=[], seen=new Set();
let observer=null, attempted=false, stopped=false, armed=false;
const pending=[];
function cleanPath(path) {return path.replace(/\//g,'\\').toLowerCase();}
function announce(type, extra) {send(Object.assign({type:type},extra||{}));}
function stop() {
  if (stopped) return;
  stopped=true;
  if(observer) {observer.detach();observer=null;}
  for(const hook of installed) {try {hook.detach();} catch (_) {}}
  installed.length=0;seen.clear();pending.length=0;armed=false;Interceptor.flush();
}
function install(module) {
  if(attempted||stopped||module.name.toLowerCase()!==cfg.module_name.toLowerCase()) return;
  attempted=true;
  if(Process.arch!=='x64'||module.size!==cfg.image_size||cleanPath(module.path)!==cleanPath(cfg.module_path)) {
    announce('error',{code:'module_changed'});return;
  }
  try {
    // Validate every entry before modifying any of them.
    for(const rva of cfg.entry_rvas) {
      const expected=cfg.entry_signatures[String(rva)];
      if(typeof expected!=='string'||!/^[a-f0-9]{64}$/.test(expected)) throw Error('signature');
      const bytes=new Uint8Array(module.base.add(rva).readByteArray(32));
      let actual='';for(const byte of bytes) actual+=byte.toString(16).padStart(2,'0');
      if(actual!==expected) {announce('error',{code:'module_changed'});stop();return;}
    }
    for(const rva of cfg.entry_rvas) {
      if(!Number.isSafeInteger(rva)||rva<0||rva>=module.size) throw Error('entry');
      const address=module.base.add(rva),range=Process.findRangeByAddress(address);
      if(!range||range.protection.indexOf('x')<0) throw Error('protection');
      installed.push(Interceptor.attach(address,{onEnter:function(args) {
        if(stopped) return;
        try {
          if(args[2].isNull()) return;
          const block=new Uint8Array(args[1].readByteArray(128));
          for(let i=32;i<128;i++) if(block[i]!==0x36) return;
          const key=new Uint8Array(32);let fingerprint='';
          for(let i=0;i<32;i++) {key[i]=block[i]^0x36;fingerprint+=key[i].toString(16).padStart(2,'0');}
          if(seen.has(fingerprint)) {key.fill(0);return;}
          if(seen.size>=1024) {key.fill(0);announce('error',{code:'candidate_limit'});stop();return;}
          seen.add(fingerprint);
          if(armed) {send({type:'candidate'},key.buffer);key.fill(0);} else pending.push(key);
        } catch (_) { /* an unrelated invocation is not a database candidate */ }
      }}));
    }
    if(!installed.length) throw Error('empty');
    Interceptor.flush();announce('ready',{entries:installed.length});armed=true;
    for(const key of pending) {send({type:'candidate'},key.buffer);key.fill(0);}pending.length=0;
  } catch (_) {stop();announce('error',{code:'hook_failed'});}
}
rpc.exports={stop:stop};
observer=Process.attachModuleObserver({onAdded:install});
if(stopped&&observer) {observer.detach();observer=null;}
'''


def run_worker(connection,pid,plan):
    """Only a parent-owned pipe carries 32-byte candidate values, in memory."""
    session=None;script=None;events=queue.Queue(maxsize=1100);detached=False
    def event(kind,**fields):
        try:events.put_nowait(('json',dict(type=kind,**fields)))
        except queue.Full:pass
    def message(message,data):
        if message.get('type')!='send':event('error',code='agent_error');return
        payload=message.get('payload')
        if not isinstance(payload,dict):event('error',code='agent_protocol');return
        kind=payload.get('type')
        if kind=='candidate':
            if not isinstance(data,bytes) or len(data)!=32:event('error',code='agent_protocol');return
            try:events.put_nowait(('candidate',data))
            except queue.Full:event('error',code='candidate_limit')
        elif kind=='ready':event('ready',entries=int(payload.get('entries',0)))
        elif kind=='error':event('error',code=str(payload.get('code','agent_error')) if payload.get('code') in ('module_changed','hook_failed','candidate_limit') else 'agent_error')
    def on_detached(*unused):
        nonlocal detached
        detached=True
    try:
        # PID reuse is checked again in the helper immediately before attach.
        if plan.get('target'):
            from login_capture import ensure_same_process
            ensure_same_process(pid,plan['target'])
        from pathlib import Path
        import hashlib
        with Path(plan['module_path']).open('rb') as source:
            digest=hashlib.file_digest(source,'sha256').hexdigest()
        if digest!=plan.get('sha256'):
            event('error',code='module_changed')
            connection.send_bytes(b'J'+json.dumps({'type':'error','code':'module_changed'}).encode());return
        import frida
        device=frida.get_local_device()
        session=device.attach(int(pid));session.on('detached',on_detached)
        if plan.get('target'):
            from login_capture import ensure_same_process
            ensure_same_process(pid,plan['target'])
        script=session.create_script(agent_source(plan));script.on('message',message);script.load()
        expires=time.monotonic()+660
        while time.monotonic()<expires:
            if connection.poll(.05):
                if connection.recv_bytes(16)==b'STOP':break
            if detached:event('error',code='process_exited');break
            for _ in range(32):
                try:kind,value=events.get_nowait()
                except queue.Empty:break
                if kind=='candidate':connection.send_bytes(b'K'+value)
                else:connection.send_bytes(b'J'+json.dumps(value).encode('utf-8'))
        if detached:
            connection.send_bytes(b'J'+json.dumps({'type':'error','code':'process_exited'}).encode())
    except (EOFError,BrokenPipeError,OSError):pass
    except Exception as error:
        # Frida/JS exception strings can contain captured values. Never forward them.
        code='attach_denied' if type(error).__name__=='PermissionDeniedError' else 'process_exited' if type(error).__name__=='ProcessNotFoundError' else 'capture_failed'
        try:connection.send_bytes(b'J'+json.dumps({'type':'error','code':code}).encode())
        except (OSError,EOFError):pass
    finally:
        if script:
            try:script.unload()
            except Exception:pass
        if session:
            try:session.detach()
            except Exception:pass
        try:connection.send_bytes(b'J'+b'{"type":"stopped"}')
        except (OSError,EOFError):pass
        connection.close()
