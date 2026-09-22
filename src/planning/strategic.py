"""Стратегическое планирование (горизонт 12 месяцев).

Планирует многодневные ревизии (IS510…IS700) — то, что не помещается в
двухнедельный тактический горизонт. Каждая ревизия, наступающая в горизонте
года, планируется отдельным событием (у поезда может быть несколько в году:
IS510 → IS520 → … → IS540).

Исправлено относительно прежней версии:
- уникальные имена ограничений PuLP (падал при двух типах ревизий);
- единая шкала времени (`now` — момент симуляции, не datetime.now());
- учитывается дата ввода поезда в эксплуатацию;
- план сохраняется и реально исполняется симулятором (замкнутый контур).
"""
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from loguru import logger
from pulp import (LpProblem, LpMinimize, LpVariable, lpSum, value,
                  PULP_CBC_CMD, LpStatus)

from src.core.config import config
from src.core.database import OPERATIONAL, RESERVE, Schedule, Train, get_session

MONTH_HOURS = 730  # ~30.4 суток


class StrategicPlanner:
    """Долгосрочный планировщик (на год)"""

    def __init__(self, db_engine, now: Optional[datetime] = None):
        self.engine = db_engine
        self.session = get_session(db_engine)
        self.now = now or datetime.fromisoformat(config.OPERATION_START_DATE)
        self.horizon_months = 12
        self.monthly_capacity = {"IS510": 6, "IS520": 5, "IS530": 4,
                                 "IS540": 4, "IS600": 2, "IS700": 1}

    # ------------------------------------------------------------------ API
    def create_annual_plan(self) -> List[Dict]:
        """Список событий: [{train_id, train_number, service_type, month}]."""
        logger.info("Стратегическое планирование на 12 месяцев от {}", self.now.date())
        jobs = self._forecast_service_needs()
        if not jobs:
            logger.info("Нет поездов, требующих крупных ревизий в ближайший год")
            return []

        by_type: Dict[str, int] = {}
        for j in jobs:
            by_type[j["service_type"]] = by_type.get(j["service_type"], 0) + 1
        logger.info("К планированию: {}", by_type)

        schedule = self._optimize_distribution(jobs)
        if not schedule:
            logger.warning("PuLP не дал решения — жадный алгоритм")
            schedule = self._greedy_distribution(jobs)

        self._save_plan(schedule)
        return schedule

    def get_current_plan(self) -> Optional[List[Dict]]:
        plan = (self.session.query(Schedule)
                .filter_by(type="strategic", active=True).first())
        return plan.schedule_data.get("schedule") if plan else None

    # ------------------------------------------------------------- прогноз
    def _forecast_service_needs(self) -> List[Dict]:
        """Все ревизии, наступающие в горизонте года (по каждой — своё событие).

        Приближение: после каждой запланированной ревизии счётчик сбрасывается,
        поэтому следующая ревизия того же уровня сдвигается на период.
        """
        trains = self.session.query(Train).filter(
            Train.status.in_([OPERATIONAL, RESERVE])).all()
        annual = config.ANNUAL_MILEAGE_PER_TRAIN
        jobs = []

        for train in trains:
            commissioned = train.commissioned_date or self.now
            delay_months = 0.0
            if commissioned > self.now:
                delay_months = (commissioned - self.now).days / 30.4

            for stype in config.STRATEGIC_SERVICES:
                current = getattr(train, f"mileage_since_{stype.lower()}") or 0
                period_months = (config.MILEAGE_TRIGGERS[stype] / annual) * 12
                remaining = config.MILEAGE_TRIGGERS[stype] - current
                first_months = (max(0.0, remaining) / annual) * 12

                # все наступления этого уровня в горизонте года
                m = first_months
                while m <= self.horizon_months - 1:
                    month = max(m, delay_months)
                    if month <= self.horizon_months - 1:
                        jobs.append({
                            "train_id": train.id,
                            "train_number": train.number,
                            "service_type": stype,
                            "months_to_service": month,
                            "priority": config.SERVICE_CRITICALITY[stype],
                        })
                    m += period_months
        return jobs

    # ---------------------------------------------------------- оптимизация
    def _optimize_distribution(self, jobs: List[Dict]) -> Optional[List[Dict]]:
        model = LpProblem("Strategic_Plan", LpMinimize)
        x = {}
        for ji, j in enumerate(jobs):
            for m in range(self.horizon_months):
                x[(ji, m)] = LpVariable(f"x_{ji}_{m}", cat="Binary")
        max_load = LpVariable("max_load", lowBound=0)

        for ji in range(len(jobs)):
            model += (lpSum(x[(ji, m)] for m in range(self.horizon_months)) == 1,
                      f"one_{ji}")

        penalties = []
        for m in range(self.horizon_months):
            for stype in config.STRATEGIC_SERVICES:
                group = [ji for ji, j in enumerate(jobs) if j["service_type"] == stype]
                if not group:
                    continue
                load = lpSum(x[(ji, m)] for ji in group)
                model += (load <= self.monthly_capacity.get(stype, 2),
                          f"cap_{stype}_{m}")
                model += (load <= max_load, f"maxload_{stype}_{m}")

        for ji, j in enumerate(jobs):
            pref = min(int(j["months_to_service"]), self.horizon_months - 1)
            for m in range(self.horizon_months):
                penalties.append(j["priority"] * abs(m - pref) * x[(ji, m)])
        model += max_load + 0.01 * lpSum(penalties)

        status = model.solve(PULP_CBC_CMD(msg=0, timeLimit=30))
        if LpStatus[status] != "Optimal":
            logger.warning("PuLP статус: {}", LpStatus[status])
            return None

        out = []
        for ji, j in enumerate(jobs):
            for m in range(self.horizon_months):
                if value(x[(ji, m)]) > 0.5:
                    out.append({**j, "month": m})
                    break
        logger.info("PuLP: {} назначений", len(out))
        return out

    def _greedy_distribution(self, jobs: List[Dict]) -> List[Dict]:
        counts = {s: [0] * self.horizon_months for s in config.STRATEGIC_SERVICES}
        out = []
        for j in sorted(jobs, key=lambda t: (t["months_to_service"], -t["priority"])):
            stype = j["service_type"]
            pref = min(int(j["months_to_service"]), self.horizon_months - 1)
            for off in range(self.horizon_months):
                m = min(pref + off, self.horizon_months - 1)
                if counts[stype][m] < self.monthly_capacity.get(stype, 2):
                    counts[stype][m] += 1
                    out.append({**j, "month": m})
                    break
        return out

    # ---------------------------------------------------------- сохранение
    def _save_plan(self, schedule: List[Dict]):
        self.session.query(Schedule).filter_by(type="strategic", active=True)\
            .update({"active": False})
        self.session.add(Schedule(
            type="strategic",
            created_at=self.now,
            valid_from=self.now,
            valid_to=self.now + timedelta(days=365),
            schedule_data={
                "schedule": schedule,
                "horizon_months": self.horizon_months,
                "total_events": len(schedule),
                "now": self.now.isoformat(),
            },
            active=True,
        ))
        self.session.commit()
