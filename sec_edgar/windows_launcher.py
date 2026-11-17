"""Existing Python from PATH/.python_exe; Windows batch entrypoints."""
import os
import re
import runpy
import sys
from contextlib import contextmanager
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
NAME=re.compile('[A-Za-z_][A-Za-z0-9_]*\\Z')

def load_env(path=None):
    file=Path(path) if path else ROOT/'.env'
    if not file.exists():return
    for lineno,raw in enumerate(file.read_text(encoding='utf-8-sig').splitlines(),1):
        line=raw.strip()
        if not line or line.startswith('#'):continue
        name,sep,val=line.partition('=');name=name.strip()
        if not sep or not NAME.fullmatch(name):raise ValueError(f'.env line {lineno} malformed')
        val=val.strip()
        if val.startswith(('"',"'")) and val.endswith(val[0]):val=val[1:-1]
        else:val=val.split(' #',1)[0].rstrip()
        os.environ.setdefault(name,val)

@contextmanager
def extraction_lock(path):
    file=Path(path);file.parent.mkdir(parents=True,exist_ok=True)
    with file.open('a+b') as f:
        f.seek(0,2)
        if f.tell()==0:f.write(b'0');f.flush()
        f.seek(0)
        if os.name=='nt':
            import msvcrt
            try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:yield False;return
            try:yield True
            finally:f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            try:fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:yield False;return
            try:yield True
            finally:fcntl.flock(f.fileno(),fcntl.LOCK_UN)

def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ('extract_recent_filings', 'extract', 'extract_forms_items', 'dash'):
        print('Usage: python -m sec_edgar.windows_launcher {extract_recent_filings|extract|extract_forms_items|dash}',file=sys.stderr);return 2
    kind,*extras=argv
    os.chdir(ROOT)
    if kind in ('extract_recent_filings', 'extract'):
        from .cli import main
        return main(extras)
    if kind=='extract_forms_items':
        from .forms_cli import main
        return main(extras)
    if extras:print('Dash accepts no positional arguments');return 2
    load_env()
    runpy.run_path(str(ROOT/'app.py'),run_name='__main__');return 0
if __name__=='__main__':sys.exit(main())
