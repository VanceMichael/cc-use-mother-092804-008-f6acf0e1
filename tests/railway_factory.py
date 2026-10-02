"""测试夹具：脱敏的站点、车次、人员、物件与预约工厂。"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.railway.models import (
    Cage,
    Eligibility,
    EligibilityCategory,
    LuggageItem,
    Person,
    Pet,
    Relation,
    RelationKind,
    ServiceRequest,
    ServiceType,
    Staff,
    Station,
    Stop,
    TempCredential,
    TimeWindow,
    Train,
)

NKN = "NKN"  # 南京南
HZH = "HZH"  # 杭州东
WZN = "WZN"  # 温州南（不办宠物托运）
SZX = "SZX"  # 苏州北（示例车次不经停）


def dt(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute)


def build_network():
    from src.railway.policies import ServiceNetwork

    stations = [
        Station(NKN, "南京南"),
        Station(HZH, "杭州东"),
        Station(WZN, "温州南", pet_service=False),
        Station(SZX, "苏州北"),
    ]
    g1 = Train(
        "G1",
        (
            Stop(NKN, None, dt(5, 8, 0)),
            Stop(HZH, dt(5, 10, 0), dt(5, 10, 5)),
            Stop(WZN, dt(5, 12, 0), None),
        ),
    )
    g2 = Train(
        "G2",
        (
            Stop(NKN, None, dt(5, 9, 30)),
            Stop(HZH, dt(5, 11, 30), dt(5, 11, 35)),
            Stop(WZN, dt(5, 13, 30), None),
        ),
    )
    g3 = Train(
        "G3",
        (
            Stop(NKN, None, dt(5, 8, 30)),
            Stop(WZN, dt(5, 12, 30), None),
        ),
    )
    return ServiceNetwork(stations, [g1, g2, g3])


def build_staff():
    return [
        Staff("S-ESC-NKN", "赵护", NKN, "station_escort"),
        Staff("S-ESC-HZH", "钱护", HZH, "station_escort"),
        Staff("S-ESC-WZN", "孙护", WZN, "station_escort"),
        Staff("S-COND", "周车长", NKN, "train_conductor"),
        Staff("S-PET-NKN", "冯宠", NKN, "pet_clerk"),
        Staff("S-PET-HZH", "陈宠", HZH, "pet_clerk"),
        Staff("S-LUG-NKN", "褚行李", NKN, "luggage_clerk"),
        Staff("S-LUG-HZH", "卫行李", HZH, "luggage_clerk"),
        Staff("S-LUG-WZN", "沈行李", WZN, "luggage_clerk"),
        Staff("S-DUTY-NKN", "蒋值班", NKN, "duty_officer"),
    ]


STAFF_BY_ROLE_STATION = {
    ("station_escort", NKN): "S-ESC-NKN",
    ("station_escort", HZH): "S-ESC-HZH",
    ("station_escort", WZN): "S-ESC-WZN",
    ("train_conductor", NKN): "S-COND",
    ("train_conductor", HZH): "S-COND",
    ("pet_clerk", NKN): "S-PET-NKN",
    ("pet_clerk", HZH): "S-PET-HZH",
    ("luggage_clerk", NKN): "S-LUG-NKN",
    ("luggage_clerk", HZH): "S-LUG-HZH",
    ("luggage_clerk", WZN): "S-LUG-WZN",
    ("duty_officer", NKN): "S-DUTY-NKN",
}


def elderly(verified: bool = False) -> Person:
    eligibility = Eligibility(
        category=EligibilityCategory.ELDERLY,
        evidence="年龄资料（脱敏）",
        verified=verified,
        verified_by="客服甲" if verified else None,
        verified_at=dt(4, 10) if verified else None,
    )
    return Person("P-ELDER", "张大爷", eligibility)


def daughter() -> Person:
    return Person("P-DAUGHTER", "张女")


def pet_owner() -> Person:
    return Person("P-OWNER", "李先生")


def pet_receiver() -> Person:
    return Person("P-RECV", "王女士")


def cat(cage: Cage | None = None, weight: float = 4.0) -> Pet:
    return Pet(
        pet_id="PET-CAT",
        species="猫",
        weight_kg=weight,
        cage=cage or Cage(50, 35, 35),
        owner_id="P-OWNER",
    )


def cat_unaccompanied() -> Pet:
    pet = cat()
    return Pet(
        pet_id=pet.pet_id,
        species=pet.species,
        weight_kg=pet.weight_kg,
        cage=pet.cage,
        owner_id=pet.owner_id,
        unaccompanied=True,
        receiver_id="P-RECV",
    )


def dog() -> Pet:
    return Pet("PET-DOG", "犬", 15.0, Cage(70, 50, 50), owner_id="P-OWNER")


def snake() -> Pet:
    return Pet("PET-SNAKE", "蛇", 1.0, Cage(30, 20, 20), owner_id="P-OWNER")


def oversized_cat() -> Pet:
    return Pet("PET-CAT", "猫", 4.0, Cage(65, 35, 35), owner_id="P-OWNER")


LUGGAGE_ITEMS: dict[str, LuggageItem] = {
    "L1": LuggageItem("L1", "行李箱", 18.0),
    "L2": LuggageItem("L2", "纸箱", 8.0),
    "L3": LuggageItem("L3", "背包", 5.0),
    "L4": LuggageItem("L4", "编织袋", 10.0),
    "L5": LuggageItem("L5", "乐器盒", 7.0),
    "L6": LuggageItem("L6", "童车", 12.0),
}


def _window(day: int = 5) -> TimeWindow:
    return TimeWindow(dt(day, 6, 0), dt(day, 8, 0))


def key_passenger_request(
    request_id: str = "REQ-KP-1",
    *,
    verified: bool = False,
    train_code: str = "G1",
    route: tuple[str, str] = (NKN, WZN),
    with_companion: bool = True,
) -> ServiceRequest:
    persons = [elderly(verified)]
    relations = ()
    if with_companion:
        persons.append(daughter())
        relations = (Relation("P-DAUGHTER", "P-ELDER", RelationKind.COMPANION),)
    return ServiceRequest(
        request_id=request_id,
        passenger_id="P-DAUGHTER" if with_companion else "P-ELDER",
        train_code=train_code,
        service_type=ServiceType.KEY_PASSENGER,
        window=_window(),
        persons=tuple(persons),
        route=route,
        target_passenger_id="P-ELDER",
        relations=relations,
    )


def pet_request(
    request_id: str = "REQ-PET-1",
    *,
    pet: Pet | None = None,
    train_code: str = "G1",
    route: tuple[str, str] = (NKN, HZH),
    persons: tuple[Person, ...] | None = None,
) -> ServiceRequest:

    pet = pet or cat()
    if persons is None:
        people = [pet_owner()]
        if pet.receiver_id:
            people.append(pet_receiver())
        persons = tuple(people)
    relations = ()
    if pet.unaccompanied and pet.receiver_id:
        relations = (Relation(pet.receiver_id, pet.owner_id, RelationKind.RECEIVER),)
    return ServiceRequest(
        request_id=request_id,
        passenger_id=pet.owner_id,
        train_code=train_code,
        service_type=ServiceType.PET,
        window=_window(),
        persons=persons,
        route=route,
        pets=(pet,),
        pet_id=pet.pet_id,
        relations=relations,
    )


def luggage_request(
    request_id: str = "REQ-LUG-1",
    *,
    item_ids: tuple[str, ...] = ("L1", "L2", "L3"),
    train_code: str = "G1",
    route: tuple[str, str] = (NKN, WZN),
) -> ServiceRequest:
    from src.railway.models import ServiceType

    return ServiceRequest(
        request_id=request_id,
        passenger_id="P-OWNER",
        train_code=train_code,
        service_type=ServiceType.LUGGAGE,
        window=_window(),
        persons=(pet_owner(),),
        route=route,
        items=tuple(LUGGAGE_ITEMS[i] for i in sorted(set(item_ids))),
        item_ids=item_ids,
    )


def credential_request(
    request_id: str = "REQ-CRED-1",
    *,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    station: str = NKN,
    train_code: str = "G1",
) -> ServiceRequest:
    from src.railway.models import ServiceType

    valid_from = valid_from or dt(5, 6, 0)
    valid_until = valid_until or dt(5, 8, 0)
    credential = TempCredential(
        credential_id="CRED-1",
        passenger_id="P-OWNER",
        station_code=station,
        valid_from=valid_from,
        valid_until=valid_until,
    )
    return ServiceRequest(
        request_id=request_id,
        passenger_id="P-OWNER",
        train_code=train_code,
        service_type=ServiceType.TEMP_CREDENTIAL,
        window=_window(),
        persons=(pet_owner(),),
        credential=credential,
    )


def run_handovers(backend, reservation, *, counterparty: str = "旅客"):
    """按计划顺序派工并在窗口开始时刻完成全部环节，返回完成的交接记录。"""
    service_type = reservation.request.service_type
    handovers = []
    for task in reservation.plan_tasks(service_type):
        if not task.is_open:
            continue
        staff_id = STAFF_BY_ROLE_STATION[(task.role, task.station_code)]
        backend.assign_task(task.task_id, staff_id, task.window_start - timedelta(minutes=5))
        handovers.append(
            backend.complete_handover(task.task_id, counterparty, task.window_start)
        )
    return handovers
