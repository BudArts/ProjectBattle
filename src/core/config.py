"""Конфигурация системы управления обслуживанием парка ВСМ.

Единый источник правды для всех модулей: планировщиков, симулятора, диспетчера.
Все сценарные параметры вынесены сюда, чтобы их можно было менять без правки кода.
"""
from typing import Dict

from pydantic import BaseModel


class SystemConfig(BaseModel):
    """Конфигурация системы"""

    # ------------------------------------------------------------------ парк
    TOTAL_TRAINS: int = 43
    RESERVE_TRAINS: int = 4
    # Целевой KPI: не менее 38 составов (89%) доступны под движение
    REQUIRED_OPERATIONAL: int = 38
    TARGET_AVAILABILITY: float = 0.89

    # ------------------------------------------------------------------ депо
    DEPOT_CAPACITY: int = 8            # позиций (стойл) в депо
    WHEELSET_LATHE_CAPACITY: int = 1   # колёсных станков (токарных)

    # ------------------------------------------------------- временная шкала
    OPERATION_START_DATE: str = "2028-04-01"   # начало эксплуатации (Q2 2028)
    OPERATION_START_HOUR: int = 6              # начало движения
    OPERATION_END_HOUR: int = 24               # конец движения (ночное окно 00-06)

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

    # Допустимое отклонение пробега (+%)
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

    # Длительность работ (часы)
    SERVICE_DURATIONS: Dict[str, float] = {
        "IS100": 2.0,
        "IS200": 4.0,
        "IS510": 10.0,
        "IS520": 16.0,
        "IS530": 36.0,
        "IS540": 56.0,
        "IS600": 384.0,
        "IS700": 575.0,
        "wheelset_turning": 9.6,
    }

    # Внеплановые работы (добавка к плановым, %)
    UNPLANNED_OVERHEAD: Dict[str, float] = {
        "IS100": 0.30,
        "IS200": 0.30,
        "IS510": 0.30,
        "IS520": 0.30,
        "IS530": 0.30,
        "IS540": 0.30,
        "IS600": 0.10,
        "IS700": 0.10,
        "wheelset_turning": 0.20,
    }

    ANNUAL_MILEAGE_PER_TRAIN: float = 900_000

    @property
    def DAILY_MILEAGE_PER_TRAIN(self) -> float:
        return self.ANNUAL_MILEAGE_PER_TRAIN / 365

    # Какие работы планируются на каком уровне
    # Ночные (короткие) — тактический уровень; многодневные — стратегический.
    TACTICAL_SERVICES: tuple = ("IS100", "IS200", "wheelset_turning")
    STRATEGIC_SERVICES: tuple = ("IS510", "IS520", "IS530", "IS540", "IS600", "IS700")

    # Иерархия обслуживания (высший включает низшие)
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

    # Приоритеты обслуживания (1.0 = критично)
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
    }

    # ------------------------------------------------------------ модель отказов
    # Базовая вероятность внепланового отказа на один рейс
    FAILURE_BASE_PER_TRIP: float = 0.0020
    # Рост интенсивности при превышении пробегов (стохастика, зависящая от состояния)
    FAILURE_COEF_OVER_IS100: float = 2.0    # множитель на величину превышения (ratio-1)
    FAILURE_COEF_OVER_IS200: float = 1.0
    FAILURE_COEF_WEAR_IS540: float = 0.5    # накопленный износ до IS540

    # Длительность внепланового ремонта, часы
    REPAIR_HOURS_MIN: float = 12.0
    REPAIR_HOURS_MAX: float = 36.0

    # Горизонт метки для ML: отказ в течение N часов после снимка состояния
    ML_LABEL_HORIZON_HOURS: float = 72.0

    # ------------------------------------------------------------ сценарии
    SCENARIOS: Dict[str, Dict[str, float]] = {
        "normal": {"mileage": 1.0, "breakdown": 1.0},
        "high_load": {"mileage": 1.2, "breakdown": 1.0},
        "poor_maintenance": {"mileage": 1.0, "breakdown": 2.0},
    }


config = SystemConfig()
