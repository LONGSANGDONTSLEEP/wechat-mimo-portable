"""Local record browsing, export and descriptive statistics."""
import csv
import html
import json
from pathlib import Path
from collections import Counter
from toolbox import parse_time


def query(store, contact='', keyword='', offset=0, limit=200):
    where = []; args = []
    if contact:
        where.append('contact=?'); args.append(contact)
    if keyword:
        # Literal substring search: %, _ and quotes are not SQL wildcards.
        where.append('(instr(content,?)>0 OR instr(sender,?)>0)'); args.extend([keyword, keyword])
    clause = ' WHERE ' + ' AND '.join(where) if where else ''
    with store.db() as db:
        total = db.execute('SELECT COUNT(*) FROM messages' + clause, args).fetchone()[0]
        rows = [dict(r) for r in db.execute('SELECT * FROM messages' + clause + ' ORDER BY id DESC LIMIT ? OFFSET ?', args + [limit, offset])]
    return rows, total


def all_rows(store, contact=''):
    with store.db() as db:
        return [dict(r) for r in db.execute('SELECT * FROM messages' + (' WHERE contact=?' if contact else '') + ' ORDER BY id', (contact,) if contact else ())]


def export_file(path, rows):
    path = Path(path)
    # Same portable field schema for round trips across all export formats.
    data = [dict(contact=r['contact'], sender=r['sender'], role=r['role'], time=r['ts'], content=r['content']) for r in rows]
    if path.suffix.lower() == '.json':
        path.write_text(json.dumps({'messages': data}, ensure_ascii=False, indent=2), 'utf-8')
    elif path.suffix.lower() == '.csv':
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['contact','sender','role','time','content'])
            writer.writeheader()
            # Spreadsheet formulas must stay plain text when CSV is opened in Excel.
            writer.writerows({k: "'"+v if v.startswith(('=','+','-','@','\t','\r')) else v for k,v in row.items()} for row in data)
    elif path.suffix.lower() in ('.html', '.htm'):
        cards = ''.join('<article><small>' + html.escape(r['time']+' · '+r['contact']+' · '+r['sender']) + '</small><p>' + html.escape(r['content']) + '</p></article>' for r in data)
        payload = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&','\\u0026')
        path.write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>聊天记录导出</title><style>body{max-width:900px;margin:40px auto;padding:20px;font:16px system-ui;background:#f3f6fa;color:#20324a}article{background:white;border-radius:12px;padding:18px;margin:12px 0}small{color:#61748b}p{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>聊天记录</h1>' + cards + '<script type="application/json">' + payload + '</script></html>', 'utf-8')
    else:
        raise ValueError('请选择 JSON、CSV 或 HTML 格式')
    return len(data)


def statistics(store, contact=''):
    rows = all_rows(store, contact)
    dates = []; invalid = 0; hours = Counter(); days = Counter(); senders = Counter()
    for row in rows:
        senders[row['sender']] += 1
        date = parse_time(row['ts'])
        if date is None:
            invalid += 1
            continue
        dates.append(date); days[date.strftime('%Y-%m-%d')] += 1; hours[date.hour] += 1
    return dict(total=len(rows), own=sum(r['role']=='self' for r in rows), unknown=sum(r['role']=='unknown' for r in rows),
                first=min(dates) if dates else None, last=max(dates) if dates else None, invalid=invalid,
                hours=hours, days=days, senders=senders)
