"""Исполнение суточного оборота из примера заказчика.

Оборот на следующий день назначается заново: состав, который вечером
пришёл в город, утром берёт нитку, которая из этого города начинается.
Сцепка — как в примере, два состава на оборот. Если второго нет,
нитка уходит одним: обязательность сцепки эксперты не подтвердили.
"""
from typing import Dict, List, Tuple

from src.core.config import config
from src.core.database import BROKEN, MAINTENANCE, OPERATIONAL, RESERVE
from src.core.timetable import (OPERATING_DUTIES, consists_needed, revenue_legs)


def assign_duties(sim) -> Tuple[List[Tuple[Dict, List[int]]], set]:
    used = set()
    out = []
    duties = sorted(OPERATING_DUTIES, key=lambda d: d["legs"][0]["dep"])
    for duty in duties:
        ready = _ready_for(sim, duty["start_city"], used)
        ready.sort(key=lambda t: (_legs_fit(sim, t, duty), sim._km_to_hard(t)), reverse=True)
        capable = [t for t in ready if _legs_fit(sim, t, duty) >= 1]
        n = 2 if config.COUPLE_CONSISTS else 1
        crew = capable[:n]
        for tid in crew:
            used.add(tid)
        out.append((duty, crew))
    return out, used


def _ready_for(sim, city: str, used: set) -> List[int]:
    out = []
    for tid, st in sim.state.items():
        if tid in used or st["location"] != city or st["busy"]:
            continue
        if st["commissioned_hour"] > sim.env.now + 1e-6:
            continue
        if st["status"] not in (OPERATIONAL, RESERVE):
            continue
        if st["blocked"] and sim._past_any_hard(st):
            continue
        out.append(tid)
    return out


def _shortest(sim, city: str) -> Dict:
    duties = [d for d in OPERATING_DUTIES if d["start_city"] == city]
    return min(duties, key=lambda d: len(d["legs"]))


def _legs_fit(sim, tid: int, duty: Dict) -> int:
    left = sim._km_to_hard(tid)
    n = 0
    for _leg in duty["legs"]:
        if left < config.ROUTE_KM - 1:
            break
        left -= config.ROUTE_KM
        n += 1
    return n


def protected_morning_ids(sim, candidates: List[Dict]) -> set:
    """Кого нельзя забирать на работу длиннее ночи: они нужны утренним ниткам."""
    need = consists_needed("spb", coupled=config.COUPLE_CONSISTS)
    ranked = sorted((c["tid"] for c in candidates),
                    key=lambda t: sim._km_to_hard(t), reverse=True)
    return set(ranked[:need])


def rebalance_cities(sim):
    """Ночной перегон излишка, если в одном городе не хватает составов на утро.

    Ход 2 ч 15 мин плюс экипировка укладываются в окно 00:00–06:00.
    """
    need = {city: consists_needed(city, coupled=config.COUPLE_CONSISTS) for city in ("spb", "msk")}
    ready = {city: _ready_for(sim, city, set()) for city in ("spb", "msk")}
    for src, dst in (("msk", "spb"), ("spb", "msk")):
        surplus = len(ready[src]) - need[src]
        deficit = need[dst] - len(ready[dst])
        n = min(max(0, surplus), max(0, deficit))
        if n <= 0:
            continue
        movers = sorted(ready[src], key=lambda t: sim._km_to_hard(t), reverse=True)[:n]
        for tid in movers:
            sim.state[tid]["busy"] = True
            sim.metrics["deadhead"] += 1
            sim.metrics["departures"] += 1
            sim.env.process(sim._one_way_process(tid, src))
        ready[src] = [t for t in ready[src] if t not in movers]


def run_diagram_day(sim):
    for key in ("miss_no_crew", "miss_late", "miss_km"):
        sim.metrics.setdefault(key, 0)
    day0 = (sim.env.now // 24) * 24
    assignments, used = assign_duties(sim)
    n_legs = len(revenue_legs())
    sim.metrics["pairs_target"] += n_legs
    sim.metrics["diagram_target"] += n_legs
    if sim.scenario["mileage"] > 1.05:
        extra = max(1, int(round(0.2 * n_legs)))
        sim.metrics["pairs_target"] += extra
        sim.metrics["missed_departures"] += extra

    idle = {"spb": [], "msk": []}
    for city in ("spb", "msk"):
        for tid in _ready_for(sim, city, used):
            if sim._km_to_hard(tid) >= config.RESERVE_MARGIN_KM:
                idle[city].append(tid)
            if sim._km_to_hard(tid) < config.ROUTE_KM:
                sim.state[tid]["blocked"] = True
        if len(idle[city]) < config.RESERVE_PER_CITY:
            sim.metrics["reserve_shortfall_days"] += 1
    sim.hot_reserve = idle

    for duty, crew in assignments:
        if not crew:
            sim.metrics["missed_departures"] += len(duty["legs"])
            sim.metrics["miss_no_crew"] += len(duty["legs"])
            continue
        for tid in crew:
            sim.state[tid]["busy"] = True
            sim.state[tid]["blocked"] = False
        sim.env.process(_duty_process(sim, duty, crew, day0))

    # Кому в Москве уже нельзя в рейс — вернуть в Обухово, иначе допуск сгорит на стоянке.
    for tid in list(idle["msk"]):
        st = sim.state[tid]
        if st["busy"] or not sim._needs_depot(tid):
            continue
        st["busy"] = True
        sim.metrics["departures"] += 1
        sim.metrics["deadhead"] += 1
        sim.env.process(sim._one_way_process(tid, "msk"))

    midnight = day0 + 24
    if sim.env.now < midnight:
        yield sim.env.timeout(midnight - sim.env.now)


def _duty_process(sim, duty: Dict, crew: List[int], day0: float):
    handed = set()
    try:
        for i, leg in enumerate(duty["legs"]):
            alive = [t for t in crew if t not in handed and _can_run(sim, t)]
            if not alive or sim.env.now > day0 + leg["dep"] + 0.25:
                sim.metrics["missed_departures"] += 1
                continue
            alive = [t for t in alive if sim._km_to_hard(t) >= config.ROUTE_KM - 1]
            if not alive:
                sim.metrics["missed_departures"] += 1
                for tid in crew:
                    if tid not in handed:
                        sim.state[tid]["blocked"] = True
                continue
            wait = day0 + leg["dep"] - sim.env.now
            if wait > 0.01:
                yield sim.env.timeout(wait)
            if leg["approach_km"]:
                for tid in alive:
                    sim._add_mileage(tid, leg["approach_km"])
                sim.metrics["deadhead_km"] += leg["approach_km"] * len(alive)
            if len(alive) >= 2:
                sim.metrics["coupled_departures"] += 1
            else:
                sim.metrics["single_departures"] += 1
            sim.metrics["departures"] += len(alive)
            sim.metrics["diagram_covered"] += 1
            sim.metrics["pairs_completed"] += 1
            travel = (day0 + leg["arr"]) - sim.env.now
            disrupted, newly = yield from _coupled_leg(sim, alive, leg, max(0.05, travel))
            handed |= newly
            if disrupted:
                rest = len(duty["legs"]) - i - 1
                sim.metrics["missed_departures"] += rest
                sim.metrics["miss_disrupted"] = sim.metrics.get("miss_disrupted", 0) + rest
                break
            survivors = [t for t in alive if t not in handed]
            if leg["after_km"] and survivors:
                for tid in survivors:
                    sim._add_mileage(tid, leg["after_km"])
                sim.metrics["deadhead_km"] += leg["after_km"] * len(survivors)
        for tid in crew:
            if tid in handed:
                continue
            st = sim.state[tid]
            if st["status"] in (OPERATIONAL, RESERVE, "line", "equipping"):
                st["location"] = duty["end_city"]
                sim._set_status(tid, OPERATIONAL)
            if not sim.use_planning and duty["end_city"] == "spb" and st["status"] == OPERATIONAL:
                yield from sim._reactive_pull(tid)
    finally:
        for tid in crew:
            if tid in handed:
                continue
            sim.state[tid]["busy"] = False
            if sim.state[tid]["status"] in ("line", "equipping"):
                sim._set_status(tid, OPERATIONAL)


def _can_run(sim, tid: int) -> bool:
    st = sim.state[tid]
    return st["status"] not in (BROKEN, MAINTENANCE, "waiting")


def _coupled_leg(sim, crew: List[int], leg: Dict, travel: float):
    handed = set()
    dest = leg["dest"]
    origin = leg["origin"]
    half = travel / 2.0
    for tid in crew:
        sim._set_status(tid, "line", location=f"to_{dest}")
    yield sim.env.timeout(max(0.01, half))
    broken = _roll_half(sim, crew, origin, dest, first_half=True)
    if broken:
        handed |= _hand_to_repair(sim, crew, broken, dest)
        return True, handed
    yield sim.env.timeout(max(0.01, travel - half))
    for tid in crew:
        sim.state[tid]["location"] = dest
    broken = _roll_half(sim, crew, origin, dest, first_half=False)
    if broken:
        handed |= _hand_to_repair(sim, crew, broken, dest)
        return True, handed
    yield from _equip_group(sim, crew, dest)
    return False, handed


def _roll_half(sim, crew, origin, dest, first_half: bool):
    broken = []
    place_choices = (dest, origin) if first_half else (dest,)
    for tid in crew:
        sim._add_mileage(tid, config.ROUTE_KM / 2)
        sim._log_trip(tid, origin)
        kind = sim._roll_failure(tid, config.ROUTE_KM / 2)
        if kind:
            place = place_choices[0] if len(place_choices) == 1 else (
                dest if sim.rng.random() < 0.5 else origin)
            broken.append((tid, kind, place))
    return broken


def _hand_to_repair(sim, crew, broken, dest) -> set:
    handed = set()
    sim.metrics["disrupted_pairs"] += 1
    for tid, kind, place in broken:
        handed.add(tid)
        sim.env.process(_repair_owned(sim, tid, place, kind))
    for tid in crew:
        if tid in handed:
            continue
        sim.state[tid]["location"] = dest
        sim._set_status(tid, OPERATIONAL)
        sim.state[tid]["busy"] = False
        handed.add(tid)
    _cover_quietly(sim, dest)
    return handed


def _cover_quietly(sim, city: str):
    if city not in ("spb", "msk"):
        city = "msk" if "msk" in str(city) else "spb"
    for tid in sim.hot_reserve.get(city, []):
        st = sim.state[tid]
        if st["busy"] or st["status"] not in (OPERATIONAL, RESERVE) or st["location"] != city:
            continue
        st["busy"] = True
        sim.metrics["reserve_activations"] += 1
        sim.metrics["departures"] += 1
        sim.env.process(sim._one_way_process(tid, city, stay=True))
        return True
    return False


def _repair_owned(sim, tid, place, kind):
    try:
        yield from sim._repair(tid, place, kind)
    finally:
        sim.state[tid]["busy"] = False


def _equip_group(sim, crew, city):
    hours = config.EQUIP_HOURS
    for tid in crew:
        sim._set_status(tid, "equipping")
    yield sim.env.timeout(hours)
    sim.metrics["equipping_hours"] += hours * len(crew)
    for tid in crew:
        sim.state[tid]["location"] = city
        sim._set_status(tid, OPERATIONAL)
