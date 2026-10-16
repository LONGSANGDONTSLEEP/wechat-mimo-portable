// Run with: node --test tests/frontend.test.cjs
// A small in-memory DOM tests user flows without opening a browser or a local URL.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(doc, tag = 'div', root = false) {
    this.doc = doc; this.tagName = tag.toUpperCase(); this.root = root;
    this.children = []; this.parent = null; this.dataset = {}; this.style = {};
    this.listeners = {}; this.attrs = {}; this.value = ''; this.textContent = '';
    this.disabled = false; this.hidden = false; this.open = false;
    this.classList = { add() {}, remove() {}, toggle() {} };
    this.doc.nodes.push(this);
  }
  get isConnected() { return this.root || !!this.parent?.isConnected; }
  set innerHTML(value) {
    this.replaceChildren(); this.markup = value;
    for (const match of value.matchAll(/<([\w-]+)\b([^>]*)>/g)) {
      const el = new Element(this.doc, match[1]); el.parent = this; this.children.push(el);
      for (const a of match[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
        const [, key, val = ''] = a; el.attrs[key] = val;
        if (key === 'id') el.id = val;
        if (key === 'value') el.value = val;
        if (key === 'disabled') el.disabled = true;
        if (key.startsWith('data-')) el.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = val;
      }
    }
  }
  get innerHTML() { return this.markup || this.children.map(c => c.innerHTML).join(''); }
  replaceChildren(...children) {
    for (const child of this.children) child.parent = null;
    this.children = children; this.markup = '';
    for (const child of children) child.parent = this;
  }
  append(el) { el.parent = this; this.children.push(el); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(el => el !== this); this.parent = null; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(key, fn) { (this.listeners[key] ||= []).push(fn); }
  dispatch(key) { for (const fn of this.listeners[key] || []) fn({ preventDefault() {}, target: this }); }
  click() { if (!this.disabled) return this.onclick?.({ preventDefault() {}, target: this }); }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

function fakeDocument() {
  const doc = { nodes: [] };
  doc.createElement = tag => new Element(doc, tag);
  doc.body = new Element(doc, 'body', true);
  doc.querySelectorAll = selector => {
    if (selector.includes(',')) return [...new Set(selector.split(',').flatMap(s => doc.querySelectorAll(s.trim())))];
    if (selector.includes(' ')) return []; // Unneeded media descendants and nav styles.
    return doc.nodes.filter(el => {
      if (!el.isConnected) return false;
      if (selector.startsWith('#')) return el.id === selector.slice(1);
      if (selector === '.brand') return el.brand;
      const attr = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(selector);
      return attr ? attr[1] in el.attrs && (attr[2] === undefined || el.attrs[attr[1]] === attr[2]) : false;
    });
  };
  doc.querySelector = selector => doc.querySelectorAll(selector)[0] || null;
  doc.getElementById = id => doc.querySelector('#' + id);
  for (const id of ['main', 'crumb', 'nav', 'modelBadge', 'connectionStatus', 'taskbar', 'toast', 'dialog', 'dialogTitle', 'dialogBody', 'closeDialog', 'recordFiles', 'mediaFiles', 'restoreFile', 'quit']) {
    const el = doc.createElement('div'); el.id = id; doc.body.append(el);
  }
  const brand = doc.createElement('a'); brand.brand = true; doc.body.append(brand);
  return doc;
}

function boot() {
  const document = fakeDocument();
  const context = vm.createContext({
    document, URLSearchParams, URL, Blob, console,
    location: { hash: '', pathname: '/' }, history: { replaceState() {} },
    sessionStorage: { getItem() { return ''; }, setItem() {} },
    navigator: { clipboard: { writeText: async () => {} } },
    setTimeout() {}, clearTimeout() {}, setInterval() {}, confirm: () => true,
    window: { scrollTo() {} }, fetch: async () => { throw Error('No network in frontend tests'); }
  });
  // Suppress only the automatic boot request. All view and action code is unchanged.
  const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  const start = source.lastIndexOf('startPolling();');
  assert.ok(start > 0, 'Find automatic startup');
  vm.runInContext(source.slice(0, start), context);
  const state = { config: { provider: 'lmstudio', model: 'demo', base_url: 'http://127.0.0.1:1234/v1', context_size: 8192, prompt: '', max_tokens: 768 }, jobs: [], contacts: [], engine: { running: false }, stats: { total: 0 } };
  context.fixture = state; vm.runInContext('state=fixture;firstPoll=false', context);
  return { context, document, state, run: code => vm.runInContext(code, context) };
}
const response = value => ({ ok: true, json: async () => value });
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

test('a slow records request cannot replace the page opened afterward', async () => {
  const ui = boot(), pending = deferred();
  ui.context.fetch = () => pending.promise;
  ui.run("views.reply=async m=>{m.innerHTML='CURRENT_REPLY_PAGE'}");
  const records = ui.run("render('records')");
  await ui.run("render('reply')");
  pending.resolve(response({ rows: [], total: 0 })); await records;
  assert.equal(ui.document.getElementById('main').innerHTML, 'CURRENT_REPLY_PAGE');
  assert.equal(ui.document.getElementById('crumb').textContent, '回复试验室');
});

test('a failed request from a page that was left cannot replace the current page', async () => {
  const ui = boot(), pending = deferred(); ui.context.fetch = () => pending.promise;
  ui.run("views.reply=async m=>{m.innerHTML='CURRENT_REPLY_PAGE'}");
  const records = ui.run("render('records')"); await ui.run("render('reply')");
  pending.reject(Error('disconnected')); await records;
  assert.equal(ui.document.getElementById('main').innerHTML, 'CURRENT_REPLY_PAGE');
});

test('a second job is rejected while the first submission has no response yet', async () => {
  const ui = boot(), pending = deferred(); let requests = 0;
  ui.context.fetch = () => { requests++; return pending.promise; };
  ui.run('poll=async()=>{}');
  const first = ui.run("job('/api/reply',{text:'hello'},'replyOutput')");
  await assert.rejects(ui.run("job('/api/analysis',{},'analysisOutput')"), /当前任务尚未结束/);
  pending.resolve(response({ job: 'j1' })); await first;
  assert.equal(requests, 1); assert.equal(ui.run('resultStates.replyOutput'), 'running');
});

test('changed reply inputs invalidate copying the previous response', () => {
  const ui = boot();
  ui.document.getElementById('main').innerHTML = '<textarea id="replyText"></textarea><button data-action="copyReply"></button><div id="replyOutput"></div><div id="replyOutputStatus"></div>';
  ui.run("watchResult('replyOutput',['#replyText']);updateResult('replyOutput','previous answer','success')");
  const copy = ui.document.querySelector('[data-action="copyReply"]'); assert.equal(copy.disabled, false);
  ui.document.getElementById('replyText').dispatch('input');
  assert.equal(copy.disabled, true); assert.match(ui.document.getElementById('replyOutputStatus').textContent, /输入已更改/);
});

test('model errors remain errors and cannot be copied as generated responses', async () => {
  const ui = boot();
  ui.document.getElementById('main').innerHTML = '<button data-action="copyReply"></button><div id="replyOutput"></div><div id="replyOutputStatus"></div>';
  ui.run("jobTargets.j2='replyOutput'");
  ui.state.jobs = [{ id: 'j2', state: 'error', label: '生成回复', error: 'Token 无效' }];
  ui.context.fetch = async () => response(ui.state); await ui.run('poll()');
  assert.match(ui.document.getElementById('replyOutput').textContent, /Token 无效/);
  assert.equal(ui.document.querySelector('[data-action="copyReply"]').disabled, true);
  assert.equal(ui.run('resultStates.replyOutput'), 'error');
});

test('editing while generation is in progress keeps the returned answer marked stale', async () => {
  const ui = boot();
  ui.document.getElementById('main').innerHTML = '<textarea id="replyText"></textarea><button data-action="copyReply"></button><div id="replyOutput"></div><div id="replyOutputStatus"></div>';
  ui.run("jobTargets.j3='replyOutput';jobRevisions.j3=0;watchResult('replyOutput',['#replyText']);updateResult('replyOutput','','running')");
  ui.document.getElementById('replyText').dispatch('input');
  ui.state.jobs = [{ id: 'j3', state: 'done', label: '生成回复', result: { text: 'answer for the previous message' } }];
  ui.context.fetch = async () => response(ui.state); await ui.run('poll()');
  assert.equal(ui.run('resultStates.replyOutput'), 'stale');
  assert.equal(ui.document.querySelector('[data-action="copyReply"]').disabled, true);
});

test('settings save output budget and local thinking control without leaking a Token into config fields', async () => {
  const ui = boot(), calls = [];
  ui.run('poll=async()=>{}');
  ui.context.fetch = async (url, options) => { calls.push([url, JSON.parse(options.body)]); return response({ saved: true, key_set: true }); };
  const mount = ui.document.createElement('div'); ui.document.getElementById('main').replaceChildren(mount);
  ui.context.mount = mount; await ui.run('views.settings(mount)');
  for (const [id, value] of Object.entries({ contextSize: '8192', maxTokens: '1536', modelId: 'demo-model', apiKey: 'synthetic-token', reasoning: 'none' })) ui.document.getElementById(id).value = value;
  await ui.document.querySelector('[data-action="saveSettings"]').onclick();
  const saved = calls.find(([url]) => url === '/api/config')[1];
  assert.equal(saved.max_tokens, 1536); assert.equal(saved.reasoning, 'none'); assert.equal(saved.key, 'synthetic-token');
  assert.equal(ui.document.getElementById('apiKey').value, '');
  assert.equal(ui.document.getElementById('baseUrl').value, 'http://127.0.0.1:1234/v1');
});

test('closing an unconfirmed import clears its temporary preview identity', async () => {
  const ui = boot(), calls = [];
  ui.context.fetch = async (url, options) => {
    calls.push([url, options]);
    return response(url.startsWith('/api/upload') ? { import_id: 'preview1', fields: ['content'], rows: [{ content: 'synthetic' }], count: 1 } : { discarded: true });
  };
  ui.context.files = [{ name: 'demo.json', size: 3 }];
  await ui.run('importFiles(files)'); assert.equal(ui.document.getElementById('dialog').open, true);
  await ui.run('closeModal()');
  assert.equal(ui.document.getElementById('dialog').open, false);
  const cleanup = calls.find(([url]) => url === '/api/import-discard');
  assert.deepEqual(JSON.parse(cleanup[1].body), { import_id: 'preview1' });
});

test('a partially failed batch retries only the unfinished file', async () => {
  const ui = boot(), confirmations = [], uploads = []; let failed = false;
  ui.run("poll=async()=>{};render=async name=>{route=name}");
  ui.context.fetch = async (url, options) => {
    if (url.startsWith('/api/upload')) { const name = new URL('http://test' + url).searchParams.get('name'); uploads.push(name); return response({ import_id: name, fields: ['content'], rows: [], count: 1 }); }
    if (url === '/api/import-confirm') { const name = JSON.parse(options.body).import_id; confirmations.push(name); if (name === 'second.json' && !failed) { failed = true; return { ok: false, json: async () => ({ error: 'temporary failure' }) }; } return response({ inserted: 1 }); }
    return response({ discarded: true });
  };
  ui.context.files = [{ name: 'first.json', size: 2 }, { name: 'second.json', size: 2 }];
  await ui.run('importFiles(files)');
  await ui.document.getElementById('confirmImport').onclick();
  assert.match(ui.document.getElementById('importProgress').textContent, /已完成 1\/2/);
  await ui.document.getElementById('confirmImport').onclick();
  assert.deepEqual(confirmations, ['first.json', 'second.json', 'second.json']);
  assert.deepEqual(uploads, ['first.json', 'second.json', 'second.json']);
  assert.equal(ui.document.getElementById('dialog').open, false);
});

test('asset names and URLs are escaped and SVG is never embedded as an image', () => {
  const ui = boot();
  const markup = ui.run(`assetLinks([{id:'a/b',name:'<script>alert(1)</script>',mime:'image/svg+xml',size:42}])`);
  assert.ok(!markup.includes('<script>')); assert.ok(!markup.includes('<img'));
  assert.match(markup, /&lt;script&gt;/);
});

test('media batch stop preserves the current file and skips remaining files', async () => {
  const ui = boot(), pending = deferred(); let uploads = 0;
  ui.context.fetch = async url => {
    if (url.startsWith('/api/upload')) { uploads++; return pending.promise; }
    return response(ui.state);
  };
  const task = ui.document.getElementById('mediaFiles').onchange({ target: { value: 'selection', files: [{ name: 'one.png', size: 4 }, { name: 'two.png', size: 4 }] } });
  await ui.document.getElementById('cancelJob').onclick();
  pending.resolve(response({ id: 'first-asset' })); await task;
  assert.equal(uploads, 1); assert.equal(ui.run('fileBusy'), false);
  assert.equal(ui.document.getElementById('dialogTitle').textContent, '媒体导入部分完成');
  assert.match(ui.document.getElementById('dialogBody').innerHTML, /未处理 1 个/);
});

test('startup HTTP 400 shows the configuration error as a service failure with a retry action', async () => {
  const ui = boot(); ui.run('state=null;firstPoll=true');
  ui.context.fetch = async url => url === '/api/session' ? response({ ok: true }) : { ok: false, status: 400, json: async () => ({ error: '模型配置范围不正确：context_size' }) };
  await ui.run('poll()');
  assert.equal(ui.document.getElementById('connectionStatus').textContent, '本机服务异常');
  assert.match(ui.document.getElementById('main').innerHTML, /模型配置范围不正确：context_size/);
  assert.ok(ui.document.getElementById('retryStartup'));
  assert.equal(ui.document.getElementById('modelId'), null, 'Do not invent a settings form before configuration has loaded');
});

test('a successful poll after startup failure automatically opens the route selected during the failure', async () => {
  const ui = boot(); ui.run('state=null;firstPoll=true');
  let fail = true;
  ui.context.fetch = async url => url === '/api/session' ? response({ ok: true }) : fail ? { ok: false, status: 500, json: async () => ({ error: 'configuration unavailable' }) } : response(ui.state);
  await ui.run('poll()'); await ui.run("render('settings')");
  assert.equal(ui.document.getElementById('modelId'), null);
  fail = false; await ui.run('poll()');
  assert.equal(ui.document.getElementById('connectionStatus').textContent, '本机已连接');
  assert.equal(ui.document.getElementById('modelId').value, 'demo');
  assert.equal(ui.document.getElementById('crumb').textContent, '模型与设置');
  assert.equal(ui.document.getElementById('retryStartup'), null);
});

test('the retry button recovers the workspace without requiring a page reload', async () => {
  const ui = boot(); ui.run('state=null;firstPoll=true'); let fail = true;
  ui.context.fetch = async url => url === '/api/session' ? response({ ok: true }) : fail ? { ok: false, status: 400, json: async () => ({ error: 'old config' }) } : response(ui.state);
  await ui.run('poll()'); const retry = ui.document.getElementById('retryStartup');
  fail = false; await retry.onclick();
  assert.match(ui.document.getElementById('main').innerHTML, /让记录与交流/);
  assert.equal(ui.document.getElementById('retryStartup'), null);
});

test('network failures are classified separately and session setup is retried after recovery', async () => {
  const ui = boot(), calls = []; ui.run('state=null;firstPoll=true'); let fail = true;
  ui.context.fetch = async url => { calls.push(url); if (fail) throw Error('connection refused'); return response(url === '/api/session' ? { ok: true } : ui.state); };
  await ui.run('poll()');
  assert.equal(ui.document.getElementById('connectionStatus').textContent, '本机连接中断');
  assert.match(ui.document.getElementById('main').innerHTML, /无法连接本机工作台/);
  fail = false; await ui.run('poll()');
  assert.deepEqual(calls, ['/api/session', '/api/session', '/api/state']);
  assert.equal(ui.document.getElementById('connectionStatus').textContent, '本机已连接');
});

test('an HTTP response with unreadable JSON is a service error rather than a disconnected network', async () => {
  const ui = boot(); ui.run('state=null;firstPoll=true;sessionReady=true');
  ui.context.fetch = async () => ({ ok: false, status: 500, json: async () => { throw Error('invalid JSON'); } });
  await ui.run('poll()');
  assert.equal(ui.document.getElementById('connectionStatus').textContent, '本机服务异常');
  assert.match(ui.document.getElementById('main').innerHTML, /无法读取的响应/);
});

test('poll errors preserve a loaded form and expose the actual application error', async () => {
  const ui = boot();
  ui.document.getElementById('main').innerHTML = '<textarea id="unsavedDraft"></textarea>';
  ui.document.getElementById('unsavedDraft').value = 'Keep this draft';
  ui.context.fetch = async url => url === '/api/session' ? response({ ok: true }) : { ok: false, status: 400, json: async () => ({ error: '模型配置范围不正确：context_size' }) };
  await ui.run('poll()');
  assert.equal(ui.document.getElementById('unsavedDraft').value, 'Keep this draft');
  assert.equal(ui.document.getElementById('connectionStatus').textContent, '本机服务异常');
  assert.match(ui.document.getElementById('connectionStatus').title, /context_size/);
  assert.match(ui.document.getElementById('toast').textContent, /context_size/);
});

test('configuration migration warnings are escaped, displayed, and removed after settings save', async () => {
  const ui = boot(); ui.state.config_warnings = ['context_size 无效，已使用默认值 <8192>'];
  ui.run('poll=async()=>{}');
  ui.context.fetch = async () => response({ saved: true, key_set: false });
  const mount = ui.document.createElement('div'); ui.document.getElementById('main').replaceChildren(mount);
  ui.context.mount = mount; await ui.run('views.settings(mount)');
  assert.ok(ui.document.getElementById('configWarnings'));
  assert.match(mount.innerHTML, /context_size 无效/); assert.match(mount.innerHTML, /&lt;8192&gt;/);
  for (const [id, value] of Object.entries({ contextSize: '8192', maxTokens: '768', reasoning: 'none' })) ui.document.getElementById(id).value = value;
  await ui.document.querySelector('[data-action="saveSettings"]').onclick();
  assert.equal(ui.document.getElementById('configWarnings'), null);
});

async function sourcesView(ui) {
  const mount = ui.document.createElement('div'); ui.document.getElementById('main').replaceChildren(mount);
  ui.context.mount = mount; await ui.run('views.sources(mount)');
  ui.document.getElementById('dbPath').value = 'F:/synthetic/account/db_storage';
  return mount;
}

test('data sources marks automatic decryption as experimental and keeps process controls off by default', async () => {
  const ui = boot(), mount = await sourcesView(ui);
  assert.match(mount.innerHTML, /已验证导入流程/);
  assert.match(mount.innerHTML, /自动取密钥尚未完成真实解密验证/);
  assert.equal(ui.document.getElementById('dbPid').disabled, true);
  assert.equal(ui.document.getElementById('processArea').hidden, true);
  assert.equal(ui.document.getElementById('encryptedOptions').open, false);
});

test('plaintext database import does not require or submit a WeChat process ID', async () => {
  const ui = boot(), calls = []; await sourcesView(ui); ui.run('poll=async()=>{}');
  ui.context.fetch = async (url, options) => { calls.push([url, options]); return response(url === '/api/inventory' ? [{ name: 'message_0.db', size: 4096, encrypted: false }] : { job: 'plain-import' }); };
  ui.document.getElementById('dbPid').value = 'a stale process value';
  ui.document.getElementById('autoKey').checked = false;
  await ui.document.querySelector('[data-action="localImport"]').onclick();
  const submitted = JSON.parse(calls.find(([url]) => url === '/api/local-import')[1].body);
  assert.equal(submitted.auto_key, false); assert.equal(Object.hasOwn(submitted, 'pid'), false);
  assert.equal(calls.some(([url]) => url === '/api/processes'), false);
});

test('encrypted database import with an existing key also avoids the process requirement', async () => {
  const ui = boot(), calls = []; await sourcesView(ui); ui.run('poll=async()=>{}');
  ui.context.fetch = async (url, options) => { calls.push([url, options]); return response(url === '/api/inventory' ? [{ name: 'message_0.db', size: 4096, encrypted: true }] : { job: 'key-import' }); };
  ui.document.getElementById('dbKey').value = 'a'.repeat(64);
  ui.document.getElementById('autoKey').checked = false;
  await ui.document.querySelector('[data-action="localImport"]').onclick();
  const submitted = JSON.parse(calls.find(([url]) => url === '/api/local-import')[1].body);
  assert.equal(submitted.key, 'a'.repeat(64)); assert.equal(Object.hasOwn(submitted, 'pid'), false);
  assert.equal(ui.document.getElementById('dbKey').value, '');
});

test('fully encrypted input without a key explains the unsupported path before creating a job', async () => {
  const ui = boot(), calls = []; await sourcesView(ui);
  ui.context.fetch = async url => { calls.push(url); return response([{ name: 'message_0.db', size: 4096, encrypted: true }]); };
  await ui.document.querySelector('[data-action="localImport"]').onclick();
  assert.deepEqual(calls, ['/api/inventory']);
  assert.match(ui.document.getElementById('sourceStatus').textContent, /所选数据库全部已加密/);
  assert.equal(ui.document.getElementById('encryptedOptions').open, true);
});

test('unreadable WeChat processes are disabled with an escaped reason and cannot start auto-key discovery', async () => {
  const ui = boot(), calls = []; await sourcesView(ui);
  ui.context.fetch = async url => { calls.push(url); return response([{ pid: 11, title: '微信 - synthetic chat', accessible: true, access_error: '' }, { pid: 22, title: '微信后台', accessible: false, access_error: '拒绝访问 <Access denied>' }]); };
  const auto = ui.document.getElementById('autoKey'); auto.checked = true; auto.onchange();
  await ui.document.querySelector('[data-action="refreshPid"]').onclick();
  const select = ui.document.getElementById('dbPid');
  assert.equal(select.children.find(el => el.value === '22').disabled, true);
  assert.equal(select.children.find(el => el.value === '11').disabled, false);
  assert.match(ui.document.getElementById('processStatus').innerHTML, /拒绝访问 &lt;Access denied&gt;/);
  assert.match(select.innerHTML, /微信 - synthetic chat/);
  select.value = '22'; await ui.document.querySelector('[data-action="localImport"]').onclick();
  assert.deepEqual(calls, ['/api/processes']);
  assert.match(ui.document.getElementById('sourceStatus').textContent, /选择可只读访问/);
});

test('an explicitly selected readable process is used only for the experimental auto-key request', async () => {
  const ui = boot(), calls = []; await sourcesView(ui); ui.run('poll=async()=>{}');
  ui.context.fetch = async (url, options) => {
    calls.push([url, options]);
    return response(url === '/api/processes' ? [{ pid: 11, title: '微信 - demo', accessible: true }] : url === '/api/inventory' ? [{ name: 'message_0.db', size: 4096, encrypted: true }] : { job: 'experimental-key-import' });
  };
  const auto = ui.document.getElementById('autoKey'); auto.checked = true; auto.onchange();
  await ui.document.querySelector('[data-action="refreshPid"]').onclick();
  ui.document.getElementById('dbPid').value = '11';
  await ui.document.querySelector('[data-action="localImport"]').onclick();
  const submitted = JSON.parse(calls.find(([url]) => url === '/api/local-import')[1].body);
  assert.equal(submitted.auto_key, true); assert.equal(submitted.pid, 11);
});

test('database job failures remain visible inside the source form with the actual cause', async () => {
  const ui = boot(); await sourcesView(ui);
  ui.run("jobTargets.denied='sourceStatus'");
  ui.state.jobs = [{ id: 'denied', state: 'error', label: '读取本机/已解密数据库', error: '无法只读访问所选微信进程：拒绝访问' }];
  ui.context.fetch = async () => response(ui.state); await ui.run('poll()');
  assert.match(ui.document.getElementById('sourceStatus').textContent, /无法只读访问所选微信进程：拒绝访问/);
  assert.equal(ui.document.getElementById('sourceStatus').hidden, false);
});

async function showView(ui, name) {
  const mount = ui.document.createElement('div'); ui.document.getElementById('main').replaceChildren(mount);
  ui.context.mount = mount; await ui.run('views.' + name + '(mount)'); return mount;
}
function fillSettings(ui) {
  for (const [id, value] of Object.entries({ contextSize: '8192', maxTokens: '768', reasoning: 'none' })) ui.document.getElementById(id).value = value;
}

test('switching contacts keeps a search that the user cleared before pressing Search', async () => {
  const ui = boot(), requests = []; ui.state.contacts = [{ contact: '界面验收', total: 1 }];
  ui.run("keyword='公园'");
  ui.context.fetch = async url => { requests.push(url); return response({ rows: [], total: 0 }); };
  await ui.run("render('records')");
  ui.document.getElementById('recordSearch').value = '';
  await ui.document.querySelector('[data-contact="界面验收"]').onclick();
  const next = new URL('http://synthetic' + requests.at(-1));
  assert.equal(next.searchParams.get('contact'), '界面验收'); assert.equal(next.searchParams.get('keyword'), '');
  assert.equal(ui.document.getElementById('recordSearch').value, '');
});

test('settings preserve a valid custom context size and prevent concurrent saving/testing', async () => {
  const ui = boot(), pending = deferred(); ui.state.config.context_size = 12288; ui.run('poll=async()=>{}');
  const mount = await showView(ui, 'settings');
  assert.match(mount.innerHTML, /value="12288" selected/);
  fillSettings(ui); ui.context.fetch = async () => pending.promise;
  const save = ui.document.querySelector('[data-action="saveSettings"]').onclick();
  assert.equal(ui.document.getElementById('baseUrl').disabled, true);
  assert.equal(ui.document.querySelector('[data-action="testModel"]').disabled, true);
  pending.resolve(response({ saved: true, key_set: false })); await save;
  assert.equal(ui.document.getElementById('baseUrl').disabled, false);
  assert.match(ui.document.getElementById('settingsStatus').textContent, /配置已保存到本机/);
});

test('a failed settings save leaves the entered token for correction and shows the error persistently', async () => {
  const ui = boot(); await showView(ui, 'settings'); fillSettings(ui);
  ui.document.getElementById('apiKey').value = 'synthetic-token';
  ui.context.fetch = async () => ({ ok: false, status: 400, json: async () => ({ error: '请检查本地上下文长度和回复长度设置' }) });
  await ui.document.querySelector('[data-action="saveSettings"]').onclick();
  assert.equal(ui.document.getElementById('apiKey').value, 'synthetic-token');
  assert.match(ui.document.getElementById('settingsStatus').textContent, /上下文长度/);
  assert.equal(ui.document.getElementById('baseUrl').disabled, false);
});

test('a stale poll cannot roll the model configuration back after saving', async () => {
  const ui = boot(), pending = deferred(); await showView(ui, 'settings'); fillSettings(ui);
  ui.run('sessionReady=true');
  const old = JSON.parse(JSON.stringify(ui.state));
  ui.context.fetch = async url => url === '/api/state' ? pending.promise : response({ saved: true, key_set: true });
  const poll = ui.run('poll()');
  ui.document.getElementById('modelId').value = 'newly-saved-model';
  ui.document.getElementById('apiKey').value = 'synthetic-token';
  await ui.document.querySelector('[data-action="saveSettings"]').onclick();
  pending.resolve(response(old)); await poll;
  assert.equal(ui.run('state.config.model'), 'newly-saved-model'); assert.equal(ui.run('state.key_set'), true);
  assert.equal(ui.run("Object.hasOwn(state.config,'key')"), false);
});

test('an accepted job stays locked when status polling fails and unlocks on its final result', async () => {
  const ui = boot(); ui.run('sessionReady=true');
  ui.context.fetch = async url => url === '/api/reply' ? response({ job: 'waiting' }) : Promise.reject(Error('offline'));
  await ui.run("job('/api/reply',{text:'hello'},'replyOutput')");
  await assert.rejects(ui.run("job('/api/reply',{text:'duplicate'},'replyOutput')"), /当前任务尚未结束/);
  ui.state.jobs = [{ id: 'waiting', state: 'done', label: '生成回复预览', result: { text: 'synthetic answer' } }];
  ui.context.fetch = async () => response(ui.state); await ui.run('poll()');
  assert.equal(ui.run('busy()'), false); assert.equal(ui.run('results.replyOutput'), 'synthetic answer');
});

test('the reply lab explains missing AI setup and retains a real submission failure in its result', async () => {
  const ui = boot(); ui.state.config.provider = 'mimo'; ui.state.key_set = false;
  const mount = await showView(ui, 'reply');
  assert.match(mount.innerHTML, /尚未填写本次运行的 MiMo API Key/);
  assert.ok(ui.document.querySelector('[data-action="modelSettings"]'));
  ui.document.getElementById('replyText').value = 'A synthetic message';
  ui.context.fetch = async url => {
    if (url === '/api/reply') return response({ job: 'ai-unavailable' });
    ui.state.jobs = [{ id: 'ai-unavailable', state: 'error', label: '生成回复预览', error: '请填写 API Key；本地服务请检查 Token' }];
    return response(ui.state);
  };
  await ui.document.querySelector('[data-action="generateReply"]').onclick();
  assert.match(ui.document.getElementById('replyOutput').textContent, /请填写 API Key/);
  assert.equal(ui.document.querySelector('[data-action="copyReply"]').disabled, true);
});

test('backup completion exposes a persistent download link when automatic download is missed', async () => {
  const ui = boot(); await showView(ui, 'backups');
  ui.run("jobTargets.b1='backupOutput'");
  ui.state.jobs = [{ id: 'b1', state: 'done', label: '完整备份（含媒体）', result: { download: '/api/download/abc123', name: 'Studio备份.zip' } }];
  ui.context.fetch = async () => response(ui.state); await ui.run('poll()');
  assert.match(ui.document.getElementById('backupOutputDownload').innerHTML, /href="\/api\/download\/abc123"/);
  const reopened = await showView(ui, 'backups');
  assert.match(reopened.innerHTML, /href="\/api\/download\/abc123" download="Studio备份.zip"/);
});

test('restore completion clears the old reply draft instead of remembering it again', async () => {
  const ui = boot(); await ui.run("render('reply')");
  ui.document.getElementById('replyText').value = 'old workspace text';
  ui.run("drafts.reply={replyText:'old workspace text'};results.replyOutput='old answer';jobTargets.r1='restoreOutput'");
  ui.state.jobs = [{ id: 'r1', state: 'done', label: '校验并恢复完整工作区', result: { safety: 'F:/synthetic/safety.zip' } }];
  ui.context.fetch = async () => response(ui.state); await ui.run('poll()');
  assert.equal(ui.document.getElementById('replyText').value, '');
  assert.equal(ui.run('results.replyOutput'), undefined); assert.equal(ui.run('drafts.reply'), undefined);
  assert.match(ui.run('results.restoreOutput'), /F:\/synthetic\/safety.zip/);
});

test('restore errors stay in the backup page and large restore uploads use their separate limit', async () => {
  const ui = boot(); await showView(ui, 'backups');
  let requests = 0;
  ui.context.fetch = async () => { requests++; return { ok: false, status: 400, json: async () => ({ error: '备份校验失败，当前工作区未替换' }) }; };
  await ui.document.getElementById('restoreFile').onchange({ target: { value: 'zip', files: [{ name: 'large.zip', size: 600 * 1024 * 1024 }] } });
  await ui.document.getElementById('confirmRestore').onclick();
  assert.equal(requests, 1, 'Restore files above the ordinary media limit must reach the validator');
  assert.match(ui.document.getElementById('restoreOutput').textContent, /备份校验失败/);
  ui.context.file = { name: 'too-large.zip', size: 10 * 1024 * 1024 * 1024 };
  await assert.rejects(ui.run("upload(file,'restore')"), /9 GB/);
  ui.context.file = { name: 'records.json', size: 600 * 1024 * 1024 };
  await assert.rejects(ui.run("upload(file,'records')"), /150 MB/);
  await assert.rejects(ui.run("upload(file,'media')"), /512 MB/);
});

test('selecting a backup explains replacement and safety backup before any upload', async () => {
  const ui = boot(); await showView(ui, 'backups'); let requests = 0;
  ui.context.confirm = () => { throw Error('Do not use a blocking native confirm'); };
  ui.context.fetch = async () => { requests++; return response({}); };
  const event = { target: { value: 'chosen.zip', files: [{ name: '<my-backup>.zip', size: 2048 }] } };
  await ui.document.getElementById('restoreFile').onchange(event);
  assert.equal(event.target.value, ''); assert.equal(requests, 0);
  assert.equal(ui.document.getElementById('dialog').open, true);
  assert.equal(ui.document.getElementById('dialogTitle').textContent, '确认恢复工作区');
  const body = ui.document.getElementById('dialogBody').innerHTML;
  assert.match(body, /&lt;my-backup&gt;\.zip/); assert.doesNotMatch(body, /<my-backup>/);
  assert.match(body, /替换当前记录、已导入媒体、个人风格、联系人记忆和模型设置/);
  assert.match(body, /先把当前工作区备份到 data\/backups/);
  assert.ok(ui.document.getElementById('cancelRestore'));
  assert.ok(ui.document.getElementById('confirmRestore'));
});

test('cancel and Escape discard the chosen restore file without uploading it', async () => {
  for (const action of ['cancel', 'escape', 'close']) {
    const ui = boot(); await showView(ui, 'backups'); let requests = 0;
    ui.context.fetch = async () => { requests++; return response({}); };
    await ui.document.getElementById('restoreFile').onchange({ target: { value: 'zip', files: [{ name: 'cancel-me.zip', size: 10 }] } });
    const oldConfirm = ui.document.getElementById('confirmRestore');
    if (action === 'cancel') await ui.document.getElementById('cancelRestore').onclick();
    else if (action === 'close') await ui.document.getElementById('closeDialog').onclick();
    else ui.document.getElementById('dialog').dispatch('cancel');
    await oldConfirm.onclick();
    assert.equal(ui.document.getElementById('dialog').open, false, action);
    assert.equal(ui.run('dialogCleanup'), null, action);
    assert.equal(requests, 0, action + ' must not upload a backup');
  }
});

test('restore confirmation uploads once and locks replacement until the accepted job finishes', async () => {
  const ui = boot(), uploading = deferred(), calls = []; await showView(ui, 'backups');
  ui.context.confirm = () => { throw Error('Do not use a blocking native confirm'); };
  ui.context.fetch = async (url, options) => {
    calls.push([url, options]);
    return url.startsWith('/api/upload?') ? uploading.promise : response(ui.state);
  };
  const file = { name: 'safe backup.zip', size: 256 };
  await ui.document.getElementById('restoreFile').onchange({ target: { value: 'zip', files: [file] } });
  const confirm = ui.document.getElementById('confirmRestore'), cancel = ui.document.getElementById('cancelRestore');
  const submitted = confirm.onclick(); await confirm.onclick();
  assert.equal(confirm.disabled, true); assert.equal(cancel.disabled, true);
  await ui.document.getElementById('closeDialog').onclick();
  assert.equal(ui.document.getElementById('dialog').open, true, 'In-progress uploads cannot be dismissed');
  assert.equal(calls.length, 1);
  const url = new URL('http://synthetic' + calls[0][0]);
  assert.equal(url.searchParams.get('name'), file.name); assert.equal(url.searchParams.get('mode'), 'restore');
  assert.equal(url.searchParams.get('confirm'), 'replace'); assert.equal(calls[0][1].body, file);
  ui.state.jobs = [{ id: 'restore1', state: 'running', label: '校验并恢复完整工作区', progress: '校验中' }];
  uploading.resolve(response({ job: 'restore1' })); await submitted;
  assert.equal(ui.document.getElementById('dialog').open, false);
  assert.equal(ui.run('fileBusy'), false); assert.equal(ui.run('pendingJobs.has("restore1")'), true);
  assert.equal(ui.document.querySelector('[data-action="restore"]').disabled, true);
  assert.equal(ui.run('jobTargets.restore1'), 'restoreOutput');
  assert.match(ui.document.getElementById('restoreOutput').textContent, /正在校验并恢复/);
});

test('restore confirmation rechecks whether another task or WeChat monitor started after selection', async () => {
  for (const blockedBy of ['task', 'monitor']) {
    const ui = boot(); await showView(ui, 'backups'); let requests = 0;
    ui.context.fetch = async () => { requests++; return response({}); };
    await ui.document.getElementById('restoreFile').onchange({ target: { value: 'zip', files: [{ name: 'wait.zip', size: 10 }] } });
    if (blockedBy === 'task') ui.state.jobs = [{ id: 'other', state: 'running' }];
    else ui.state.engine.running = true;
    await ui.document.getElementById('confirmRestore').onclick();
    assert.equal(requests, 0, blockedBy);
    assert.equal(ui.document.getElementById('dialog').open, true, blockedBy);
    assert.match(ui.document.getElementById('restoreConfirmStatus').textContent, /结束当前任务并停止微信监听/);
  }
});

test('a large export keeps its download available and clearly warns that it cannot be directly reimported', async () => {
  const ui = boot();
  ui.context.fetch = async () => response({ rows: [], total: 0 }); await ui.run("render('records')");
  ui.run("jobTargets.e1='exportOutput'");
  ui.state.jobs = [{ id: 'e1', state: 'partial', label: '导出会话', result: { download: '/api/download/export123', name: 'large-export.json', messages: 1000, warnings: ['导出文件超过150MB，当前不能直接回导。请使用完整备份迁移。'] } }];
  ui.context.fetch = async () => response(ui.state); await ui.run('poll()');
  assert.equal(ui.document.getElementById('exportOutput').hidden, false);
  assert.match(ui.document.getElementById('exportOutput').textContent, /超过150MB/);
  assert.match(ui.document.getElementById('exportOutputDownload').innerHTML, /export123/);
  assert.match(ui.document.getElementById('toast').textContent, /不能直接回导/);
});
