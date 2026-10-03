"""CPU-only regression checks for process ownership and committed stop evidence."""
import io
import signal
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pwb.core import Conflict, Store, Supervisor
from pwb import worker
from pwb.util import atomic, read


class LifecycleReview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')

    def tearDown(self):
        self.temp.cleanup()

    def active_run(self, members=()):
        run = self.store.new()
        old = {'pid': 111, 'pgid': 111, 'boot_id': 'sameboot', 'start_ticks': 'old', 'state': 'S'}
        run.update(status='STOPPING', force_stop=True, force_stop_at=time.time() - 10)
        run['attempts'] = [{'id': 'a_test', 'status': 'RUNNING', 'started_at': 't',
                            'process': old, 'group_members': list(members)}]
        self.store.save(run)
        atomic(self.store.run_dir(run['id']) / 'attempts/a_test/result.json',
               {'run_id': run['id'], 'attempt_id': 'a_test', 'status': 'STOPPED',
                'artifacts': [], 'candidates': []})
        return run

    def test_dead_leader_cannot_adopt_reused_foreign_group(self):
        foreign = {'pid': 222, 'pgid': 111, 'boot_id': 'sameboot', 'start_ticks': 'foreign', 'state': 'S'}
        run = self.active_run()
        sent = []
        with patch('pwb.core.identity_matches', side_effect=lambda p: p == foreign), \
             patch('pwb.core.read_boot_id', return_value='sameboot'), \
             patch('pwb.core.process_group_members', return_value=[foreign]), \
             patch('pwb.core.os.kill', side_effect=lambda pid, sig: sent.append((pid, sig))), \
             patch('pwb.feedback.export_run'):
            Supervisor(self.store).collect(run)
        self.assertEqual([], self.store.get(run['id'])['attempts'][0]['group_members'])
        self.assertEqual([], sent)
        self.assertEqual('STOPPED', self.store.get(run['id'])['status'])

    def test_recorded_owned_child_remains_owned_after_leader_exit(self):
        owned = {'pid': 222, 'pgid': 111, 'boot_id': 'sameboot', 'start_ticks': 'owned', 'state': 'S'}
        foreign = dict(owned, pid=333, start_ticks='foreign')
        run = self.active_run([owned])
        sent = []
        with patch('pwb.core.identity_matches', side_effect=lambda p: p == owned), \
             patch('pwb.core.read_boot_id', return_value='sameboot'), \
             patch('pwb.core.process_group_members', return_value=[foreign]), \
             patch('pwb.core.os.kill', side_effect=lambda pid, sig: sent.append((pid, sig))):
            Supervisor(self.store).collect(run)
        self.assertEqual([owned], self.store.get(run['id'])['attempts'][0]['group_members'])
        self.assertEqual([(222, signal.SIGKILL)], sent)
        self.assertTrue(self.store.setting('paused'))

    def test_live_verified_leader_records_current_children(self):
        run = self.active_run()
        run['force_stop'] = False
        old = run['attempts'][0]['process']
        child = dict(old, pid=222, start_ticks='child')
        with patch('pwb.core.identity_matches', side_effect=lambda p: p == old), \
             patch('pwb.core.process_group_members', return_value=[old, child]):
            Supervisor(self.store).collect(run)
        self.assertEqual([old, child], self.store.get(run['id'])['attempts'][0]['group_members'])

    def test_slow_loop_keeps_supervisor_lock_until_thread_exits(self):
        supervisor = Supervisor(self.store)
        entered, release = threading.Event(), threading.Event()
        def slow_tick():
            entered.set()
            release.wait(5)
        supervisor.tick = slow_tick
        supervisor.start()
        competitor = Supervisor(self.store)
        original_join = supervisor.thread.join
        try:
            self.assertTrue(entered.wait(2))
            with patch.object(supervisor.thread, 'join', side_effect=lambda timeout=None: original_join(.02)):
                supervisor.close()
            self.assertTrue(supervisor.thread.is_alive())
            with self.assertRaises(Conflict):
                competitor.acquire()
            self.assertIsNone(competitor.guard)
            release.set()
            original_join(2)
            self.assertFalse(supervisor.thread.is_alive())
            competitor.acquire()
        finally:
            release.set()
            original_join(2)
            supervisor.close()
            competitor.close()

    def test_launch_after_close_does_not_validate_or_create_attempt(self):
        supervisor = Supervisor(self.store)
        supervisor.close()
        run = self.store.new()
        validated = []
        def reject_snapshot(r):
            validated.append(r)
            raise Conflict('not frozen')
        with patch.object(self.store, 'verify_snapshot', side_effect=reject_snapshot):
            try:
                supervisor.launch(run)
            except Conflict:
                pass
        self.assertEqual([], validated)
        self.assertEqual([], self.store.get(run['id'])['attempts'])
        self.assertEqual([], list((self.store.run_dir(run['id']) / 'attempts').iterdir()))

    def test_halt_during_snapshot_validation_prevents_attempt_creation(self):
        supervisor = Supervisor(self.store)
        run = self.store.new()
        def validated_snapshot(r):
            supervisor.halt.set()
            return {}
        with patch.object(self.store, 'verify_snapshot', side_effect=validated_snapshot):
            supervisor.launch(run)
        self.assertEqual([], self.store.get(run['id'])['attempts'])
        self.assertEqual([], list((self.store.run_dir(run['id']) / 'attempts').iterdir()))

    def test_halt_during_request_write_prevents_worker_launch(self):
        supervisor = Supervisor(self.store)
        run = self.store.new()
        run['status'] = 'QUEUED'
        self.store.save(run)
        snapshot = {'workflow': 'generic_script', 'source_path': None, 'config': {},
                    'environment': {'python': sys.executable}, 'environment_sha256': 'bound'}
        spawned = []
        def write_then_halt(path, value, **kwargs):
            atomic(path, value, **kwargs)
            if Path(path).name == 'request.json':
                supervisor.halt.set()
        def process(*args, **kwargs):
            spawned.append(args)
            return SimpleNamespace(pid=111)
        with patch.object(self.store, 'verify_snapshot', return_value=snapshot), \
             patch('pwb.core.atomic', side_effect=write_then_halt), \
             patch('pwb.core.subprocess.Popen', side_effect=process), \
             patch('pwb.core.process_identity', return_value={'pid': 111, 'pgid': 111}):
            supervisor.launch(run)
        self.assertEqual([], spawned)
        self.assertEqual([], self.store.get(run['id'])['attempts'])
        self.assertEqual('QUEUED', self.store.get(run['id'])['status'])

    def worker_result(self, stop=None, marker=None, exception=None, result=None):
        attempt, output = self.root / 'attempt', self.root / 'output'
        attempt.mkdir(exist_ok=True)
        output.mkdir(exist_ok=True)
        request = {'run_id': 'r_review', 'attempt_id': 'a_review', 'attempt_dir': str(attempt),
                   'output_dir': str(output), 'snapshot': {'workflow': 'generic_notebook'}}
        atomic(self.root / 'request.json', request)
        if stop is not None:
            atomic(output / 'control/stop.json', stop)
        if marker is not None:
            atomic(output / 'control/safe_stopped.json', marker)
        with patch.object(sys, 'argv', ['worker', str(self.root / 'request.json')]), \
             patch('pwb.worker.execute', side_effect=exception, return_value=result or {'status': 'COMPLETED'}), \
             redirect_stderr(io.StringIO()):
            code = worker.main()
        return code, read(attempt / 'result.json')

    def test_same_named_error_without_committed_request_is_failed(self):
        error = type('SafeStop', (ValueError,), {})('ordinary failure without checkpoint')
        code, result = self.worker_result(exception=error)
        self.assertEqual(1, code)
        self.assertEqual('FAILED', result['status'])
        self.assertIn('ordinary failure without checkpoint', result['error'])

    def test_wrong_run_marker_cannot_prove_safe_stop(self):
        error = type('SafeStop', (ValueError,), {})('wrong run boundary')
        code, result = self.worker_result(
            stop={'run_id': 'r_review', 'attempt_id': 'a_review'},
            marker={'run_id': 'r_other', 'attempt_id': 'a_review', 'stage': 'design', 'boundary': 'completed stage'},
            exception=error)
        self.assertEqual(1, code)
        self.assertEqual('FAILED', result['status'])

    def test_matching_marker_without_stop_request_is_failed(self):
        error = type('SafeStop', (ValueError,), {})('no stop request')
        code, result = self.worker_result(
            marker={'run_id': 'r_review', 'attempt_id': 'a_review', 'stage': 'design', 'boundary': 'completed stage'},
            exception=error)
        self.assertEqual(1, code)
        self.assertEqual('FAILED', result['status'])

    def test_matching_committed_stop_survives_nbclient_exception_transport(self):
        error = RuntimeError('notebook cell exception transport')
        error.ename = 'SafeStop'
        code, result = self.worker_result(
            stop={'run_id': 'r_review', 'attempt_id': 'a_review'},
            marker={'run_id': 'r_review', 'attempt_id': 'a_review', 'stage': 'design', 'event': 'SAFE_STOP'},
            exception=error)
        self.assertEqual(0, code)
        self.assertEqual('STOPPED', result['status'])
        self.assertIsNone(result['error'])

    def test_unrelated_failure_after_stop_marker_is_still_failed(self):
        code, result = self.worker_result(
            stop={'run_id': 'r_review', 'attempt_id': 'a_review'},
            marker={'run_id': 'r_review', 'attempt_id': 'a_review', 'stage': 'design', 'event': 'SAFE_STOP'},
            exception=ValueError('output write failed after checkpoint'))
        self.assertEqual(1, code)
        self.assertEqual('FAILED', result['status'])

    def test_returned_stopped_without_matching_boundary_is_failed(self):
        code, result = self.worker_result(result={'status': 'STOPPED'})
        self.assertEqual(1, code)
        self.assertEqual('FAILED', result['status'])

    def test_returned_stopped_with_matching_boundary_is_preserved(self):
        code, result = self.worker_result(
            stop={'run_id': 'r_review', 'attempt_id': 'a_review'},
            marker={'run_id': 'r_review', 'attempt_id': 'a_review', 'stage': 'production', 'boundary': 'GROMACS checkpoint'},
            result={'status': 'STOPPED'})
        self.assertEqual(0, code)
        self.assertEqual('STOPPED', result['status'])

    def test_wrong_attempt_stop_request_cannot_prove_safe_stop(self):
        error = type('SafeStop', (ValueError,), {})('stale request')
        code, result = self.worker_result(
            stop={'run_id': 'r_review', 'attempt_id': 'a_old'},
            marker={'run_id': 'r_review', 'attempt_id': 'a_review', 'stage': 'design', 'event': 'SAFE_STOP'},
            exception=error)
        self.assertEqual(1, code)
        self.assertEqual('FAILED', result['status'])

    def test_workflow_boundary_marker_records_run_and_attempt(self):
        from pwb.workflows import StageRunner, WorkflowStop
        output = self.root / 'workflow-output'
        atomic(output / 'control/stop.json', {'run_id': 'r_review', 'attempt_id': 'a_review'})
        runner = StageRunner('gromacs_md', {}, {}, output, lambda *args, **kwargs: None)
        with patch.dict('os.environ', {'PWB_RUN_ID': 'r_review', 'PWB_ATTEMPT_ID': 'a_review'}):
            with self.assertRaises(WorkflowStop):
                runner.boundary('committed_stage')
        marker = read(output / 'control/safe_stopped.json')
        self.assertEqual('r_review', marker.get('run_id'))
        self.assertEqual('a_review', marker.get('attempt_id'))


if __name__ == '__main__':
    unittest.main()
