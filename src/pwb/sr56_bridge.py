"""Core-neutral SR56 observation and committed-checkpoint stop boundaries.

The worker owns all identities. The supervisor imports atomic per-attempt spool
files and assigns run-global sequence numbers in its own transaction.
"""
from __future__ import annotations

import json
import hashlib
import os
import threading
import uuid
import time
from pathlib import Path


class SafeStop(KeyboardInterrupt):
    """A requested cooperative stop after an authoritative checkpoint receipt."""


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


class Bridge:
    REQUIRED = ('PWB_DATA_ROOT', 'PWB_RUN_ID', 'PWB_ATTEMPT_ID', 'PWB_OUTPUT_DIR',
                'PWB_ATTEMPT_DIR', 'NESPRIN_RUN_ROOT', 'PWB_PACKAGE_ROOT', 'PWB_NOTEBOOK_PATH')

    def __init__(self, environ=None):
        env = os.environ if environ is None else environ
        missing = [name for name in self.REQUIRED if not env.get(name)]
        if missing:
            raise RuntimeError('Worker binding missing: ' + ', '.join(missing))
        self.run_id = env['PWB_RUN_ID']
        self.attempt_id = env['PWB_ATTEMPT_ID']
        self.root = Path(env['NESPRIN_RUN_ROOT']).expanduser().resolve()
        self.output = Path(env['PWB_OUTPUT_DIR']).expanduser().resolve()
        self.attempt = Path(env['PWB_ATTEMPT_DIR']).expanduser().resolve()
        self.package = Path(env['PWB_PACKAGE_ROOT']).expanduser().resolve()
        self.notebook = Path(env['PWB_NOTEBOOK_PATH']).expanduser().resolve()
        self.data_root = Path(env['PWB_DATA_ROOT']).expanduser().resolve()
        for path in (self.output, self.root, self.attempt):
            if not path.is_relative_to(self.data_root):
                raise ValueError('Worker output must be within PWB_DATA_ROOT')
        if not self.notebook.is_file():
            raise FileNotFoundError('Explicit PWB_NOTEBOOK_PATH is not a file')
        self.spool = self.attempt / 'events'
        self.spool.mkdir(parents=True, exist_ok=True)
        self.seq = max((int(p.stem) for p in self.spool.glob('*.json') if p.stem.isdigit()), default=0)
        self.lock = threading.RLock()
        self.queued = {}
        self.models = {}
        self.session = None

    def emit(self, event, stage, key='', **data):
        with self.lock:
            self.seq += 1
            row = {'seq': self.seq, 'local_seq': self.seq, 'run_id': self.run_id,
                   'attempt_id': self.attempt_id, 'ts': self.helper.now(), 'monotonic_ns':time.monotonic_ns(),
                   'event': event, 'stage': str(stage), 'key': str(key),
                   'data': self.helper.clean(data)}
            _atomic(self.spool / f'{self.seq:020d}.json', row)
            return row

    def plan(self, stage, planned_upper):
        self.emit('PLAN', stage, planned_upper=int(planned_upper))

    def queue(self, stage, keys, complete=False):
        keys = [str(k) for k in keys]
        known = self.queued.setdefault(stage, set())
        new = [k for k in keys if k not in known]
        known.update(new)
        if new or complete:
            self.emit('QUEUE', stage, keys=new, actual=len(known), complete=bool(complete))

    def observe(self, event, stage, key='', **data):
        if event == 'START' and stage not in ('session', 'attempt'):
            self.queue(stage, [key])
        if event == 'MODEL':
            self.models[str(data.get('id', key))] = self.helper.clean(data)
        normalized = {'START': 'START', 'REUSED': 'REUSED', 'COMMITTED': 'COMMITTED',
                      'INVALID': 'INVALID', 'MODEL': 'MODEL', 'PAIR': 'PAIR',
                      'END': 'END', 'ERROR': 'ERROR', 'CONFIGURED': 'CONFIGURED',
                      'FINAL_SELECTED': 'FINAL_SELECTED', 'LINEAGE': 'LINEAGE'}.get(event, event)
        if event == 'END' and stage not in ('session', 'attempt'):
            normalized = 'STAGE_END'
        self.emit(normalized, stage, key, original_event=event, **data)
        if event == 'PAIR' and data.get('selected_mi') is not None:
            model_id = f"{data.get('pair_id', key)}:M{data['selected_mi']}"
            self.publish_candidate(stage, model_id, selection='pair')
        elif event == 'FINAL_SELECTED':
            self.publish_candidate(stage, str(data.get('id', key)), **data)

    def publish_candidate(self, stage, model_id, **selection):
        model = self.models.get(model_id)
        if not model or model.get('valid_output') is not True or not model.get('sequence'):
            return
        structure = Path(model.get('cif', ''))
        confidence = Path(model.get('conf', ''))
        if not structure.is_file() or not confidence.is_file() or structure.is_symlink() or confidence.is_symlink():
            return
        structure = structure.resolve()
        confidence = confidence.resolve()
        if not structure.is_relative_to(self.output) or not confidence.is_relative_to(self.output):
            raise ValueError('Selected structure/confidence must be within stable worker output')
        summary = model.get('summary') or {}
        candidate = dict(model)
        candidate.update(id=model_id, structure=structure.relative_to(self.output).as_posix(),
                         confidence=confidence.relative_to(self.output).as_posix(),
                         structure_sha256=hashlib.sha256(structure.read_bytes()).hexdigest(),
                         confidence_sha256=hashlib.sha256(confidence.read_bytes()).hexdigest(),
                         ranking_score=summary.get('ranking_score'), selection=selection,
                         valid_output=True)
        self.emit('CANDIDATE', stage, model_id, candidate=candidate,
                  original_event='FINAL_SELECTED' if selection.get('selection') != 'pair' else 'PAIR')

    def start(self, helper, branch, namespace, background=True):
        self.helper = helper
        bridge = self

        class BoundSession(helper.Session):
            def event(self, event, stage, key='', **data):
                # Session.__init__ calls this method before publishing its first event.
                if not getattr(self, '_pwb_bound', False):
                    previous = helper.read_events(self.monitor / 'events.jsonl')[0]
                    self.seq = max((int(r.get('seq', 0)) for r in previous), default=0)
                    self.run['run_id'] = bridge.run_id
                    self.run['attempt_id'] = bridge.attempt_id
                    self.attempt = bridge.attempt_id
                    self.run['attempts'][-1]['attempt_id'] = bridge.attempt_id
                    helper.write_json(self.monitor / 'session.json', self.run)
                    self._pwb_bound = True
                    if stage == 'session':
                        key = self.attempt
                super().event(event, stage, key, **data)
                bridge.observe(event, stage, key, **data)

        previous = helper.read_json(self.root / 'monitor' / 'session.json')
        if previous and previous.get('run_id') != self.run_id:
            raise ValueError('Existing helper session belongs to a different workbench run')
        self.session = BoundSession(root=self.root, branch=branch,
                                    notebook_path=self.notebook, namespace=namespace,
                                    background=background)
        return self.session

    def boundary(self, stage, key, receipt=None):
        """Only callers immediately AFTER a cache hit/save may enter this method."""
        stop = self.output / 'control' / 'stop.json'
        if not stop.is_file():
            return
        try:
            request = json.loads(stop.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise RuntimeError('Invalid safe-stop control file') from exc
        if request.get('run_id', self.run_id) != self.run_id:
            raise ValueError('Safe-stop request has a different run identity')
        if request.get('attempt_id', self.attempt_id) != self.attempt_id:
            return
        row = self.emit('SAFE_STOP', stage, key, receipt=receipt, requested=request)
        _atomic(self.output / 'control' / 'safe_stopped.json', row)
        raise SafeStop(f'Safe checkpoint boundary: {stage}/{key}')


def bootstrap(helper, branch, namespace):
    bridge = Bridge()
    return bridge, bridge.start(helper, branch, namespace)
