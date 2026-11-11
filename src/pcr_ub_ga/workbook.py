"""Read and write PCRCalculator's compressed Excel savePage."""

import base64
from copy import deepcopy
from dataclasses import asdict, dataclass
import gzip
import json
from pathlib import Path
import re
from zipfile import BadZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from .engine import BattleSample
from .fingerprint import finite_number
from .semantics import EMPTY_GENOME, Genome, genome_from_rows, integer, sequence_roles

FrameAxis = tuple[tuple[float, ...], ...]
UNIT_FIELDS = (
    "enemyid", "unitId", "level", "rarity", "love", "rank", "equipLevel", "skillLevel",
    "playLoveDic", "uniqueEqLv", "uniqueEq2Lv", "exEquip", "exEquipLevel", "exEquipSubs",
    "PrefabId", "love2",
)
DURATION = re.compile(r"(?:尾刀|补偿)\s*(\d+(?:\.\d+)?)\s*(?:s|秒)", re.IGNORECASE)


def object_fields(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是对象")
    return value


def frame_axis(value: object, *, allow_boss: bool = False) -> FrameAxis:
    sizes = (5, 6) if allow_boss else (5,)
    if not isinstance(value, (list, tuple)) or len(value) not in sizes or any(
        not isinstance(row, (list, tuple)) for row in value
    ):
        raise ValueError("帧轴必须包含五条角色UB序列" + ("和可选的 Boss UB序列" if allow_boss else ""))
    return tuple(tuple(finite_number(item, "UB 帧时间") for item in row) for row in value)


def _role_mastery(value: object) -> dict[str, list[int]]:
    normalized = {}
    for key, pair in object_fields(value, "roleMastery").items():
        if isinstance(pair, dict):
            pair = [
                pair.get("slotLevel", pair.get("Item1", pair.get("item1", 0))),
                pair.get("enhanceLevel", pair.get("Item2", pair.get("item2", 0))),
            ]
        if not isinstance(pair, (list, tuple)) or len(pair) > 2:
            raise ValueError("roleMastery 必须提供槽位和强化等级")
        normalized[str(key)] = [integer(item, "roleMastery") for item in pair] + [0] * (2 - len(pair))
    return normalized


@dataclass(frozen=True, slots=True)
class Timeline:
    raw: dict
    header: str
    players: tuple[dict, ...]
    enemy_ids: tuple[int, ...]
    battle_seed: int | None
    frames: FrameAxis
    semantic: Genome
    sequence: tuple[int, ...] | None
    use_semantic: bool
    use_auto: bool
    use_set: bool

    @property
    def unit_ids(self) -> tuple[int, ...]:
        return tuple(player["unitId"] for player in self.players)

    @property
    def group(self) -> dict:
        return self.raw["playerGroupData"]

    def suggested_duration(self, name: str) -> int:
        for text in (name, self.raw.get("timeLineName"), self.raw.get("detail")):
            if not isinstance(text, str):
                continue
            match = DURATION.search(text)
            if match:
                seconds = float(match[1])
                if seconds.is_integer() and 1 <= seconds <= 90:
                    return int(seconds)
        return 90

    def request_context(self, *, battle_time: int, battle_seed: int) -> dict:
        group = self.group
        settings = object_fields(group.get("timeLineData", {}), "timeLineData")
        is_talent = group.get("isTalentQuestBoss")
        if is_talent is None:
            is_talent = any(
                group.get(key, 0) for key in ("talentQuestMap", "talentQuestArea", "talentQuestIndex")
            )
        return {
            "PlayerList": self.players,
            "EnemyList": [],
            "GuildEnemyList": self.enemy_ids,
            "Settings": {**settings, "UseFixedRandomSeed": True, "RandomSeed": battle_seed},
            "TalentSkillLevels": object_fields(group.get("talentLevel", {}), "talentLevel"),
            "TalentSkillNodeLevels": object_fields(group.get("talentSkill", {}), "talentSkill"),
            "MasterSkillNodeLevels": object_fields(group.get("teamSkill", {}), "teamSkill"),
            "RoleMastery": _role_mastery(group.get("roleMastery", {})),
            "TalentFullInfo": True,
            "IsTalentQuest": bool(is_talent),
            "ToMaxUnitAttr": False,
            "IsGuildBattle": True,
            "BattleTime": battle_time,
            "WaveGroupId": integer(group.get("waveGroupId", 0), "waveGroupId"),
            "SetToggles": [],
            "NeedOnceResult": False,
        }


def read_timeline(path: Path) -> Timeline:
    if path.suffix.lower() != ".xlsx":
        raise ValueError("请选择 PCRCalculator 的 .xlsx 轴")
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook["savePage"]
            header, encoded = sheet["A1"].value, sheet["A2"].value
        finally:
            workbook.close()
        if not isinstance(encoded, str):
            raise ValueError("savePage!A2 缺少压缩轴数据")
        raw = object_fields(json.loads(gzip.decompress(base64.b64decode(encoded))), "Excel 轴")
    except (KeyError, OSError, EOFError, ValueError, BadZipFile, InvalidFileException) as exc:
        raise ValueError(f"无法读取 Excel 的 savePage 轴数据，文件 {path.name}，{exc}") from exc

    group = object_fields(raw.get("playerGroupData"), "playerGroupData")
    players = object_fields(group.get("playerData"), "playerData").get("playrCharacters")
    if not isinstance(players, list) or len(players) != 5:
        raise ValueError("Excel 必须包含五个角色")
    units = tuple(
        {name: unit[name] for name in UNIT_FIELDS if name in unit}
        for unit in (object_fields(player, "角色") for player in players)
    )
    ids = [integer(unit.get("unitId"), "unitId", 1) for unit in units]
    if len(set(ids)) != len(ids):
        raise ValueError("Excel 角色 ID 必须唯一")
    enemies = group.get("selectedEnemyIDs")
    if not isinstance(enemies, list) or not enemies:
        raise ValueError("Excel 缺少 selectedEnemyIDs")
    enemy_ids = tuple(integer(enemy, "Boss ID", 1) for enemy in enemies)
    settings = object_fields(group.get("timeLineData", {}), "timeLineData")
    battle_seed = settings.get("RandomSeed")
    if battle_seed is not None and type(battle_seed) is not int:
        raise ValueError("Excel 的 timeLineData.RandomSeed 必须是整数")

    frames = frame_axis(group.get("UBExecTimeData") or EMPTY_GENOME, allow_boss=True)
    if not any(frames) and raw.get("UBExecTime"):
        frames = frame_axis(raw["UBExecTime"], allow_boss=True)
    semantic = genome_from_rows(group.get("SemanUBExecTimeData") or EMPTY_GENOME, trim_sentinels=True)
    roles = group.get("SemanUBSequenceData")
    use_sequence = bool(group.get("useSequenceUB", roles))
    use_semantic = bool(group.get("useSemanMode", use_sequence or (any(semantic) and not any(frames))))
    if use_sequence and not use_semantic:
        raise ValueError("序列轴需要启用语义模式")
    sequence = sequence_roles(roles, semantic) if use_sequence else None
    use_auto = bool(group.get("useAutoMode"))
    use_set = bool(group.get("useSetMode")) or (
        not use_auto and not any(frames) and not any(semantic)
    )
    return Timeline(raw, str(header or "savePage"), units, enemy_ids, battle_seed,
                    frames, semantic, sequence, use_semantic, use_auto, use_set)


def write_optimized_workbook(
    path: Path, timeline: Timeline, *, lanes: Genome, sequence: tuple[int, ...] | None,
    frames: FrameAxis, battle_seed: int, damage: int, battle_frames: int,
    samples: tuple[BattleSample, ...] = (),
) -> None:
    """Write a fresh importable savePage with a matching, human-readable summary."""
    raw = deepcopy(timeline.raw)
    group = raw["playerGroupData"]
    six_lanes = [list(row) for row in frames] + [[]]
    group.update(
        UBExecTimeData=six_lanes, SemanUBExecTimeData=lanes,
        SemanUBSequenceData=sequence or [], useAutoMode=False, useSetMode=False,
        useSemanMode=True, useSequenceUB=sequence is not None,
    )
    group.setdefault("timeLineData", {}).update(RandomSeed=battle_seed, UseFixedRandomSeed=True)
    raw.update(UBExecTime=six_lanes, currentRandomSeed=battle_seed, totalDamage=damage)
    # The savePage frame axis and actual damage refer to the first fixed seed.
    # Keep the multi-seed estimate and its evidence alongside those replay facts.
    raw.pop("gaMeanDamage", None)
    raw.pop("gaDamageSamples", None)
    if samples:
        raw["gaMeanDamage"] = sum(sample.damage for sample in samples) / len(samples)
        raw["gaDamageSamples"] = [asdict(sample) for sample in samples]
    raw["exceptDamage"] = 0
    raw["backDamage"] = 0
    raw.pop("uBDetails", None)
    encoded = base64.b64encode(gzip.compress(json.dumps(raw, ensure_ascii=False).encode())).decode()
    if len(encoded) > 32767:
        raise ValueError("优化后的 savePage 超出 Excel 单元格容量")
    workbook = Workbook()
    try:
        summary = workbook.active
        summary.title = "优化结果"
        if samples:
            summary.append([f"{len(samples)} 次平均伤害", raw["gaMeanDamage"]])
        summary.append(["参考种子实际伤害", damage])
        summary.append(["战斗帧数", battle_frames])
        summary.append(["战斗随机种子", battle_seed])
        summary.append(["角色 ID", "UB 时间", "语义基因"])
        for unit, timings, lane in zip(timeline.unit_ids, frames, lanes, strict=True):
            summary.append([unit, ", ".join(map(str, timings)), ", ".join(map(str, lane))])
        if samples:
            summary.append([])
            summary.append(["战斗随机种子", "实际伤害", "战斗帧数", "战斗结果"])
            for sample in samples:
                summary.append([sample.seed, sample.damage, sample.frames, sample.result])
        summary.column_dimensions["A"].width = 20
        summary.column_dimensions["B"].width = 48
        summary.column_dimensions["C"].width = 58
        saved = workbook.create_sheet("savePage")
        saved.append([timeline.header])
        saved.append([encoded])
        saved.sheet_state = "hidden"
        workbook.save(path)
    finally:
        workbook.close()
