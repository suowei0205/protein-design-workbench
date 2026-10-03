"""Build six bounded workbench notebook copies without rewriting compute code."""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path

OLD_BEGIN, OLD_END = '# SR56_FEEDBACK_BEGIN', '# SR56_FEEDBACK_END'
BEGIN, END = '# PWB_OBSERVE_BEGIN', '# PWB_OBSERVE_END'
PARAMETER_CELLS = (8, 9)
PARAMETERS = frozenset(('n_batches diffusion_batch_size mpnn_seqs_per_backbone rf3_validations '
    'rf3_models_per_sequence refine_top_n refine_rounds refine_beam_k refine_backbone_batch '
    'refine_batch refine_mpnn_seqs refine_rf3_validations random_seed low_memory_mode '
    'num_timesteps step_scale gamma_0 refine_timesteps refine_partial_t_start refine_partial_t_end '
    'max_designs mpnn_model preview_batch preview_in_batch report_contact_distance_angstrom '
    'report_clash_distance_angstrom').split())


def strip_observation(source):
    result, active = [], None
    pairs = {OLD_BEGIN: OLD_END, BEGIN: END}
    for line in source.splitlines(keepends=True):
        marker = line.strip()
        if marker in pairs:
            if active is not None:
                raise ValueError('Nested observation marker')
            active = pairs[marker]
        elif marker in pairs.values():
            if marker != active:
                raise ValueError('Mismatched observation marker')
            active = None
        elif active is None:
            result.append(line)
    if active:
        raise ValueError('Unclosed observation marker')
    return ''.join(result)


def block(code, indent=0):
    pad = ' ' * indent
    return '\n'.join(pad + line if line else '' for line in
                     (BEGIN, *code.strip().splitlines(), END)) + '\n'


def _source(cell):
    return ''.join(cell['source'])


def _put(cell, source):
    cell['source'] = source.splitlines(keepends=True)


def _literal_assignments(source):
    found = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            if name in PARAMETERS:
                found[name] = (node.value, value)
    return found


def apply_parameters(notebook, config):
    """Return a copy; replace only explicitly supported original literal RHSs.

    No expression evaluation, environment lookup, global text replacement, target
    identity edit or derived-value substitution is performed.
    """
    nb = copy.deepcopy(notebook)
    available = {}
    for index in PARAMETER_CELLS:
        for name, pair in _literal_assignments(_source(nb['cells'][index - 1])).items():
            if name in available:
                raise ValueError('Duplicate supported parameter: ' + name)
            available[name] = (index, *pair)
    unknown = sorted(set(config) - set(available))
    if unknown:
        raise ValueError('Unsupported SR56 literal parameters: ' + ', '.join(unknown))
    changes = {}
    for name, value in config.items():
        index, node, old = available[name]
        if isinstance(old, bool):
            valid = isinstance(value, bool)
        elif isinstance(old, int):
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(old, float):
            import math
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        else:
            valid = isinstance(value, type(old))
        if not valid:
            raise ValueError(f'{name} must have literal type {type(old).__name__}')
        source = _source(nb['cells'][index - 1])
        lines = source.splitlines(keepends=True)
        # AST columns are UTF-8 byte offsets, so use byte positions for correctness.
        raw = source.encode('utf-8')
        start = sum(len(s.encode('utf-8')) for s in lines[:node.lineno - 1]) + node.col_offset
        end = sum(len(s.encode('utf-8')) for s in lines[:node.end_lineno - 1]) + node.end_col_offset
        changes.setdefault(index, []).append((start, end, repr(value).encode('utf-8')))
    for index, edits in changes.items():
        raw = _source(nb['cells'][index - 1]).encode('utf-8')
        for start, end, replacement in sorted(edits, reverse=True):
            raw = raw[:start] + replacement + raw[end:]
        _put(nb['cells'][index - 1], raw.decode('utf-8'))
    return nb


def parameter_schema(notebook):
    return {name: {'default': old, 'type': type(old).__name__, 'cell': index}
            for index in PARAMETER_CELLS
            for name, (_, old) in _literal_assignments(_source(notebook['cells'][index - 1])).items()}


def _bootstrap(source):
    tree = ast.parse(source)
    boot = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_feedback_boot')
    text = ast.get_source_segment(source, boot)
    old_tree = ast.parse(text)
    # Branch/namespace literals come from the already bounded original observation.
    calls=[node for node in ast.walk(old_tree) if isinstance(node,ast.Call)]
    start=next((node for node in calls if isinstance(node.func,ast.Attribute) and node.func.attr=='start'),None)
    if start is not None:
        kwargs={key.arg:key.value for key in start.keywords}
        branch,namespace=ast.literal_eval(kwargs['branch']),ast.literal_eval(kwargs['namespace'])
    else:
        bound=next(node for node in calls if isinstance(node.func,ast.Name) and node.func.id=='_pwb_bootstrap')
        branch,namespace=map(ast.literal_eval,bound.args[1:3])
    replacement = '''def _feedback_boot():
    global _feedback, _pwb
    package = _feedback_Path(_feedback_os.environ['PWB_PACKAGE_ROOT']).expanduser().resolve()
    _feedback_sys.path.insert(0, str(package.parent))
    from pwb.sr56_bridge import bootstrap as _pwb_bootstrap
    input_root = _feedback_Path(_feedback_os.environ['PWB_INPUT_DIR']).expanduser().resolve()
    helper = input_root/'sr56_feedback.py'
    if not helper.is_file():
        helper = next((p for p in (package/'sr56'/'notebooks'/'sr56_feedback.py', package/'sr56'/'sr56_feedback.py') if p.is_file()), None)
    if helper is None:
        raise FileNotFoundError('Bundled SR56 helper missing under explicit PWB_PACKAGE_ROOT/sr56')
    spec = _feedback_importlib.spec_from_file_location('sr56_feedback', helper)
    module = _feedback_importlib.module_from_spec(spec)
    _feedback_sys.modules['sr56_feedback'] = module
    exec(compile(helper.read_bytes(), str(helper), 'exec'), module.__dict__)
    _pwb, _feedback = _pwb_bootstrap(module, __BRANCH__, __NAMESPACE__)'''.replace('__BRANCH__', repr(branch)).replace('__NAMESPACE__', repr(namespace))
    if source.count(text) != 1:
        raise ValueError('Ambiguous feedback bootstrap')
    source = source.replace(text, replacement).replace('_feedback_safe(_feedback_boot)', '_feedback_boot()')
    return source, branch, namespace

def bind_imported_helper(original):
    """Only rewrite the existing observation bootstrap in the imported copy."""
    notebook=copy.deepcopy(original)
    if len(notebook.get('cells',[]))!=36:raise ValueError('SR56 source requires 36 original cells')
    source=_source(notebook['cells'][3]);bound,_,_=_bootstrap(source)
    if ast.dump(ast.parse(strip_observation(source)))!=ast.dump(ast.parse(strip_observation(bound))):
        raise ValueError('Bootstrap must remain inside an observation block')
    compile(bound,'SR56 imported observation','exec');_put(notebook['cells'][3],bound)
    return notebook


def _boundaries(source):
    """Append direct boundaries inside observational blocks after their receipt event."""
    lines = source.splitlines(keepends=True)
    result, active, pending = [], False, None
    for line in lines:
        if line.strip() == BEGIN:
            active = True
        if active and "_feedback.event('REUSED', " in line:
            call = next(n for n in ast.walk(ast.parse(line.strip())) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute) and n.func.attr == 'event')
            stage = ast.literal_eval(call.args[1])
            key = next(k.value for k in call.keywords if k.arg == 'key')
            rec = next(k.value for k in call.keywords if k.arg is None)
            pending = f'_pwb.boundary({stage!r}, {ast.unparse(key)}, receipt={ast.unparse(rec)}) if {ast.unparse(rec)} is not None else None'
        elif active and "_feedback.event('COMMITTED', " in line:
            call = next(n for n in ast.walk(ast.parse(line.strip())) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute) and n.func.attr == 'event')
            stage = ast.literal_eval(call.args[1])
            key = next(k.value for k in call.keywords if k.arg == 'key')
            pending = f'_pwb.boundary({stage!r}, {ast.unparse(key)}, receipt={{"committed": True}})'
        if line.strip() == END:
            if pending:
                pad = line[:len(line) - len(line.lstrip())]
                result.append(pad + pending + '\n')
                pending = None
            active = False
        result.append(line)
    return ''.join(result)


def instrument(original):
    nb = copy.deepcopy(original)
    if len(nb['cells']) != 36:
        raise ValueError('SR56 source must preserve the original 36 cells')
    for cell in nb['cells']:
        if cell['cell_type'] == 'code':
            _put(cell, _source(cell).replace(OLD_BEGIN, BEGIN).replace(OLD_END, END))
    source, branch, namespace = _bootstrap(_source(nb['cells'][3]))
    _put(nb['cells'][3], source)
    for index in (16, 22, 24, 31):
        _put(nb['cells'][index - 1], _boundaries(_source(nb['cells'][index - 1])))
    _put(nb['cells'][12], _source(nb['cells'][12]) + block('''_pwb.plan('main_rfd3', n_batches)
_pwb.plan('main_mpnn', n_proc)
_pwb.plan('main_rf3', main_pair_limit)
_pwb.plan('refine_rf3', refine_pair_limit)
_pwb.queue('main_rfd3', [f'rfd3_batch_{b}' for b in range(n_batches)], complete=True)'''))
    _put(nb['cells'][17], _source(nb['cells'][17]) + block('''_pwb.queue('main_mpnn', [f'mpnn_design_{di}' for di in _valid_design_indices], complete=True)
_pwb.emit('ELIGIBLE', 'main_mpnn', actual=len(_valid_design_indices), requested=n_designs,
          invalid=n_designs-len(_valid_design_indices), design_indices=list(_valid_design_indices))'''))
    for index, (old, new) in enumerate(zip(original['cells'], nb['cells']), 1):
        if (old['id'], old['cell_type']) != (new['id'], new['cell_type']):
            raise ValueError('Cell identity changed')
        if old['cell_type'] == 'code':
            compile(_source(new), f'SR56:{index}', 'exec')
            if ast.dump(ast.parse(strip_observation(_source(old)))) != ast.dump(ast.parse(strip_observation(_source(new)))):
                raise ValueError(f'Compute AST changed at cell {index}')
        elif old != new:
            raise ValueError('Non-code cell changed')
    return nb, branch, namespace


def build(source_kit, destination):
    source_kit, destination = Path(source_kit).resolve(), Path(destination).resolve()
    if destination == source_kit or destination.is_relative_to(source_kit):
        raise ValueError('Integration destination must preserve the source kit unchanged')
    notebooks = sorted((source_kit / 'notebooks').glob('*.ipynb'))
    if len(notebooks) != 6:
        raise ValueError('Exactly six original monitored SR56 notebooks required')
    target = destination / 'notebooks'
    target.mkdir(parents=True, exist_ok=True)
    entries = []
    for source in notebooks:
        raw = source.read_bytes()
        original = json.loads(raw)
        nb, branch, namespace = instrument(original)
        path = target / source.name
        path.write_text(json.dumps(nb, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
        entries.append({'branch': branch, 'namespace': namespace, 'path': str(path),
                        'source': str(source), 'source_sha256': hashlib.sha256(raw).hexdigest(),
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'cells': 36,
                        'cell_identity_preserved': True, 'compute_ast_preserved': True,
                        'parameters': parameter_schema(original)})
    manifest = {'schema': 'pwb-sr56-integration-v1', 'entries': entries,
                'parameter_cells': list(PARAMETER_CELLS), 'real_gpu': 'NOT RUN'}
    (destination / 'integration_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('source_kit')
    parser.add_argument('destination')
    args = parser.parse_args()
    print(json.dumps(build(args.source_kit, args.destination), ensure_ascii=False))
