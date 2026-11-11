"""Run-folder lifecycle, crash-safe checkpoints and workbook export."""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile

import httpx

from .config import DEFAULT_EVALUATION_REPEATS, RunConfig
from .engine import BattleSample, Candidate, Evaluation, GenerationState, optimize
from .fingerprint import ActionProgressFingerprint, sample_frames
from .mzq import MzqEvaluator
from .semantics import (
    Genome, SemanSequenceType, SemanSkillType, SemanUBType, full_genome, genome_from_rows, search_pools,
)
from .workbook import Timeline, object_fields, read_timeline, write_optimized_workbook

SCHEMA = 1
INPUT = "input.xlsx"
CONFIG = "config.json"
MANIFEST = "manifest.json"
PREPARED = "prepared.json"
CHECKPOINT = "checkpoint.json.gz"
STATUS = "status.json"
RESULT = "result.json"
OUTPUT = "optimized.xlsx"


@contextmanager
def atomic_file(target: Path) -> Iterator[Path]:
    descriptor, filename = tempfile.mkstemp(prefix=f".{target.stem}-", suffix=target.suffix, dir=target.parent)
    os.close(descriptor)
    path = Path(filename)
    try:
        yield path
        with path.open("rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(path, target)
        if os.name != "nt":
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        path.unlink(missing_ok=True)


def write_json(target: Path, data: dict, *, compress: bool = False) -> None:
    with atomic_file(target) as temporary:
        if compress:
            with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=3) as stream:
                json.dump(data, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        else:
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8",
            )


def read_json(path: Path, *, compress: bool = False) -> dict:
    try:
        opener = gzip.open if compress else open
        with opener(path, "rt", encoding="utf-8") as stream:
            return object_fields(json.load(stream), path.name)
    except (ValueError, EOFError, OSError) as exc:
        raise ValueError(f"无法读取 {path.name}，{exc}") from exc


def file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def run_lock(folder: Path) -> Iterator[None]:
    with (folder / ".run.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError("这个运行目录正在被另一个进程使用") from exc
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("这个运行目录正在被另一个进程使用") from exc
        # The OS releases the lock when the handle closes, including process exit.
        yield


def create_run(
    workbook: Path, config: RunConfig, root: Path, *, source_name: str | None = None,
) -> Path:
    timeline = read_timeline(workbook)
    if config.battle_seed is None:
        if timeline.battle_seed is None:
            raise ValueError("Excel 缺少战斗随机种子，请在配置中填写 battle_seed")
        config = replace(config, battle_seed=timeline.battle_seed)
    timeline.request_context(battle_time=config.battle_time, battle_seed=config.battle_seed)
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=root))
    try:
        with atomic_file(folder / INPUT) as temporary:
            shutil.copyfile(workbook, temporary)
        write_json(folder / CONFIG, config.to_dict())
        write_json(folder / STATUS, {"state": "ready", "generation": None})
        write_json(folder / MANIFEST, {
            "schema": SCHEMA, "source_name": source_name or workbook.name,
            "input_sha256": file_hash(folder / INPUT), "config_sha256": file_hash(folder / CONFIG),
        })
    except BaseException:
        shutil.rmtree(folder)
        raise
    return folder.resolve()


def load_run(folder: Path) -> tuple[RunConfig, Timeline, dict]:
    manifest = read_json(folder / MANIFEST)
    if manifest.get("schema") != SCHEMA:
        raise ValueError("运行目录版本不受支持")
    for filename, key in ((INPUT, "input_sha256"), (CONFIG, "config_sha256")):
        if file_hash(folder / filename) != manifest.get(key):
            raise ValueError(f"{filename} 与初始快照不符，恢复要求使用原输入和配置")
    raw_config = read_json(folder / CONFIG)
    config = RunConfig.from_dict({
        "evaluation_repeats": 1,
        **raw_config,
    })
    if config.battle_seed is None:
        raise ValueError("运行配置缺少固定战斗随机种子")
    return config, read_timeline(folder / INPUT), manifest


def restart_run(folder: Path, root: Path | None = None) -> Path:
    config, _, manifest = load_run(folder)
    if "evaluation_repeats" not in read_json(folder / CONFIG):
        config = replace(config, evaluation_repeats=DEFAULT_EVALUATION_REPEATS)
    return create_run(folder / INPUT, config, root if root is not None else folder.parent,
                      source_name=manifest["source_name"])


def _restore_candidate(raw: dict) -> Candidate:
    data = dict(raw["evaluation"])
    payload = data["fingerprint"]
    if payload is not None:
        data["fingerprint"] = ActionProgressFingerprint(
            frames=tuple(payload["frames"]), unit_ids=tuple(payload["unit_ids"]),
            start_frames=tuple(payload["start_frames"]), loop_frames=tuple(payload["loop_frames"]),
            progress=tuple(tuple(row) for row in payload["progress"]),
            loop_masks=tuple(payload["loop_masks"]),
        )
    data["ub_counts"] = tuple(data["ub_counts"])
    data["samples"] = tuple(BattleSample(**sample) for sample in data.get("samples", ()))
    return Candidate(genome_from_rows(raw["genome"]), Evaluation(**data))


def _restore_state(raw: dict) -> GenerationState:
    data = dict(raw)
    data["islands"] = tuple(tuple(map(_restore_candidate, island)) for island in data["islands"])
    data["best"] = _restore_candidate(data["best"])
    version, numbers, spare = data["rng_state"]
    data["rng_state"] = (version, tuple(numbers), spare)
    return GenerationState(**data)


def _summary(state: GenerationState, config: RunConfig) -> dict:
    best = state.best.evaluation
    return {
        "generation": state.generation, "target_generation": config.ga.generations,
        "best_generation": state.best_generation, "damage": best.mean_damage,
        "reference_damage": best.damage,
        "evaluation_repeats": config.evaluation_repeats,
        "damage_samples": [sample.damage for sample in best.samples] or [best.damage],
        "sample_seeds": [sample.seed for sample in best.samples] or [config.battle_seed],
        "score": best.score(config.battle_time), "battle_frames": best.frames,
        "battle_result": best.result, "ub_counts": list(best.ub_counts),
        "simulations": state.simulations, "exact_duplicates": state.exact_duplicates,
        "behavior_duplicates": state.behavior_duplicates,
        "observed_in_last_generation": state.observed,
    }


def _same_battle(left: Evaluation, right: Evaluation) -> bool:
    return (
        (left.damage, left.frames, left.result) == (right.damage, right.frames, right.result)
        and left.samples == right.samples
    )


def _best_workbook_prefix(state: GenerationState) -> str:
    return f"best_g{state.best_generation:06d}_{state.best.evaluation.mean_damage}_"


def _best_workbook_name(state: GenerationState) -> str:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S%z")
    return f"{_best_workbook_prefix(state)}{timestamp}.xlsx"


async def _export_best(
    path: Path, timeline: Timeline, mzq: MzqEvaluator,
    candidate: Candidate, genes: Genome, config: RunConfig,
) -> Timeline:
    sequence_mode = config.semantic.sequence_type is SemanSequenceType.SEQUENCE
    if path.is_file():
        saved = read_timeline(path)
        if (
            saved.battle_seed != config.battle_seed or saved.unit_ids != timeline.unit_ids
            or saved.raw.get("totalDamage") != candidate.evaluation.damage
            or (
                candidate.evaluation.samples
                and (
                    saved.raw.get("gaMeanDamage") != candidate.evaluation.mean_damage
                    or saved.raw.get("gaDamageSamples") != [
                        asdict(sample) for sample in candidate.evaluation.samples
                    ]
                )
            )
            or not saved.use_semantic or (saved.sequence is not None) != sequence_mode
            or (not sequence_mode and saved.semantic != genes)
        ):
            raise ValueError(f"最佳快照与检查点不符，{path.name}")
        return saved

    replay, frames = await mzq.replay(genes)
    if not _same_battle(candidate.evaluation, replay):
        raise ValueError("最优基因固定种子重放与检查点评分不一致")
    exported_lanes, roles = genes, None
    if sequence_mode:
        converted = await mzq.convert(frames)
        exported_lanes, roles = converted.lanes, converted.sequence
        verified, frames = await mzq.replay(exported_lanes, roles=roles)
        if not _same_battle(replay, verified):
            raise ValueError("全局UB序列转换后的重放与五条角色UB序列的最优评分不一致")
    with atomic_file(path) as temporary:
        write_optimized_workbook(
            temporary, timeline, lanes=exported_lanes, sequence=roles, frames=frames,
            battle_seed=config.battle_seed, damage=replay.damage, battle_frames=replay.frames,
            samples=replay.samples,
        )
        saved = read_timeline(temporary)
        if (
            saved.semantic != exported_lanes or saved.sequence != roles
            or saved.frames[:5] != frames or saved.battle_seed != config.battle_seed
        ):
            raise ValueError("优化 Excel 写入后校验失败")
    return saved


async def execute_run(
    folder: Path, *, transport: httpx.AsyncBaseTransport | None = None,
    report: Callable[[dict], None] | None = None,
) -> dict:
    with run_lock(folder):
        config, timeline, manifest = load_run(folder)
        previous = None
        if (folder / RESULT).is_file() and (folder / OUTPUT).is_file():
            result = read_json(folder / RESULT)
            if result.get("output_sha256") == file_hash(folder / OUTPUT):
                write_json(folder / STATUS, {"state": "complete", **result["summary"]})
                return result

        try:
            async with httpx.AsyncClient(timeout=config.timeout, transport=transport) as client:
                capture = sample_frames(config.battle_time) if config.ga.fingerprint_distance is not None else ()
                mzq = MzqEvaluator(
                    client, endpoint=config.endpoint, timeline=timeline,
                    battle_time=config.battle_time, battle_seed=config.battle_seed,
                    profile=config.semantic, capture_frames=capture,
                    batch_size=config.batch_size, concurrency=config.concurrency,
                    evaluation_repeats=config.evaluation_repeats,
                )
                prepared_path = folder / PREPARED
                checkpoint_path = folder / CHECKPOINT
                checkpoint = read_json(checkpoint_path, compress=True) if checkpoint_path.exists() else None
                if checkpoint is not None and not prepared_path.is_file():
                    raise ValueError("检查点缺少对应的 prepared.json")
                if prepared_path.exists():
                    prepared = read_json(prepared_path)
                else:
                    converted = await mzq.initial_axis(timeline)
                    prepared = {
                        "schema": SCHEMA, "manifest": manifest, "seed_lanes": converted.lanes,
                        "source_conversion_roles": converted.sequence,
                    }
                    write_json(prepared_path, prepared)
                if prepared.get("schema") != SCHEMA or prepared.get("manifest") != manifest:
                    raise ValueError("prepared.json 与当前运行的输入或配置不符")

                prepared_hash = file_hash(prepared_path)
                if checkpoint is not None:
                    if checkpoint.get("schema") != SCHEMA or checkpoint.get("prepared_sha256") != prepared_hash:
                        raise ValueError("检查点与当前运行的 prepared.json 不符")
                    try:
                        previous = _restore_state(checkpoint["state"])
                        candidates = [previous.best] + [
                            candidate for island in previous.islands for candidate in island
                        ]
                        if any(
                            (tuple(sample.seed for sample in candidate.evaluation.samples) or (config.battle_seed,))
                            != mzq.sample_seeds for candidate in candidates
                        ):
                            raise ValueError("检查点评分的采样种子与当前配置不一致")
                    except (TypeError, ValueError, KeyError) as exc:
                        raise ValueError(f"检查点内容无效，{exc}") from exc

                seed = genome_from_rows(prepared.get("seed_lanes"))
                cuts = config.fixed_ub_counts
                if any(count > len(lane) for count, lane in zip(cuts, seed, strict=True)):
                    raise ValueError("固定 UB 数超过转换后对应角色的轴长度")
                prefix = tuple(lane[:count] for lane, count in zip(seed, cuts, strict=True))
                initial = tuple(lane[count:] for lane, count in zip(seed, cuts, strict=True))
                legacy = config.legacy_ub
                if legacy is None:
                    legacy = (
                        config.semantic.ub_type is SemanUBType.SERIES_1
                        and config.semantic.skill_type is SemanSkillType.SERIES_20
                    )
                pools = search_pools(
                    initial, config.extra_genes, legacy_ub=legacy,
                    own_skill=config.own_skill, other_skill=config.other_skill, delay=config.delay,
                )

                best_workbook = None
                best_export = None

                def persist_checkpoint(state: GenerationState, name: str) -> None:
                    nonlocal previous
                    write_json(checkpoint_path, {
                        "schema": SCHEMA, "prepared_sha256": prepared_hash, "state": asdict(state),
                        "best_workbook": name,
                    }, compress=True)
                    previous = state

                async def save_checkpoint(state: GenerationState) -> None:
                    nonlocal best_workbook, best_export
                    improved = best_workbook is None or state.best_generation == state.generation
                    if improved:
                        best_workbook = _best_workbook_name(state)
                    persist_checkpoint(state, best_workbook)
                    if improved:
                        best_export = await _export_best(
                            folder / best_workbook, timeline, mzq, state.best,
                            full_genome(prefix, state.best.genome), config,
                        )
                    summary = _summary(state, config)
                    write_json(folder / STATUS, {"state": "running", **summary})
                    if report is not None:
                        report(summary)

                if previous is not None:
                    best_workbook = checkpoint.get("best_workbook")
                    if best_workbook is None:
                        best_workbook = _best_workbook_name(previous)
                        persist_checkpoint(previous, best_workbook)
                    elif (
                        not isinstance(best_workbook, str) or Path(best_workbook).name != best_workbook
                        or not best_workbook.startswith(_best_workbook_prefix(previous))
                        or not best_workbook.endswith(".xlsx")
                    ):
                        raise ValueError("检查点的最佳 Excel 文件名无效")
                    best_export = await _export_best(
                        folder / best_workbook, timeline, mzq, previous.best,
                        full_genome(prefix, previous.best.genome), config,
                    )

                state = await optimize(
                    initial=initial, prefix=prefix, pools=pools, battle_time=config.battle_time,
                    evaluator=mzq.evaluate, config=config.ga, resume=previous, on_checkpoint=save_checkpoint,
                )
                best = full_genome(prefix, state.best.genome)
                assert best_workbook is not None and best_export is not None
                with atomic_file(folder / OUTPUT) as temporary:
                    shutil.copyfile(folder / best_workbook, temporary)

                summary = _summary(state, config)
                result = {
                    "schema": SCHEMA, "summary": summary, "workbook": OUTPUT,
                    "output_sha256": file_hash(folder / OUTPUT),
                    "best_genes": [list(lane) for lane in best],
                    "exported_genes": [list(lane) for lane in best_export.semantic],
                    "sequence_roles": list(best_export.sequence) if best_export.sequence is not None else None,
                    "frame_axis": [list(lane) for lane in best_export.frames[:5]], "battle_seed": config.battle_seed,
                }
                write_json(folder / RESULT, result)
                write_json(folder / STATUS, {"state": "complete", **summary})
                return result
        except (Exception, asyncio.CancelledError) as exc:
            status = {"state": "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed"}
            status.update(_summary(previous, config) if previous else {"generation": None})
            if not isinstance(exc, asyncio.CancelledError):
                status["error"] = str(exc)
            write_json(folder / STATUS, status)
            raise
