"""Transactional run catalogue and single-workstation supervisor."""
import contextlib, json, os, platform, shutil, sqlite3, subprocess, sys, threading, time, uuid, zipfile, signal
from pathlib import Path
import math
from .util import now, sha, fingerprint, atomic, read, inside, idcheck, zip_entries, extract, validate_plan

TERMINAL={'COMPLETED','FAILED','STOPPED','INTERRUPTED'}
ACTIVE={'STARTING','RUNNING','STOPPING'}
PACKAGE=Path(__file__).resolve().parent
class Conflict(ValueError): pass

ENV_PROBE="""import json,sys,platform,importlib.metadata as m
v={}
for k in ('torch','numpy','scipy','jax','jaxlib','nbclient','ipykernel','rc-foundry','atomworks','biotite'):
 try:v[k]=m.version(k)
 except m.PackageNotFoundError:v[k]=None
print(json.dumps({'python':sys.version,'executable':sys.executable,'platform':platform.platform(),'packages':v}))
"""
def environment_fingerprint(env):
    p=Path(env.get('python','')).expanduser()
    if not p.is_file():raise ValueError('请选择实际存在的 Python 解释器')
    r=subprocess.run([str(p),'-c',ENV_PROBE],capture_output=True,text=True,timeout=25,check=True)
    data=json.loads(r.stdout.strip());data['binding']={k:env.get(k) for k in ('python','gmx','bindcraft_root')}
    if env.get('gmx'):
        g=Path(env['gmx']);
        if not g.is_file():raise ValueError('GROMACS 可执行文件不存在')
        data['gmx_sha256']=sha(g)
        v=subprocess.run([str(g),'--version'],capture_output=True,text=True,timeout=10)
        data['gmx_version']=(v.stdout+v.stderr)[-10000:]
    if env.get('bindcraft_root'):
        bc=Path(env['bindcraft_root']);script=bc/'bindcraft.py'
        if not script.is_file():raise ValueError('BindCraft 目录缺少 bindcraft.py')
        data['bindcraft_source']={str(p.relative_to(bc)):sha(p) for p in sorted(bc.rglob('*.py')) if '.git' not in p.parts}
    try:
        r=subprocess.run(['nvidia-smi','--query-gpu=uuid,name,driver_version','--format=csv,noheader'],capture_output=True,text=True,timeout=5)
        data['gpu_driver']=r.stdout.strip() if r.returncode==0 else None
    except (OSError,subprocess.TimeoutExpired):data['gpu_driver']=None
    nvcc=shutil.which('nvcc');data['nvcc_path']=nvcc
    if nvcc:
        try:data['nvcc_version']=subprocess.run([nvcc,'--version'],capture_output=True,text=True,timeout=5).stdout.strip()
        except (OSError,subprocess.TimeoutExpired):data['nvcc_version']=None
    return data

def process_identity(pid):
    try:
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        text=Path(f'/proc/{pid}/stat').read_text();fields=text[text.rfind(')')+2:].split()
        return {'pid':int(pid),'boot_id':boot,'start_ticks':fields[19],'pgid':os.getpgid(pid),'state':fields[0]}
    except (OSError,IndexError):
        try:os.kill(pid,0)
        except OSError:return None
        # Non-Linux process handles are only trusted by the live supervisor.
        return {'pid':int(pid),'boot_id':platform.system(),'start_ticks':None,'pgid':os.getpgid(pid),'state':'UNKNOWN'}
def identity_matches(old):
    if not old or old.get('start_ticks') is None:return False
    cur=process_identity(old['pid'])
    return bool(cur and cur.get('state')!='Z' and all(cur.get(k)==old.get(k) for k in ('pid','boot_id','start_ticks','pgid')))

def read_boot_id():
    try:return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:return None

def process_group_members(pgid):
    members=[]
    if Path('/proc').is_dir():
        for item in Path('/proc').iterdir():
            if not item.name.isdigit():continue
            ident=process_identity(int(item.name))
            if ident and ident.get('pgid')==pgid and ident.get('state')!='Z':members.append(ident)
    return members

def sample_resources(pid=None,root=None):
    d={'ts':now(),'availability':'unavailable','gpus':[],'cpu_load':None,'memory':None,'disk':None}
    try:d['cpu_load']=list(os.getloadavg())
    except OSError:pass
    try:
        values={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()}
        d['memory']={k:values.get(k) for k in ('MemTotal','MemAvailable','SwapTotal','SwapFree')}
    except (OSError,ValueError):pass
    if root:
        disk=shutil.disk_usage(root);d['disk']={'total':disk.total,'used':disk.used,'free':disk.free}
    try:
        r=subprocess.run(['nvidia-smi','--query-gpu=uuid,name,utilization.gpu,memory.used,memory.total','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=5)
        if r.returncode==0:
            for line in r.stdout.splitlines():
                x=[a.strip() for a in line.split(',')]
                d['gpus'].append({'uuid':x[0],'name':x[1],'utilization_pct':float(x[2]),'memory_used_mb':float(x[3]),'memory_total_mb':float(x[4]),'process_memory_mb':None})
            d['availability']='available'
        r=subprocess.run(['nvidia-smi','--query-compute-apps=pid,gpu_uuid,used_memory','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=5)
        d['gpu_processes']=[]
        if r.returncode==0:
            for line in r.stdout.splitlines():
                x=[a.strip() for a in line.split(',')]
                if len(x)!=3:continue
                row={'pid':int(x[0]),'uuid':x[1],'used_memory_mb':float(x[2])};d['gpu_processes'].append(row)
                if pid and (row['pid']==pid or (process_identity(row['pid']) or {}).get('pgid')==pid):
                    for g in d['gpus']:
                        if g['uuid']==row['uuid']:g['process_memory_mb']=(g['process_memory_mb'] or 0)+row['used_memory_mb']
    except (OSError,ValueError,subprocess.TimeoutExpired):pass
    return d

class Store:
    def __init__(self,root):
        self.root=Path(root).expanduser().resolve();self.root.mkdir(parents=True,exist_ok=True)
        for n in ('runs','inbox','local_reports','feedback'): (self.root/n).mkdir(exist_ok=True)
        self.db_path=self.root/'workbench.sqlite3';self.lock=threading.RLock()
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS envs(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,payload TEXT NOT NULL,position INTEGER);
CREATE TABLE IF NOT EXISTS events(run_id TEXT,seq INTEGER,attempt_id TEXT,local_seq INTEGER,payload TEXT,PRIMARY KEY(run_id,seq),UNIQUE(run_id,attempt_id,local_seq));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS resources(seq INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT);
CREATE TABLE IF NOT EXISTS triggers(parent TEXT,candidate TEXT,plan TEXT,child TEXT,PRIMARY KEY(parent,candidate,plan));''')
            db.execute("INSERT OR IGNORE INTO settings VALUES('paused','true')")
            db.execute("INSERT OR IGNORE INTO settings VALUES('pause_reason',?)",(json.dumps('启动后等待手动恢复队列',ensure_ascii=False),))
        if not self.projects():self.project('默认项目')
    @contextlib.contextmanager
    def db(self):
        with self.lock:
            db=sqlite3.connect(self.db_path,timeout=30);db.row_factory=sqlite3.Row
            try:db.execute('PRAGMA foreign_keys=ON');yield db;db.commit()
            except BaseException:db.rollback();raise
            finally:db.close()
    def setting(self,key,default=None):
        with self.db() as db:r=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(r[0]) if r else default
    def set_setting(self,key,value):
        with self.db() as db:db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
    def pause(self,value=True,reason='用户暂停队列'):
        if not value:
            for run in self.list_runs():
                if run.get('ownership_uncertain'):
                    identities=[]
                    for attempt in run['attempts']:
                        identities.extend([attempt.get('process'),read(self.run_dir(run['id'])/'attempts'/attempt['id']/'kernel_process.json')]);identities.extend(attempt.get('group_members',[]))
                    if any(x and process_identity(x['pid']) for x in identities):raise Conflict('旧执行进程身份尚未核对，队列继续暂停')
                    run['ownership_uncertain']=False;self.save(run);self.event(run['id'],'RECONCILED',data={'all_recorded_processes_gone':True})
        with self.db() as db:
            for k,v in [('paused',value),('pause_reason',reason if value else '')]:db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(k,json.dumps(v,ensure_ascii=False)))
    def project(self,name):
        p={'id':'p_'+uuid.uuid4().hex[:12],'name':str(name).strip()[:150] or '未命名项目'}
        with self.db() as db:db.execute('INSERT INTO projects VALUES(?,?)',(p['id'],p['name']))
        return p
    def projects(self):
        with self.db() as db:return [dict(r) for r in db.execute('SELECT * FROM projects')]
    def environment(self,name,python,gmx=None,bindcraft_root=None):
        e={'id':'e_'+uuid.uuid4().hex[:12],'name':str(name)[:100],'python':str(Path(python).expanduser().absolute()),'gmx':gmx or None,'bindcraft_root':bindcraft_root or None}
        e['fingerprint']=environment_fingerprint(e);e['sha256']=fingerprint(e['fingerprint'])
        with self.db() as db:db.execute('INSERT INTO envs VALUES(?,?)',(e['id'],json.dumps(e,ensure_ascii=False)))
        return e
    def env(self,ident):
        with self.db() as db:r=db.execute('SELECT payload FROM envs WHERE id=?',(ident,)).fetchone()
        if not r:raise ValueError('请绑定已有执行环境')
        return json.loads(r[0])
    def environments(self):
        with self.db() as db:return [json.loads(r[0]) for r in db.execute('SELECT payload FROM envs')]
    def run_dir(self,ident):return self.root/'runs'/idcheck(ident)
    def get(self,ident,details=True):
        idcheck(ident)
        with self.db() as db:r=db.execute('SELECT payload FROM runs WHERE id=?',(ident,)).fetchone()
        if not r:raise ValueError('运行不存在')
        run=json.loads(r[0])
        if details:
            run['events']=self.events(ident);run['progress']=self.progress(run['events'])
        return run
    def save(self,run,position=None):
        clean={k:v for k,v in run.items() if k not in ('events','progress')}
        with self.db() as db:
            db.execute('INSERT INTO runs(id,payload,position) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(run['id'],json.dumps(clean,ensure_ascii=False,allow_nan=False),position))
    def event(self,ident,event,stage='',key='',data=None,attempt_id='system',local_seq=None,ts=None,monotonic_ns=None):
        if event in ('PLAN','STAGE_PLANNED','QUEUE'):validate_plan(data or {})
        with self.db() as db:
            if local_seq is not None:
                r=db.execute('SELECT seq,payload FROM events WHERE run_id=? AND attempt_id=? AND local_seq=?',(ident,attempt_id,local_seq)).fetchone()
                if r:
                    old=json.loads(r[1])
                    if (old['event'],old['stage'],old['key'],old['data'])!=(event,stage,str(key),data or {}):raise Conflict('重复事件序号对应冲突内容')
                    return r[0]
            seq=db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id=?',(ident,)).fetchone()[0]
            row={'seq':seq,'ts':ts or now(),'event':event,'stage':stage,'key':str(key),'attempt_id':attempt_id,'monotonic_ns':monotonic_ns,'data':data or {}}
            db.execute('INSERT INTO events VALUES(?,?,?,?,?)',(ident,seq,attempt_id,local_seq,json.dumps(row,ensure_ascii=False,allow_nan=False)))
        return seq
    def events(self,ident):
        with self.db() as db:return [json.loads(r[0]) for r in db.execute('SELECT payload FROM events WHERE run_id=? ORDER BY seq',(ident,))]
    @staticmethod
    def progress(events):
        stages={};models={};last=None;artifact=None;collector=None;starts={}
        for e in events:
            ev={'STAGE_COMMITTED':'COMMITTED','STAGE_REUSED':'REUSED','STAGE_RUNNING':'RUNNING','STAGE_COMPLETED':'STAGE_END','STAGE_FAILED':'ERROR'}.get(e['event'],e['event']);stage=e['stage'];d=e.get('data',{});key=e['key']
            if ev in ('PLAN','STAGE_PLANNED','QUEUE'):validate_plan(d)
            if ev=='RESOURCE':collector=e['ts'];continue
            if ev in ('MD_PROGRESS','GROMACS_PROGRESS','BINDCRAFT_PROGRESS','PROGRESS'):
                last=e['ts']
                # Adapter supplies live progress independently from checkpoint commit counts.
                continue
            if ev in ('CREATED','QUEUED','ATTEMPT_START','ATTEMPT_END','FEEDBACK_EXPORTED','RECONCILED','STOP_REQUESTED'):continue
            if ev in ('CONFIGURED','START','END') and stage in ('configuration','session'):continue
            s=stages.setdefault(stage or '执行',{'name':stage or '执行','status':'PENDING','planned_upper':None,'actual':None,'completed':0,'new':0,'reused':0,'invalid':0,'_done':{},'_invalid':set(),'duration_s':0.0})
            if ev in ('PLAN','STAGE_PLANNED','QUEUE'):
                s['planned_upper']=d.get('planned_upper',d.get('n_requested',s['planned_upper']))
                s['actual']=d.get('actual',len(d['keys']) if isinstance(d.get('keys'),list) else s['actual'])
            if ev in ('RUNNING','START','STAGE_START'):
                s['status']='RUNNING'
                if e.get('monotonic_ns'):starts[(stage,key,e.get('attempt_id'))]=e['monotonic_ns']
            if ev in ('COMMITTED','REUSED'):
                fresh=key not in s['_done'];s['_done'].setdefault(key,ev);s['status']='RUNNING';last=e['ts'];artifact=e['ts']
                start=starts.get((stage,key,e.get('attempt_id')))
                if fresh and ev=='COMMITTED' and start and e.get('monotonic_ns'):s['duration_s']+=max(0,(e['monotonic_ns']-start)/1e9)
            if ev in ('INVALID','EXCLUDED'):s['_invalid'].add(key);last=e['ts']
            if ev=='MODEL':models[key]=d;last=e['ts']
            if ev in ('STAGE_END','STAGE_COMPLETE'):s['status']='COMPLETED'
            if ev in ('ERROR','FAILED'):s['status']='FAILED'
        for s in stages.values():
            done=s.pop('_done');s['completed']=len(done);s['new']=sum(v=='COMMITTED' for v in done.values());s['reused']=sum(v=='REUSED' for v in done.values());s['invalid']=len(s.pop('_invalid'))
            if s['status']!='FAILED' and s['actual'] is not None and s['completed']>=s['actual']:s['status']='COMPLETED'
        ss=list(stages.values());live={}
        for event in events:
            if event['event'] in ('MD_PROGRESS','GROMACS_PROGRESS','BINDCRAFT_PROGRESS','PROGRESS'):live[event['stage']]=event['data']
        for stage in ss:
            if stage['name'] in live:stage['live']=live[stage['name']]
        return {'stages':ss,'planned_upper':None,'actual':None,'completed':sum(s['completed'] for s in ss),'new':sum(s['new'] for s in ss),'reused':sum(s['reused'] for s in ss),'invalid':sum(s['invalid'] for s in ss),'models':len(models),'collector_at':collector,'progress_at':last,'artifact_at':artifact}
    def list_runs(self):
        with self.db() as db:rows=list(db.execute('SELECT payload,position FROM runs ORDER BY position IS NULL,position,id'))
        return [self.get(json.loads(r[0])['id']) for r in rows]
    def new(self,workflow='generic_script',project_id=None,name=None,environment_id=None,config=None,_run_id=None):
        from .workflows import TEMPLATES
        if workflow not in [t['id'] for t in TEMPLATES]:raise ValueError('未知工作流')
        project_id=project_id or self.projects()[0]['id']
        if project_id not in [p['id'] for p in self.projects()]:raise ValueError('项目不存在')
        ident=idcheck(_run_id) if _run_id else 'r_'+uuid.uuid4().hex
        if _run_id and self.run_dir(ident).exists():return self.get(ident)
        run={'id':ident,'project_id':project_id,'name':name or workflow,'workflow':workflow,'branch':(config or {}).get('branch',''),'status':'DRAFT','created_at':now(),'parent_run_id':None,'source_candidate_id':None,'environment_id':environment_id,'config':config or {},'attempts':[],'candidates':[],'artifacts':[],'source_path':None,'report_url':None,'frozen':False}
        base=self.run_dir(ident)
        for n in ('input','output','attempts'): (base/n).mkdir(parents=True,exist_ok=True)
        key='script' if workflow=='generic_script' else 'notebook'
        source=run['config'].get(key) if workflow in ('generic_script','generic_notebook','sr56') else None
        if source and Path(source).is_file():
            shutil.copy2(source,base/'input'/Path(source).name);run['source_path']=Path(source).name
        self.save(run);self.event(ident,'CREATED',data={'workflow':workflow});return run
    def inspect(self,path):
        p=Path(path).expanduser().resolve()
        if not p.is_file():raise ValueError('输入文件不存在')
        if p.suffix=='.zip':
            with zipfile.ZipFile(p) as z:
                names=[i.filename for i in zip_entries(z) if not i.is_dir()]
                entries=[n for n in names if n.endswith('.ipynb') or (n.endswith('.py') and '/pwb/' not in n and Path(n).name not in ('sr56_feedback.py','check_bundle.py'))]
            return {'entries':entries,'files':len(names),'sha256':sha(p)}
        return {'entries':[p.name],'sha256':sha(p)}
    def import_file(self,path,**kw):
        p=Path(path).expanduser().resolve();config=kw.get('config') or {};info=self.inspect(p)
        entry=config.get('entrypoint')
        if p.suffix=='.zip' and not entry:
            nbs=[n for n in info['entries'] if n.endswith('.ipynb')]
            entries=nbs or info['entries']
            if len(entries)!=1:raise ValueError('压缩包包含多个入口，请在参数 entrypoint 中明确选择：'+', '.join(entries))
            entry=entries[0]
        workflow=kw.pop('workflow',None) or ('generic_notebook' if str(entry or p.name).endswith('.ipynb') else 'generic_script')
        run=self.new(workflow=workflow,**kw);base=self.run_dir(run['id'])/'input'
        if p.suffix=='.zip':
            with zipfile.ZipFile(p) as z:extract(z,base)
            src=inside(base,base/entry)
        else:
            src=base/p.name;shutil.copy2(p,src)
            # The declared SR56 package companions are copied; no parent directory scanning.
            if workflow=='sr56':
                kit=p.parent.parent if p.parent.name=='notebooks' else p.parent
                for n in ('inputs','assets','licenses'):
                    if (kit/n).is_dir():shutil.copytree(kit/n,base/n,symlinks=False)
                if (kit/'sr56_feedback.py').is_file():shutil.copy2(kit/'sr56_feedback.py',base/'sr56_feedback.py')
        if not src.is_file() or src.suffix not in ('.py','.ipynb'):raise ValueError('入口必须是 .py 或 .ipynb')
        if workflow=='sr56':
            from .notebooks import bind_imported_helper
            notebook=bind_imported_helper(read(src))
            atomic(src,notebook)
        run.update(source_path=str(src.relative_to(base)),import_sha256=info['sha256']);self.save(run);return run
    def clone(self,run_id,name=None,config=None,environment_id=None):
        old=self.get(run_id);new=self.new(old['workflow'],old['project_id'],name or old['name']+' · 副本',environment_id or old['environment_id'],config if config is not None else old['config'])
        shutil.copytree(self.run_dir(run_id)/'input',self.run_dir(new['id'])/'input',dirs_exist_ok=True)
        new.update(parent_run_id=run_id,branch=old.get('branch',''),source_path=old['source_path'])
        new['parameter_diff']={k:{'before':old['config'].get(k),'after':new['config'].get(k)} for k in set(old['config'])|set(new['config']) if old['config'].get(k)!=new['config'].get(k)}
        self.save(new);return new
    def _copy_config_inputs(self,config,dest):
        def walk(v,key=''):
            if key in ('weights','weight_references','checkpoint_weights'):return v
            if isinstance(v,dict):return {k:walk(x,k) for k,x in v.items()}
            if isinstance(v,list):return [walk(x,key) for x in v]
            if isinstance(v,str):
                p=Path(v).expanduser()
                if p.is_absolute() and dest.resolve() in p.resolve().parents:return str(p)
                if p.is_absolute() and p.is_dir() and key=='prepared_root':
                    # Prepared topology includes keep their relative paths. Large trajectories/weights excluded.
                    target=dest/'prepared_system';target.mkdir(exist_ok=True)
                    for src in sorted(p.rglob('*')):
                        if src.is_symlink():raise ValueError('prepared_root 包含符号链接，请提供实际文件')
                        if src.is_file() and src.suffix.lower() in ('.gro','.pdb','.cif','.top','.itp','.ndx','.cpt','.mdp','.tpr'):
                            dst=target/src.relative_to(p);dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dst)
                    return str(target)
                if p.is_absolute() and p.is_file():
                    target=dest/'declared_inputs'/sha(p)[:16]/p.name;target.parent.mkdir(parents=True,exist_ok=True)
                    if not target.exists():shutil.copy2(p,target)
                    if key in ('settings','filters','advanced') and p.suffix.lower()=='.json':
                        content=read(p)
                        if not isinstance(content,dict):raise ValueError('工作流 JSON 配置必须为对象')
                        rewritten=walk(content)
                        if rewritten!=content:
                            target=target.with_name('snapshot_'+target.name);atomic(target,rewritten)
                    return str(target)
            return v
        return walk(config)
    def freeze(self,run):
        from .workflows import validate
        env=self.env(run.get('environment_id'));validation_config=dict(run['config'])
        if run.get('source_path'):validation_config['script' if run['workflow']=='generic_script' else 'notebook']=str(self.run_dir(run['id'])/'input'/run['source_path'])
        errors=validate(run['workflow'],validation_config,env)
        if run['workflow'] in ('generic_script','generic_notebook','sr56') and not run.get('source_path'):errors.append('请导入运行入口')
        plans=run['config'].get('downstream',[])
        if not isinstance(plans,list):errors.append('downstream 必须为计划列表')
        else:
            for plan in plans:
                if not isinstance(plan,dict):errors.append('每项自动衔接计划必须为配置对象');continue
                if not plan.get('enabled'):continue
                if plan.get('rule','ranking_score_desc')!='ranking_score_desc':errors.append('首版自动选择规则仅支持 ranking_score_desc')
                if not str(plan.get('workflow','')).startswith('gromacs_'):errors.append('自动衔接只允许 GROMACS 模板')
                if type(plan.get('count')) is not int or plan['count']<1:errors.append('自动衔接候选数量必须为正整数')
                if not isinstance(plan.get('config'),dict) or not plan['config'].get('mdp'):errors.append('自动衔接必须预先填写完整模拟配置和预算')
                try:plan_env=self.env(plan.get('environment_id'))
                except ValueError:errors.append('自动衔接必须绑定实际环境');continue
                plan['environment_sha256']=plan_env['sha256']
                if isinstance(plan.get('config'),dict):
                    errors.extend('自动衔接: '+message for message in validate(plan.get('workflow',''),plan['config'],plan_env,check_structure=False))
        if errors:raise ValueError('；'.join(errors))
        current=environment_fingerprint(env)
        if fingerprint(current)!=env['sha256']:raise Conflict('执行环境发生改变，请重新绑定环境并创建副本')
        base=self.run_dir(run['id']);execution_config=dict(run['config'])
        if run.get('source_path'):execution_config['script' if run['workflow']=='generic_script' else 'notebook']=str(base/'input'/run['source_path'])
        cfg=self._copy_config_inputs(execution_config,base/'input')
        if run['workflow']=='sr56' and cfg.get('parameters'):
            from .notebooks import apply_parameters
            src=base/'input'/run['source_path'];doc=json.loads(src.read_text());doc=apply_parameters(doc,cfg['parameters']);atomic(src,doc)
        files=[{'path':str(p.relative_to(base/'input')),'sha256':sha(p),'bytes':p.stat().st_size} for p in sorted((base/'input').rglob('*')) if p.is_file()]
        snapshot={'schema':'pwb-run-v1','run_id':run['id'],'workflow':run['workflow'],'config':cfg,'config_sha256':fingerprint(run['config']),'execution_config_sha256':fingerprint(cfg),'environment':env,'environment_sha256':env['sha256'],'worker_environment':environment_fingerprint({'python':sys.executable}),'files':files,'source_path':run.get('source_path')}
        atomic(base/'snapshot.json',snapshot);run.update(frozen=True,snapshot_sha256=sha(base/'snapshot.json'),config_sha256=snapshot['config_sha256']);self.save(run)
    def verify_snapshot(self,run):
        base=self.run_dir(run['id']);p=base/'snapshot.json'
        if not p.is_file() or sha(p)!=run.get('snapshot_sha256'):raise Conflict('运行快照校验失败')
        s=read(p)
        if fingerprint(run['config'])!=s['config_sha256']:raise Conflict('固定参数被改变')
        actual=set()
        for file in (base/'input').rglob('*'):
            if file.is_symlink():raise Conflict('输入快照出现符号链接')
            if file.is_file() and '__pycache__' not in file.relative_to(base/'input').parts:actual.add(file.relative_to(base/'input').as_posix())
        if actual!={f['path'] for f in s['files'] if '__pycache__' not in Path(f['path']).parts}:raise Conflict('输入文件集合在提交后改变')
        for f in s['files']:
            file=inside(base/'input',base/'input'/f['path'])
            if not file.is_file() or sha(file)!=f['sha256']:raise Conflict('输入散列不匹配: '+f['path'])
        if run['workflow'] in ('generic_notebook','sr56') and fingerprint(environment_fingerprint({'python':sys.executable}))!=fingerprint(s.get('worker_environment')):raise Conflict('工作台执行器环境发生漂移')
        if fingerprint(environment_fingerprint(s['environment']))!=s['environment_sha256']:raise Conflict('执行环境发生漂移，不能混入原运行')
        return s
    def enqueue(self,run_id,resume=False):
        from .workflows import TEMPLATES
        with self.lock:
            run=self.get(run_id)
            if run['status'] in ACTIVE or run['status']=='QUEUED':raise Conflict('任务已在执行或队列中')
            if resume:
                cap=next(t['capabilities'] for t in TEMPLATES if t['id']==run['workflow'])
                if not cap.get('resume') or run['status'] not in ('STOPPED','FAILED','INTERRUPTED'):raise Conflict('此任务不支持当前状态下的恢复')
                self.verify_snapshot(run)
                for a in run['attempts']:
                    if any(identity_matches(m) for m in a.get('group_members',[])):raise Conflict('旧计算子进程仍在运行')
                    if identity_matches(a.get('process')):raise Conflict('旧进程仍在运行，不能再次启动')
                    kernel=read(self.run_dir(run_id)/'attempts'/a['id']/'kernel_process.json')
                    if identity_matches(kernel) or (kernel and kernel.get('start_ticks') is None and process_identity(kernel['pid'])):raise Conflict('旧内核仍在运行或身份无法确认')
                    if (a.get('process') or {}).get('start_ticks') is None and a.get('process') and process_identity(a['process']['pid']):raise Conflict('旧进程身份无法确认，恢复被阻止')
            elif run['status']!='DRAFT':raise Conflict('新计算请复制为新运行；接续请使用恢复')
            else:self.freeze(run)
            with self.db() as db:
                pos=db.execute('SELECT COALESCE(MAX(position),0)+1 FROM runs').fetchone()[0]
                run['status']='QUEUED';run['resume_requested']=resume
                clean={k:v for k,v in run.items() if k not in ('events','progress')}
                db.execute('UPDATE runs SET payload=?,position=? WHERE id=?',(json.dumps(clean,ensure_ascii=False),pos,run_id))
            self.event(run_id,'QUEUED',data={'resume':resume});return self.get(run_id)
    def reorder(self,run_id,position):
        with self.db() as db:
            ids=[r[0] for r in db.execute("SELECT id FROM runs WHERE json_extract(payload,'$.status')='QUEUED' ORDER BY position")]
            if run_id not in ids:raise Conflict('只能调整等待中的任务')
            i=ids.index(run_id);j=0 if position=='next' else max(0,i-1) if position=='up' else min(len(ids)-1,i+1) if position=='down' else None
            if j is None:raise ValueError('未知排序操作')
            ids.pop(i);ids.insert(j,run_id)
            for n,k in enumerate(ids):db.execute('UPDATE runs SET position=? WHERE id=?',(n+1,k))
    def spool(self,run):
        for a in run['attempts']:
            root=self.run_dir(run['id'])/'attempts'/a['id']/'events'
            for p in sorted(root.glob('*.json')):
                r=read(p)
                if not r:continue
                if r.get('attempt_id',a['id'])!=a['id'] or r.get('run_id',run['id'])!=run['id']:raise Conflict('事件身份冲突')
                self.event(run['id'],r['event'],r.get('stage',''),r.get('key',''),r.get('data',{}),a['id'],int(r.get('local_seq',r.get('seq',int(p.stem)))),ts=r.get('ts'),monotonic_ns=r.get('monotonic_ns'))
                if r['event']=='CANDIDATE':
                    c=r.get('data',{}).get('candidate',r.get('data',{}))
                    if c.get('structure') and c.get('id'):
                        if c.get('attempt_id') and c['attempt_id']!=a['id']:raise Conflict('候选执行批次身份冲突')
                        c=dict(c,attempt_id=a['id']);self.commit_candidate(run,c)
        self.save(run)
    def commit_candidate(self,run,candidate):
        c=dict(candidate);c['id']=str(c['id']);p=inside(self.run_dir(run['id'])/'output',self.run_dir(run['id'])/'output'/c['structure'])
        if not p.is_file():raise Conflict('候选结构尚未完整提交')
        h=sha(p)
        if c.get('structure_sha256') and c['structure_sha256']!=h:raise Conflict('候选结构散列不匹配')
        c.update(structure_sha256=h,run_id=run['id'],attempt_id=c.get('attempt_id',run['attempts'][-1]['id']),committed=True)
        if c.get('sequence'):
            import hashlib;c['sequence_sha256']=hashlib.sha256(c['sequence'].encode()).hexdigest()
        existing=next((x for x in run['candidates'] if x['id']==c['id']),None)
        if existing and existing['structure_sha256']!=h:raise Conflict('候选身份冲突')
        for field in ('sequence','confidence','confidence_sha256','generated_path','mpnn_path'):
            if existing and existing.get(field)!=c.get(field):raise Conflict('候选身份字段冲突: '+field)
        if c.get('confidence'):
            confidence=inside(self.run_dir(run['id'])/'output',self.run_dir(run['id'])/'output'/c['confidence'])
            if not confidence.is_file() or sha(confidence)!=c.get('confidence_sha256'):raise Conflict('候选置信度工件校验失败')
        if not existing:run['candidates'].append(c)
        elif c.get('selection'):existing['selection']=c['selection']
    def child(self,run_id,candidate_id,workflow,config,environment_id,project_id=None,_child_id=None):
        parent=self.get(run_id);c=next((c for c in parent['candidates'] if c['id']==candidate_id and c.get('committed') and c.get('valid_output',True)),None)
        if not c:raise Conflict('请选择已完整提交且有效的候选')
        p=inside(self.run_dir(run_id)/'output',self.run_dir(run_id)/'output'/c['structure'])
        if not p.is_file() or sha(p)!=c['structure_sha256']:raise Conflict('候选文件完整性校验失败')
        if not workflow.startswith('gromacs_'):raise ValueError('候选衔接仅支持已声明的 GROMACS 流程')
        cfg=dict(config);cfg['source_structure']=str(p)
        # CIF conversion must be explicit through the adapter, never merely renamed to PDB.
        cfg.setdefault('input_mode','protein_pdb');cfg['structure']=str(p);cfg['pdb']=str(p)
        run=self.new(workflow,project_id or parent['project_id'],parent['name']+' → MD',environment_id,cfg,_run_id=_child_id)
        if not run.get('frozen'):
            fixed=self.run_dir(run['id'])/'input/candidate_source'/p.name;fixed.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,fixed)
            run['config'].update(pdb=str(fixed),source_structure=str(fixed),structure=str(fixed))
        run.update(parent_run_id=run_id,source_candidate_id=candidate_id,source_partial=parent['status']!='COMPLETED',source_candidate_sha256=c['structure_sha256']);self.save(run);return run
    def state(self):
        from .workflows import TEMPLATES
        runs=self.list_runs()
        with self.db() as db:resources=[json.loads(r[0]) for r in db.execute('SELECT payload FROM resources ORDER BY seq DESC LIMIT 240')][::-1]
        return {'projects':self.projects(),'runs':runs,'queue':[r for r in runs if r['status']=='QUEUED'],'current':next((r for r in runs if r['status'] in ACTIVE),None),'paused':self.setting('paused'),'pause_reason':self.setting('pause_reason'),'resources':resources,'issues':[{'run_id':r['id'],'message':r['error']} for r in runs if r.get('error')],'templates':TEMPLATES,'environments':self.environments(),'synthetic':self.setting('synthetic',False)}

class Supervisor:
    def __init__(self,store):
        self.store=store;self.handles={};self.halt=threading.Event();self.last_sample=0;self.guard=None
    def acquire(self):
        import fcntl
        self.guard=open(self.store.root/'supervisor.lock','a+')
        try:fcntl.flock(self.guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.guard.close();self.guard=None
            raise Conflict('已有工作台后台管理此数据目录')
        self.store.pause(True,'后台启动后等待核对并手动恢复队列')
        for run in self.store.list_runs():
            if run['status'] in ACTIVE:
                kernel=read(self.store.run_dir(run['id'])/'attempts'/run['attempts'][-1]['id']/'kernel_process.json') if run['attempts'] else None
                proc=(run['attempts'][-1].get('process') or read(self.store.run_dir(run['id'])/'attempts'/run['attempts'][-1]['id']/'process.json',{})) if run['attempts'] else {}
                if run['attempts'] and proc:run['attempts'][-1]['process']=proc
                members=run['attempts'][-1].get('group_members',[]) if run['attempts'] else []
                if identity_matches(proc) or identity_matches(kernel) or any(identity_matches(m) for m in members):
                    run['status']='RUNNING';run['orphan_observed']=True;self.store.event(run['id'],'RECONCILED',data={'process_alive':True,'message':'旧执行进程仍在运行'})
                else:
                    run['status']='INTERRUPTED';run['error']='后台恢复：计算进程已结束或身份待确认'
                    run['ownership_uncertain']=any(bool(x and process_identity(x.get('pid',-1))) for x in (proc,kernel));self.store.event(run['id'],'INTERRUPTED',data={'process':proc})
                self.store.save(run)
    def start(self):self.acquire();self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()
    def close(self):
        self.halt.set()
        if hasattr(self,'thread'):self.thread.join(timeout=7)
        if (not hasattr(self,'thread') or not self.thread.is_alive()) and self.guard:self.guard.close()
    def launch(self,run):
        if self.halt.is_set():return
        s=self.store.verify_snapshot(run)
        if self.halt.is_set():return
        base=self.store.run_dir(run['id']);aid='a_'+uuid.uuid4().hex[:16];adir=base/'attempts'/aid;adir.mkdir()
        out=base/'output';(out/'control').mkdir(exist_ok=True)
        for name in ('stop.json','safe_stopped.json'):
            p=out/'control'/name
            if p.exists():p.unlink()
        env=os.environ.copy();env.update(PWB_DATA_ROOT=str(self.store.root),PWB_RUN_ID=run['id'],PWB_ATTEMPT_ID=aid,PWB_OUTPUT_DIR=str(out),PWB_ATTEMPT_DIR=str(adir),NESPRIN_RUN_ROOT=str(out),PWB_PACKAGE_ROOT=str(PACKAGE),PWB_NOTEBOOK_PATH=str(base/'input'/s['source_path']) if s.get('source_path') else '')
        env['PYTHONDONTWRITEBYTECODE']='1'
        env['PYTHONPATH']=str(PACKAGE.parent)+os.pathsep+env.get('PYTHONPATH','');env['PWB_INPUT_DIR']=str(base/'input');atomic(adir/'request.json',{'snapshot':s,'run_id':run['id'],'attempt_id':aid,'resume':run.get('resume_requested',False),'input_dir':str(base/'input'),'output_dir':str(out),'attempt_dir':str(adir)})
        if self.halt.is_set():return
        run['attempts'].append({'id':aid,'started_at':now(),'status':'STARTING','process':None,'environment_sha256':s['environment_sha256']});run.update(status='STARTING',error=None);self.store.save(run)
        log=(adir/'worker.log').open('ab')
        try:p=subprocess.Popen([sys.executable if s['workflow'] in ('generic_notebook','sr56') else s['environment']['python'],'-m','pwb.worker',str(adir/'request.json')],cwd=base/'input',env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        finally:log.close()
        proc=process_identity(p.pid);run['attempts'][-1].update(status='RUNNING',process=proc);run.update(status='RUNNING',orphan_observed=False,error=None,force_stop=False,ownership_uncertain=False);self.store.save(run);self.store.event(run['id'],'ATTEMPT_START',key=aid,data={'process':proc},attempt_id=aid);self.handles[run['id']]=p
    def stop(self,run_id,force=False):
        with self.store.lock:return self._stop_locked(run_id,force)
    def _stop_locked(self,run_id,force=False):
        run=self.store.get(run_id)
        if run['status'] not in ACTIVE:raise Conflict('任务当前未运行')
        from .workflows import TEMPLATES
        cap=next(t['capabilities'] for t in TEMPLATES if t['id']==run['workflow'])
        if not force and not cap.get('safe_stop'):raise Conflict('此流程不支持安全保存边界，可使用强制终止')
        a=run['attempts'][-1];base=self.store.run_dir(run_id)
        if force:
            owned_members=[m for m in a.get('group_members',[]) if identity_matches(m)]
            kernel=read(base/'attempts'/a['id']/'kernel_process.json')
            if identity_matches(kernel):
                if kernel['pgid']==kernel['pid']:os.killpg(kernel['pgid'],signal.SIGTERM)
                else:os.kill(kernel['pid'],signal.SIGTERM)
            p=self.handles.get(run_id);old=a.get('process')
            if not ((p and p.poll() is None) or identity_matches(old) or owned_members or identity_matches(kernel)):raise Conflict('无法确认进程所有权，未发送终止信号')
            if not old or old['pgid']!=old['pid']:raise Conflict('进程组身份异常，未终止')
            if (p and p.poll() is None) or identity_matches(old) or owned_members:os.killpg(old['pgid'],signal.SIGTERM)
            run['force_stop']=True;run['force_stop_at']=time.time()
        else:atomic(base/'output/control/stop.json',{'run_id':run_id,'attempt_id':a['id'],'requested_at':now()})
        run['status']='STOPPING';self.store.save(run);self.store.event(run_id,'STOP_REQUESTED',key=a['id'],data={'force':force})
    def collect(self,run):
        self.store.spool(run);a=run['attempts'][-1];base=self.store.run_dir(run['id']);receipt=read(base/'attempts'/a['id']/'result.json');p=self.handles.get(run['id'])
        alive=(p.poll() is None) if p else identity_matches(a.get('process'))
        if alive:
            a['group_members']=process_group_members((a.get('process') or {}).get('pgid'))
            self.store.save(run)
            if run.get('force_stop') and time.time()-run.get('force_stop_at',time.time())>5:
                owned=a.get('process') or {}
                if (p and p.poll() is None) or identity_matches(owned):os.killpg(owned['pgid'],signal.SIGKILL)
            return
        # Once the leader is gone, its numeric process-group ID can be reused.
        # Only identities recorded while the leader was alive remain owned.
        if run.get('force_stop') and time.time()-run.get('force_stop_at',time.time())>5:
            # A parent can exit before a resistant child. Signal only identities
            # verified for this attempt; never use an unverified old process group.
            for member in a.get('group_members',[]):
                if identity_matches(member):os.kill(member['pid'],signal.SIGKILL)
            owned_kernel=read(base/'attempts'/a['id']/'kernel_process.json')
            if identity_matches(owned_kernel):os.kill(owned_kernel['pid'],signal.SIGKILL)
        if any(identity_matches(member) for member in a.get('group_members',[])):
            self.store.pause(True,'执行器结束但其子进程仍在运行，需先处理原任务');return
        kernel=read(base/'attempts'/a['id']/'kernel_process.json')
        if identity_matches(kernel):
            self.store.pause(True,'执行器结束但其内核仍在运行，需先处理原任务');return
        if p:self.handles.pop(run['id'],None)
        if receipt and receipt.get('attempt_id')==a['id'] and receipt.get('run_id')==run['id']:
            status=receipt.get('status','FAILED')
            if status not in TERMINAL:status='FAILED'
            if p and p.returncode not in (None,0) and status=='COMPLETED':status='FAILED';receipt['error']='进程退出码与完成收据冲突'
            for c in receipt.get('candidates',[]):self.store.commit_candidate(run,c)
            run['artifacts']=receipt.get('artifacts',run.get('artifacts',[]));run['error']=receipt.get('error')
        else:status='STOPPED' if run.get('force_stop') else 'INTERRUPTED';run['error']='进程结束，未提交完整结束记录；保留最近有效产物'
        if run.get('force_stop'):status='STOPPED'
        if run['workflow']=='bindcraft' and status!='COMPLETED':
            try:
                from .workflows import collect_partial_candidates
                snapshot=self.store.verify_snapshot(run)
                def partial_event(event,stage='',key='',data=None,**kwargs):
                    self.store.event(run['id'],event,stage,key,data or kwargs,attempt_id=a['id'])
                partial=collect_partial_candidates(run['workflow'],snapshot['config'],snapshot['environment'],base/'output',partial_event)
                for candidate in partial['candidates']:self.store.commit_candidate(run,candidate)
            except Exception as collection_error:
                self.store.event(run['id'],'PARTIAL_COLLECTION_WARNING','bindcraft',data={'message':str(collection_error)},attempt_id=a['id'])
        a.update(status=status,ended_at=now());run.update(status=status,ended_at=now());self.store.save(run);self.store.event(run['id'],'ATTEMPT_END',key=a['id'],data={'status':status},attempt_id=a['id'])
        error=run.get('error') or ''
        log=base/'attempts'/a['id']/'worker.log'
        if log.is_file():
            with log.open('rb') as f:f.seek(max(0,log.stat().st_size-100000));error+='\n'+f.read().decode(errors='replace')
        if any(x in error.lower() for x in ('out of memory','memoryerror','cannot allocate memory','[errno 12]','no space left','disk full','cuda error: out of memory')):self.store.pause(True,'资源异常，等待检查后恢复')
        try:
            from .feedback import export_run
            export_run(self.store,run['id'])
        except Exception as e:self.store.event(run['id'],'REPORT_ERROR',data={'message':str(e)})
        if status=='COMPLETED':self.auto_children(run)
    def auto_children(self,run):
        # Deterministic child identity survives a crash between creation and trigger commit.
        snapshot=read(self.store.run_dir(run['id'])/'snapshot.json',{})
        plans=snapshot.get('config',run['config']).get('downstream',[])
        for plan in plans:
            if not plan.get('enabled'):continue
            if self.store.env(plan['environment_id'])['sha256']!=plan.get('environment_sha256'):raise Conflict('下游环境绑定在提交后改变')
            ph=fingerprint(plan);count=int(plan['count'])
            def key(c):
                value=c.get('ranking_score');finite=isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)
                return (not finite,-value if finite else 0,c['id'])
            eligible=[c for c in run['candidates'] if c.get('committed') and c.get('valid_output',True)]
            if run['workflow']=='sr56':eligible=[c for c in eligible if c.get('selection',{}).get('selection')!='pair']
            for c in sorted(eligible,key=key)[:count]:
                cid='r_'+fingerprint([run['id'],c['id'],ph])[:32]
                with self.store.db() as db:prior=db.execute('SELECT child FROM triggers WHERE parent=? AND candidate=? AND plan=?',(run['id'],c['id'],ph)).fetchone()
                if prior:
                    child=self.store.get(prior[0])
                else:
                    child=self.store.child(run['id'],c['id'],plan['workflow'],plan['config'],plan['environment_id'],_child_id=cid)
                    with self.store.db() as db:db.execute('INSERT OR IGNORE INTO triggers VALUES(?,?,?,?)',(run['id'],c['id'],ph,child['id']))
                if child['status']=='DRAFT':
                    try:self.store.enqueue(child['id'])
                    except Exception as exc:self.store.event(run['id'],'DOWNSTREAM_BLOCKED',key=c['id'],data={'child_id':child['id'],'message':str(exc)})
    def tick(self):
        with self.store.lock:self._tick_locked()
    def _tick_locked(self):
        runs=self.store.list_runs();active=[r for r in runs if r['status'] in ACTIVE]
        for r in active:
            try:self.collect(r)
            except Exception as e:self.store.pause(True,'证据核对失败: '+str(e))
        if time.monotonic()-self.last_sample>=15:
            r=next((r for r in runs if r['status'] in ACTIVE),None);pid=r['attempts'][-1]['process']['pid'] if r and r['attempts'] and r['attempts'][-1].get('process') else None
            sample=sample_resources(pid,self.store.root)
            with self.store.db() as db:db.execute('INSERT INTO resources(payload) VALUES(?)',(json.dumps(sample),))
            if r:self.store.event(r['id'],'RESOURCE',data=sample)
            self.last_sample=time.monotonic()
        if self.store.setting('paused') or any(r['status'] in ACTIVE for r in self.store.list_runs()):return
        queued=next((r for r in self.store.list_runs() if r['status']=='QUEUED'),None)
        if queued:
            sample=sample_resources(root=self.store.root)
            if sample.get('gpu_processes'):
                self.store.set_setting('pause_reason','GPU 被外部进程占用，等待释放');return
            try:self.launch(queued)
            except Exception as e:
                queued.update(status='BLOCKED',error=str(e));self.store.save(queued);self.store.event(queued['id'],'PREFLIGHT_FAILED',data={'message':str(e)})
    def loop(self):
        try:
            while not self.halt.is_set():
                try:self.tick()
                except Exception as e:self.store.pause(True,'后台核对失败: '+str(e))
                self.halt.wait(1)
        finally:
            if self.guard:self.guard.close()
