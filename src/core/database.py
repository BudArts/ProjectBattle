"""Модель данных и работа с БД (SQLite для прототипа).

Изменения относительно прежней версии:
- init_db() сам создаёт каталог под файл БД (раньше падал без data/);
- у MaintenanceEvent появилось work_description (описание внепланового ремонта);
- добавлен статус 'not_delivered' для ещё не поставленных поездов;
- добавлена таблица ServiceSegment для истории статусов (гантт, аналитика);
- reset_service_mileage() использует SERVICE_HIERARCHY из конфига (без дублей).
"""
import os
from datetime import datetime, timedelta

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        JSON, String, create_engine, inspect, text)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .config import config

Base = declarative_base()

NOT_DELIVERED = "not_delivered"
OPERATIONAL = "operational"
RESERVE = "reserve"
MAINTENANCE = "maintenance"
BROKEN = "broken"


class Train(Base):
    """Высокоскоростной поезд"""
    __tablename__ = 'trains'
    id = Column(Integer, primary_key=True)
    number = Column(String(20), unique=True)
    status = Column(String(20), default=NOT_DELIVERED)
    total_mileage = Column(Float, default=0.0)
    mileage_since_is100 = Column(Float, default=0.0)
    mileage_since_is200 = Column(Float, default=0.0)
    mileage_since_is510 = Column(Float, default=0.0)
    mileage_since_is520 = Column(Float, default=0.0)
    mileage_since_is530 = Column(Float, default=0.0)
    mileage_since_is540 = Column(Float, default=0.0)
    mileage_since_is600 = Column(Float, default=0.0)
    mileage_since_is700 = Column(Float, default=0.0)
    mileage_since_wheelset = Column(Float, default=0.0)
    commissioned_date = Column(DateTime)
    last_maintenance_date = Column(DateTime)
    location = Column(String(16), default="spb")
    maintenance_events = relationship("MaintenanceEvent", back_populates="train")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MaintenanceEvent(Base):
    """События обслуживания"""
    __tablename__ = 'maintenance_events'
    id = Column(Integer, primary_key=True)
    train_id = Column(Integer, ForeignKey('trains.id'))
    service_type = Column(String(50))
    scheduled_start = Column(DateTime)
    scheduled_end = Column(DateTime)
    planned_duration_hours = Column(Float)
    actual_start = Column(DateTime, nullable=True)
    actual_end = Column(DateTime, nullable=True)
    actual_duration_hours = Column(Float, nullable=True)
    status = Column(String(20), default='planned')  # planned/in_progress/completed/postponed/cancelled
    depot_bay = Column(Integer, nullable=True)
    reason = Column(String(20), default='scheduled')  # scheduled/breakdown
    work_description = Column(String(300), nullable=True)
    train = relationship("Train", back_populates="maintenance_events")
    created_at = Column(DateTime, default=datetime.utcnow)


class ServiceSegment(Base):
    """История статусов поезда: [start, end) в статусе status.

    Используется для гантт-диаграмм и графика работы парка.
    """
    __tablename__ = 'service_segments'
    id = Column(Integer, primary_key=True)
    train_id = Column(Integer, ForeignKey('trains.id'))
    status = Column(String(20))
    service_type = Column(String(50), nullable=True)
    start_hour = Column(Float)
    end_hour = Column(Float, nullable=True)
    bay = Column(Integer, nullable=True)


class Schedule(Base):
    """Сохранённые расписания"""
    __tablename__ = 'schedules'
    id = Column(Integer, primary_key=True)
    type = Column(String(20))
    created_at = Column(DateTime, default=datetime.utcnow)
    valid_from = Column(DateTime)
    valid_to = Column(DateTime)
    schedule_data = Column(JSON if False else __import__('sqlalchemy').JSON)
    active = Column(Boolean, default=True)


def init_db(db_url='sqlite:///data/hsr.db'):
    """Создать движок БД, при необходимости создав каталог под файл."""
    connect_args = {}
    if db_url.startswith('sqlite:///') and db_url != 'sqlite:///:memory:':
        path = db_url[len('sqlite:///'):]
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        connect_args = {"check_same_thread": False}
    elif db_url.startswith("sqlite:"):
        connect_args = {"check_same_thread": False}
    engine = create_engine(db_url, echo=False, connect_args=connect_args)
    Base.metadata.create_all(engine)
    _ensure_columns(engine)
    return engine


def _ensure_columns(engine):
    """Добавить колонки, которых не было в базе предыдущей версии."""
    try:
        insp = inspect(engine)
        if "trains" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("trains")}
    except Exception:
        return
    if "location" not in cols:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE trains ADD COLUMN location VARCHAR(16) DEFAULT 'spb'"))


def get_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()


def base_datetime() -> datetime:
    return datetime.fromisoformat(config.OPERATION_START_DATE)


def hour_to_datetime(hour: float) -> datetime:
    return base_datetime() + timedelta(hours=hour)


def seed_initial_data(session, num_trains=43, reset=False, phased=True):
    """Создать начальный парк поездов.

    phased=True — ввод как в постановке: 6 составов сразу, далее по одному в месяц.
    phased=False — весь парк на линии с первого дня (нормальный год эксплуатации).
    """
    if reset:
        session.query(ServiceSegment).delete()
        session.query(MaintenanceEvent).delete()
        session.query(Train).delete()
        session.query(Schedule).delete()
        session.commit()

    if session.query(Train).count() > 0:
        return

    base = base_datetime()
    for i in range(1, num_trains + 1):
        if (not phased) or i <= 6:
            commissioned = base
            status = OPERATIONAL
        else:
            commissioned = base + timedelta(days=30 * (i - 6))
            status = NOT_DELIVERED  # резервные (i>39) станут reserve при вводе

        session.add(Train(
            number=f"ЭВС-{i:03d}",
            status=status,
            commissioned_date=commissioned,
            total_mileage=0.0,
            mileage_since_is100=0.0,
            mileage_since_is200=0.0,
            mileage_since_is510=0.0,
            mileage_since_is520=0.0,
            mileage_since_is530=0.0,
            mileage_since_is540=0.0,
            mileage_since_is600=0.0,
            mileage_since_is700=0.0,
            mileage_since_wheelset=0.0,
            last_maintenance_date=commissioned,
        ))
    session.commit()


MILEAGE_FIELDS = {
    "IS100": "mileage_since_is100",
    "IS200": "mileage_since_is200",
    "IS510": "mileage_since_is510",
    "IS520": "mileage_since_is520",
    "IS530": "mileage_since_is530",
    "IS540": "mileage_since_is540",
    "IS600": "mileage_since_is600",
    "IS700": "mileage_since_is700",
    "wheelset_turning": "mileage_since_wheelset",
}


def reset_service_mileage(train: Train, service_type: str, at: datetime = None):
    """Сбросить счётчики пробега с учётом иерархии (единая точка правды)."""
    for level in config.SERVICE_HIERARCHY.get(service_type, [service_type]):
        field = MILEAGE_FIELDS.get(level)
        if field:
            setattr(train, field, 0.0)
    train.last_maintenance_date = at or datetime.utcnow()
