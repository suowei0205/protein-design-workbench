"""Loopback-only API. Imported content is never served as application HTML."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote
from pathlib import Path
import json, mimetypes, secrets, shutil
from .core import Supervisor, Conflict, PACKAGE
from .util import inside

PUBLIC_FIELDS={
    '/api/project':{'name'},
    '/api/environment':{'name','python','gmx','bindcraft_root'},
    '/api/inspect':{'path'},
    '/api/import':{'path','workflow','project_id','name','environment_id','config'},
    '/api/create':{'workflow','project_id','name','environment_id','config'},
    '/api/clone':{'run_id','name','config','environment_id'},
    '/api/enqueue':{'run_id'}, '/api/resume':{'run_id'},
    '/api/reorder':{'run_id','position'}, '/api/queue':{'paused'},
    '/api/stop':{'run_id','force'}, '/api/export':{'run_id'},
    '/api/child':{'run_id','candidate_id','workflow','config','environment_id','project_id'},
}

def public_request(path,payload):
    if not isinstance(payload,dict):raise ValueError('请求必须是 JSON 对象')
    if path in PUBLIC_FIELDS and set(payload)-PUBLIC_FIELDS[path]:
        raise ValueError('请求包含未公开字段')
    if path=='/api/queue' and type(payload.get('paused')) is not bool:raise ValueError('paused 必须是布尔值')
    if path=='/api/stop' and 'force' in payload and type(payload['force']) is not bool:raise ValueError('force 必须是布尔值')
    return payload

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,store):
        if address[0]!='127.0.0.1':raise ValueError('首版仅允许本机回环监听')
        self.store=store;self.supervisor=Supervisor(store);self.token=secrets.token_urlsafe(32)
        super().__init__(address,Handler)
    def serve_forever(self,*a,**kw):
        self.supervisor.start()
        try:super().serve_forever(*a,**kw)
        finally:self.supervisor.close()
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send(self,value,status=200,ctype='application/json; charset=utf-8',attachment=None,trusted_report=False):
        payload=json.dumps(value,ensure_ascii=False,allow_nan=False).encode() if isinstance(value,(dict,list)) else value if isinstance(value,bytes) else str(value).encode()
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'"+ (" 'unsafe-inline'" if trusted_report else '')+"; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
        if trusted_report:self.send_header('Content-Security-Policy',"sandbox allow-scripts allow-downloads")
        if attachment:self.send_header('Content-Disposition','attachment; filename="download"')
        self.end_headers();self.wfile.write(payload)
    def send_file(self,path,attachment=True):
        path=Path(path);self.send_response(200)
        self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Length',str(path.stat().st_size));self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store')
        if attachment:
            from urllib.parse import quote
            self.send_header('Content-Disposition',"attachment; filename=download"+path.suffix+"; filename*=UTF-8''"+quote(path.name))
        self.end_headers()
        with path.open('rb') as stream:shutil.copyfileobj(stream,self.wfile,length=1024*1024)
    def valid_host(self):
        host=self.headers.get('Host','')
        valid={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
        return host in valid
    def do_GET(self):
        try:
            if not self.valid_host():return self.send({'error':'host','message':'仅接受本机工作台请求'},403)
            path=unquote(urlparse(self.path).path);store=self.server.store
            if path=='/api/state':return self.send(store.state())
            if path=='/api/templates':return self.send(store.state()['templates'])
            if path.startswith('/api/run/'):return self.send(store.get(path.split('/')[3]))
            if path.startswith('/api/log/'):
                run=store.get(path.split('/')[3]);text='暂无执行日志'
                if run['attempts']:
                    p=store.run_dir(run['id'])/'attempts'/run['attempts'][-1]['id']/'worker.log'
                    if p.is_file():
                        with p.open('rb') as f:f.seek(max(0,p.stat().st_size-100000));text=f.read().decode('utf-8',errors='replace')
                return self.send(text,ctype='text/plain; charset=utf-8')
            if path.startswith('/api/files/'):
                _,_,_,rid,*parts=path.split('/');root=store.run_dir(rid)/'output';p=inside(root,root/'/'.join(parts))
                if not p.is_file():raise ValueError('工件不可用')
                return self.send_file(p)
            if path.startswith('/reports/'):
                _,_,rid,*parts=path.split('/');store.get(rid);root=store.run_dir(rid)/'output/pwb_report';p=inside(root,root/'/'.join(parts))
                if not p.is_file():raise ValueError('报告尚未生成')
                if any(part in ('files','logs','data','downloads') for part in parts):return self.send_file(p)
                # Legacy inline viewers run in a sandbox without the API origin.
                legacy=bool(parts and parts[0]=='legacy')
                return self.send(p.read_bytes(),ctype=mimetypes.guess_type(p.name)[0] or 'application/octet-stream',trusted_report=legacy)
            if path.startswith('/api/feedback/'):
                run=store.get(path.split('/')[3]);p=inside(store.root/'feedback',Path(run['feedback_path']))
                return self.send_file(p)
            if path=='/':
                html=(PACKAGE/'assets/index.html').read_text().replace('__PWB_CSRF__',self.server.token).replace('__CSRF_TOKEN__',self.server.token)
                return self.send(html,ctype='text/html; charset=utf-8')
            if path.startswith('/assets/'):
                p=inside(PACKAGE/'assets',PACKAGE/path.lstrip('/'))
                if not p.is_file():raise ValueError('资源不存在')
                return self.send(p.read_bytes(),ctype=mimetypes.guess_type(p.name)[0] or 'application/octet-stream')
            self.send({'error':'missing','message':'页面不存在'},404)
        except (ValueError,OSError) as e:self.send({'error':'request','message':str(e)},400)
    def do_POST(self):
        try:
            origin=self.headers.get('Origin');allowed={f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'}
            if not self.valid_host() or self.headers.get('X-Workbench-Token')!=self.server.token or (origin is not None and origin not in allowed):return self.send({'error':'csrf','message':'请求来源或令牌无效'},403)
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<2*1024*1024:raise ValueError('请求大小无效')
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('请求必须是 JSON')
            path=urlparse(self.path).path;data=public_request(path,json.loads(self.rfile.read(length)));s=self.server.store
            if path=='/api/project':r=s.project(data['name'])
            elif path=='/api/environment':r=s.environment(**data)
            elif path=='/api/inspect':r=s.inspect(data['path'])
            elif path=='/api/import':r=s.import_file(**data)
            elif path=='/api/create':r=s.new(**data)
            elif path=='/api/clone':r=s.clone(**data)
            elif path=='/api/enqueue':r=s.enqueue(**data)
            elif path=='/api/resume':r=s.enqueue(data['run_id'],resume=True)
            elif path=='/api/reorder':s.reorder(**data);r={'ok':True}
            elif path=='/api/queue':s.pause(data['paused']);r={'ok':True}
            elif path=='/api/stop':self.server.supervisor.stop(**data);r={'ok':True}
            elif path=='/api/export':
                from .feedback import export_run;r={'path':str(export_run(s,data['run_id'])),'url':'/api/feedback/'+data['run_id']}
            elif path=='/api/child':r=s.child(**data)
            else:return self.send({'error':'missing','message':'接口不存在'},404)
            self.send(r)
        except Conflict as e:self.send({'error':'conflict','message':str(e)},409)
        except (ValueError,KeyError,TypeError,OSError) as e:self.send({'error':'request','message':str(e)},400)
        except Exception as e:self.send({'error':'internal','message':str(e)},500)
