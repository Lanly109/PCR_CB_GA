# PCR 会战 UB 优化

[![License: AGPL-3.0-only](https://img.shields.io/badge/License-AGPL--3.0--only-blue)](LICENSE)

基于遗传算法（GA）的《公主连结》（Princess Connect Re:Dive）公会战UB序列外置优化工具。

[算法文档](https://docs.qq.com/doc/DU0tvaVZwY3Jma0ZH)

## 用 uv 运行

需要 Python 3.10+。在项目目录执行以下命令。

```sh
uv python install 3.10
uv sync --locked
uv run pcr-ub-ga
```

启动后按菜单选择 `.xlsx` 或输入路径，再选择参数配置。菜单也支持继续运行和从头重跑。

<details>
<summary>直接用命令运行</summary>

```sh
uv run pcr-ub-ga --excel-dir ./inputs --config-dir ./configs --runs ./runs
uv run pcr-ub-ga new ./inputs/轴.xlsx --config ./configs/快速试跑.json
uv run pcr-ub-ga status ./runs/运行目录
uv run pcr-ub-ga resume ./runs/运行目录
uv run pcr-ub-ga restart ./runs/运行目录
```

</details>

## 配置

支持菜单设置、加载 JSON 或使用默认配置。JSON 只需填写要修改的字段。

- `endpoint` 是 MZQ 服务地址。
- `battle_time` 是战斗时长，单位为秒，默认 90，可用于设置短轴。
- `battle_seed` 是战斗随机种子，默认读取 Excel 的世界线设置。
- `evaluation_repeats` 是每个候选的模拟次数，默认 5。共用一组不同的固定种子，伤害和早胜奖励分别取平均。
- `ga.population` 是种群大小，默认 50。
- `ga.generations` 是进化代数，默认 800，不含初始种群评估。
- `ga.islands` 是分组搜索的岛屿数，默认 3。
- `ga.seed` 控制 GA 的随机抽样，默认 42。
- `ga.fingerprint_distance` 是指纹去重距离，单位为帧，默认 1.0，填 `null` 关闭。缺少指纹的候选按普通排名处理。
- `fixed_ub_counts` 固定五名角色各自开头的 UB 次数，默认全为 0。
- `batch_size` 是每批请求数，默认 16。
- `concurrency` 是并行批数，默认 2。
- `timeout` 是请求超时时间，单位为秒，默认 120。

<details>
<summary>快速试跑的 JSON 示例</summary>

```json
{
  "endpoint": "http://127.0.0.1:8080/simulate",
  "battle_time": 90,
  "evaluation_repeats": 5,
  "semantic": {"ub_type": 0, "skill_type": 6, "sequence_type": 0},
  "ga": {"population": 12, "islands": 3, "generations": 3, "seed": 42, "fingerprint_distance": 1.0},
  "fixed_ub_counts": [0, 0, 0, 0, 0]
}
```

</details>

<details>
<summary>语义编码与进阶配置</summary>

- `semantic.ub_type` 的 0/1 对应 UB 条件的 1/4 系列，默认 0。
- `semantic.skill_type` 的 0–7 对应帧、1、2、3、5、10、20、30 系列，默认 6。
- `semantic.sequence_type` 的 0/1 对应按角色UB序列/全局UB序列导出，默认 0。
- `extra_genes` 为五名角色分别补充候选基因。
- `legacy_ub` 控制旧 GA 的 UB 候选值，默认在 1 系列 UB、20 系列技能组合下启用。
- `own_skill`、`other_skill` 分别添加自身、其他角色的技能候选，默认关闭。
- `delay` 添加 1 到该值的延迟候选，默认 0。

</details>

## 保存和恢复

每次运行使用独立目录，保留输入 Excel、配置和检查点。

- 初始代及每次全局最优刷新后，都会重放校验并保存新的 Excel，文件名含平均伤害和时间。
- 完成后，`optimized.xlsx` 保存最后一份最佳快照，`result.json` 保存结果。Excel 中可查看均值和逐场明细。
- 按 Ctrl+C 中断后用 `resume` 继续。未提交的代会重跑，已提交检查点对应的 Excel 会补存。
- `restart` 使用原 Excel 和配置创建新目录，从头优化。

旧配置若缺少 `evaluation_repeats`，`resume` 沿用单次评分，`restart` 默认启用 5 次评分。

## 验证

```sh
uv run --locked python -m compileall -q src
uv build
```

## 许可证

Copyright (C) 2026 Lanly109.

本项目采用 `AGPL-3.0-only`，详见 [LICENSE](LICENSE)。
