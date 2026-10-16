"""Migrate persisted settings without preventing the workspace from opening."""
import json
from pathlib import Path
import re

from backup import clean_config


def load_config(path,defaults):
    result=dict(defaults,auto=False);warnings=[];path=Path(path)
    if not path.exists():return result,warnings
    try:raw=json.loads(path.read_text('utf-8-sig'))
    except (OSError,UnicodeError,ValueError):
        return result,['配置文件无法读取，已临时使用默认模型设置；原文件和聊天数据保留，请在“模型与设置”检查后保存。']
    if not isinstance(raw,dict):
        return result,['配置文件应为对象，已临时使用默认模型设置；原文件和聊天数据保留。']
    for field in ('base_url','model','prompt','title'):
        if field not in raw:continue
        if isinstance(raw[field],str):result[field]=raw[field]
        else:warnings.append('配置项 '+field+' 格式不正确，已使用默认值。')
    for field,options in (('provider',('mimo','lmstudio','ollama')),('reasoning',('default','none'))):
        if field not in raw:continue
        if raw[field] in options:result[field]=raw[field]
        else:warnings.append('配置项 '+field+' 不受支持，已使用默认值。')
    for field,minimum,maximum in (('context_size',2048,131072),('max_tokens',128,8192),('interval',1,3600)):
        if field not in raw:continue
        value=raw[field]
        if isinstance(value,str) and re.fullmatch(r'[0-9]{1,9}',value.strip()):value=int(value.strip())
        elif isinstance(value,float) and value.is_integer():value=int(value)
        if isinstance(value,int) and not isinstance(value,bool) and minimum<=value<=maximum:
            result[field]=value
        else:warnings.append('配置项 '+field+' 无效，已临时使用默认值；请在“模型与设置”检查后保存。')
    if result['max_tokens']>=result['context_size']-384:
        result['max_tokens']=defaults['max_tokens']
        warnings.append('回复长度上限与上下文长度冲突，已使用默认回复长度；请检查模型设置。')
    # Discard credentials/unknown legacy fields from runtime settings as well.
    return dict(defaults,**clean_config(result)),warnings
