import json, pathlib, subprocess, sys, tempfile, time, unittest, zipfile
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from pwb.core import Store,Supervisor,Conflict,process_identity
from pwb.util import atomic
from pwb.feedback import export_run,verify,merge

class CoreChecks(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.temp.name);self.s=Store(self.root/'data');self.e=self.s.environment('CPU test',sys.executable)
 def tearDown(self):self.temp.cleanup()
 def script(self,text='print("committed")',name='script'):
  p=self.root/(name+'.py');p.write_text(text);return self.s.import_file(str(p),environment_id=self.e['id'],config={'argv':[]},name=name)
 def finish(self,supervisor,run,timeout=15):
  end=time.monotonic()+timeout
  while time.monotonic()<end:
   supervisor.tick();r=self.s.get(run['id'])
   if r['status'] in ('COMPLETED','FAILED','STOPPED','INTERRUPTED','BLOCKED'):return r
   time.sleep(.05)
  self.fail('worker timeout')
 def test_seventh_run_clone_preserves_history(self):
  runs=[self.script(name=str(i)) for i in range(6)];c=self.s.clone(runs[0]['id'],config={'argv':['new']})
  self.assertEqual(len(self.s.list_runs()),7);self.assertEqual(c['parent_run_id'],runs[0]['id']);self.assertEqual(self.s.get(runs[0]['id'])['config']['argv'],[])
  self.assertEqual(list((self.s.run_dir(c['id'])/'output').iterdir()),[])
 def test_queue_reorder_and_fixed_snapshot(self):
  rs=[self.script(name=str(i)) for i in range(3)]
  for r in rs:self.s.enqueue(r['id'])
  self.s.reorder(rs[2]['id'],'next');self.assertEqual(self.s.state()['queue'][0]['id'],rs[2]['id'])
  p=self.s.run_dir(rs[2]['id'])/'input'/rs[2]['source_path'];p.write_text('print("changed")')
  with self.assertRaises(Conflict):self.s.verify_snapshot(self.s.get(rs[2]['id']))
 def test_cpu_execution_browser_independent_and_report(self):
  r=self.script();self.s.enqueue(r['id']);sup=Supervisor(self.s);sup.acquire();self.s.pause(False)
  try:
   done=self.finish(sup,r);self.assertEqual(done['status'],'COMPLETED');self.assertEqual(len(done['attempts']),1)
   self.assertTrue(pathlib.Path(done['feedback_path']).exists());m=verify(done['feedback_path']);self.assertTrue(m['computation_complete'])
   self.assertEqual(done['progress']['completed'],1)
  finally:sup.close()
 def test_ordinary_failure_continues_independent(self):
  bad=self.script('raise RuntimeError("failure fixture")','bad');good=self.script(name='good')
  self.s.enqueue(bad['id']);self.s.enqueue(good['id']);sup=Supervisor(self.s);sup.acquire();self.s.pause(False)
  try:self.assertEqual(self.finish(sup,bad)['status'],'FAILED');self.assertEqual(self.finish(sup,good)['status'],'COMPLETED')
  finally:sup.close()
 def test_resource_failure_pauses(self):
  for i,source in enumerate(('raise RuntimeError("CUDA out of memory")','raise MemoryError()','raise OSError(12,"Cannot allocate memory")','raise OSError(28,"No space left on device")')):
   with self.subTest(source=source):
    r=self.script(source,'resource_'+str(i));self.s.enqueue(r['id']);sup=Supervisor(self.s);sup.acquire();self.s.pause(False)
    try:self.assertEqual(self.finish(sup,r)['status'],'FAILED');self.assertTrue(self.s.setting('paused'))
    finally:sup.close()
 def test_force_stop_owned_process_only(self):
  r=self.script('import time\ntime.sleep(100)');self.s.enqueue(r['id']);sup=Supervisor(self.s);sup.acquire();self.s.pause(False)
  try:
   sup.tick();self.assertEqual(self.s.get(r['id'])['status'],'RUNNING')
   with self.assertRaises(Conflict):sup.stop(r['id'],False)
   sup.stop(r['id'],True);self.assertEqual(self.finish(sup,r)['status'],'STOPPED')
  finally:sup.close()
 def test_restart_always_paused_and_generic_resume_rejected(self):
  r=self.script();r['status']='INTERRUPTED';self.s.save(r)
  with self.assertRaises(Conflict):self.s.enqueue(r['id'],resume=True)
  sup=Supervisor(self.s);sup.acquire()
  try:self.assertTrue(self.s.setting('paused'))
  finally:sup.close()
 def test_event_dedup_and_conflict_monotonic(self):
  r=self.script();first=self.s.event(r['id'],'COMMITTED','s','x',{'ok':1},'a',1)
  self.assertEqual(first,self.s.event(r['id'],'COMMITTED','s','x',{'ok':1},'a',1))
  with self.assertRaises(Conflict):self.s.event(r['id'],'COMMITTED','s','x',{'ok':2},'a',1)
  self.assertGreater(Store(self.s.root).event(r['id'],'REUSED','s','x',{},'b',1),first)
  progress=self.s.progress(self.s.events(r['id']));self.assertEqual(progress['completed'],1);self.assertEqual(progress['new'],1)
 def test_env_drift_rejected(self):
  r=self.script();self.s.enqueue(r['id'])
  with patch('pwb.core.environment_fingerprint',return_value={'different':True}):
   with self.assertRaises(Conflict):self.s.verify_snapshot(self.s.get(r['id']))
 def test_foreign_gpu_waits(self):
  r=self.script();self.s.enqueue(r['id']);sup=Supervisor(self.s);sup.acquire();self.s.pause(False)
  try:
   with patch('pwb.core.sample_resources',return_value={'ts':'t','gpu_processes':[{'pid':999999}]}):sup.tick()
   self.assertEqual(self.s.get(r['id'])['status'],'QUEUED');self.assertIn('外部',self.s.setting('pause_reason'))
  finally:sup.close()
 def test_safe_zip_and_snapshot_merge(self):
  r=self.script();z1=export_run(self.s,r['id']);z2=export_run(self.s,r['id']);dest=self.root/'local'
  merge([z2,z1,z1],dest);index=json.loads((dest/'imports.json').read_text());self.assertEqual(len(index['imports']),2)
  html=(dest/'index.html').read_text();self.assertEqual(html.count('<article class="card">'),1);self.assertIn('快照 2',html)
  with zipfile.ZipFile(self.root/'bad.zip','w') as z:z.writestr('../x','escape')
  with self.assertRaises(ValueError):verify(self.root/'bad.zip')
  # Same snapshot sequence with different bytes is a conflict, even with valid fresh hashes.
  unpack=self.root/'unpack'
  with zipfile.ZipFile(z1) as z:z.extractall(unpack)
  m=json.loads((unpack/'MANIFEST.json').read_text());m['created_at']='different';atomic(unpack/'MANIFEST.json',m)
  conflict=self.root/'conflict.zip'
  with zipfile.ZipFile(conflict,'w') as z:
   for p in unpack.rglob('*'):
    if p.is_file():z.write(p,p.relative_to(unpack))
  with self.assertRaises(ValueError):merge([conflict],dest)
 def test_all_zero_still_exports_fixed_headers(self):
  r=self.script();z=export_run(self.s,r['id'])
  with zipfile.ZipFile(z) as f:self.assertIn('没有已完整提交',f.read('index.html').decode());self.assertEqual(json.loads(f.read('report.json'))['run']['candidates'],[])
 def test_candidate_child_partial_and_hash_failure(self):
  r=self.script();r['status']='FAILED';r['attempts']=[{'id':'a_test','started_at':'t','status':'FAILED'}];p=self.s.run_dir(r['id'])/'output/model.pdb';p.write_text('ATOM fixture')
  self.s.commit_candidate(r,{'id':'c1','structure':'model.pdb','valid_output':True,'sequence':'AAA'});self.s.save(r)
  child=self.s.child(r['id'],'c1','gromacs_md',{},self.e['id']);self.assertTrue(child['source_partial']);self.assertEqual(child['source_candidate_id'],'c1')
  p.write_text('changed')
  with self.assertRaises(Conflict):self.s.child(r['id'],'c1','gromacs_md',{},self.e['id'])
 def test_real_notebook_bound_kernel_error_preserves_output(self):
  import socket
  try:
   with socket.socket() as probe:probe.bind(('127.0.0.1',0))
  except PermissionError:self.skipTest('sandbox prohibits notebook kernel loopback sockets; authorized live kernel smoke NOT RUN')
  try:
   import nbformat
   __import__('nbclient')
  except ImportError:self.skipTest('NBClient unavailable')
  doc=nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell('print("NBClient owned kernel fixture")'),nbformat.v4.new_code_cell('raise ValueError("cell failure fixture")'),nbformat.v4.new_code_cell('raise AssertionError("must not execute")')])
  source=self.root/'fixture.ipynb';nbformat.write(doc,source)
  r=self.s.import_file(str(source),environment_id=self.e['id'],config={});self.s.enqueue(r['id']);sup=Supervisor(self.s);sup.acquire();self.s.pause(False)
  try:
   done=self.finish(sup,r,timeout=35);self.assertEqual(done['status'],'FAILED')
   executed=self.s.run_dir(r['id'])/'attempts'/done['attempts'][0]['id']/'executed.ipynb';self.assertTrue(executed.exists());nb=nbformat.read(executed,as_version=4)
   self.assertIn('NBClient owned',nb.cells[0].outputs[0].text);self.assertEqual(nb.cells[1].outputs[-1].ename,'ValueError');self.assertEqual(nb.cells[2].execution_count,None)
  finally:sup.close()
 def test_orphan_recovery_linux(self):
  if not pathlib.Path('/proc').is_dir():self.skipTest('Linux process identity smoke; macOS CPU tests cannot prove Linux adoption')
  p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
  try:
   r=self.script();r.update(status='RUNNING',attempts=[{'id':'a_test','started_at':'t','status':'RUNNING','process':process_identity(p.pid)}]);self.s.save(r);sup=Supervisor(self.s);sup.acquire()
   try:self.assertEqual(self.s.get(r['id'])['status'],'RUNNING');self.assertTrue(self.s.setting('paused'))
   finally:sup.close()
  finally:p.terminate();p.wait()
if __name__=='__main__':unittest.main()
