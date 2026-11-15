// DOM integration tests: run with jsdom available on NODE_PATH.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('static/index.html', 'utf8');
const dom = new JSDOM(html, { url: 'https://example.test/#/feeds', runScripts: 'outside-only' });
const { window } = dom;
const { document } = window;
window.scrollTo = () => {};
let feeds = [], writes = [], failWrite = false, release;
window.fetch = async (url, init) => {
  let data = {};
  if (url === '/api/meta') data = { authenticated: true, defaults: {}, platforms: [] };
  else if (url === '/api/feeds' && init.method === 'GET') data = { feeds };
  else if (url === '/api/feeds' && init.method === 'POST') {
    writes.push(JSON.parse(init.body));
    if (release) await new Promise(resolve => { release.resolve = resolve; });
    if (failWrite) return { ok: false, status: 400, json: async () => ({ detail: 'save_failed' }) };
    feeds.push({ id: 1, ...writes.at(-1) });
    data = { ok: true, id: 1 };
  } else if (url === '/api/feeds/1' && init.method === 'PATCH') {
    writes.push(JSON.parse(init.body));
    Object.assign(feeds[0], writes.at(-1));
    data = { ok: true };
  } else if (url === '/api/feeds/1' && init.method === 'DELETE') {
    writes.push('delete'); feeds = []; data = { ok: true };
  } else if (url === '/api/topics') data = { topics: [] };
  return { ok: true, status: 200, json: async () => data };
};
window.eval(fs.readFileSync('static/assets/i18n.js', 'utf8') + '\n' + fs.readFileSync('static/assets/app.js', 'utf8'));
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
async function waitFor(predicate) {
  for (let i = 0; i < 100; i++) { if (predicate()) return; await tick(); }
  throw new Error('Timed out waiting for UI');
}
const q = selector => { const el = document.querySelector(selector); assert.ok(el, selector); return el; };
const click = selector => q(selector).click();
const opened = () => !!document.querySelector('.modal');
const openFeed = () => click('#view [data-act="feed.new"]');
const save = () => click('.modal-foot [data-act="submit"]');

(async () => {
  await waitFor(() => document.querySelector('#view [data-act="feed.new"]'));
  openFeed();
  // Ordinary clicks and controls must not dismiss or have their defaults cancelled.
  q('.modal-body').click();
  assert.ok(opened());
  const input = q('#feedForm [name="title"]');
  assert.equal(input.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })), true);
  assert.ok(opened());
  click('[data-act="feed.color"][data-color="sky"]');
  assert.ok(q('[data-act="feed.color"][data-color="sky"]').classList.contains('on'));
  click('[data-act="feed.emoji"]');
  const icon = q('#feedIcon'); icon.value = '⭐';
  click('[data-tg="show_url"]');
  assert.equal(q('[data-tg="show_url"]').getAttribute('aria-pressed'), 'false');
  save(); // required URL is empty; do not send anything or close
  assert.equal(writes.length, 0); assert.ok(opened());
  q('#feedForm [name="url"]').value = 'not-a-url'; save();
  assert.equal(writes.length, 0); assert.ok(opened());
  input.value = 'Test'; q('#feedForm [name="url"]').value = 'https://example.com/rss';
  release = {}; save(); save();
  assert.equal(writes.length, 1, 'pending form must not be submitted twice');
  assert.deepEqual(writes[0], { title: 'Test', url: 'https://example.com/rss', icon: '⭐', color: 'sky', show_url: 0, enabled: 1 });
  release.resolve(); release = null;
  await waitFor(() => !opened() && document.querySelector('[data-act="feed.edit"]'));
  click('[data-act="feed.edit"]');
  q('#feedIcon').value = '🌟'; save();
  await waitFor(() => !opened() && writes.length === 2);
  assert.equal(writes[1].icon, '🌟');
  openFeed(); click('.modal-foot [data-act="modal.close"]'); assert.ok(!opened());
  openFeed(); click('.modal-head [data-act="modal.close"]'); assert.ok(!opened());
  openFeed(); click('.modal-back'); assert.ok(!opened());
  openFeed(); q('#feedForm [name="url"]').value = 'https://example.com/rss2';
  failWrite = true; save();
  await waitFor(() => document.querySelector('#toasts')?.textContent.includes('save_failed'));
  assert.ok(opened(), 'error should leave the form open');
  click('.modal-foot [data-act="modal.close"]');
  click('[data-act="feed.del"]'); click('[data-act="modal.yes"]');
  await waitFor(() => writes.includes('delete') && document.querySelector('#view [data-act="feed.new"]'));
  await tick();
  click('.mhead[data-act="mobile.toggle"]');
  assert.ok(q('.mhead[data-act="mobile.toggle"]').closest('.mgroup').classList.contains('open'));
  let moduleCalls = 0;
  window.Rozaneh.forms.regression = async () => { moduleCalls++; };
  const form = document.createElement('form');
  form.id = 'regressionForm'; form.dataset.form = 'regression';
  form.innerHTML = '<input name="required" required><button type="submit" data-act="submit" data-form-id="regressionForm"><span>Save</span></button>';
  document.body.appendChild(form);
  form.querySelector('button span').click();
  assert.equal(moduleCalls, 0);
  form.querySelector('input').value = 'ok';
  form.querySelector('button span').click();
  await tick(); assert.equal(moduleCalls, 1, 'button inside form submits exactly once');
  form.requestSubmit();
  await tick(); assert.equal(moduleCalls, 2, 'native submit works');
  console.log('modal and form actions OK');
  dom.window.close();
})().catch(err => { console.error(err); dom.window.close(); process.exitCode = 1; });
