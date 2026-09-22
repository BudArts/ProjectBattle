"""Доменные сущности: состояние поезда, состояние депо.

Исправлено: TrainState собирается по всем 8 счётчикам пробега
(ранее диспетчер видел только 4 и «не замечал» IS520–IS700).
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .config import config
from .database import MILEAGE_FIELDS


@dataclass
class TrainState:
    """Состояние поезда в конкретный момент"""
    train_id: int
    number: str
    status: str
    total_mileage: float
    mileages: Dict[str, float] = field(default_factory=dict)  # {"since_is100": ...}

    @classmethod
    def from_orm(cls, train) -> "TrainState":
        return cls(
            train_id=train.id,
            number=train.number,
            status=train.status,
            total_mileage=train.total_mileage,
            mileages={f"since_{k.lower()}": getattr(train, f) or 0.0
                      for k, f in MILEAGE_FIELDS.items()},
        )

    def since(self, service_type: str) -> float:
        return self.mileages.get(f"since_{service_type.lower()}", 0.0)

    def ratio(self, service_type: str) -> float:
        trigger = config.MILEAGE_TRIGGERS.get(service_type)
        if not trigger:
            return 0.0
        return self.since(service_type) / trigger

    def services_due(self, urgency_threshold: float = 0.9,
                     only: Optional[tuple] = None) -> List[str]:
        """Типы работ, у которых пробег достиг порога (в %)."""
        due = []
        types = only if only is not None else config.MILEAGE_TRIGGERS.keys()
        for service_type in types:
            ratio = self.ratio(service_type)
            if ratio >= urgency_threshold:
                due.append((ratio * config.SERVICE_CRITICALITY.get(service_type, 1.0),
                            ratio, service_type))
        due.sort(reverse=True)
        return [t for _, _, t in due]

    def next_service_type(self, only: Optional[tuple] = None) -> Optional[str]:
        """Наиболее срочная работа из достигших порога 85%.

        Отбор по баллу срочности (доля порога × критичность), поэтому обточка
        колёсных пар не вытесняется вечно более частыми IS100/IS200.
        Исключение — иерархия: если наступил IS200, отдельный IS100 не нужен.
        """
        services = self.services_due(0.85, only=only)
        if not services:
            return None
        if "IS200" in services and "IS100" in services:
            services.remove("IS100")
        return services[0]

    def can_defer_service(self, service_type: str, days: int = 7) -> bool:
        """Можно ли отложить обслуживание на N дней без выхода за допуск."""
        trigger = config.MILEAGE_TRIGGERS[service_type]
        tolerance = config.MILEAGE_TOLERANCE[service_type]
        max_mileage = trigger * (1 + tolerance)
        projected = self.since(service_type) + config.DAILY_MILEAGE_PER_TRAIN * days
        return projected < max_mileage


@dataclass
class MaintenanceSlot:
    """Слот для обслуживания в депо"""
    start_time: datetime
    end_time: datetime
    service_type: str
    train_id: int
    bay: int

    def duration_hours(self) -> float:
        return (self.end_time - self.start_time).total_seconds() / 3600

    def overlaps(self, other: "MaintenanceSlot") -> bool:
        return (self.start_time < other.end_time and self.end_time > other.start_time
                and self.bay == other.bay)


class DepotState:
    """Состояние депо (используется для пост-обработки и проверки расписаний)."""

    def __init__(self, capacity: int = None, wheelset_capacity: int = None):
        self.capacity = capacity or config.DEPOT_CAPACITY
        self.wheelset_capacity = wheelset_capacity or config.WHEELSET_LATHE_CAPACITY
        self.scheduled_slots: List[MaintenanceSlot] = []

    def is_available(self, start: datetime, duration_hours: float,
                     needs_wheelset: bool = False) -> Optional[int]:
        end = start + timedelta(hours=duration_hours)
        if needs_wheelset:
            for slot in self.scheduled_slots:
                if slot.bay == 9 and slot.start_time < end and slot.end_time > start:
                    return None
            return 9
        for bay in range(1, self.capacity + 1):
            if not any(s.bay == bay and s.start_time < end and s.end_time > start
                       for s in self.scheduled_slots):
                return bay
        return None

    def add_slot(self, slot: MaintenanceSlot):
        self.scheduled_slots.append(slot)

    def assign_bay(self, start: datetime, duration_hours: float) -> Optional[int]:
        """Первая свободная позиция (first-fit)."""
        return self.is_available(start, duration_hours)

    def utilization(self, start: datetime, end: datetime) -> float:
        total_hours = (end - start).total_seconds() / 3600
        occupied = 0.0
        for slot in self.scheduled_slots:
            if slot.start_time < end and slot.end_time > start:
                occupied += (min(slot.end_time, end) -
                             max(slot.start_time, start)).total_seconds() / 3600
        return occupied / (total_hours * self.capacity) if total_hours else 0.0

    def max_concurrent(self, start: datetime, end: datetime) -> int:
        """Максимум одновременно занятых позиций в окне."""
        points = sorted({start, end} | {s.start_time for s in self.scheduled_slots}
                        | {s.end_time for s in self.scheduled_slots})
        best = 0
        for p in points:
            if not (start <= p < end):
                continue
            best = max(best, sum(1 for s in self.scheduled_slots
                                 if s.start_time <= p < s.end_time))
        return best
