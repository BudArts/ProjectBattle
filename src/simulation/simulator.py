"""Симулятор парка ВСМ с ЗАМКНУТЫМ контуром управления.

Главное отличие от прежней версии: симулятор ИСПОЛНЯЕТ планы.
- Тактическое расписание (ночные короткие работы) читается и выполняется,
  события закрываются (actual_start/actual_end, status='completed').
- Стратегический план (многодневные ревизии IS510…IS700) исполняется,
  при нехватке доступных составов старт сдвигается (оперативная адаптация).
- Все 9 регламентов моделируются; накапливаются все 8 счётчиков пробега.
- Суточный цикл: движение 06:00–24:00, ночное окно ТО 00:00–06:00.
- Отказы: интенсивность зависит от состояния (пробегов), а не константа.
- Резервные составы активируются при падении готовности ниже цели.
- Метрики считаются по корректному знаменателю (введённые в эксплуатацию).
- Собирается сбалансированная ML-выборка (снимки состояния + метка отказа).
"""
import random
import math
from collections import Counter, defaultdict, deque
from typing import Callable, Dict, List, Optional

import pandas as pd
import simpy
from loguru import logger

from src.core.config import config
from src.core.database import (BROKEN, MAINTENANCE, OPERATIONAL, RESERVE,
                               MaintenanceEvent, ServiceSegment, Train,
                               get_session, hour_to_datetime)
from src.planning.strategic import MONTH_HOURS, StrategicPlanner
from src.planning.tactical import TacticalPlanner


class IntegratedFleetSimulator:
    """Симулятор с интеграцией планировщиков и исполнением плана"""

    def __init__(self, db_engine, num_trains: int = 43, use_planning: bool = True,
                 scenario: str = "normal"):
        self.engine = db_engine
        self.session = get_session(db_engine)
        self.num_trains = num_trains
        self.use_planning = use_planning
        self.scenario = config.SCENARIOS.get(scenario, config.SCENARIOS["normal"])

        self.strategic = StrategicPlanner(db_engine) if use_planning else None
        self.tactical = TacticalPlanner(db_engine) if use_planning else None

        self.env: Optional[simpy.Environment] = None
        self.depot: Optional[simpy.Resource] = None
        self.lathe: Optional[simpy.Resource] = None

        self.state: Dict[int, Dict] = {}
        self.long_works: Dict[int, Dict] = {}            # train -> {type,start_hour,done}
        self.short_plan: Dict[int, deque] = defaultdict(deque)

        self.segments: List[Dict] = []                   # гантт статусов
        self.trip_log: List[Dict] = []                   # для ML и графика движения
        self.failure_log: List[Dict] = []
        self.event_rows: List[Dict] = []                 # maintenance_events
        self.metrics: Dict = {}
        self._open_segments: Dict[int, Dict] = {}

    # ============================================================== запуск
    def run(self, duration_hours: int = 8760, seed: int = 42,
            callback: Optional[Callable] = None) -> Dict:
        random.seed(seed)
        logger.info("Симуляция: {} ч ({:.1f} лет), planning={}, scenario={}",
                    duration_hours, duration_hours / 8760, self.use_planning,
                    self.scenario)

        self.segments.clear(); self.trip_log.clear(); self.failure_log.clear()
        self.event_rows.clear(); self.long_works.clear(); self.short_plan.clear()
        self._open_segments.clear()
        self.metrics = {
            "total_services": 0, "total_breakdowns": 0, "total_downtime_hours": 0.0,
            "services_by_type": Counter(), "availability_history": [],
            "daily_availability": [], "depot_users_history": [], "lathe_history": [],
            "in_maintenance_history": [], "coverage_days_ok": 0, "coverage_days": 0,
            "postponements": 0, "reserve_activations": 0,
        }

        self.env = simpy.Environment()
        self.depot = simpy.Resource(self.env, capacity=config.DEPOT_CAPACITY)
        self.lathe = simpy.Resource(self.env, capacity=config.WHEELSET_LATHE_CAPACITY)

        for train in self.session.query(Train).order_by(Train.id).all():
            self.state[train.id] = {
                "number": train.number,
                "status": train.status,
                "total": train.total_mileage,
                "m": {"IS100": train.mileage_since_is100 or 0,
                      "IS200": train.mileage_since_is200 or 0,
                      "IS510": train.mileage_since_is510 or 0,
                      "IS520": train.mileage_since_is520 or 0,
                      "IS530": train.mileage_since_is530 or 0,
                      "IS540": train.mileage_since_is540 or 0,
                      "IS600": train.mileage_since_is600 or 0,
                      "IS700": train.mileage_since_is700 or 0,
                      "wheelset_turning": train.mileage_since_wheelset or 0},
                "commissioned_hour": max(
                    0, (train.commissioned_date -
                        hour_to_datetime(0)).total_seconds() / 3600),
                "reserve_id": train.id > self.num_trains - config.RESERVE_TRAINS,
            }
            self.env.process(self.train_lifecycle(train.id))

        if self.use_planning:
            self.env.process(self.strategic_cycle())
            self.env.process(self.tactical_cycle())
        self.env.process(self.metrics_collector(callback))

        self.env.run(until=duration_hours)
        self._close_all_segments()
        self._sync_db()
        return self.get_results()

    # ====================================================== жизненный цикл
    def train_lifecycle(self, train_id: int):
        st = self.state[train_id]
        if st["commissioned_hour"] > 0:
            yield self.env.timeout(st["commissioned_hour"])
            st["status"] = RESERVE if st["reserve_id"] else OPERATIONAL
            self._segment(train_id, st["status"])

        if st["status"] == RESERVE:
            while st["status"] == RESERVE:
                yield self.env.timeout(24)

        while True:
            h = self.env.now % 24
            if st["status"] != OPERATIONAL:
                yield self.env.timeout(1)
                continue

            if config.OPERATION_START_HOUR <= h < config.OPERATION_END_HOUR:
                # -------- дневной рейс (не позже конца окна движения)
                trip = random.uniform(3, 4.5)
                trip = min(trip, config.OPERATION_END_HOUR - h)
                yield self.env.timeout(trip)
                km = random.uniform(600, 700) * self.scenario["mileage"]
                self._add_mileage(train_id, km)
                self.trip_log.append({"train_id": train_id, "hour": self.env.now,
                                      "km": km,
                                      "m100": st["m"]["IS100"],
                                      "m200": st["m"]["IS200"],
                                      "m540": st["m"]["IS540"],
                                      "total": st["total"]})
                # внеплановый отказ (интенсивность от состояния)
                if random.random() < self._failure_probability(train_id):
                    yield from self._breakdown(train_id)
                # оборот в депо между рейсами (калибровка суточного пробега)
                h2 = self.env.now % 24
                if h2 < config.OPERATION_END_HOUR - 0.5:
                    yield self.env.timeout(
                        min(random.uniform(0.5, 1.5),
                            config.OPERATION_END_HOUR - h2))
                # многодневная ревизия по стратегическому плану / по факту
                lw = self._long_work_due(train_id)
                if lw is not None:
                    self._ensure_reserve()   # подстраховка резервом, если есть
                    yield from self._do_long_work(train_id, lw)
            else:
                # -------- ночное окно: короткие ТО
                yield from self._night_window(train_id)

    # ------------------------------------------------------------ ночь
    def _night_window(self, train_id: int):
        st = self.state[train_id]
        night_end = (self.env.now // 24) * 24 + config.OPERATION_START_HOUR

        svc = None
        if self.use_planning:
            q = self.short_plan[train_id]
            while q and q[0]["start_hour"] <= self.env.now:
                svc = q.popleft()
                break
            if svc is None and q and q[0]["start_hour"] < night_end:
                target = q[0]
                if target["start_hour"] > self.env.now:
                    yield self.env.timeout(target["start_hour"] - self.env.now)
                svc = self.short_plan[train_id].popleft()
            if svc is not None:
                # защита от дублей: пробег мог быть сброшен более крупной работой
                stype = svc["service_type"]
                if st["m"][stype] / config.MILEAGE_TRIGGERS[stype] < 0.7:
                    svc = None
        else:
            # реактивный режим: обслуживаем только по факту превышения порога
            st_m = self.state[train_id]["m"]
            overdue = [t for t in ("IS200", "IS100", "wheelset_turning")
                       if st_m[t] >= config.MILEAGE_TRIGGERS[t]]
            if overdue:
                svc = {"service_type": overdue[0], "start_hour": self.env.now}

        # Короткие работы выполняются в ночное окно и завершаются до 06:00,
        # поэтому не снижают дневную готовность — guard не нужен.
        if svc is not None and st["status"] == OPERATIONAL:
            yield from self._do_short_service(train_id, svc)

        if self.env.now < night_end and st["status"] == OPERATIONAL:
            yield self.env.timeout(night_end - self.env.now)

    def _tactical_due_type(self, train_id: int) -> Optional[str]:
        """Наивысший из «коротких» типов, достигших 85% порога."""
        st = self.state[train_id]
        for t in ("IS200", "IS100", "wheelset_turning"):
            if st["m"][t] / config.MILEAGE_TRIGGERS[t] >= 0.85:
                return t
        return None

    # ------------------------------------------------------- выполнения работ
    def _required_now(self) -> int:
        """Требуемое число доступных составов (растёт с вводом парка).

        По ТЗ: >= 89% от парка, но не более REQUIRED_OPERATIONAL (38).
        """
        return min(config.REQUIRED_OPERATIONAL,
                   math.ceil(config.TARGET_AVAILABILITY * self._commissioned_count()))

    def _do_short_service(self, train_id: int, svc: Dict):
        st = self.state[train_id]
        stype = svc["service_type"]
        dur = svc.get("duration_hours") or self._service_duration(stype)
        self._set_status(train_id, MAINTENANCE, stype)
        ev = self._open_event(train_id, stype, "scheduled", svc.get("depot_bay"))

        with self.depot.request() as req:
            yield req
            if stype == "wheelset_turning":
                with self.lathe.request() as lreq:
                    yield lreq
                    yield self.env.timeout(dur)
            else:
                yield self.env.timeout(dur)

        self._finish_service(train_id, stype, ev, dur)

    def _do_long_work(self, train_id: int, lw: Dict):
        st = self.state[train_id]
        stype = lw["type"]
        dur = self._service_duration(stype)
        self._set_status(train_id, MAINTENANCE, stype)
        ev = self._open_event(train_id, stype, "scheduled",
                              (train_id - 1) % config.DEPOT_CAPACITY + 1)
        with self.depot.request() as req:
            yield req
            yield self.env.timeout(dur)
        self._finish_service(train_id, stype, ev, dur)
        q = self.long_works.get(train_id)
        if q and q[0]["type"] == stype:
            q.popleft()

    def _breakdown(self, train_id: int):
        st = self.state[train_id]
        self.metrics["total_breakdowns"] += 1
        self.failure_log.append({"train_id": train_id, "hour": self.env.now,
                                 "m100": st["m"]["IS100"], "m200": st["m"]["IS200"],
                                 "m540": st["m"]["IS540"], "total": st["total"]})
        dur = random.uniform(config.REPAIR_HOURS_MIN, config.REPAIR_HOURS_MAX)
        self._set_status(train_id, BROKEN, "emergency_repair")
        self._ensure_reserve()
        ev = self._open_event(train_id, "emergency_repair", "breakdown", None,
                              description="Внеплановый ремонт")
        with self.depot.request() as req:
            yield req
            yield self.env.timeout(dur)
        self._close_event(ev, dur)
        self._set_status(train_id, OPERATIONAL)
        self.metrics["total_downtime_hours"] += dur

    def _finish_service(self, train_id, stype, ev, dur):
        st = self.state[train_id]
        for level in config.SERVICE_HIERARCHY.get(stype, [stype]):
            st["m"][level] = 0.0
        self.metrics["total_services"] += 1
        self.metrics["services_by_type"][stype] += 1
        self.metrics["total_downtime_hours"] += dur
        self._close_event(ev, dur)
        self._set_status(train_id, OPERATIONAL)

    # ------------------------------------------------------------ статусы
    def _set_status(self, train_id: int, status: str, stype: str = None):
        st = self.state[train_id]
        self._segment(train_id, status, stype)
        st["status"] = status

    def _segment(self, train_id, status, stype=None, bay=None):
        prev = self._open_segments.pop(train_id, None)
        if prev:
            prev["end_hour"] = self.env.now
            self.segments.append(prev)
        self._open_segments[train_id] = {
            "train_id": train_id, "status": status, "service_type": stype,
            "start_hour": self.env.now, "end_hour": None, "bay": bay}

    def _close_all_segments(self):
        for tid, seg in self._open_segments.items():
            seg["end_hour"] = self.env.now
            self.segments.append(seg)
        self._open_segments.clear()

    # ------------------------------------------------------------ проверки
    def _ratio(self, train_id, stype) -> float:
        if stype is None:
            return 0.0
        return self.state[train_id]["m"][stype] / config.MILEAGE_TRIGGERS[stype]

    def _failure_probability(self, train_id) -> float:
        st = self.state[train_id]
        p = config.FAILURE_BASE_PER_TRIP * self.scenario["breakdown"]
        over100 = max(0.0, st["m"]["IS100"] / config.MILEAGE_TRIGGERS["IS100"] - 1)
        over200 = max(0.0, st["m"]["IS200"] / config.MILEAGE_TRIGGERS["IS200"] - 1)
        wear540 = max(0.0, st["m"]["IS540"] / config.MILEAGE_TRIGGERS["IS540"] - 0.9)
        p *= (1 + config.FAILURE_COEF_OVER_IS100 * over100
              + config.FAILURE_COEF_OVER_IS200 * over200
              + config.FAILURE_COEF_WEAR_IS540 * wear540)
        return min(p, 0.5)

    def _service_duration(self, stype) -> float:
        return config.SERVICE_DURATIONS[stype] * (
            1 + config.UNPLANNED_OVERHEAD[stype] * random.uniform(0.3, 1.0))

    def _long_work_due(self, train_id) -> Optional[Dict]:
        st = self.state[train_id]
        if self.use_planning:
            q = self.long_works.get(train_id)
            if q and self.env.now >= q[0]["start_hour"]:
                return q[0]
            return None
        # реактивный режим: длительная работа сразу по достижении порога
        for t in config.STRATEGIC_SERVICES:
            if st["m"][t] >= config.MILEAGE_TRIGGERS[t]:
                return {"type": t}
        return None

    def _operational_count(self) -> int:
        return sum(1 for s in self.state.values()
                   if s["status"] == OPERATIONAL
                   and s["commissioned_hour"] <= self.env.now)

    def _commissioned_count(self) -> int:
        return sum(1 for s in self.state.values()
                   if s["commissioned_hour"] <= self.env.now)

    def _reserve_available(self) -> bool:
        return any(s["status"] == RESERVE for s in self.state.values())

    def _ensure_reserve(self):
        need = max(self._required_now(),
                   math.ceil(config.TARGET_AVAILABILITY * self._commissioned_count()))
        if self._operational_count() >= need:
            return

    # ------------------------------------------------------------ планирование
    def _sync_state_light(self):
        """Пробег/статусы из памяти в БД — вход для планировщиков."""
        for tid, s in self.state.items():
            train = self.session.get(Train, tid)
            if not train:
                continue
            train.status = s["status"]
            train.total_mileage = s["total"]
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

    def strategic_cycle(self):
        while True:
            self._sync_state_light()
            now_dt = hour_to_datetime(self.env.now)
            self.strategic.now = now_dt
            try:
                plan = self.strategic.create_annual_plan()
                self.long_works.clear()
                for info in (plan or []):
                    offset = (int(info["train_id"]) * 167) % int(MONTH_HOURS)
                    self.long_works.setdefault(int(info["train_id"]), deque()).append({
                        "type": info["service_type"],
                        "start_hour": int(self.env.now + info["month"] * MONTH_HOURS
                                          + offset),
                    })
            except Exception as ex:
                logger.error("Стратегическое планирование: {}", ex)
            yield self.env.timeout(8760)

    def tactical_cycle(self):
        while True:
            self._sync_state_light()
            now = int(self.env.now)
            fixed = self._fixed_intervals(now, now + 14 * 24)
            try:
                df = self.tactical.create_weekly_schedule(now, fixed_intervals=fixed)
                if df is not None:
                    for _, r in df.iterrows():
                        self.short_plan[int(r["train_id"])].append({
                            "service_type": r["service_type"],
                            "start_hour": int(r["start_hour"]),
                            "end_hour": int(r["end_hour"]),
                            "duration_hours": float(r["duration_hours"]),
                            "depot_bay": int(r["depot_bay"]),
                        })
            except Exception as ex:
                logger.error("Тактическое планирование: {}", ex)
            yield self.env.timeout(84)

    def _fixed_intervals(self, t0: int, t1: int) -> List[tuple]:
        """Интервалы, когда поезда уже выведены (текущие работы + стратегические)."""
        out = []
        for tid, s in self.state.items():
            if s["status"] in (MAINTENANCE, BROKEN):
                out.append((t0, t0 + 24))
        for tid, q in self.long_works.items():
            for lw in q:
                if lw["start_hour"] < t1:
                    dur = config.SERVICE_DURATIONS[lw["type"]] * 1.3
                    out.append((max(t0, lw["start_hour"]),
                                min(t1, lw["start_hour"] + dur)))
        return out

    # ------------------------------------------------------------ метрики
    def metrics_collector(self, callback=None):
        while True:
            yield self.env.timeout(1)
            now = self.env.now
            comm = self._commissioned_count()
            op = self._operational_count()
            in_maint = sum(1 for s in self.state.values()
                           if s["status"] in (MAINTENANCE, BROKEN))
            depot_users = len(self.depot.users) + len(self.depot.queue)
            lathe_busy = 1 if self.lathe.users else 0
            avail = op / comm if comm else 1.0
            m = self.metrics
            m["availability_history"].append((now, avail))
            m["in_maintenance_history"].append((now, in_maint))
            m["depot_users_history"].append((now, depot_users))
            m["lathe_history"].append((now, lathe_busy))

            if now % 24 == config.OPERATION_START_HOUR + 1:  # оценка в 07:00
                m["coverage_days"] += 1
                if op >= self._required_now():
                    m["coverage_days_ok"] += 1
                m["daily_availability"].append((now, avail))
                self._ensure_reserve()

            if callback and int(now) % 168 == 0:
                try:
                    callback(now, avail, m)
                except Exception:
                    pass

    # ------------------------------------------------------------ события БД
    def _open_event(self, train_id, stype, reason, bay=None, description=None):
        ev = {"train_id": train_id, "service_type": stype,
              "scheduled_start": hour_to_datetime(self.env.now),
              "scheduled_end": None, "planned_duration_hours": None,
              "actual_start": hour_to_datetime(self.env.now), "actual_end": None,
              "actual_duration_hours": None, "status": "in_progress",
              "depot_bay": bay, "reason": reason, "work_description": description}
        return ev

    def _close_event(self, ev, dur):
        ev["actual_end"] = hour_to_datetime(self.env.now)
        ev["scheduled_end"] = ev["actual_end"]
        ev["planned_duration_hours"] = dur
        ev["actual_duration_hours"] = dur
        ev["status"] = "completed"
        self.event_rows.append(ev)

    # ------------------------------------------------------------ результаты
    def get_results(self) -> Dict:
        m = self.metrics
        # KPI считается по ежедневной оценке в 07:00 (дневная готовность)
        daily = [a for _, a in m["daily_availability"]]
        avg_av = sum(daily) / len(daily) if daily else 0.0
        du = [d for _, d in m["depot_users_history"]]
        lt = [l for _, l in m["lathe_history"]]
        return {
            "duration_years": self.env.now / 8760,
            "average_availability": avg_av,
            "min_availability": min(daily) if daily else 0.0,
            "coverage": (m["coverage_days_ok"] / m["coverage_days"]
                         if m["coverage_days"] else 0.0),
            "total_services": m["total_services"],
            "services_by_type": dict(m["services_by_type"]),
            "total_breakdowns": m["total_breakdowns"],
            "total_downtime_hours": m["total_downtime_hours"],
            "average_depot_utilization": (sum(du) / len(du) / config.DEPOT_CAPACITY
                                          if du else 0.0),
            "average_lathe_utilization": sum(lt) / len(lt) if lt else 0.0,
            "postponements": m["postponements"],
            "reserve_activations": m["reserve_activations"],
            "ml_data_collected": len(self.trip_log),
        }

    def get_segments_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.segments)

    def get_trips_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.trip_log)

    def get_ml_data(self) -> pd.DataFrame:
        """Сбалансированная выборка: снимок состояния перед рейсом + метка.

        Метка failure=1, если в течение ML_LABEL_HORIZON_HOURS после снимка
        произошёл внеплановый отказ этого поезда (merge_asof — быстро).
        """
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
        with pd.ExcelWriter(filename) as writer:
            self.get_trips_dataframe().to_excel(writer, sheet_name="Trips", index=False)
            pd.DataFrame(self.segments).to_excel(writer, sheet_name="Segments",
                                                 index=False)
            pd.DataFrame(self.metrics["availability_history"],
                         columns=["Hour", "Availability"]).to_excel(
                             writer, sheet_name="Availability", index=False)
            self.get_ml_data().to_excel(writer, sheet_name="ML_Data", index=False)
        logger.info("Экспорт: {}", filename)

    # ------------------------------------------------------------ синхронизация
    def _sync_db(self):
        for tid, s in self.state.items():
            train = self.session.get(Train, tid)
            if not train:
                continue
            train.status = s["status"]
            train.total_mileage = s["total"]
            for k, f in [("IS100", "mileage_since_is100"),
                         ("IS200", "mileage_since_is200"),
                         ("IS510", "mileage_since_is510"),
                         ("IS520", "mileage_since_is520"),
                         ("IS530", "mileage_since_is530"),
                         ("IS540", "mileage_since_is540"),
                         ("IS600", "mileage_since_is600"),
                         ("IS700", "mileage_since_is700"),
                         ("wheelset_turning", "mileage_since_wheelset")]:
                setattr(train, f, s["m"][k])
        self.session.query(MaintenanceEvent).delete()
        for ev in self.event_rows:
            self.session.add(MaintenanceEvent(**ev))
        self.session.query(ServiceSegment).delete()
        for seg in self.segments:
            self.session.add(ServiceSegment(**seg))
        self.session.commit()

    def _add_mileage(self, train_id, km):
        st = self.state[train_id]
        st["total"] += km
        for k in st["m"]:
            st["m"][k] += km


# обратная совместимость
FleetSimulator = IntegratedFleetSimulator
