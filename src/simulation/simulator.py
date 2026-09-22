"""Цифровой двойник парка ВСМ. Контур замкнут: план исполняется.

Что взято из чата с заказчиком (АО «Сервис Высоких Скоростей»):
- оборот — пара 679 км × 2, запас резерва 1 493 км, по 2 состава в каждом городе;
- плановое ТО только в Обухово; в Москве — экипировка и внеплановые работы, включая замену КП;
- одновременно на сервисе не больше 5 составов; IS600/IS700 — 3 пути с домкратами;
- обточка повагонно, 120 мин, отдельный станок, до или после ТО, но не одновременно;
- IS510–IS540 делятся на блоки; неразрывная цепочка не отпускает состав;
- IS100/IS200 можно сдвинуть только внутри допуска; клапан — раз в 365 суток, без пропуска;
- готовность важнее равномерности загрузки. Равномерность не покупается просрочкой.
"""
import math
import random
from collections import Counter, defaultdict
from typing import Callable, Dict, List, Optional

import pandas as pd
import simpy
from loguru import logger

from src.core.config import config
from src.core.database import (BROKEN, MAINTENANCE, NOT_DELIVERED, OPERATIONAL,
                               RESERVE, MaintenanceEvent, ServiceSegment, Train,
                               get_session, hour_to_datetime)
from src.core.labels import ru_location, ru_node, ru_service, ru_status
from src.planning.strategic import MONTH_HOURS, StrategicPlanner
from src.planning.tactical import TacticalPlanner
from src.simulation.diagram import protected_morning_ids, run_diagram_day
from src.simulation.report import (monthly_load_cv, required_count,
                                   service_slot_limit)
from src.core.timetable import home_city, timetable_rows

AVAILABLE = {OPERATIONAL, RESERVE, "line", "equipping"}
HEAVY = {"IS530", "IS540", "IS600", "IS700"}
MILEAGE_TYPES = (
    "IS100", "IS200", "IS510", "IS520", "IS530", "IS540", "IS600", "IS700",
    "wheelset_turning",
)


class IntegratedFleetSimulator:
    """Симулятор с исполнением стратегического плана и ночного диспетчера."""

    def __init__(self, db_engine, num_trains: int = 43, use_planning: bool = True,
                 scenario: str = "normal", lathes: int = None, steady_state: bool = False,
                 use_diagram: bool = False):
        self.engine = db_engine
        self.session = get_session(db_engine)
        self.num_trains = num_trains
        self.use_planning = use_planning
        self.steady_state = steady_state
        self.use_diagram = use_diagram
        self.scenario_name = scenario
        self.scenario = config.SCENARIOS.get(scenario, config.SCENARIOS["normal"])
        self.n_lathes = lathes if lathes is not None else config.WHEELSET_LATHE_CAPACITY

        self.strategic = StrategicPlanner(db_engine) if use_planning else None
        self.tactical = TacticalPlanner(db_engine) if use_planning else None
        self.plan_windows: Dict = {}
        self.tactical_rows: List[Dict] = []
        self.last_tactical_rows = 0

        self.env: Optional[simpy.Environment] = None
        self.rng = random.Random(42)
        self.state: Dict[int, Dict] = {}
        self.hot_reserve = {"spb": [], "msk": []}

        self.slots_taken = 0
        self.free_bays = set(range(1, config.DEPOT_CAPACITY + 1))
        self.free_jacks = set(range(1, config.JACK_TRACKS + 1))
        self.moscow_repair_used = 0
        self.lathe_busy_until = [0.0] * self.n_lathes

        self.segments: List[Dict] = []
        self.trip_log: List[Dict] = []
        self.failure_log: List[Dict] = []
        self.event_rows: List[Dict] = []
        self._open_segments: Dict[int, Dict] = {}
        self._open_events: List[Dict] = []
        self.hourly: List[tuple] = []
        self.metrics: Dict = {}
        self._until = 0

    # ============================================================== запуск
    def run(self, duration_hours: int = 8760, seed: int = 42,
            callback: Optional[Callable] = None) -> Dict:
        self.rng = random.Random(seed)
        self._until = duration_hours
        self.callback = callback
        logger.info(
            "Симуляция: {} ч, планирование={}, сценарий={}, станков={}",
            duration_hours, self.use_planning, self.scenario_name, self.n_lathes)

        self.segments.clear()
        self.trip_log.clear()
        self.failure_log.clear()
        self.event_rows.clear()
        self._open_segments.clear()
        self._open_events.clear()
        self.hourly.clear()
        self.plan_windows.clear()
        self.tactical_rows = []
        self.slots_taken = 0
        self.free_bays = set(range(1, config.DEPOT_CAPACITY + 1))
        self.free_jacks = set(range(1, config.JACK_TRACKS + 1))
        self.moscow_repair_used = 0
        self.lathe_busy_until = [0.0] * max(1, self.n_lathes)
        self.metrics = {
            "total_services": 0, "total_breakdowns": 0, "total_downtime_hours": 0.0,
            "services_by_type": Counter(), "nodes": Counter(),
            "line_failures": 0, "postponements": 0, "reserve_activations": 0,
            "missed_departures": 0, "disrupted_pairs": 0, "departures": 0,
            "pairs_completed": 0, "pairs_target": 0,
            "queue_events": 0, "wheel_overdue": 0, "mileage_overdue": 0,
            "service_hours": 0.0, "additional_hours": 0.0, "equipping_hours": 0.0,
            "lathe_hours": 0.0, "day_lathe_hours": 0.0,
            "deadhead": 0, "deadhead_km": 0.0, "strategic_events": 0,
            "reserve_shortfall_days": 0,
            "diagram_covered": 0, "diagram_target": 0,
            "coupled_departures": 0, "single_departures": 0,
            "lathe_deficit_hour": None, "max_on_service": 0,
            "coverage_days_ok": 0, "coverage_days": 0,
            "hours_below": 0, "hours_sampled": 0,
        }

        self.env = simpy.Environment()
        self._init_state()
        self.env.process(self._main())
        self.env.process(self._collector())
        self.env.run(until=duration_hours)

        self._close_all_segments()
        self._flush_open_events()
        self._sync_db()
        return self.get_results()

    def _init_state(self):
        self.state.clear()
        for train in self.session.query(Train).order_by(Train.id).all():
            if self.use_diagram:
                home = home_city(train.id)
            else:
                home = "spb"
                if train.id in (5, 6) or train.id >= self.num_trains - 1:
                    home = "msk"
            preferred = train.id > self.num_trains - config.RESERVE_TRAINS
            commissioned = max(
                0.0, (train.commissioned_date - hour_to_datetime(0)).total_seconds() / 3600)
            cars = [i * 2_000.0 for i in range(config.CARS_PER_TRAIN)]
            self.state[train.id] = {
                "number": train.number,
                "status": train.status,
                "location": home if commissioned <= 0 else "spb",
                "home": home,
                "total": train.total_mileage or 0.0,
                "m": {k: getattr(train, f"mileage_since_{k.lower() if k != 'wheelset_turning' else 'wheelset'}") or 0.0
                      for k in MILEAGE_TYPES},
                "cars": cars,
                "commissioned_hour": commissioned,
                "preferred_reserve": preferred,
                "busy": False,
                "blocked": False,
                "calendar_due": {
                    code: commissioned + spec["interval_days"] * 24
                    for code, spec in config.CALENDAR_TASKS.items()
                },
                "open_block": {},
            }
            self.state[train.id]["m"]["wheelset_turning"] = max(cars)
            if self.steady_state and commissioned <= 0:
                self._stagger(train.id)
            if commissioned <= 0 and train.status == NOT_DELIVERED:
                self.state[train.id]["status"] = RESERVE if preferred else OPERATIONAL
            if self.state[train.id]["status"] != NOT_DELIVERED:
                self.state[train.id]["location"] = home
                self._segment(train.id, self.state[train.id]["status"])

    def _stagger(self, tid: int):
        """Нормальный год: счётчики уже разнесены по циклу, а не обнулены в один день.

        Иначе 43 состава одновременно подходят к IS100, а ночь физически
        пропускает около 15 коротких работ. Допущение, не цитата заказчика:
        в примере оборота пробеги уже разные.
        """
        # Разнос только внутри допуска IS100, ниже порога 85%.
        # Иначе часть парка в первый день уже не закрывает длинный оборот.
        span = config.MILEAGE_TRIGGERS["IS100"] * 0.64
        offset = (tid - 1) / max(1, self.num_trains) * span
        st = self.state[tid]
        st["total"] = offset
        for stype in ("IS510", "IS520", "IS530", "IS540", "IS600", "IS700"):
            st["m"][stype] = offset
        st["m"]["IS200"] = offset % config.MILEAGE_TRIGGERS["IS200"]
        st["m"]["IS100"] = offset % config.MILEAGE_TRIGGERS["IS100"]
        base = offset
        st["cars"] = [base + i * 1_500.0 for i in range(len(st["cars"]))]
        st["m"]["wheelset_turning"] = max(st["cars"])

    # ====================================================== главный цикл
    def _main(self):
        self._maybe_commission()
        if self.use_planning:
            self._refresh_strategic()
            self._refresh_tactical()
        while self.env.now < self._until - 0.05:
            self._maybe_commission()
            day_index = int(self.env.now // 24)
            if self.use_planning and day_index > 0 and day_index % 180 == 0 and self.env.now % 24 < 0.1:
                self._refresh_strategic()
            # Тактический план — на 14 суток. Дольше его не на чем исполнять.
            if self.use_planning and day_index > 0 and day_index % 14 == 0 and self.env.now % 24 < 0.1:
                self._refresh_tactical()

            if self.env.now % 24 < 0.1:
                if self.use_diagram:
                    from src.simulation.diagram import rebalance_cities
                    rebalance_cities(self)
                for job in self._plan_night():
                    self.env.process(self._job_process(job))
                to_morning = config.OPERATION_START_HOUR - (self.env.now % 24)
                if to_morning > 0:
                    yield self.env.timeout(to_morning)
            yield from self._run_day()

    def _maybe_commission(self):
        for tid, st in self.state.items():
            if st["status"] == NOT_DELIVERED and self.env.now + 1e-6 >= st["commissioned_hour"]:
                st["status"] = RESERVE if st["preferred_reserve"] else OPERATIONAL
                st["location"] = st["home"]
                st["calendar_due"] = {
                    code: self.env.now + spec["interval_days"] * 24
                    for code, spec in config.CALENDAR_TASKS.items()
                }
                self._segment(tid, st["status"])

    # ------------------------------------------------------- планировщики
    def _refresh_strategic(self):
        if not self.strategic:
            return
        self._sync_state_light()
        self.strategic.now = hour_to_datetime(self.env.now)
        try:
            plan = self.strategic.create_annual_plan() or []
        except Exception as ex:
            logger.error("Стратегический план не построен: {}", ex)
            return
        self.plan_windows = {}
        for item in plan:
            tid = int(item["train_id"])
            stype = item["service_type"]
            start = self.env.now + float(item["month"]) * MONTH_HOURS
            offset = (tid * 37) % int(18 * 24)
            self.plan_windows[(tid, stype)] = {
                "earliest": start + offset,
                "latest": start + MONTH_HOURS + 12 * 24,
            }
        self.metrics["strategic_events"] = len(plan)
        logger.info("Стратегический план: {} событий", len(plan))

    def _refresh_tactical(self):
        """CP-SAT на 14 суток. Исполняет те же ограничения, что и ночной диспетчер.
        Если решатель не успел — диспетчер всё равно собирает ночь сам."""
        if not self.tactical:
            return
        self._sync_state_light()
        try:
            df = self.tactical.create_weekly_schedule(
                int(self.env.now), fixed_intervals=[], time_limit=1.5)
            self.last_tactical_rows = 0 if df is None else len(df)
        except Exception as ex:
            logger.error("Тактический план: {}", ex)

    # ------------------------------------------------------- ночь
    def _plan_night(self) -> List[Dict]:
        comm = self._commissioned_count()
        slack = service_slot_limit(comm)
        morning_free = max(0, slack - self.slots_taken)
        night_cap = max(0, config.MAX_ON_SERVICE - self.slots_taken)
        if night_cap <= 0 and morning_free <= 0:
            return []

        candidates = []
        for tid, st in self.state.items():
            if st["commissioned_hour"] > self.env.now + 1e-6:
                continue
            if st["location"] != "spb" or st["busy"]:
                continue
            if st["status"] not in (OPERATIONAL, RESERVE):
                continue
            cand = self._candidate(tid)
            if cand:
                candidates.append(cand)
        due_plan = {}
        for row in self.tactical_rows:
            if row.get("done"):
                continue
            if float(row["start_hour"]) <= self.env.now + 30:
                due_plan[int(row["train_id"])] = row
        for cand in candidates:
            row = due_plan.get(cand["tid"])
            if row and row["service_type"] == cand["type"]:
                cand["urgency"] += 8
                cand["from_plan"] = True
        candidates.sort(key=lambda c: c["urgency"], reverse=True)
        protected = protected_morning_ids(self, candidates) if self.use_diagram else set()

        packed: List[Dict] = []
        # Дорожка = момент (часы от полуночи), когда она снова свободна.
        # Уже идущие работы в эти списки не входят: их держит slots_taken / free_bays.
        service_lanes = [0.0] * night_cap
        bay_lanes = [0.0] * len(self.free_bays)
        jack_lanes = [0.0] * len(self.free_jacks)
        lathe_lanes = [0.0] * self.n_lathes
        heavy_new = 0
        seen = set()

        for cand in candidates:
            if cand["tid"] in seen:
                continue
            if cand["spill"]:
                # Утренние нитки из Петербурга не отдаём работе, которая вылезет в день.
                if cand["tid"] in protected and not cand["past_hard"]:
                    self.metrics["postponements"] += 1
                    continue
                if heavy_new >= 2 and self.use_planning and not cand["past_hard"]:
                    self.metrics["postponements"] += 1
                    continue
                if morning_free <= 0 and not cand["past_hard"]:
                    self.metrics["postponements"] += 1
                    continue
                resource_lanes = jack_lanes if cand["resource"] == "jack" else bay_lanes
                if not service_lanes or not resource_lanes:
                    self._cannot_place(cand)
                    continue
                service_lanes.pop()
                resource_lanes.pop()
                cand["delay"] = 0.0
                packed.append(cand)
                seen.add(cand["tid"])
                self._mark_tactical_done(cand["tid"])
                if cand["heavy"]:
                    heavy_new += 1
                morning_free -= 1
                continue

            resource_lanes = {"bay": bay_lanes, "jack": jack_lanes,
                              "lathe": lathe_lanes}[cand["resource"]]
            placed = self._place_on_lanes(service_lanes, resource_lanes, cand["hours"])
            if placed is None and cand["type"] == "wheelset_turning" and cand.get("cars", 1) > 1:
                cand["cars"] = 1
                cand["hours"] = config.TURNING_HOURS_PER_CAR
                cand["steps"][0]["hours"] = cand["hours"]
                cand["steps"][0]["cars"] = 1
                placed = self._place_on_lanes(service_lanes, resource_lanes, cand["hours"])
            if placed is None:
                self._defer_or_overdue(cand)
                continue
            cand["delay"] = placed
            packed.append(cand)
            seen.add(cand["tid"])
            self._mark_tactical_done(cand["tid"])
        return packed

    def _mark_tactical_done(self, tid: int):
        for row in self.tactical_rows:
            if not row.get("done") and int(row["train_id"]) == tid:
                row["done"] = True
                return

    @staticmethod
    def _place_on_lanes(service_lanes, resource_lanes, hours, night=6.0):
        """Поставить работу на свободные дорожки. None — в эту ночь не влезает."""
        if hours > night + 1e-6 or not service_lanes or not resource_lanes:
            return None
        best = None
        for s_i, s_end in enumerate(service_lanes):
            for r_i, r_end in enumerate(resource_lanes):
                start = max(s_end, r_end)
                if start + hours <= night + 1e-6 and (best is None or start < best[0]):
                    best = (start, s_i, r_i)
        if best is None:
            return None
        start,  s_i, r_i = best
        service_lanes[s_i] = start + hours
        resource_lanes[r_i] = start + hours
        return start

    def _cannot_place(self, cand):
        if cand["past_hard"]:
            self.state[cand["tid"]]["blocked"] = True
            self.metrics["queue_events"] += 1
            if cand["type"] == "wheelset_turning":
                self.metrics["wheel_overdue"] += 1
                if self.metrics["lathe_deficit_hour"] is None:
                    self.metrics["lathe_deficit_hour"] = self.env.now
            else:
                self.metrics["mileage_overdue"] += 1
        else:
            self.metrics["postponements"] += 1

    def _defer_or_overdue(self, cand):
        km_left = self._km_to_hard(cand["tid"])
        daily = self._expected_daily_km()
        if cand["past_hard"] or km_left < daily:
            self._cannot_place(cand)
        else:
            self.metrics["postponements"] += 1

    def _binding_type(self, tid: int) -> str:
        """Какой счётчик ближе всего к жёсткому допуску."""
        st = self.state[tid]
        best_left, best = 1e18, "IS100"
        for stype in ("IS100", "IS200", "IS510", "IS520", "IS530", "IS540", "IS600", "IS700"):
            left = self._hard(stype) - st["m"][stype]
            if left < best_left:
                best_left, best = left, stype
        if st["cars"]:
            wleft = self._hard("wheelset_turning") - max(st["cars"])
            if wleft < best_left:
                best_left, best = wleft, "wheelset_turning"
        for code, due in st["calendar_due"].items():
            if due - self.env.now < 36:
                return code
        return best

    def _candidate(self, tid: int) -> Optional[Dict]:
        st = self.state[tid]
        threshold = 0.85 if self.use_planning else 1.0
        best = None

        for code, due in st["calendar_due"].items():
            if self.env.now >= due - 72:
                spec = config.CALENDAR_TASKS[code]
                past = self.env.now >= due
                best = self._pack_candidate(
                    tid, code, spec["duration_hours"], urgency=50 if past else 6,
                    past_hard=past, resource="bay", spill=False, heavy=False)

        order = ["IS700", "IS600", "IS540", "IS530", "IS520", "IS510", "IS200", "IS100"]
        for stype in order:
            ratio = self._ratio(tid, stype)
            past = st["m"][stype] >= self._hard(stype) - 1
            window = self.plan_windows.get((tid, stype))
            # Окно стратегического плана сдвигает заход, но не дальше допуска.
            if self.use_planning and stype in config.STRATEGIC_SERVICES and window and not past:
                if self.env.now < window["earliest"] and ratio < 0.95:
                    continue
            # Короткие циклы — с 85%. Крупные — раньше, иначе волна одного
            # ввода не помещается в ночные окна до жёсткого допуска.
            need = 0.85
            if self.use_planning and stype in config.STRATEGIC_SERVICES:
                need = 0.72
            if not self.use_planning:
                need = 1.0
            if self.use_diagram and self._km_to_hard(tid) < config.ROUTE_KM:
                need = 0.0
            due = ratio >= need or past or stype in st["open_block"]
            if not due:
                continue
            if best and best["type"] in config.CALENDAR_TASKS and not past:
                # Календарную работу приклеим к регламенту, если влезет.
                pass
            urgency = ratio * config.SERVICE_CRITICALITY.get(stype, 1) + (12 if past else 0)
            if self._km_to_hard(tid) < config.PAIR_KM * 2:
                urgency += 4
            if window and self.env.now > window.get("latest", 1e18):
                urgency += 3
            hours, spill, resource = self._next_chunk(tid, stype)
            cand = self._pack_candidate(
                tid, stype, hours, urgency=urgency, past_hard=past,
                resource=resource, spill=spill, heavy=stype in HEAVY)
            # Длинный цикл, который сегодня не закроется, не должен
            # перехватывать очередь у короткого ТО: иначе состав стоит
            # у жёсткого допуска и выпадает из графика.
            binding = self._binding_type(tid)
            resets = cand["steps"][0].get("completes", False) and (
                binding in config.SERVICE_HIERARCHY.get(stype, [stype]))
            if (self._km_to_hard(tid) < config.PAIR_KM * 1.5 and not resets
                    and stype != binding):
                continue
            if best is None or cand["urgency"] > best["urgency"]:
                best = cand
            break

        cars = self._cars_due(tid)
        if cars:
            past = max(st["cars"]) >= self._hard("wheelset_turning") - 1
            urgency = 1.2 + (12 if past else 0) + max(st["cars"]) / 200_000
            hours = config.TURNING_HOURS_PER_CAR * cars
            # В одну ночь берём столько вагонов, сколько влезает в 6 часов.
            cars_tonight = min(cars, int(6 / config.TURNING_HOURS_PER_CAR))
            hours = config.TURNING_HOURS_PER_CAR * cars_tonight
            turning = self._pack_candidate(
                tid, "wheelset_turning", hours, urgency=urgency, past_hard=past,
                resource="lathe", spill=False, heavy=False, cars=cars_tonight)
            if best is None or (past and not best["past_hard"]) or turning["urgency"] > best["urgency"] + 0.4:
                # Обточку можно поставить следом за коротким ТО, если вместе влезают в ночь.
                if (best and not best["spill"] and best["resource"] == "bay"
                        and best["hours"] + hours <= 6 and not past):
                    best["steps"].append(turning["steps"][0])
                    best["hours"] += hours
                else:
                    best = turning
        if best:
            st["blocked"] = False
        return best

    def _pack_candidate(self, tid, stype, hours, urgency, past_hard, resource,
                        spill, heavy, cars=0) -> Dict:
        step = {"type": stype, "hours": hours, "resource": resource,
                "cars": cars, "completes": stype not in config.SERVICE_BLOCKS
                or self._chunk_completes(tid, stype)}
        return {"tid": tid, "type": stype, "hours": hours, "urgency": urgency,
                "past_hard": past_hard, "resource": resource, "spill": spill,
                "heavy": heavy, "cars": cars, "delay": 0.0, "steps": [step]}

    def _next_chunk(self, tid, stype):
        blocks = config.SERVICE_BLOCKS.get(stype)
        if not blocks:
            hours = config.SERVICE_DURATIONS[stype]
            spill = hours > 6
            resource = "jack" if stype in config.JACK_SERVICES else "bay"
            return hours, spill, resource
        idx = self.state[tid]["open_block"].get(stype, 0)
        idx = min(idx, len(blocks) - 1)
        name, hours, interruptible = blocks[idx]
        resource = "jack" if stype in config.JACK_SERVICES else "bay"
        if not self.use_planning:
            # Реактивный режим не дробит: вся оставшаяся длительность подряд.
            rest = sum(b[1] for b in blocks[idx:])
            return rest, True, resource
        spill = (not interruptible and hours > 6) or stype in HEAVY
        if stype in HEAVY:
            rest = sum(b[1] for b in blocks[idx:])
            return rest, True, resource
        return hours, False, resource

    def _chunk_completes(self, tid, stype) -> bool:
        blocks = config.SERVICE_BLOCKS.get(stype) or []
        if not blocks:
            return True
        if not self.use_planning or stype in HEAVY:
            return True
        idx = self.state[tid]["open_block"].get(stype, 0)
        return idx >= len(blocks) - 1

    def _job_process(self, job):
        tid = job["tid"]
        st = self.state[tid]
        delay = job.get("delay") or 0.0
        held_slot = False
        if delay > 0.05:
            yield self.env.timeout(delay)
        if st["status"] not in (OPERATIONAL, RESERVE, "waiting", MAINTENANCE):
            return
        waited = 0.0
        while self.slots_taken >= config.MAX_ON_SERVICE and waited < 36:
            st["blocked"] = True
            yield self.env.timeout(0.5)
            waited += 0.5
        if self.slots_taken >= config.MAX_ON_SERVICE:
            self.metrics["postponements"] += 1
            self.metrics["queue_events"] += 1
            return
        self.slots_taken += 1
        held_slot = True
        self.metrics["max_on_service"] = max(self.metrics["max_on_service"], self.slots_taken)
        st["busy"] = True
        st["blocked"] = False
        try:
            for step in job["steps"]:
                bay = self._grab(step["resource"])
                if step["resource"] == "lathe":
                    self.lathe_busy_until[0] = self.env.now + step["hours"]
                self._set_status(tid, MAINTENANCE, step["type"], bay=bay)
                ev = self._open_event(tid, step["type"], "calendar" if step["type"] in config.CALENDAR_TASKS else "scheduled", bay)
                start = self.env.now
                yield self.env.timeout(step["hours"])
                extra = self._extra_hours(step)
                if extra > 0.05:
                    yield self.env.timeout(extra)
                    self.metrics["additional_hours"] += extra
                    self.metrics["total_downtime_hours"] += extra
                    if step["resource"] == "lathe":
                        self.lathe_busy_until[0] = self.env.now
                        self.metrics["lathe_hours"] += extra
                self._release(step["resource"], bay)
                self._after_step(tid, step, ev, step["hours"])
                if step["resource"] == "lathe":
                    self.metrics["lathe_hours"] += step["hours"]
                    # Часы станка после 06:00 — уже удар по дневному графику.
                    night_end = (start // 24) * 24 + config.OPERATION_START_HOUR
                    if self.env.now > night_end:
                        spilled = min(step["hours"], self.env.now - night_end)
                        self.metrics["day_lathe_hours"] += spilled
                        if self.metrics["lathe_deficit_hour"] is None:
                            self.metrics["lathe_deficit_hour"] = start
                if step["type"] in config.CALENDAR_TASKS:
                    spec = config.CALENDAR_TASKS[step["type"]]
                    st["calendar_due"][step["type"]] = self.env.now + spec["interval_days"] * 24
            self._set_status(tid, OPERATIONAL)
            st["location"] = "spb"
        finally:
            if held_slot:
                self.slots_taken = max(0, self.slots_taken - 1)
            st["busy"] = False

    def _extra_hours(self, step) -> float:
        """Доля дополнительных работ. Не предписывается — выпадает в ходе ТО."""
        stype = step["type"]
        frac = config.UNPLANNED_OVERHEAD.get(stype)
        if not frac or stype in config.CALENDAR_TASKS:
            return 0.0
        if self.rng.random() > 0.35:
            return 0.0
        return step["hours"] * frac * self.rng.uniform(0.4, 1.0)

    def _after_step(self, tid, step, ev, hours):
        stype = step["type"]
        self._close_event(ev, hours)
        self.metrics["service_hours"] += hours
        self.metrics["total_downtime_hours"] += hours
        if stype == "wheelset_turning":
            self._turn_cars(tid, step.get("cars") or 1)
            self.metrics["total_services"] += 1
            self.metrics["services_by_type"][stype] += 1
            return
        if stype in config.CALENDAR_TASKS:
            self.metrics["total_services"] += 1
            self.metrics["services_by_type"][stype] += 1
            return
        if step.get("completes", True):
            self._reset_mileage(tid, stype)
            self.state[tid]["open_block"].pop(stype, None)
            self.metrics["total_services"] += 1
            self.metrics["services_by_type"][stype] += 1
        else:
            idx = self.state[tid]["open_block"].get(stype, 0) + 1
            self.state[tid]["open_block"][stype] = idx

    def _grab(self, resource: str):
        if resource == "jack":
            return self.free_jacks.pop() if self.free_jacks else 1
        if resource == "lathe":
            return 9
        return self.free_bays.pop() if self.free_bays else 1

    def _release(self, resource: str, bay):
        if resource == "jack" and bay is not None:
            self.free_jacks.add(bay)
        elif resource == "bay" and bay is not None:
            self.free_bays.add(bay)
        elif resource == "lathe":
            # занятость станка для метрики — по факту только что закончившейся работы
            pass

    def _turn_cars(self, tid, n):
        st = self.state[tid]
        order = sorted(range(len(st["cars"])), key=lambda i: st["cars"][i], reverse=True)
        for i in order[:n]:
            st["cars"][i] = 0.0
        st["m"]["wheelset_turning"] = max(st["cars"]) if st["cars"] else 0.0

    # ------------------------------------------------------- день, оборот
    def _run_day(self):
        """Сутки. По умолчанию — оборот пары 1 358 км.

        use_diagram включает нитки примера заказчика. На длинном горизонте
        они пока хуже держат состав у допуска в Москве, поэтому годовой
        прогон в интерфейсе идёт по проверенному контуру.
        """
        if self.use_diagram:
            yield from run_diagram_day(self)
            return
        yield from self._run_circulation()


    def _run_circulation(self):
        """Оборот пары 1 358 км. Резерв ротируется, а не сидит месяцами."""
        day_end = (self.env.now // 24) * 24 + 24
        comm = self._commissioned_count()
        need = required_count(comm)
        slack = service_slot_limit(comm)
        running = sum(1 for s in self.state.values()
                      if s["busy"] and s["status"] in (MAINTENANCE, BROKEN, "waiting"))
        free_slots = max(0, min(slack, config.MAX_ON_SERVICE) - running)

        spb = self._ready("spb")
        msk = self._ready("msk")
        # Кто уже не может закрыть пару — не занимает нитку.
        spb_run = [t for t in spb if self._km_to_hard(t) >= config.PAIR_KM]
        msk_run = [t for t in msk if self._km_to_hard(t) >= config.ROUTE_KM]
        for tid in spb + msk:
            if self._km_to_hard(tid) < config.RESERVE_MARGIN_KM:
                self.state[tid]["blocked"] = True

        reserve_pool = [t for t in spb if self._km_to_hard(t) >= config.RESERVE_MARGIN_KM]
        reserve_pool.sort(key=lambda t: (
            0 if self.state[t]["preferred_reserve"] else 1,
            self.state[t]["total"]))
        n_res = min(config.RESERVE_PER_CITY, len(reserve_pool), max(0, len(spb_run) - 8))
        reserve = reserve_pool[:n_res]
        runners = [t for t in spb_run if t not in reserve]
        if len(runners) < 8 and len(reserve) > config.RESERVE_PER_CITY - 1:
            extra = reserve[-(len(reserve) - max(0, config.RESERVE_PER_CITY - 1)):]
            # не раздуваем: если ниток не хватает, резерв выходит
            if len(runners) < need - len(msk_run):
                runners.extend(extra)
                reserve = [t for t in reserve if t not in extra]

        self.hot_reserve = {"spb": reserve, "msk": msk[:1]}
        if len(reserve) < config.RESERVE_PER_CITY:
            self.metrics["reserve_shortfall_days"] += 1

        # Сколько пар успевает состав до полуночи, если стартовать с 06:00
        # со сдвигом 12 минут.
        assigned = 0
        for i, tid in enumerate(runners):
            start = self.env.now + i * 0.2
            latest_pair_start = day_end - config.PAIR_HOURS
            if start > latest_pair_start:
                break
            hours_left = latest_pair_start - start
            n_pairs = int(hours_left // config.PAIR_HOURS) + 1
            n_pairs = max(1, min(2, n_pairs))
            km_left = self._km_to_hard(tid)
            n_pairs = min(n_pairs, int(km_left // config.PAIR_KM))
            if n_pairs <= 0:
                continue
            self.state[tid]["busy"] = True
            self.state[tid]["blocked"] = False
            self.metrics["departures"] += n_pairs * 2
            self.env.process(self._pair_process(tid, n_pairs))
            assigned += 1

        # Составы в Москве возвращаются одним рейсом. Это закрывает «висящие» пары.
        for i, tid in enumerate(msk_run):
            if self.state[tid]["busy"]:
                continue
            self.state[tid]["busy"] = True
            self.metrics["departures"] += 1
            self.env.process(self._one_way_process(tid, "msk"))

        target = max(assigned, need // 2)
        self.metrics["pairs_target"] += target * 2
        if self.scenario["mileage"] > 1.05:
            extra = max(1, int(round(0.2 * target)))
            self.metrics["pairs_target"] += extra * 2
            self.metrics["missed_departures"] += extra

        if self.env.now < day_end:
            yield self.env.timeout(day_end - self.env.now)

    def _designate_reserve(self):
        """Совместимость: резерв назначается внутри _run_day, с ротацией."""
        return

    def _ready(self, city) -> List[int]:
        out = []
        for tid, st in self.state.items():
            if st["location"] == city and not st["busy"] and not st["blocked"]:
                if st["status"] in (OPERATIONAL, RESERVE) and st["commissioned_hour"] <= self.env.now:
                    out.append(tid)
        return out

    def _pair_process(self, tid, n_pairs=2, stay_msk=False):
        st = self.state[tid]
        day_end = (self.env.now // 24) * 24 + 24
        try:
            if stay_msk:
                failed = yield from self._leg(tid, "spb")
                if not failed and st["status"] != BROKEN:
                    st["location"] = "msk"
                    self._set_status(tid, OPERATIONAL)
                return
            for _ in range(max(0, int(n_pairs))):
                if self.env.now + config.PAIR_HOURS > day_end + 0.05:
                    return
                if self._km_to_hard(tid) < config.PAIR_KM:
                    st["blocked"] = True
                    return
                failed = yield from self._leg(tid, "spb")
                if failed or st["status"] == BROKEN:
                    return
                failed = yield from self._leg(tid, "msk")
                if failed or st["status"] == BROKEN:
                    return
                self.metrics["pairs_completed"] += 1
                st["location"] = "spb"
                self._set_status(tid, OPERATIONAL)
                if not self.use_planning:
                    yield from self._reactive_pull(tid)
                    if st["status"] != OPERATIONAL or st["location"] != "spb":
                        return
        finally:
            st["busy"] = False
            if st["status"] in ("line", "equipping"):
                self._set_status(tid, OPERATIONAL)

    def _one_way_process(self, tid, origin, stay=False):
        st = self.state[tid]
        try:
            failed = yield from self._leg(tid, origin)
            if failed:
                return
            dest = "msk" if origin == "spb" else "spb"
            st["location"] = dest
            self._set_status(tid, OPERATIONAL)
            if dest == "spb" and not self.use_planning:
                yield from self._reactive_pull(tid)
        finally:
            st["busy"] = False

    def _leg(self, tid, origin) -> bool:
        """Один рейс 679 км. True — рейс сорван отказом."""
        dest = "msk" if origin == "spb" else "spb"
        st = self.state[tid]
        self._set_status(tid, "line", location=f"to_{dest}")
        half = config.TRAVEL_HOURS / 2
        yield self.env.timeout(half)
        self._add_mileage(tid, config.ROUTE_KM / 2)
        self._log_trip(tid, origin)
        kind = self._roll_failure(tid, config.ROUTE_KM / 2)
        if kind:
            self.metrics["disrupted_pairs"] += 1
            yield from self._repair(tid, dest if self.rng.random() < 0.5 else origin, kind)
            self._try_cover(dest)
            return True
        yield self.env.timeout(half)
        self._add_mileage(tid, config.ROUTE_KM / 2)
        self._log_trip(tid, origin)
        st["location"] = dest
        kind = self._roll_failure(tid, config.ROUTE_KM / 2)
        if kind:
            self.metrics["disrupted_pairs"] += 1
            yield from self._repair(tid, dest, kind)
            self._try_cover(dest)
            return True
        yield from self._equip(tid, dest)
        return False

    def _equip(self, tid, city):
        self._set_status(tid, "equipping")
        # Ёмкость пункта: не больше 4 составов. Очередь — это задержка оборота.
        # Модель очереди упрощена до фиксированных 0,7 ч: составы уже разведены
        # по отправлению на 12 минут, одновременность не превышает 4.
        yield self.env.timeout(config.EQUIP_HOURS)
        self.metrics["equipping_hours"] += config.EQUIP_HOURS

    def _try_cover(self, city):
        if city not in ("spb", "msk"):
            city = "msk" if "msk" in str(city) else "spb"
        for tid in self.hot_reserve.get(city, []):
            st = self.state[tid]
            if st["busy"] or st["status"] not in (OPERATIONAL, RESERVE):
                continue
            if st["location"] != city:
                continue
            st["busy"] = True
            self.metrics["reserve_activations"] += 1
            self.metrics["departures"] += 1
            self.env.process(self._one_way_process(tid, city, stay=True))
            return True
        self.metrics["missed_departures"] += 1
        return False

    def _reactive_pull(self, tid):
        st = self.state[tid]
        if st["location"] != "spb" or st["status"] not in (OPERATIONAL, RESERVE):
            return
        stype = None
        for candidate_type in ("IS700", "IS600", "IS540", "IS530", "IS520", "IS510", "IS200", "IS100"):
            if self._ratio(tid, candidate_type) >= 1.0:
                stype = candidate_type
                break
        cars = self._cars_due(tid, threshold=1.0)
        if stype is None and not cars:
            return
        if self.slots_taken >= config.MAX_ON_SERVICE:
            st["blocked"] = True
            self.metrics["queue_events"] += 1
            return
        if stype:
            hours, _, resource = self._next_chunk(tid, stype)
            job = self._pack_candidate(
                tid, stype, hours, urgency=5, past_hard=True,
                resource=resource, spill=True, heavy=stype in HEAVY)
        else:
            job = self._pack_candidate(
                tid, "wheelset_turning",
                config.TURNING_HOURS_PER_CAR * min(cars, 3),
                urgency=5, past_hard=True, resource="lathe", spill=False,
                heavy=False, cars=min(cars, 3))
        yield from self._job_process(job)

    # ------------------------------------------------------- отказы
    def _roll_failure(self, tid, km) -> Optional[str]:
        p_line, p_unp = self._failure_probs(tid, km)
        u = self.rng.random()
        if u < p_line:
            return "line"
        if u < p_line + p_unp:
            return "unplanned"
        return None

    def _failure_probs(self, tid, km):
        st = self.state[tid]
        r100 = self._ratio(tid, "IS100")
        r540 = self._ratio(tid, "IS540")
        wheel = max(st["cars"]) / config.MILEAGE_TRIGGERS["wheelset_turning"]
        wear = 0.0
        if r100 > 0.75:
            wear += ((r100 - 0.75) / 0.35) ** 2 * config.FAILURE_COEF_OVER_IS100
        if r540 > 0.85:
            wear += (r540 - 0.85) * config.FAILURE_COEF_WEAR_IS540
        if wheel > 0.9:
            wear += (wheel - 0.9) * 1.2
        scale = self.scenario["breakdown"]
        lam_line = config.LINE_FAILURE_PER_MILLION_KM * scale
        lam_unp = config.UNPLANNED_PER_MILLION_KM * scale * (1.0 + wear)
        p_line = 1 - math.exp(-lam_line * km / 1e6)
        p_unp = 1 - math.exp(-lam_unp * km / 1e6)
        return min(p_line, 0.5), min(p_unp, 0.5)

    def _pick_node(self):
        r = self.rng.random()
        acc = 0.0
        for node in config.FAILURE_NODES:
            acc += node["weight"]
            if r <= acc:
                return node
        return config.FAILURE_NODES[-1]

    def _repair(self, tid, place, kind):
        st = self.state[tid]
        node = self._pick_node()
        self.metrics["total_breakdowns"] += 1
        self.metrics["nodes"][node["code"]] += 1
        if kind == "line":
            self.metrics["line_failures"] += 1
        self.failure_log.append({
            "train_id": tid, "hour": self.env.now,
            "m100": st["m"]["IS100"], "m200": st["m"]["IS200"],
            "m540": st["m"]["IS540"], "total": st["total"],
            "node": node["code"], "kind": kind, "place": place,
        })
        desc = f"{ru_node(node['code'])}. {'Отказ в пути' if kind == 'line' else 'Внеплановый заход'}."
        # Чат: в Москве возможны внеплановые работы, включая замену КП. Плановые циклы — нет.
        if place == "msk" and self.moscow_repair_used < config.MOSCOW_UNPLANNED_CAPACITY:
            self.moscow_repair_used += 1
            dur = self.rng.uniform(config.MOSCOW_REPAIR_HOURS_MIN, config.MOSCOW_REPAIR_HOURS_MAX)
            self._set_status(tid, BROKEN, "emergency_repair")
            ev = self._open_event(tid, "emergency_repair", "breakdown", None, desc + " Ремонт в Москве.")
            try:
                yield self.env.timeout(dur)
            finally:
                self.moscow_repair_used = max(0, self.moscow_repair_used - 1)
            self._close_event(ev, dur)
            self.metrics["total_downtime_hours"] += dur
            st["location"] = "msk"
            self._set_status(tid, OPERATIONAL)
            return

        if place != "spb":
            self._set_status(tid, "line", location="to_spb")
            self.metrics["deadhead"] += 1
            yield self.env.timeout(config.TRAVEL_HOURS)
            st["location"] = "spb"
        while self.slots_taken >= config.MAX_ON_SERVICE:
            self._set_status(tid, "waiting", "emergency_repair")
            self.metrics["queue_events"] += 1
            yield self.env.timeout(1.0)
        self.slots_taken += 1
        self.metrics["max_on_service"] = max(self.metrics["max_on_service"], self.slots_taken)
        dur = self.rng.uniform(config.REPAIR_HOURS_MIN, config.REPAIR_HOURS_MAX)
        if kind == "line":
            dur *= 1.2
        bay = self._grab("bay")
        self._set_status(tid, BROKEN, "emergency_repair", bay=bay)
        ev = self._open_event(tid, "emergency_repair", "breakdown", bay, desc + " Ремонт в Обухово.")
        try:
            yield self.env.timeout(dur)
        finally:
            self._release("bay", bay)
            self.slots_taken = max(0, self.slots_taken - 1)
        self._close_event(ev, dur)
        self.metrics["total_downtime_hours"] += dur
        st["location"] = "spb"
        self._set_status(tid, OPERATIONAL)

    # ------------------------------------------------------- пробег и пороги
    def _add_mileage(self, tid, km):
        km *= self.scenario["mileage"] if False else 1.0
        # Пробег рейса фиксирован (679 км). Сценарий «высокая нагрузка»
        # добавляет рейсы, а не удлиняет перегон.
        st = self.state[tid]
        st["total"] += km
        for key in st["m"]:
            st["m"][key] += km
        for i in range(len(st["cars"])):
            st["cars"][i] += km
        st["m"]["wheelset_turning"] = max(st["cars"])

    def _reset_mileage(self, tid, stype):
        st = self.state[tid]
        for level in config.SERVICE_HIERARCHY.get(stype, [stype]):
            if level in st["m"] and level != "wheelset_turning":
                st["m"][level] = 0.0

    def _ratio(self, tid, stype) -> float:
        trigger = config.MILEAGE_TRIGGERS.get(stype) or 1
        return self.state[tid]["m"].get(stype, 0.0) / trigger

    def _hard(self, stype) -> float:
        return config.MILEAGE_TRIGGERS[stype] * (1 + config.MILEAGE_TOLERANCE[stype])

    def _km_to_hard(self, tid) -> float:
        st = self.state[tid]
        rems = []
        for stype in ("IS100", "IS200", "IS510", "IS520", "IS530", "IS540", "IS600", "IS700"):
            rems.append(self._hard(stype) - st["m"][stype])
        rems.append(self._hard("wheelset_turning") - max(st["cars"]))
        for code, due in st["calendar_due"].items():
            if due - self.env.now < 36:
                rems.append(0.0)
        return min(rems) if rems else 1e9

    def _cars_due(self, tid, threshold=None) -> int:
        if threshold is None:
            threshold = 0.85 if self.use_planning else 1.0
        limit = config.MILEAGE_TRIGGERS["wheelset_turning"] * threshold
        return sum(1 for km in self.state[tid]["cars"] if km >= limit)

    def _needs_depot(self, tid) -> bool:
        if self._cars_due(tid):
            return True
        thr = 0.85 if self.use_planning else 1.0
        for stype in ("IS100", "IS200", "IS510", "IS520", "IS530", "IS540", "IS600", "IS700"):
            if self._ratio(tid, stype) >= thr:
                return True
        for due in self.state[tid]["calendar_due"].values():
            if due - self.env.now < 48:
                return True
        return False

    def _expected_daily_km(self) -> float:
        # Средний рейс оборота: около 2 500 км на состав, который вышел на нитки.
        return config.PAIR_KM * 1.8

    # ------------------------------------------------------- статусы, события
    def _set_status(self, tid, status, stype=None, bay=None, location=None):
        st = self.state[tid]
        if location:
            st["location"] = location
        self._segment(tid, status, stype, bay)
        st["status"] = status

    def _segment(self, tid, status, stype=None, bay=None):
        prev = self._open_segments.pop(tid, None)
        if prev:
            prev["end_hour"] = self.env.now
            self.segments.append(prev)
        self._open_segments[tid] = {
            "train_id": tid, "status": status, "service_type": stype,
            "start_hour": self.env.now, "end_hour": None, "bay": bay,
            "location": self.state[tid]["location"],
        }

    def _close_all_segments(self):
        for seg in self._open_segments.values():
            seg["end_hour"] = self.env.now
            self.segments.append(seg)
        self._open_segments.clear()

    def _log_trip(self, tid, origin):
        st = self.state[tid]
        self.trip_log.append({
            "train_id": tid, "hour": self.env.now, "km": config.ROUTE_KM / 2,
            "m100": st["m"]["IS100"], "m200": st["m"]["IS200"],
            "m540": st["m"]["IS540"], "total": st["total"], "origin": origin,
        })

    def _open_event(self, tid, stype, reason, bay=None, description=None):
        ev = {
            "train_id": tid, "service_type": stype,
            "scheduled_start": hour_to_datetime(self.env.now),
            "scheduled_end": None, "planned_duration_hours": None,
            "actual_start": hour_to_datetime(self.env.now), "actual_end": None,
            "actual_duration_hours": None, "status": "in_progress",
            "depot_bay": bay, "reason": reason,
            "work_description": description or ru_service(stype),
        }
        self._open_events.append(ev)
        return ev

    def _close_event(self, ev, dur):
        ev["actual_end"] = hour_to_datetime(self.env.now)
        ev["scheduled_end"] = ev["actual_end"]
        ev["planned_duration_hours"] = dur
        ev["actual_duration_hours"] = dur
        ev["status"] = "completed"
        if ev in self._open_events:
            self._open_events.remove(ev)
        self.event_rows.append(ev)

    def _flush_open_events(self):
        for ev in list(self._open_events):
            dur = max(0.1, (self.env.now - (ev["actual_start"] - hour_to_datetime(0)).total_seconds() / 3600))
            self._close_event(ev, dur)

    # ------------------------------------------------------- метрики
    def _collector(self):
        while True:
            self._sample()
            if self.callback and int(self.env.now) % 168 == 0 and self.env.now > 0:
                try:
                    comm = self._commissioned_count()
                    op = self._available_count()
                    self.callback(self.env.now, (op / comm) if comm else 1.0, self.metrics)
                except Exception:
                    pass
            yield self.env.timeout(1.0)

    def _sample(self):
        comm = self._commissioned_count()
        op = self._available_count()
        on_service = sum(1 for s in self.state.values()
                         if s["status"] in (MAINTENANCE, BROKEN, "waiting")
                         and s["commissioned_hour"] <= self.env.now)
        broken = sum(1 for s in self.state.values() if s["status"] == BROKEN)
        lathe_busy = sum(1 for t in self.lathe_busy_until if t > self.env.now)
        # lathe_busy_until не всегда заполнен — считаем по сегментам статуса нет.
        # Дополняем: если есть открытый сегмент обточки, станок занят.
        if any(seg.get("service_type") == "wheelset_turning"
               for seg in self._open_segments.values()):
            lathe_busy = max(lathe_busy, 1)
        avail = op / comm if comm else 1.0
        self.hourly.append((self.env.now, avail, op, comm, on_service, broken, lathe_busy))
        self.metrics["hours_sampled"] += 1
        if comm and avail + 1e-9 < config.TARGET_AVAILABILITY:
            self.metrics["hours_below"] += 1
        self.metrics["max_on_service"] = max(self.metrics["max_on_service"], self.slots_taken)
        if int(self.env.now) % 24 == config.OPERATION_START_HOUR + 1:
            self.metrics["coverage_days"] += 1
            if op >= required_count(comm):
                self.metrics["coverage_days_ok"] += 1

    def _commissioned_count(self) -> int:
        return sum(1 for s in self.state.values() if s["commissioned_hour"] <= self.env.now + 1e-6
                   and s["status"] != NOT_DELIVERED)

    def _available_count(self) -> int:
        n = 0
        for s in self.state.values():
            if s["commissioned_hour"] > self.env.now + 1e-6 or s["status"] == NOT_DELIVERED:
                continue
            if s["status"] not in AVAILABLE:
                continue
            if s["blocked"] and self._past_any_hard(s):
                continue
            n += 1
        return n

    def _past_any_hard(self, st) -> bool:
        for stype in ("IS100", "IS200", "IS510", "IS520", "IS530", "IS540", "IS600", "IS700"):
            if st["m"][stype] >= self._hard(stype) - 1:
                return True
        if st["cars"] and max(st["cars"]) >= self._hard("wheelset_turning") - 1:
            return True
        for due in st["calendar_due"].values():
            if self.env.now >= due:
                return True
        return False

    def _sync_state_light(self):
        for tid, s in self.state.items():
            train = self.session.get(Train, tid)
            if not train:
                continue
            train.status = s["status"] if s["status"] in (
                OPERATIONAL, RESERVE, NOT_DELIVERED, MAINTENANCE, BROKEN) else OPERATIONAL
            train.total_mileage = s["total"]
            train.location = s["location"] if s["location"] in ("spb", "msk") else "spb"
            train.mileage_since_is100 = s["m"]["IS100"]
            train.mileage_since_is200 = s["m"]["IS200"]
            train.mileage_since_is510 = s["m"]["IS510"]
            train.mileage_since_is520 = s["m"]["IS520"]
            train.mileage_since_is530 = s["m"]["IS530"]
            train.mileage_since_is540 = s["m"]["IS540"]
            train.mileage_since_is600 = s["m"]["IS600"]
            train.mileage_since_is700 = s["m"]["IS700"]
            train.mileage_since_wheelset = s["m"]["wheelset_turning"]
        self.session.commit()

    def _sync_db(self):
        self._sync_state_light()
        self.session.query(MaintenanceEvent).delete()
        for ev in self.event_rows:
            self.session.add(MaintenanceEvent(**{k: ev[k] for k in (
                "train_id", "service_type", "scheduled_start", "scheduled_end",
                "planned_duration_hours", "actual_start", "actual_end",
                "actual_duration_hours", "status", "depot_bay", "reason",
                "work_description")}))
        self.session.query(ServiceSegment).delete()
        for seg in self.segments:
            if seg.get("status") not in (MAINTENANCE, BROKEN, "waiting"):
                continue
            self.session.add(ServiceSegment(
                train_id=seg["train_id"], status=seg["status"],
                service_type=seg.get("service_type"),
                start_hour=seg["start_hour"], end_hour=seg["end_hour"],
                bay=seg.get("bay")))
        self.session.commit()

    # ------------------------------------------------------- результаты
    def get_results(self) -> Dict:
        m = self.metrics
        hourly = self.hourly or [(0, 1, 0, 0, 0, 0, 0)]
        avails = [a for _, a, *_ in hourly]
        avg = sum(avails) / len(avails)
        op_avails = [a for h, a, *_ in hourly if 6 <= (h % 24) < 24]
        op_avg = sum(op_avails) / len(op_avails) if op_avails else avg
        # Загрузка стойл — по часам, когда на сервисе есть составы, нормированная на 8.
        on_svc = [row[4] for row in hourly]
        lathe = [1 if row[6] else 0 for row in hourly]
        # Суточные сервисные часы для равномерности и проверки лимита 120.
        by_day_service = defaultdict(float)
        for seg in self.segments:
            if seg.get("status") not in (MAINTENANCE, BROKEN):
                continue
            start = seg["start_hour"] or 0
            end = seg["end_hour"] if seg["end_hour"] is not None else self.env.now
            day = int(start // 24)
            by_day_service[day] += max(0.0, end - start)
        months = defaultdict(float)
        for day, hours in by_day_service.items():
            months[day // 30] += hours
        month_list = [months[i] for i in range(max(months) + 1)] if months else []
        days_over_120 = sum(1 for h in by_day_service.values() if h > config.DAILY_SERVICE_HOUR_CAP)
        pairs_target = m["pairs_target"] or 1
        return {
            "duration_years": self.env.now / 8760,
            "average_availability": avg,
            "operating_availability": op_avg,
            "min_availability": min(avails) if avails else 0.0,
            "coverage": (m["coverage_days_ok"] / m["coverage_days"]) if m["coverage_days"] else 0.0,
            "hours_meeting_target": 1 - (m["hours_below"] / m["hours_sampled"]) if m["hours_sampled"] else 0.0,
            "total_services": m["total_services"],
            "services_by_type": dict(m["services_by_type"]),
            "total_breakdowns": m["total_breakdowns"],
            "line_failures": m["line_failures"],
            "nodes": dict(m["nodes"]),
            "total_downtime_hours": m["total_downtime_hours"],
            "service_hours": m["service_hours"],
            "additional_hours": m["additional_hours"],
            "equipping_hours": m["equipping_hours"],
            "average_depot_utilization": (sum(on_svc) / len(on_svc) / config.DEPOT_CAPACITY) if on_svc else 0.0,
            "average_lathe_utilization": sum(lathe) / len(lathe) if lathe else 0.0,
            "lathe_hours": m["lathe_hours"],
            "day_lathe_hours": m["day_lathe_hours"],
            "postponements": m["postponements"],
            "reserve_activations": m["reserve_activations"],
            "missed_departures": m["missed_departures"],
            "disrupted_pairs": m["disrupted_pairs"],
            "pairs_completed": m["pairs_completed"],
            "pairs_target": m["pairs_target"],
            "schedule_fulfillment": m["pairs_completed"] / pairs_target,
            "queue_events": m["queue_events"],
            "wheel_overdue": m["wheel_overdue"],
            "mileage_overdue": m["mileage_overdue"],
            "max_on_service": m["max_on_service"],
            "days_over_120h": days_over_120,
            "load_cv": monthly_load_cv(month_list),
            "lathe_deficit_hour": m["lathe_deficit_hour"],
            "strategic_events": m["strategic_events"],
            "deadhead": m["deadhead"],
            "deadhead_km": m.get("deadhead_km", 0.0),
            "diagram_covered": m.get("diagram_covered", 0),
            "diagram_target": m.get("diagram_target", 0),
            "coupled_departures": m.get("coupled_departures", 0),
            "single_departures": m.get("single_departures", 0),
            "departures": m["departures"],
            "reserve_shortfall_days": m["reserve_shortfall_days"],
            "fleet_km": sum(s["total"] for s in self.state.values()),
            "ml_data_collected": len(self.trip_log),
            "scenario": self.scenario_name,
            "use_planning": self.use_planning,
        }

    def get_segments_dataframe(self) -> pd.DataFrame:
        cols = ["train_id", "status", "service_type", "start_hour", "end_hour", "bay", "location"]
        if not self.segments:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame(self.segments)

    def get_trips_dataframe(self) -> pd.DataFrame:
        cols = ["train_id", "hour", "km", "m100", "m200", "m540", "total", "origin"]
        if not self.trip_log:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame(self.trip_log)

    def get_hourly_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.hourly, columns=[
            "hour", "avail", "available_n", "commissioned", "on_service", "broken", "lathe"])

    def get_ml_data(self) -> pd.DataFrame:
        trips = pd.DataFrame(self.trip_log)
        fails = pd.DataFrame(self.failure_log)
        if trips.empty:
            return trips
        trips = trips.sort_values("hour").reset_index(drop=True)
        trips["failure"] = 0
        if not fails.empty:
            fails = fails[["train_id", "hour"]].copy()
            fails["next_fail"] = fails["hour"]
            fails = fails.sort_values("hour")
            merged = pd.merge_asof(
                trips[["train_id", "hour"]].sort_values("hour"), fails,
                on="hour", by="train_id", direction="forward",
                tolerance=config.ML_LABEL_HORIZON_HOURS)
            trips["failure"] = merged["next_fail"].notna().astype(int)
        return trips

    def export_results(self, filename: str = "simulation_results.xlsx"):
        hourly = self.get_hourly_dataframe()
        origin_ru = {"spb": "Санкт-Петербург", "msk": "Москва"}
        with pd.ExcelWriter(filename) as writer:
            trips = self.get_trips_dataframe()
            if not trips.empty:
                trips = trips.copy()
                trips["origin"] = trips["origin"].map(lambda x: origin_ru.get(x, x))
            trips.rename(columns={
                "train_id": "Состав", "hour": "Час", "km": "Км",
                "m100": "Пробег с IS100", "m200": "Пробег с IS200",
                "m540": "Пробег с IS540", "total": "Суммарный пробег",
                "origin": "Откуда",
            }).to_excel(writer, sheet_name="Рейсы", index=False)
            seg = self.get_segments_dataframe()
            if not seg.empty:
                seg = seg.copy()
                seg["status"] = seg["status"].map(ru_status)
                seg["service_type"] = seg["service_type"].map(
                    lambda x: ru_service(x) if x else "—")
                seg["location"] = seg["location"].map(ru_location)
                seg = seg.rename(columns={
                    "train_id": "Состав", "status": "Статус", "service_type": "Работа",
                    "start_hour": "Начало, ч", "end_hour": "Конец, ч",
                    "bay": "Позиция", "location": "Дислокация",
                })
            seg.to_excel(writer, sheet_name="Интервалы", index=False)
            hourly.rename(columns={
                "hour": "Час", "avail": "Готовность", "available_n": "Доступно",
                "commissioned": "Поставлено", "on_service": "На сервисе",
                "broken": "В ремонте", "lathe": "Станок занят",
            }).to_excel(writer, sheet_name="Готовность", index=False)
            ml = self.get_ml_data()
            if not ml.empty:
                ml = ml.copy()
                if "origin" in ml.columns:
                    ml["origin"] = ml["origin"].map(lambda x: origin_ru.get(x, x))
                if "failure" in ml.columns:
                    ml["failure"] = ml["failure"].map({0: "Нет", 1: "Да"}).fillna("Нет")
                ml = ml.rename(columns={
                    "train_id": "Состав", "hour": "Час", "km": "Км полурейса",
                    "m100": "Пробег с IS100", "m200": "Пробег с IS200",
                    "m540": "Пробег с IS540", "total": "Суммарный пробег",
                    "origin": "Откуда", "failure": "Отказ в ближайшие 72 ч",
                })
            ml.to_excel(writer, sheet_name="Данные для моделей", index=False)
            pd.DataFrame(timetable_rows()).to_excel(
                writer, sheet_name="График оборота", index=False)
        logger.info("Экспорт: {}", filename)


FleetSimulator = IntegratedFleetSimulator
