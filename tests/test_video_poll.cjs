/* Verify polling transfers full reports only for newly completed jobs. */
const assert = require('assert');
const vm = require('vm');
const fs = require('fs');
let tick, cleared = false, page;
const calls = [];
const state = { route: 'video', video: {status: null, settings: null, jobs: [], polling: null, activeJob: 1} };
const reports = {1: {title:'previous', frames:[{thumb:'a'}], duration:2}, 2: {title:'finished', frames:[{thumb:'b'}], duration:3}};
let finished = false;
const base = (id, status) => ({id, title: `job ${id}`, url:'https://example.test/v.mp4', status, mode:'manual', stage: status === 'done' ? 'done' : 'frames', created_at:'2025-01-01'});
async function api(path) {
  calls.push(path);
  if (path === '/api/video/status') return {settings:{}, ffmpeg:true};
  if (path === '/api/video/analyses?limit=30') return {analyses:[{...base(2,'running'),report:{}},{...base(1,'done'),report:reports[1]}]};
  if (path === '/api/video/analyses?limit=30&brief=true') return {analyses:[base(2, finished?'done':'running'),base(1,'done')]};
  if (path === '/api/video/jobs/2') return {job:{...base(2,'done'), report:reports[2]}};
  throw new Error('unexpected request '+path);
}
const elements = {'#videoHistoryBody': {dataset:{},innerHTML:''}, '#videoProgBox': {dataset:{},innerHTML:''}};
const id = x => String(x ?? '');
const bridge = {
  $: s => elements[s] || null, h:id, t:id, num:id, ico:()=>'', api, toast:()=>{}, errMsg:id,
  confirmModal:()=>{}, field:()=>'', card:()=>'', pageHead:()=>'', toggleEl:()=>'', emptyBox:()=>'',
  fmtDate:id, fmtLogTime:id, ICO_CHEV:'', state, reload:()=>{}, fd:()=>({}), actions:{}, forms:{},
  registerPage:(name, fn) => { assert.equal(name,'video'); page=fn; },
};
const context = {window:{Rozaneh:bridge}, setInterval:(fn)=>{tick=fn; return 12;}, clearInterval:()=>{cleared=true;}, console, navigator:{}};
vm.runInNewContext(fs.readFileSync('static/assets/video.js','utf8'),context);
(async () => {
  await page({innerHTML:''});
  assert.ok(tick);
  calls.length=0;
  await tick();
  assert.deepEqual(calls,['/api/video/status','/api/video/analyses?limit=30&brief=true']);
  assert.strictEqual(state.video.jobs[1].report,reports[1]);
  assert.match(elements['#videoHistoryBody'].innerHTML,/previous/);
  finished=true;
  calls.length=0;
  await tick();
  assert.deepEqual(calls,['/api/video/status','/api/video/analyses?limit=30&brief=true','/api/video/jobs/2']);
  assert.strictEqual(state.video.jobs[0].report,reports[2]);
  assert.ok(cleared);
  console.log('video polling: compact updates, preserved history and completed report OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
