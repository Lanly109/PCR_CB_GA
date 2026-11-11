"""Interactive Excel optimizer with explicit resume and restart commands."""

import argparse
import asyncio
from dataclasses import replace
from enum import IntEnum
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from .config import DEFAULT_ENDPOINT, RunConfig
from .mzq import MzqError
from .run import MANIFEST, STATUS, create_run, execute_run, read_json, restart_run
from .semantics import SemanticConversionProfile
from .workbook import read_timeline


def _path(value: str) -> Path:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return Path(value).expanduser().resolve()


def _ask(label: str, default: str | int | float) -> str:
    return input(f"{label} [{default}] ").strip() or str(default)


def _number(label: str, default: int, low: int, high: int) -> int:
    while True:
        try:
            value = int(_ask(label, default))
            if low <= value <= high:
                return value
        except ValueError:
            pass
        print(f"请输入 {low} 到 {high} 之间的整数。")


def _enum(label: str, default: IntEnum) -> int:
    print(f"{label}  " + "  ".join(f"{member.value} {member.name}" for member in type(default)))
    return _number("编号", default.value, 0, max(type(default)))


def _choose_path(label: str, paths: list[Path]) -> Path:
    choices = paths[:40]
    for index, path in enumerate(choices, 1):
        print(f"  {index}. {path.name}")
    while True:
        raw = input(f"{label}，输入编号或路径 ").strip()
        if raw.isdecimal() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        if raw:
            return _path(raw)
        print("请输入编号或路径。")


def _default_config(excel: Path) -> RunConfig:
    timeline = read_timeline(excel)
    return RunConfig(
        endpoint=os.environ.get("PCR_UB_GA_MZQ_URL") or DEFAULT_ENDPOINT,
        battle_time=timeline.suggested_duration(excel.name), battle_seed=timeline.battle_seed,
    )


def _customize(base: RunConfig) -> RunConfig:
    endpoint = _ask("MZQ 地址", base.endpoint)
    duration = _number("战斗秒数", base.battle_time, 1, 90)
    seed = _number("战斗随机种子", base.battle_seed if base.battle_seed is not None else 666,
                   -(2**31), 2**31 - 1)
    population = _number("种群大小", base.ga.population, 1, 10000)
    islands = _number("岛屿数", min(base.ga.islands, population), 1, population)
    generations = _number("进化代数（0 只评估初始种群）", base.ga.generations, 0, 1000000)
    ga_seed = _number("GA 随机种子", base.ga.seed, 0, 2**63 - 1)
    while True:
        raw = _ask("指纹去重距离（帧，off 关闭）",
                   base.ga.fingerprint_distance if base.ga.fingerprint_distance is not None else "off")
        try:
            threshold = None if raw.lower() == "off" else float(raw)
            ga = replace(base.ga, population=population, islands=islands, generations=generations,
                         seed=ga_seed, fingerprint_distance=threshold)
            break
        except ValueError as exc:
            print(exc)

    profile = SemanticConversionProfile(
        ub_type=_enum("UB 语义编码", base.semantic.ub_type),
        skill_type=_enum("技能语义编码", base.semantic.skill_type),
        sequence_type=_enum("导出语义格式", base.semantic.sequence_type),
    )
    while True:
        raw = _ask("五个角色固定 UB 数（逗号分隔）", ",".join(map(str, base.fixed_ub_counts)))
        try:
            counts = tuple(int(part.strip()) for part in raw.split(","))
            if len(counts) == 5 and all(count >= 0 for count in counts):
                break
        except ValueError:
            pass
        print("请输入五个非负整数。")
    return replace(base, endpoint=endpoint, battle_time=duration, battle_seed=seed,
                   semantic=profile, ga=ga, fixed_ub_counts=counts)


def _interactive(args: argparse.Namespace) -> tuple[str, Path] | None:
    print("PCR 会战 UB 优化")
    print("  1. 新建优化")
    print("  2. 继续已有运行")
    print("  3. 使用已有配置从头重跑")
    print("  0. 退出")
    choice = _number("选择操作", 1, 0, 3)
    if choice == 0:
        return None
    if choice in (2, 3):
        runs = sorted((path.parent for path in args.runs.glob(f"*/{MANIFEST}")), reverse=True)
        folder = _choose_path("选择运行目录", runs)
        return ("resume" if choice == 2 else "restart"), folder

    workbooks = sorted(
        path for path in args.excel_dir.glob("*.xlsx") if not path.name.startswith("~$")
    )
    excel = _choose_path("选择待优化 Excel", workbooks)
    base = _default_config(excel)
    print("  1. 交互设置")
    print("  2. 从 JSON 选择配置")
    print("  3. 使用默认配置")
    config_choice = _number("选择优化配置", 1, 1, 3)
    if config_choice == 1:
        base = _customize(base)
    elif config_choice == 2:
        presets = sorted(args.config_dir.glob("*.json"))
        config_path = _choose_path("选择配置文件", presets)
        base = _load_config(base, config_path)
    profile = base.semantic
    print(f"将运行 {base.ga.population} 个体 × {base.ga.generations + 1} 代（含初始代），"
          f"{base.battle_time} 秒，MZQ {base.endpoint}")
    print(f"每个候选模拟 {base.evaluation_repeats} 次，以平均伤害评分。")
    print(f"语义转换 {profile.ub_type.name}/{profile.skill_type.name}/{profile.sequence_type.name}")
    if _ask("开始优化？y/n", "n").lower() not in ("y", "yes"):
        return None
    return "resume", create_run(excel, base, args.runs)


def _load_config(base: RunConfig, path: Path) -> RunConfig:
    data = read_json(path)
    return RunConfig.from_dict({**base.to_dict(), **data})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PCR 会战 UB 优化器")
    parser.add_argument("--runs", type=_path, default=Path.cwd() / "runs", help="运行目录根路径")
    parser.add_argument("--excel-dir", type=_path, default=Path.cwd(), help="交互列表中的 Excel 目录")
    parser.add_argument("--config-dir", type=_path, default=Path.cwd() / "configs",
                        help="交互列表中的配置目录")
    commands = parser.add_subparsers(dest="command")
    new = commands.add_parser("new", help="使用指定 Excel 新建优化")
    new.add_argument("excel", type=_path)
    new.add_argument("--config", type=_path)
    new.add_argument("--endpoint", help="覆盖 MZQ 地址")
    for command, help_text in (
        ("resume", "从最后一个完整代继续"),
        ("restart", "复制原输入和配置到新目录，从头重跑"),
        ("status", "显示运行状态"),
    ):
        subparser = commands.add_parser(command, help=help_text)
        subparser.add_argument("folder", type=_path)
    return parser


def _progress(info: dict) -> None:
    print(f"第 {info['generation']}/{info['target_generation']} 代  "
          f"最佳 {info['evaluation_repeats']} 次平均伤害 {info['damage']:,}  "
          f"评分仿真 {info['simulations']:,}  "
          f"本代指纹 {info['observed_in_last_generation']:,}  "
          f"累计指纹去重 {info['behavior_duplicates']:,}", flush=True)


def _resume_command(folder: Path) -> str:
    project = Path(__file__).resolve().parents[2]
    if (project / "pyproject.toml").is_file():
        command = ["uv", "run", "--project", str(project), "pcr-ub-ga"]
    else:
        command = [sys.executable, "-m", "pcr_ub_ga.cli"]
    command.extend(("resume", str(folder)))
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def main() -> int:
    args = _parser().parse_args()
    folder = None
    try:
        command = args.command
        if command is None:
            selected = _interactive(args)
            if selected is None:
                return 0
            command, folder = selected
        elif command == "new":
            config = _default_config(args.excel)
            if args.config:
                config = _load_config(config, args.config)
            if args.endpoint:
                config = replace(config, endpoint=args.endpoint)
            folder = create_run(args.excel, config, args.runs)
        else:
            folder = args.folder
        if command == "restart":
            folder = restart_run(folder)
        elif command == "status":
            print(json.dumps(read_json(folder / STATUS), ensure_ascii=False, indent=2))
            return 0

        print(f"运行目录 {folder}", flush=True)
        print(f"恢复命令 {_resume_command(folder)}", flush=True)
        result = asyncio.run(execute_run(folder, report=_progress))
        print(f"完成，平均伤害 {result['summary']['damage']:,}")
        print(f"优化 Excel {folder / result['workbook']}")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\n已中断。已有运行可通过 resume 继续。", file=sys.stderr)
        return 130
    except (ValueError, MzqError, OSError) as exc:
        print(f"错误 {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
