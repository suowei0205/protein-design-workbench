"""Owned background worker. Never imports/executes an uploaded notebook during ingestion."""
import os, subprocess, sys, traceback, math, time
from pathlib import Path
from .util import atomic, now, read, sha

class Emitter:
    def __init__(self,req):self.req=req;self.seq=0;self.root=Path(req['attempt_dir'])/'events'
    def __call__(self,event,stage='',key='',data=None,**kwargs):
        self.seq+=1;payload=data or kwargs
        def clean(x):
            if isinstance(x,dict):return {str(k):clean(v) for k,v in x.items()}
            if isinstance(x,(tuple,list)):return [clean(v) for v in x]
            if isinstance(x,float) and not math.isfinite(x):return None
            if isinstance(x,Path):return str(x)
            return x
        row={'local_seq':self.seq,'run_id':self.req['run_id'],'attempt_id':self.req['attempt_id'],'ts':now(),'monotonic_ns':time.monotonic_ns(),'event':event,'stage':stage,'key':str(key),'data':clean(payload)}
        atomic(self.root/f'{self.seq:020d}.json',row)

def notebook(req):
    import nbformat
    from nbclient import NotebookClient
    from jupyter_client.kernelspec import KernelSpecManager
    from jupyter_client import KernelManager
    import tempfile
    s=req['snapshot'];path=Path(req['input_dir'])/s['source_path'];nb=nbformat.read(path,as_version=4)
    # Explicit kernel spec prevents stale notebook metadata selecting a different environment.
    with tempfile.TemporaryDirectory(prefix='pwb-kernel-',dir=req['attempt_dir']) as d:
        kernel=Path(d)/'pwb-owned';kernel.mkdir()
        atomic(kernel/'kernel.json',{'argv':[s['environment']['python'],'-m','ipykernel_launcher','-f','{connection_file}'],'display_name':'Workbench owned kernel','language':'python'})
        manager=KernelSpecManager(kernel_dirs=[d]);km=KernelManager(kernel_name='pwb-owned',kernel_spec_manager=manager)
        client=NotebookClient(nb,km=km,timeout=None,kernel_name='pwb-owned',resources={'metadata':{'path':req['input_dir']}},allow_errors=False,store_widget_state=False)
        def track_kernel(**kwargs):
            pid=getattr(getattr(client.km,'provisioner',None),'pid',None)
            if pid:
                from .core import process_identity
                atomic(Path(req['attempt_dir'])/'kernel_process.json',process_identity(pid))
        client.on_cell_start=track_kernel
        try:client.execute()
        finally:
            try:
                if km.has_kernel:km.shutdown_kernel(now=True)
            finally:nbformat.write(nb,str(Path(req['attempt_dir'])/'executed.ipynb'))
    return {'status':'COMPLETED','artifacts':[{'path':str(Path(req['attempt_dir'])/'executed.ipynb'),'kind':'executed_notebook'}],'candidates':[]}

def execute(req,emit):
    s=req['snapshot'];flow=s['workflow'];cfg=s['config'];out=Path(req['output_dir'])
    if flow in ('generic_notebook','sr56'):
        # SR56 bridge emits its own local sequence, avoid Emitter collision.
        if flow=='generic_notebook':emit('START','notebook',key='notebook');emit('PLAN','notebook',data={'actual':1})
        result=notebook(req)
        if flow=='generic_notebook':emit('COMMITTED','notebook',key='notebook');emit('STAGE_END','notebook')
        return result
    if flow=='generic_script':
        source=Path(req['input_dir'])/s['source_path'];argv=cfg.get('argv',[])
        if not isinstance(argv,list) or not all(isinstance(a,str) for a in argv):raise ValueError('argv 必须是字符串数组')
        emit('PLAN','script',data={'actual':1});emit('START','script',key='script')
        env=os.environ.copy();env['PWB_OUTPUT_DIR']=str(out)
        subprocess.run([s['environment']['python'],str(source),*argv],cwd=out,env=env,check=True)
        emit('COMMITTED','script',key='script');emit('STAGE_END','script')
        return {'status':'COMPLETED','candidates':[],'artifacts':[]}
    from .workflows import execute as workflow_execute
    return workflow_execute(flow,cfg,s['environment'],out,emit)

def committed_stop_matches(req):
    control=Path(req['output_dir'])/'control'
    request=read(control/'stop.json',{});marker=read(control/'safe_stopped.json',{})
    if not isinstance(request,dict) or not isinstance(marker,dict):return False
    return (all(record.get(key)==req[key] for record in (request,marker) for key in ('run_id','attempt_id'))
            and bool(marker.get('stage')) and (marker.get('event')=='SAFE_STOP' or bool(marker.get('boundary'))))

def main():
    req=read(sys.argv[1]);emit=Emitter(req);result={};code=0
    from .core import process_identity
    atomic(Path(req['attempt_dir'])/'process.json',process_identity(os.getpid()))
    try:
        result=execute(req,emit)
        if result.get('status')=='STOPPED' and not committed_stop_matches(req):
            raise RuntimeError('停止记录缺少匹配当前运行和批次的请求与提交边界')
    except BaseException as exc:
        # NBClient transports the exception by ename, not the Python class.
        named_safe=type(exc).__name__=='SafeStop' or getattr(exc,'ename',None)=='SafeStop'
        is_safe=named_safe and committed_stop_matches(req)
        status='STOPPED' if is_safe else 'FAILED';code=0 if is_safe else 1
        result={'status':status,'error':None if is_safe else ''.join(traceback.format_exception(exc))[-40000:],'candidates':[],'artifacts':[]}
        traceback.print_exc()
        if req['snapshot']['workflow']!='sr56':emit('SAFE_STOP' if is_safe else 'ERROR','worker',data={'message':str(exc)})
        if req['snapshot']['workflow']=='bindcraft':
            try:
                from .workflows import collect_partial_candidates
                s=req['snapshot'];partial=collect_partial_candidates(s['workflow'],s['config'],s['environment'],req['output_dir'],emit)
                result['candidates']=partial['candidates']
            except Exception as collection_error:
                emit('PARTIAL_COLLECTION_WARNING','bindcraft',data={'message':str(collection_error)})
    result.update(run_id=req['run_id'],attempt_id=req['attempt_id'],ended_at=now())
    # Finished artifact inventory contains paths/hashes, trajectories indexed but not packed by default.
    inventory=[]
    out=Path(req['output_dir'])
    for p in sorted(out.rglob('*')):
        if p.is_file() and not p.is_symlink() and 'control' not in p.relative_to(out).parts:
            try:inventory.append({'path':str(p.relative_to(out)),'bytes':p.stat().st_size,'sha256':sha(p),'kind':'trajectory' if p.suffix in ('.xtc','.trr','.tng') else 'data'})
            except OSError:pass
    result['artifacts']=inventory;atomic(Path(req['attempt_dir'])/'result.json',result)
    return code
if __name__=='__main__':sys.exit(main())
