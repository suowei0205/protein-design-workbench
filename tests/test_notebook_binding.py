"""Synthetic SR56 observation tests; no target data or private notebook."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from pwb.core import Store,Conflict
from pwb.notebooks import bind_imported_helper,strip_observation

SOURCE='''# PWB_OBSERVE_BEGIN
def _feedback_boot():
    global _feedback
    _feedback = Session.start(branch='SYNTHETIC', namespace='synthetic-only')
_feedback_safe(_feedback_boot)
# PWB_OBSERVE_END
sentinel_compute = 7
'''

def notebook(source=SOURCE):
    cells=[{'id':f'cell-{index}','cell_type':'code','metadata':{},'source':['pass\n'],'outputs':[],'execution_count':None} for index in range(36)]
    cells[3]['source']=source.splitlines(keepends=True)
    return {'nbformat':4,'nbformat_minor':5,'metadata':{},'cells':cells}

class NotebookBinding(unittest.TestCase):
    def test_original_and_already_bound_layout_and_compute_ast(self):
        original=notebook();once=bind_imported_helper(original);twice=bind_imported_helper(once)
        self.assertEqual(once,twice)
        self.assertEqual([(cell['id'],cell['cell_type']) for cell in once['cells']],[(cell['id'],cell['cell_type']) for cell in original['cells']])
        for before,after in zip(original['cells'],once['cells']):
            self.assertEqual(ast.dump(ast.parse(strip_observation(''.join(before['source'])))),ast.dump(ast.parse(strip_observation(''.join(after['source'])))))

    def test_bound_input_helper_used_without_package_private_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'sr56_feedback.py').write_text('synthetic_marker="BOUND INPUT ONLY"\n')
            source=''.join(bind_imported_helper(notebook())['cells'][3]['source'])
            def bootstrap(helper,branch,namespace):
                self.assertEqual(helper.synthetic_marker,'BOUND INPUT ONLY')
                self.assertEqual((branch,namespace),('SYNTHETIC','synthetic-only'))
                return 'BRIDGE','SESSION'
            scope={'_feedback_Path':Path,'_feedback_os':os,'_feedback_sys':sys,'_feedback_importlib':importlib.util}
            with patch.dict(os.environ,{'PWB_PACKAGE_ROOT':str(root/'absent-package'),'PWB_INPUT_DIR':str(root)}),patch('pwb.sr56_bridge.bootstrap',bootstrap),patch.dict(sys.modules):
                exec(source,scope)
            self.assertEqual(scope['_pwb'],'BRIDGE');self.assertEqual(scope['sentinel_compute'],7)

    def test_imported_helper_is_in_frozen_input_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);kit=root/'kit';kit.mkdir()
            source=kit/'synthetic.ipynb';source.write_text(json.dumps(notebook()))
            helper=kit/'sr56_feedback.py';helper.write_text('synthetic=True\n')
            store=Store(root/'data');environment=store.environment('Synthetic interpreter',sys.executable)
            run=store.import_file(str(source),workflow='sr56',environment_id=environment['id'])
            store.enqueue(run['id']);snapshot=store.verify_snapshot(store.get(run['id']))
            self.assertIn('sr56_feedback.py',[member['path'] for member in snapshot['files']])
            helper_copy=store.run_dir(run['id'])/'input/sr56_feedback.py';helper_copy.write_text('changed=True\n')
            with self.assertRaises(Conflict):store.verify_snapshot(store.get(run['id']))

if __name__=='__main__':unittest.main()
