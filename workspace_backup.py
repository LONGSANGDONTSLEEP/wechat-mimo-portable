"""Portable workspace archives, including locally managed media."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
import re
import backup

MEDIA_NAME=re.compile(r'[0-9a-f]{64}\.[a-z0-9]{1,6}')
MAX_WORKSPACE_BYTES=8*1024**3
MAX_MANIFEST_BYTES=8*1024**2


def clean_config(config):
    result=backup.clean_config(config)
    if 'max_tokens' in config:
        value=result['max_tokens']
        if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=131072:raise ValueError('备份中的回复长度设置不正确')
        result['max_tokens']=value
    if 'reasoning' in config:
        if config['reasoning'] not in ('default','none','low','medium','high'):raise ValueError('备份中的思考模式设置不正确')
        result['reasoning']=config['reasoning']
    return result


def asset_records(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='assets'").fetchone():return []
    columns={row[1] for row in db.execute('PRAGMA table_info(assets)')}
    if not {'id','filename','size'}<=columns:raise ValueError('媒体登记表不完整')
    records=[]
    md5_column='md5' if 'md5' in columns else 'NULL'
    for identity,name,size,md5 in db.execute('SELECT id,filename,size,'+md5_column+' FROM assets'):
        if not isinstance(name,str) or not MEDIA_NAME.fullmatch(name):raise ValueError('媒体文件名异常')
        if identity!=name[:64]:raise ValueError('媒体登记ID与文件名不一致')
        if not isinstance(size,int) or isinstance(size,bool) or size<0:raise ValueError('媒体登记大小异常')
        if md5 is not None and (not isinstance(md5,str) or not re.fullmatch(r'[0-9a-f]{32}',md5)):raise ValueError('媒体登记MD5异常')
        records.append((name,identity,size,md5))
    return records


def check_asset(path,record):
    name,identity,size,registered_md5=record
    if not path.is_file():raise ValueError('备份缺少媒体文件：'+name)
    sha=hashlib.sha256();md5=hashlib.md5();actual_size=0
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            actual_size+=len(block);sha.update(block);md5.update(block)
    if actual_size!=size or sha.hexdigest()!=identity or (registered_md5 is not None and md5.hexdigest()!=registered_md5):
        raise ValueError('媒体内容与登记的校验值或大小不一致，请从原文件重新导入：'+name)


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def create(store,config,destination):
    destination=Path(destination)
    resolved=destination.resolve();data_root=store.path.parent.resolve()
    if resolved==store.path.resolve() or resolved==data_root.parent/'config.json' or resolved.is_relative_to(data_root/'media'):
        raise ValueError('备份不能覆盖工作库、配置或媒体文件')
    destination.parent.mkdir(parents=True,exist_ok=True)
    config=clean_config(config)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        folder=Path(tmp);core=folder/'core.zip';backup.create_backup(store,config,core)
        with zipfile.ZipFile(core) as archive:(folder/'snapshot.db').write_bytes(archive.read('persona.sqlite3'))
        files={'core.zip':core}
        with closing(sqlite3.connect(folder/'snapshot.db')) as db:
            for record in asset_records(db):
                name=record[0]
                path=store.path.parent/'media'/name
                if not path.is_file():raise ValueError('备份缺少媒体文件：'+name)
                snapshot=folder/'media'/name;snapshot.parent.mkdir(exist_ok=True);shutil.copyfile(path,snapshot)
                check_asset(snapshot,record)
                files['media/'+name]=snapshot
        manifest={'format':'wechat-studio-workspace','version':2,'config':config,'files':{name:{'size':p.stat().st_size,'sha256':digest(p)} for name,p in files.items()}}
        if sum(info['size'] for info in manifest['files'].values())>MAX_WORKSPACE_BYTES:raise ValueError('工作区超过8GB完整备份上限，请按来源分批导出媒体')
        manifest_data=json.dumps(manifest).encode('utf-8')
        if len(manifest_data)>MAX_MANIFEST_BYTES:raise ValueError('媒体条目过多，完整备份清单超过8MB恢复上限，请分批整理后再备份')
        target=folder/'workspace.zip'
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,path in files.items():archive.write(path,name)
            archive.writestr('workspace.json',manifest_data)
        os.replace(target,destination)
        return {'files':len(files),'media':len(files)-1}


def restore(store,current_config,source,config_path):
    root=store.path.parent
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        folder=Path(tmp);payload=folder/'payload';payload.mkdir();media=payload/'media';media.mkdir()
        with zipfile.ZipFile(source) as archive:
            if 'workspace.json' not in archive.namelist():
                checked=folder/'legacy-check';checked.mkdir();backup.unpack_checked(source,checked)
                with closing(sqlite3.connect(checked/'persona.sqlite3')) as db:
                    for record in asset_records(db):
                        path=root/'media'/record[0]
                        if not path.is_file():raise ValueError('旧版备份不含媒体文件，当前工作区缺少对应媒体；请使用含媒体的完整工作区备份')
                        check_asset(path,record)
                backups=root/'backups';backups.mkdir(exist_ok=True)
                safety=backups/('恢复前-含媒体-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.zip')
                create(store,current_config,safety)
                config,counts,_=backup.restore_backup(store,current_config,source,config_path)
                return config,counts,safety
            if archive.getinfo('workspace.json').file_size>MAX_MANIFEST_BYTES:raise ValueError('备份清单过大')
            try:manifest=json.loads(archive.read('workspace.json'))
            except (ValueError,UnicodeError) as exc:raise ValueError('备份清单损坏') from exc
            if not isinstance(manifest,dict) or manifest.get('format')!='wechat-studio-workspace' or manifest.get('version')!=2:raise ValueError('不支持的工作区备份格式')
            files=manifest.get('files',{})
            if not isinstance(files,dict) or 'core.zip' not in files:raise ValueError('备份缺少核心记录库')
            if set(archive.namelist())!=set(files)|{'workspace.json'} or len(archive.namelist())!=len(files)+1:raise ValueError('备份文件清单不一致')
            total=0
            for name,expected in files.items():
                if name!='core.zip' and not (name.startswith('media/') and MEDIA_NAME.fullmatch(name[6:])):raise ValueError('备份包含无效路径')
                if not isinstance(expected,dict) or not isinstance(expected.get('size'),int) or isinstance(expected.get('size'),bool) or not isinstance(expected.get('sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',expected['sha256']):raise ValueError('备份文件信息不正确')
                info=archive.getinfo(name);total+=info.file_size
                if info.file_size!=expected['size'] or total>MAX_WORKSPACE_BYTES:raise ValueError('备份大小不符或超过8GB恢复上限')
                path=payload/name
                with archive.open(info) as stream,path.open('wb') as target:shutil.copyfileobj(stream,target,1024*1024)
                if digest(path)!=expected['sha256']:raise ValueError('备份内容校验失败')
        checked=folder/'checked';checked.mkdir()
        config,counts=backup.unpack_checked(payload/'core.zip',checked)
        if 'config' in manifest:config=clean_config(manifest['config'])
        with closing(sqlite3.connect(checked/'persona.sqlite3')) as db:
            records=asset_records(db)
            expected_media={'media/'+record[0] for record in records}
            if set(files)-{'core.zip'}!=expected_media:raise ValueError('备份媒体与记录库登记不一致')
            for record in records:check_asset(media/record[0],record)
        backups=root/'backups';backups.mkdir(exist_ok=True)
        safety=backups/('恢复前-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.zip');create(store,current_config,safety)
        old_media=folder/'old-media';old_db=folder/'old-db'
        # SQLite's backup API includes committed WAL data; a file copy may omit it.
        with store.db() as source_db,closing(sqlite3.connect(old_db)) as target:source_db.backup(target)
        with closing(sqlite3.connect(store.path,timeout=1)) as db:
            if db.execute('PRAGMA journal_mode').fetchone()[0]=='wal':
                if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:raise ValueError('记录库仍被其他任务使用，请关闭相关任务后恢复')
                if db.execute('PRAGMA journal_mode=DELETE').fetchone()[0]!='delete':raise ValueError('记录库仍被其他任务使用，请稍后恢复')
        config_path=Path(config_path);old_config=config_path.read_bytes() if config_path.exists() else None
        if old_config is not None:(folder/'old-config').write_bytes(old_config)
        (folder/'new-config').write_text(json.dumps(config,ensure_ascii=False,indent=2),'utf-8')
        moved=False;installed_media=False;installed_db=False;installed_config=False
        try:
            if (root/'media').exists():os.replace(root/'media',old_media);moved=True
            os.replace(media,root/'media');installed_media=True
            os.replace(checked/'persona.sqlite3',store.path);installed_db=True
            os.replace(folder/'new-config',config_path);installed_config=True
        except Exception as exc:
            errors=[]
            def rollback(action):
                try:action()
                except Exception as error:errors.append(str(error))
            if installed_db:rollback(lambda:os.replace(old_db,store.path))
            if installed_media:rollback(lambda:os.replace(root/'media',folder/'failed-media'))
            if moved:rollback(lambda:os.replace(old_media,root/'media'))
            if installed_config:
                if old_config is None:rollback(lambda:config_path.unlink(missing_ok=True))
                else:rollback(lambda:os.replace(folder/'old-config',config_path))
            if errors:raise RuntimeError('恢复失败，自动回滚未完成。原工作区的完整安全备份：'+str(safety)) from exc
            raise
        return config,counts,safety
