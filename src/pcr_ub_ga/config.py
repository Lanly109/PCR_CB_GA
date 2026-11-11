"""Validated, serializable configuration for the command-line application."""

from dataclasses import asdict, dataclass, field

from .engine import GAConfig
from .fingerprint import finite_number
from .mzq import simulation_url
from .semantics import EMPTY_GENOME, Genome, SemanticConversionProfile, genome_from_rows, integer

DEFAULT_ENDPOINT = "http://127.0.0.1:8080/simulate"
DEFAULT_EVALUATION_REPEATS = 5


@dataclass(frozen=True, slots=True)
class RunConfig:
    endpoint: str = DEFAULT_ENDPOINT
    battle_time: int = 90
    battle_seed: int | None = None
    batch_size: int = 16
    concurrency: int = 2
    timeout: float = 120
    evaluation_repeats: int = DEFAULT_EVALUATION_REPEATS
    semantic: SemanticConversionProfile = field(default_factory=SemanticConversionProfile)
    ga: GAConfig = field(default_factory=GAConfig)
    fixed_ub_counts: tuple[int, ...] = (0,) * 5
    extra_genes: Genome = EMPTY_GENOME
    legacy_ub: bool | None = None
    own_skill: bool = False
    other_skill: bool = False
    delay: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint", simulation_url(self.endpoint))
        if integer(self.battle_time, "battle_time", 1) > 90:
            raise ValueError("battle_time 不能超过 90 秒")
        if self.battle_seed is not None and type(self.battle_seed) is not int:
            raise ValueError("battle_seed 必须是整数")
        integer(self.batch_size, "batch_size", 1)
        integer(self.concurrency, "concurrency", 1)
        finite_number(self.timeout, "timeout", 0.001)
        if integer(self.evaluation_repeats, "evaluation_repeats", 1) > 100:
            raise ValueError("evaluation_repeats 不能超过 100")
        if len(self.fixed_ub_counts) != 5:
            raise ValueError("fixed_ub_counts 必须包含五个角色的计数")
        object.__setattr__(self, "fixed_ub_counts", tuple(
            integer(count, "fixed_ub_counts") for count in self.fixed_ub_counts
        ))
        object.__setattr__(self, "extra_genes", genome_from_rows(self.extra_genes))
        for name in ("legacy_ub", "own_skill", "other_skill"):
            value = getattr(self, name)
            if type(value) is not bool and (name != "legacy_ub" or value is not None):
                raise ValueError(f"{name} 必须是 true 或 false")
        if integer(self.delay, "delay") >= 10000:
            raise ValueError("delay 必须小于 10000，避免混入语义编码区间")

    @classmethod
    def from_dict(cls, data: dict) -> "RunConfig":
        try:
            return cls(**{
                **data, "ga": GAConfig(**data.get("ga", {})),
                "semantic": SemanticConversionProfile(**data.get("semantic", {})),
            })
        except (TypeError, AttributeError) as exc:
            raise ValueError(f"配置字段格式错误：{exc}") from exc

    def to_dict(self) -> dict:
        return asdict(self)
