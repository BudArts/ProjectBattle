"""Сквозной прогон системы (демо + санити-чек).

Запуск:  python test_run.py
Не является юнит-тестом — см. tests/test_system.py.
"""
from loguru import logger

from src.core.database import init_db, get_session, seed_initial_data
from src.planning.strategic import StrategicPlanner
from src.planning.tactical import TacticalPlanner
from src.simulation.simulator import IntegratedFleetSimulator


def test_full_system():
    logger.info("=" * 70)
    logger.info("СКВОЗНОЙ ПРОГОН СИСТЕМЫ")
    logger.info("=" * 70)

    engine = init_db()
    session = get_session(engine)
    seed_initial_data(session, num_trains=43, reset=True)

    logger.info("[1/4] Стратегическое планирование...")
    plan = StrategicPlanner(engine).create_annual_plan()
    logger.info("   план: {} событий", len(plan))

    logger.info("[2/4] Тактическое планирование (t=0)...")
    schedule = TacticalPlanner(engine).create_weekly_schedule(0, fixed_intervals=[])
    logger.info("   расписание: {} событий",
                0 if schedule is None else len(schedule))

    logger.info("[3/4] Симуляция (1 год, планировщики ВКЛ)...")
    sim = IntegratedFleetSimulator(engine, num_trains=43, use_planning=True)
    results = sim.run(duration_hours=8760)

    logger.info("[4/4] Экспорт...")
    sim.export_results("test_results.xlsx")

    logger.info("=" * 70)
    logger.info("Готовность: {:.1%}  (цель {:.0%})",
                results["average_availability"], 0.89)
    logger.info("ТО: {}  Поломок: {}  Загрузка депо: {:.1%}",
                results["total_services"], results["total_breakdowns"],
                results["average_depot_utilization"])
    logger.info("Структура ТО: {}", results["services_by_type"])
    logger.info("ML-строк: {}", results["ml_data_collected"])
    logger.info("=" * 70)


if __name__ == "__main__":
    test_full_system()
