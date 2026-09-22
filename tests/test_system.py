"""Юнит/интеграционные тесты ключевых исправлений (pytest).

Запуск:  python -m pytest tests/ -q
"""
import os
from datetime import datetime

import pytest

from src.core.config import config
from src.core.database import (Train, get_session, init_db,
                               reset_service_mileage, seed_initial_data)
from src.core.entities import MaintenanceSlot, DepotState
from src.planning.strategic import StrategicPlanner
from src.planning.tactical import TacticalPlanner


def make_train(session, i, **m):
    t = Train(id=i, number=f"T-{i:02d}", status="operational", total_mileage=0.0,
              mileage_since_is100=0.0, mileage_since_is200=0.0,
              mileage_since_is510=0.0, mileage_since_is520=0.0,
              mileage_since_is530=0.0, mileage_since_is540=0.0,
              mileage_since_is600=0.0, mileage_since_is700=0.0,
              mileage_since_wheelset=0.0,
              commissioned_date=datetime(2028, 4, 1))
    for k, v in m.items():
        setattr(t, k, v)
    session.add(t)
    return t


@pytest.fixture()
def session(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path}/t.db")
    return get_session(engine)


def test_init_db_creates_directory(tmp_path):
    target = tmp_path / "nested" / "dirs" / "x.db"
    init_db(f"sqlite:///{target}")
    assert target.exists()


def test_hierarchy_reset(session):
    t = make_train(session, 1, mileage_since_is100=9000, mileage_since_is200=20000,
                   mileage_since_is540=500000, mileage_since_is600=900000)
    session.commit()
    reset_service_mileage(t, "IS540")
    assert t.mileage_since_is100 == 0
    assert t.mileage_since_is200 == 0
    assert t.mileage_since_is540 == 0
    assert t.mileage_since_is600 == 900000  # не сбрасывается иерархией IS540


def test_urgency_positive_when_overdue(session):
    t = make_train(session, 1, mileage_since_is100=20_000)
    session.commit()
    tp = TacticalPlanner(session.get_bind())
    assert tp._calculate_urgency(t, "IS100") > 1.0  # просрочено => приоритет >1


def test_strategic_two_types_no_crash(session):
    for i in range(1, 6):
        make_train(session, i)
    for i in range(6, 11):
        make_train(session, i, total_mileage=1_600_000,
                   mileage_since_is700=1_600_000)
    session.commit()
    engine = session.get_bind()
    plan = StrategicPlanner(engine).create_annual_plan()
    types = {p["service_type"] for p in plan}
    assert len(plan) >= 10
    assert "IS540" in types and "IS700" in types  # оба типа, без падения


def test_tactical_night_window_and_limits(session):
    for i in range(1, 7):
        make_train(session, i, mileage_since_is100=11_000,
                   mileage_since_is200=11_000)
    session.commit()
    engine = session.get_bind()
    df = TacticalPlanner(engine).create_weekly_schedule(0, fixed_intervals=[])
    assert df is not None and len(df) == 6
    # ночное окно: час начала 0..5
    assert all(int(s) % 24 in (0, 1, 2, 3, 4, 5) for s in df.start_hour)
    # нет пересечений по позициям депо
    depot = DepotState()
    base = datetime(2028, 4, 1)
    slots = []
    for _, r in df.iterrows():
        s = base + __import__("datetime").timedelta(hours=int(r.start_hour))
        slots.append(MaintenanceSlot(s, s + __import__("datetime").timedelta(
            hours=r.duration_hours), r.service_type, int(r.train_id),
            int(r.depot_bay)))
    for a in slots:
        for b in slots:
            if a is not b:
                assert not a.overlaps(b)


def test_simulation_executes_and_closes_events():
    from src.simulation.simulator import IntegratedFleetSimulator
    from src.core.database import MaintenanceEvent
    engine = init_db("sqlite:///data/test_sys.db")
    session = get_session(engine)
    seed_initial_data(session, 43, reset=True)
    sim = IntegratedFleetSimulator(engine, 43, use_planning=True)
    res = sim.run(duration_hours=4380)  # полгода
    assert res["total_services"] > 0
    assert res["average_depot_utilization"] > 0
    assert res["average_availability"] > 0.85
    events = session.query(MaintenanceEvent).all()
    assert events, "события записаны в БД"
    assert all(e.status == "completed" for e in events)
    assert all(e.actual_start is not None for e in events)
    # ML-выборка сбалансирована
    ml = sim.get_ml_data()
    assert ml["failure"].sum() > 0 and (ml["failure"] == 0).sum() > 0
    os.remove("data/test_sys.db")
