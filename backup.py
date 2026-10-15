"""Complete, verified personal backups. Never included in public releases."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile
from contextlib import closing

CONFIG_KEYS = {'provider','base_url','model','prompt','title','interval','context_size'}
TABLES = {'messages': {'id','contact','sender','role','ts','content','source','digest'},
          'settings': {'key','value'}, 'memories': {'id','contact','content','created'},
          'exchanges': {'id','contact','user','answer','created'}}


def clean_config(config):
    if not isinstance(config,dict):raise ValueError('备份中的模型配置应为对象')
    result = {k:v for k,v in config.items() if k in CONFIG_KEYS}
    if any(not isinstance(v,(str,int,float)) or isinstance(v,bool) for v in result.values()):
        raise ValueError('备份中的模型配置格式不正确')
    result['auto'] = False
    return result


def validate_database(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('备份数据库完整性校验失败')
        if db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()[0]:
            raise ValueError('备份数据库包含不支持的对象')
        for table,columns in TABLES.items():
            if {r[1] for r in db.execute(f'PRAGMA table_info({table})')} != columns:
                raise ValueError('备份缺少记录、记忆或配置表：'+table)
        return {table:db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in TABLES}


def create_backup(store, config, destination):
    destination=Path(destination)
    if destination.resolve()==store.path.resolve():raise ValueError('备份不能覆盖记录库')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.studio-backup-',dir=destination.parent) as temporary:
        folder=Path(temporary); snapshot=folder/'persona.sqlite3'
        with store.db() as source, closing(sqlite3.connect(snapshot)) as target:source.backup(target)
        if snapshot.stat().st_size>512*1024*1024:raise ValueError('完整备份当前支持512MB以内的记录库，请先按会话导出')
        counts=validate_database(snapshot)
        config_data=json.dumps(clean_config(config),ensure_ascii=False,indent=2).encode('utf-8')
        database=snapshot.read_bytes()
        manifest={'format':'wechat-studio-backup','version':1,'created':datetime.now().astimezone().isoformat(),
                  'counts':counts,'sha256':{name:hashlib.sha256(data).hexdigest() for name,data in [('persona.sqlite3',database),('config.json',config_data)]}}
        archive=folder/'complete.zip'
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr('persona.sqlite3',database); bundle.writestr('config.json',config_data)
            bundle.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
        os.replace(archive,destination)
    return counts


def unpack_checked(path, folder):
    try:
        with zipfile.ZipFile(path) as archive:
            if sorted(archive.namelist()) != ['config.json','manifest.json','persona.sqlite3']:
                raise ValueError('这不是完整备份文件，请选择“完整备份”生成的ZIP')
            for info in archive.infolist():
                maximum=512*1024*1024 if info.filename=='persona.sqlite3' else 1024*1024
                if info.file_size>maximum:raise ValueError('备份文件过大，当前恢复上限为512MB数据库')
                (folder/info.filename).write_bytes(archive.read(info))
        manifest=json.loads((folder/'manifest.json').read_text('utf-8'))
        if not isinstance(manifest,dict) or manifest.get('format')!='wechat-studio-backup' or manifest.get('version')!=1:
            raise ValueError('不支持的备份版本')
        for name in ('persona.sqlite3','config.json'):
            if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=manifest['sha256'][name]:
                raise ValueError('备份文件校验失败：'+name)
        config=clean_config(json.loads((folder/'config.json').read_text('utf-8')))
        counts=validate_database(folder/'persona.sqlite3')
        if counts!=manifest['counts']:raise ValueError('备份消息数量与清单不一致')
        return config,counts
    except (zipfile.BadZipFile,KeyError,TypeError,json.JSONDecodeError,sqlite3.DatabaseError) as exc:
        raise ValueError('备份损坏或格式不正确，尚未恢复') from exc


def restore_backup(store, current_config, archive_path, config_path):
    """Caller must stop all writers; validate first, save current state, then replace."""
    config_path=Path(config_path)
    with tempfile.TemporaryDirectory(prefix='.studio-restore-',dir=store.path.parent) as temporary:
        folder=Path(temporary); config,counts=unpack_checked(archive_path,folder)
        safety=store.path.parent/'backups'/('恢复前-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.zip')
        create_backup(store,current_config,safety)
        previous=config_path.read_bytes() if config_path.exists() else None
        (folder/'restored-config.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),'utf-8')
        os.replace(folder/'restored-config.json',config_path)
        try:os.replace(folder/'persona.sqlite3',store.path)
        except OSError:
            if previous is None:config_path.unlink(missing_ok=True)
            else:
                (folder/'old-config.json').write_bytes(previous); os.replace(folder/'old-config.json',config_path)
            raise
        return config,counts,safety
