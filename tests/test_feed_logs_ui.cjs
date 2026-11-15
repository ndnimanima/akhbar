// Bulk source checking is bounded, reports partial failures, and stays in place.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const dom = new JSDOM(fs.readFileSync('static/index.html', 'utf8'), {url:'https://example.test/#/feeds',runScripts:'outside-only'});
const {window} = dom, {document} = window;
let scrolls=0, pending=0, peak=0, calls=[];
window.scrollTo=()=>{scrolls++};
const feeds=Array.from({length:5},(_,i)=>({id:i+1,title:'Source '+(i+1),url:'https://example.test/'+i,enabled:i===0?0:1,show_url:1}));
window.fetch=async(url,opts={})=>{
 let data;
 if(url==='/api/meta') data={authenticated:true,defaults:{},themes:['slate','amber','aurora','ocean'].map(id=>({id})),platforms:[]};
 else if(url==='/api/feeds') data={feeds};
 else if (/^\/api\/feeds\/\d+\/check$/.test(url)) {
   const id=+url.split('/')[3]; calls.push(id); pending++; peak=Math.max(peak,pending);
   await new Promise(r=>setTimeout(r,15)); pending--;
   data=id===2?{ok:false,error:'broken'}:{ok:true,total:4,in_window:3};
 } else if(url==='/api/logs?limit=300') data={logs:[{level:'info',message:'good event'},{level:'error',message:'bad event'}]};
 else if(url==='/api/settings') data={ok:true};
 else throw Error('Unexpected '+url);
 return {ok:true,status:200,json:async()=>data};
};
window.eval(['static/assets/i18n.js','static/assets/app.js'].map(f=>fs.readFileSync(f,'utf8')).join('\n'));
const tick=()=>new Promise(r=>setTimeout(r,5));
async function waitFor(f){for(let i=0;i<200;i++){if(f())return;await tick()}throw Error('timeout')}
(async()=>{
 await waitFor(()=>document.querySelector('[data-act="feed.checkAll"]'));
 const count=scrolls;
 document.querySelector('[data-act="feed.checkAll"]').click();
 await waitFor(()=>document.querySelector('#feedProgress').textContent.includes('۵'));
 assert.ok(document.querySelector('[data-act="feed.checkAll"]').disabled);
 await waitFor(()=>calls.length===5 && pending===0 && !document.querySelector('[data-act="feed.checkAll"]').disabled);
 assert.equal(peak,3); assert.deepEqual(calls.slice().sort(),[1,2,3,4,5]);
 assert.equal(scrolls,count); assert.equal(document.querySelectorAll('.chip.ok').length,4);
 assert.equal(document.querySelectorAll('.chip.bad').length,1);
 document.querySelector('[data-act="feed.check"]').click();
 await waitFor(()=>calls.length===6 && pending===0);
 assert.equal(scrolls,count);
 document.querySelector('[data-group="logs"] .mlink').click();
 await waitFor(()=>document.querySelector('#logsSearch'));
 assert.equal(document.querySelectorAll('.logline').length,2);
 const search=document.querySelector('#logsSearch');search.value='bad';search.dispatchEvent(new window.Event('input',{bubbles:true}));
 assert.equal(document.querySelectorAll('.logline').length,1);
 assert.match(document.querySelector('#logsList').textContent,/bad event/);
 document.querySelector('#themeBtn').click();
 assert.equal(document.documentElement.dataset.theme,'amber');
 document.querySelector('#themeBtn').click();
 assert.equal(document.documentElement.dataset.theme,'aurora');
 document.querySelector('#themeBtn').click();
 assert.equal(document.documentElement.dataset.theme,'ocean');
 console.log('feed checks, logs, scroll and themes OK'); dom.window.close();
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1});
