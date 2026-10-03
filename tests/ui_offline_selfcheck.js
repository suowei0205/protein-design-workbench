'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const UI=require('../src/pwb/assets/offline-viewer.js');
assert.equal(UI.score(''),null);assert.equal(UI.score(' '),null);assert.equal(UI.score('0'),0);assert.equal(UI.score('Infinity'),null);
assert.deepEqual(UI.orderedRows([{index:0,score:'1'},{index:1,score:''},{index:2,score:'1'},{index:3,score:'2'}],'desc').map(x=>x.index),[3,0,2,1]);
assert.deepEqual(UI.orderedRows([{index:0,score:null},{index:1,score:'0'},{index:2,score:'1'}],'asc').map(x=>x.index),[1,2,0]);
assert.throws(()=>UI.parseStructures('{}'),/数组/);assert.throws(()=>UI.parseStructures('[{"id":"x","data":"A"},{"id":"x","data":"B"}]'),/唯一/);
function harness(rawData){
 const nodes={},loaded=[],blobURLs=[],revoked=[],viewers=[],listeners={};
 const node=(extra={})=>({innerHTML:'',textContent:'',hidden:false,disabled:false,value:'',dataset:{},handlers:{},classList:{add(){}},addEventListener(type,fn){this.handlers[type]=fn;},querySelector(){return null;},replaceChildren(){this.innerHTML='';},focus(){this.focused=true;},...extra});
 const controls=node(),container=node(),tableControls=node(),first=node(),second=node(),show=node(),structureStatus=node(),search=node(),sort=node({value:'original'}),pageLabel=node(),previous=node({dataset:{offlinePage:'previous'}}),next=node({dataset:{offlinePage:'next'}}),rowStatus=node();
 controls.querySelector=selector=>({'[name="offline-first"]':first,'[name="offline-second"]':second,'[data-offline-show]':show,'[data-offline-status]':structureStatus}[selector]||null);
 tableControls.querySelector=selector=>({'[name="offline-search"]':search,'[name="offline-sort"]':sort,'[data-offline-page-label]':pageLabel,'[data-offline-page="previous"]':previous,'[data-offline-page="next"]':next,'[data-offline-row-status]':rowStatus}[selector]||null);
 for(let i=0;i<2;i++)for(const prefix of ['offline-molecule','offline-chains','offline-model-status','offline-download'])nodes[`${prefix}-${i}`]=node();
 const rows=Array.from({length:127},(_,i)=>node({textContent:`Candidate R${i}`,dataset:{candidateId:`R${i}`,score:i===3?'':String(126-i),search:`Candidate R${i} group${i%3}`}}));
 const originalAttributes=rows.map(r=>JSON.stringify(r.dataset));
 const tbody={rows:rows.slice(),appendChild(row){this.rows=this.rows.filter(r=>r!==row);this.rows.push(row);}};
 const table=node({tBodies:[tbody]});
 const document={currentScript:{src:'file:///tmp/offline/assets/offline-viewer.js'},head:{appendChild(script){if(script.tagName==='script'){loaded.push(script.src);window.$3Dmol={createViewer};script.onload();}}},createElement(tagName){return node({tagName,remove(){}});},getElementById(id){return nodes[id]||null;},querySelector(selector){return {'#offline-structure-controls':controls,'#offline-structures':container,'script#structure-data[type="application/json"]':{textContent:rawData},'table[data-offline-candidates]':table,'#offline-candidate-controls':tableControls}[selector]||null;}};
 const window={addEventListener(type,fn){listeners[type]=fn;}};
 function createViewer(element){const viewer={styles:[],cleared:false,clear(){this.cleared=true;},setStyle(selection,style){this.styles.push({selection,style});},addModel(text,format){this.text=text;this.format=format;return {selectedAtoms(){return [{chain:'A'},{chain:'B'}];}};},zoomTo(){},render(){}};viewers.push(viewer);return viewer;}
 class LocalURL extends URL{static createObjectURL(blob){const url=`blob:TEST-${blobURLs.length}`;blobURLs.push({url,blob});return url;}static revokeObjectURL(url){revoked.push(url);}}
 const context={window,document,URL:LocalURL,Blob,console,Set,Map,fetch(){throw new Error('Offline companion must not fetch anything');}};
 const source=fs.readFileSync(path.join(__dirname,'../src/pwb/assets/offline-viewer.js'),'utf8');vm.runInNewContext(source,context);
 const clickShow=()=>controls.handlers.click({target:{closest(){return show;}}});
 const clickPage=button=>tableControls.handlers.click({target:{closest(){return button;}}});
 return {window,nodes,controls,container,loaded,blobURLs,revoked,viewers,listeners,first,second,show,structureStatus,search,sort,pageLabel,previous,next,rowStatus,tableControls,tbody,rows,originalAttributes,clickShow,clickPage};
}
async function settle(){await new Promise(resolve=>setImmediate(resolve));}
async function main(){
 const a=harness(JSON.stringify([{id:'pdb-candidate',format:'pdb',data:'RAW PDB',target_chains:['A'],binder_chains:['B']},{id:'cif-candidate',format:'cif',data:'RAW CIF'},{id:'third',format:'pdb',data:'NOT SELECTED'}]));
 assert.equal(a.loaded.length,0,'library not loaded by report initialization');assert.match(a.controls.innerHTML,/最多 2 个/);
 assert.equal(a.tbody.rows.filter(r=>!r.hidden).length,50);assert.equal(a.previous.disabled,true);assert.match(a.pageLabel.textContent,/第 1 \/ 3 页/);
 a.clickPage(a.next);assert.equal(a.tbody.rows.filter(r=>!r.hidden).length,50);assert.equal(a.tbody.rows.find(r=>!r.hidden).dataset.candidateId,'R50');assert.match(a.pageLabel.textContent,/第 2 \/ 3 页/);
 a.clickPage(a.next);assert.equal(a.tbody.rows.filter(r=>!r.hidden).length,27);assert.equal(a.next.disabled,true);
 a.search.value='group0';a.tableControls.handlers.input({target:{name:'offline-search'}});assert.match(a.pageLabel.textContent,/第 1 \/ 1 页/);assert.equal(a.tbody.rows.filter(r=>!r.hidden).length,43);
 a.search.value='';a.tableControls.handlers.input({target:{name:'offline-search'}});a.clickPage(a.next);a.sort.value='asc';a.tableControls.handlers.change({target:{name:'offline-sort'}});assert.match(a.pageLabel.textContent,/第 1 \/ 3 页/);assert.equal(a.tbody.rows[0].dataset.candidateId,'R126');assert.equal(a.tbody.rows.at(-1).dataset.candidateId,'R3','missing score stays last even ascending');
 assert.deepEqual(a.rows.map(r=>JSON.stringify(r.dataset)),a.originalAttributes,'search/sort/page do not change source attributes');
 a.clickShow();assert.match(a.structureStatus.textContent,/至少选择/);assert.equal(a.loaded.length,0);
 a.first.value='pdb-candidate';a.second.value='pdb-candidate';a.clickShow();assert.match(a.structureStatus.textContent,/不同候选/);assert.equal(a.viewers.length,0);
 a.second.value='cif-candidate';a.clickShow();await settle();assert.deepEqual(a.loaded,['file:///tmp/offline/assets/3Dmol-min.js']);assert.equal(a.viewers.length,2);assert.equal(a.viewers[0].text,'RAW PDB');assert.equal(a.viewers[0].format,'pdb');assert.equal(a.viewers[1].format,'cif');assert.equal(a.show.focused,true);
 assert.match(a.nodes['offline-chains-0'].innerHTML,/data-chain="B"/);assert.match(a.nodes['offline-model-status-0'].textContent,/没有坐标叠合/);assert.equal(a.nodes['offline-download-0'].download,'pdb-candidate.pdb');assert.equal(await a.blobURLs[0].blob.text(),'RAW PDB');
 a.container.handlers.change({target:{dataset:{offlineView:'0',chain:'B'},checked:false}});assert.equal(a.viewers[0].styles.at(-1).selection.chain,'B');assert.equal(Object.keys(a.viewers[0].styles.at(-1).style).length,0);
 a.clickShow();await settle();assert.equal(a.viewers.filter(v=>!v.cleared).length,2,'new selection disposes previous two viewers');assert.equal(a.revoked.length,2);
 a.window.$3Dmol={createViewer(){throw new Error('TEST WebGL unavailable');}};a.clickShow();await settle();assert.equal(a.viewers.filter(v=>!v.cleared).length,0);assert.match(a.nodes['offline-model-status-0'].textContent,/TEST WebGL unavailable/);assert.match(a.nodes['offline-model-status-0'].textContent,/原始结构下载/);assert.match(a.nodes['offline-download-0'].href,/^blob:/);
 a.listeners.pagehide();assert.equal(a.revoked.length,a.blobURLs.length,'all download object URLs revoked on pagehide');
 const broken=harness('{}');assert.match(broken.controls.textContent,/JSON 数组/);assert.equal(broken.loaded.length,0);
 console.log('PASS: offline file URL library path, embedded-only PDB/CIF handlers, max-two lifecycle, chain toggle, keyboard focus, download/WebGL fallback, stable raw-score ordering and 50-row search/sort pagination. No browser/WebGL acceptance implied.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
