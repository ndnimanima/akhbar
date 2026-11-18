/* Verify polling transfers full reports only for newly completed jobs. */
const assert = require('assert');
const vm = require('vm');
const fs = require('fs');
let tick, cleared = false, page;
const calls = [];
const state = { route: 'video', video: {status: null, settings: null, jobs: [], polling: null, activeJob: 1} };
const reports = {1: {title:'previous', frames:[{thumb:'a'}], duration:2}, 2: {title:'finished', frames:[{thumb:'b'}], duration:3}};
let finished = false, fail = false, failReport = false, pause = null;
const base = (id, status) => ({id, title: `job ${id}`, url:'https://example.test/v.mp4', status, mode:'manual', stage: status === 'done' ? 'done' : 'frames', created_at:'2025-01-01'});
async function api(path) {
  calls.push(path);
  if (path.includes('brief=true') && fail) { fail = false; throw Error('network'); }
  if (path.includes('brief=true') && pause) await pause;
  if (path === '/api/video/snapshot?limit=30') return {status:{settings:{},ffmpeg:true}, analyses:[{...base(2,'running'),report:{}},{...base(1,'done'),report:reports[1]}]};
  if (path === '/api/video/snapshot?limit=30&brief=true') return {status:{settings:{},ffmpeg:true}, analyses:[base(2, finished?'done':'running'),base(1,'done')]};
  if (path === '/api/video/jobs/2' && failReport) { failReport = false; throw Error('network'); }
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
  assert.deepEqual(calls,['/api/video/snapshot?limit=30&brief=true']);
  assert.strictEqual(state.video.jobs[1].report,reports[1]);
  assert.match(elements['#videoHistoryBody'].innerHTML,/previous/);
  // A transient failure keeps the timer alive and the next poll recovers.
  fail=true;
  await tick();
  assert.equal(cleared, false);
  await tick();
  assert.equal(state.video.jobs[0].status, 'running');
  // No second request while the first one is pending.
  let release;
  pause = new Promise(resolve => { release = resolve; });
  calls.length=0;
  const pending = tick();
  await tick();
  assert.equal(calls.length, 1);
  state.route = 'feeds';
  release(); await pending; pause=null;
  assert.equal(cleared, true);
  assert.equal(state.video.jobs[0].status, 'running');
  // Returning to the page starts a fresh timer and reads current data.
  state.route = 'video'; cleared = false;
  await page({innerHTML:''});
  // A refresh while a poll is in flight supersedes the older response.
  pause = new Promise(resolve => { release = resolve; });
  const oldPoll = tick();
  await page({innerHTML:''});
  const refreshedJobs = state.video.jobs;
  release(); await oldPoll; pause = null;
  assert.strictEqual(state.video.jobs, refreshedJobs);
  assert.equal(cleared, false);
  finished=true;
  failReport=true;
  await tick();
  assert.equal(state.video.jobs[0].status, 'running', 'failed report fetch must be retried');
  assert.equal(cleared, false);
  calls.length=0;
  await tick();
  assert.deepEqual(calls,['/api/video/snapshot?limit=30&brief=true','/api/video/jobs/2']);
  assert.strictEqual(state.video.jobs[0].report,reports[2]);
  assert.ok(cleared);
  console.log('video polling: compact updates, preserved history and completed report OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
