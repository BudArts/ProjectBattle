"""Точка входа.

  python main.py            — веб-интерфейс (Streamlit)
  python main.py sim        — прогон симуляции из командной строки
  python main.py seed       — создать/пересоздать парк в БД

Примеры:
  python main.py sim --years 2 --seed 7 --scenario normal --no-planning
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run_web(args):
    cmd = [sys.executable, "-m", "streamlit", "run", str(ROOT / "src/dashboard/app.py"),
           "--server.address", args.host, "--server.port", str(args.port),
           "--server.headless", "true", "--browser.gatherUsageStats", "false"]
    print("Запуск веб-интерфейса:", " ".join(cmd))
    raise SystemExit(subprocess.call(cmd, cwd=ROOT))


def run_sim(args):
    from src.core.database import init_db, get_session, seed_initial_data
    from src.simulation.simulator import IntegratedFleetSimulator

    engine = init_db()
    session = get_session(engine)
    seed_initial_data(session, num_trains=43, reset=args.reset)

    sim = IntegratedFleetSimulator(engine, num_trains=43,
                                   use_planning=not args.no_planning,
                                   scenario=args.scenario)
    hours = int(args.years * 8760)

    def cb(now, avail, metrics):
        print(f"  t={now / 8760:5.2f} лет  готовность {avail:6.1%}  "
              f"ТО={metrics['total_services']}  поломок={metrics['total_breakdowns']}")

    results = sim.run(duration_hours=hours, seed=args.seed, callback=cb)

    print("\n=== РЕЗУЛЬТАТЫ ===")
    for k, v in results.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.3f}")
        else:
            print(f"  {k}: {v}")
    if args.export:
        sim.export_results(args.export)


def run_seed(_args):
    from src.core.database import init_db, get_session, seed_initial_data
    session = get_session(init_db())
    seed_initial_data(session, num_trains=43, reset=True)
    print("Парк создан.")


def main():
    p = argparse.ArgumentParser(description="Система управления обслуживанием ВСМ")
    sub = p.add_subparsers(dest="cmd")

    web = sub.add_parser("web", help="веб-интерфейс")
    web.add_argument("--host", default="0.0.0.0")
    web.add_argument("--port", type=int, default=8501)

    sim = sub.add_parser("sim", help="прогон симуляции")
    sim.add_argument("--years", type=float, default=1.0)
    sim.add_argument("--seed", type=int, default=42)
    sim.add_argument("--scenario", default="normal",
                     choices=["normal", "high_load", "poor_maintenance"])
    sim.add_argument("--no-planning", action="store_true")
    sim.add_argument("--reset", action="store_true")
    sim.add_argument("--export", default=None)

    sub.add_parser("seed", help="пересоздать парк")

    args = p.parse_args()
    if args.cmd == "sim":
        run_sim(args)
    elif args.cmd == "seed":
        run_seed(args)
    else:
        run_web(args)


if __name__ == "__main__":
    main()
