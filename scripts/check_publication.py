"""Check the exact staged/tracked source boundary without printing matched secrets."""
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
SECRET_PATTERNS=(r'gh[pousr]_[A-Za-z0-9]{30,}',r'github_pat_[A-Za-z0-9_]{40,}',
                 r'sk-[A-Za-z0-9]{30,}',r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
PRIVATE_PATHS=(r'/(?:Users/[A-Za-z0-9_-]+|media/user|home/user)/',)
FORBIDDEN={'.zip','.log','.sqlite','.sqlite3','.db','.pem','.key','.pdb','.cif','.xtc','.trr','.tpr','.cpt','.fasta'}

def source_files():
    if (ROOT/'.git').is_dir():
        listed=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
        return [ROOT/name for name in listed if name]
    ignored={'__pycache__','build','dist','.venv','.ruff_cache'}
    return [path for path in ROOT.rglob('*') if path.is_file() and not any(part in ignored or part.endswith('.egg-info') for part in path.parts)]

def violations(path):
    relative=path.relative_to(ROOT).as_posix()
    if path.is_symlink():return ['symlink']
    if path.suffix in FORBIDDEN:return ['runtime/research artifact']
    if path.suffix=='.ipynb' and relative!='src/pwb/templates/generic_notebook.ipynb':return ['private notebook']
    if path.stat().st_size>8*1024**2:return ['unexpected large file']
    if path.suffix in {'.ttf','.png','.jpg'}:return []
    text=path.read_text(encoding='utf-8');issues=[]
    if any(re.search(pattern,text) for pattern in SECRET_PATTERNS):issues.append('credential pattern')
    if any(re.search(pattern,text) for pattern in PRIVATE_PATHS):issues.append('machine-specific private path')
    return issues

def main():
    checked=source_files();problems=[(path.relative_to(ROOT).as_posix(),violations(path)) for path in checked]
    problems=[entry for entry in problems if entry[1]]
    for relative,issues in problems:print(relative+': '+', '.join(issues),file=sys.stderr)
    if problems:return 1
    print(f'PASS: {len(checked)} public files checked; no listed credential, private-path or research-artifact pattern')
    return 0

if __name__=='__main__':sys.exit(main())
