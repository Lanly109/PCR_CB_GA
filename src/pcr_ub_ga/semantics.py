"""Five-lane genomes and PCRCalculator semantic conversion settings."""

from dataclasses import dataclass
from enum import IntEnum

ROLE_COUNT = 5
Genome = tuple[tuple[int, ...], ...]
EMPTY_GENOME: Genome = ((),) * ROLE_COUNT


class SemanUBType(IntEnum):
    SERIES_1 = 0
    SERIES_4 = 1


class SemanSkillType(IntEnum):
    FRAME = 0
    SERIES_1 = 1
    SERIES_2 = 2
    SERIES_3 = 3
    SERIES_5 = 4
    SERIES_10 = 5
    SERIES_20 = 6
    SERIES_30 = 7


class SemanSequenceType(IntEnum):
    LANES = 0
    SEQUENCE = 1


@dataclass(frozen=True)
class SemanticConversionProfile:
    ub_type: SemanUBType = SemanUBType.SERIES_1
    skill_type: SemanSkillType = SemanSkillType.SERIES_20
    sequence_type: SemanSequenceType = SemanSequenceType.LANES

    def __post_init__(self) -> None:
        for field, enum in (
            ("ub_type", SemanUBType),
            ("skill_type", SemanSkillType),
            ("sequence_type", SemanSequenceType),
        ):
            value = getattr(self, field)
            if type(value) not in (int, enum):
                raise ValueError(f"{field} 必须是 {enum.__name__} 或对应的整数")
            object.__setattr__(self, field, enum(value))

    def wire_fields(self) -> dict[str, int]:
        return {
            "SemanUBType": int(self.ub_type),
            "SemanSkillType": int(self.skill_type),
            "SemanSequenceType": int(self.sequence_type),
        }


def integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} 必须是大于等于 {minimum} 的整数")
    return value


def genome_from_rows(rows: object, *, trim_sentinels: bool = False) -> Genome:
    if not isinstance(rows, (tuple, list)) or len(rows) != ROLE_COUNT:
        raise ValueError("语义轴必须包含五条角色UB序列")
    result = []
    for row in rows:
        if not isinstance(row, (tuple, list)):
            raise ValueError("角色UB序列必须是数组")
        end = len(row)
        if trim_sentinels:
            while end and type(row[end - 1]) is int and row[end - 1] == -1:
                end -= 1
        result.append(tuple(integer(value, "语义基因") for value in row[:end]))
    return tuple(result)


def sequence_roles(raw: object, lanes: Genome) -> tuple[int, ...]:
    if not isinstance(raw, (tuple, list)):
        raise ValueError("SemanUBSequenceData 必须是角色序列")
    roles = tuple(integer(role, "序列角色") for role in raw)
    if any(role >= ROLE_COUNT for role in roles):
        raise ValueError("序列角色必须在 0..4 内")
    if any(roles.count(role) != len(lane) for role, lane in enumerate(lanes)):
        raise ValueError("角色序列必须恰好消费五条UB序列中的基因")
    return roles


def full_genome(prefix: Genome, genome: Genome) -> Genome:
    return tuple(fixed + lane for fixed, lane in zip(prefix, genome, strict=True))


def search_pools(
    seed: Genome,
    extra: Genome,
    *,
    legacy_ub: bool,
    own_skill: bool,
    other_skill: bool,
    delay: int,
) -> Genome:
    """Use the original search families plus the converted seed and explicit values."""
    pools = []
    skill_offsets = (0, 10, 200, 210, 220, 300, 310, 320)
    for role, (seed_lane, extra_lane) in enumerate(zip(seed, extra, strict=True)):
        values = {0, *seed_lane, *extra_lane, *range(1, delay + 1)}
        if legacy_ub:
            for target in range(ROLE_COUNT + 1):
                base = 10000 + target * 1000 + (900 if target == role else 100)
                values.update((base, base + 10))
        targets = [role] if own_skill else []
        if other_skill:
            targets.extend(target for target in range(ROLE_COUNT + 1) if target != role)
        for target in targets:
            base = 10000 + target * 1000
            for offset in skill_offsets:
                values.update(range(base + offset, base + offset + 10))
        pools.append(tuple(sorted(values)))
    return tuple(pools)
