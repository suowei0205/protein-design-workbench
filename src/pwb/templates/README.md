# Workflow templates

这些配置模板刻意以 `null` 标出必须由研究者决定的参数，不能直接用于科研计算。模板中没有猜测温度、压力、时间步长、力场、质子化、端基、离子浓度或拉伸载荷。填写后仍需科学复核；程序运行和文件哈希不等于平衡、结合、力响应或机制成立。

所选 environment 必须显式绑定 Python 解释器绝对路径；GROMACS 绑定 `gmx` 绝对路径；BindCraft 绑定含官方 `bindcraft.py` 的根目录。不安装依赖、不下载模型、不自动启动 GPU。

`generic_script.py` 和 `generic_notebook.ipynb` 是实际可运行的本地空白起点，执行时只报告 starter 状态。通用脚本和 Notebook 没有内部恢复能力，只有已桥接 SR56 和 GROMACS 接收可验证边界的续跑。

## BindCraft

填写目标 settings、filters、advanced 的绝对 JSON 路径。settings 内 `starting_pdb` 同样必须绝对定位。官方命令形态为绑定解释器执行根目录 `bindcraft.py --settings ... --filters ... --advanced ...`。工作台把 settings 和输入 PDB 复制到运行目录，只在副本中定向 `design_path`，保留原文件。筛选通过的预测文件只是候选；不是实验结合证据。

停止请求只能在整个官方脚本返回成功并提交 receipt 后兑现，不能保证当前内部轨迹可暂停或恢复。强停后不宣称支持内部续跑，请创建新的 run 并保留原输出。

运行期间读取官方 `Starting trajectory:` 日志与完整 CSV 行，分别报告尝试次数、轨迹统计行数、MPNN 统计行数、Accepted 文件数及接受目标。官方 `max_trajectories` 实际按 `Trajectory/Relaxed` 内 PDB 数量判断，不代表全部尝试次数上限；因此显示独立分母。停止原因只来自实际原生输出，区分目标达成、轨迹预算、接受率阈值和未报告原因。完整结束后，Accepted 文件必须匹配 `final_design_stats.csv` 的 Design，有限坐标与官方 binder 链 B 的完整序列均须通过核验，才收集为候选；原生数值指标与完整原始 CSV 行保留。目标链恰好匹配序列不能代替 binder 链核验。

## GROMACS：protein_pdb

只接受明确声明的普通蛋白水溶液与 `special_features: []`。含膜、配体、金属、修饰或未识别残基的 PDB 会被拒绝并原样保留。可使用 `.cif/.mmcif` 结构作为输入；此时先调用所选 Python 环境内的 Bio.PDB 实际转换为 PDB（缺失依赖或 PDB 不支持的链编号会失败，不自动安装或猜测映射），保留原 CIF，并重新检查转换后的特殊组分。流程为 `pdb2gmx → box → solvate → ions → minimize → nvt → npt → production → analysis`。

必须填写力场、水模型、水盒坐标、chainsep、merge、质子化与端基选择。`protonation.flags` 允许 `-his/-lys/-arg/-asp/-glu/-gln/-ter/-ss/-ignh`，`stdin` 是已按所选版本和力场菜单核对的精确选择序列，`decisions` 写清残基与端基的选择依据。若明确接受 GROMACS 的自动选择，应显式填 `flags: []`、`stdin: ""`，并在 decisions 中说明。端基和力场支持应人工核对，例如 AMBER 的端基机制与 `-ter` 不兼容。工作台不以一个 pH 数值推断所有质子化状态。

每个 MDP 阶段提供完整参数对象。时间单位为 ps，距离为 nm，温度 K，压力 bar（GROMACS 原生单位）。NVT 强制 `pcoupl=no`，NPT 明确压力耦合；NPT/production 不重新生成速度。动力学阶段需正值 `nstlog`、`nstenergy` 与 `nstxout-compressed`，确保原生步数、能量与轨迹可观察。离子配置须明确正负离子名称、电荷、`minimum_distance_nm`、添加浓度、是否中和、溶剂组和随机种子；seed=0 的含义是让 GROMACS 自动生成，不能作为可复现固定种子。GROMACS 的预处理 warning 直接失败，不传递 `-maxwarn`。

## GROMACS：prepared

`prepared_root` 含 gro、top 和全部本地 include，coordinate/topology/checkpoint 使用该目录内相对文件名。目录全部复制且拒绝符号链接。start_stage 选择 minimize/nvt/npt/production，提供从该阶段开始的完整 MDP。上游 cpt 在阶段转换通过 `grompp -t` 传递坐标与速度；新阶段使用新 TPR，此过程不宣称完整 thermostat/barostat 状态或同一系综连续性。需要精确保持完整系综状态的专项方案，应人工准备并核对相应 GROMACS 协议；它不同于本适配器支持的同一 TPR 内中断续跑。

使用位置约束时，`restraint_reference` 指向已人工核对的固定全系统参考坐标绝对路径；常规 `define=-DPOSRES` 配置必须填写。该文件独立纳入每个阶段输入哈希，并通过 `grompp -r reference.gro` 固定传递。不会悄悄用每阶段新坐标重新定义参考位置。自定义拓扑中的其他约束宏也须按实际协议提供该文件并人工审查预处理拓扑。

特殊系统可使用 `system_kind: "prepared_custom"`，保留全部特殊组分、提供 `special_features` 说明及 `prepared_verified: true` 的人工参数复核。此路径不会为配体、膜、金属或修饰自动生成参数。缺失 include 失败，不自动移除组分。

可用 `index` 指定 prepared_root 内自定义 ndx 文件。各使用该文件的阶段先通过绑定 GROMACS 生成默认索引，再合并明确的自定义组，保留 System/Backbone 等默认组。同名组原子必须完全一致；最终全系统范围之外的编号拒绝运行。生产索引也传给 RMSD 与代表帧分析，避免自定义组在分析阶段丢失。

## 拉伸

两个 groups 明确包含最终已准备全系统的 1-based 原子编号和组内 PBC 参考原子；组不重叠。蛋白 PDB 原始编号不能直接充当 pdb2gmx 后编号，必须在 group_numbering_basis 解释如何映射并验证最终编号。geometry 仅接受 distance/direction，dimensions 三个 Y/N，vector 三个分量。direction 要求非零向量且各非零分量启用。protocol_rationale 说明控制、方向与物理假设。

生产拉伸之前独立提交 `production_index` 阶段，以 `gmx make_ndx` 从最终坐标生成默认全系统索引，再追加两组，保留温控及分析需要的默认组。拉伸组名称不能覆盖已有组，编号不能超出默认 System 的实际范围。

恒速：umbrella，rate_nm_per_ps，spring_kj_mol_nm2，initial_nm 和 start_from_initial 必填。恒力：constant-force，force_kj_mol_nm 填有符号力，适配器遵循官方线性势定义，写 `pull-coord1-k = -force`；不写 rate/init 或弹簧参数。1 kJ mol^-1 nm^-1 约为 1.66054 pN，但模板不自动猜测或转换用户单位。两类工作流都输出 pullx.xvg 与 pullf.xvg。

## 停止与恢复

`output/control/stop.json` 是停止请求。普通阶段完成、非空产物存在、配置/环境/输入/输出哈希提交原子 receipt 后才响应边界停止。GROMACS 动力学 mdrun 期间发送一次 SIGINT 并等待进程正常返回；必须有停止请求后新写入的非空 cpt 才提交原子 partial receipt。非零返回或仅残留旧 cpt 都是失败。能量最小化等待完整阶段提交后停止，不承诺其内部 checkpoint 恢复。不把提前退出算阶段完成。BindCraft 在完整官方脚本结束、候选收集与阶段 receipt 提交后，若存在停止请求，则返回 STOPPED 并保留已收集候选。

运行期间每 15 秒从 `md.log` 的 Step/Time 块报告观察到的绝对步数和 ps 时间，独立记录运行中的检查点与阶段完成状态。实际步数包含 init-step/重启的影响，不直接当作该阶段已完成的步数比例；阶段完成只由最新有效 receipt 证明。

恢复已完成阶段需重新核对所有绑定和产物哈希。恢复 partial 还需同一 TPR、检查点、输出集合和环境通过哈希核验，然后使用相同输出命名的 `mdrun -cpi state.cpt -append`；GROMACS 再校验其 checkpoint 内记录的输出 checksum。校验失败保留原文件并要求克隆新 run，不自动切换 `-noappend`、改写参数或重跑。强停没有受支持 receipt 时不承诺续跑。

分析包括显式选择的 energy terms、RMSD 与请求时间附近的 representative.pdb。请求时间与实际最近帧可能不同；这不是构象聚类代表或平衡证明。

原始证据：

- [BindCraft 官方脚本](https://github.com/martinpacesa/BindCraft/blob/main/bindcraft.py)
- [BindCraft 原生计数、停止与最终统计规则](https://github.com/martinpacesa/BindCraft/blob/main/functions/generic_utils.py)
- [GROMACS make_ndx 默认组和原子编号](https://manual.gromacs.org/current/onlinehelp/gmx-make_ndx.html)
- [GROMACS genion 电荷、间距和 seed](https://manual.gromacs.org/current/onlinehelp/gmx-genion.html)
- [GROMACS mdrun 输出和 append 参数](https://manual.gromacs.org/current/onlinehelp/gmx-mdrun.html)
- [GROMACS grompp 位置约束参考及 -t 的状态边界](https://manual.gromacs.org/current/onlinehelp/gmx-grompp.html)
- [GROMACS pdb2gmx](https://manual.gromacs.org/current/onlinehelp/gmx-pdb2gmx.html)
- [GROMACS MDP 与 pull 单位、恒力符号](https://manual.gromacs.org/current/user-guide/mdp-options.html)
- [GROMACS checkpoint 与 append 规则](https://manual.gromacs.org/current/user-guide/managing-simulations.html)
