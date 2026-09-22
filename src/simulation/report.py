"""Формулы KPI и расчёт по станку, не зависящий от случайного прогона.

Нужен и дашборду (вкладка «Разбор»), и симулятору (сверка с фактом).
"""
import math
from typing import Dict, List, Optional

from src.core.config import config


def required_count(commissioned: int) -> int:
    """Сколько составов должно быть доступно.

    На полном парке floor(0,89 × 43) = 38 — это и есть «не менее 38 (89%)».
    Пока парк вводится по одному в месяц, норма считается от уже поставленных,
    иначе в знаменатель попадают поезда, которых ещё нет.
    """
    if commissioned <= 0:
        return 0
    return max(1, math.floor(commissioned * config.TARGET_AVAILABILITY + 1e-9))


def slack_count(commissioned: int) -> int:
    """Сколько составов можно одновременно снять с перевозки, не уронив норму."""
    if commissioned <= 0:
        return 0
    return max(0, commissioned - required_count(commissioned))


def service_slot_limit(commissioned: int) -> int:
    """Жёсткий потолок: и норма готовности, и «не более 5» из чата."""
    return max(0, min(config.MAX_ON_SERVICE, slack_count(commissioned)))


def hard_limit_km(service_type: str) -> float:
    trigger = config.MILEAGE_TRIGGERS[service_type]
    return trigger * (1.0 + config.MILEAGE_TOLERANCE[service_type])


def lathe_cohort_report(daily_km: float = 2500.0, lathes: int = 1,
                        night_only: bool = True) -> Dict:
    """Первая волна обточки: 6 составов поставлены в один день.

    Заказчик прямо попросил увидеть момент, когда одного тандемного станка
    перестаёт хватать. Считаем от графика поставки, а не от удачного seed.
    """
    cars = config.CARS_PER_TRAIN
    hours_per_car = config.TURNING_HOURS_PER_CAR
    cohort = 6  # составы №1–6, ввод 01.04.2028
    demand_hours = cohort * cars * hours_per_car  # 6 × 8 × 2 = 96 ч

    trigger = config.MILEAGE_TRIGGERS["wheelset_turning"]
    tol = config.MILEAGE_TOLERANCE["wheelset_turning"]
    # Планировать начинаем с 85% порога, жёсткий предел — порог × (1 + допуск).
    window_km = trigger * (1.0 + tol) - trigger * 0.85
    window_days = window_km / daily_km if daily_km else 0.0
    night_hours = float(config.OPERATION_START_HOUR)  # 00:00–06:00
    day_hours = 24.0
    cap_hours = (night_hours if night_only else day_hours) * window_days * lathes
    slack = cap_hours - demand_hours
    start_day = (trigger * 0.85) / daily_km if daily_km else 0.0
    deadline_day = (trigger * (1.0 + tol)) / daily_km if daily_km else 0.0

    if night_only and lathes == 1 and slack <= 0.15 * cap_hours:
        verdict = (
            "Одного станка впритык хватает только если гонять его каждую ночь "
            "без единого срыва. Для дневной готовности это рискованно."
        )
        recommend = True
    elif slack < 0:
        verdict = "Мощности не хватает: часть вагонов выйдет за допуск пробега."
        recommend = True
    else:
        verdict = "Запас есть, второй станок на этой волне не обязателен."
        recommend = False

    return {
        "cohort_trains": cohort,
        "cars": cars,
        "hours_per_car": hours_per_car,
        "demand_hours": demand_hours,
        "window_days": window_days,
        "window_km": window_km,
        "capacity_hours": cap_hours,
        "slack_hours": slack,
        "slack_ratio": (slack / cap_hours) if cap_hours else 0.0,
        "start_day": start_day,
        "deadline_day": deadline_day,
        "night_only": night_only,
        "lathes": lathes,
        "recommend_second": recommend,
        "verdict": verdict,
        "night_hours": night_hours,
    }


def lathe_recommendation_text(report: Optional[Dict] = None,
                              observed_day: Optional[float] = None) -> str:
    report = report or lathe_cohort_report()
    start = report["start_day"]
    deadline = report["deadline_day"]
    lines = [
        f"Первая волна — {report['cohort_trains']} состава единовременного ввода, "
        f"{report['cars']} вагонов × {report['hours_per_car']:.0f} ч = "
        f"{report['demand_hours']:.0f} ч станка.",
        f"Окно от 85% порога до жёсткого допуска: {report['window_days']:.0f} суток "
        f"(примерно с {start:.0f}-х по {deadline:.0f}-е сутки).",
        f"Ночная мощность одного станка в этом окне: {report['capacity_hours']:.0f} ч, "
        f"запас {report['slack_hours']:.0f} ч ({report['slack_ratio']:.0%}).",
        report["verdict"],
    ]
    if report["recommend_second"]:
        lines.append(
            f"Второй станок нужен к {start:.0f}-м суткам — до входа первой волны "
            "в окно обточки. Иначе обточка вылезает в день и снимает составы с графика."
        )
    if observed_day is not None:
        lines.append(
            f"В прогоне дефицит станка (ожидание дольше допуска или дневная обточка) "
            f"впервые зафиксирован на {observed_day:.0f}-е сутки."
        )
    return " ".join(lines)


READINESS_FORMULA = (
    "Кг = (сумма часов, когда состав можно выдать в перевозку) / "
    "(сумма часов с момента его поставки). "
    "В числителе — эксплуатация, рейс и горячий резерв. "
    "Не входят плановое ТО, внеплановый ремонт и ожидание свободной позиции. "
    "Норма на текущий парк: не меньше floor(0,89 × поставленных), "
    "на полном парке это 38 из 43. "
    "Экипировка оборота — часть графика, её часы показываем отдельно и в Кг не вычитаем. "
    "Сервисные часы — только плановое ТО и дополнительные работы, без мойки и экипировки."
)


def monthly_load_cv(hours_by_month: List[float]) -> float:
    vals = [h for h in hours_by_month if h is not None]
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    if mean <= 1e-9:
        return 0.0
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return math.sqrt(var) / mean
