"""Суточный оборот из примера заказчика.

Источник: таблица «Суточный оборот», которую эксперты приложили к вопросу
о частоте отправления. Сцепка в примере записана как «1+2». Обязательность
сцепки эксперты не подтвердили — одиночный рейс в модели разрешён, если
второго состава нет. «Пара» в ответе про резерв — это 679×2 км, не сцепка.

Числа 16,4 и 4,4 стоят в той же колонке, что и 679 км перегона: это
пробег подачи, не минуты. Времени подачи в таблице нет.
"""
from typing import Dict, List, Optional

DEPOT_ACCESS_KM = 16.4
MOSCOW_TECH_KM = 4.4
ROUTE_KM = 679.0

# Составы, которые в примере сутки проводят в цикле
# «экипировка → ТО → подготовка», а не на нитках.
EXAMPLE_SERVICE_IDS = (13, 22, 31, 32, 43)


def _hm(hour: int, minute: int = 0) -> float:
    return hour + minute / 60.0


def _leg(train: str, origin: str, dep_h: int, dep_m: int, arr_h: int, arr_m: int,
         approach_km: float = 0.0, after_km: float = 0.0) -> Dict:
    dep = _hm(dep_h, dep_m)
    arr = _hm(arr_h, arr_m)
    if arr <= dep:
        arr += 24.0
    return {
        "train": train,
        "origin": origin,
        "dest": "msk" if origin == "spb" else "spb",
        "dep": dep,
        "arr": arr,
        "km": ROUTE_KM,
        "approach_km": approach_km,
        "after_km": after_km,
    }


def _duty(code: str, consists: List[int], start: str, end: str,
          legs: List[Dict], note: str = "") -> Dict:
    return {
        "id": code,
        "consists": consists,
        "start_city": start,
        "end_city": end,
        "legs": legs,
        "revenue_km": ROUTE_KM * len(legs),
        "note": note,
    }


# Подача в депо и на техническую станцию приписана к соседней нитке:
# время уже сидит в графике, в модель добавляется только пробег.
OPERATING_DUTIES: List[Dict] = [
    _duty("1+2", [1, 2], "spb", "msk", [
        _leg("761", "spb", 8, 0, 10, 10, approach_km=DEPOT_ACCESS_KM, after_km=MOSCOW_TECH_KM),
        _leg("784", "msk", 13, 30, 15, 50, approach_km=MOSCOW_TECH_KM, after_km=DEPOT_ACCESS_KM),
        _leg("777", "spb", 17, 30, 19, 50, approach_km=DEPOT_ACCESS_KM, after_km=MOSCOW_TECH_KM),
    ]),
    _duty("3+4", [3, 4], "msk", "msk", [
        _leg("746", "msk", 7, 0, 9, 20, approach_km=MOSCOW_TECH_KM),
        _leg("749", "spb", 10, 30, 12, 50, after_km=MOSCOW_TECH_KM),
    ]),
    _duty("5+6", [5, 6], "msk", "msk", [
        _leg("758", "msk", 8, 0, 10, 10, approach_km=MOSCOW_TECH_KM),
        _leg("707", "spb", 11, 30, 13, 50, after_km=MOSCOW_TECH_KM),
        _leg("708", "msk", 18, 0, 20, 10, approach_km=MOSCOW_TECH_KM),
        _leg("725", "spb", 21, 30, 23, 50, after_km=MOSCOW_TECH_KM),
    ]),
    _duty("7+8", [7, 8], "msk", "msk", [
        _leg("774", "msk", 8, 30, 10, 50, approach_km=MOSCOW_TECH_KM),
        _leg("705", "spb", 12, 0, 14, 10),
        _leg("756", "msk", 16, 0, 18, 10),
        _leg("759", "spb", 20, 0, 22, 10, after_km=MOSCOW_TECH_KM),
    ]),
    _duty("9+10", [9, 10], "msk", "msk", [
        _leg("726", "msk", 6, 15, 8, 35, approach_km=MOSCOW_TECH_KM),
        _leg("745", "spb", 10, 0, 12, 10),
        _leg("766", "msk", 14, 0, 16, 10, after_km=DEPOT_ACCESS_KM),
        _leg("779", "spb", 19, 30, 21, 50, approach_km=DEPOT_ACCESS_KM, after_km=MOSCOW_TECH_KM),
    ]),
    _duty("11+12", [11, 12], "msk", "spb", [
        _leg("730", "msk", 21, 0, 23, 20, approach_km=MOSCOW_TECH_KM, after_km=DEPOT_ACCESS_KM),
    ], note="В примере помечено ТО, рядом число 12 382"),
    _duty("14+15", [14, 15], "spb", "spb", [
        _leg("703", "spb", 9, 0, 11, 20, approach_km=DEPOT_ACCESS_KM),
        _leg("714", "msk", 12, 30, 14, 50),
        _leg("775", "spb", 16, 30, 18, 50),
        _leg("768", "msk", 20, 0, 22, 10, after_km=DEPOT_ACCESS_KM),
    ]),
    _duty("16+17", [16, 17], "spb", "msk", [
        _leg("701", "spb", 6, 0, 8, 10, approach_km=DEPOT_ACCESS_KM),
        _leg("722", "msk", 9, 30, 11, 50),
        _leg("755", "spb", 13, 0, 15, 20, after_km=MOSCOW_TECH_KM),
        _leg("724", "msk", 18, 30, 20, 50, approach_km=MOSCOW_TECH_KM),
        _leg("739", "spb", 22, 0, 0, 10, after_km=MOSCOW_TECH_KM),
    ]),
    _duty("18+19", [18, 19], "msk", "spb", [
        _leg("702", "msk", 6, 0, 8, 10, approach_km=MOSCOW_TECH_KM),
        _leg("727", "spb", 9, 30, 11, 50),
        _leg("748", "msk", 13, 0, 15, 20, after_km=DEPOT_ACCESS_KM),
        _leg("711", "spb", 18, 30, 20, 50, approach_km=DEPOT_ACCESS_KM),
        _leg("736", "msk", 22, 0, 0, 10, after_km=DEPOT_ACCESS_KM),
    ]),
    _duty("20+21", [20, 21], "spb", "spb", [
        _leg("721", "spb", 6, 30, 8, 50, approach_km=DEPOT_ACCESS_KM),
        _leg("742", "msk", 10, 15, 12, 35),
        _leg("767", "spb", 17, 0, 19, 20),
        _leg("710", "msk", 20, 30, 22, 50, after_km=DEPOT_ACCESS_KM),
    ], note="В примере помечено ТО, рядом число 12 370,8"),
    _duty("23+24", [23, 24], "spb", "spb", [
        _leg("773", "spb", 8, 30, 10, 50, approach_km=DEPOT_ACCESS_KM),
        _leg("706", "msk", 12, 0, 14, 10),
        _leg("747", "spb", 16, 0, 18, 10),
        _leg("744", "msk", 20, 15, 22, 35, after_km=DEPOT_ACCESS_KM),
    ]),
    _duty("25+26", [25, 26], "spb", "spb", [
        _leg("715", "spb", 6, 15, 8, 35, approach_km=DEPOT_ACCESS_KM),
        _leg("754", "msk", 10, 0, 12, 10),
        _leg("763", "spb", 14, 0, 16, 10, after_km=MOSCOW_TECH_KM),
        _leg("782", "msk", 19, 30, 21, 50, approach_km=MOSCOW_TECH_KM, after_km=DEPOT_ACCESS_KM),
    ]),
    _duty("27+28", [27, 28], "spb", "spb", [
        _leg("741", "spb", 7, 15, 9, 35, approach_km=DEPOT_ACCESS_KM),
        _leg("718", "msk", 10, 30, 12, 50),
        _leg("769", "spb", 14, 30, 16, 50),
        _leg("720", "msk", 18, 15, 20, 35, after_km=DEPOT_ACCESS_KM),
    ]),
    _duty("29+30", [29, 30], "spb", "spb", [
        _leg("735", "spb", 7, 30, 9, 50, approach_km=DEPOT_ACCESS_KM),
        _leg("762", "msk", 11, 0, 13, 20),
        _leg("743", "spb", 15, 0, 17, 20),
        _leg("740", "msk", 19, 0, 21, 20, after_km=DEPOT_ACCESS_KM),
    ], note="В примере помечено ТО, рядом число 11 004"),
    _duty("33+34", [33, 34], "spb", "msk", [
        _leg("753", "spb", 7, 0, 9, 20, approach_km=DEPOT_ACCESS_KM, after_km=MOSCOW_TECH_KM),
        _leg("776", "msk", 14, 30, 16, 50, approach_km=MOSCOW_TECH_KM),
        _leg("781", "spb", 18, 15, 20, 35, after_km=MOSCOW_TECH_KM),
    ]),
    _duty("35+36", [35, 36], "msk", "msk", [
        _leg("704", "msk", 9, 0, 11, 20, approach_km=MOSCOW_TECH_KM),
        _leg("723", "spb", 12, 30, 14, 50),
        _leg("772", "msk", 16, 30, 18, 50),
        _leg("751", "spb", 20, 30, 22, 50, after_km=MOSCOW_TECH_KM),
    ]),
    _duty("37+38", [37, 38], "msk", "msk", [
        _leg("750", "msk", 7, 30, 9, 50, approach_km=MOSCOW_TECH_KM),
        _leg("757", "spb", 11, 0, 13, 20),
        _leg("752", "msk", 15, 0, 17, 20),
        _leg("733", "spb", 19, 0, 21, 20, after_km=MOSCOW_TECH_KM),
    ]),
    _duty("39+40", [39, 40], "msk", "msk", [
        _leg("712", "msk", 6, 30, 8, 50, approach_km=MOSCOW_TECH_KM),
        _leg("713", "spb", 10, 15, 12, 35, after_km=MOSCOW_TECH_KM),
        _leg("764", "msk", 17, 30, 19, 50, approach_km=MOSCOW_TECH_KM),
        _leg("771", "spb", 21, 0, 23, 20, after_km=MOSCOW_TECH_KM),
    ]),
    _duty("41+42", [41, 42], "msk", "spb", [
        _leg("778", "msk", 11, 30, 13, 50, approach_km=MOSCOW_TECH_KM, after_km=DEPOT_ACCESS_KM),
        _leg("729", "spb", 18, 0, 20, 10, approach_km=DEPOT_ACCESS_KM),
        _leg("716", "msk", 21, 30, 23, 50, after_km=DEPOT_ACCESS_KM),
    ], note="В примере помечено ТО, рядом число 12 340,4"),
]


def revenue_legs() -> List[Dict]:
    rows = []
    for duty in OPERATING_DUTIES:
        for leg in duty["legs"]:
            rows.append({**leg, "duty": duty["id"], "note": duty["note"]})
    return rows


def consists_needed(city: str, coupled: bool = True) -> int:
    per = 2 if coupled else 1
    return per * sum(1 for d in OPERATING_DUTIES if d["start_city"] == city)


def home_city(train_id: int) -> str:
    """Где состав стоит утром примера. Служебные — в Обухово."""
    for duty in OPERATING_DUTIES:
        if train_id in duty["consists"]:
            return duty["start_city"]
    return "spb"


def duty_by_id(code: str) -> Optional[Dict]:
    for duty in OPERATING_DUTIES:
        if duty["id"] == code:
            return duty
    return None


def diagram_summary() -> Dict:
    legs = revenue_legs()
    coupled = 2
    fleet_km_day = sum(leg["km"] for leg in legs) * coupled
    deadhead = 0.0
    for duty in OPERATING_DUTIES:
        for leg in duty["legs"]:
            deadhead += (leg["approach_km"] + leg["after_km"]) * coupled
    running = coupled * len(OPERATING_DUTIES)
    service = len(EXAMPLE_SERVICE_IDS)
    return {
        "duties": len(OPERATING_DUTIES),
        "revenue_trains": len(legs),
        "running_consists": running,
        "service_consists": service,
        "fleet": running + service,
        "spb_need": consists_needed("spb"),
        "msk_need": consists_needed("msk"),
        "revenue_km_day": fleet_km_day,
        "deadhead_km_day": deadhead,
        "km_per_train_year": (fleet_km_day + deadhead) * 365 / (running + service),
        "min_turnaround_h": _min_turnaround(),
    }


def _min_turnaround() -> float:
    gaps = []
    for duty in OPERATING_DUTIES:
        legs = duty["legs"]
        for a, b in zip(legs, legs[1:]):
            gaps.append(b["dep"] - a["arr"])
    return min(gaps) if gaps else 0.0


def balance_text() -> str:
    s = diagram_summary()
    return (
        f"В примере {s['duties']} оборотов по два состава — {s['running_consists']} на нитках "
        f"и {s['service_consists']} в сервисном цикле (№13, 22, 31, 32, 43). "
        f"Вместе {s['fleet']}, это весь парк. Ниток в сутки — {s['revenue_trains']}. "
        f"Отдельного простаивающего резерва 2+2 в этой раскладке нет: "
        f"38 на графике и 5 на сервисе уже занимают 43. "
        f"Резерв с запасом 1 493 км в модели — это составы на стоянке между нитками "
        f"и те, кто не влез в оборот. Снимать с нитки ради пустой стоянки нельзя: "
        f"готовность важнее."
    )


def _fmt_clock(hour: float) -> str:
    h = int(hour) % 24
    m = int(round((hour - int(hour)) * 60))
    if m == 60:
        h = (h + 1) % 24
        m = 0
    return f"{h:02d}:{m:02d}"


def timetable_rows() -> List[Dict]:
    city = {
        "spb": "Санкт-Петербург",
        "msk": "Москва",
    }
    rows = []
    for duty in OPERATING_DUTIES:
        for leg in duty["legs"]:
            note = []
            if leg["approach_km"]:
                note.append(f"подача {leg['approach_km']:g} км")
            if leg["after_km"]:
                note.append(f"уборка {leg['after_km']:g} км")
            if duty["note"] and leg is duty["legs"][-1]:
                note.append(duty["note"])
            rows.append({
                "Оборот": duty["id"],
                "Нитка": leg["train"],
                "Откуда": city[leg["origin"]],
                "Отправление": _fmt_clock(leg["dep"]),
                "Прибытие": _fmt_clock(leg["arr"]),
                "Куда": city[leg["dest"]],
                "Км": int(leg["km"]),
                "Примечание": "; ".join(note) if note else "—",
            })
    for tid in EXAMPLE_SERVICE_IDS:
        rows.append({
            "Оборот": "сервис",
            "Нитка": "—",
            "Откуда": "Депо Обухово",
            "Отправление": "—",
            "Прибытие": "—",
            "Куда": "Депо Обухово",
            "Км": 0,
            "Примечание": f"Состав {tid}: экипировка, ТО, подготовка",
        })
    return rows
