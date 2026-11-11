"""PCRCalculator /simulate adapter using the GA Studio wire contract."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import islice
from urllib.parse import urlsplit, urlunsplit

import httpx

from .engine import BattleSample, Evaluation
from .fingerprint import ActionProgressFingerprint
from .semantics import (
    EMPTY_GENOME, Genome, SemanticConversionProfile, SemanSequenceType, genome_from_rows,
    integer, sequence_roles,
)
from .workbook import FrameAxis, Timeline, frame_axis, object_fields

FAST_BASE = 3
UB_TIME = 2
SEMANTIC_UB_CONVERSION = 32


class MzqError(RuntimeError):
    """Transport or simulation failed before an evaluation could be committed."""


def simulation_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("MZQ 地址必须是 HTTP(S) URL")
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("MZQ 地址必须是 HTTP(S) URL，且地址中不能包含用户名或密码")
    if parts.fragment or parts.query:
        raise ValueError("MZQ 地址不能包含查询参数或片段")
    # Accessing port also validates explicit non-numeric or out-of-range ports.
    _ = parts.port
    return urlunsplit(parts._replace(path=parts.path.rstrip("/") or "/simulate"))


@dataclass(frozen=True, slots=True)
class ConvertedAxis:
    lanes: Genome
    sequence: tuple[int, ...] | None


class MzqEvaluator:
    def __init__(
        self, client: httpx.AsyncClient, *, endpoint: str, timeline: Timeline,
        battle_time: int, battle_seed: int, profile: SemanticConversionProfile,
        capture_frames: tuple[int, ...], batch_size: int, concurrency: int,
        evaluation_repeats: int = 1,
    ) -> None:
        self.client = client
        self.endpoint = simulation_url(endpoint)
        self.context = timeline.request_context(battle_time=battle_time, battle_seed=battle_seed)
        self.battle_seed = battle_seed
        self.profile = profile
        self.capture_frames = capture_frames
        self.batch_size = integer(batch_size, "batch_size", 1)
        self.concurrency = integer(concurrency, "concurrency", 1)
        repeats = integer(evaluation_repeats, "evaluation_repeats", 1)
        self.sample_seeds = (battle_seed,) + tuple(
            (abs(battle_seed) + index) % (2**31 - 1) for index in range(1, repeats)
        )
        self.unit_ids = set(timeline.unit_ids)
        self.frame_set = set(capture_frames)

    def _request(
        self, calc_stats: int, *, lanes: Genome = EMPTY_GENOME, frames: FrameAxis = (),
        semantic: bool, roles: tuple[int, ...] | None = None,
        auto: bool = False, set_mode: bool = False, capture: bool = False,
        seed: int | None = None,
    ) -> dict:
        request = {
            **self.context, **self.profile.wire_fields(), "CalcStats": calc_stats,
            "Settings": {
                **self.context["Settings"],
                "RandomSeed": self.battle_seed if seed is None else seed,
            },
            "UBExecTimeList": frames, "SemanUBExecTimeList": lanes,
            "SemanUBSequenceData": roles or (), "UseSequenceUB": roles is not None,
            "IsAutoMode": auto, "IsSetMode": set_mode, "IsSemanMode": semantic,
        }
        if capture and self.capture_frames:
            request["ActionProgressFrames"] = self.capture_frames
        return request

    async def _send(self, requests: list[dict]) -> list[dict]:
        try:
            response = await self.client.post(self.endpoint, json=requests)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise MzqError(f"MZQ 返回 HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise MzqError(f"MZQ 请求失败（{type(exc).__name__}）") from exc
        except ValueError as exc:
            raise MzqError("MZQ 回包无法解析为 JSON") from exc
        if not isinstance(payload, list) or len(payload) != len(requests):
            raise MzqError("MZQ 返回数量与请求不匹配")
        for index, (request, result) in enumerate(zip(requests, payload, strict=True)):
            if not isinstance(result, dict):
                raise MzqError(f"MZQ 回包第 {index + 1} 项必须是对象")
            if result.get("Success") is not True:
                raise MzqError(f"MZQ 战斗失败（批内第 {index + 1} 项）{result.get('Message') or ''}")
            if (
                type(result.get("CurrentSeed")) is not int
                or result["CurrentSeed"] != request["Settings"]["RandomSeed"]
            ):
                raise MzqError("MZQ 回包的 CurrentSeed 与固定战斗种子不符")
        return payload

    async def _one(self, request: dict) -> dict:
        return (await self._send([request]))[0]

    @staticmethod
    def _stats(response: dict) -> dict:
        return object_fields(response.get("Stats"), "Stats")

    def _evaluation(self, response: dict, *, capture: bool = False) -> Evaluation:
        stats = self._stats(response)
        raw = stats.get("ActionProgress") if capture else None
        fingerprint = None if raw is None else ActionProgressFingerprint.from_payload(raw)
        if fingerprint and (
            not self.unit_ids.issuperset(fingerprint.unit_ids)
            or not self.frame_set.issuperset(fingerprint.frames)
        ):
            raise ValueError("ActionProgress 的角色或采样帧与请求不符")
        counts = response.get("SemanUBNum")
        if not isinstance(counts, list):
            raise ValueError("MZQ 缺少 SemanUBNum 数组")
        return Evaluation(stats.get("TotalDamage"), response.get("BattleFrames"),
                          response.get("Result"), tuple(counts), fingerprint)

    async def _send_batches(self, requests: list[dict]) -> list[dict]:
        chunks = (
            requests[start:start + self.batch_size] for start in range(0, len(requests), self.batch_size)
        )

        results = []
        while window := list(islice(chunks, self.concurrency)):
            tasks = [asyncio.create_task(self._send(chunk)) for chunk in window]
            try:
                for batch in await asyncio.gather(*tasks):
                    results.extend(batch)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        return results

    def _aggregate(self, responses: list[dict], *, capture: bool = False) -> Evaluation:
        first = self._evaluation(responses[0], capture=capture)
        if len(responses) == 1:
            return first
        evaluations = [first] + [self._evaluation(response) for response in responses[1:]]
        samples = tuple(
            BattleSample(seed, item.damage, item.frames, item.result)
            for seed, item in zip(self.sample_seeds, evaluations, strict=True)
        )
        return Evaluation(
            first.damage, first.frames, first.result, first.ub_counts, first.fingerprint, samples,
        )

    async def evaluate(self, genomes: Sequence[Genome]) -> list[Evaluation]:
        requests = [
            self._request(
                FAST_BASE, lanes=genome, semantic=True,
                capture=index == 0, seed=seed,
            )
            for genome in genomes for index, seed in enumerate(self.sample_seeds)
        ]
        responses = await self._send_batches(requests)
        count = len(self.sample_seeds)
        return [
            self._aggregate(responses[start:start + count], capture=bool(self.capture_frames))
            for start in range(0, len(responses), count)
        ]

    async def convert(
        self, frames: FrameAxis, *, auto: bool = False, set_mode: bool = False,
    ) -> ConvertedAxis:
        result = await self._one(self._request(
            SEMANTIC_UB_CONVERSION, frames=frames, semantic=False, auto=auto, set_mode=set_mode,
        ))
        raw = object_fields(self._stats(result).get("UbConversion"), "Stats.UbConversion")
        lanes = genome_from_rows(raw.get("SemanUBExecTimeList"), trim_sentinels=True)
        roles = raw.get("SemanUBSequenceData")
        if self.profile.sequence_type is SemanSequenceType.SEQUENCE:
            return ConvertedAxis(lanes, sequence_roles(roles, lanes))
        if roles is not None:
            raise ValueError("LANES 转换回包意外含有 SemanUBSequenceData")
        return ConvertedAxis(lanes, None)

    async def initial_axis(self, timeline: Timeline) -> ConvertedAxis:
        if timeline.use_semantic:
            response = await self._one(self._request(
                UB_TIME, lanes=timeline.semantic, roles=timeline.sequence, semantic=True,
                auto=timeline.use_auto, set_mode=timeline.use_set,
            ))
            return await self.convert(frame_axis(self._stats(response).get("UBTime")))
        return await self.convert(timeline.frames, auto=timeline.use_auto, set_mode=timeline.use_set)

    async def replay(
        self, lanes: Genome, *, roles: tuple[int, ...] | None = None,
    ) -> tuple[Evaluation, FrameAxis]:
        responses = await self._send_batches([
            self._request(FAST_BASE, lanes=lanes, roles=roles, semantic=True, seed=seed)
            for seed in self.sample_seeds
        ])
        return self._aggregate(responses), frame_axis(self._stats(responses[0]).get("UBTime"))
