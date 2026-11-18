// End-to-end DOM wiring with a mocked backend, without any real API keys.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { JSDOM } = require('jsdom');
const dom = new JSDOM(fs.readFileSync('static/index.html', 'utf8'), {url: 'https://example.test/#/video', runScripts: 'outside-only'});
const {window} = dom, {document} = window;
window.scrollTo = () => {};
const calls = [];
let uploadXhr;
window.XMLHttpRequest = class {
  constructor() { this.upload = {}; uploadXhr = this; }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader() {}
  send(file) { this.file = file; calls.push({url:this.url, method:this.method, body:file}); }
  abort() { this.onabort(); }
};
let cfg = { smart: false, interval_minutes: 30, ai_base_url: 'https://api.openai.com/v1', ai_model: 'gpt-4o-mini', ai_key_set: false, ai_vision: false };
window.fetch = async (url, init) => {
  calls.push({url, method: init.method, body: typeof init.body === 'string' ? JSON.parse(init.body) : init.body});
  let result = {};
  if (url === '/api/meta') result = {authenticated: true, defaults: {}, platforms: []};
  else if (url.startsWith('/api/video/snapshot')) result = {status:{settings:cfg, ffmpeg:true}, analyses:[]};
  else if (url === '/api/video/ai-settings') {
    const b = JSON.parse(init.body);
    cfg = {...cfg, ai_provider:b.provider, ai_base_url:b.base_url, ai_model:b.model, ai_vision:b.vision, ai_key_set:b.clear_key ? false : !!b.api_key || cfg.ai_key_set};
    result = {ok:true, settings:cfg};
  } else if (url === '/api/video/analyze') result = {ok:true, job_id:9};
  else if (url.startsWith('/api/video/upload?')) result = {ok:true, job_id:10};
  else if (url === '/api/video/settings') { cfg = {...cfg, ...JSON.parse(init.body)}; result = {ok:true, settings:cfg}; }
  else if (url === '/api/video/ai-test') result = {ok:true};
  else throw new Error('Unexpected ' + url);
  return {ok:true, status:200, json:async () => result};
};
window.eval(['static/assets/i18n.js','static/assets/app.js','static/assets/video.js'].map(f => fs.readFileSync(f, 'utf8')).join('\n'));
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
async function waitFor(fn) { for (let i=0; i<100; i++) { if (fn()) return; await tick(); } throw new Error('timed out'); }
const q = s => {const el = document.querySelector(s); assert.ok(el, s); return el;};
(async () => {
  await waitFor(() => document.querySelector('#videoAiSettings'));
  q('#videoAiSettings [name="api_key"]').value = 'secret';
  q('#videoAiSettings [name="vision"]').checked = true;
  q('#videoAiSettings [type="submit"]').click();
  await waitFor(() => cfg.ai_key_set && document.querySelector('[data-act="video.aiClear"]'));
  assert.equal(q('#videoAiSettings [name="api_key"]').value, '');
  assert.ok(!document.body.innerHTML.includes('secret'));
  assert.equal(cfg.ai_vision, true);
  q('[data-act="video.aiTest"]').click();
  await waitFor(() => calls.some(c => c.url === '/api/video/ai-test'));
  const url = q('#videoQuickForm [name="url"]');
  url.value = 'https://youtube.com.evil.test/watch?v=abcdefghi01';
  q('#videoQuickForm button').click(); await tick();
  assert.equal(calls.filter(c => c.url === '/api/video/analyze').length, 0);
  url.value = 'https://www.youtube.com/watch?v=abcdefghi01';
  q('#videoQuickForm button').click();
  await waitFor(() => calls.some(c => c.url === '/api/video/analyze'));
  const request = calls.find(c => c.url === '/api/video/analyze');
  assert.deepEqual(request.body, {url: 'https://www.youtube.com/watch?v=abcdefghi01', mode: 'manual'});
  assert.equal(cfg.smart, false, 'quick request must not flip smart mode');
  await tick();
  q('[data-act="video.smart"]').click();
  await waitFor(() => cfg.smart && document.querySelector('[data-act="video.aiClear"]'));
  assert.ok(cfg.ai_key_set);
  await tick(); await tick();
  const upload = q('#videoFile');
  Object.defineProperty(upload, 'files', {value: [new window.File(['video'], 'sample.mp4', {type:'video/mp4'})]});
  upload.dispatchEvent(new window.Event('change', {bubbles:true}));
  await waitFor(() => calls.some(c => c.url.startsWith('/api/video/upload?')));
  uploadXhr.upload.onprogress({loaded: 2, total: 5, lengthComputable:true});
  assert.equal(q('#videoUploadProgress progress').value, 40);
  assert.equal(q('[data-act="video.pick"]').disabled, true);
  uploadXhr.upload.onprogress({loaded: 5, total: 5, lengthComputable:true});
  assert.equal(q('#videoUploadProgress progress').value, 100);
  assert.ok(q('#videoUploadProgress').textContent.includes('آپلود کامل'));
  uploadXhr.status=200; uploadXhr.responseText=JSON.stringify({ok:true,job_id:10}); uploadXhr.onload();
  await tick(); await tick();
  assert.equal(window.Rozaneh.state.video.activeJob, 10);
  assert.ok(!document.querySelector('#toasts').textContent.includes('render is not defined'));
  const secondUpload = q('#videoFile');
  Object.defineProperty(secondUpload, 'files', {value: [new window.File(['sample'], 'second.mp4', {type:'video/mp4'})]});
  secondUpload.dispatchEvent(new window.Event('change', {bubbles:true}));
  await waitFor(() => document.querySelector('[data-act="video.uploadCancel"]'));
  q('[data-act="video.uploadCancel"]').click(); await tick();
  assert.equal(document.querySelector('#videoUploadProgress progress'), null);
  assert.equal(q('[data-act="video.pick"]').disabled, false);
  const provider = q('#videoAiSettings [name="provider"]');
  provider.value = 'gemini'; provider.dispatchEvent(new window.Event('change', {bubbles:true}));
  assert.equal(q('#videoAiSettings [name="base_url"]').value, 'https://generativelanguage.googleapis.com/v1beta');
  assert.equal(q('#videoAiSettings [name="model"]').value, 'gemini-2.5-flash');
  q('#videoAiSettings [name="api_key"]').value = 'new-secret';
  q('#videoAiSettings [type="submit"]').click();
  await waitFor(() => cfg.ai_provider === 'gemini'); await tick();
  const lastSettings = calls.filter(c => c.url === '/api/video/ai-settings').at(-1);
  assert.equal(lastSettings.body.provider, 'gemini');
  assert.equal(lastSettings.body.api_key, 'new-secret');
  assert.equal(cfg.smart, true, 'changing provider must not disable smart mode');
  await tick();
  console.log('video settings and quick analysis UI OK');
  dom.window.close();
})().catch(e => {console.error(e); dom.window.close(); process.exitCode=1;});
