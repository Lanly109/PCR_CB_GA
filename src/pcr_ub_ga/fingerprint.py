"""ActionProgress parsing and exact, time-weighted phase distance."""

from dataclasses import dataclass
from functools import lru_cache
import math

from .semantics import integer

SCHEDULE = ((30, 90), (40, 30), (60, 60), (70, 10), (89, 15), (90, 10))


def sample_frames(battle_time: int) -> tuple[int, ...]:
    total = integer(battle_time, "战斗秒数", 1) * 60
    if total > 5400:
        raise ValueError("战斗时长最多 90 秒")
    start = 0
    frames = []
    for seconds, step in SCHEDULE:
        end = seconds * 60
        frames.extend(range(start + step, min(end, total) + 1, step))
        if end >= total:
            break
        start = end
    return tuple(frames) or (min(30, total),)


def finite_number(value: object, label: str, minimum: float = 0) -> float:
    if type(value) not in (float, int) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{label} 必须是有限且大于等于 {minimum:g} 的数字")
    return float(value)


@dataclass(frozen=True, slots=True)
class ActionProgressFingerprint:
    frames: tuple[int, ...]
    unit_ids: tuple[int, ...]
    start_frames: tuple[float, ...]
    loop_frames: tuple[float, ...]
    progress: tuple[tuple[float, ...], ...]
    loop_masks: tuple[int, ...]

    @classmethod
    def from_payload(cls, payload: object) -> "ActionProgressFingerprint":
        names = ("Frames", "UnitIds", "StartFrames", "LoopFrames", "Progress")
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(name), (list, tuple)) for name in names
        ):
            raise ValueError("ActionProgress 缺少完整的帧、角色或进度数组")
        frames = tuple(integer(value, "Frames") for value in payload["Frames"])
        ids = tuple(integer(value, "UnitIds", 1) for value in payload["UnitIds"])
        starts = tuple(finite_number(value, "StartFrames") for value in payload["StartFrames"])
        loops = tuple(finite_number(value, "LoopFrames") for value in payload["LoopFrames"])
        rows = payload["Progress"]
        if not frames or any(a >= b for a, b in zip(frames, frames[1:])):
            raise ValueError("ActionProgress.Frames 必须非空且严格递增")
        if not ids or len(set(ids)) != len(ids) or len(starts) != len(ids) or len(loops) != len(ids):
            raise ValueError("ActionProgress 角色 ID 和起手、循环长度必须一一对应")
        if any(period <= 0 for period in loops):
            raise ValueError("ActionProgress.LoopFrames 必须大于零")
        if len(rows) != len(frames) or any(
            not isinstance(row, (list, tuple)) or len(row) != len(ids) for row in rows
        ):
            raise ValueError("ActionProgress.Progress 必须按帧排列并匹配角色数量")

        order = sorted(range(len(ids)), key=ids.__getitem__)
        progress, masks = [], []
        for row in rows:
            normalized, mask = [], 0
            for bit, index in enumerate(order):
                value = finite_number(row[index], "Progress")
                if value > starts[index]:
                    value = (value - starts[index]) % loops[index]
                    mask |= 1 << bit
                normalized.append(value)
            progress.append(tuple(normalized))
            masks.append(mask)
        return cls(
            frames, tuple(ids[i] for i in order), tuple(starts[i] for i in order),
            tuple(loops[i] for i in order), tuple(progress), tuple(masks),
        )

    @property
    def layout(self) -> tuple:
        return self.frames, self.unit_ids, self.start_frames, self.loop_frames


@lru_cache(maxsize=128)
def _weights(frames: tuple[int, ...]) -> tuple[float, ...]:
    if len(frames) == 1:
        return (1.0,)
    gaps = tuple(right - left for left, right in zip(frames, frames[1:]))
    return (gaps[0], *((a + b) / 2 for a, b in zip(gaps, gaps[1:])), gaps[-1])


def _squared_phase_sum(
    left: ActionProgressFingerprint,
    right: ActionProgressFingerprint,
    cutoff: float,
) -> float:
    total = 0.0
    for row, other, mask, other_mask, weight in zip(
        left.progress, right.progress, left.loop_masks, right.loop_masks, _weights(left.frames),
        strict=True,
    ):
        row_sum = 0.0
        for role, (a, b, start, period) in enumerate(zip(
            row, other, left.start_frames, left.loop_frames, strict=True,
        )):
            bit = 1 << role
            if (mask & bit) and (other_mask & bit):
                delta = abs(a - b)
                delta = min(delta, period - delta)
            elif mask & bit:
                delta = start - b + min(a, period - a)
            elif other_mask & bit:
                delta = start - a + min(b, period - b)
            else:
                delta = a - b
            row_sum += delta * delta
        total += weight * row_sum
        if total > cutoff:
            break
    return total


def _weight_sum(fingerprint: ActionProgressFingerprint) -> float:
    return sum(_weights(fingerprint.frames)) * len(fingerprint.unit_ids)


def fingerprint_distance(
    left: ActionProgressFingerprint, right: ActionProgressFingerprint,
) -> float | None:
    """Return weighted RMS in frames, or None for incompatible layouts."""
    if left.layout != right.layout:
        return None
    return math.sqrt(_squared_phase_sum(left, right, math.inf) / _weight_sum(left))


def fingerprints_close(
    left: ActionProgressFingerprint, right: ActionProgressFingerprint, threshold: float,
) -> bool:
    if left.layout != right.layout:
        return False
    if left == right:
        return True
    cutoff = threshold * threshold * _weight_sum(left)
    return _squared_phase_sum(left, right, cutoff) <= cutoff
