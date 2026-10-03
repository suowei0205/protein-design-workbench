"""Explicit, locally bound workflow adapters. No scientific defaults or shell execution.

GROMACS stage receipts describe process completion, never scientific acceptance.
Source inputs are copied; a stage becomes reusable only after its receipt is committed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from typing import Callable


def field(name, label, kind="string", required=True, help=None):
    item = {"name": name, "label": label, "type": kind, "required": required}
    if help:
        item["help"] = help
    return item


GENERIC_FIELDS = [field("input_files", "输入文件列表", "json", False),
                  field("parameters", "参数", "json", False)]
MD_FIELDS = [
    field("input_mode", "准备方式", help="protein_pdb 或 prepared"),
    field("system_kind", "系统类型", help="protein_aqueous；特殊系统仅 prepared_custom 路径"),
    field("special_features", "特殊组分说明", "json", help="蛋白水溶液请显式填写 []；配体、膜、金属、修饰须 prepared 路径"),
    field("pdb", "原始 PDB 路径", required=False),
    field("forcefield", "力场名称", required=False),
    field("water_model", "水模型", required=False),
    field("solvent_coordinates", "水盒坐标路径或 GROMACS 库名称", required=False),
    field("protonation", "质子化与端基配置", "json", False,
          '例如 {"flags":["-his","-ter"],"stdin":"菜单编号\\n","decisions":"残基和端基选择依据"}；必须按当前版本菜单复核'),
    field("chainsep", "链分隔规则", required=False),
    field("merge", "链合并规则", required=False),
    field("box", "盒型与缓冲距离", "json", False, '{"type":"dodecahedron","distance_nm":填写数值}'),
    field("ions", "离子配置", "json", False,
          '{"positive":"NA","negative":"CL","positive_charge":整数,"negative_charge":整数,"minimum_distance_nm":正数,"concentration_molar":填写数值,"neutralize":true,"solvent_group":"SOL","seed":整数}'),
    field("prepared_root", "已准备输入目录", required=False),
    field("coordinate", "prepared_root 内 gro 文件名", required=False),
    field("topology", "prepared_root 内 top 文件名", required=False),
    field("checkpoint", "prepared_root 内输入 cpt 文件名", required=False),
    field("index", "prepared_root 内自定义 ndx 文件名", required=False, help="原子编号必须对应最终全系统；与默认组同名时必须完全一致"),
    field("restraint_reference", "固定位置约束参考坐标绝对路径", required=False, help="启用 -DPOSRES 必填；对应最终全系统编号，所有阶段使用同一文件"),
    field("start_stage", "已准备系统起始阶段", required=False, help="minimize、nvt、npt 或 production"),
    field("prepared_verified", "已人工复核特殊系统参数", "boolean", False),
    field("mdp", "各阶段完整 MDP 参数", "json", help="ions/minimize/nvt/npt/production 的参数对象；不得留空或依赖隐含科学默认值"),
    field("mdrun", "运行资源与检查点间隔", "json", help='{"ntomp":整数,"ntmpi":整数,"checkpoint_minutes":正数,"device":"cpu"或"gpu"}'),
    field("analysis", "分析与代表帧配置", "json", help='{"energy_terms":["Potential","Temperature"],"fit_group":"Backbone","rms_group":"Backbone","frame_group":"System","frame_time_ps":填写数值}')]
PULL_FIELD = field("pull", "显式拉伸定义", "json", help="两个互不重叠的全系统 1-based 原子编号组、PBC 参考原子、distance/direction 几何、dimensions/vector、输出间隔、速度/弹簧或有符号力和单位；详见模板")
TEMPLATES = [
    {"id": "generic_script", "title": "通用 Python 脚本", "description": "绑定环境执行。安全停止需脚本主动提交边界；无通用内部续跑。",
     "fields": [field("script", "脚本路径")] + GENERIC_FIELDS,
     "capabilities": {"safe_stop": False, "resume": False}},
    {"id": "generic_notebook", "title": "通用 Notebook", "description": "按所选 Python 内核执行；单元错误即停止。",
     "fields": [field("notebook", "Notebook 路径")] + GENERIC_FIELDS,
     "capabilities": {"safe_stop": False, "resume": False}},
    {"id": "sr56", "title": "SR56 已桥接 Notebook", "description": "仅在已提交阶段边界停止和按原缓存语义续跑。",
     "fields": [field("notebook", "Notebook 路径")] + GENERIC_FIELDS,
     "capabilities": {"safe_stop": True, "resume": True}},
    {"id": "bindcraft", "title": "BindCraft 官方工作流", "description": "使用官方 bindcraft.py 和三个明确配置文件。停止请求仅在完整脚本完成后兑现；内部轨迹续跑未实现。",
     "fields": [field("settings", "目标 settings JSON"), field("filters", "filters JSON"), field("advanced", "advanced JSON")],
     "capabilities": {"safe_stop": True, "safe_stop_boundary": "entire official script", "resume": False}},
    *[{"id": name, "title": title,
       "description": "显式准备、能量最小化、NVT、NPT、生产、标量分析与代表帧；参数需人工科学复核。",
       "fields": MD_FIELDS + ([PULL_FIELD] if "pull" in name else []),
       "capabilities": {"safe_stop": True, "resume": True, "resume_boundary": "validated completed stage or GROMACS checkpoint"}}
      for name, title in [("gromacs_md", "GROMACS 水溶液 MD"),
                          ("gromacs_pull_velocity", "GROMACS 恒速拉伸"),
                          ("gromacs_pull_force", "GROMACS 恒力拉伸")]]]

MD_STAGES = ("minimize", "nvt", "npt", "production")
PROGRESS_POLL_SECONDS = 15
AA = set("ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL HID HIE HIP HSD HSE HSP ASH GLH LYN CYX".split())
PDB_FLAGS = {"-his", "-lys", "-arg", "-asp", "-glu", "-gln", "-ter", "-ss", "-ignh"}
SOURCES = ["https://github.com/martinpacesa/BindCraft/blob/main/bindcraft.py",
           "https://github.com/martinpacesa/BindCraft/blob/main/functions/generic_utils.py",
           "https://manual.gromacs.org/current/onlinehelp/gmx-pdb2gmx.html",
           "https://manual.gromacs.org/current/onlinehelp/gmx-make_ndx.html",
           "https://manual.gromacs.org/current/onlinehelp/gmx-genion.html",
           "https://manual.gromacs.org/current/onlinehelp/gmx-mdrun.html",
           "https://manual.gromacs.org/current/onlinehelp/gmx-grompp.html",
           "https://manual.gromacs.org/current/user-guide/mdp-options.html",
           "https://manual.gromacs.org/current/user-guide/managing-simulations.html"]


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _absolute_file(value):
    return isinstance(value, str) and Path(value).is_absolute() and Path(value).is_file()


def _inside(root, name):
    if not isinstance(name, str) or Path(name).is_absolute():
        raise ValueError("prepared 文件名必须是目录内的相对路径")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("prepared 输入路径越过目录边界")
    return path


def _mdp(value):
    if not isinstance(value, dict):
        return {}
    return {str(k).strip().lower().replace("_", "-"): v for k, v in value.items()}


def _pdb_special(path):
    bad = set()
    for line in Path(path).read_text(errors="replace").splitlines():
        if line[:6].strip() in {"ATOM", "HETATM"}:
            residue = line[17:20].strip()
            if residue not in AA:
                bad.add(residue or "unnamed")
    return sorted(bad)


def validate(workflow, config, env, *, check_structure=True):
    """Validate all runtime/science fields; defer only a future MD candidate file.

    check_structure=False permits an absent protein_pdb candidate or an absolute
    future path. Existing structures, prepared inputs, environments, references,
    budgets, MDPs and pull definitions retain their normal checks.
    """
    errors = []
    if workflow not in {t["id"] for t in TEMPLATES}:
        return [f"未知工作流：{workflow}"]
    if not isinstance(config, dict) or not isinstance(env, dict):
        return ["config 和 environment 必须为 JSON 对象"]
    if not _absolute_file(env.get("python")):
        errors.append("environment.python 必须绑定实际存在的绝对解释器路径")
    if workflow in {"generic_script", "generic_notebook", "sr56"}:
        key = "script" if workflow == "generic_script" else "notebook"
        if not _absolute_file(config.get(key)):
            errors.append(f"{key} 必须为实际存在的绝对路径")
        return errors
    if workflow == "bindcraft":
        root = Path(env["bindcraft_root"]) if isinstance(env.get("bindcraft_root"), str) else Path()
        if not root.is_absolute() or not (root / "bindcraft.py").is_file():
            errors.append("environment.bindcraft_root 必须指向包含官方 bindcraft.py 的绝对目录")
        for key in ("settings", "filters", "advanced"):
            if not _absolute_file(config.get(key)):
                errors.append(f"{key} 必须指向实际存在的绝对 JSON 路径")
                continue
            try:
                value = json.loads(Path(config[key]).read_text())
                if not isinstance(value, dict) or not value:
                    raise ValueError("empty object")
                if key == "settings":
                    for setting in ("binder_name", "starting_pdb", "chains", "lengths", "number_of_final_designs", "target_hotspot_residues"):
                        if setting not in value:
                            errors.append(f"BindCraft settings 缺少 {setting}")
                    pdb = value.get("starting_pdb")
                    if not _absolute_file(pdb):
                        errors.append("BindCraft starting_pdb 必须显式使用实际存在的绝对路径")
                    if not value.get("chains"):
                        errors.append("BindCraft chains 必须非空")
                    lengths = value.get("lengths")
                    if not isinstance(lengths, list) or len(lengths) != 2 or any(type(x) is not int or x < 1 for x in lengths) or lengths[0] > lengths[1]:
                        errors.append("BindCraft lengths 必须为 [最小长度, 最大长度] 正整数")
                    n = value.get("number_of_final_designs")
                    if type(n) is not int or n < 1:
                        errors.append("BindCraft number_of_final_designs 必须为正整数")
            except (OSError, ValueError, TypeError) as exc:
                errors.append(f"{key} JSON 无法读取：{exc}")
        return errors
    if not _absolute_file(env.get("gmx")):
        errors.append("environment.gmx 必须绑定实际存在的绝对 GROMACS 可执行路径")
    reference = config.get("restraint_reference")
    if reference and not _absolute_file(reference):
        errors.append("restraint_reference 必须为已人工核对全系统原子编号的现有绝对坐标路径")
    mode = config.get("input_mode")
    if mode not in {"protein_pdb", "prepared"}:
        errors.append("input_mode 必须明确为 protein_pdb 或 prepared")
    features = config.get("special_features")
    if not isinstance(features, list):
        errors.append("special_features 必须显式填写列表，普通蛋白水溶液填写 []")
    if mode == "protein_pdb":
        if config.get("system_kind") != "protein_aqueous" or features:
            errors.append("简单水溶液模板不接受膜、配体、金属、修饰或未知系统；保留原输入，改用人工参数化的 prepared 路径")
        pdb = config.get("pdb")
        exists = _absolute_file(pdb)
        if check_structure and not exists:
            errors.append("pdb 必须为实际存在的绝对路径")
        elif not check_structure and pdb is not None and (not isinstance(pdb, str) or not Path(pdb).is_absolute()):
            errors.append("未来候选 pdb 若填写路径，必须为绝对结构文件路径")
        elif isinstance(pdb, str) and Path(pdb).suffix.lower() not in {".pdb", ".cif", ".mmcif"}:
            errors.append("输入结构格式仅支持 .pdb/.cif/.mmcif")
        elif isinstance(pdb, str) and Path(pdb).exists() and not exists:
            errors.append("pdb 路径已存在但不是结构文件")
        elif exists and Path(pdb).suffix.lower() == ".pdb":
            bad = _pdb_special(pdb)
            if bad:
                errors.append("PDB 含简单模板未参数化残基/组分：" + ", ".join(bad) + "；请保留文件并走 prepared 路径")
        for key in ("forcefield", "water_model", "solvent_coordinates", "chainsep", "merge"):
            if not isinstance(config.get(key), str) or not config[key].strip():
                errors.append(f"必须明确填写 {key}")
        if config.get("chainsep") not in {"id", "ter", "id_or_ter", "id_and_ter"}:
            errors.append("chainsep 必须为 id/ter/id_or_ter/id_and_ter；非交互批处理")
        if config.get("merge") not in {"no", "all"}:
            errors.append("merge 必须明确为 no 或 all")
        proton = config.get("protonation", {})
        if not isinstance(proton, dict) or not isinstance(proton.get("flags"), list) or any(not isinstance(flag, str) or flag not in PDB_FLAGS for flag in proton.get("flags", [])) or not isinstance(proton.get("stdin"), str) or not proton.get("decisions"):
            errors.append("protonation 必須明确 flags、stdin 和 decisions；flags 仅支持质子化/端基/二硫键/氢原子选择")
        elif any(flag in proton["flags"] for flag in PDB_FLAGS - {"-ignh"}) and not proton["stdin"].strip():
            errors.append("交互质子化 flags 必须提供已按当前版本菜单确认的 stdin 选择编号")
        box = config.get("box", {})
        if not isinstance(box, dict) or box.get("type") not in {"cubic", "triclinic", "dodecahedron", "octahedron"} or not _finite(box.get("distance_nm")) or box.get("distance_nm", 0) <= 0:
            errors.append("box 必须明确 type 和正值 distance_nm")
        ions = config.get("ions", {})
        if not isinstance(ions, dict) or any(not isinstance(ions.get(k), str) or not ions[k].strip() or "\n" in ions[k] for k in ("positive", "negative", "solvent_group")) or not _finite(ions.get("concentration_molar")) or ions.get("concentration_molar", -1) < 0 or type(ions.get("neutralize")) is not bool or type(ions.get("seed")) is not int or ions.get("seed", -1) < 0 or type(ions.get("positive_charge")) is not int or ions.get("positive_charge", 0) <= 0 or type(ions.get("negative_charge")) is not int or ions.get("negative_charge", 0) >= 0 or not _finite(ions.get("minimum_distance_nm")) or ions.get("minimum_distance_nm", 0) <= 0:
            errors.append("ions 必须明确 positive/negative/positive_charge/negative_charge/minimum_distance_nm/solvent_group/concentration_molar/neutralize/seed")
    elif mode == "prepared":
        root = Path(config["prepared_root"]) if isinstance(config.get("prepared_root"), str) else Path()
        if not root.is_absolute() or not root.is_dir():
            errors.append("prepared_root 必须为现有绝对目录，内含全部本地拓扑 include")
        for key in ("coordinate", "topology"):
            try:
                if not _inside(root, config.get(key)).is_file():
                    errors.append(f"prepared {key} 文件不存在")
            except ValueError as exc:
                errors.append(str(exc))
        if config.get("checkpoint"):
            try:
                if not _inside(root, config["checkpoint"]).is_file():
                    errors.append("prepared checkpoint 文件不存在")
            except ValueError as exc:
                errors.append(str(exc))
        if config.get("index"):
            try:
                if not _inside(root, config["index"]).is_file():
                    errors.append("prepared index 文件不存在")
            except ValueError as exc:
                errors.append(str(exc))
        if config.get("start_stage") not in MD_STAGES:
            errors.append("prepared start_stage 必须为 minimize/nvt/npt/production")
        if config.get("start_stage") in {"npt", "production"} and not config.get("checkpoint"):
            errors.append("prepared NPT/production 起点必须提供上游 checkpoint 状态")
        if config.get("system_kind") not in {"protein_aqueous", "prepared_custom"}:
            errors.append("prepared system_kind 必须为 protein_aqueous 或 prepared_custom")
        if features or config.get("system_kind") == "prepared_custom":
            if config.get("prepared_verified") is not True:
                errors.append("特殊 prepared 系统必须明确 prepared_verified=true；不会自动移除或重参数化任何组分")
    stages = list(MD_STAGES)
    if mode == "prepared" and config.get("start_stage") in stages:
        stages = stages[stages.index(config["start_stage"]):]
    if mode == "protein_pdb":
        stages.insert(0, "ions")
    mdp = config.get("mdp", {})
    if not isinstance(mdp, dict):
        errors.append("mdp 必须为阶段参数 JSON 对象")
        mdp = {}
    shared = {"integrator", "nsteps", "cutoff-scheme", "coulombtype", "rcoulomb", "vdwtype", "rvdw", "pbc"}
    for stage in stages:
        values = _mdp(mdp.get(stage))
        required = shared | ({"emtol", "emstep"} if stage in {"ions", "minimize"} else
                             {"dt", "constraints", "tcoupl", "tc-grps", "tau-t", "ref-t", "pcoupl", "gen-vel", "continuation", "nstenergy", "nstlog", "nstxout-compressed"})
        if stage in {"npt", "production"} and str(values.get("pcoupl", "")).lower() != "no":
            required |= {"pcoupltype", "tau-p", "ref-p", "compressibility"}
        if str(values.get("gen-vel", "")).lower() == "yes":
            required |= {"gen-temp", "gen-seed"}
        missing = sorted(key for key in required if key not in values or values[key] is None or values[key] == "" or values[key] == [])
        if missing:
            errors.append(f"mdp.{stage} 缺少显式科学参数：{', '.join(missing)}")
        if re.search(r"(?:^|\s)-DPOSRES(?:\s|=|$)", str(values.get("define", ""))) and not _absolute_file(reference):
            errors.append(f"mdp.{stage} 启用 POSRES；必须提供显式固定 restraint_reference，避免阶段间参考坐标漂移")
        if values:
            if type(values.get("nsteps")) is not int or values.get("nsteps", 0) <= 0:
                errors.append(f"mdp.{stage}.nsteps 必须为正整数")
            if stage == "nvt" and str(values.get("pcoupl", "")).lower() != "no":
                errors.append("NVT 阶段 pcoupl 必须为 no")
            if stage == "npt" and str(values.get("pcoupl", "")).lower() == "no":
                errors.append("NPT 阶段必须明确压力耦合参数")
            if stage in {"npt", "production"} and str(values.get("continuation", "")).lower() != "yes":
                errors.append(f"{stage} 必须 continuation=yes，继承上游检查点状态")
            if stage in {"npt", "production"} and str(values.get("gen-vel", "")).lower() != "no":
                errors.append(f"{stage} 必须 gen-vel=no，保留上游状态")
            if stage not in {"ions", "minimize"}:
                if not _finite(values.get("dt")) or values.get("dt", 0) <= 0:
                    errors.append(f"mdp.{stage}.dt 必须为正值 ps")
                if type(values.get("nstxout-compressed")) is not int or values.get("nstxout-compressed", 0) <= 0:
                    errors.append(f"mdp.{stage}.nstxout-compressed 必须为正整数以支持轨迹分析")
                for interval in ("nstlog", "nstenergy"):
                    if type(values.get(interval)) is not int or values.get(interval, 0) <= 0:
                        errors.append(f"mdp.{stage}.{interval} 必须为正整数以支持原生进度和标量分析")
            if any(k == "pull" or k.startswith("pull-") for k in values):
                errors.append(f"mdp.{stage} 不接受隐藏 pull 参数；请使用显式 pull 配置")
            for key, value in values.items():
                if not re.fullmatch(r"[a-z0-9-]+", key) or "\n" in str(value) or "\r" in str(value) or ";" in str(value):
                    errors.append(f"mdp.{stage}.{key} 必须为单行参数，不允许注入其他设置")
    execution = config.get("mdrun", {})
    if not isinstance(execution, dict) or any(type(execution.get(k)) is not int or execution.get(k, 0) < 1 for k in ("ntomp", "ntmpi")) or not _finite(execution.get("checkpoint_minutes")) or execution.get("checkpoint_minutes", 0) <= 0 or execution.get("device") not in {"cpu", "gpu"}:
        errors.append("mdrun 必须明确正整数 ntomp/ntmpi、正值 checkpoint_minutes、cpu/gpu device")
    analysis = config.get("analysis", {})
    if not isinstance(analysis, dict) or not isinstance(analysis.get("energy_terms"), list) or not analysis.get("energy_terms") or any(not isinstance(x, str) or not x.strip() or "\n" in x for x in analysis.get("energy_terms", [])) or any(not isinstance(analysis.get(k), str) or not analysis[k].strip() or "\n" in analysis[k] for k in ("fit_group", "rms_group", "frame_group")) or not _finite(analysis.get("frame_time_ps")) or analysis.get("frame_time_ps", -1) < 0:
        errors.append("analysis 必须明确 energy_terms、fit_group、rms_group、frame_group 和非负 frame_time_ps")
    if "pull" in workflow:
        errors += _validate_pull(workflow, config.get("pull"))
    return errors


def _validate_pull(workflow, pull):
    if not isinstance(pull, dict):
        return ["拉伸工作流必须填写 pull 对象"]
    errors = []
    groups = pull.get("groups")
    if not isinstance(groups, list) or len(groups) != 2:
        errors.append("pull.groups 必须为两个明确原子组")
        groups = []
    names, atoms = set(), []
    for group in groups:
        if not isinstance(group, dict):
            errors.append("pull group 必须为对象")
            continue
        name = group.get("name", "")
        indices = group.get("atoms")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) or name in names:
            errors.append("pull group 名称必须唯一并仅含字母数字下划线")
        if isinstance(name, str):
            names.add(name)
        if not isinstance(indices, list) or not indices or any(type(x) is not int or x < 1 for x in indices) or len(indices) != len(set(indices)):
            errors.append("pull group atoms 必须为唯一的正整数全系统 1-based 原子编号")
            indices = []
        if group.get("pbcatom") not in indices:
            errors.append("pull group pbcatom 必须显式选择组内一个全系统原子")
        atoms.append(set(indices))
    if len(atoms) == 2 and atoms[0] & atoms[1]:
        errors.append("pull 两组不得重叠")
    geometry = pull.get("geometry")
    if geometry not in {"distance", "direction"}:
        errors.append("本模板只接受 distance 或 direction；其他几何需专用工作流")
    dims = pull.get("dimensions")
    if not isinstance(dims, list) or len(dims) != 3 or any(x not in ("Y", "N") for x in dims) or "Y" not in dims:
        errors.append("pull.dimensions 必须为三个 Y/N 且至少一维启用")
    vector = pull.get("vector")
    if not isinstance(vector, list) or len(vector) != 3 or any(not _finite(x) for x in vector):
        errors.append("pull.vector 必须明确三个有限数值")
    elif geometry == "direction":
        if not any(vector):
            errors.append("direction pull vector 不得为零向量")
        if isinstance(dims, list) and len(dims) == 3 and any(x != 0 and d != "Y" for x, d in zip(vector, dims)):
            errors.append("direction 非零向量分量须在 dimensions 中启用")
    for key in ("nstxout", "nstfout"):
        if type(pull.get(key)) is not int or pull.get(key, 0) <= 0:
            errors.append(f"pull.{key} 必须为正整数")
    if not pull.get("group_numbering_basis") or not pull.get("protocol_rationale"):
        errors.append("pull 必须说明 group_numbering_basis（最终已准备全系统编号）和 protocol_rationale")
    if workflow.endswith("velocity"):
        required = {"rate_nm_per_ps", "spring_kj_mol_nm2", "initial_nm", "start_from_initial"}
        for key in required - {"start_from_initial"}:
            if not _finite(pull.get(key)):
                errors.append(f"pull.{key} 必须明确有限数值及指定单位")
        if _finite(pull.get("spring_kj_mol_nm2")) and pull["spring_kj_mol_nm2"] <= 0:
            errors.append("弹簧系数必须为正值 kJ mol^-1 nm^-2")
        if type(pull.get("start_from_initial")) is not bool:
            errors.append("pull.start_from_initial 必须显式 true/false")
        if "force_kj_mol_nm" in pull:
            errors.append("恒速模板不接受恒力参数")
    else:
        if not _finite(pull.get("force_kj_mol_nm")):
            errors.append("pull.force_kj_mol_nm 必须明确有符号力，单位 kJ mol^-1 nm^-1；适配器按官方定义写 k=-force")
        if any(k in pull for k in ("rate_nm_per_ps", "spring_kj_mol_nm2", "initial_nm", "start_from_initial")):
            errors.append("恒力模板不接受速度、弹簧、初始位置参数")
    return errors


def _hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w") as file:
        json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_command(workflow, config, env, output_dir):
    errors = validate(workflow, config, env)
    if errors:
        raise ValueError("; ".join(errors))
    if workflow not in {"bindcraft", "gromacs_md", "gromacs_pull_velocity", "gromacs_pull_force"}:
        raise ValueError("通用脚本和 Notebook 由绑定环境的 worker 执行")
    root = Path(output_dir).resolve()
    request = root / "workflow-request.json"
    _atomic_json(request, {"workflow": workflow, "config": config, "env": env, "output_dir": str(root)})
    return [env["python"], "-u", "-m", "pwb.workflows", "execute", "--request", str(request)]


def bindcraft_command(env, settings, filters, advanced):
    return [env["python"], "-u", str(Path(env["bindcraft_root"]) / "bindcraft.py"),
            "--settings", str(settings), "--filters", str(filters), "--advanced", str(advanced)]


def pull_parameters(workflow, pull):
    values = {"pull": "yes", "pull-ngroups": 2, "pull-ncoords": 1,
              "pull-coord1-groups": "1 2", "pull-coord1-geometry": pull["geometry"],
              "pull-coord1-dim": " ".join(pull["dimensions"]), "pull-coord1-vec": " ".join(map(str, pull["vector"])),
              "pull-nstxout": pull["nstxout"], "pull-nstfout": pull["nstfout"]}
    for i, group in enumerate(pull["groups"], 1):
        values[f"pull-group{i}-name"] = group["name"]
        values[f"pull-group{i}-pbcatom"] = group["pbcatom"]
    if workflow.endswith("velocity"):
        values.update({"pull-coord1-type": "umbrella", "pull-coord1-rate": pull["rate_nm_per_ps"],
                       "pull-coord1-k": pull["spring_kj_mol_nm2"], "pull-coord1-init": pull["initial_nm"],
                       "pull-coord1-start": "yes" if pull["start_from_initial"] else "no"})
    else:
        values.update({"pull-coord1-type": "constant-force", "pull-coord1-k": -pull["force_kj_mol_nm"]})
    return values


class WorkflowStop(Exception):
    pass


class StageRunner:
    def __init__(self, workflow, config, env, root, emit):
        self.workflow, self.config, self.env = workflow, config, env
        self.root = Path(root).resolve()
        self.stage_root = self.root / "workflow"
        self.stage_root.mkdir(parents=True, exist_ok=True)
        self.emit = emit
        self.binding = _digest({"workflow": workflow, "config": config, "env": env,
                                "executables": {k: _hash(env[k]) for k in ("python", "gmx") if env.get(k) and Path(env[k]).is_file()}})
        self.completed, self.reused, self.invalid = 0, 0, 0
        self.stages = []

    def event(self, event, stage, **data):
        if event == "CANDIDATE_INVALID":
            self.invalid += 1
        self.emit(event, stage=stage, key=stage, data=data)

    def stopped(self):
        return (self.root / "control" / "stop.json").is_file()

    def boundary(self, stage):
        if self.stopped():
            _atomic_json(self.root / "control" / "safe_stopped.json", {"run_id": os.getenv("PWB_RUN_ID"), "attempt_id": os.getenv("PWB_ATTEMPT_ID"), "stage": stage, "boundary": "completed stage", "committed_at": time.time()})
            self.event("SAFE_STOP", stage, boundary="completed stage", attempt_id=os.getenv("PWB_ATTEMPT_ID"))
            raise WorkflowStop(stage)

    def _inputs(self, files):
        return {str(Path(p).resolve()): _hash(p) for p in files}

    def _outputs(self, folder):
        return {str(p.relative_to(self.root)): _hash(p) for p in sorted(folder.rglob("*"))
                if p.is_file() and not p.is_symlink() and p.name not in {"receipt.json", "partial.json", "execution.log"} and not p.name.endswith(".tmp")}

    def _valid(self, receipt, inputs, folder=None):
        if receipt.get("binding") != self.binding or receipt.get("inputs") != inputs:
            return False
        outputs = receipt.get("outputs", {})
        if folder is not None and outputs != self._outputs(folder):
            return False
        if not outputs:
            return False
        for name, digest in outputs.items():
            path = (self.root / name).resolve()
            if not path.is_relative_to(self.root) or not path.is_file() or _hash(path) != digest:
                return False
        return True

    def _load(self, path):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def run(self, stage, inputs, prepare, commands, expected, mdrun=False, stop_boundary=True, finalize=None):
        folder = self.stage_root / stage
        hashes = self._inputs(inputs)
        receipt_path = folder / "receipt.json"
        if receipt_path.exists():
            receipt = self._load(receipt_path)
            if not self._valid(receipt, hashes, folder) or receipt.get("status") != "completed":
                self.event("STAGE_INVALID", stage, reason="config/input/output binding changed")
                raise ValueError(f"{stage} 已完成 receipt 核验失败；请克隆新 run，原产物保留")
            self.completed += 1
            self.reused += 1
            self.stages.append({"name": stage, "status": "COMPLETED", "planned_upper": 1, "actual": 1, "completed": 1, "reused": 1, "new": 0, "invalid": 0})
            self.event("STAGE_REUSED", stage, artifacts=list(receipt["outputs"]), completed=1, reused=1)
            if stop_boundary:
                self.boundary(stage)
            return folder
        partial_path = folder / "partial.json"
        partial = self._load(partial_path)
        resume = bool(partial)
        if resume and (not mdrun or not self._valid(partial, hashes, folder) or partial.get("status") != "checkpoint"):
            raise ValueError(f"{stage} 检查点绑定或输出校验失败；不能续跑，原文件保留")
        if folder.exists() and any(folder.iterdir()) and not resume:
            raise ValueError(f"{stage} 有未提交输出且无可验证检查点；请克隆新 run，原文件保留")
        folder.mkdir(parents=True, exist_ok=True)
        if not resume:
            prepare(folder)
        planned = commands(folder)
        if resume:
            planned = [(command + ["-cpi", "state.cpt", "-append"], stdin, True)
                       for command, stdin, is_md in planned if is_md]
        self.event("STAGE_RUNNING", stage, resume_checkpoint=resume, planned_upper=1)
        try:
            for command, stdin, is_md in planned:
                self._command(command, stdin, folder, stage, is_md)
        except WorkflowStop:
            cpt = folder / "state.cpt"
            if not cpt.is_file() or cpt.stat().st_size == 0:
                self.event("STAGE_FAILED", stage, reason="GROMACS interrupted without checkpoint")
                raise RuntimeError("停止后未生成 GROMACS 检查点；不宣称可续跑")
            _atomic_json(partial_path, {"status": "checkpoint", "binding": self.binding, "inputs": hashes,
                                        "outputs": self._outputs(folder), "commands": planned,
                                        "attempt_id": os.getenv("PWB_ATTEMPT_ID")})
            self.event("CHECKPOINT_COMMITTED", stage, artifacts=list(self._outputs(folder)), boundary="GROMACS SIGINT checkpoint")
            _atomic_json(self.root / "control" / "safe_stopped.json", {"run_id": os.getenv("PWB_RUN_ID"), "attempt_id": os.getenv("PWB_ATTEMPT_ID"), "stage": stage, "boundary": "GROMACS checkpoint", "committed_at": time.time()})
            self.event("SAFE_STOP", stage, boundary="GROMACS checkpoint")
            raise
        if finalize:
            finalize(folder)
        for name in expected:
            path = folder / name
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError(f"{stage} 命令返回成功但缺少非空产物 {name}")
        outputs = self._outputs(folder)
        receipt = {"status": "completed", "binding": self.binding, "inputs": hashes, "outputs": outputs,
                   "commands": planned, "committed_at": time.time(), "attempt_id": os.getenv("PWB_ATTEMPT_ID")}
        _atomic_json(receipt_path, receipt)
        self.completed += 1
        self.stages.append({"name": stage, "status": "COMPLETED", "planned_upper": 1, "actual": 1, "completed": 1, "new": 1, "reused": 0, "invalid": 0})
        self.event("STAGE_COMMITTED", stage, artifacts=list(outputs), hashes=outputs, completed=1, new=1)
        if stop_boundary:
            self.boundary(stage)
        return folder

    def _command(self, command, stdin, folder, stage, is_md):
        self.event("COMMAND", stage, command=command)
        with (folder / "execution.log").open("a") as log:
            child = subprocess.Popen(command, cwd=folder, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True, shell=False)
            if child.stdin:
                try:
                    child.stdin.write(stdin or "")
                    child.stdin.close()
                except BrokenPipeError:
                    pass
            interrupted = False
            checkpoint_before_stop = None
            last_progress = 0.0
            while child.poll() is None:
                if time.monotonic() - last_progress >= PROGRESS_POLL_SECONDS:
                    self.native_progress(folder, stage, is_md, process_running=True)
                    last_progress = time.monotonic()
                if is_md and self.stopped() and not interrupted:
                    cpt = folder / "state.cpt"
                    if cpt.is_file():
                        checkpoint_before_stop = (_hash(cpt), cpt.stat().st_mtime_ns)
                    child.send_signal(signal.SIGINT)
                    interrupted = True
                    self.event("STOP_REQUESTED", stage, action="SIGINT to GROMACS; awaiting checkpoint")
                time.sleep(0.1)
            self.native_progress(folder, stage, is_md, process_running=False)
            if child.returncode:
                self.event("STAGE_FAILED", stage, returncode=child.returncode, log=str((folder / "execution.log").relative_to(self.root)))
                raise RuntimeError(f"{stage} 命令失败，返回码 {child.returncode}；查看本地 execution.log")
            if interrupted:
                cpt = folder / "state.cpt"
                if not cpt.is_file() or not cpt.stat().st_size or (_hash(cpt), cpt.stat().st_mtime_ns) == checkpoint_before_stop:
                    self.event("STAGE_FAILED", stage, reason="no newly written checkpoint after SIGINT")
                    raise RuntimeError("GROMACS 停止后没有新提交的非空检查点；不宣称可续跑")
                raise WorkflowStop(stage)


    def native_progress(self, folder, stage, is_md, process_running=False):
        if is_md:
            nsteps = _mdp(self.config["mdp"].get(stage)).get("nsteps")
            observed = gromacs_progress(folder / "md.log", nsteps)
            if observed:
                self.event("MD_PROGRESS", stage, **observed, process_running=process_running)
        elif stage == "bindcraft":
            self.event("BINDCRAFT_PROGRESS", stage, **bindcraft_progress(folder), process_running=process_running)


def _files(folder):
    return [p for p in sorted(Path(folder).rglob("*")) if p.is_file() and p.name not in {"receipt.json", "partial.json", "execution.log"}]


def _copy_tree(source, target):
    source, target = Path(source), Path(target)
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"输入目录不接受符号链接：{path.name}")
        if path.is_file() and path.name not in {"receipt.json", "partial.json", "execution.log"}:
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def _topology_dependencies(root, topology):
    root = Path(root).resolve()
    found = set()
    def visit(path):
        path = path.resolve()
        if not path.is_relative_to(root):
            raise ValueError("拓扑 include 不得引用快照目录之外的本地文件")
        if path in found:
            return
        if not path.is_file() or path.is_symlink():
            raise ValueError("拓扑输入不存在或为符号链接")
        found.add(path)
        for line in path.read_text(errors="replace").splitlines():
            match = re.match(r'\s*#include\s+["<]([^">]+)[">]', line)
            if not match:
                continue
            name = match.group(1)
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("拓扑 include 必须为快照内相对路径；请打包本地 include")
            included = path.parent / name
            if included.is_file():
                visit(included)
            elif ".ff/" not in name:
                raise ValueError(f"缺少本地拓扑 include：{name}")
            # Standard force-field .ff entries may be resolved by the bound gmx library.
    visit(root / topology)
    return sorted(found)


def _copy_dependencies(root, target, files):
    for source in files:
        destination = target / Path(source).relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _write_mdp(path, values):
    Path(path).write_text("\n".join(f"{key} = {' '.join(map(str, value)) if isinstance(value, list) else value}" for key, value in values.items()) + "\n")


def _index_groups(path):
    groups, current = {}, None
    for line in Path(path).read_text().splitlines():
        line = line.split(";", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"\[\s*(.+?)\s*\]", line)
        if match:
            current = match.group(1)
            if current in groups:
                raise ValueError(f"索引含重复组名：{current}")
            groups[current] = []
        elif current is None or not all(re.fullmatch(r"[1-9][0-9]*", value) for value in line.split()):
            raise ValueError("索引需包含明确组名和正整数原子编号")
        else:
            groups[current].extend(map(int, line.split()))
    return groups


def _finalize_index(folder, custom_index, pull):
    """Extend engine-generated defaults; never replace thermostat/analysis groups."""
    path = folder / "index.ndx"
    groups = _index_groups(path)
    if "System" not in groups or not groups["System"]:
        raise ValueError("GROMACS 未生成非空 System 索引组")
    system = set(groups["System"])
    additions = _index_groups(folder / "custom.ndx") if custom_index else {}
    for group in pull.get("groups", []) if pull else []:
        if group["name"] in groups or group["name"] in additions:
            raise ValueError(f"拉伸组名与现有索引冲突：{group['name']}")
        additions[group["name"]] = group["atoms"]
    with path.open("a") as file:
        for name, atoms in additions.items():
            if not atoms or not set(atoms).issubset(system):
                raise ValueError(f"索引组 {name} 包含超出最终全系统的原子编号")
            if name in groups:
                if groups[name] != atoms:
                    raise ValueError(f"自定义索引与默认组同名但原子不同：{name}")
                continue
            file.write(f"\n[ {name} ]\n" + " ".join(map(str, atoms)) + "\n")


def _md_commands(gmx, folder, stage, coordinate, topology, checkpoint, config, pulling=False):
    pre = [gmx, "grompp", "-f", "stage.mdp", "-c", coordinate, "-p", topology,
           "-o", "stage.tpr", "-po", "resolved.mdp", "-pp", "processed.top"]
    if config.get("restraint_reference"):
        pre += ["-r", "reference.gro"]
    if checkpoint:
        pre += ["-t", checkpoint]
    if (folder / "index.ndx").is_file():
        pre += ["-n", "index.ndx"]
    opts = config["mdrun"]
    md = [gmx, "mdrun", "-s", "stage.tpr", "-deffnm", stage, "-c", "final.gro", "-cpo", "state.cpt",
          "-e", "energy.edr", "-x", "trajectory.xtc", "-g", "md.log", "-ntomp", str(opts["ntomp"]),
          "-ntmpi", str(opts["ntmpi"]), "-cpt", str(opts["checkpoint_minutes"]), "-nb", opts["device"]]
    if pulling:
        md += ["-px", "pullx.xvg", "-pf", "pullf.xvg"]
    return [(pre, "", False), (md, "", stage != "minimize")]


def csv_complete_rows(path):
    try:
        content = Path(path).read_text()
    except OSError:
        return []
    # Exclude the currently written, newline-incomplete record.
    content = content[:content.rfind("\n") + 1]
    rows = []
    try:
        reader = csv.DictReader(io.StringIO(content), strict=True)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            return []
        for row in reader:
            if None not in row and all(value is not None for value in row.values()):
                rows.append(row)
    except (csv.Error, ValueError):
        pass  # Preserve already parsed records when the writer is mid quoted row.
    return rows


def bindcraft_progress(folder):
    folder = Path(folder)
    try:
        settings = json.loads((folder / "settings.json").read_text())
        advanced = json.loads((folder / "advanced.json").read_text())
        log = (folder / "execution.log").read_text(errors="replace")
    except (OSError, ValueError):
        return {"state": "native progress unavailable"}
    designs = folder / "designs"
    starts = re.findall(r"^Starting trajectory:\s*(\S+)", log, re.MULTILINE)
    trajectory_rows = csv_complete_rows(designs / "trajectory_stats.csv")
    mpnn_rows = csv_complete_rows(designs / "mpnn_design_stats.csv")
    final_rows = csv_complete_rows(designs / "final_design_stats.csv")
    accepted = len(list((designs / "Accepted").glob("*.pdb")))
    relaxed = len(list((designs / "Trajectory" / "Relaxed").glob("*.pdb")))
    stop_reason = "running_or_unknown"
    if re.search(r"Target number .* of designs reached!", log):
        stop_reason = "accepted_goal_reached"
    elif re.search(r"Target number of .* trajectories reached", log):
        stop_reason = "trajectory_budget_reached"
    elif "No GPU device found" in log:
        stop_reason = "gpu_unavailable"
    elif "The ratio of successful designs is lower than defined acceptance rate!" in log:
        stop_reason = "acceptance_rate_below_threshold"
    maximum = advanced.get("max_trajectories")
    return {"attempted_trajectories": len(starts), "attempted_source": "official Starting trajectory log lines",
            "recorded_trajectory_rows": len(trajectory_rows), "mpnn_rows": len(mpnn_rows),
            "relaxed_trajectory_files": relaxed, "trajectory_budget_basis": "official max_trajectories counts Trajectory/Relaxed PDB files, not all attempts",
            "accepted_files": accepted, "final_rows": len(final_rows), "accepted_goal": settings.get("number_of_final_designs"),
            "max_trajectories": maximum if type(maximum) is int and maximum > 0 else None,
            "max_trajectories_unbounded": maximum is False, "stop_reason": stop_reason,
            "committed_candidates": False, "counts_are_separate_denominators": True}


def gromacs_progress(path, nsteps):
    try:
        with Path(path).open("rb") as file:
            file.seek(max(0, Path(path).stat().st_size - 131072))
            content = file.read().decode(errors="replace")
    except OSError:
        return None
    matches = re.findall(r"^\s*Step\s+Time\s*\n\s*(\d+)\s+([-+0-9.eE]+)", content, re.MULTILINE)
    if not matches:
        return None
    step, moment = int(matches[-1][0]), float(matches[-1][1])
    if not math.isfinite(moment):
        return None
    return {"observed_step": step, "observed_time_ps": moment, "configured_nsteps": nsteps,
            "source": "GROMACS md.log Step/Time block", "stage_complete": False,
            "note": "absolute observed step; restart/init-step may differ from stage elapsed steps"}


RESIDUE_CODE = dict(zip("ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL".split(), "ARNDCQEGHILKMFPSTWYV"))


def pdb_chain_sequences(path):
    chains, seen = {}, set()
    for line in Path(path).read_text(errors="replace").splitlines():
        if line[:6].strip() != "ATOM":
            continue
        chain, residue = line[21:22], line[22:27]
        key = (chain, residue)
        if key not in seen:
            seen.add(key)
            chains.setdefault(chain, []).append(RESIDUE_CODE.get(line[17:20].strip(), "X"))
    return {key: "".join(value) for key, value in chains.items()}


def inspect_pdb_output(path):
    atoms = 0
    chains = set()
    for line in Path(path).read_text(errors="replace").splitlines():
        if line[:6].strip() not in {"ATOM", "HETATM"}:
            continue
        try:
            coordinates = [float(line[start:start + 8]) for start in (30, 38, 46)]
        except ValueError as exc:
            raise ValueError("候选 PDB 原子坐标无法解析") from exc
        if any(not math.isfinite(x) for x in coordinates):
            raise ValueError("候选 PDB 坐标必须有限")
        atoms += 1
        chains.add(line[21:22].strip())
    if not atoms:
        raise ValueError("候选 PDB 没有可解析的原子坐标")
    return {"atom_count": atoms, "chains": sorted(chains), "validation": "coordinate syntax only"}


def summarize_xvg(path):
    headers, rows = [], []
    for line in Path(path).read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("#", "@")):
            headers.append(line)
            continue
        if line == "&":
            raise ValueError("XVG 多 dataset 不能按单矩阵自动汇总")
        try:
            row = [float(x) for x in line.split()]
        except ValueError as exc:
            raise ValueError("XVG 含非数值数据") from exc
        if len(row) < 2 or any(not math.isfinite(x) for x in row):
            raise ValueError("XVG 数据需至少时间和值两列且全部有限")
        if rows and len(row) != len(rows[0]):
            raise ValueError("XVG 列数不一致")
        rows.append(row)
    if not rows:
        raise ValueError("XVG 没有可汇总数据")
    columns = []
    for index in range(1, len(rows[0])):
        values = [row[index] for row in rows]
        columns.append({"column": index, "mean": statistics.fmean(values), "std_population": statistics.pstdev(values),
                        "minimum": min(values), "maximum": max(values), "count": len(values)})
    return {"file": Path(path).name, "headers": headers, "time_min": min(row[0] for row in rows),
            "time_max": max(row[0] for row in rows), "rows": len(rows), "columns": columns,
            "units": "read original XVG headers; no unit conversion performed"}


def _execute_md(runner):
    config, env, gmx = runner.config, runner.env, runner.env["gmx"]
    prep_mode = config["input_mode"]
    if prep_mode == "protein_pdb":
        source = Path(config["pdb"])
        if source.suffix.lower() in {".cif", ".mmcif"}:
            conversion = (
                "from Bio.PDB import MMCIFParser, PDBIO; "
                "import sys; "
                "s=MMCIFParser(QUIET=False).get_structure('input', sys.argv[1]); "
                "w=PDBIO(); w.set_structure(s); w.save(sys.argv[2])"
            )
            converted = runner.run("cif_to_pdb", [source], lambda d: shutil.copy2(source, d / "input.cif"),
                                   lambda d: [([env["python"], "-c", conversion, "input.cif", "input.pdb"], "", False)], ["input.pdb"])
            source = converted / "input.pdb"
            bad = _pdb_special(source)
            if bad:
                raise ValueError("CIF 转换保留了特殊组分，简单模板不予参数化：" + ", ".join(bad))
        proton = config["protonation"]
        previous = runner.run("pdb2gmx", [source], lambda d: shutil.copy2(source, d / "input.pdb"),
                              lambda d: [([gmx, "pdb2gmx", "-f", "input.pdb", "-o", "system.gro", "-p", "topol.top", "-i", "posre.itp",
                                           "-ff", config["forcefield"], "-water", config["water_model"], "-chainsep", config["chainsep"], "-merge", config["merge"]] + proton["flags"], proton["stdin"], False)],
                              ["system.gro", "topol.top"])
        box = config["box"]
        previous = runner.run("box", _files(previous), lambda d: _copy_tree(previous, d),
                              lambda d: [([gmx, "editconf", "-f", "system.gro", "-o", "boxed.gro", "-c", "-d", str(box["distance_nm"]), "-bt", box["type"]], "", False)], ["boxed.gro", "topol.top"])
        solvent = config["solvent_coordinates"]
        sources = _files(previous) + ([Path(solvent)] if Path(solvent).is_absolute() else [])
        def solvate_prepare(d):
            _copy_tree(previous, d)
            if Path(solvent).is_absolute():
                shutil.copy2(solvent, d / "solvent.gro")
        previous = runner.run("solvate", sources, solvate_prepare,
                              lambda d: [([gmx, "solvate", "-cp", "boxed.gro", "-cs", "solvent.gro" if Path(solvent).is_absolute() else solvent, "-o", "solvated.gro", "-p", "topol.top"], "", False)], ["solvated.gro", "topol.top"])
        ions = config["ions"]
        def ions_prepare(d):
            _copy_tree(previous, d)
            _write_mdp(d / "stage.mdp", _mdp(config["mdp"]["ions"]))
        def ions_commands(d):
            command = [gmx, "genion", "-s", "ions.tpr", "-o", "ions.gro", "-p", "topol.top", "-pname", ions["positive"], "-nname", ions["negative"], "-pq", str(ions["positive_charge"]), "-nq", str(ions["negative_charge"]), "-rmin", str(ions["minimum_distance_nm"]), "-conc", str(ions["concentration_molar"]), "-seed", str(ions["seed"])]
            if ions["neutralize"]:
                command += ["-neutral"]
            return [([gmx, "grompp", "-f", "stage.mdp", "-c", "solvated.gro", "-p", "topol.top", "-o", "ions.tpr"], "", False),
                    (command, ions["solvent_group"] + "\n", False)]
        previous = runner.run("ions", _files(previous), ions_prepare, ions_commands, ["ions.gro", "topol.top"])
        coordinate, topology, checkpoint = "ions.gro", "topol.top", None
        stages = MD_STAGES
    else:
        source = Path(config["prepared_root"])
        previous = runner.run("prepared_input", _files(source), lambda d: _copy_tree(source, d), lambda d: [], [config["coordinate"], config["topology"]])
        coordinate, topology, checkpoint = config["coordinate"], config["topology"], config.get("checkpoint")
        stages = MD_STAGES[MD_STAGES.index(config["start_stage"]):]
    for stage in stages:
        pulling = "pull" in runner.workflow and stage == "production"
        custom_index = config.get("index") if prep_mode == "prepared" else None
        stage_index = None
        if pulling or custom_index:
            index_inputs = [previous / coordinate]
            if custom_index:
                index_inputs.append(runner.stage_root / "prepared_input" / custom_index)
            def index_prepare(d):
                shutil.copy2(previous / coordinate, d / "input.gro")
                if custom_index:
                    shutil.copy2(runner.stage_root / "prepared_input" / custom_index, d / "custom.ndx")
            stage_index = runner.run(stage + "_index", index_inputs, index_prepare,
                                     lambda d: [([gmx, "make_ndx", "-f", "input.gro", "-o", "index.ndx"], "q\n", False)],
                                     ["index.ndx"], finalize=lambda d: _finalize_index(d, custom_index, config["pull"] if pulling else None))
        topology_files = _topology_dependencies(previous, topology)
        stage_inputs = topology_files + [previous / coordinate] + ([previous / checkpoint] if checkpoint else []) + ([stage_index / "index.ndx"] if stage_index else [])
        if config.get("restraint_reference"):
            stage_inputs.append(Path(config["restraint_reference"]))
        def prepare(d):
            _copy_dependencies(previous, d, topology_files)
            shutil.copy2(previous / coordinate, d / "input.gro")
            if checkpoint:
                shutil.copy2(previous / checkpoint, d / "input.cpt")
            if config.get("restraint_reference"):
                shutil.copy2(config["restraint_reference"], d / "reference.gro")
            values = _mdp(config["mdp"][stage])
            if pulling:
                values.update(pull_parameters(runner.workflow, config["pull"]))
            if stage_index:
                shutil.copy2(stage_index / "index.ndx", d / "index.ndx")
            _write_mdp(d / "stage.mdp", values)
        previous = runner.run(stage, stage_inputs, prepare,
                              lambda d: _md_commands(gmx, d, stage, "input.gro", topology, "input.cpt" if checkpoint else None, config, pulling),
                              ["stage.tpr", "final.gro", "energy.edr"] + (["state.cpt", "trajectory.xtc"] if stage != "minimize" else []) + (["pullx.xvg", "pullf.xvg"] if pulling else []), mdrun=True)
        coordinate, checkpoint = "final.gro", "state.cpt" if (previous / "state.cpt").is_file() else None
    production = previous
    analysis = config["analysis"]
    def analysis_commands(d):
        index_flags = ["-n", str(production / "index.ndx")] if (production / "index.ndx").is_file() else []
        return [([gmx, "energy", "-f", str(production / "energy.edr"), "-o", "energy.xvg"], "\n".join(analysis["energy_terms"]) + "\n0\n", False),
                ([gmx, "rms", "-s", str(production / "stage.tpr"), "-f", str(production / "trajectory.xtc"), "-o", "rmsd.xvg", "-tu", "ps"] + index_flags, analysis["fit_group"] + "\n" + analysis["rms_group"] + "\n", False),
                ([gmx, "trjconv", "-s", str(production / "stage.tpr"), "-f", str(production / "trajectory.xtc"), "-o", "representative.pdb", "-dump", str(analysis["frame_time_ps"])] + index_flags, analysis["frame_group"] + "\n", False)]
    analysis_inputs = [production / name for name in ("stage.tpr", "energy.edr", "trajectory.xtc")]
    if (production / "index.ndx").is_file():
        analysis_inputs.append(production / "index.ndx")
    for name in ("pullx.xvg", "pullf.xvg"):
        if (production / name).is_file():
            analysis_inputs.append(production / name)
    def finalize(d):
        summary = {"interpretation": "scalar summaries only; no equilibrium, binding, or force-state claim", "requested_frame_time_ps": analysis["frame_time_ps"],
                   "energy": summarize_xvg(d / "energy.xvg"), "rmsd": summarize_xvg(d / "rmsd.xvg"),
                   "structure": inspect_pdb_output(d / "representative.pdb")}
        for name in ("pullx.xvg", "pullf.xvg"):
            if (production / name).is_file():
                summary[name] = summarize_xvg(production / name)
        _atomic_json(d / "analysis-summary.json", summary)
    result = runner.run("analysis", analysis_inputs, lambda d: None,
                        analysis_commands, ["energy.xvg", "rmsd.xvg", "representative.pdb", "analysis-summary.json"], finalize=finalize)
    structural = inspect_pdb_output(result / "representative.pdb")
    return [{"id": "representative_frame", "structure": str((result / "representative.pdb").relative_to(runner.root)),
             "valid_output": True, "metrics": {"requested_frame_time_ps": analysis["frame_time_ps"], **structural},
             "interpretation": "trajectory frame selected near requested time; no claim of equilibrium or force-state selectivity"}]


def _execute_bindcraft(runner):
    config, env = runner.config, runner.env
    settings = json.loads(Path(config["settings"]).read_text())
    inputs = [Path(config[k]) for k in ("settings", "filters", "advanced")] + [Path(settings["starting_pdb"]), Path(env["bindcraft_root"]) / "bindcraft.py"]
    # The official pipeline writes design_path. Redirect only in a private copy.
    def prepare(d):
        _atomic_json(d / "input-binding.json", {"binding": runner.binding, "inputs": runner._inputs(inputs)})
        copied = dict(settings)
        shutil.copy2(settings["starting_pdb"], d / "target.pdb")
        copied["starting_pdb"] = str(d / "target.pdb")
        copied["design_path"] = str(d / "designs")
        _atomic_json(d / "settings.json", copied)
        for key in ("filters", "advanced"):
            shutil.copy2(config[key], d / f"{key}.json")
        # Some official advanced files use paths relative to the BindCraft root.
        # Resolve only known path entries in a copy; all originals are immutable.
        advanced = json.loads((d / "advanced.json").read_text())
        for key in ("dalphaball_path", "af_params_dir", "dssp_path"):
            value = advanced.get(key)
            if isinstance(value, str) and value and not Path(value).is_absolute():
                advanced[key] = str((Path(env["bindcraft_root"]) / value).resolve())
        _atomic_json(d / "advanced.json", advanced)
    folder = runner.run("bindcraft", inputs, prepare,
                        lambda d: [(bindcraft_command(env, d / "settings.json", d / "filters.json", d / "advanced.json"), "", False)],
                        ["designs/final_design_stats.csv", "native-completion.json", "collected-candidates.json"],
                        stop_boundary=False, finalize=lambda d: _collect_bindcraft(d, runner))
    if runner.stopped():
        runner.event("STOP_SATISFIED", "bindcraft", boundary="entire official script completed and candidates collected")
    return json.loads((folder / "collected-candidates.json").read_text())


def _native_file(path, root):
    path, root = Path(path), Path(root).resolve()
    if not path.resolve().is_relative_to(root) or any(part.is_symlink() for part in [path, *path.parents] if part != root and part.is_relative_to(root)):
        raise ValueError("原生产物路径越界或包含符号链接")
    if not path.is_file():
        raise ValueError("原生产物文件不存在")
    return path


def _verified_bindcraft_candidates(folder, runner):
    candidates = []
    csv_path = folder / "designs" / "final_design_stats.csv"
    if not csv_path.is_file():
        return candidates
    _native_file(csv_path, runner.root)
    csv_hash = _hash(csv_path)
    rows, duplicate = {}, set()
    for row in csv_complete_rows(csv_path):
        name = row.get("Design")
        if name in rows:
            duplicate.add(name)
        if name:
            rows[name] = row
    for path in sorted((folder / "designs" / "Accepted").glob("*.pdb")):
        design = path.stem.rsplit("_model", 1)[0]
        row = rows.get(design)
        if row is None or design in duplicate:
            runner.event("CANDIDATE_INVALID", "bindcraft", candidate=path.stem, reason="Accepted structure has no matching official final CSV Design row")
            continue
        try:
            _native_file(path, runner.root)
            structure_hash = _hash(path)
            structural = inspect_pdb_output(path)
            sequence = row.get("Sequence", "")
            if not sequence or not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+", sequence):
                raise ValueError("official CSV Sequence missing or not canonical protein sequence")
            matched = [chain for chain, observed in pdb_chain_sequences(path).items() if chain == "B" and observed == sequence]
            if not matched:
                raise ValueError("official CSV sequence does not match BindCraft binder chain B in accepted structure")
            numeric = {}
            for key, value in row.items():
                if value and key not in {"Sequence", "Design", "InterfaceResidues", "InterfaceAAs", "Target_Hotspot"}:
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(number):
                        raise ValueError(f"official CSV numeric metric {key} is nonfinite")
                    numeric[key] = number
            candidate = {"id": path.stem, "structure": str(path.relative_to(runner.root)), "sequence": sequence,
                         "structure_sha256": structure_hash, "native_csv": str(csv_path.relative_to(runner.root)), "native_csv_sha256": csv_hash,
                         "native_row_sha256": _digest(row),
                         "valid_output": True, "metrics": {**structural, **numeric, "sequence_matching_chains": matched},
                         "native_row": row, "native_design": design,
                         "interpretation": "BindCraft native accepted output and sequence verified; predictive scores do not establish binding"}
            if "Average_i_pTM" in numeric:
                candidate["ranking_score"] = numeric["Average_i_pTM"]
                candidate["ranking_basis"] = "official Average_i_pTM; predictive score, not affinity"
            if _hash(_native_file(path, runner.root)) != structure_hash:
                raise ValueError("候选结构在核验期间发生变化")
            candidates.append(candidate)
        except (ValueError, OSError) as exc:
            runner.event("CANDIDATE_INVALID", "bindcraft", candidate=path.stem, reason=str(exc))
    if _hash(_native_file(csv_path, runner.root)) != csv_hash:
        raise ValueError("官方 CSV 在候选核验期间发生变化；本次不提交候选")
    return candidates


def _collect_bindcraft(folder, runner):
    candidates = _verified_bindcraft_candidates(folder, runner)
    native = bindcraft_progress(folder)
    if native.get("stop_reason") == "running_or_unknown":
        native["stop_reason"] = "official_process_completed_reason_unreported"
    _atomic_json(folder / "native-completion.json", {**native, "validated_candidates": len(candidates)})
    runner.event("BINDCRAFT_COMPLETE", "bindcraft", **native, validated_candidates=len(candidates))
    _atomic_json(folder / "collected-candidates.json", candidates)
    return candidates


def collect_partial_candidates(workflow, config, env, root, emit):
    """Recover verified Accepted files without completing any stage/run.

    Call after the engine has exited. Binding/collector errors raise separately;
    the caller must preserve the original execution error and failure status.
    CANDIDATE events are repeat-safe; returned candidates may be passed through
    the Store's normal hash/conflict checking even after a supervisor restart.
    """
    empty = {"candidates": [], "new_candidates": [], "artifacts": [], "errors": [], "stage_complete": False}
    root = Path(root).resolve()
    folder = root / "workflow" / "bindcraft"
    if workflow != "bindcraft" or not folder.is_dir():
        return empty
    errors = validate(workflow, config, env)
    if errors:
        raise ValueError("部分候选输入绑定检查失败：" + "; ".join(errors))
    native_names = ("settings.json", "filters.json", "advanced.json", "target.pdb")
    native_hashes = {name: _hash(_native_file(folder / name, root)) for name in native_names}
    original = json.loads(Path(config["settings"]).read_text())
    expected = dict(original, starting_pdb=str(folder / "target.pdb"), design_path=str(folder / "designs"))
    advanced = json.loads(Path(config["advanced"]).read_text())
    for name in ("dalphaball_path", "af_params_dir", "dssp_path"):
        value = advanced.get(name)
        if isinstance(value, str) and value and not Path(value).is_absolute():
            advanced[name] = str((Path(env["bindcraft_root"]) / value).resolve())
    if (json.loads((folder / "settings.json").read_text()) != expected or
        json.loads((folder / "advanced.json").read_text()) != advanced or
        native_hashes["filters.json"] != _hash(config["filters"]) or
        native_hashes["target.pdb"] != _hash(original["starting_pdb"])):
        raise ValueError("部分候选原生 settings/filters/advanced/target 与冻结输入绑定不一致")
    invalid = []
    def collector_emit(event, **kwargs):
        if event == "CANDIDATE_INVALID":
            invalid.append(kwargs["data"])
        emit(event, **kwargs)
    runner = StageRunner(workflow, config, env, root, collector_emit)
    binding_file = _native_file(folder / "input-binding.json", root)
    binding = json.loads(binding_file.read_text())
    source_inputs = [Path(config[key]) for key in ("settings", "filters", "advanced")] + [Path(original["starting_pdb"]), Path(env["bindcraft_root"]) / "bindcraft.py"]
    if binding.get("binding") != runner.binding or binding.get("inputs") != runner._inputs(source_inputs):
        raise ValueError("部分候选启动时的输入/官方入口绑定校验失败")
    candidates = _verified_bindcraft_candidates(folder, runner)
    if any(_hash(_native_file(folder / name, root)) != digest for name, digest in native_hashes.items()):
        raise ValueError("部分候选输入绑定在读取期间变化")
    manifest_path = root / "partial-candidates.json"
    previous = {}
    if manifest_path.exists():
        _native_file(manifest_path, root)
        previous = json.loads(manifest_path.read_text())
        if previous.get("binding") != runner.binding:
            raise ValueError("部分候选收据绑定发生变化")
    committed = dict(previous.get("committed", {}))
    new = []
    for candidate in candidates:
        candidate["collection_kind"] = "partial_after_engine_exit"
        identity = candidate["id"]
        digest = _digest({key: candidate[key] for key in ("structure_sha256", "sequence", "native_row_sha256")})
        if identity in committed:
            if committed[identity] != digest:
                raise ValueError("部分候选身份或结构散列与已提交记录冲突")
            continue
        emit("CANDIDATE", stage="bindcraft", key=identity, data={"candidate": candidate, "partial_collection": True})
        committed[identity] = digest
        new.append(candidate)
        # Commit after emission: a crash before this write may replay the same
        # event, which Store.commit_candidate deduplicates by identity/hash.
        _atomic_json(manifest_path, {"binding": runner.binding, "committed": committed, "stage_complete": False,
                                    "input_hashes": native_hashes, "committed_at": time.time()})
    return {"candidates": candidates, "new_candidates": new,
            "artifacts": [str(manifest_path.relative_to(root))] if manifest_path.is_file() else [],
            "errors": invalid, "stage_complete": False}


def execute(workflow, config, env, output_dir, emit: Callable):
    errors = validate(workflow, config, env)
    if errors:
        raise ValueError("; ".join(errors))
    runner = StageRunner(workflow, config, env, output_dir, emit)
    if workflow == "bindcraft":
        planned = ["bindcraft"]
    elif workflow.startswith("gromacs_"):
        planned = (["pdb2gmx", "box", "solvate", "ions"] + list(MD_STAGES) if config["input_mode"] == "protein_pdb" else
                   ["prepared_input"] + list(MD_STAGES[MD_STAGES.index(config["start_stage"]):])) + ["analysis"]
    else:
        raise ValueError("generic/SR56 execution belongs to worker")
    if config.get("input_mode") == "protein_pdb" and Path(config["pdb"]).suffix.lower() in {".cif", ".mmcif"}:
        planned.insert(0, "cif_to_pdb")
    for stage in list(planned):
        if stage in MD_STAGES and (config.get("index") or ("pull" in workflow and stage == "production")):
            planned.insert(planned.index(stage), stage + "_index")
    for stage in planned:
        runner.event("STAGE_PLANNED", stage, planned_upper=1, actual=1)
    candidates = []
    status = "COMPLETED"
    try:
        candidates = _execute_bindcraft(runner) if workflow == "bindcraft" else _execute_md(runner)
        if workflow == "bindcraft":
            runner.boundary("bindcraft")
    except WorkflowStop:
        status = "STOPPED"
    progress = {"stages": runner.stages, "planned_upper": len(planned), "actual": len(planned),
                "completed": runner.completed, "new": runner.completed - runner.reused, "reused": runner.reused,
                "invalid": runner.invalid, "models": len(candidates)}
    artifacts = [str(p.relative_to(runner.root)) for p in runner.stage_root.rglob("receipt.json")]
    result = {"status": status, "artifacts": artifacts, "candidates": candidates, "progress": progress,
              "validation": "process and receipt evidence only; GPU/scientific acceptance not inferred"}
    _atomic_json(runner.root / "workflow-result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["execute"])
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    request = json.loads(Path(args.request).read_text())
    def emit(event, **kwargs):
        print("PWB_EVENT " + json.dumps({"event": event, **kwargs}, ensure_ascii=False), flush=True)
    try:
        result = execute(request["workflow"], request["config"], request["env"], request["output_dir"], emit)
        print("PWB_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        emit("FAILED", stage="workflow", key="workflow", data={"type": type(exc).__name__, "message": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
