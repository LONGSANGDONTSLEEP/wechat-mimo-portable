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


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def create(store,config,destination):
    destination=Path(destination)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        folder=Path(tmp);core=folder/'core.zip';backup.create_backup(store,config,core)
        with zipfile.ZipFile(core) as archive:(folder/'snapshot.db').write_bytes(archive.read('persona.sqlite3'))
        files={'core.zip':core}
        with closing(sqlite3.connect(folder/'snapshot.db')) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='assets'").fetchone():
                for (name,) in db.execute('SELECT filename FROM assets'):
                    if not re.fullmatch(r'[0-9a-f]{64}\.[a-z0-9]{1,6}',name):raise ValueError('媒体文件名异常')
                    path=store.path.parent/'media'/name
                    if not path.is_file():raise ValueError('备份缺少媒体文件：'+name)
                    files['media/'+name]=path
        manifest={'format':'wechat-studio-workspace','version':2,'files':{name:{'size':p.stat().st_size,'sha256':digest(p)} for name,p in files.items()}}
        if sum(info['size'] for info in manifest['files'].values())>8*1024**3:raise ValueError('工作区超过8GB完整备份上限，请按来源分批导出媒体')
        target=folder/'workspace.zip'
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,path in files.items():archive.write(path,name)
            archive.writestr('workspace.json',json.dumps(manifest))
        os.replace(target,destination)
        return {'files':len(files),'media':len(files)-1}


def restore(store,current_config,source,config_path):
    root=store.path.parent
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        folder=Path(tmp);payload=folder/'payload';payload.mkdir();media=payload/'media';media.mkdir()
        with zipfile.ZipFile(source) as archive:
            if 'workspace.json' not in archive.namelist():
                return backup.restore_backup(store,current_config,source,config_path)
            if archive.getinfo('workspace.json').file_size>8*1024*1024:raise ValueError('备份清单过大')
            manifest=json.loads(archive.read('workspace.json'))
            if manifest.get('format')!='wechat-studio-workspace' or manifest.get('version')!=2:raise ValueError('不支持的工作区备份格式')
            files=manifest.get('files',{})
            if set(archive.namelist())!=set(files)|{'workspace.json'} or len(archive.namelist())!=len(files)+1:raise ValueError('备份文件清单不一致')
            total=0
            for name,expected in files.items():
                if name!='core.zip' and not (name.startswith('media/') and len(Path(name).parts)==2 and '..' not in name and '\\' not in name):raise ValueError('备份包含无效路径')
                info=archive.getinfo(name);total+=info.file_size
                if info.file_size!=expected['size'] or total>8*1024**3:raise ValueError('备份大小不符或超过8GB恢复上限')
                path=payload/name
                with archive.open(info) as stream,path.open('wb') as target:shutil.copyfileobj(stream,target,1024*1024)
                if digest(path)!=expected['sha256']:raise ValueError('备份内容校验失败')
        checked=folder/'checked';checked.mkdir()
        config,counts=backup.unpack_checked(payload/'core.zip',checked)
        with closing(sqlite3.connect(checked/'persona.sqlite3')) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='assets'").fetchone():
                for (filename,) in db.execute('SELECT filename FROM assets'):
                    if not re.fullmatch(r'[0-9a-f]{64}\.[a-z0-9]{1,6}',filename) or not (media/filename).is_file():raise ValueError('备份缺少已登记媒体')
        backups=root/'backups';backups.mkdir(exist_ok=True)
        safety=backups/('恢复前-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.zip');create(store,current_config,safety)
        old_media=folder/'old-media';old_db=folder/'old-db';shutil.copyfile(store.path,old_db)
        config_path=Path(config_path);old_config=config_path.read_bytes() if config_path.exists() else None
        moved=False
        try:
            if (root/'media').exists():os.replace(root/'media',old_media);moved=True
            os.replace(media,root/'media');os.replace(checked/'persona.sqlite3',store.path)
            (folder/'new-config').write_text(json.dumps(config,ensure_ascii=False,indent=2),'utf-8');os.replace(folder/'new-config',config_path)
        except Exception:
            os.replace(old_db,store.path)
            if (root/'media').exists():os.replace(root/'media',folder/'failed-media')
            if moved:os.replace(old_media,root/'media')
            if old_config is None:config_path.unlink(missing_ok=True)
            else:config_path.write_bytes(old_config)
            raise
        return config,counts,safety
