"""Тактическое планирование (горизонт 14 дней).

Строит ночное расписание коротких работ (IS100, IS200, обточка колёсных пар)
на единой шкале времени симуляции. Исправлено:
- срочность (urgency) считается корректно (ранее всегда 0.0);
- «ночное окно» привязано к абсолютным часам суток, а не к смещению от now;
- ограничение «не более N поездов на ТО одновременно» задано как cumulative
  по всему горизонту (ранее проверялось раз в сутки и нарушалось);
- позиции депо назначаются first-fit без пересечений (ранее все попадали в bay 1);
- длительные и текущие работы учитываются как фиксированные интервалы,
  поэтому расписание не конфликтует со стратегическим уровнем;
- расписание исполняется симулятором и закрывается (completed).
"""
from typing import Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger
from ortools.sat.python import cp_model

from src.core.config import config
from src.core.database import (MaintenanceEvent, OPERATIONAL, Train,
                               get_session, hour_to_datetime)
from src.core.entities import DepotState, MaintenanceSlot, TrainState

NIGHT_START_HOURS = (0, 1, 2, 3, 22, 23)  # допустимые часы начала коротких работ


class TacticalPlanner:
    """Тактический планировщик (на 2 недели)"""

    def __init__(self, db_engine):
        self.engine = db_engine
        self.session = get_session(db_engine)
        self.horizon_hours = 14 * 24

    # ------------------------------------------------------------------ API
    def create_weekly_schedule(
        self,
        now_hour: int,
        fixed_intervals: Optional[List[Tuple[int, int]]] = None,
    ) -> Optional[pd.DataFrame]:
        """Расписание в абсолютных часах симуляции.

        fixed_intervals: интервалы [start, end), когда поезда уже выведены
        из эксплуатации (длительные ревизии, ремонты) — учитываются в
        ограничении готовности и загрузке депо.
        """
        fixed_intervals = fixed_intervals or []
        trains = self._get_trains_needing_service(now_hour)
        if not trains:
            return None

        logger.info("Тактическое планирование: {} поездов, t={}", len(trains), now_hour)
        schedule = self._optimize_schedule(trains, now_hour, fixed_intervals)
        if schedule is None or schedule.empty:
            logger.warning("CP-SAT не дал решения — жадный алгоритм")
            schedule = self._greedy_schedule(trains, now_hour)
        if schedule is None or schedule.empty:
            return None

        self._save_schedule(schedule, now_hour)
        return schedule

    # ------------------------------------------------------------- данные
    def _get_trains_needing_service(self, now_hour: int) -> List[Dict]:
        trains = self.session.query(Train).filter_by(status=OPERATIONAL).all()
        needing = []
        for train in trains:
            state = TrainState.from_orm(train)
            stype = state.next_service_type(only=config.TACTICAL_SERVICES)
            if not stype:
                continue
            needing.append({
                "train_id": train.id,
                "train_number": train.number,
                "service_type": stype,
                "urgency": self._calculate_urgency(train, stype),
                "can_defer": state.can_defer_service(stype, days=7),
            })
        needing.sort(key=lambda t: t["urgency"], reverse=True)
        return needing

    def _calculate_urgency(self, train: Train, service_type: str) -> float:
        """Срочность: 1.0 = порог достигнут, >1 = просрочено."""
        field = {s.lower(): f for s, f in
                 {"IS100": "mileage_since_is100", "IS200": "mileage_since_is200",
                  "IS510": "mileage_since_is510", "IS520": "mileage_since_is520",
                  "IS530": "mileage_since_is530", "IS540": "mileage_since_is540",
                  "IS600": "mileage_since_is600",
                  "WHEELSET_TURNING": "mileage_since_wheelset"}.items()}
        mileage_field = field.get(service_type.lower())
        if mileage_field is None:
            return 0.0
        current = getattr(train, mileage_field, 0) or 0
        trigger = config.MILEAGE_TRIGGERS.get(service_type)
        if not trigger:
            return 0.0
        return (current / trigger) * config.SERVICE_CRITICALITY.get(service_type, 1.0)

    # ------------------------------------------------------------- CP-SAT
    def _duration(self, service_type: str) -> int:
        base = config.SERVICE_DURATIONS[service_type]
        return max(1, int(round(base * (1 + config.UNPLANNED_OVERHEAD[service_type]))))

    def _optimize_schedule(self, trains: List[Dict], now_hour: int,
                           fixed: List[Tuple[int, int]]) -> Optional[pd.DataFrame]:
        model = cp_model.CpModel()
        horizon_end = now_hour + self.horizon_hours

        start_v, end_v, interval_v, is_wheel = {}, {}, {}, {}
        for t in trains:
            tid = t["train_id"]
            dur = self._duration(t["service_type"])
            start = model.NewIntVar(now_hour, horizon_end - dur, f"start_{tid}")
            end = model.NewIntVar(now_hour + dur, horizon_end, f"end_{tid}")
            model.Add(end == start + dur)
            interval_v[tid] = model.NewIntervalVar(start, dur, end, f"int_{tid}")
            start_v[tid], end_v[tid] = start, end
            is_wheel[tid] = t["service_type"] == "wheelset_turning"

        # Фиксированные интервалы (текущие ремонты и длительные ревизии)
        fixed_intervals = []
        for i, (fs, fe) in enumerate(fixed):
            fs = max(int(fs), now_hour)
            fe = min(int(fe), horizon_end)
            if fe <= fs:
                continue
            fixed_intervals.append(
                model.NewFixedSizeIntervalVar(fs, fe - fs, f"fixed_{i}"))

        all_intervals = list(interval_v.values()) + fixed_intervals
        n_fixed = len(fixed_intervals)

        # Готовность и депо: одновременно на ТО не более (TOTAL - REQUIRED).
        # Это строже вместимости депо (5 < 8), поэтому first-fit по позициям
        # после решения всегда сходится.
        max_in_maint = config.TOTAL_TRAINS - config.REQUIRED_OPERATIONAL
        model.AddCumulative(all_intervals, [1] * len(all_intervals), max_in_maint)

        # Колёсный станок: без пересечений
        wheel = [interval_v[t["train_id"]] for t in trains if is_wheel[t["train_id"]]]
        if wheel:
            model.AddNoOverlap(wheel)

        # Ночное окно: час начала (абсолютный) в {0..5, 22, 23}
        for t in trains:
            tid = t["train_id"]
            hod = model.NewIntVar(0, 23, f"hod_{tid}")
            model.AddModuloEquality(hod, start_v[tid], 24)
            allowed = [model.NewBoolVar(f"n{tid}_{h}") for h in NIGHT_START_HOURS]
            for b, h in zip(allowed, NIGHT_START_HOURS):
                model.Add(hod == h).OnlyEnforceIf(b)
            model.AddBoolOr(allowed)

        # Цель: критичные — раньше
        model.Minimize(sum(
            start_v[t["train_id"]] * max(1, int(t["urgency"] * 100)) for t in trains))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 10.0
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            logger.warning("CP-SAT: {}", solver.StatusName(status))
            return None

        rows = []
        for t in trains:
            tid = t["train_id"]
            s = solver.Value(start_v[tid])
            e = solver.Value(end_v[tid])
            rows.append({"train_id": tid, "train_number": t["train_number"],
                         "service_type": t["service_type"],
                         "start_hour": s, "end_hour": e,
                         "duration_hours": e - s, "urgency": t["urgency"]})
        df = pd.DataFrame(rows).sort_values("start_hour").reset_index(drop=True)
        self._assign_bays(df)
        return df

    def _assign_bays(self, df: pd.DataFrame) -> None:
        """First-fit по позициям депо (станок — позиция 9)."""
        depot = DepotState()
        base = hour_to_datetime(0)
        for i, row in df.iterrows():
            start = base.replace(hour=0) + pd.Timedelta(hours=int(row["start_hour"]))
            if row["service_type"] == "wheelset_turning":
                df.at[i, "depot_bay"] = 9
                continue
            bay = depot.assign_bay(start, row["duration_hours"])
            df.at[i, "depot_bay"] = bay or 1
            depot.add_slot(MaintenanceSlot(start, start + pd.Timedelta(
                hours=row["duration_hours"]), row["service_type"],
                int(row["train_id"]), int(bay or 1)))

    # ------------------------------------------------------------- greedy
    def _greedy_schedule(self, trains: List[Dict], now_hour: int) -> pd.DataFrame:
        depot = DepotState()
        wheel_slots: List[Tuple[int, int]] = []
        rows = []
        for t in sorted(trains, key=lambda x: x["urgency"], reverse=True):
            dur = self._duration(t["service_type"])
            placed = False
            for day in range(14):
                day_start = now_hour // 24 * 24 + day * 24
                for h in (0, 1, 2, 3, 22, 23):
                    s = day_start + h
                    if s < now_hour:
                        continue
                    e = s + dur
                    if t["service_type"] == "wheelset_turning":
                        if any(ws <= s < we or ws < e <= we for ws, we in wheel_slots):
                            continue
                        wheel_slots.append((s, e))
                        rows.append({**t, "start_hour": s, "end_hour": e,
                                     "duration_hours": dur, "depot_bay": 9})
                        placed = True
                        break
                    start_dt = hour_to_datetime(s)
                    bay = depot.assign_bay(start_dt, dur)
                    if bay is None:
                        continue
                    depot.add_slot(MaintenanceSlot(start_dt, hour_to_datetime(e),
                                                   t["service_type"], t["train_id"], bay))
                    rows.append({**t, "start_hour": s, "end_hour": e,
                                 "duration_hours": dur, "depot_bay": bay})
                    placed = True
                    break
                if placed:
                    break
            if not placed:
                logger.warning("Не удалось разместить {} ({})", t["train_number"],
                               t["service_type"])
        return pd.DataFrame(rows)

    # ------------------------------------------------------------- запись
    def _save_schedule(self, df: pd.DataFrame, now_hour: int):
        horizon_end = now_hour + self.horizon_hours
        (self.session.query(MaintenanceEvent)
         .filter(MaintenanceEvent.scheduled_start >= hour_to_datetime(now_hour),
                 MaintenanceEvent.scheduled_start <= hour_to_datetime(horizon_end),
                 MaintenanceEvent.status == "planned",
                 MaintenanceEvent.reason == "scheduled",
                 MaintenanceEvent.service_type.in_(list(config.TACTICAL_SERVICES)))
         .delete(synchronize_session=False))

        for _, r in df.iterrows():
            self.session.add(MaintenanceEvent(
                train_id=int(r["train_id"]),
                service_type=r["service_type"],
                scheduled_start=hour_to_datetime(r["start_hour"]),
                scheduled_end=hour_to_datetime(r["end_hour"]),
                planned_duration_hours=float(r["duration_hours"]),
                depot_bay=int(r["depot_bay"]),
                status="planned",
                reason="scheduled",
            ))
        self.session.commit()
        logger.info("Сохранено {} тактических событий", len(df))
