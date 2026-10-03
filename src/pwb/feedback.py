"""Verified immutable feedback snapshots and unlimited offline run history."""
import copy, html, json, os, shutil, tempfile, zipfile, uuid, math, hashlib
from pathlib import Path
from .util import atomic, now, sha, read, inside, idcheck, zip_entries, extract
SCHEMA='pwb-feedback-v1'

def _safe_json(x):return json.dumps(x,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
JSON_LIMITS={'MANIFEST.json':32*1024**2,'report.json':128*1024**2,'run_snapshot.json':16*1024**2}
def _zip_json(archive,name):
    if archive.getinfo(name).file_size>JSON_LIMITS[name]:raise ValueError('反馈元数据过大: '+name)
    return json.loads(archive.read(name))
def _member_digest(archive,name):
    digest=hashlib.sha256();count=0
    with archive.open(name) as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk);count+=len(chunk)
    return count,digest.hexdigest()
def _copy_file(src,dst):
    """Copy an open-file snapshot. Reject changes to the bytes actually copied."""
    dst.parent.mkdir(parents=True,exist_ok=True)
    with src.open('rb') as f, dst.open('wb') as target:
        size=os.fstat(f.fileno()).st_size;remaining=size;first=hashlib.sha256()
        while remaining:
            data=f.read(min(1024*1024,remaining))
            if not data:raise ValueError('导出时工件被截断: '+src.name)
            target.write(data);first.update(data);remaining-=len(data)
        f.seek(0);remaining=size;again=hashlib.sha256()
        while remaining:
            data=f.read(min(1024*1024,remaining))
            if not data:raise ValueError('导出时工件被截断: '+src.name)
            again.update(data);remaining-=len(data)
    if first.digest()!=again.digest():raise ValueError('导出时工件正在变化，请稍后重试: '+src.name)
    return {'bytes':size,'sha256':first.hexdigest()}

def _report_page(run,events,artifacts,destination,legacy=False):
    e=html.escape;status=e(run['status']);progress=run.get('progress',{});stages=progress.get('stages',[])
    rows=''.join(f'<tr><td>{e(s["name"])}</td><td>{e(str(s["completed"]))} / {e(str(s.get("actual"))) if s.get("actual") is not None else "尚未确定"}</td><td>{e(str(s["new"]))}</td><td>{e(str(s["reused"]))}</td><td>{e(str(s["invalid"]))}</td></tr>' for s in stages)
    candidates=''.join(f'<tr data-candidate-id="{e(c["id"],quote=True)}" data-score="{e(str(c.get("ranking_score","")),quote=True)}" data-search="{e(c["id"]+" "+c.get("sequence",""),quote=True)}"><td>{e(c["id"])}</td><td>{e(str(c.get("ranking_score","缺失")))}</td><td><code>{e(c.get("sequence", ""))}</code></td><td>'+ (f'<a href="{e(c["download"],quote=True)}" download>结构</a>' if c.get('download') else '未随包携带')+'</td></tr>' for c in run.get('candidates',[]))
    structures=[]
    for c in run.get('candidates',[]):
        if not c.get('download'):continue
        p=inside(destination,destination/c['download'])
        if p.suffix.lower() not in ('.pdb','.cif') or p.stat().st_size>25*1024**2:continue
        structures.append({'id':c['id'],'format':p.suffix.lower()[1:],'data':p.read_text(),'target_chains':c.get('target_chains',[]),'binder_chains':c.get('binder_chains',[])})
    errors=''.join(f'<li>{e(ev["ts"])} · {e(ev["event"])} · {e(str(ev.get("data",{}).get("message",ev.get("data",{}))))}</li>' for ev in events if ev['event'] in ('ERROR','REPORT_ERROR','PREFLIGHT_FAILED','INTERRUPTED'))
    md=''.join(f'<tr><td>{e(a["path"])}</td><td>{a["bytes"]:,}</td><td>'+ (f'<a href="files/{e(a["path"],quote=True)}" download>下载</a>' if a.get('included') else '未随包携带')+'</td></tr>' for a in artifacts)
    doc=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(run['name'])} · 运行档案</title><link rel="stylesheet" href="assets/offline.css"><body><header><a href="#progress">运行档案</a><span>OFFLINE EVIDENCE / {e(run['id'])}</span></header><main><p class="eyebrow">{e(run['workflow'])} · 冻结反馈快照</p><h1>{e(run['name'])}</h1><p class="subtitle">{status} · 采集截止 {e(run['cutoff'])} · 证据序号 {run['evidence_seq']} · 计算完整与反馈包完整分别记录</p><nav><a href="#progress">计算记录</a><a href="#candidates">候选</a><a href="#artifacts">工件</a><a href="report.json" download>JSON</a><a href="summary.md" download>摘要</a>{'<a href="legacy/index.html">结构比较与 refine 证据</a>' if legacy else ''}</nav><section id="progress"><h2>计算记录</h2><div class="table"><table><thead><tr><th>阶段</th><th>提交 / 实际工作项</th><th>新算</th><th>缓存</th><th>无效</th></tr></thead><tbody>{rows}</tbody></table></div><h3>执行批次</h3><ol>{''.join('<li>'+e(a['id'])+' · '+e(a['status'])+' · '+e(a['started_at'])+'</li>' for a in run['attempts'])}</ol><ul>{errors or '<li>本快照未记录异常；不代表实验验证成功。</li>'}</ul></section><section id="candidates"><h2>候选目录 <small>{len(run.get('candidates',[]))}</small></h2><p>按本流程的原生选择记录审阅，不同流程或靶标的分数不混排。完整指标、模型与 refine 路径见 JSON 和 SR56 详情。</p><div class="table"><table><thead><tr><th>候选</th><th>原生分数</th><th>序列</th><th>结构</th></tr></thead><tbody>{candidates or '<tr><td colspan="4">没有已完整提交的有效候选。</td></tr>'}</tbody></table></div></section><section id="artifacts"><h2>工件索引</h2><p>大轨迹默认保留远程；下载补取清单可用于手动转移。</p><a href="missing_files.json" download>补取文件清单</a><div class="table"><table><thead><tr><th>相对路径</th><th>字节</th><th>可用性</th></tr></thead><tbody>{md}</tbody></table></div></section></main><footer>计算记录与科学结论分开。此页面在采集截止时间冻结，不显示实时心跳。</footer></body></html>'''
    doc=doc.replace('<div class="table"><table><thead><tr><th>候选</th>', '<div id="offline-candidate-controls"></div><div class="table"><table data-offline-candidates><thead><tr><th>候选</th>')
    structure_section='<section id="structures"><h2>结构展区</h2><p>包内精选结构按需显示，最多两个视窗。这里只显示原坐标，不进行新的叠合或接触推断；SR56 目标参考系与 refine 证据见原详情。WebGL 不可用时可继续下载结构。</p><div id="offline-structure-controls"></div><div id="offline-structures"></div></section>'
    doc=doc.replace('<section id="artifacts">',structure_section+'<section id="artifacts">')
    doc=doc.replace('</body>','<script id="structure-data" type="application/json">'+_safe_json(structures)+'</script><script src="assets/offline-viewer.js"></script></body>')
    atomic(destination/'index.html',doc,raw=True)

OFFLINE_CSS='''@font-face{font-family:PaperSerif;src:url(Serif.ttf)}@font-face{font-family:PaperSans;src:url(Sans.ttf)}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f5f2ea;color:#252b2a;font:15px PaperSans,serif;line-height:1.7}header,footer{padding:24px 5%;border-bottom:1px solid #cfcfc5;display:flex;gap:24px;justify-content:space-between}header span,.eyebrow{font:11px monospace;letter-spacing:.15em;color:#63786b}main{max-width:1280px;padding:40px 5%;margin:auto}h1{font:clamp(32px,5vw,66px) PaperSerif,serif;line-height:1.15;overflow-wrap:anywhere}h2{font:30px PaperSerif,serif}h3{font-size:18px}.subtitle{color:#68736d;overflow-wrap:anywhere}nav{display:flex;gap:22px;flex-wrap:wrap;padding:18px 0;border-bottom:1px solid #a7b6ab}a{color:#245d51;text-underline-offset:4px}section{padding:32px 0;border-bottom:1px solid #d9d9ce}.table{overflow:auto;max-height:650px}table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:13px 16px;text-align:left;border-bottom:1px solid #d9d9ce;max-width:500px;overflow-wrap:anywhere}th{position:sticky;top:0;background:#ecece2}code{word-break:break-all;font-size:12px}small{font:14px monospace;color:#527965}#offline-structures{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px}#offline-structures article{background:#172b28;color:#f7f2e7;padding:18px;border-radius:4px}#offline-structures .molecular-canvas{height:360px;position:relative}#offline-structure-controls,#offline-candidate-controls{display:flex;flex-wrap:wrap;gap:14px;padding:16px 0}button,input,select{font:inherit;padding:8px 12px;border:1px solid #94a99e;background:#fffdf8;border-radius:3px}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #a15437;outline-offset:3px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:20px}.card{background:#fffdf8;padding:26px;border:1px solid #dadbce;border-top:3px solid #397e6d}.card h2{font-size:24px}.card p{font-size:13px}.meta{font:11px monospace;overflow-wrap:anywhere}a:focus-visible{outline:2px solid #a15437;outline-offset:4px}@media(max-width:600px){header{flex-direction:column}main{padding:24px 5%}td,th{padding:10px;min-width:110px}}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}'''
def assets(dest):
    folder=dest/'assets';folder.mkdir(exist_ok=True);atomic(folder/'offline.css',OFFLINE_CSS,raw=True)
    root=Path(__file__).resolve().parent
    for name in ('offline-viewer.js','3Dmol-min.js','3Dmol-LICENSE.txt','3Dmol-provenance.json','3Dmol-min.js.LICENSE.txt'):
        src=root/'assets'/name
        if src.is_file():shutil.copy2(src,folder/name)
    for source,name in [('SR56_Serif_CJK.ttf','Serif.ttf'),('SR56_Sans_CJK.ttf','Sans.ttf')]:
        src=root/'assets/fonts'/source
        if not src.is_file():continue
        if src.is_file():shutil.copy2(src,folder/name)
    for name in ('NotoSansSC_OFL.txt','NotoSerifSC_OFL.txt','font_provenance.json'):
        src=root/'assets/fonts'/name
        if src.is_file():shutil.copy2(src,folder/name)

def export_run(store,run_id):
    # Store lock serializes snapshot reservation, event cutoff and concurrent export.
    with store.lock:
        run=copy.deepcopy(store.get(run_id));base=store.run_dir(run_id);events=run.pop('events');run['progress']=store.progress(events)
        seq=store.setting('snapshot_'+run_id,0)+1;store.set_setting('snapshot_'+run_id,seq)
        cutoff=now();run.update(cutoff=cutoff,evidence_seq=events[-1]['seq'] if events else 0,snapshot_seq=seq)
        out=store.root/'feedback';final=out/f'feedback_{run_id}_S{seq:06d}.zip'
        with tempfile.TemporaryDirectory(prefix='.feedback_',dir=out) as temp:
            stage=Path(temp);assets(stage);artifacts=[]
            selected=set();
            def native_key(c):
                score=c.get('ranking_score');finite=isinstance(score,(int,float)) and not isinstance(score,bool) and math.isfinite(score)
                return (not finite,-score if finite else 0,c['id'])
            shortlist=[c for c in run['candidates'] if c.get('committed') and c.get('valid_output',True)]
            if run['workflow']=='sr56':shortlist=[c for c in shortlist if c.get('selection',{}).get('selection')!='pair']
            for c in sorted(shortlist,key=native_key)[:10]:selected.add(c['structure'])
            for p in sorted((base/'output').rglob('*')):
                if not p.is_file() or p.is_symlink():continue
                rel=p.relative_to(base/'output').as_posix()
                if rel.startswith(('feedback/','control/','pwb_report/')):continue
                include=p.suffix.lower() in ('.json','.jsonl','.csv','.tsv','.xvg','.md','.txt','.log','.mdp','.ndx','.top','.itp') or rel in selected
                item={'path':rel,'bytes':p.stat().st_size,'sha256':None,'included':include,'reason':'数据或精选结构' if include else '原件保留远程，可按需导出'}
                if include:item.update(_copy_file(p,stage/'files'/rel))
                else:
                    prior=next((a for a in run.get('artifacts',[]) if a.get('path')==rel),None);item['sha256']=prior.get('sha256') if prior else None
                artifacts.append(item)
            for c in run['candidates']:
                rel=c['structure'];entry=next((a for a in artifacts if a['path']==rel and a['included']),None)
                if entry:
                    if entry['sha256']!=c['structure_sha256']:raise ValueError('已提交候选结构在导出前被改变')
                    c['download']='files/'+rel
            if run['attempts']:
                for a in run['attempts']:
                    p=base/'attempts'/a['id']/'worker.log'
                    if p.is_file():_copy_file(p,stage/'logs'/a['id']/p.name)
            if (base/'snapshot.json').is_file():shutil.copy2(base/'snapshot.json',stage/'run_snapshot.json')
            legacy=False
            if run['workflow']=='sr56' and (base/'output/monitor/session.json').exists():
                try:
                    import importlib.util
                    helper=Path(__file__).resolve().parent/'sr56/sr56_feedback.py'
                    spec=importlib.util.spec_from_file_location('pwb_legacy_feedback',helper);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
                    mod.assemble(base/'output',stage/'legacy');legacy=True
                except Exception as exc:run['report_warning']='SR56 结构详情导出失败，可重新导出: '+str(exc)
            report={'schema':SCHEMA,'run':run,'events':events,'artifacts':artifacts,'package_complete':True,'computation_complete':run['status']=='COMPLETED','integrity':'member hashes, committed candidate bindings; scientific interpretation separate'}
            atomic(stage/'report.json',report);atomic(stage/'missing_files.json',[a for a in artifacts if not a['included']])
            atomic(stage/'summary.md',f"# {run['name']}\n\n状态：{run['status']}。快照 {seq}，证据截止 {run['evidence_seq']}。\n\n已提交候选：{len(run['candidates'])}；新工作项 {run['progress']['new']}；复用 {run['progress']['reused']}；无效 {run['progress']['invalid']}。\n\n异常：{run.get('error') or '本快照未记录'}。\n\nGPU/MD 成功与结合实验结论分别核验。完整机器可读记录见 report.json。\n",raw=True)
            _report_page(run,events,artifacts,stage,legacy)
            files=[{'path':p.relative_to(stage).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(stage.rglob('*')) if p.is_file()]
            manifest={'schema':SCHEMA,'run_id':run_id,'workflow':run['workflow'],'snapshot_seq':seq,'evidence_seq':run['evidence_seq'],'config_sha256':run.get('config_sha256'),'snapshot_sha256':run.get('snapshot_sha256'),'environment_sha256':read(base/'snapshot.json',{}).get('environment_sha256'),'attempt_ids':[a['id'] for a in run['attempts']],'commits':[{'seq':e['seq'],'stage':e['stage'],'key':e['key'],'attempt_id':e['attempt_id']} for e in events if e['event'] in ('COMMITTED','REUSED','STAGE_COMMITTED','STAGE_REUSED')],'created_at':cutoff,'package_complete':True,'computation_complete':run['status']=='COMPLETED','files':files}
            atomic(stage/'MANIFEST.json',manifest);partial=final.with_suffix('.zip.partial')
            with zipfile.ZipFile(partial,'w',zipfile.ZIP_DEFLATED) as z:
                for p in sorted(stage.rglob('*')):
                    if p.is_file():z.write(p,p.relative_to(stage).as_posix())
            verify(partial);os.replace(partial,final)
            preview=base/'output/pwb_report';next_preview=base/'output'/('.report_'+uuid.uuid4().hex)
            shutil.copytree(stage,next_preview)
            if preview.exists():
                retired=base/'output'/('.old_report_'+uuid.uuid4().hex);os.replace(preview,retired);os.replace(next_preview,preview);shutil.rmtree(retired)
            else:os.replace(next_preview,preview)
            atomic(final.with_suffix('.zip.sha256'),sha(final)+'  '+final.name+'\n',raw=True)
        current=store.get(run_id);current['feedback_path']=str(final);current['report_url']='/reports/'+run_id+'/index.html';store.save(current)
        store.event(run_id,'FEEDBACK_EXPORTED',data={'snapshot_seq':seq,'path':str(final),'sha256':sha(final)});return final

def verify(path):
    with zipfile.ZipFile(path) as z:
        zip_entries(z,max_bytes=20*1024**3,max_members=500000)
        m=_zip_json(z,'MANIFEST.json')
        if m.get('schema')!=SCHEMA:
            # Explicit legacy adapter, preserve its bytes and old frozen semantics.
            import importlib.util
            root=Path(__file__).resolve().parent;helper=root/'sr56/sr56_feedback.py'

            if not helper.is_file():raise ValueError('Legacy feedback requires a separately installed, trusted adapter')
            spec=importlib.util.spec_from_file_location('pwb_legacy_verify',helper);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
            old=mod.verify_bundle(path)
            return {'schema':'legacy-sr56','run_id':old['run_id'],'snapshot_seq':None,'evidence_seq':None,'created_at':old['snapshot_at'],'legacy_manifest':old,'config_sha256':old.get('config_sha256'),'snapshot_sha256':old.get('notebook_sha256'),'workflow':'sr56','files':old['files']}
        idcheck(m['run_id'])
        if type(m.get('snapshot_seq')) is not int or m['snapshot_seq']<1 or m.get('package_complete') is not True:raise ValueError('反馈快照身份无效')
        declared=[e['path'] for e in m['files']]
        if len(declared)!=len(set(declared)) or set(z.namelist())!=set(declared)|{'MANIFEST.json'}:raise ValueError('反馈成员与清单不一致')
        for item in m['files']:
            size,digest=_member_digest(z,item['path'])
            if size!=item['bytes'] or digest!=item['sha256']:raise ValueError('反馈成员校验失败: '+item['path'])
        report=_zip_json(z,'report.json');run=report['run']
        for k in ('snapshot_seq','evidence_seq'):
            if run[k]!=m[k]:raise ValueError('报告证据身份与清单不同')
        if run['id']!=m['run_id'] or run.get('config_sha256')!=m['config_sha256'] or run.get('snapshot_sha256')!=m['snapshot_sha256']:raise ValueError('报告运行身份与清单不同')
        events=report.get('events',[])
        if [e.get('seq') for e in events]!=list(range(1,len(events)+1)) or m['evidence_seq']!=len(events):raise ValueError('反馈事件序号或截止证据不一致')
        commits=[{'seq':e['seq'],'stage':e['stage'],'key':e['key'],'attempt_id':e['attempt_id']} for e in events if e['event'] in ('COMMITTED','REUSED','STAGE_COMMITTED','STAGE_REUSED')]
        if commits!=m.get('commits'):raise ValueError('提交清单与事件不一致')
        if [a['id'] for a in run['attempts']]!=m['attempt_ids']:raise ValueError('执行批次身份不一致')
        allowed=set(m['attempt_ids'])|{'system'}
        if any(e.get('attempt_id') not in allowed for e in events):raise ValueError('事件属于未声明执行批次')
        if m.get('snapshot_sha256'):
            if _member_digest(z,'run_snapshot.json')[1]!=m['snapshot_sha256']:raise ValueError('固定运行快照散列不一致')
            snapshot=_zip_json(z,'run_snapshot.json')
            if (snapshot['run_id'],snapshot['workflow'],snapshot['config_sha256'],snapshot['environment_sha256'])!=(m['run_id'],m['workflow'],m['config_sha256'],m['environment_sha256']):raise ValueError('固定运行快照身份不一致')
        return m

def _rebuild_import_view(folder,source):
    """Hashes prove consistency, not trust in imported executable HTML/JS."""
    from .core import Store
    report=read(folder/'report.json');run=report['run'];events=report['events']
    run=copy.deepcopy(run);run['progress']=Store.progress(events)
    for candidate in run.get('candidates',[]):
        candidate.pop('download',None)
        relative='files/'+candidate['structure']
        structure=inside(folder,folder/relative)
        if structure.is_file():candidate['download']=relative
    # Preserve original members in the original ZIP; regenerate all active code.
    _copy_file(Path(source),folder/'original-feedback.zip')
    assets(folder);_report_page(run,events,report['artifacts'],folder)

def merge(paths,output):
    out=Path(output).resolve();out.mkdir(parents=True,exist_ok=True)
    index=read(out/'imports.json',{'schema':'pwb-local-v1','imports':[]});imports=index['imports']
    for existing in imports:
        idcheck(existing['run_id']);inside(out,out/existing['path'])
        if not existing['path'].startswith('runs/'):raise ValueError('本地索引路径无效')
    seen={x['zip_sha256'] for x in imports};pending=[]
    for path in paths:
        path=Path(path);h=sha(path)
        if h in seen:continue
        m=verify(path);binding={k:m.get(k) for k in ('workflow','config_sha256','snapshot_sha256','environment_sha256')}
        prior=[x for x in imports+pending if x['run_id']==m['run_id']]
        for x in prior:
            if x['binding']!=binding:raise ValueError('同一运行的固定身份发生冲突')
            if x['snapshot_seq']==m['snapshot_seq'] and m['snapshot_seq'] is not None:raise ValueError('相同快照序号对应不同内容')
            if x.get('evidence_seq') is not None and m.get('evidence_seq') is not None:
                if (m['snapshot_seq']>x['snapshot_seq'] and m['evidence_seq']<x['evidence_seq']) or (m['snapshot_seq']<x['snapshot_seq'] and m['evidence_seq']>x['evidence_seq']):raise ValueError('快照序号与提交证据顺序冲突')
                with zipfile.ZipFile(path) as z:incoming=_zip_json(z,'report.json')['events']
                existing_path=inside(out,out/x['path'])/'report.json'
                if existing_path.exists():existing=read(existing_path)['events']
                else:
                    with zipfile.ZipFile(x['source']) as z:existing=_zip_json(z,'report.json')['events']
                length=min(len(incoming),len(existing))
                if incoming[:length]!=existing[:length]:raise ValueError('同一运行的已提交事件前缀发生冲突')
        pending.append({'zip_sha256':h,'run_id':m['run_id'],'snapshot_seq':m['snapshot_seq'],'evidence_seq':m['evidence_seq'],'binding':binding,'legacy':m['schema']=='legacy-sr56','created_at':m['created_at'],'source':str(path),'path':f'runs/{m["run_id"]}/{h[:16]}'})
        seen.add(h)
    for item in pending:
        with tempfile.TemporaryDirectory(prefix='.import_',dir=out) as temp:
            with zipfile.ZipFile(item['source']) as z:extract(z,Path(temp)/'snapshot',max_bytes=20*1024**3)
            # Verify the bytes actually extracted before publishing the view.
            with zipfile.ZipFile(item['source']) as z:declared=_zip_json(z,'MANIFEST.json').get('files',[])
            for f in declared:
                extracted=inside(Path(temp)/'snapshot',Path(temp)/'snapshot'/f['path'])
                if not extracted.is_file() or sha(extracted)!=f['sha256'] or extracted.stat().st_size!=f['bytes']:raise ValueError('解压后的成员散列不一致')
            # Reverify source identity after reading, prior to publishing local view.
            if sha(item['source'])!=item['zip_sha256']:raise ValueError('导入期间反馈包发生变化')
            if not item['legacy']:_rebuild_import_view(Path(temp)/'snapshot',item['source'])
            dest=inside(out,out/item['path']);dest.parent.mkdir(parents=True,exist_ok=True);os.replace(Path(temp)/'snapshot',dest)
        imports.append(item)
    atomic(out/'imports.json',index);assets(out)
    latest={}
    for item in imports:
        rid=item['run_id'];old=latest.get(rid)
        key=(item['snapshot_seq'] is not None,item['snapshot_seq'] or 0,item['created_at'] if item['legacy'] else '')
        if not old or key>old[0]:latest[rid]=(key,item)
    cards=[]
    for _,item in sorted(latest.values(),key=lambda x:x[1]['created_at'],reverse=True):
        report=read(out/item['path']/'report.json',{});run=report.get('run',{});name=run.get('name',run.get('branch',item['run_id']));status=run.get('status','未知');n=len(run.get('candidates',report.get('candidates',[])))
        history=[x for x in imports if x['run_id']==item['run_id']]
        links=''.join(f'<li><a href="{html.escape(x["path"],quote=True)}/index.html">快照 {x["snapshot_seq"] if x["snapshot_seq"] is not None else "旧版"} · {html.escape(x["created_at"])}</a></li>' for x in history)
        cards.append(f'<article class="card"><p class="eyebrow">{html.escape(item["binding"]["workflow"] or "旧版 SR56")}</p><h2><a href="{item["path"]}/index.html">{html.escape(name)}</a></h2><p>{html.escape(status)} · {n} 候选</p><p class="meta">{html.escape(item["run_id"])}</p><details><summary>{len(history)} 个历史快照</summary><ul>{links}</ul></details></article>')
    doc='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>研究运行档案</title><link rel="stylesheet" href="assets/offline.css"><body><header>PROTEIN DESIGN WORKBENCH<span>离线反馈 / 永久运行档案</span></header><main><p class="eyebrow">RUNS ARE RECORDS, NOT SIX SLOTS</p><h1>每次探索，都有自己的记录。</h1><p>同一参数集的新运行独立归档；恢复批次属于原运行。这里只展示已导入的冻结快照，各靶标分数分别审阅。</p><p>'+str(len(latest))+' 次运行 · '+str(len(imports))+' 个快照</p><div class="cards">'+''.join(cards)+'</div></main><footer>成员散列已核验；计算完整性按每份报告独立标识。<a href="imports.json" download>导入索引</a></footer></body></html>'
    atomic(out/'index.html',doc,raw=True);return out/'index.html'
