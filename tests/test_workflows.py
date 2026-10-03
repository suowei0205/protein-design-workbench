"""Command/receipt tests against a fake local tool. No MD or GPU execution."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pwb.workflows import (StageRunner, TEMPLATES, bindcraft_command,
                           build_command, execute, pull_parameters, validate)

FAKE_GMX = r'''
import json, os, signal, sys, time
from pathlib import Path
args=sys.argv[1:]
stdin=sys.stdin.read()
trace=Path(os.environ["PWB_FAKE_TRACE"])
with trace.open("a") as file:
    file.write(json.dumps({"args":args,"cwd":str(Path.cwd()),"stdin":stdin})+"\n")
def get(flag, default=None):
    return args[args.index(flag)+1] if flag in args else default
def write(name, content="fake output\n"):
    if name:
        path=Path(name);path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('@ yaxis label "Native units"\n0 1\n1 3\n' if path.suffix==".xvg" else content)
if os.getenv("PWB_FAKE_FAIL")==args[0]:
    sys.exit(7)
if args[0]=="pdb2gmx":
    write(get("-o"),"coordinate\n")
    write(get("-p"),'#include "posre.itp"\n[ system ]\nprotein\n')
    write(get("-i"),"[ position_restraints ]\n")
elif args[0]=="grompp":
    write(get("-o"),Path(get("-f")).read_text())
    write(get("-po"),Path(get("-f")).read_text())
    write(get("-pp"),Path(get("-p")).read_text())
elif args[0]=="mdrun":
    for flag in ("-cpo","-e","-x","-g","-px","-pf"):
        write(get(flag))
    write(get('-g'), '     Step          Time\n      6          0.012\n')
    if os.getenv("PWB_FAKE_SLOW")==Path.cwd().name and "-cpi" not in args:
        def stop(sig, frame):
            if not os.getenv('PWB_FAKE_STALE_CPT'):
                write(get("-cpo"),"interrupted checkpoint\n")
            sys.exit(int(os.getenv('PWB_FAKE_STOP_EXIT','0')))
        signal.signal(signal.SIGINT, stop)
        marker=Path(os.environ["PWB_FAKE_STARTED"]);marker.write_text("ready")
        while True: time.sleep(.02)
    write(get("-c"),"completed coordinate\n")
elif args[0]=="make_ndx":
    write(get('-o'), '[ System ]\n1 2 3 4\n[ Backbone ]\n1 3\n')
elif args[0] in ("editconf","solvate","genion","energy","rms","trjconv"):
    write(get("-o"), "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 90.00           C\n" if args[0]=="trjconv" else "fake output\n")
    if args[0] in ("solvate","genion"):
        with Path(get("-p")).open("a") as file: file.write("; updated\n")
else: sys.exit(3)
'''
FAKE_BINDCRAFT = '''import argparse, json, os, time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--settings');p.add_argument('--filters');p.add_argument('--advanced');a=p.parse_args()
s=json.loads(Path(a.settings).read_text());r=Path(s['design_path']);(r/'Accepted').mkdir(parents=True)
(r/'Accepted'/'candidate_model1.pdb').write_text('ATOM      1  CA  ALA B   1       0.000   0.000   0.000  1.00 90.00           C\\n')
(r/'final_design_stats.csv').write_text('Rank,Design,Sequence,Average_i_pTM\\n1,candidate,A,0.8\\n')
(r/'trajectory_stats.csv').write_text('Design,Sequence\\none,A\\n')
print('Starting trajectory: one', flush=True)
print('Starting trajectory: two', flush=True)
time.sleep(float(os.getenv('PWB_FAKE_BINDCRAFT_DELAY','0')))
print('Target number 1 of designs reached! Reranking...', flush=True)
'''


def mdp(stage):
    value = {"integrator": "steep" if stage in {"ions", "minimize"} else "md", "nsteps": 12,
             "cutoff-scheme": "Verlet", "coulombtype": "PME", "rcoulomb": 1.0,
             "vdwtype": "Cut-off", "rvdw": 1.0, "pbc": "xyz"}
    if stage in {"ions", "minimize"}:
        value.update(emtol=1000.0, emstep=.01)
    else:
        value.update({"dt": .002, "constraints": "h-bonds", "tcoupl": "v-rescale", "tc-grps": "System",
                      "tau-t": .1, "ref-t": 300.0, "pcoupl": "no" if stage == "nvt" else "C-rescale",
                      "gen-vel": "yes" if stage == "nvt" else "no", "continuation": "no" if stage == "nvt" else "yes",
                      "nstenergy": 2, "nstlog": 2, "nstxout-compressed": 2})
        if stage == "nvt":
            value.update({"gen-temp": 300.0, "gen-seed": 42})
        else:
            value.update({"pcoupltype": "isotropic", "tau-p": 5.0, "ref-p": 1.0, "compressibility": 4.5e-5})
    return value


class WorkflowsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gmx = self.root / "fake_gmx"
        self.gmx.write_text(f"#!{sys.executable}\n" + FAKE_GMX)
        self.gmx.chmod(0o755)
        self.pdb = self.root / "protein.pdb"
        self.pdb.write_text("ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 90.00           C\n")
        self.trace = self.root / "trace.jsonl"
        self.env = {"python": sys.executable, "gmx": str(self.gmx)}
        self.config = {"input_mode": "protein_pdb", "system_kind": "protein_aqueous", "special_features": [],
                       "pdb": str(self.pdb), "forcefield": "testff", "water_model": "tip3p", "solvent_coordinates": "spc216.gro",
                       "chainsep": "id_or_ter", "merge": "no", "protonation": {"flags": ["-his", "-ter"], "stdin": "0\n1\n0\n", "decisions": "explicit test menu"},
                       "box": {"type": "dodecahedron", "distance_nm": 1.2},
                       "ions": {"positive": "NA", "negative": "CL", "positive_charge": 1, "negative_charge": -1, "minimum_distance_nm": .6, "solvent_group": "SOL", "concentration_molar": .15, "neutralize": True, "seed": 42},
                       "mdp": {s: mdp(s) for s in ("ions", "minimize", "nvt", "npt", "production")},
                       "mdrun": {"ntomp": 1, "ntmpi": 1, "checkpoint_minutes": .1, "device": "cpu"},
                       "analysis": {"energy_terms": ["Potential", "Temperature"], "fit_group": "Backbone", "rms_group": "Backbone", "frame_group": "System", "frame_time_ps": .02}}
        self.events = []
        self.emit = lambda event, **kw: self.events.append({"event": event, **kw})
        self.patch = patch.dict(os.environ, {"PWB_FAKE_TRACE": str(self.trace)}, clear=False)
        self.patch.start()

    def test_pull_preserves_default_groups_through_analysis(self):
        cfg = copy.deepcopy(self.config);cfg['pull'] = self.pull()
        result = execute('gromacs_pull_velocity', cfg, self.env, self.root/'out', self.emit)
        self.assertEqual(result['status'], 'COMPLETED')
        index = self.root/'out/workflow/production/index.ndx'
        self.assertIn('[ System ]', index.read_text())
        self.assertIn('[ Backbone ]', index.read_text())
        self.assertIn('[ Reference ]', index.read_text())
        commands = self.commands()
        self.assertEqual(len([x for x in commands if x['args'][0]=='make_ndx']), 1)
        for item in commands:
            if item['args'][0] in ('rms','trjconv'):
                self.assertIn('-n', item['args'])
                self.assertEqual(Path(item['args'][item['args'].index('-n')+1]), index.resolve())

    def test_md_observed_step_uses_backend_event_and_not_completed_count(self):
        execute('gromacs_md', self.config, self.env, self.root/'out', self.emit)
        events = [x for x in self.events if x['event']=='MD_PROGRESS']
        self.assertTrue(events)
        self.assertEqual(events[-1]['data']['observed_step'], 6)
        self.assertFalse(events[-1]['data']['stage_complete'])

    def test_bindcraft_stop_commits_candidates_then_returns_stopped(self):
        root = self.root/'BindCraft';root.mkdir();(root/'bindcraft.py').write_text(FAKE_BINDCRAFT)
        cfg = {}
        settings = {'binder_name':'Test','starting_pdb':str(self.pdb),'chains':'A','target_hotspot_residues':None,'lengths':[1,2],'number_of_final_designs':1}
        for key, value in [('settings',settings),('filters',{'test':True}),('advanced',{'max_trajectories':False})]:
            path=self.root/f'{key}.json';path.write_text(json.dumps(value));cfg[key]=str(path)
        output=self.root/'out';(output/'control').mkdir(parents=True);(output/'control/stop.json').write_text('{}')
        result=execute('bindcraft',cfg,{'python':sys.executable,'bindcraft_root':str(root)},output,self.emit)
        self.assertEqual(result['status'],'STOPPED')
        self.assertEqual(len(result['candidates']),1)
        self.assertTrue((output/'workflow/bindcraft/receipt.json').is_file())
        self.assertTrue((output/'control/safe_stopped.json').is_file())

    def test_bindcraft_target_chain_match_is_not_binder_validation(self):
        from pwb.workflows import _collect_bindcraft
        runner = StageRunner('bindcraft',{},self.env,self.root/'out',self.emit)
        folder = runner.stage_root/'bindcraft';(folder/'designs/Accepted').mkdir(parents=True)
        (folder/'settings.json').write_text('{"number_of_final_designs":1}')
        (folder/'advanced.json').write_text('{}');(folder/'execution.log').write_text('')
        (folder/'designs/final_design_stats.csv').write_text('Design,Sequence,Average_i_pTM\ncandidate,A,0.9\n')
        (folder/'designs/Accepted/candidate_model1.pdb').write_text(self.pdb.read_text())
        self.assertEqual(_collect_bindcraft(folder,runner),[])
        self.assertTrue(any(x['event']=='CANDIDATE_INVALID' for x in self.events))

    def test_bindcraft_rejection_stop_reason_and_budget_semantics(self):
        from pwb.workflows import bindcraft_progress
        folder = self.root/'native';folder.mkdir();(folder/'designs/Trajectory/Relaxed').mkdir(parents=True)
        (folder/'settings.json').write_text('{"number_of_final_designs":10}')
        (folder/'advanced.json').write_text('{"max_trajectories":2}')
        (folder/'execution.log').write_text('Starting trajectory: one\nStarting trajectory: two\nStarting trajectory: three\nThe ratio of successful designs is lower than defined acceptance rate! Consider changing your design settings!\nScript execution stopping...\n')
        (folder/'designs/Trajectory/Relaxed/one.pdb').write_text(self.pdb.read_text())
        native=bindcraft_progress(folder)
        self.assertEqual(native['attempted_trajectories'],3)
        self.assertEqual(native['relaxed_trajectory_files'],1)
        self.assertEqual(native['stop_reason'],'acceptance_rate_below_threshold')
        self.assertIn('relaxed',native['trajectory_budget_basis'].lower())

    def test_live_md_progress_arrives_before_checkpoint_commit(self):
        output=self.root/'out';marker=self.root/'started'
        def emit(event,**kw):
            self.emit(event,**kw)
            if event=='MD_PROGRESS' and kw['data'].get('process_running'):
                (output/'control').mkdir(exist_ok=True);(output/'control/stop.json').write_text('{}')
        with patch('pwb.workflows.PROGRESS_POLL_SECONDS',.1), patch.dict(os.environ,{'PWB_FAKE_SLOW':'nvt','PWB_FAKE_STARTED':str(marker)}):
            result=execute('gromacs_md',self.config,self.env,output,emit)
        self.assertEqual(result['status'],'STOPPED')
        observed=[i for i,x in enumerate(self.events) if x['event']=='MD_PROGRESS' and x['data'].get('process_running')]
        committed=[i for i,x in enumerate(self.events) if x['event']=='CHECKPOINT_COMMITTED']
        self.assertTrue(observed);self.assertTrue(committed)
        self.assertLess(observed[-1],committed[0])
        self.assertFalse(any(x['event']=='STAGE_COMMITTED' and x['stage']=='nvt' for x in self.events))

    def test_nonzero_exit_during_sigint_never_commits_safe_checkpoint(self):
        output=self.root/'out';marker=self.root/'started'
        def request():
            deadline=time.monotonic()+10
            while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
            (output/'control').mkdir(exist_ok=True);(output/'control/stop.json').write_text('{}')
        thread=threading.Thread(target=request)
        with patch.dict(os.environ,{'PWB_FAKE_SLOW':'nvt','PWB_FAKE_STARTED':str(marker),'PWB_FAKE_STOP_EXIT':'7'}):
            thread.start()
            with self.assertRaisesRegex(RuntimeError,'返回码 7'):
                execute('gromacs_md',self.config,self.env,output,self.emit)
        thread.join(10)
        self.assertFalse((output/'workflow/nvt/partial.json').exists())
        self.assertFalse((output/'control/safe_stopped.json').exists())

    def test_unchanged_old_checkpoint_after_sigint_is_not_safe_stop(self):
        output=self.root/'out';marker=self.root/'started'
        def emit(event,**kw):
            self.emit(event,**kw)
            if event=='MD_PROGRESS' and kw['data'].get('process_running'):
                (output/'control').mkdir(exist_ok=True);(output/'control/stop.json').write_text('{}')
        with patch('pwb.workflows.PROGRESS_POLL_SECONDS',.1),patch.dict(os.environ,{'PWB_FAKE_SLOW':'nvt','PWB_FAKE_STARTED':str(marker),'PWB_FAKE_STALE_CPT':'1'}):
            with self.assertRaisesRegex(RuntimeError,'新提交'):
                execute('gromacs_md',self.config,self.env,output,emit)
        self.assertFalse((output/'workflow/nvt/partial.json').exists())

    def test_native_csv_complete_rows_survive_quoted_partial_tail(self):
        from pwb.workflows import csv_complete_rows
        path=self.root/'native.csv';path.write_text('Design,Sequence,Score\nfirst,A,0.5\nsecond,"broken\nrecord,')
        self.assertEqual(csv_complete_rows(path),[{'Design':'first','Sequence':'A','Score':'0.5'}])

    def test_live_bindcraft_progress_separates_attempts_goal_and_final_validation(self):
        root=self.root/'BindCraft';root.mkdir();(root/'bindcraft.py').write_text(FAKE_BINDCRAFT)
        cfg={}
        settings={'binder_name':'Test','starting_pdb':str(self.pdb),'chains':'A','target_hotspot_residues':None,'lengths':[1,2],'number_of_final_designs':1}
        for key,value in [('settings',settings),('filters',{'test':True}),('advanced',{'max_trajectories':False})]:
            path=self.root/f'{key}.json';path.write_text(json.dumps(value));cfg[key]=str(path)
        with patch('pwb.workflows.PROGRESS_POLL_SECONDS',.1),patch.dict(os.environ,{'PWB_FAKE_BINDCRAFT_DELAY':'.4'}):
            result=execute('bindcraft',cfg,{'python':sys.executable,'bindcraft_root':str(root)},self.root/'out',self.emit)
        live=[x for x in self.events if x['event']=='BINDCRAFT_PROGRESS' and x['data'].get('process_running') and x['data'].get('attempted_trajectories')==2]
        self.assertTrue(live)
        self.assertEqual(live[-1]['data']['accepted_goal'],1)
        self.assertFalse(live[-1]['data']['committed_candidates'])
        self.assertEqual(result['candidates'][0]['sequence'],'A')
        self.assertEqual(result['candidates'][0]['metrics']['sequence_matching_chains'],['B'])

    def test_prepared_custom_index_passes_to_grompp_and_analysis(self):
        source=self.root/'prepared';source.mkdir()
        (source/'system.gro').write_text('coordinate');(source/'state.cpt').write_text('checkpoint')
        (source/'topol.top').write_text('[ system ]\nprotein\n')
        (source/'custom.ndx').write_text('[ ExplicitSelection ]\n1 3\n')
        cfg=copy.deepcopy(self.config)
        cfg.update(input_mode='prepared',prepared_root=str(source),coordinate='system.gro',topology='topol.top',checkpoint='state.cpt',index='custom.ndx',start_stage='production')
        cfg['analysis']['rms_group']='ExplicitSelection'
        result=execute('gromacs_md',cfg,self.env,self.root/'out',self.emit)
        self.assertEqual(result['status'],'COMPLETED')
        self.assertIn('[ ExplicitSelection ]', (self.root/'out/workflow/production/index.ndx').read_text())
        command=next(x for x in self.commands() if x['args'][0]=='grompp')
        self.assertIn('-n',command['args'])
        command=next(x for x in self.commands() if x['args'][0]=='rms')
        self.assertIn('ExplicitSelection',command['stdin'])
        self.assertIn('-n',command['args'])

    def test_posres_requires_explicit_fixed_reference_across_stages(self):
        cfg=copy.deepcopy(self.config)
        for stage in ('nvt','npt','production'):cfg['mdp'][stage]['define']='-DPOSRES'
        self.assertTrue(any('restraint_reference' in value for value in validate('gromacs_md',cfg,self.env)))
        reference=self.root/'reference.gro';reference.write_text('explicit full-system restraint coordinates')
        cfg['restraint_reference']=str(reference)
        self.assertEqual(validate('gromacs_md',cfg,self.env),[])
        execute('gromacs_md',cfg,self.env,self.root/'out',self.emit)
        commands=[x for x in self.commands() if x['args'][0]=='grompp' and Path(x['cwd']).name in ('nvt','npt','production')]
        self.assertEqual(len(commands),3)
        for item in commands:
            self.assertEqual(item['args'][item['args'].index('-r')+1],'reference.gro')
            self.assertEqual((Path(item['cwd'])/'reference.gro').read_bytes(),reference.read_bytes())

    def test_index_out_of_system_and_reserved_name_fail_without_production(self):
        for group in ({'name':'Moving','atoms':[3,99],'pbcatom':3},{'name':'System','atoms':[3,4],'pbcatom':3}):
            cfg=copy.deepcopy(self.config);cfg['pull']=self.pull();cfg['pull']['groups'][1]=group
            with self.assertRaisesRegex(ValueError,'索引|拉伸组名'):
                execute('gromacs_pull_velocity',cfg,self.env,self.root/group['name'],self.emit)
        self.assertFalse(any(Path(x['cwd']).name=='production' for x in self.commands()))

    def test_ion_charge_distance_and_log_interval_must_be_explicit(self):
        for key in ('positive_charge','negative_charge','minimum_distance_nm'):
            cfg=copy.deepcopy(self.config);cfg['ions'].pop(key,None)
            self.assertTrue(validate('gromacs_md',cfg,self.env))
        cfg=copy.deepcopy(self.config);cfg['mdp']['production'].pop('nstlog',None)
        self.assertTrue(any('nstlog' in x for x in validate('gromacs_md',cfg,self.env)))

    def test_malformed_form_values_report_errors_without_typeerror(self):
        cfg=copy.deepcopy(self.config);cfg['protonation']['flags']=[{}]
        self.assertTrue(validate('gromacs_md',cfg,self.env))
        cfg=copy.deepcopy(self.config);cfg.update(input_mode='prepared',prepared_root=None,coordinate=None,topology=None,start_stage='production')
        self.assertTrue(validate('gromacs_md',cfg,self.env))
        cfg=copy.deepcopy(self.config);cfg['pull']=self.pull();cfg['pull']['groups'][0]['name']=[];cfg['pull']['dimensions']=[{},'N','Y']
        self.assertTrue(validate('gromacs_pull_velocity',cfg,self.env))
        self.assertTrue(validate('bindcraft',{'settings':None,'filters':None,'advanced':None},{'python':sys.executable,'bindcraft_root':None}))

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def commands(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def pull(self, velocity=True):
        value = {"groups": [{"name": "Reference", "atoms": [1, 2], "pbcatom": 1}, {"name": "Moving", "atoms": [3, 4], "pbcatom": 3}],
                 "group_numbering_basis": "checked final processed full system 1-based indices", "protocol_rationale": "fake command test only",
                 "geometry": "direction", "dimensions": ["N", "N", "Y"], "vector": [0, 0, 1], "nstxout": 2, "nstfout": 2}
        value.update({"rate_nm_per_ps": .001, "spring_kj_mol_nm2": 500.0, "initial_nm": 0., "start_from_initial": True}
                     if velocity else {"force_kj_mol_nm": 60.0})
        return value

    def test_full_preparation_order_and_protonation(self):
        self.assertEqual(validate("gromacs_md", self.config, self.env), [])
        before = hashlib.sha256(self.pdb.read_bytes()).hexdigest()
        result = execute("gromacs_md", self.config, self.env, self.root / "out", self.emit)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["progress"]["completed"], 9)
        commands = self.commands()
        self.assertEqual([x["args"][0] for x in commands], ["pdb2gmx", "editconf", "solvate", "grompp", "genion", "grompp", "mdrun", "grompp", "mdrun", "grompp", "mdrun", "grompp", "mdrun", "energy", "rms", "trjconv"])
        self.assertIn("-his", commands[0]["args"])
        self.assertIn("-ter", commands[0]["args"])
        self.assertEqual(commands[0]["stdin"], "0\n1\n0\n")
        self.assertEqual(commands[4]["stdin"], "SOL\n")
        self.assertEqual(before, hashlib.sha256(self.pdb.read_bytes()).hexdigest())
        self.assertTrue((self.root / "out" / result["candidates"][0]["structure"]).is_file())
        self.assertFalse(any("-maxwarn" in x["args"] for x in commands))
        prodpre = [x for x in commands if Path(x["cwd"]).name == "production" and x["args"][0] == "grompp"][0]
        self.assertIn("-t", prodpre["args"])
        self.assertNotIn("-cpi", prodpre["args"])

    def test_completed_receipts_reuse_and_tamper_blocks(self):
        output = self.root / "out"
        execute("gromacs_md", self.config, self.env, output, self.emit)
        count = len(self.commands())
        result = execute("gromacs_md", self.config, self.env, output, self.emit)
        self.assertEqual(result["progress"]["reused"], 9)
        self.assertEqual(len(self.commands()), count)
        (output / "workflow" / "nvt" / "final.gro").write_text("modified")
        with self.assertRaisesRegex(ValueError, "receipt"):
            execute("gromacs_md", self.config, self.env, output, self.emit)
        self.assertEqual(len(self.commands()), count)

    def test_config_or_input_change_blocks_reuse(self):
        output = self.root / "out"
        execute("gromacs_md", self.config, self.env, output, self.emit)
        changed = copy.deepcopy(self.config)
        changed["box"]["distance_nm"] = 2.0
        with self.assertRaisesRegex(ValueError, "receipt"):
            execute("gromacs_md", changed, self.env, output, self.emit)
        self.pdb.write_text(self.pdb.read_text() + "REMARK changed\n")
        with self.assertRaisesRegex(ValueError, "receipt"):
            execute("gromacs_md", self.config, self.env, output, self.emit)

    def test_safe_stop_only_after_committed_stage(self):
        output = self.root / "out"
        (output / "control").mkdir(parents=True)
        (output / "control" / "stop.json").write_text("{}")
        result = execute("gromacs_md", self.config, self.env, output, self.emit)
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(result["progress"]["completed"], 1)
        self.assertTrue((output / "workflow" / "pdb2gmx" / "receipt.json").is_file())
        self.assertEqual([x["args"][0] for x in self.commands()], ["pdb2gmx"])
        (output / "control" / "stop.json").unlink()
        result = execute("gromacs_md", self.config, self.env, output, self.emit)
        self.assertEqual(result["progress"]["reused"], 1)
        self.assertEqual(result["status"], "COMPLETED")

    def test_checkpoint_sigint_then_same_tpr_append_resume(self):
        output = self.root / "out"
        marker = self.root / "started"
        def request():
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            if marker.exists():
                (output / "control").mkdir(parents=True, exist_ok=True)
                (output / "control" / "stop.json").write_text("{}")
        thread = threading.Thread(target=request)
        with patch.dict(os.environ, {"PWB_FAKE_SLOW": "nvt", "PWB_FAKE_STARTED": str(marker)}):
            thread.start()
            result = execute("gromacs_md", self.config, self.env, output, self.emit)
        thread.join(10)
        self.assertEqual(result["status"], "STOPPED")
        nvt = output / "workflow" / "nvt"
        self.assertTrue((nvt / "partial.json").is_file())
        self.assertFalse((nvt / "receipt.json").exists())
        tpr_hash = hashlib.sha256((nvt / "stage.tpr").read_bytes()).hexdigest()
        (output / "control" / "stop.json").unlink()
        result = execute("gromacs_md", self.config, self.env, output, self.emit)
        self.assertEqual(result["status"], "COMPLETED")
        commands = [x["args"] for x in self.commands() if Path(x["cwd"]).name == "nvt"]
        self.assertEqual([x[0] for x in commands], ["grompp", "mdrun", "mdrun"])
        self.assertEqual(commands[-1][-3:], ["-cpi", "state.cpt", "-append"])
        self.assertEqual(hashlib.sha256((nvt / "stage.tpr").read_bytes()).hexdigest(), tpr_hash)

    def test_partial_checkpoint_tamper_never_fallback_noappend(self):
        output = self.root / "out"
        runner = StageRunner("gromacs_md", self.config, self.env, output, self.emit)
        src = self.pdb
        folder = runner.stage_root / "production"
        folder.mkdir()
        (folder / "state.cpt").write_text("original")
        from pwb.workflows import _atomic_json
        _atomic_json(folder / "partial.json", {"status": "checkpoint", "binding": runner.binding,
                                               "inputs": runner._inputs([src]), "outputs": runner._outputs(folder)})
        (folder / "state.cpt").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "检查点"):
            runner.run("production", [src], lambda d: None, lambda d: [], ["final.gro"], mdrun=True)
        self.assertEqual(self.commands(), [])

    def test_velocity_and_force_distinct_units_and_negative_linear_potential(self):
        for mode in ("velocity", "force"):
            cfg = copy.deepcopy(self.config);cfg["pull"] = self.pull(mode == "velocity")
            workflow = "gromacs_pull_" + mode
            self.assertEqual(validate(workflow, cfg, self.env), [])
            output = self.root / mode
            result = execute(workflow, cfg, self.env, output, self.emit)
            self.assertEqual(result["status"], "COMPLETED")
            prod = output / "workflow" / "production"
            params = (prod / "stage.mdp").read_text()
            self.assertIn("pull-coord1-geometry = direction", params)
            self.assertIn("pull-group2-pbcatom = 3", params)
            index = (prod / "index.ndx").read_text()
            self.assertIn("[ System ]\n1 2 3 4", index)
            self.assertIn("[ Reference ]\n1 2", index)
            self.assertIn("[ Moving ]\n3 4", index)
            if mode == "velocity":
                self.assertIn("pull-coord1-type = umbrella", params)
                self.assertIn("pull-coord1-rate = 0.001", params)
                self.assertIn("pull-coord1-k = 500.0", params)
            else:
                self.assertIn("pull-coord1-type = constant-force", params)
                self.assertIn("pull-coord1-k = -60.0", params)
                self.assertNotIn("pull-coord1-rate", params)
                self.assertNotIn("pull-coord1-init", params)
            for filename in ("pullx.xvg", "pullf.xvg"):
                self.assertTrue((prod / filename).is_file())
        negative = self.pull(False);negative["force_kj_mol_nm"] = -10.0
        self.assertEqual(pull_parameters("gromacs_pull_force", negative)["pull-coord1-k"], 10.0)

    def test_required_science_and_special_systems_blocked_preserved(self):
        for key in ("protonation", "box", "ions", "analysis", "mdrun"):
            cfg = copy.deepcopy(self.config);cfg.pop(key)
            self.assertTrue(validate("gromacs_md", cfg, self.env), key)
        cfg = copy.deepcopy(self.config);cfg["mdp"]["npt"].pop("compressibility")
        self.assertTrue(any("compressibility" in e for e in validate("gromacs_md", cfg, self.env)))
        cfg = copy.deepcopy(self.config);cfg["mdp"]["production"]["ref-t"] = None
        self.assertTrue(any("ref-t" in e for e in validate("gromacs_md", cfg, self.env)))
        self.pdb.write_text(self.pdb.read_text() + "HETATM    2 ZN    ZN A   2       0.000   0.000   0.000  1.00 90.00          ZN\n")
        original = self.pdb.read_bytes()
        self.assertTrue(any("残基/组分" in e for e in validate("gromacs_md", self.config, self.env)))
        self.assertEqual(self.pdb.read_bytes(), original)

    def test_prepared_topology_include_and_checkpoint_state_path(self):
        source = self.root / "prepared";source.mkdir()
        (source / "system.gro").write_text("prepared coord")
        (source / "state.cpt").write_text("prepared checkpoint")
        (source / "topol.top").write_text('#include "ligand.itp"\n')
        (source / "ligand.itp").write_text("; custom ligand parameters")
        cfg = {k: copy.deepcopy(v) for k, v in self.config.items() if k in {"mdp", "analysis", "mdrun"}}
        cfg.update(input_mode="prepared", system_kind="prepared_custom", special_features=["ligand"], prepared_verified=True,
                   prepared_root=str(source), coordinate="system.gro", topology="topol.top", checkpoint="state.cpt", start_stage="production")
        self.assertEqual(validate("gromacs_md", cfg, self.env), [])
        result = execute("gromacs_md", cfg, self.env, self.root / "out", self.emit)
        self.assertEqual(result["progress"]["completed"], 3)
        pre = self.commands()[0]["args"]
        self.assertEqual(pre[pre.index("-t") + 1], "input.cpt")
        self.assertFalse(any("-cpi" in x["args"] for x in self.commands()))
        self.assertEqual((self.root / "out/workflow/production/ligand.itp").read_bytes(), (source / "ligand.itp").read_bytes())
        self.assertEqual((source / "state.cpt").read_text(), "prepared checkpoint")
        cfg["prepared_verified"] = False
        self.assertTrue(validate("gromacs_md", cfg, self.env))

    def test_missing_include_failure_no_silent_atom_removal(self):
        source = self.root / "prepared";source.mkdir()
        (source / "system.gro").write_text("coord")
        (source / "state.cpt").write_text("cpt")
        (source / "topol.top").write_text('#include "missing.itp"\n')
        cfg = copy.deepcopy(self.config)
        cfg.update(input_mode="prepared", prepared_root=str(source), coordinate="system.gro", topology="topol.top", checkpoint="state.cpt", start_stage="production")
        with self.assertRaisesRegex(ValueError, "include"):
            execute("gromacs_md", cfg, self.env, self.root / "out", self.emit)
        self.assertTrue((source / "topol.top").is_file())

    def test_failure_retains_inputs_no_completed_receipt(self):
        original = self.pdb.read_bytes()
        with patch.dict(os.environ, {"PWB_FAKE_FAIL": "pdb2gmx"}):
            with self.assertRaisesRegex(RuntimeError, "返回码 7"):
                execute("gromacs_md", self.config, self.env, self.root / "out", self.emit)
        self.assertEqual(original, self.pdb.read_bytes())
        self.assertFalse((self.root / "out/workflow/pdb2gmx/receipt.json").exists())

    def test_pull_validation_geometry_overlap_units_protocol(self):
        cfg = copy.deepcopy(self.config);cfg["pull"] = self.pull(False)
        cfg["pull"]["force_kj_mol_nm"] = "100 pN"
        self.assertTrue(validate("gromacs_pull_force", cfg, self.env))
        cfg["pull"] = self.pull(True);cfg["pull"]["groups"][1]["atoms"] = [1, 3]
        self.assertTrue(any("重叠" in e for e in validate("gromacs_pull_velocity", cfg, self.env)))
        cfg["pull"] = self.pull(True);cfg["pull"]["vector"] = [0, 0, 0]
        self.assertTrue(any("零向量" in e for e in validate("gromacs_pull_velocity", cfg, self.env)))

    def test_bindcraft_official_command_and_private_settings(self):
        root = self.root / "BindCraft";root.mkdir();(root / "bindcraft.py").write_text(FAKE_BINDCRAFT)
        env = {"python": sys.executable, "bindcraft_root": str(root)}
        settings = {"binder_name": "Test", "design_path": str(self.root / "original_design_path"), "starting_pdb": str(self.pdb), "chains": "A",
                    "target_hotspot_residues": None, "lengths": [10, 20], "number_of_final_designs": 1}
        paths = {}
        for key, value in (("settings", settings), ("filters", {"test": True}), ("advanced", {"test": True})):
            p = self.root / f"{key}.json";p.write_text(json.dumps(value));paths[key] = str(p)
        original = Path(paths["settings"]).read_bytes()
        self.assertEqual(validate("bindcraft", paths, env), [])
        cmd = bindcraft_command(env, paths["settings"], paths["filters"], paths["advanced"])
        self.assertEqual(cmd[:3], [sys.executable, "-u", str(root / "bindcraft.py")])
        self.assertEqual(cmd[3::2], ["--settings", "--filters", "--advanced"])
        result = execute("bindcraft", paths, env, self.root / "out", self.emit)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["sequence"], "A")
        self.assertEqual(result["candidates"][0]["ranking_score"], .8)
        native = json.loads((self.root / "out/workflow/bindcraft/native-completion.json").read_text())
        self.assertEqual(native["attempted_trajectories"], 2)
        self.assertEqual(native["recorded_trajectory_rows"], 1)
        self.assertEqual(native["accepted_files"], 1)
        self.assertEqual(native["stop_reason"], "accepted_goal_reached")
        self.assertTrue(any(event["event"] == "BINDCRAFT_PROGRESS" for event in self.events))
        self.assertEqual(Path(paths["settings"]).read_bytes(), original)
        self.assertFalse((self.root / "original_design_path").exists())
        capabilities = next(t["capabilities"] for t in TEMPLATES if t["id"] == "bindcraft")
        self.assertFalse(capabilities["resume"])

    def test_cif_conversion_binds_interpreter_and_preserves_source(self):
        cif = self.root / "candidate.cif"
        cif.write_text("data_candidate\n# original CIF preserved\n")
        converter = self.root / "fake_selected_python"
        converter.write_text(f"#!{sys.executable}\n" +
                            "import sys\nfrom pathlib import Path\n" +
                            "assert sys.argv[1]=='-c'\nassert 'MMCIFParser' in sys.argv[2]\n" +
                            "Path(sys.argv[4]).write_text(" + repr(self.pdb.read_text()) + ")\n")
        converter.chmod(0o755)
        cfg = copy.deepcopy(self.config);cfg["pdb"] = str(cif)
        env = dict(self.env, python=str(converter))
        original = cif.read_bytes()
        result = execute("gromacs_md", cfg, env, self.root / "out", self.emit)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["progress"]["completed"], 10)
        self.assertEqual(cif.read_bytes(), original)
        commands = [event for event in self.events if event["event"]=="COMMAND" and event["stage"]=="cif_to_pdb"]
        self.assertEqual(commands[0]["data"]["command"][0], str(converter))

    def test_scalar_analysis_outputs_statistics_and_no_equilibrium_claim(self):
        from pwb.workflows import summarize_xvg
        xvg = self.root / "scalar.xvg"
        xvg.write_text('@ s0 legend "Potential"\n@ yaxis label "kJ/mol"\n0 1\n2 3\n')
        summary = summarize_xvg(xvg)
        self.assertEqual(summary["columns"][0]["mean"], 2.0)
        self.assertEqual(summary["columns"][0]["std_population"], 1.0)
        self.assertEqual(summary["rows"], 2)
        self.assertIn('kJ/mol', " ".join(summary["headers"]))
        xvg.write_text("0 nan\n")
        with self.assertRaises(ValueError): summarize_xvg(xvg)

    def test_native_progress_csv_partial_record_and_gromacs_actual_step(self):
        from pwb.workflows import csv_complete_rows, gromacs_progress
        csv = self.root / "progress.csv"
        csv.write_text('Design,Score\nfirst,0.5\nsecond,')
        self.assertEqual(csv_complete_rows(csv), [{"Design": "first", "Score": "0.5"}])
        log = self.root / "md.log"
        log.write_text('Step Time\n0 0.000\nenergy block\n     Step          Time\n     250           0.50000\n')
        progress = gromacs_progress(log, 1000)
        self.assertEqual(progress["observed_step"], 250)
        self.assertEqual(progress["observed_time_ps"], .5)
        self.assertEqual(progress["configured_nsteps"], 1000)
        self.assertFalse(progress["stage_complete"])

    def test_bound_wrapper_request_and_relative_env_rejected(self):
        output = self.root / "out"
        command = build_command("gromacs_md", self.config, self.env, output)
        self.assertEqual(command[:4], [sys.executable, "-u", "-m", "pwb.workflows"])
        request = json.loads((output / "workflow-request.json").read_text())
        self.assertEqual(request["env"]["gmx"], str(self.gmx))
        env = dict(self.env, gmx="gmx")
        self.assertTrue(validate("gromacs_md", self.config, env))

    def test_downstream_preflight_defers_only_future_structure(self):
        cfg=copy.deepcopy(self.config);cfg['pdb']=str(self.root/'future_candidate.pdb')
        self.assertTrue(validate('gromacs_md',cfg,self.env))
        self.assertEqual(validate('gromacs_md',cfg,self.env,check_structure=False),[])
        cfg['pdb']=None
        self.assertEqual(validate('gromacs_md',cfg,self.env,check_structure=False),[])
        for workflow in ('gromacs_md','gromacs_pull_velocity','gromacs_pull_force'):
            configured=copy.deepcopy(cfg)
            if workflow!='gromacs_md':configured['pull']=self.pull(workflow.endswith('velocity'))
            self.assertEqual(validate(workflow,configured,self.env,check_structure=False),[])
            invalid=copy.deepcopy(configured);invalid['mdp']['production'].pop('nsteps')
            self.assertTrue(validate(workflow,invalid,self.env,check_structure=False))
            invalid=copy.deepcopy(configured);invalid['mdrun'].pop('ntmpi')
            self.assertTrue(validate(workflow,invalid,self.env,check_structure=False))
            self.assertTrue(validate(workflow,configured,dict(self.env,gmx='/missing/gmx'),check_structure=False))
            if workflow!='gromacs_md':
                invalid=copy.deepcopy(configured);invalid['pull'].pop('protocol_rationale')
                self.assertTrue(validate(workflow,invalid,self.env,check_structure=False))
        cfg['pdb']='relative.pdb'
        self.assertTrue(validate('gromacs_md',cfg,self.env,check_structure=False))
        cfg['pdb']=str(self.root/'future.exe')
        self.assertTrue(validate('gromacs_md',cfg,self.env,check_structure=False))
        self.pdb.write_text(self.pdb.read_text()+'HETATM    2 ZN    ZN A   2       0.000   0.000   0.000  1.00 90.00          ZN\n')
        cfg['pdb']=str(self.pdb)
        self.assertTrue(any('残基/组分' in error for error in validate('gromacs_md',cfg,self.env,check_structure=False)))
        cfg.update(input_mode='prepared',prepared_root='/missing/prepared',coordinate='future.gro',topology='topol.top',checkpoint='state.cpt',start_stage='production')
        self.assertTrue(validate('gromacs_md',cfg,self.env,check_structure=False))

    def failed_bindcraft(self):
        root=self.root/'BindCraft';root.mkdir();(root/'bindcraft.py').write_text(FAKE_BINDCRAFT+'\nraise SystemExit(7)\n')
        cfg={}
        settings={'binder_name':'Test','starting_pdb':str(self.pdb),'chains':'A','target_hotspot_residues':None,'lengths':[1,2],'number_of_final_designs':1}
        for key,value in [('settings',settings),('filters',{'test':True}),('advanced',{'max_trajectories':False})]:
            path=self.root/f'{key}.json';path.write_text(json.dumps(value));cfg[key]=str(path)
        env={'python':sys.executable,'bindcraft_root':str(root)};output=self.root/'out'
        with self.assertRaisesRegex(RuntimeError,'返回码 7'):execute('bindcraft',cfg,env,output,self.emit)
        return cfg,env,output

    def test_failed_bindcraft_partial_collection_verified_and_idempotent(self):
        from pwb.workflows import collect_partial_candidates
        cfg,env,output=self.failed_bindcraft()
        folder=output/'workflow/bindcraft'
        self.assertFalse((folder/'receipt.json').exists())
        result=collect_partial_candidates('bindcraft',cfg,env,output,self.emit)
        self.assertEqual(len(result['candidates']),1);self.assertFalse(result['stage_complete'])
        candidate=result['candidates'][0]
        self.assertEqual(candidate['sequence'],'A')
        self.assertEqual(candidate['structure_sha256'],hashlib.sha256((output/candidate['structure']).read_bytes()).hexdigest())
        self.assertIn('native_csv_sha256',candidate)
        second=collect_partial_candidates('bindcraft',cfg,env,output,self.emit)
        self.assertEqual(second['new_candidates'],[])
        self.assertEqual(len([x for x in self.events if x['event']=='CANDIDATE']),1)
        self.assertFalse((folder/'receipt.json').exists());self.assertFalse((folder/'native-completion.json').exists())
        self.assertFalse(any(x['event']=='STAGE_COMMITTED' for x in self.events))

    def test_partial_bindcraft_rejects_bad_files_and_wrong_input_binding(self):
        from pwb.workflows import collect_partial_candidates
        cfg,env,output=self.failed_bindcraft();folder=output/'workflow/bindcraft'
        pdb=folder/'designs/Accepted/candidate_model1.pdb'
        pdb.write_text(pdb.read_text().replace('0.000','nan  ',1))
        result=collect_partial_candidates('bindcraft',cfg,env,output,self.emit)
        self.assertEqual(result['candidates'],[])
        self.assertTrue(any(x['event']=='CANDIDATE_INVALID' for x in self.events))
        (folder/'target.pdb').write_text('changed target')
        with self.assertRaisesRegex(ValueError,'绑定|binding'):
            collect_partial_candidates('bindcraft',cfg,env,output,self.emit)

    def test_partial_bindcraft_rejects_symlink_duplicates_and_changed_entrypoint(self):
        from pwb.workflows import collect_partial_candidates
        cfg,env,output=self.failed_bindcraft();folder=output/'workflow/bindcraft'
        pdb=folder/'designs/Accepted/candidate_model1.pdb';original=pdb.read_bytes()
        outside=self.root/'outside.pdb';outside.write_bytes(original)
        pdb.unlink();pdb.symlink_to(outside)
        self.assertEqual(collect_partial_candidates('bindcraft',cfg,env,output,self.emit)['candidates'],[])
        pdb.unlink();pdb.write_bytes(original)
        native=folder/'designs/final_design_stats.csv';original_csv=native.read_text()
        native.write_text(original_csv+'2,candidate,A,0.5\n')
        self.assertEqual(collect_partial_candidates('bindcraft',cfg,env,output,self.emit)['candidates'],[])
        native.write_text(original_csv)
        entry=Path(env['bindcraft_root'])/'bindcraft.py';entry.write_text(entry.read_text()+'\n# changed after execution\n')
        with self.assertRaisesRegex(ValueError,'绑定'):
            collect_partial_candidates('bindcraft',cfg,env,output,self.emit)
        self.assertFalse(any(event['event']=='CANDIDATE' for event in self.events))


if __name__ == "__main__":
    unittest.main()
