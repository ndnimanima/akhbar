// Expanding or closing history must not rerender the page, scroll to top or fetch data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { JSDOM } = require('jsdom');
const dom = new JSDOM(fs.readFileSync('static/index.html', 'utf8'), {url:'https://example.test/#/video',runScripts:'outside-only'});
const {window} = dom, {document} = window;
let scrolls=0, historyLoads=0;
window.scrollTo=()=>{ scrolls++; };
const job = {id:17,status:'done',stage:'done',mode:'manual',title:'A report',created_at:'2026-01-01',report:{title:'Example',summary:'Real report content',frames:[]}};
window.fetch=async(url)=>{
 let data;
 if (url==='/api/meta') data={authenticated:true,defaults:{},platforms:[]};
 else if (url.startsWith('/api/video/snapshot')) {historyLoads++;data={status:{settings:{},ffmpeg:true},analyses:[job]};}
 else throw Error('Unexpected request '+url);
 return {ok:true,status:200,json:async()=>data};
};
window.eval(['static/assets/i18n.js','static/assets/app.js','static/assets/video.js'].map(f=>fs.readFileSync(f,'utf8')).join('\n'));
const tick=()=>new Promise(r=>setTimeout(r,0));
async function waitFor(f) {for(let i=0;i<100;i++){if(f())return;await tick()}throw Error('timeout');}
(async()=>{
 await waitFor(()=>document.querySelector('[data-act="video.toggle"]'));
 const before=scrolls, loads=historyLoads;
 const button=()=>document.querySelector('[data-act="video.toggle"]');
 assert.equal(button().getAttribute('aria-expanded'),'false');
 button().click(); await tick();
 assert.equal(button().getAttribute('aria-expanded'),'true');
 assert.ok(document.querySelector('.vjob-body').textContent.includes('Real report content'));
 assert.equal(scrolls,before);assert.equal(historyLoads,loads);
 button().click(); await tick();
 assert.equal(button().getAttribute('aria-expanded'),'false');
 assert.equal(document.querySelector('.vjob-body'),null);
 assert.equal(scrolls,before);assert.equal(historyLoads,loads);
 document.querySelector('[data-act="video.refresh"]').click();
 await waitFor(()=>historyLoads>loads); await tick();
 assert.equal(historyLoads,loads+1, 'refresh requires only one snapshot');
 console.log('video history opens in place'); dom.window.close();
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1});
