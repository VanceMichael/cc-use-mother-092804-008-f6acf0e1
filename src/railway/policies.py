"""服务资格与托运策略：物种尺寸、重点旅客资格、件数与路线、站点车次网络。"""

from __future__ import annotations

from dataclasses import dataclass

from src.railway.errors import PolicyViolation, ValidationError
from src.railway.models import (
    EligibilityCategory,
    Person,
    Pet,
    Station,
    Train,
)


@dataclass(frozen=True)
class SpeciesRule:
    """单一物种的托运规则：最大重量、航空箱单边与三边之和上限。"""

    species: str
    max_weight_kg: float
    max_edge_cm: float
    max_dimension_sum_cm: float

    def check(self, pet: Pet) -> None:
        if pet.species != self.species:
            raise PolicyViolation(f"物种规则不匹配：{pet.species}")
        if pet.weight_kg > self.max_weight_kg:
            raise PolicyViolation(
                f"{self.species}重量 {pet.weight_kg}kg 超过上限 {self.max_weight_kg}kg"
            )
        edges = pet.cage.dimensions()
        if max(edges) > self.max_edge_cm:
            raise PolicyViolation(
                f"{self.species}航空箱单边 {max(edges)}cm 超过上限 {self.max_edge_cm}cm"
            )
        total = sum(edges)
        if total > self.max_dimension_sum_cm:
            raise PolicyViolation(
                f"{self.species}航空箱三边之和 {total}cm 超过上限 {self.max_dimension_sum_cm}cm"
            )


# 平台公开规则的脱敏常量：以小型常见宠物为主，蛇类等禁运。
SPECIES_RULES: dict[str, SpeciesRule] = {
    "猫": SpeciesRule("猫", max_weight_kg=10, max_edge_cm=60, max_dimension_sum_cm=120),
    "犬": SpeciesRule("犬", max_weight_kg=20, max_edge_cm=80, max_dimension_sum_cm=160),
    "兔": SpeciesRule("兔", max_weight_kg=6, max_edge_cm=50, max_dimension_sum_cm=100),
}

FORBIDDEN_SPECIES_HINT = "蛇、猛禽等物种不予托运"

MAX_LUGGAGE_PIECES = 5
MIN_LUGGAGE_PIECES = 1


class ServiceNetwork:
    """车站与车次资料；所有路线判断都经过网络，防止跨站错运。"""

    def __init__(self, stations: list[Station], trains: list[Train]) -> None:
        self._stations = {s.code: s for s in stations}
        self._trains = {t.code: t for t in trains}

    def station(self, code: str) -> Station:
        try:
            return self._stations[code]
        except KeyError:
            raise ValidationError(f"站点不存在：{code}") from None

    def train(self, code: str) -> Train:
        try:
            return self._trains[code]
        except KeyError:
            raise ValidationError(f"车次不存在：{code}") from None

    def ensure_route(self, train: Train, origin: str, destination: str) -> None:
        """路线必须沿同一车次停站顺序，且发站在前。"""
        self.station(origin)
        self.station(destination)
        if not train.serves(origin) or not train.serves(destination):
            raise PolicyViolation(f"车次 {train.code} 不经停路线端点 {origin}->{destination}")
        try:
            train.between(origin, destination)
        except ValueError:
            raise PolicyViolation(f"路线方向错误：{origin}->{destination}") from None

    def ensure_pet_stations(self, origin: str, destination: str) -> None:
        for code in (origin, destination):
            if not self.station(code).pet_service:
                raise PolicyViolation(f"车站 {code} 未开办宠物托运")


def check_eligibility(person: Person) -> EligibilityCategory:
    """重点旅客必须有明确资格类别；是否已现场核验由受理流程确认。"""
    if person.eligibility is None:
        raise PolicyViolation(f"旅客 {person.person_id} 缺少重点旅客资格资料")
    return person.eligibility.category


def check_pet(pet: Pet) -> None:
    """物种白名单 + 尺寸重量；单独出行必须有到站接收人。"""
    rule = SPECIES_RULES.get(pet.species)
    if rule is None:
        raise PolicyViolation(f"暂不受理 {pet.species} 托运（{FORBIDDEN_SPECIES_HINT}）")
    rule.check(pet)
    if pet.weight_kg <= 0:
        raise PolicyViolation("宠物体重必须为正数")
    if pet.unaccompanied and not pet.receiver_id:
        raise PolicyViolation("宠物单独出行必须指定到站接收人")


def check_luggage(piece_count: int, train: Train, origin: str, destination: str) -> None:
    """件数上下限 + 沿车次的合法路线。"""
    if not (MIN_LUGGAGE_PIECES <= piece_count <= MAX_LUGGAGE_PIECES):
        raise PolicyViolation(
            f"行李件数 {piece_count} 超出每次 {MIN_LUGGAGE_PIECES}-{MAX_LUGGAGE_PIECES} 件限制"
        )
    if train.station_index(origin) >= train.station_index(destination):
        raise PolicyViolation(f"行李运输路线方向错误：{origin}->{destination}")
