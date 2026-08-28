// Run: node --test main/js_tests/lead-form.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const source = readFileSync(require('node:path').join(__dirname, '../static/main/lead-form.js'), 'utf8');

function setup(options = {}) {
  const goals = [], requests = [];
  const element = () => ({
    listeners: {}, attrs: {}, value: '', validity: { valid: true },
    classList: { add() {}, remove() {} },
    addEventListener(type, callback) { this.listeners[type] = callback; },
    setAttribute(key, value) { this.attrs[key] = value; },
    removeAttribute(key) { delete this.attrs[key]; },
    focus() {}, appendChild() {},
  });
  const form = element(), success = element(), section = element(), dialog = element();
  const button = element(), error = element(), optional = element(), trigger = element(), close = element();
  button.textContent = 'Получить оценку проекта';
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dialog.listeners.close(); };
  dialog.querySelector = () => close;
  section.querySelector = () => element();
  form.querySelector = s => s === '.lead-error' ? error : s === '.lead-optional' ? optional : button;
  form.dataset = { metrikaId: '111680333' };
  form.elements = Object.fromEntries(['contact', 'name', 'message', 'page_url', 'page_title',
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'].map(k => [k, element()]));
  form.reportValidity = () => options.valid !== false;
  form.action = '/request/';
  let observe;
  const storage = new Map(Object.entries(options.storage || {}));
  const context = {
    document: {
      getElementById: id => ({ 'lead-form': form, 'lead-success': success, request: section, 'lead-dialog': dialog })[id],
      querySelectorAll: () => [trigger], body: element(), title: 'Test',
    },
    URLSearchParams, FormData: class {},
    sessionStorage: { getItem: k => storage.get(k), setItem: (k,v) => storage.set(k,v) },
    IntersectionObserver: class {
      constructor(callback) { observe = callback; }
      observe() {} disconnect() {}
    },
    fetch: (...args) => {
      requests.push(args);
      return options.fetch ? options.fetch() : Promise.resolve({ ok: true, json: async () => ({ ok: true, lead_id: 123 }) });
    },
  };
  context.window = { IntersectionObserver: context.IntersectionObserver,
    location: { search: options.search || '', href: 'https://maksonchik.ru/' },
    ym: (...args) => { if (options.analyticsFails) throw Error('blocked'); goals.push(args); },
  };
  vm.runInNewContext(source, context);
  return { form, success, button, error, dialog, trigger, goals, requests,
    visible: () => observe([{ isIntersecting: true }]),
    input: (name = 'contact', value = '@example') => form.listeners.input({ target: { name, value } }),
    submit: () => form.listeners.submit({ preventDefault() {} }),
    flush: () => new Promise(resolve => setImmediate(resolve)),
  };
}

test('view and start are counted once, not on load or empty/honeypot input', () => {
  const x = setup();
  assert.equal(x.goals.length, 0);
  x.visible(); x.visible(); x.input('company'); x.input('contact', ' ');
  x.input(); x.input('name');
  assert.deepEqual(x.goals.map(g => g[2]), ['lead_form_open', 'lead_form_start']);
  assert.equal(JSON.stringify(x.goals).includes('@example'), false);
});

test('modal opens and closes without resetting the form', () => {
  const x = setup();
  x.form.elements.contact.value = '@test';
  x.trigger.listeners.click({ preventDefault() {} });
  assert.equal(x.dialog.open, true);
  assert.equal(x.goals[0][3].placement, 'modal');
  x.dialog.close();
  x.trigger.listeners.click({ preventDefault() {} });
  assert.equal(x.form.elements.contact.value, '@test');
  assert.equal(x.goals.length, 1);
});

test('success is recorded only after server acknowledgment and once per submission', async () => {
  let resolve;
  const x = setup({ fetch: () => new Promise(r => { resolve = r; }) });
  x.submit(); x.submit();
  assert.equal(x.requests.length, 1);
  assert.equal(x.goals.some(g => g[2] === 'lead_sent'), false);
  resolve({ ok: true, json: async () => ({ ok: true, lead_id: 3 }) });
  await x.flush();
  x.submit();
  assert.equal(x.requests.length, 1);
  assert.deepEqual(x.goals.map(g => g[2]), ['lead_form_open', 'lead_form_start', 'lead_sent']);
  assert.equal(x.form.inert, true);
});

test('invalid form sends neither a request nor success', () => {
  const x = setup({ valid: false }); x.submit();
  assert.equal(x.requests.length, 0);
  assert.equal(x.goals.length, 0);
});

test('failed request allows retry and does not produce a successful lead', async () => {
  let fail = true;
  const x = setup({ fetch: () => fail ? Promise.reject(Error('network')) : Promise.resolve({ok: true, json: async () => ({ok: true, lead_id: 1})}) });
  x.submit(); await x.flush();
  assert.equal(x.button.disabled, false);
  assert.equal(x.error.textContent, 'network');
  assert.equal(x.goals.some(g => g[2] === 'lead_sent'), false);
  fail = false; x.submit(); await x.flush();
  assert.equal(x.goals.filter(g => g[2] === 'lead_sent').length, 1);
});

test('honeypot response never counts as lead_sent', async () => {
  const x = setup({ fetch: () => Promise.resolve({ok: true, json: async () => ({ok: true})}) });
  x.submit(); await x.flush();
  assert.equal(x.goals.some(g => g[2] === 'lead_sent'), false);
});

test('analytics failure cannot turn a saved lead into a form error', async () => {
  const x = setup({ analyticsFails: true }); x.submit(); await x.flush();
  assert.equal(x.form.inert, true);
  assert.equal(x.error.textContent, '');
});

test('UTM survives internal navigation and a new tagged campaign replaces it', () => {
  const storage = { lead_attribution: JSON.stringify({utm_source: 'yandex', utm_campaign: 'old'}) };
  assert.equal(setup({storage}).form.elements.utm_campaign.value, 'old');
  const x = setup({storage, search: '?utm_source=telegram&utm_campaign=new'});
  assert.equal(x.form.elements.utm_source.value, 'telegram');
  assert.equal(x.form.elements.utm_campaign.value, 'new');
  assert.equal(x.form.elements.utm_term.value, '');
});
