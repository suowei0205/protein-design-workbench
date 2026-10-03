import argparse, json, os
from pathlib import Path
from .core import Store

def main():
    p=argparse.ArgumentParser(description='Local protein design workbench')
    p.add_argument('--data-root',default=os.environ.get('PWB_DATA_ROOT',str(Path.home()/'ProteinWorkbenchData')))
    sub=p.add_subparsers(dest='command',required=True)
    serve=sub.add_parser('serve');serve.add_argument('--port',type=int,default=8970)
    export=sub.add_parser('export');export.add_argument('run_id')
    verify=sub.add_parser('verify');verify.add_argument('zip')
    merge=sub.add_parser('update-local');merge.add_argument('--inbox');merge.add_argument('--output')
    sub.add_parser('state')
    a=p.parse_args();s=Store(a.data_root)
    if a.command=='serve':
        from .server import Server
        print(f'工作台 http://127.0.0.1:{a.port} · 数据目录 {s.root}',flush=True)
        try:Server(('127.0.0.1',a.port),s).serve_forever()
        except KeyboardInterrupt:pass
    elif a.command=='export':
        from .feedback import export_run;print(export_run(s,a.run_id))
    elif a.command=='verify':
        from .feedback import verify;print(json.dumps(verify(a.zip),ensure_ascii=False,indent=2))
    elif a.command=='update-local':
        from .feedback import merge
        inbox=Path(a.inbox) if a.inbox else s.root/'inbox';output=Path(a.output) if a.output else s.root/'local_reports'
        print(merge(sorted(inbox.glob('*.zip')),output))
    else:print(json.dumps(s.state(),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
