"""Конфигурация системы управления обслуживанием парка ВСМ.

Единый источник правды. Числа, которые изменились после разбора чата
с заказчиком (АО «Сервис Высоких Скоростей»), помечены комментарием «чат».
Регламентные пробеги и длительности циклов оставлены из постановки.
"""
from typing import Dict, List, Tuple

from pydantic import BaseModel


class SystemConfig(BaseModel):
    """Конфигурация системы."""

    # ------------------------------------------------------------------ парк
    TOTAL_TRAINS: int = 43
    RESERVE_TRAINS: int = 4
    # Целевой KPI постановки: не менее 38 составов из 43.
    # 0,89 × 43 = 38,27, поэтому норма = floor(0,89 × введённых) = 38 на полном парке.
    REQUIRED_OPERATIONAL: int = 38
    TARGET_AVAILABILITY: float = 0.89

    # ------------------------------------------------------------------ депо
    # Чат: IS100–IS540 — 4 стойла по 2 состава = 8 ремонтных позиций.
    DEPOT_CAPACITY: int = 8
    # Чат: IS600–IS700 — отдельные пути с домкратами, 3 пути.
    JACK_TRACKS: int = 3
    # Чат: обточка на отдельном пути, тандемный станок. Один — базовый вариант.
    WHEELSET_LATHE_CAPACITY: int = 1
    # Чат: одномоментно на сервисе не более 5 поездов.
    MAX_ON_SERVICE: int = 5
    # Пример оборота сцепляет составы («1+2»). Обязательность эксперты не подтвердили:
    # если второго нет, нитка уходит одним составом.
    COUPLE_CONSISTS: bool = True
    # Чат, альтернативное прочтение: 5 × 24 ч = 120 поездо-часов в сутки.
    DAILY_SERVICE_HOUR_CAP: float = 120.0
    # Чат: экипировка в Москве — 2 пути, 4 поезда; в Петербурге так же.
    EQUIP_CAPACITY: int = 4
    # Внеплановый ремонт в пункте оборота (Москва), не путать со стойлами Обухово.
    MOSCOW_UNPLANNED_CAPACITY: int = 2

    # ------------------------------------------------------- временная шкала
    OPERATION_START_DATE: str = "2028-04-01"
    OPERATION_START_HOUR: int = 6
    OPERATION_END_HOUR: int = 24
    # Последнее отправление, после которого оборот успевает вернуться до полуночи.
    LAST_DEPARTURE_HOUR: float = 17.5

    # ------------------------------------------------------- полигон (чат)
    # Длина ВСМ Москва — Санкт-Петербург. «Пара» в ответе заказчика = оборот туда-обратно.
    ROUTE_KM: float = 679.0
    TRAVEL_HOURS: float = 2.25          # 2 ч 15 мин, средняя по перегону ~302 км/ч
    EQUIP_HOURS: float = 0.7            # экипировка и уборка в обороте (~40 мин)
    # 679 × 2 × 1,10 = 1 493,8; в ответе заказчика округлено до 1 493 км.
    RESERVE_MARGIN_KM: float = 1493.0
    RESERVE_PER_CITY: int = 2
    CARS_PER_TRAIN: int = 8
    WHEELSETS_PER_TRAIN: int = 32       # 16 моторных + 16 немоторных
    TURNING_HOURS_PER_CAR: float = 2.0  # чат: 120 мин простоя на вагон, тандем

    # ---------------------------------------------------- регламенты (км)
    MILEAGE_TRIGGERS: Dict[str, float] = {
        "IS100": 12_500,
        "IS200": 25_000,
        "IS510": 75_000,
        "IS520": 150_000,
        "IS530": 300_000,
        "IS540": 600_000,
        "IS600": 1_200_000,
        "IS700": 2_400_000,
        "wheelset_turning": 200_000,
    }

    # Допуск из таблицы регламента. Чат: сдвигать IS100/IS200 можно, но не дальше допуска.
    MILEAGE_TOLERANCE: Dict[str, float] = {
        "IS100": 0.10,
        "IS200": 0.20,
        "IS510": 0.20,
        "IS520": 0.20,
        "IS530": 0.20,
        "IS540": 0.20,
        "IS600": 0.20,
        "IS700": 0.20,
        "wheelset_turning": 0.10,
    }

    # Длительность плановых работ, часы (постановка). Обточка — на один вагон (чат).
    SERVICE_DURATIONS: Dict[str, float] = {
        "IS100": 2.0,
        "IS200": 4.0,
        "IS510": 10.0,
        "IS520": 16.0,
        "IS530": 36.0,
        "IS540": 56.0,
        "IS600": 384.0,
        "IS700": 575.0,
        "wheelset_turning": 2.0,
    }

    # Доля дополнительных работ сверх плановой длительности.
    UNPLANNED_OVERHEAD: Dict[str, float] = {
        "IS100": 0.30,
        "IS200": 0.30,
        "IS510": 0.30,
        "IS520": 0.30,
        "IS530": 0.30,
        "IS540": 0.30,
        "IS600": 0.10,
        "IS700": 0.10,
        "wheelset_turning": 0.10,
    }

    # Чат: циклы IS510–IS540 можно делить на блоки.
    # interruptible=False — технологическая цепочка, поезд нельзя выпустить,
    # пока блок не закрыт (пример заказчика: снял фильтр — поставь новый).
    # Сумма часов блока = SERVICE_DURATIONS.
    SERVICE_BLOCKS: Dict[str, List[Tuple[str, float, bool]]] = {
        "IS100": [("Осмотр", 2.0, True)],
        "IS200": [("Инспекция", 4.0, True)],
        "IS510": [
            ("Диагностика и осмотр", 4.0, True),
            ("Регламентные замены", 6.0, False),
        ],
        "IS520": [
            ("Осмотр узлов", 4.0, True),
            ("Тормозная система", 6.0, False),
            ("Тяговое оборудование", 6.0, False),
        ],
        "IS530": [
            ("Экипажная часть", 12.0, False),
            ("Пневматика, фильтр компрессора", 12.0, False),
            ("Контрольные испытания", 12.0, False),
        ],
        "IS540": [
            ("Ревизия тележек", 20.0, False),
            ("Силовая схема", 20.0, False),
            ("Контрольные испытания", 16.0, False),
        ],
        "IS600": [("Ревизия на домкратах", 384.0, False)],
        "IS700": [("Ревизия на домкратах", 575.0, False)],
    }

    # Чат: предохранительный клапан — раз в 365 суток, срок пропускать нельзя.
    CALENDAR_TASKS: Dict[str, Dict[str, float]] = {
        "safety_valve": {"interval_days": 365.0, "duration_hours": 2.0},
    }

    # Ориентир стратегического плана. Подставляется из суточного оборота
    # (сцепка, как в примере): около 817 тыс. км, а не круглая цифра 900 тыс.
    ANNUAL_MILEAGE_PER_TRAIN: float = 817_000

    @property
    def DAILY_MILEAGE_PER_TRAIN(self) -> float:
        return self.ANNUAL_MILEAGE_PER_TRAIN / 365

    @property
    def PAIR_KM(self) -> float:
        return self.ROUTE_KM * 2

    @property
    def PAIR_HOURS(self) -> float:
        """Оборот туда-обратно с двумя экипировками, без стоянки в депо."""
        return self.TRAVEL_HOURS * 2 + self.EQUIP_HOURS * 2

    # Какие работы планируются на каком уровне
    TACTICAL_SERVICES: tuple = ("IS100", "IS200", "wheelset_turning")
    STRATEGIC_SERVICES: tuple = ("IS510", "IS520", "IS530", "IS540", "IS600", "IS700")
    JACK_SERVICES: tuple = ("IS600", "IS700")

    SERVICE_HIERARCHY: Dict[str, list] = {
        "IS700": ["IS700", "IS600", "IS540", "IS530", "IS520", "IS510", "IS200", "IS100"],
        "IS600": ["IS600", "IS540", "IS530", "IS520", "IS510", "IS200", "IS100"],
        "IS540": ["IS540", "IS530", "IS520", "IS510", "IS200", "IS100"],
        "IS530": ["IS530", "IS520", "IS510", "IS200", "IS100"],
        "IS520": ["IS520", "IS510", "IS200", "IS100"],
        "IS510": ["IS510", "IS200", "IS100"],
        "IS200": ["IS200", "IS100"],
        "IS100": ["IS100"],
        "wheelset_turning": ["wheelset_turning"],
    }

    SERVICE_CRITICALITY: Dict[str, float] = {
        "IS100": 0.9,
        "IS200": 0.9,
        "IS510": 0.8,
        "IS520": 0.8,
        "IS530": 0.7,
        "IS540": 0.7,
        "IS600": 0.6,
        "IS700": 0.5,
        "wheelset_turning": 1.0,
        "safety_valve": 1.0,
    }

    # ------------------------------------------------------------ модель отказов
    # Потолок непланового ремонта в литературе — λ ≤ 3,5 на млн км
    # (Галахов, Лакин, Скворцов, 2023). Это предел, не ожидание.
    # Рабочие интенсивности ниже потолка. Подобраны прогоном полного парка
    # (seed 42, 365 суток, планирование включено): 185 внеплановых событий.
    # Это около 9 на млн км пробега модели, а не регуляторный потолок 3,5
    # и не артефакт в 500. Реактивный режим на тех же числах даёт больше.
    LINE_FAILURE_PER_MILLION_KM: float = 2.3
    UNPLANNED_PER_MILLION_KM: float = 6.1
    # Рост интенсивности при приближении к порогу и при просрочке.
    FAILURE_COEF_OVER_IS100: float = 1.6
    FAILURE_COEF_OVER_IS200: float = 0.6
    FAILURE_COEF_WEAR_IS540: float = 0.8
    # Совместимость со старыми полями (на один рейс, если кто-то читает напрямую).
    FAILURE_BASE_PER_TRIP: float = 0.012

    REPAIR_HOURS_MIN: float = 8.0
    REPAIR_HOURS_MAX: float = 24.0
    MOSCOW_REPAIR_HOURS_MIN: float = 6.0
    MOSCOW_REPAIR_HOURS_MAX: float = 14.0

    # Узлы. Доли — рабочая гипотеза (статистики заказчик не дал): у «Сапсана»
    # доминирует износ колёс, далее тормоз, токоприёмник, тяга, двери.
    # moscow=True — внеплановую работу этого узла можно закрыть в пункте оборота.
    FAILURE_NODES: List[Dict] = [
        {"code": "wheel", "weight": 0.28, "moscow": True},
        {"code": "brake", "weight": 0.16, "moscow": True},
        {"code": "pantograph", "weight": 0.12, "moscow": False},
        {"code": "traction", "weight": 0.12, "moscow": False},
        {"code": "doors", "weight": 0.12, "moscow": True},
        {"code": "hvac", "weight": 0.08, "moscow": True},
        {"code": "electronics", "weight": 0.08, "moscow": False},
        {"code": "other", "weight": 0.04, "moscow": True},
    ]

    ML_LABEL_HORIZON_HOURS: float = 72.0

    SCENARIOS: Dict[str, Dict[str, float]] = {
        "normal": {"mileage": 1.0, "breakdown": 1.0},
        "high_load": {"mileage": 1.2, "breakdown": 1.0},
        "poor_maintenance": {"mileage": 1.0, "breakdown": 2.0},
    }


config = SystemConfig()
