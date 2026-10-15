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
  const start = source.lastIndexOf('(async()=>{try{await api(');
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
