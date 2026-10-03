"""An uninstalled scientific interpreter must still find the bound worker."""
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from pwb.core import Store,Supervisor

class PackagingChecks(unittest.TestCase):
    def test_independent_python_environment_executes_a_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);environment=root/'scientific'
            subprocess.run([sys.executable,'-m','venv','--without-pip',str(environment)],check=True)
            interpreter=environment/'bin/python'
            absent=subprocess.run([str(interpreter),'-I','-c','import pwb'],capture_output=True)
            self.assertNotEqual(absent.returncode,0,'fixture environment must not have pwb installed')
            store=Store(root/'data');bound=store.environment('Independent CPU fixture',str(interpreter))
            source=root/'hello.py';source.write_text('from pathlib import Path\nPath("result.txt").write_text("committed")\n')
            run=store.import_file(str(source),environment_id=bound['id']);store.enqueue(run['id'])
            supervisor=Supervisor(store);supervisor.acquire();store.pause(False)
            try:
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    supervisor.tick();current=store.get(run['id'])
                    if current['status'] in ('COMPLETED','FAILED','BLOCKED'):break
                    time.sleep(.05)
                self.assertEqual(current['status'],'COMPLETED',current.get('error'))
                self.assertEqual((store.run_dir(run['id'])/'output/result.txt').read_text(),'committed')
            finally:supervisor.close()

if __name__=='__main__':unittest.main()
