"""Concrete regression checks for the public release trust boundaries."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pwb.core import Store
from pwb.feedback import export_run,verify,merge,_report_page,JSON_LIMITS
from pwb.server import public_request,Handler
from pwb.util import validate_plan

class PublicationSecurity(unittest.TestCase):
    def test_only_public_request_fields(self):
        for endpoint,key in (('/api/import','_run_id'),('/api/create','_run_id'),('/api/child','_child_id')):
            with self.subTest(endpoint=endpoint),self.assertRaises(ValueError):public_request(endpoint,{key:'r_existing'})
        with self.assertRaises(ValueError):public_request('/api/import',[])
        with self.assertRaises(ValueError):public_request('/api/queue',{'paused':'false'})
        self.assertEqual(public_request('/api/create',{'workflow':'generic_script'}),{'workflow':'generic_script'})

    def test_non_numeric_stage_denominators(self):
        for count in ('<img src=x onerror=alert(1)>',-1,True,1.2):
            with self.subTest(count=count),self.assertRaises(ValueError):validate_plan({'actual':count})
        for count in (None,0,1):validate_plan({'actual':count})
        with self.assertRaises(ValueError):Store.progress([{'event':'PLAN','stage':'s','key':'k','ts':'t','data':{'actual':'bad'}}])

    def test_report_escapes_a_corrupt_stage_denominator(self):
        attack='<img src=x onerror=alert(1)>'
        run={'name':'Test','id':'r_test','workflow':'generic_script','status':'FAILED','cutoff':'t','evidence_seq':0,
             'attempts':[],'candidates':[],'progress':{'stages':[{'name':'s','actual':attack,'completed':0,'new':0,'reused':0,'invalid':0}]}}
        with tempfile.TemporaryDirectory() as directory:
            _report_page(run,[],[],Path(directory))
            html=(Path(directory)/'index.html').read_text()
            self.assertNotIn(attack,html);self.assertIn('&lt;img',html)

    def test_csp_inline_legacy_viewer_has_opaque_origin(self):
        class Capture:
            wfile=io.BytesIO()
            def send_response(self,status):pass
            def send_header(self,name,value):headers.setdefault(name,[]).append(value)
            def end_headers(self):pass
        headers={};Handler.send(Capture(),'page',ctype='text/html')
        self.assertNotIn("script-src 'self' 'unsafe-inline'",headers['Content-Security-Policy'][0])
        headers={};Handler.send(Capture(),'page',ctype='text/html',trusted_report=True)
        self.assertIn('sandbox allow-scripts allow-downloads',headers['Content-Security-Policy'])
        self.assertNotIn('allow-same-origin',' '.join(headers['Content-Security-Policy']))

    def test_streamed_member_hash_and_metadata_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'data');env=store.environment('CPU fixture',sys.executable)
            source=Path(directory)/'a.py';source.write_text('print("fixture")')
            run=store.import_file(str(source),environment_id=env['id'])
            (store.run_dir(run['id'])/'output/medium.log').write_bytes(b'x'*(2*1024**2))
            bundle=export_run(store,run['id']);original=zipfile.ZipFile.read
            def read_only_metadata(archive,name,*args,**kwargs):
                self.assertIn(name,JSON_LIMITS,'ordinary members must be streamed')
                return original(archive,name,*args,**kwargs)
            with patch.object(zipfile.ZipFile,'read',read_only_metadata):verify(bundle)
            with patch.dict(JSON_LIMITS,{'MANIFEST.json':1}):
                with self.assertRaisesRegex(ValueError,'元数据过大'):verify(bundle)

    def test_import_rebuilds_active_html_and_keeps_original_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root/'data');env=store.environment('CPU fixture',sys.executable)
            source=root/'a.py';source.write_text('print("fixture")')
            run=store.import_file(str(source),environment_id=env['id']);bundle=export_run(store,run['id'])
            with zipfile.ZipFile(bundle) as archive:members={name:archive.read(name) for name in archive.namelist()}
            members['index.html']=b'<script>EVIL_IMPORTED_HTML()</script>'
            members['assets/offline-viewer.js']=b'EVIL_IMPORTED_JS()'
            manifest=json.loads(members['MANIFEST.json'])
            for member in manifest['files']:
                raw=members[member['path']];member.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
            members['MANIFEST.json']=json.dumps(manifest).encode();malicious=root/'malicious.zip'
            with zipfile.ZipFile(malicious,'w') as archive:
                for name,raw in members.items():archive.writestr(name,raw)
            verify(malicious);merge([malicious],root/'local')
            view=next((root/'local/runs'/run['id']).iterdir())
            self.assertNotIn('EVIL_IMPORTED_HTML',(view/'index.html').read_text())
            self.assertNotIn('EVIL_IMPORTED_JS',(view/'assets/offline-viewer.js').read_text())
            self.assertEqual((view/'original-feedback.zip').read_bytes(),malicious.read_bytes())

if __name__=='__main__':unittest.main()
