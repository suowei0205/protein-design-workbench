import json,pathlib,sys,tempfile,threading,unittest,urllib.request,urllib.error
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from pwb.core import Store
from pwb.server import Server

class HTTPChecks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
  try:self.server=Server(('127.0.0.1',0),self.s)
  except PermissionError:self.tmp.cleanup();self.skipTest('sandbox prohibits loopback listener; execute dedicated authorized loopback check')
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.url=f'http://127.0.0.1:{self.server.server_port}'
 def tearDown(self):
  if hasattr(self,'thread'):self.server.shutdown();self.thread.join();self.server.server_close();self.tmp.cleanup()
 def request(self,path,data=None,token=None,origin=None,host=None):
  headers={}
  if data is not None:headers['Content-Type']='application/json'
  if token is not None:headers['X-Workbench-Token']=token
  if origin is not None:headers['Origin']=origin
  if host is not None:headers['Host']=host
  req=urllib.request.Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
  return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req)
 def test_no_csrf_mutation_wrongorigin_host(self):
  for kwargs in ({},{'token':'wrong'},{'token':self.server.token,'origin':'http://localhost.attacker.test'},{'token':self.server.token,'host':'attacker.test'}):
   with self.assertRaises(urllib.error.HTTPError) as e:self.request('/api/project',{'name':'bad'},**kwargs)
   self.assertEqual(e.exception.code,403)
  self.assertEqual(len(self.s.projects()),1)
 def test_actual_get_post_token_and_snapshot(self):
  with self.request('/') as r:
   html=r.read().decode();self.assertIn(self.server.token,html);self.assertNotIn('__PWB_CSRF__',html)
  with self.request('/api/project',{'name':'HTTP fixture'},self.server.token,self.url) as r:self.assertEqual(json.loads(r.read())['name'],'HTTP fixture')
  with self.request('/api/state') as r:self.assertEqual(len(json.loads(r.read())['projects']),2)
 def test_no_private_file_serving(self):
  for path in ('/assets/../../pwb/core.py','/api/files/r_fake/../../snapshot.json'):
   with self.assertRaises(urllib.error.HTTPError):self.request(path)
 def test_private_identity_fields_never_reach_import(self):
  for endpoint,key in (('/api/import','_run_id'),('/api/create','_run_id'),('/api/child','_child_id')):
   with self.assertRaises(urllib.error.HTTPError) as error:self.request(endpoint,{key:'r_existing'},self.server.token,self.url)
   self.assertEqual(error.exception.code,400)
  self.assertEqual(self.s.list_runs(),[])
if __name__=='__main__':unittest.main()
