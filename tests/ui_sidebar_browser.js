'use strict';
// Real Chromium layout/keyboard checks. Viewer/API stubs are separately labelled.
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {pathToFileURL}=require('node:url');
const {chromium}=require('playwright');
const assets=path.resolve(process.env.PWB_ASSETS_DIR||path.join(__dirname,'../src/pwb/assets'));
const output=path.resolve(process.env.PWB_QA_DIR||path.join(__dirname,'../test-results/sidebar'));fs.mkdirSync(output,{recursive:true});
const report={started_at:new Date().toISOString(),assets_dir:assets,node:process.version,browser:null,checks:[],screenshots:[],console_errors:[],page_errors:[],http_requests:[],not_tested:['Full accessibility audit / screen reader','Real WebGL rendering, molecular rotation or GPU execution','Live backend mutations / scientific workflows','Visual regression: no comparison baseline'],runtime_identity:{model:'UNVERIFIED',provider:'UNVERIFIED',backend:'UNVERIFIED'},files:{}};
for(const name of ['app.js','app.css','demo-fixtures.js'])report.files[name]=crypto.createHash('sha256').update(fs.readFileSync(path.join(assets,name))).digest('hex');
let browser;
function check(name,condition,details={}){report.checks.push({name,status:condition?'PASS':'FAIL',details});if(!condition)process.exitCode=1;}
async function contextPage(label,viewport={width:1440,height:1000},init){
 const context=await browser.newContext({viewport});await context.setOffline(true);
 await context.route(/^https?:\/\//,route=>{report.http_requests.push({label,url:route.request().url(),blocked:true});return route.abort('internetdisconnected');});
 if(init)await context.addInitScript(init);
 const page=await context.newPage();page.on('console',msg=>{if(msg.type()==='error')report.console_errors.push({label,text:msg.text()});});page.on('pageerror',error=>report.page_errors.push({label,message:error.message}));
 await page.goto(pathToFileURL(path.join(assets,'demo-normal.html')).href,{waitUntil:'load'});await page.waitForSelector('#sidebar-toggle');await page.evaluate(()=>document.fonts.ready);
 return {context,page};
}
async function measure(page){return page.evaluate(()=>{const aside=document.querySelector('#workbench-sidebar'),main=document.querySelector('#main'),toggle=document.querySelector('#sidebar-toggle'),rect=main.getBoundingClientRect(),sidebar=aside.getBoundingClientRect();return {viewport:innerWidth,client_width:document.documentElement.clientWidth,body_width:document.body.scrollWidth,root_width:document.documentElement.scrollWidth,collapsed:document.querySelector('.shell').classList.contains('sidebar-collapsed'),hidden:aside.hidden,display:getComputedStyle(aside).display,sidebar_width:sidebar.width,sidebar_height:sidebar.height,aria_expanded:toggle.getAttribute('aria-expanded'),aria_controls:toggle.getAttribute('aria-controls'),active_id:document.activeElement?.id,main_width:rect.width,main_center:(rect.left+rect.right)/2,viewport_center:innerWidth/2,center_delta:Math.abs((rect.left+rect.right)/2-innerWidth/2)};});}
async function screenshot(page,name){const filename=`${name}.png`;await page.screenshot({path:path.join(output,filename),fullPage:true});report.screenshots.push(filename);}
async function widths(){
 for(const width of [1440,1024,768,390,1920]){
  const {context,page}=await contextPage(`sidebar-${width}`,{width,height:1000});await page.evaluate(()=>localStorage.removeItem('pwb.sidebar-collapsed'));await page.reload({waitUntil:'load'});
  let m=await measure(page);check(`expanded-${width}`,!m.hidden&&m.display!=='none'&&m.aria_expanded==='true'&&m.aria_controls==='workbench-sidebar',m);
  await page.locator('#sidebar-toggle').click();m=await measure(page);check(`collapsed-layout-${width}`,m.hidden&&m.display==='none'&&m.sidebar_width===0&&m.sidebar_height===0&&m.collapsed,m);
  check(`collapsed-center-${width}`,m.center_delta<=1,m);check(`collapsed-no-overflow-${width}`,m.body_width<=m.viewport&&m.root_width<=m.viewport,m);check(`aria-focus-${width}`,m.aria_expanded==='false'&&m.active_id==='sidebar-toggle',m);await screenshot(page,`normal-collapsed-${width}`);
  await page.locator('#main [data-action="nav"][data-view="projects"]').first().click();m=await measure(page);check(`navigation-retains-collapse-${width}`,m.hidden&&m.collapsed&&m.aria_expanded==='false',m);
  await page.reload({waitUntil:'load'});m=await measure(page);check(`reload-retains-collapse-${width}`,m.hidden&&m.aria_expanded==='false'&&await page.evaluate(()=>localStorage.getItem('pwb.sidebar-collapsed'))==='1',m);
  await page.locator('#sidebar-toggle').focus();await page.keyboard.press('Enter');m=await measure(page);check(`keyboard-enter-expands-${width}`,!m.hidden&&m.aria_expanded==='true'&&m.active_id==='sidebar-toggle',m);
  await page.keyboard.press('Space');m=await measure(page);check(`keyboard-space-collapses-${width}`,m.hidden&&m.aria_expanded==='false'&&m.active_id==='sidebar-toggle',m);
  let hiddenFocus=false;for(let i=0;i<14;i++){await page.keyboard.press('Tab');hiddenFocus ||= await page.evaluate(()=>!!document.activeElement?.closest('#workbench-sidebar'));}check(`hidden-nav-not-tabbed-${width}`,!hiddenFocus);
  await context.close();
 }
}
async function screenshotScenarios(){
 for(const mode of ['normal','failed','empty','rich'])for(const width of [1440,1024,768,390]){
  const {context,page}=await contextPage(`scenario-${mode}-${width}`,{width,height:1000});await page.goto(pathToFileURL(path.join(assets,`demo-${mode}.html`)).href,{waitUntil:'load'});await page.evaluate(()=>document.fonts.ready);
  const m=await measure(page);check(`scenario-no-overflow-${mode}-${width}`,m.body_width<=m.viewport&&m.root_width<=m.viewport,m);await screenshot(page,`${mode}-expanded-${width}`);await context.close();
 }
}
async function deniedStorage(){
 const {context,page}=await contextPage('storage-denied',{width:390,height:1000},()=>{Object.defineProperty(window,'localStorage',{configurable:true,get(){throw new DOMException('TEST storage denied','SecurityError');}});});
 await page.locator('#sidebar-toggle').click();const collapsed=await measure(page);await page.locator('#sidebar-toggle').click();const expanded=await measure(page);check('storage-denied-toggle',collapsed.hidden&&!expanded.hidden&&expanded.active_id==='sidebar-toggle',{collapsed,expanded});await context.close();
}
async function viewerPreservation(){
 const {context,page}=await contextPage('viewer-stub',{width:1440,height:1000},()=>{
  let synthetic;const telemetry=window.__PWB_BROWSER_STUB={viewers:[],stateReads:0,runReads:0};
  Object.defineProperty(window,'PWB_DEMO_STATE',{configurable:true,get(){return undefined;},set(value){synthetic=JSON.parse(JSON.stringify(value));synthetic.synthetic=true;const run=synthetic.runs[0];run.candidates.slice(0,2).forEach((c,index)=>{c.structure=`stub/model-${index}.pdb`;c.structure_sha256=`STUB-SHA-${index}`;});synthetic.current=run;}});
  window.fetch=async(input,options={})=>{const url=String(input);if(options.method==='POST')throw new Error('Browser stub forbids mutations');if(url==='/api/state'){telemetry.stateReads++;return new Response(JSON.stringify(synthetic),{headers:{'Content-Type':'application/json'}});}if(url.startsWith('/api/run/')){telemetry.runReads++;return new Response(JSON.stringify(synthetic.runs.find(r=>r.id===decodeURIComponent(url.split('/').at(-1))))); }if(url.startsWith('/api/files/'))return new Response('STUB PDB TEXT; viewer parsing also stubbed');throw new Error(`Unexpected stub request: ${url}`);};
  window.$3Dmol={createViewer(element){const marker=document.createElement('div');marker.dataset.viewerStub='true';marker.textContent='BROWSER TEST VIEWER STUB — NO REAL STRUCTURE';element.appendChild(marker);const viewer={element,styles:[],cleared:false,rotation:'initial',clear(){this.cleared=true;},setStyle(selection,style){this.styles.push({selection,style});},addModel(){return {selectedAtoms(){return [{chain:'A',resi:1},{chain:'B',resi:2}];}};},zoomTo(){},resize(){},render(){}};telemetry.viewers.push(viewer);return viewer;}};
 });
 await page.locator('.hero [data-action="run"]').click();await page.locator('#tab-candidates').click();await page.locator('[data-candidate]').nth(0).check();await page.locator('[data-candidate]').nth(1).check();await page.locator('[data-action="view-structures"]').click();await page.waitForFunction(()=>window.__PWB_BROWSER_STUB.viewers.length===2);
 await page.locator('[data-view-slot="0"][data-view-chain="B"]').uncheck();await page.evaluate(()=>{window.__retainedMolecule=document.querySelector('#molecular-comparison');const t=window.__PWB_BROWSER_STUB;t.viewers[0].rotation='USER-ROTATION-STUB';t.initialStyles=t.viewers[0].styles.length;t.initialRunReads=t.runReads;});
 await page.locator('#sidebar-toggle').click();await page.locator('#sidebar-toggle').click();
 let result=await page.evaluate(()=>{const t=window.__PWB_BROWSER_STUB;return {same_dom:document.querySelector('#molecular-comparison')===window.__retainedMolecule,count:t.viewers.length,cleared:t.viewers.filter(v=>v.cleared).length,rotation:t.viewers[0].rotation,styles:t.viewers[0].styles.length,initial_styles:t.initialStyles,chain_B_checked:document.querySelector('[data-view-slot="0"][data-view-chain="B"]').checked};});
 check('sidebar-preserves-viewer-dom-call-path',result.same_dom&&result.count===2&&result.cleared===0&&result.rotation==='USER-ROTATION-STUB'&&result.styles===result.initial_styles&&!result.chain_B_checked,{...result,boundary:'Real browser DOM, stub viewer/API; no actual molecular/WebGL rotation claim'});
 await page.locator('#sidebar-toggle').click();await page.waitForFunction(()=>window.__PWB_BROWSER_STUB.runReads>window.__PWB_BROWSER_STUB.initialRunReads,{},{timeout:35000});
 result=await page.evaluate(()=>{const t=window.__PWB_BROWSER_STUB;return {same_dom:document.querySelector('#molecular-comparison')===window.__retainedMolecule,count:t.viewers.length,cleared:t.viewers.filter(v=>v.cleared).length,rotation:t.viewers[0].rotation,chain_B_checked:document.querySelector('[data-view-slot="0"][data-view-chain="B"]').checked,collapsed:document.querySelector('#workbench-sidebar').hidden,run_reads:t.runReads};});
 check('natural-30s-poll-preserves-collapse-and-viewer',result.same_dom&&result.count===2&&result.cleared===0&&result.rotation==='USER-ROTATION-STUB'&&!result.chain_B_checked&&result.collapsed,result);await screenshot(page,'viewer-stub-collapsed-after-poll');await context.close();
}
(async()=>{
 try{
  browser=await chromium.launch({headless:true,chromiumSandbox:true});report.browser={version:browser.version(),configured_chromium_executable:chromium.executablePath(),headless:true,chromium_sandbox:true,profile:'fresh isolated Playwright contexts; no existing user profile'};
  await widths();await screenshotScenarios();await deniedStorage();await viewerPreservation();
  check('no-http-requests',report.http_requests.length===0,{count:report.http_requests.length});check('no-browser-console-errors',report.console_errors.length===0,{errors:report.console_errors});check('no-page-errors',report.page_errors.length===0,{errors:report.page_errors});
 }catch(error){report.fatal={message:error.message,stack:error.stack};process.exitCode=1;}
 finally{if(browser)await browser.close();report.completed_at=new Date().toISOString();report.summary={pass:report.checks.filter(c=>c.status==='PASS').length,fail:report.checks.filter(c=>c.status==='FAIL').length,fatal:!!report.fatal};fs.writeFileSync(path.join(output,'results.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({summary:report.summary,fatal:report.fatal?.message,failures:report.checks.filter(c=>c.status==='FAIL'),evidence_dir:output},null,2));}
})();
