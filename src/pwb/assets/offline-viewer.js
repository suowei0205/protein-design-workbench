/* Offline report companion: embedded data only, no fetch/CDN and no coordinate alignment. */
(function(root){
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function score(value){if(value===null||value===undefined||String(value).trim()==='')return null;const n=Number(value);return Number.isFinite(n)?n:null;}
  function orderedRows(rows,direction='original'){
    const sorted=rows.slice();if(direction==='original')return sorted.sort((a,b)=>a.index-b.index);
    return sorted.sort((a,b)=>{const x=score(a.score),y=score(b.score);if(x===null||y===null)return x===y?a.index-b.index:x===null?1:-1;return (direction==='asc'?x-y:y-x)||a.index-b.index;});
  }
  function parseStructures(text){
    const data=JSON.parse(text||'[]');if(!Array.isArray(data))throw new Error('结构数据必须为 JSON 数组');
    const ids=new Set();return data.map(item=>{if(!item||typeof item.id!=='string'||!item.id||ids.has(item.id)||typeof item.data!=='string')throw new Error('每个结构需唯一字符串 id 与结构文本 data');ids.add(item.id);return {...item,format:String(item.format||'').toLowerCase(),target_chains:Array.isArray(item.target_chains)?item.target_chains.filter(c=>typeof c==='string'):[],binder_chains:Array.isArray(item.binder_chains)?item.binder_chains.filter(c=>typeof c==='string'):[]};});
  }
  const utils={esc,score,orderedRows,parseStructures};if(typeof module!=='undefined'&&module.exports)module.exports=utils;
  if(typeof document==='undefined')return;root.WorkbenchOffline=utils;
  const scriptURL=document.currentScript?.src;const libraryURL=scriptURL?new URL('./3Dmol-min.js',scriptURL).href:'./3Dmol-min.js';
  const controls=document.querySelector('#offline-structure-controls'),container=document.querySelector('#offline-structures'),dataNode=document.querySelector('script#structure-data[type="application/json"]');
  let structures=[],viewers=[],downloads=[],epoch=0,libraryPromise=null;
  function dispose(record){try{record.viewer.clear();const canvas=record.element.querySelector('canvas');const gl=canvas?.getContext('webgl')||canvas?.getContext('webgl2')||canvas?.getContext('experimental-webgl');gl?.getExtension('WEBGL_lose_context')?.loseContext();record.element.replaceChildren();}catch{}}
  function cleanup(){epoch++;for(const record of viewers)dispose(record);viewers=[];for(const url of downloads)URL.revokeObjectURL(url);downloads=[];}
  function library(){
    if(root.$3Dmol?.createViewer)return Promise.resolve(root.$3Dmol);if(libraryPromise)return libraryPromise;
    libraryPromise=new Promise((resolve,reject)=>{const script=document.createElement('script');script.src=libraryURL;script.onload=()=>root.$3Dmol?.createViewer?resolve(root.$3Dmol):reject(new Error('本地 3Dmol 未提供 createViewer'));script.onerror=()=>{script.remove();reject(new Error('本地 3Dmol 加载失败'));};document.head.appendChild(script);}).catch(error=>{libraryPromise=null;throw error;});return libraryPromise;
  }
  function style(record){
    const {viewer,item}=record;viewer.setStyle({},{cartoon:{colorscheme:'chain'}});
    if(item.target_chains.length)viewer.setStyle({chain:item.target_chains},{cartoon:{color:'#74b9af'}});
    if(item.binder_chains.length)viewer.setStyle({chain:item.binder_chains},{cartoon:{color:'#e3ba67'}});
    for(const chain of record.hidden)viewer.setStyle({chain},{});viewer.render();
  }
  function statusMessage(text){const status=controls.querySelector('[data-offline-status]');status.textContent=text;}
  async function show(){
    const values=[controls.querySelector('[name="offline-first"]').value,controls.querySelector('[name="offline-second"]').value].filter(Boolean);
    if(!values.length){statusMessage('请至少选择一个候选。');return;}if(values.length===2&&values[0]===values[1]){statusMessage('请选择两个不同候选，或将第二项留空。');return;}
    cleanup();const generation=epoch;const selected=values.map(id=>structures.find(item=>item.id===id));
    if(selected.some(item=>!item)){statusMessage('候选不存在，请重新选择。');return;}
    container.innerHTML=selected.map((item,index)=>`<article class="molecular-card"><h3>${esc(item.id)}</h3><div id="offline-molecule-${index}" class="molecular-canvas" role="img" aria-label="${esc(item.id)} 的可交互三维结构"></div><div id="offline-chains-${index}" class="molecular-controls"></div><p id="offline-model-status-${index}" role="status" class="help-note">正在读取内嵌结构文本…</p><a id="offline-download-${index}" class="btn">下载原始结构 ↓</a></article>`).join('');
    container.classList.add('molecular-grid');statusMessage('独立原始坐标查看；没有进行结构叠合或重新计算接触。');
    const button=controls.querySelector('[data-offline-show]');button.focus({preventScroll:true});
    for(const [index,item] of selected.entries()){
      const element=document.getElementById(`offline-molecule-${index}`),status=document.getElementById(`offline-model-status-${index}`),chains=document.getElementById(`offline-chains-${index}`),download=document.getElementById(`offline-download-${index}`);
      const format=item.format==='pdb'?'pdb':['cif','mmcif'].includes(item.format)?'cif':null;
      try{const url=URL.createObjectURL(new Blob([item.data],{type:'text/plain;charset=utf-8'}));downloads.push(url);download.href=url;download.download=item.id.replace(/[^A-Za-z0-9_\u3400-\u9fff.-]/g,'_').slice(0,100)+'.'+(format||'txt');}catch{download.hidden=true;status.textContent='当前环境不支持内嵌文件下载；结构文本仍保存在本 HTML 的结构数据中。';}
      try{
        if(!format)throw new Error('仅支持 PDB / CIF 格式');if(item.data.length>10000000)throw new Error('结构文本超过 10 MB 查看限制，请下载后本地查看');
        const runtime=await library();if(generation!==epoch)return;
        const viewer=runtime.createViewer(element,{backgroundColor:'#20362f'}),record={index,item,element,viewer,hidden:new Set()};viewers.push(record);
        const model=viewer.addModel(item.data,format),atoms=model.selectedAtoms({});if(!atoms.length)throw new Error('未解析到原子，请下载核对结构格式');
        style(record);viewer.zoomTo();viewer.render();
        chains.innerHTML=[...new Set(atoms.map(atom=>String(atom.chain||'')))].map(chain=>`<label><input type="checkbox" data-offline-view="${index}" data-chain="${esc(chain)}" checked>链 ${esc(chain||'未标注')}</label>`).join('');
        status.textContent='按链着色；来源明确标注 target / binder 时分别为青绿 / 金色。没有坐标叠合。';
      }catch(error){if(generation!==epoch)return;const record=viewers.find(v=>v.index===index);if(record){dispose(record);viewers=viewers.filter(v=>v!==record);}status.textContent=`三维查看不可用：${error.message}。可使用原始结构下载。`;}
    }
  }
  function initializeStructures(){
    if(!controls||!container||!dataNode)return;
    try{structures=parseStructures(dataNode.textContent);if(!structures.length){controls.textContent='此报告没有内嵌结构文本；请通过候选工件下载查看。';return;}
      const options='<option value="">不选择</option>'+structures.map(item=>`<option value="${esc(item.id)}">${esc(item.id)}</option>`).join('');
      controls.innerHTML=`<div class="form-grid"><label class="field"><span>第一个候选</span><select name="offline-first">${options}</select></label><label class="field"><span>第二个候选（可留空）</span><select name="offline-second">${options}</select></label></div><button type="button" class="btn primary" data-offline-show>显示所选结构 · 最多 2 个</button><p data-offline-status role="status" aria-live="polite" class="help-note">结构来自报告内嵌文本；显示不访问网络。原坐标独立展示，没有对齐或叠合。</p>`;
      controls.addEventListener('click',event=>{if(event.target.closest('[data-offline-show]'))show();});
      container.addEventListener('change',event=>{if(event.target.dataset.offlineView===undefined)return;const record=viewers.find(v=>v.index===Number(event.target.dataset.offlineView));if(!record)return;if(event.target.checked)record.hidden.delete(event.target.dataset.chain);else record.hidden.add(event.target.dataset.chain);style(record);});
    }catch(error){controls.textContent=`内嵌结构数据不可用：${error.message}`;}
  }
  function initializeTable(){
    const table=document.querySelector('table[data-offline-candidates]');if(!table||!table.tBodies?.[0])return;
    const tbody=table.tBodies[0],rows=[...tbody.rows].map((element,index)=>({element,index,score:element.dataset.score,search:element.dataset.search||element.textContent}));
    let host=document.querySelector('#offline-candidate-controls');if(!host){host=document.createElement('div');host.id='offline-candidate-controls';table.before(host);}
    host.innerHTML='<div class="toolbar"><label>搜索候选 <input type="search" name="offline-search" placeholder="候选名称或来源记录"></label><label>原始 score 排序 <select name="offline-sort"><option value="original">来源顺序（保留原排名）</option><option value="desc">score 降序</option><option value="asc">score 升序</option></select></label></div><div class="button-row"><button type="button" class="btn" data-offline-page="previous">上一页</button><span data-offline-page-label tabindex="-1"></span><button type="button" class="btn" data-offline-page="next">下一页</button></div><p data-offline-row-status role="status" aria-live="polite" class="help-note"></p>';
    let page=1,pages=1;const pageSize=50;
    function update(reset=false){
      if(reset)page=1;const query=host.querySelector('[name="offline-search"]').value.toLocaleLowerCase(),direction=host.querySelector('[name="offline-sort"]').value;
      const ordered=orderedRows(rows,direction),filtered=ordered.filter(row=>row.search.toLocaleLowerCase().includes(query));pages=Math.max(1,Math.ceil(filtered.length/pageSize));page=Math.min(page,pages);
      const visible=new Set(filtered.slice((page-1)*pageSize,page*pageSize));for(const row of ordered){row.element.hidden=!visible.has(row);tbody.appendChild(row.element);}
      host.querySelector('[data-offline-page-label]').textContent=`第 ${page} / ${pages} 页 · 每页 50 项`;
      host.querySelector('[data-offline-page="previous"]').disabled=page===1;host.querySelector('[data-offline-page="next"]').disabled=page===pages;
      host.querySelector('[data-offline-row-status]').textContent=`当前页 ${visible.size} 项 · 匹配 ${filtered.length} / ${rows.length} 项。排序只使用来源 data-score 原值；缺失或非有限值排在末尾，同分保留来源顺序。搜索、排序和分页仅改变显示，不改写数据。`;
    }
    host.addEventListener('input',event=>{if(event.target.name==='offline-search')update(true);});host.addEventListener('change',event=>{if(event.target.name==='offline-sort')update(true);});
    host.addEventListener('click',event=>{const button=event.target.closest('[data-offline-page]');if(!button||button.disabled)return;page+=button.dataset.offlinePage==='next'?1:-1;update();(button.disabled?host.querySelector('[data-offline-page-label]'):button).focus({preventScroll:true});});update();
  }
  const styleNode=document.createElement('style');styleNode.textContent='table[data-offline-candidates] tr[hidden]{display:none!important}#offline-candidate-controls .toolbar{display:flex;gap:12px;flex-wrap:wrap}#offline-candidate-controls label{max-width:100%}#offline-candidate-controls input,#offline-candidate-controls select{max-width:100%}#offline-structure-controls .form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}#offline-structure-controls .field{min-width:0}#offline-structures{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}#offline-structures .molecular-card{min-width:0}#offline-structures .molecular-canvas{position:relative;height:280px;background:#20362f;overflow:hidden;border-radius:4px}#offline-structures .molecular-controls{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0}#offline-structure-controls select{max-width:100%;width:100%;min-width:0}@media(max-width:650px){#offline-structures,#offline-structure-controls .form-grid{grid-template-columns:1fr}}';document.head.appendChild(styleNode);
  initializeStructures();initializeTable();root.addEventListener('pagehide',cleanup);
})(typeof window!=='undefined'?window:globalThis);
