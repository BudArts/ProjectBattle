# ВСМ · Система управления эксплуатационной готовностью парка

Решение для конкурса проектных работ: трёхуровневое планирование ТО парка из
43 высокоскоростных поездов при цели готовности **≥ 89% (≥ 38 составов)**,
с цифровым двойником (SimPy), исполняющим планы, и веб-интерфейсом в стиле
ОАО «РЖД».

## Установка

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Запуск

```bash
python main.py                 # веб-интерфейс (Streamlit, порт 8501)
python main.py sim --years 2 --seed 7 --scenario normal   # прогон из CLI
python main.py sim --years 1 --no-planning                # базовый (реактивный) режим
python main.py seed            # пересоздать парк в БД
```

## Тесты

```bash
python -m pytest tests/ -q     # юнит/интеграционные тесты исправлений
python test_run.py             # сквозной демо-прогон (1 год) + экспорт Excel
```

## Структура

```
src/core/        конфигурация, модель данных (SQLite), сущности, суточный оборот
src/planning/    strategic (PuLP, 12 мес) · tactical (OR-Tools CP-SAT, 14 дн) · operational (правила)
src/simulation/  цифровой двойник (SimPy) — исполняет планы, собирает метрики и ML-выборку
src/dashboard/   веб-интерфейс (Streamlit): KPI, аналитика, депо, парк, движение, ML
docs/            анализ (analysis.md), описание продукта (PRODUCT.md), список исправлений (IMPROVEMENTS.md)
```

## Документы

- `docs/PRODUCT.md` — простое описание того, как работает продукт.
- `docs/IMPROVEMENTS.md` — что было исправлено и подтверждено прогонами (до/после).
- `docs/analysis.md` — первичный аудит первой версии (исторический).
