import hashlib, json, os, re, tempfile, stat, unicodedata
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone

def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds')
def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()
def canonical(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':'))
def fingerprint(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def atomic(path,value,raw=False):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    data=value if raw else canonical(value)+'\n'
    fd,temp=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f: f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)
def read(path,default=None):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return default
def inside(root,path):
    root=Path(root).resolve();p=Path(path).resolve()
    if p!=root and root not in p.parents:raise ValueError('路径超出允许目录')
    return p
def idcheck(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value):raise ValueError('身份格式无效')
    return value
def validate_plan(plan):
    """Reject untrusted progress denominators before persistence or rendering."""
    for field in ('actual','planned_upper','n_requested'):
        count=plan.get(field)
        if count is not None and (type(count) is not int or count<0):
            raise ValueError('阶段计数必须是非负整数或 null: '+field)
    if 'keys' in plan and (not isinstance(plan['keys'],list) or any(not isinstance(key,str) for key in plan['keys'])):
        raise ValueError('阶段工作项必须是字符串列表')
def zip_entries(z,max_bytes=2*1024**3,max_members=50000):
    infos=z.infolist();seen=set()
    if len(infos)>max_members or sum(i.file_size for i in infos)>max_bytes:raise ValueError('压缩包超过安全大小或文件数量上限')
    for i in infos:
        n=i.filename;p=PurePosixPath(n);key=unicodedata.normalize('NFC',n).casefold()
        if key in seen:raise ValueError('压缩包重复路径')
        seen.add(key)
        mode=i.external_attr>>16
        if (not n or '\\' in n or ':' in n or p.is_absolute() or '..' in p.parts or any(ord(c)<32 for c in n)
            or any(x.endswith((' ','.')) for x in p.parts) or str(p)!=n.rstrip('/')
            or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR))):raise ValueError('压缩包包含不安全成员: '+n)
    return infos

def extract(z,destination,max_bytes=2*1024**3):
    infos=zip_entries(z,max_bytes=max_bytes);root=Path(destination);root.mkdir(parents=True,exist_ok=True)
    for info in infos:
        p=inside(root,root/info.filename)
        if info.is_dir():p.mkdir(parents=True,exist_ok=True);continue
        p.parent.mkdir(parents=True,exist_ok=True)
        with z.open(info) as src,p.open('wb') as dst:
            import shutil;shutil.copyfileobj(src,dst)
