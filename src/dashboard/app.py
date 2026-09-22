"""Веб-интерфейс системы управления обслуживанием ВСМ (Streamlit).

Стиль — корпоративная палитра ОАО «РЖД»: красный #E21A1A, графит, белый,
строгая типографика, минимум декора. Запускается:  streamlit run src/dashboard/app.py

Вкладки:
  Обзор        — KPI и целевые показатели;
  Аналитика    — готовность, ТО/поломки, загрузки депо и станка, структура работ;
  Депо         — гантт по позициям и диаграмма работы колёсного станка;
  Парк         — таблица составов и «пила» пробега до ТО;
  Движение     — график работы состава (пробег/рейсы во времени);
  Расписание   — события обслуживания из БД;
  Симуляция    — параметры и запуск прогона (горизонт, seed, сценарий, планировщики);
  ML-данные    — сбалансированная выборка для будущих моделей.
"""
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.core.config import config
from src.core.database import (MaintenanceEvent, Train, get_session, init_db,
                               seed_initial_data)

RZD_RED = "#E21A1A"
RZD_DARK = "#1D1D1B"
RZD_GRAY = "#585F69"
RZD_LIGHT = "#F2F2F2"
GREEN = "#2E7D32"
AMBER = "#F57C00"

STATUS_COLOR = {
    "operational": GREEN,
    "maintenance": AMBER,
    "broken": RZD_RED,
    "reserve": RZD_GRAY,
    "not_delivered": "#BDBDBD",
}

st.set_page_config(page_title="ВСМ · Управление обслуживанием",
                   page_icon="🚄", layout="wide")

st.markdown(f"""
<style>
  html, body, [class*="st-"] {{ font-family: "PT Sans", "Segoe UI", Arial, sans-serif; }}
  #MainMenu, footer, header {{ visibility: hidden; }}
  .stApp {{ background: {RZD_LIGHT}; }}
  div[data-testid="stHeader"] {{ background: transparent; }}
  .rzd-banner {{
     background: {RZD_DARK}; color: #fff; padding: 14px 22px;
     border-left: 6px solid {RZD_RED}; margin-bottom: 18px;
  }}
  .rzd-banner h1 {{ color: #fff; font-size: 22px; margin: 0; font-weight: 700; }}
  .rzd-banner p {{ color: #cfcfcf; margin: 2px 0 0; font-size: 12px; }}
  .kpi {{
     background: #fff; border-top: 3px solid {RZD_RED};
     padding: 12px 16px; border-radius: 2px;
     box-shadow: 0 1px 2px rgba(0,0,0,.08);
  }}
  .kpi .v {{ font-size: 26px; font-weight: 700; color: {RZD_DARK}; }}
  .kpi .t {{ font-size: 11px; color: {RZD_GRAY}; text-transform: uppercase;
             letter-spacing: .04em; }}
  .kpi .s {{ font-size: 11px; color: #9a9a9a; }}
  .stTabs [data-baseweb="tab"] {{ font-weight: 600; }}
</style>
""", unsafe_allow_html=True)


def banner():
    st.markdown(
        '<div class="rzd-banner"><h1>🚄 ВСМ · Система управления эксплуатационной '
        'готовностью</h1><p>Трёхуровневое планирование ТО · цифровой двойник парка · '
        'целевая готовность ≥ 89%</p></div>', unsafe_allow_html=True)


def kpi_row(items):
    cols = st.columns(len(items))
    for c, (title, value, sub) in zip(cols, items):
        c.markdown(f'<div class="kpi"><div class="t">{title}</div>'
                   f'<div class="v">{value}</div><div class="s">{sub}</div></div>',
                   unsafe_allow_html=True)


def base_layout(fig, height=340):
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=34, b=10),
        plot_bgcolor="#fff", paper_bgcolor="#fff",
        font=dict(family="PT Sans, Arial", size=11, color=RZD_DARK),
        xaxis=dict(showgrid=False, linecolor="#ccc"),
        yaxis=dict(gridcolor="#eee", zeroline=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
    return fig


def h_to_days(series):
    return [h / 24 for h in series]


# ---------------------------------------------------------------- загрузка
@st.cache_resource
def get_engine():
    engine = init_db()
    seed_initial_data(get_session(engine), num_trains=43, reset=False)
    return engine


engine = get_engine()
session = get_session(engine)
banner()

tab_over, tab_ana, tab_depot, tab_fleet, tab_move, tab_sched, tab_sim, tab_ml = \
    st.tabs(["Обзор", "Аналитика", "Депо", "Парк", "Движение",
             "Расписание", "Симуляция", "ML-данные"])

# ================================================================ СИМУЛЯЦИЯ
with tab_sim:
    st.subheader("Прогон цифрового двойника")
    c1, c2, c3, c4 = st.columns(4)
    years = c1.slider("Горизонт, лет", 0.5, 5.0, 1.0, 0.5)
    seed = c2.number_input("Seed", 1, 999, 42)
    scenario = c3.selectbox("Сценарий", list(config.SCENARIOS),
                            format_func=lambda s: {
                                "normal": "Нормальная эксплуатация",
                                "high_load": "Высокая нагрузка (+20% пробега)",
                                "poor_maintenance": "×2 поломок"}[s])
    use_planning = c4.checkbox("Использовать планировщики", True)
    reset_fleet = c4.checkbox("Чистый старт (сброс парка)", True)

    if st.button("▶ Запустить симуляцию", type="primary"):
        from src.simulation.simulator import IntegratedFleetSimulator
        prog = st.progress(0.0, text="Подготовка...")
        if reset_fleet:
            seed_initial_data(get_session(engine), num_trains=43, reset=True)
        sim = IntegratedFleetSimulator(engine, num_trains=43,
                                       use_planning=use_planning, scenario=scenario)

        def cb(now, avail, metrics):
            prog.progress(min(1.0, now / (years * 8760)),
                          text=f"t = {now / 8760:.2f} лет · готовность {avail:.0%}")

        with st.spinner("Считаем..."):
            results = sim.run(duration_hours=int(years * 8760), seed=int(seed),
                              callback=cb)
        prog.progress(1.0, text="Готово")

        st.session_state["sim"] = {
            "results": results,
            "segments": sim.get_segments_dataframe(),
            "trips": sim.get_trips_dataframe(),
            "ml": sim.get_ml_data(),
            "metrics": sim.metrics,
        }
        st.cache_resource.clear()
        st.rerun()

    if "sim" not in st.session_state:
        st.info("Задайте параметры и нажмите «Запустить симуляцию» — "
                "результаты появятся во всех вкладках.")

data = st.session_state.get("sim")

# ================================================================ ОБЗОР
with tab_over:
    st.subheader("Ключевые показатели")
    if data:
        r = data["results"]
        ok = r["average_availability"] >= config.TARGET_AVAILABILITY
        kpi_row([
            ("Готовность парка", f"{r['average_availability']:.1%}",
             f"цель ≥ {config.TARGET_AVAILABILITY:.0%} · {'✓' if ok else '✗'}"),
            ("Покрытие цели (дней)", f"{r['coverage']:.0%}",
             "доля дней с нужным числом составов"),
            ("Плановых ТО", f"{r['total_services']}",
             "за весь горизонт"),
            ("Внеплановых поломок", f"{r['total_breakdowns']}",
             f"простой {r['total_downtime_hours']:.0f} ч"),
            ("Загрузка депо", f"{r['average_depot_utilization']:.0%}",
             f"{config.DEPOT_CAPACITY} позиций"),
            ("Загрузка станка", f"{r['average_lathe_utilization']:.0%}",
             "обточка колёсных пар"),
        ])
        st.caption("Резервных активаций: "
                   f"{r['reserve_activations']} · переносов старта: {r['postponements']}")
    else:
        trains = session.query(Train)
        total = trains.count()
        op = trains.filter_by(status="operational").count()
        kpi_row([
            ("Всего составов", str(total), "фазированный ввод"),
            ("В эксплуатации", str(op), "сейчас в БД"),
            ("Целевая готовность", f"{config.TARGET_AVAILABILITY:.0%}",
             f"≥ {config.REQUIRED_OPERATIONAL} составов"),
            ("Позиций в депо", str(config.DEPOT_CAPACITY),
             f"+ {config.WHEELSET_LATHE_CAPACITY} станок"),
        ])
        st.info("Это данные БД без прогона. Откройте вкладку «Симуляция», "
                "чтобы получить динамику и графики.")

# ================================================================ АНАЛИТИКА
with tab_ana:
    st.subheader("Динамика эксплуатационных показателей")
    if data:
        m = data["metrics"]
        g1, g2 = st.columns(2)
        # Готовность
        dh = pd.DataFrame(m["daily_availability"], columns=["hour", "avail"])
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=h_to_days(dh.hour), y=dh.avail, name="Готовность",
                                 line=dict(color=RZD_RED, width=2)))
        fig.add_hline(y=config.TARGET_AVAILABILITY, line_dash="dash",
                      line_color=RZD_GRAY,
                      annotation_text=f"цель {config.TARGET_AVAILABILITY:.0%}")
        fig.update_xaxes(title_text="день")
        fig.update_yaxes(range=[0.4, 1.0])
        g1.plotly_chart(base_layout(fig, 320), use_container_width=True)
        # На ТО / поломки
        im = pd.DataFrame(m["in_maintenance_history"], columns=["hour", "n"])
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=h_to_days(im.hour), y=im.n, name="На ТО/ремонте",
                                 fill="tozeroy", line=dict(color=AMBER, width=1.5)))
        fig.add_hline(y=config.TOTAL_TRAINS - config.REQUIRED_OPERATIONAL,
                      line_dash="dash", line_color=RZD_GRAY,
                      annotation_text="лимит одновременного ТО")
        g2.plotly_chart(base_layout(fig, 320), use_container_width=True)

        g3, g4 = st.columns(2)
        du = pd.DataFrame(m["depot_users_history"], columns=["hour", "n"])
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=h_to_days(du.hour),
                                 y=du.n / config.DEPOT_CAPACITY, name="Загрузка депо",
                                 fill="tozeroy", line=dict(color=RZD_DARK, width=1.5)))
        g3.plotly_chart(base_layout(fig, 300), use_container_width=True)
        lt = pd.DataFrame(m["lathe_history"], columns=["hour", "n"])
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=h_to_days(lt.hour), y=lt.n, name="Станок занят",
                                 fill="tozeroy", line=dict(color=RZD_GRAY, width=1)))
        g4.plotly_chart(base_layout(fig, 300), use_container_width=True)

        # Структура работ по типам
        seg = data["segments"]
        svc = seg[seg.status.isin(["maintenance", "broken"])]
        if not svc.empty:
            svc = svc.copy()
            svc["month"] = (svc.start_hour // (24 * 30)).astype(int)
            pivot = svc.groupby(["month", "service_type"]).size().unstack(fill_value=0)
            fig = go.Figure()
            for col in pivot.columns:
                fig.add_trace(go.Bar(x=pivot.index, y=pivot[col], name=col))
            fig.update_layout(barmode="stack", xaxis_title="месяц",
                              yaxis_title="работ")
            st.plotly_chart(base_layout(fig, 320), use_container_width=True)
    else:
        st.info("Нет данных прогона.")

# ================================================================ ДЕПО
with tab_depot:
    st.subheader("Загрузка депо и колёсного станка")
    if data:
        seg = data["segments"]
        work = seg[seg.status.isin(["maintenance", "broken"])].copy()
        if not work.empty:
            upper = max(180, int(work.end_hour.max() // 24))
            tmax = st.slider("Окно, дней", 30, upper, min(180, upper))
            work = work[work.start_hour < tmax * 24]
            work["bay"] = work.bay.fillna(0).astype(int)
            fig = go.Figure()
            for _, r in work.iterrows():
                fig.add_trace(go.Bar(
                    x=[(r.end_hour - r.start_hour) / 24],
                    y=[f"Поз.{r.bay}" if r.bay else "Ремонт"],
                    base=[r.start_hour / 24], orientation="h",
                    marker_color=STATUS_COLOR.get(r.status, RZD_RED),
                    name=r.service_type, hovertemplate=
                    f"{r.service_type}<br>%{{base:.0f}}–%{{x}} дн<extra></extra>",
                    showlegend=False))
            fig.update_xaxes(title_text="день")
            st.plotly_chart(base_layout(fig, 420), use_container_width=True)
            st.caption("Каждая полоса — занятие позиции депо; цвет: плановое ТО "
                       "(оранжевый) / внеплановый ремонт (красный).")
        else:
            st.info("Нет работ в выбранном окне.")
    else:
        st.info("Нет данных прогона.")

# ================================================================ ПАРК
with tab_fleet:
    st.subheader("Парк составов")
    trains = session.query(Train).order_by(Train.id).all()
    df = pd.DataFrame([{
        "№": t.number, "Статус": t.status,
        "Пробег, тыс.км": round(t.total_mileage / 1000),
        "до IS100": round(t.mileage_since_is100),
        "до IS200": round(t.mileage_since_is200),
        "до IS540": round(t.mileage_since_is540),
        "до обточки": round(t.mileage_since_wheelset),
    } for t in trains])
    st.dataframe(df, use_container_width=True, height=380)

    if data:
        st.markdown("**«Пила» пробега до IS100 (сбросы при ТО)**")
        tid = st.selectbox("Состав", sorted(data["trips"].train_id.unique()),
                           format_func=lambda i: f"ЭВС-{i:03d}")
        tr = data["trips"][data["trips"].train_id == tid]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=h_to_days(tr.hour), y=tr.m100,
                                 name="пробег с IS100", line=dict(color=RZD_RED)))
        fig.add_hline(y=config.MILEAGE_TRIGGERS["IS100"], line_dash="dash",
                      line_color=RZD_GRAY, annotation_text="порог IS100")
        st.plotly_chart(base_layout(fig, 300), use_container_width=True)

# ================================================================ ДВИЖЕНИЕ
with tab_move:
    st.subheader("График работы состава (рейсы и простой)")
    if data:
        seg = data["segments"]
        tid = st.selectbox("Состав для графика",
                           sorted(seg.train_id.unique()), key="mv",
                           format_func=lambda i: f"ЭВС-{i:03d}")
        s = seg[seg.train_id == tid].copy()
        tmax = st.number_input("Горизонт, дней", 30, 365 * 5, 120, key="mvh")
        s = s[s.start_hour < tmax * 24]
        fig = go.Figure()
        ymap = {"operational": 1, "reserve": 0.5, "maintenance": 0, "broken": 0}
        for _, r in s.iterrows():
            fig.add_trace(go.Bar(
                x=[(r.end_hour - r.start_hour) / 24], y=[r.status],
                base=[r.start_hour / 24], orientation="h",
                marker_color=STATUS_COLOR.get(r.status, RZD_GRAY),
                showlegend=False,
                hovertemplate=f"{r.status}<extra>{r.service_type or ''}</extra>"))
        fig.update_yaxes(categoryorder="array",
                         categoryarray=["operational", "reserve", "maintenance"])
        fig.update_xaxes(title_text="день")
        st.plotly_chart(base_layout(fig, 260), use_container_width=True)

        trips = data["trips"][data["trips"].train_id == tid]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=h_to_days(trips.hour), y=trips.total / 1000,
                                 name="суммарный пробег, тыс.км",
                                 line=dict(color=RZD_DARK, width=2)))
        fig.update_yaxes(title_text="тыс. км")
        st.plotly_chart(base_layout(fig, 260), use_container_width=True)
    else:
        st.info("Нет данных прогона.")

# ================================================================ РАСПИСАНИЕ
with tab_sched:
    st.subheader("События обслуживания (БД)")
    ev = session.query(MaintenanceEvent).order_by(
        MaintenanceEvent.actual_start.desc()).limit(500)
    rows = [{
        "Состав": e.train_id, "Тип": e.service_type, "Причина": e.reason,
        "Начало": e.actual_start, "Длит.,ч": e.actual_duration_hours,
        "Поз.": e.depot_bay, "Статус": e.status,
    } for e in ev]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=420)

# ================================================================ ML
with tab_ml:
    st.subheader("Выборка для моделей прогнозирования")
    if data:
        ml = data["ml"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Строк", len(ml))
        c2.metric("Отказы (1)", int(ml.failure.sum()))
        c3.metric("Доля отказов", f"{ml.failure.mean():.1%}")
        st.caption("Признаки: пробег с IS100/IS200/IS540, суммарный пробег, час, "
                   "метка «отказ в течение 72 ч». Данные сбалансированы "
                   "(есть и отрицательные примеры) — пригодны для обучения.")
        st.dataframe(ml.head(200), use_container_width=True)
    else:
        st.info("Нет данных прогона.")
