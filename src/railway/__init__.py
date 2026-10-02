"""铁路便民服务预约与现场交接后端。

领域层（模型、策略、受理规划）与应用层（值班受理、现场交接、视图）均在本包内。
"""

from src.railway.backend import RailwayServiceBackend, SubmitOutcome, SubmitResult
from src.railway.errors import (
    ConflictError,
    DomainError,
    PolicyViolation,
    ValidationError,
)
from src.railway.models import (
    Cage,
    Eligibility,
    EligibilityCategory,
    HandoverRecord,
    LuggageItem,
    Person,
    Pet,
    Relation,
    RelationKind,
    ServicePlan,
    ServiceRequest,
    ServiceStatus,
    ServiceTask,
    ServiceType,
    Staff,
    Station,
    Stop,
    TaskKind,
    TaskStatus,
    TempCredential,
    TimeWindow,
    Train,
)
from src.railway.policies import (
    MAX_LUGGAGE_PIECES,
    ServiceNetwork,
    SpeciesRule,
    SPECIES_RULES,
)
from src.railway.reservation import Planner, Reservation
from src.railway.views import ChainEvent, ChainStep, ReservationChain, StaffTaskView

__all__ = [
    "RailwayServiceBackend",
    "SubmitOutcome",
    "SubmitResult",
    "DomainError",
    "ValidationError",
    "PolicyViolation",
    "ConflictError",
    "Cage",
    "Eligibility",
    "EligibilityCategory",
    "HandoverRecord",
    "LuggageItem",
    "Person",
    "Pet",
    "Relation",
    "RelationKind",
    "Reservation",
    "ServicePlan",
    "ServiceRequest",
    "ServiceStatus",
    "ServiceTask",
    "ServiceType",
    "Staff",
    "Station",
    "Stop",
    "TaskKind",
    "TaskStatus",
    "TempCredential",
    "TimeWindow",
    "Train",
    "ServiceNetwork",
    "SpeciesRule",
    "SPECIES_RULES",
    "MAX_LUGGAGE_PIECES",
    "Planner",
    "ChainEvent",
    "ChainStep",
    "ReservationChain",
    "StaffTaskView",
]
