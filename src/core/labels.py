"""Русские подписи для таблиц, графиков и статусов.

Внутренние коды в коде остаются машинными. На экран и в выгрузки
попадает только этот словарь — без operational / scheduled / wheelset_turning.
"""
from typing import Optional


STATUS_RU = {
    "operational": "В эксплуатации",
    "maintenance": "Плановое ТО",
    "broken": "Внеплановый ремонт",
    "reserve": "Горячий резерв",
    "not_delivered": "Не поставлен",
    "equipping": "Экипировка и уборка",
    "line": "В рейсе",
    "waiting": "Ожидание свободной позиции",
}

REASON_RU = {
    "scheduled": "Плановое",
    "breakdown": "Внеплановое",
    "calendar": "Календарное",
}

EVENT_STATUS_RU = {
    "planned": "Запланировано",
    "in_progress": "Выполняется",
    "completed": "Выполнено",
    "postponed": "Перенесено",
    "cancelled": "Отменено",
}

SERVICE_RU = {
    "IS100": "IS100 · осмотр",
    "IS200": "IS200 · инспекция",
    "IS510": "IS510 · обслуживание",
    "IS520": "IS520 · обслуживание",
    "IS530": "IS530 · обслуживание",
    "IS540": "IS540 · обслуживание",
    "IS600": "IS600 · ревизия",
    "IS700": "IS700 · ревизия",
    "wheelset_turning": "Обточка колёсных пар",
    "emergency_repair": "Внеплановый ремонт",
    "wheelset_replacement": "Замена колёсной пары",
    "safety_valve": "Проверка предохранительного клапана",
    "equipping": "Экипировка и уборка",
}

LOCATION_RU = {
    "spb": "Санкт-Петербург · Обухово",
    "msk": "Москва · пункт оборота",
    "to_msk": "Рейс в Москву",
    "to_spb": "Рейс в Санкт-Петербург",
}

SCENARIO_RU = {
    "normal": "Нормальная эксплуатация",
    "high_load": "Высокая нагрузка (+20% рейсов)",
    "poor_maintenance": "Повышенная аварийность (×2)",
}

NODE_RU = {
    "wheel": "Колёсные пары и тележки",
    "brake": "Тормозная система",
    "pantograph": "Токоприёмник",
    "traction": "Тяговое оборудование",
    "doors": "Двери",
    "hvac": "Климатическая установка",
    "electronics": "Электроника управления",
    "other": "Прочее",
}


def ru(mapping: dict, code: Optional[str], default: str = "—") -> str:
    if code is None or code == "":
        return default
    return mapping.get(code, str(code))


def ru_status(code: Optional[str]) -> str:
    return ru(STATUS_RU, code)


def ru_service(code: Optional[str]) -> str:
    return ru(SERVICE_RU, code)


def ru_reason(code: Optional[str]) -> str:
    return ru(REASON_RU, code)


def ru_event_status(code: Optional[str]) -> str:
    return ru(EVENT_STATUS_RU, code)


def ru_location(code: Optional[str]) -> str:
    return ru(LOCATION_RU, code)


def ru_node(code: Optional[str]) -> str:
    return ru(NODE_RU, code)
