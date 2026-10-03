import json,pathlib,sys,tempfile,unittest,zipfile,hashlib,time,signal
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from pwb.core import Store,Supervisor,Conflict
from pwb.feedback import export_run,merge,verify
from pwb.util import atomic
class IntegrityChecks(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.p=pathlib.Path(self.t.name);self.s=Store(self.p/'data');self.e=self.s.environment('test',sys.executable);self.src=self.p/'a.py';self.src.write_text('print("ok")');self.r=self.s.import_file(str(self.src),environment_id=self.e['id'],config={})
 def tearDown(self):self.t.cleanup()
 def tamper(self,path,mutate):
  with zipfile.ZipFile(path) as z:files={n:z.read(n) for n in z.namelist()}
  m=json.loads(files['MANIFEST.json']);mutate(files,m)
  for item in m['files']:
   data=files[item['path']];item.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
  files['MANIFEST.json']=json.dumps(m).encode();dst=self.p/('tamper'+str(len(list(self.p.glob('tamper*'))))+'.zip')
  with zipfile.ZipFile(dst,'w') as z:
   for n,data in files.items():z.writestr(n,data)
  return dst
 def test_fixed_snapshot_content_not_selfassertion(self):
  self.s.enqueue(self.r['id']);z=export_run(self.s,self.r['id'])
  def change(files,m):files['run_snapshot.json']=b'{"run_id":"r_wrong"}'
  with self.assertRaises(ValueError):verify(self.tamper(z,change))
 def test_event_evidence_identity_and_prefix(self):
  z1=export_run(self.s,self.r['id']);z2=export_run(self.s,self.r['id'])
  def change(files,m):
   report=json.loads(files['report.json']);report['events'][0]['data']={'changed':True};files['report.json']=json.dumps(report).encode()
  zbad=self.tamper(z2,change);verify(zbad)
  merge([z1],self.p/'local')
  with self.assertRaises(ValueError):merge([zbad],self.p/'local')
  def false_seq(files,m):
   report=json.loads(files['report.json']);report['run']['evidence_seq']=999;m['evidence_seq']=999;files['report.json']=json.dumps(report).encode()
  with self.assertRaises(ValueError):verify(self.tamper(z1,false_seq))
 def test_added_input_blocks_resume(self):
  self.s.enqueue(self.r['id']);(self.s.run_dir(self.r['id'])/'input/helper.py').write_text('evil shadow module')
  with self.assertRaises(Conflict):self.s.verify_snapshot(self.s.get(self.r['id']))
 def test_candidate_old_attempt_and_identity_conflict(self):
  r=self.r;r['attempts']=[{'id':'a1'},{'id':'a2'}];f=self.s.run_dir(r['id'])/'output/a.pdb';f.write_text('x')
  c={'id':'c1','structure':'a.pdb','sequence':'AAA','attempt_id':'a1'};self.s.commit_candidate(r,c);self.assertEqual(r['candidates'][0]['attempt_id'],'a1')
  with self.assertRaises(Conflict):self.s.commit_candidate(r,{**c,'sequence':'BBB'})
 def test_failure_does_not_become_complete(self):
  events=[{'seq':i+1,'ts':'t','event':ev,'stage':'s','key':'k','data':d} for i,(ev,d) in enumerate([('PLAN',{'actual':1}),('COMMITTED',{}),('ERROR',{})])]
  self.assertEqual(self.s.progress(events)['stages'][0]['status'],'FAILED')
 def test_uncertain_ownership_rechecked(self):
  r=self.r;r.update(ownership_uncertain=True,attempts=[{'id':'a1','process':{'pid':999999}}]);self.s.save(r)
  with patch('pwb.core.process_identity',return_value={'pid':999999}):
   with self.assertRaises(Conflict):self.s.pause(False)
  with patch('pwb.core.process_identity',return_value=None):self.s.pause(False)
  self.assertFalse(self.s.get(r['id'])['ownership_uncertain'])
 def test_input_source_external_edits_do_not_change_snapshot(self):
  self.s.enqueue(self.r['id']);self.src.write_text('changed original afterenqueue');self.s.verify_snapshot(self.s.get(self.r['id']))
 def test_force_stop_escalates_owned_child_after_parent_exit(self):
  r=self.r;child={'pid':923456,'boot_id':'fixture','start_ticks':'17','pgid':923455}
  r.update(status='STOPPING',force_stop=True,force_stop_at=time.time()-10,attempts=[{'id':'a1','process':{'pid':923455,'boot_id':'fixture','pgid':923455},'group_members':[child]}]);self.s.save(r)
  sup=Supervisor(self.s)
  with patch('pwb.core.read_boot_id',return_value='fixture'),patch('pwb.core.process_group_members',return_value=[child]),patch('pwb.core.identity_matches',side_effect=lambda x:x==child),patch('pwb.core.os.kill') as kill:
   sup.collect(r);kill.assert_called_once_with(child['pid'],signal.SIGKILL)
  self.assertTrue(self.s.setting('paused'));self.assertEqual(self.s.get(r['id'])['status'],'STOPPING')
 def test_offline_structure_payload_and_native_shortlist(self):
  r=self.r;r['attempts']=[{'id':'a1','status':'FAILED','started_at':'t'}]
  for i in range(12):
   rel=f'c{i:02d}.pdb';f=self.s.run_dir(r['id'])/'output'/rel;f.write_text('REMARK </script><script>bad()</script>\nEND\n')
   self.s.commit_candidate(r,{'id':f'c{i:02d}','structure':rel,'sequence':'AAA','ranking_score':None if i==11 else i})
  self.s.save(r);z=export_run(self.s,r['id']);verify(z)
  with zipfile.ZipFile(z) as archive:
   page=archive.read('index.html').decode();self.assertIn('data-offline-candidates',page);self.assertNotIn('<script>bad()',page)
   payload=json.loads(page.split('<script id="structure-data" type="application/json">')[1].split('</script>')[0])
   self.assertEqual({x['id'] for x in payload},{f'c{i:02d}' for i in range(1,11)})
   report=json.loads(archive.read('report.json'));self.assertEqual(len(report['run']['candidates']),12)
   self.assertFalse(report['computation_complete'])
 def test_auto_children_idempotent_partial_no_auto(self):
  # Mock scientific queue validation; actual scientific params separately workflow tested.
  r=self.r;r['status']='COMPLETED';r['attempts']=[{'id':'a1','started_at':'t','status':'COMPLETED'}]
  f=self.s.run_dir(r['id'])/'output/model.pdb';f.write_text('structurefixture');self.s.commit_candidate(r,{'id':'c1','structure':'model.pdb','sequence':'AA'})
  plan={'enabled':True,'rule':'ranking_score_desc','count':1,'workflow':'gromacs_md','environment_id':self.e['id'],'environment_sha256':self.e['sha256'],'config':{}}
  r['config']['downstream']=[plan];self.s.save(r);sup=Supervisor(self.s)
  with patch.object(self.s,'enqueue',side_effect=lambda ident:self.s.get(ident)):sup.auto_children(r);sup.auto_children(r)
  children=[x for x in self.s.list_runs() if x['parent_run_id']==r['id']];self.assertEqual(len(children),1)
 def test_automatic_downstream_scientific_fields_block_upstream(self):
  r=self.r;r['config']['downstream']=[{'enabled':True,'count':1,'workflow':'gromacs_md','environment_id':self.e['id'],'config':{'mdp':{'production':{'nsteps':100}}}}];self.s.save(r)
  with self.assertRaisesRegex(ValueError,'自动衔接'):self.s.enqueue(r['id'])
  self.assertFalse(self.s.get(r['id'])['frozen']);self.assertEqual(self.s.get(r['id'])['status'],'DRAFT')
 def test_failed_bindcraft_recovery_preserves_failed_status(self):
  r=self.r;r.update(workflow='bindcraft',status='RUNNING',attempts=[{'id':'a1','status':'RUNNING','started_at':'t','process':{}}]);self.s.save(r)
  out=self.s.run_dir(r['id'])/'output';(out/'accepted.pdb').write_text('fixture')
  atomic(self.s.run_dir(r['id'])/'attempts/a1/result.json',{'run_id':r['id'],'attempt_id':'a1','status':'FAILED','error':'native engine failed','candidates':[]})
  partial={'candidates':[{'id':'c1','structure':'accepted.pdb','sequence':'AAA','valid_output':True}], 'stage_complete':False}
  with patch.object(self.s,'verify_snapshot',return_value={'config':{},'environment':self.e}),patch('pwb.workflows.collect_partial_candidates',return_value=partial):Supervisor(self.s).collect(r)
  finished=self.s.get(r['id']);self.assertEqual(finished['status'],'FAILED');self.assertEqual(finished['error'],'native engine failed');self.assertEqual(len(finished['candidates']),1);self.assertEqual(finished['progress']['completed'],0)
  self.assertEqual(finished['candidates'][0]['attempt_id'],'a1');self.assertTrue(self.s.child(r['id'],'c1','gromacs_md',{},self.e['id'])['source_partial'])
if __name__=='__main__':unittest.main()
