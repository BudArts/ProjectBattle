"""Веб-интерфейс системы управления эксплуатационной готовностью ВСМ.

Все подписи, таблицы и графики — на русском. Внутренние коды
(operational, wheelset_turning) на экран не выводятся.
"""
import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.core.config import config
from src.core.database import (MaintenanceEvent, Train, get_session, init_db,
                               seed_initial_data)
from src.core.labels import (SCENARIO_RU, ru_event_status, ru_location,
                             ru_node, ru_reason, ru_service, ru_status)
from src.core.timetable import (OPERATING_DUTIES, balance_text, diagram_summary,
                                timetable_rows)
from src.simulation.report import (READINESS_FORMULA, lathe_cohort_report,
                                   lathe_recommendation_text, required_count)

ROOT = Path(__file__).resolve().parents[2]
LAST = ROOT / "data" / "last_run"

RZD_RED = "#E21A1A"
RZD_DARK = "#1D1D1B"
RZD_GRAY = "#585F69"
RZD_LIGHT = "#F2F2F2"
GREEN = "#2E7D32"
AMBER = "#F57C00"
BLUE = "#1565C0"

STATUS_COLOR = {
    "operational": GREEN,
    "maintenance": AMBER,
    "broken": RZD_RED,
    "reserve": RZD_GRAY,
    "not_delivered": "#BDBDBD",
    "equipping": BLUE,
    "line": "#455A64",
    "waiting": "#8D6E63",
}

st.set_page_config(page_title="ВСМ · Эксплуатационная готовность",
                   page_icon="🚄", layout="wide")

st.markdown(f"""
<style>
  html, body, [class*="st-"] {{ font-family: "PT Sans", "Segoe UI", Arial, sans-serif; }}
  #MainMenu, footer {{ visibility: hidden; }}
  .stApp {{ background: {RZD_LIGHT}; }}
  .rzd-banner {{
     background: {RZD_DARK}; color: #fff; padding: 14px 22px;
     border-left: 6px solid {RZD_RED}; margin-bottom: 18px;
  }}
  .rzd-banner h1 {{ color: #fff; font-size: 22px; margin: 0; font-weight: 700; }}
  .rzd-banner p {{ color: #cfcfcf; margin: 2px 0 0; font-size: 13px; }}
  .kpi {{
     background: #fff; border-top: 3px solid {RZD_RED};
     padding: 12px 16px; border-radius: 2px;
     box-shadow: 0 1px 2px rgba(0,0,0,.08); min-height: 92px;
  }}
  .kpi .v {{ font-size: 26px; font-weight: 700; color: {RZD_DARK}; }}
  .kpi .t {{ font-size: 12px; color: {RZD_GRAY}; text-transform: uppercase;
             letter-spacing: .03em; }}
  .kpi .s {{ font-size: 12px; color: #6b6b6b; }}
  .stTabs [data-baseweb="tab"] {{ font-weight: 600; }}
</style>
""", unsafe_allow_html=True)


def banner():
    st.markdown(
        '<div class="rzd-banner"><h1>ВСМ Москва — Санкт-Петербург · '
        'эксплуатационная готовность</h1>'
        '<p>Три уровня планирования · цифровой двойник парка из 43 составов · '
        'норма готовности не ниже 89%</p></div>',
        unsafe_allow_html=True)


def kpi_row(items):
    cols = st.columns(len(items))
    for col, (title, value, sub) in zip(cols, items):
        col.markdown(
            f'<div class="kpi"><div class="t">{title}</div>'
            f'<div class="v">{value}</div><div class="s">{sub}</div></div>',
            unsafe_allow_html=True)


def base_layout(fig, title, height=380, xtitle=None, ytitle=None):
    fig.update_layout(
        title=dict(text=title, font=dict(size=16, color=RZD_DARK), x=0, xanchor="left"),
        height=height,
        margin=dict(l=48, r=16, t=72, b=48),
        plot_bgcolor="#fff",
        paper_bgcolor="#fff",
        font=dict(family="PT Sans, Arial", size=12, color=RZD_DARK),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0,
                    title_text=""),
        hoverlabel=dict(font_size=12),
    )
    fig.update_xaxes(showgrid=False, linecolor="#ccc", title_text=xtitle or "")
    fig.update_yaxes(gridcolor="#eee", zeroline=False, title_text=ytitle or "")
    return fig


def load_json(tag):
    path = LAST / f"{tag}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_frame(tag, kind):
    path = LAST / f"{tag}_{kind}.csv"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def daily_availability(tag):
    hourly = load_frame(tag, "hourly")
    if hourly.empty:
        return hourly
    hourly = hourly.copy()
    hourly["сутки"] = (hourly["hour"] // 24).astype(int) + 1
    hourly["день"] = ((hourly["hour"] % 24) >= 6) & ((hourly["hour"] % 24) < 24)
    g = hourly.groupby("сутки").agg(
        готовность=("avail", "mean"),
        на_сервисе=("on_service", "mean"),
        в_ремонте=("broken", "mean"),
        станок=("lathe", "mean"),
        доступно=("available_n", "mean"),
    ).reset_index()
    op = hourly[hourly["день"]].groupby("сутки")["avail"].mean()
    g["готовность_в_графике"] = g["сутки"].map(op)
    return g


def service_month_counts(tag):
    seg = load_frame(tag, "segments")
    if seg.empty:
        return pd.DataFrame()
    work = seg[seg["status"].isin(["maintenance", "broken"])].copy()
    if work.empty:
        return work
    work["месяц"] = (work["start_hour"] // (24 * 30)).astype(int) + 1
    work["работа"] = work["service_type"].map(lambda x: ru_service(x) if pd.notna(x) else "—")
    return work.groupby(["месяц", "работа"]).size().unstack(fill_value=0)


# ---------------------------------------------------------------- загрузка
@st.cache_resource
def get_engine():
    engine = init_db()
    seed_initial_data(get_session(engine), num_trains=43, reset=False)
    return engine


engine = get_engine()
session = get_session(engine)
_saved_db = LAST / "plan_normal.db"
if st.session_state.get("live"):
    session = get_session(engine)
elif _saved_db.exists():
    session = get_session(init_db(f"sqlite:///{_saved_db}"))
banner()

plan = load_json("plan_normal")
react = load_json("react_normal")

tab_over, tab_cmp, tab_ana, tab_depot, tab_fleet, tab_move, tab_turn, tab_sched, tab_sim, tab_lathe = \
    st.tabs(["Обзор", "Сравнение", "Аналитика", "Депо", "Парк", "Движение",
             "Оборот", "Расписание", "Симуляция", "Станок"])

# ================================================================ ОБЗОР
with tab_over:
    st.subheader("Нормальный год полного парка")
    st.caption(
        "Карточки ниже — сохранённый год: 43 состава с 1 апреля 2028, 365 суток, "
        "генератор 42, нормальная эксплуатация, планирование включено. "
        "Новый прогон на 1–10 лет — вкладка «Симуляция»."
    )
    if plan:
        ok = plan["operating_availability"] >= config.TARGET_AVAILABILITY
        morning = plan["coverage"] >= 0.99
        kpi_row([
            ("Готовность в графике", f"{plan['operating_availability']:.1%}",
             f"06:00–24:00 · цель ≥ {config.TARGET_AVAILABILITY:.0%} · "
             f"{'норма держится' if ok else 'ниже нормы'}"),
            ("Утреннее покрытие", f"{plan['coverage']:.0%}",
             "доля суток, когда в 07:00 доступно не меньше нормы"),
            ("Внеплановые события", f"{plan['total_breakdowns']}",
             f"из них отказов в пути: {plan['line_failures']}"),
            ("Выполнение графика", f"{plan['schedule_fulfillment']:.1%}",
             f"{plan['pairs_completed']} пар из {plan['pairs_target']}"),
            ("Загрузка депо", f"{plan['average_depot_utilization']:.0%}",
             f"одновременно на сервисе не больше {plan['max_on_service']}"),
            ("Дневная обточка", f"{plan['day_lathe_hours']:.0f} ч",
             "часы станка после 06:00 — удар по графику"),
        ])
        st.markdown("**Как считается готовность**")
        st.write(READINESS_FORMULA)
        c1, c2 = st.columns(2)
        c1.metric("Средняя готовность за все часы, включая ночь",
                  f"{plan['average_availability']:.1%}")
        c2.metric("Часы, когда готовность не ниже 89%",
                  f"{plan['hours_meeting_target']:.0%}")
        st.caption(
            "Ночное окно 00:00–06:00 занято короткими работами: в эти часы "
            "мгновенная готовность часто чуть ниже 89%, потому что на сервисе "
            "до пяти составов. Для графика движения важны утро и день — "
            "они в первой карточке."
        )
    else:
        st.info("Годовой прогон ещё не сохранён. Откройте вкладку «Симуляция».")

# ================================================================ СРАВНЕНИЕ
with tab_cmp:
    st.subheader("План против реактивного ремонта")
    st.write(
        "Один и тот же парк, один и тот же генератор случайных чисел. "
        "Разница только в том, забирают ли состав в депо заранее, ночью и частями, "
        "или ждут порога и ставят на полный цикл днём."
    )
    if plan and react:
        rows = [
            ("Готовность в часы графика", plan["operating_availability"],
             react["operating_availability"], True),
            ("Утреннее покрытие нормы", plan["coverage"], react["coverage"], True),
            ("Средняя готовность", plan["average_availability"],
             react["average_availability"], True),
            ("Внеплановые события за год", plan["total_breakdowns"],
             react["total_breakdowns"], False),
            ("Отказы в пути", plan["line_failures"], react["line_failures"], False),
            ("Загрузка депо", plan["average_depot_utilization"],
             react["average_depot_utilization"], False),
            ("Дневные часы станка", plan["day_lathe_hours"],
             react["day_lathe_hours"], False),
            ("Выполнение своего графика", plan["schedule_fulfillment"],
             react["schedule_fulfillment"], True),
            ("Суток с сервисом дольше 120 ч", plan["days_over_120h"],
             react["days_over_120h"], False),
            ("Неравномерность месячной нагрузки", plan["load_cv"],
             react["load_cv"], False),
        ]
        percent = {
            "Готовность в часы графика", "Утреннее покрытие нормы",
            "Средняя готовность", "Загрузка депо", "Выполнение своего графика",
        }
        table = []
        for name, a, b, higher_better in rows:
            if name in percent:
                fa, fb = f"{a:.1%}", f"{b:.1%}"
            elif isinstance(a, float) and not float(a).is_integer():
                fa, fb = f"{a:.2f}", f"{b:.2f}"
            else:
                fa, fb = f"{a:.0f}", f"{b:.0f}"
            better = "план" if ((a >= b) if higher_better else (a <= b)) else "реактивный"
            table.append({
                "Показатель": name,
                "С планированием": fa,
                "Без планирования": fb,
                "Лучше": better,
            })
        st.dataframe(pd.DataFrame(table), width="stretch", hide_index=True)
        fig = go.Figure()
        fig.add_trace(go.Bar(
            name="С планированием",
            x=["Готовность в графике", "Утреннее покрытие", "Загрузка депо"],
            y=[plan["operating_availability"], plan["coverage"],
               plan["average_depot_utilization"]],
            marker_color=RZD_RED,
            hovertemplate="%{x}<br>%{y:.1%}<extra>С планированием</extra>",
        ))
        fig.add_trace(go.Bar(
            name="Без планирования",
            x=["Готовность в графике", "Утреннее покрытие", "Загрузка депо"],
            y=[react["operating_availability"], react["coverage"],
               react["average_depot_utilization"]],
            marker_color=RZD_GRAY,
            hovertemplate="%{x}<br>%{y:.1%}<extra>Без планирования</extra>",
        ))
        st.plotly_chart(
            base_layout(fig, "Готовность и загрузка депо: план и реактивный режим",
                        360, ytitle="Доля"),
            width="stretch")
        fig = go.Figure()
        fig.add_trace(go.Bar(
            name="С планированием",
            x=["Внеплановые события", "Дневные часы станка"],
            y=[plan["total_breakdowns"], plan["day_lathe_hours"]],
            marker_color=RZD_RED,
            hovertemplate="%{x}<br>%{y:.0f}<extra>С планированием</extra>",
        ))
        fig.add_trace(go.Bar(
            name="Без планирования",
            x=["Внеплановые события", "Дневные часы станка"],
            y=[react["total_breakdowns"], react["day_lathe_hours"]],
            marker_color=RZD_GRAY,
            hovertemplate="%{x}<br>%{y:.0f}<extra>Без планирования</extra>",
        ))
        st.plotly_chart(
            base_layout(fig, "Что план снимает с дневного графика",
                        360, ytitle="События или часы"),
            width="stretch")
        st.caption(
            "Реактивный режим пробегает больше: он держит состав на линии до самого "
            "порога. План снимает состав раньше и поэтому выполняет меньше пар, "
            "но не выбивает жёсткий допуск и почти не занимает станок днём. "
            "Готовность при этом выше, загрузка депо ниже."
        )
    else:
        st.info("Нет сохранённого сравнения.")

# ================================================================ АНАЛИТИКА
with tab_ana:
    st.subheader("Динамика за год")
    mode = st.radio(
        "Режим",
        ["С планированием", "Без планирования"],
        horizontal=True,
    )
    tag = "plan_normal" if mode.startswith("С") else "react_normal"
    daily = daily_availability(tag)
    if not daily.empty:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=daily["сутки"], y=daily["готовность_в_графике"],
            name="Готовность в часы графика",
            line=dict(color=RZD_RED, width=2),
            hovertemplate="Сутки %{x}<br>Готовность %{y:.1%}<extra></extra>",
        ))
        fig.add_hline(y=config.TARGET_AVAILABILITY, line_dash="dash",
                      line_color=RZD_GRAY,
                      annotation_text="норма 89%")
        st.plotly_chart(
            base_layout(fig, "Эксплуатационная готовность по суткам",
                        380, "Сутки с 1 апреля 2028", "Доля доступных составов"),
            width="stretch")

        g1, g2 = st.columns(2)
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=daily["сутки"], y=daily["на_сервисе"],
            name="На обслуживании",
            fill="tozeroy", line=dict(color=AMBER, width=1.5),
            hovertemplate="Сутки %{x}<br>Составов %{y:.1f}<extra></extra>",
        ))
        fig.add_hline(y=config.MAX_ON_SERVICE, line_dash="dash", line_color=RZD_RED,
                      annotation_text="не больше 5")
        g1.plotly_chart(
            base_layout(fig, "Сколько составов одновременно на сервисе",
                        320, "Сутки", "Составов"),
            width="stretch")
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=daily["сутки"], y=daily["станок"],
            name="Станок занят",
            fill="tozeroy", line=dict(color=RZD_DARK, width=1.2),
            hovertemplate="Сутки %{x}<br>Доля часа %{y:.0%}<extra></extra>",
        ))
        g2.plotly_chart(
            base_layout(fig, "Занятость колёсного станка",
                        320, "Сутки", "Доля часа"),
            width="stretch")

        pivot = service_month_counts(tag)
        if not pivot.empty:
            fig = go.Figure()
            for col in pivot.columns:
                fig.add_trace(go.Bar(
                    x=pivot.index, y=pivot[col], name=col,
                    hovertemplate=f"{col}<br>Месяц %{{x}}<br>%{{y}} работ<extra></extra>",
                ))
            fig.update_layout(barmode="stack")
            st.plotly_chart(
                base_layout(fig, "Структура работ по месяцам",
                            380, "Месяц эксплуатации", "Число работ"),
                width="stretch")
        chosen = plan if tag == "plan_normal" else react
        nodes = (chosen or {}).get("nodes") or {}
        if nodes:
            fig = go.Figure(go.Bar(
                x=[ru_node(k) for k in nodes],
                y=list(nodes.values()),
                marker_color=RZD_RED,
                hovertemplate="%{x}<br>%{y} событий<extra></extra>",
            ))
            st.plotly_chart(
                base_layout(fig, "Внеплановые события по узлам",
                            360, "Узел", "Событий"),
                width="stretch")
            st.caption(
                "Это не работа колёсного станка. Станок только обтачивает пары. "
                "Доли узлов — гипотеза: заказчик статистику отказов не передал."
            )
    else:
        st.info("Нет ряда готовности.")

# ================================================================ ДЕПО
with tab_depot:
    st.subheader("Позиции депо Обухово")
    tag = st.radio("Чей прогон показать", ["С планированием", "Без планирования"],
                   horizontal=True, key="depot_mode")
    tag = "plan_normal" if tag.startswith("С") else "react_normal"
    seg = load_frame(tag, "segments")
    if seg.empty:
        st.info("Нет интервалов.")
    else:
        work = seg[seg["status"].isin(["maintenance", "broken"])].copy()
        upper = max(30, int(work["end_hour"].max() // 24))
        tmax = st.slider("Окно, суток от начала", 14, upper, min(60, upper))
        work = work[work["start_hour"] < tmax * 24]
        if work.empty:
            st.info("В окне нет работ.")
        else:
            fig = go.Figure()
            for _, row in work.iterrows():
                bay = int(row["bay"]) if pd.notna(row["bay"]) else 0
                label = "Станок" if bay == 9 else (f"Позиция {bay}" if bay else "Без позиции")
                name = ru_service(row["service_type"]) if pd.notna(row["service_type"]) else "Работа"
                fig.add_trace(go.Bar(
                    x=[(row["end_hour"] - row["start_hour"]) / 24],
                    y=[label],
                    base=[row["start_hour"] / 24],
                    orientation="h",
                    marker_color=STATUS_COLOR.get(row["status"], RZD_RED),
                    name=name,
                    hovertemplate=(f"{name}<br>с %{{base:.1f}} сут., "
                                   "длительность %{x:.2f} сут.<extra></extra>"),
                    showlegend=False,
                ))
            st.plotly_chart(
                base_layout(fig, "Занятие позиций депо и станка",
                            460, "Сутки с начала эксплуатации"),
                width="stretch")
            st.caption("Оранжевый — плановое обслуживание, красный — внеплановый ремонт.")

# ================================================================ ПАРК
with tab_fleet:
    st.subheader("Парк в базе")
    trains = session.query(Train).order_by(Train.id).all()
    df = pd.DataFrame([{
        "Номер": t.number,
        "Статус": ru_status(t.status),
        "Дислокация": ru_location(getattr(t, "location", None) or "spb"),
        "Пробег, тыс. км": round((t.total_mileage or 0) / 1000, 1),
        "С последнего IS100, км": round(t.mileage_since_is100 or 0),
        "С последнего IS200, км": round(t.mileage_since_is200 or 0),
        "С последнего IS540, км": round(t.mileage_since_is540 or 0),
        "Пробег с обточки, км": round(t.mileage_since_wheelset or 0),
    } for t in trains])
    st.dataframe(df, width="stretch", height=420, hide_index=True)

    st.caption(
        "«Пробег с обточки» — километраж самого изношенного из 8 вагонов, "
        "не остаток до станка и не счётчик локомотива. Отдельного локомотива "
        "в составе нет: это электропоезд, колёсные пары есть у всех вагонов. "
        "Таблица читает текущую базу. После прогона на вкладке «Симуляция» "
        "счётчики совпадут с последним часом модели."
    )

# ================================================================ ДВИЖЕНИЕ
with tab_move:
    st.subheader("Состояния одного состава")
    seg = load_frame("plan_normal", "segments")
    if seg.empty:
        st.info("Нет сохранённых интервалов.")
    else:
        numbers = sorted(seg["train_id"].unique())
        tid = st.selectbox("Состав", numbers, format_func=lambda i: f"ЭВС-{int(i):03d}")
        tmax = st.slider("Горизонт графика, суток", 14, 365, 90)
        s = seg[(seg["train_id"] == tid) & (seg["start_hour"] < tmax * 24)].copy()
        fig = go.Figure()
        for _, row in s.iterrows():
            status_ru = ru_status(row["status"])
            work = ru_service(row["service_type"]) if pd.notna(row["service_type"]) else ""
            fig.add_trace(go.Bar(
                x=[max(0.01, (row["end_hour"] - row["start_hour"]) / 24)],
                y=[status_ru],
                base=[row["start_hour"] / 24],
                orientation="h",
                marker_color=STATUS_COLOR.get(row["status"], RZD_GRAY),
                showlegend=False,
                hovertemplate=f"{status_ru} {work}<br>с %{{base:.1f}} сут.<extra></extra>",
            ))
        order = [ru_status(x) for x in
                 ["line", "equipping", "operational", "reserve", "maintenance", "broken", "waiting"]]
        fig.update_yaxes(categoryorder="array", categoryarray=order)
        st.plotly_chart(
            base_layout(fig, f"ЭВС-{int(tid):03d}: где состав проводит время",
                        340, "Сутки с 1 апреля 2028", "Состояние"),
            width="stretch")

# ================================================================ ОБОРОТ
with tab_turn:
    st.subheader("Суточный оборот из примера заказчика")
    summary = diagram_summary()
    st.write(balance_text())
    st.write(
        "Это не пустая вкладка и не выдуманный интервал: 19 оборотов и 70 ниток "
        "взяты из таблицы, которую эксперты приложили к вопросу о частоте отправления. "
        "Красная полоса — ход из Санкт-Петербурга, синяя — из Москвы."
    )
    kpi_row([
        ("Оборотов", f"{summary['duties']}", "в примере на каждом по два состава"),
        ("Ниток в сутки", f"{summary['revenue_trains']}", "пассажирских, туда и обратно"),
        ("На нитках", f"{summary['running_consists']}",
         f"ещё {summary['service_consists']} состава в сервисном цикле"),
        ("Пассажирский пробег", f"{summary['revenue_km_day'] / 1000:.1f} тыс. км",
         "в сутки, если на нитке два состава"),
        ("На состав в год", f"{summary['km_per_train_year'] / 1000:.0f} тыс. км",
         "с подачей, если так ездить каждый день"),
    ])
    fig = go.Figure()
    for duty in OPERATING_DUTIES:
        for leg in duty["legs"]:
            from_spb = leg["origin"] == "spb"
            fig.add_trace(go.Bar(
                x=[max(0.05, leg["arr"] - leg["dep"])],
                y=[f"Оборот {duty['id']}"],
                base=[leg["dep"]],
                orientation="h",
                marker_color=RZD_RED if from_spb else BLUE,
                name="Из Санкт-Петербурга" if from_spb else "Из Москвы",
                legendgroup="spb" if from_spb else "msk",
                showlegend=False,
                hovertemplate=(
                    f"Нитка {leg['train']}<br>"
                    f"{'Санкт-Петербург' if from_spb else 'Москва'} → "
                    f"{'Москва' if from_spb else 'Санкт-Петербург'}"
                    "<br>отправление %{base:.2f} ч, ход %{x:.2f} ч<extra></extra>"
                ),
            ))
    fig.add_trace(go.Bar(
        x=[0], y=["Оборот 1+2"], name="Из Санкт-Петербурга",
        marker_color=RZD_RED, showlegend=True,
    ))
    fig.add_trace(go.Bar(
        x=[0], y=["Оборот 1+2"], name="Из Москвы",
        marker_color=BLUE, showlegend=True,
    ))
    fig.update_xaxes(
        range=[5.5, 25],
        tickvals=list(range(6, 25, 2)),
        ticktext=[f"{h:02d}:00" for h in range(6, 25, 2)],
    )
    fig.update_yaxes(autorange="reversed")
    st.plotly_chart(
        base_layout(fig, "Нитки примера: отправление и ход",
                    680, "Час суток", "Оборот"),
        width="stretch")
    st.caption(
        "Прибытие после полуночи продолжается за 24:00 тех же суток. "
        "Числа 16,4 и 4,4 в примечании — километры подачи к депо и на техническую "
        "станцию, не минуты: времени подачи в таблице нет."
    )
    st.dataframe(pd.DataFrame(timetable_rows()), width="stretch", height=480, hide_index=True)
    st.caption(
        "Карточки на вкладке «Обзор» посчитаны оборотом пары 1 358 км, а не этими 70 нитками. "
        "На длинном горизонте пример как единственный диспетчер оставляет состав в Москве "
        "у жёсткого допуска, и утреннюю нитку он уже не закрывает. Поэтому год на «Обзоре» "
        "этим графиком не подменён."
    )

# ================================================================ РАСПИСАНИЕ
with tab_sched:
    st.subheader("События обслуживания")
    ev = session.query(MaintenanceEvent).order_by(
        MaintenanceEvent.actual_start.desc()).limit(400)
    rows = [{
        "Состав": f"ЭВС-{e.train_id:03d}",
        "Работа": ru_service(e.service_type),
        "Причина": ru_reason(e.reason),
        "Начало": e.actual_start,
        "Длительность, ч": None if e.actual_duration_hours is None else round(e.actual_duration_hours, 1),
        "Позиция": e.depot_bay,
        "Состояние": ru_event_status(e.status),
        "Описание": e.work_description or "—",
    } for e in ev]
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", height=460, hide_index=True)
    else:
        st.info("В базе ещё нет закрытых событий. Запустите прогон.")

# ================================================================ СИМУЛЯЦИЯ
with tab_sim:
    st.subheader("Новый прогон")
    st.write(
        "Сохранённые карточки на «Обзоре» — один год. Здесь горизонт от 1 до 10 лет. "
        "Десять лет нужны, чтобы в прогон попали ревизии IS600 (1,2 млн км) и IS700 (2,4 млн км): "
        "за первый год состав до них не доезжает. "
        "Год с планировщиком — около двух минут. Десять лет — заметно дольше: "
        "годовой план пересчитывается каждые полгода, каждый пересчёт до 30 секунд."
    )
    c1, c2, c3, c4 = st.columns(4)
    years = c1.slider("Горизонт, лет", 1, 10, 10, 1)
    seed = c2.number_input("Начальное значение генератора", 1, 999, 42)
    scenario = c3.selectbox("Сценарий", list(config.SCENARIOS),
                            format_func=lambda s: SCENARIO_RU[s])
    use_planning = c4.checkbox("Включить планировщики", True)
    full_fleet = c4.checkbox("Полный парк с первого дня", True)

    if st.button("Запустить симуляцию", type="primary"):
        from src.simulation.simulator import IntegratedFleetSimulator
        prog = st.progress(0.0, text="Подготовка парка…")
        seed_initial_data(get_session(engine), num_trains=43, reset=True, phased=not full_fleet)
        sim = IntegratedFleetSimulator(
            engine, num_trains=43, use_planning=use_planning, scenario=scenario)

        def cb(now, avail, metrics):
            prog.progress(
                min(1.0, now / (years * 8760)),
                text=f"Прошло {now / 24:.0f} сут. · готовность {avail:.0%}",
            )

        with st.spinner("Считаем цифровой двойник…"):
            results = sim.run(duration_hours=int(years * 8760), seed=int(seed), callback=cb)
        prog.progress(1.0, text="Готово")
        st.session_state["live"] = {
            "results": results,
            "segments": sim.get_segments_dataframe(),
            "trips": sim.get_trips_dataframe(),
            "ml": sim.get_ml_data(),
        }
        per_year = results["total_breakdowns"] / years
        st.success(
            f"Горизонт {years:g} лет. "
            f"Готовность в графике {results.get('operating_availability', results['average_availability']):.1%}, "
            f"внеплановых событий {results['total_breakdowns']} ({per_year:.0f} в год), "
            f"максимум на сервисе {results['max_on_service']}."
        )

    live = st.session_state.get("live")
    if live:
        ml = live["ml"]
        st.markdown("**Выборка для будущих моделей**")
        if ml is not None and not ml.empty:
            show = ml.copy()
            if "origin" in show.columns:
                show["origin"] = show["origin"].map(
                    lambda x: {"spb": "Санкт-Петербург", "msk": "Москва"}.get(x, x))
            if "failure" in show.columns:
                show["failure"] = show["failure"].map({0: "Нет", 1: "Да"}).fillna("Нет")
            show = show.rename(columns={
                "train_id": "Состав", "hour": "Час", "km": "Км полурейса",
                "m100": "Пробег с IS100", "m200": "Пробег с IS200",
                "m540": "Пробег с IS540", "total": "Суммарный пробег",
                "origin": "Откуда", "failure": "Отказ в ближайшие 72 ч",
            })
            st.dataframe(show.head(200), width="stretch", hide_index=True)
            st.caption(
                "Признаки — пробеги и час. Метка — был ли отказ в следующие 72 часа. "
                "Есть и отказы, и спокойные рейсы, иначе модель ничему не научится."
            )

# ================================================================ СТАНОК
with tab_lathe:
    st.subheader("Колёсный станок делает только обточку")
    st.write(
        "Других работ у станка нет. Он не осматривает состав, не выполняет "
        "IS100–IS700 и не меняет колёсную пару. Единственная операция — обточка."
    )
    st.write(
        "Состав ВСМ здесь — электропоезд из 8 вагонов, отдельного локомотива нет. "
        "Обтачивать «только голову» нечего: колёсные пары стоят на каждом вагоне, "
        "4 на вагон, 32 на состав. Из них 16 моторных и 16 немоторных. "
        "На станок по очереди встают все 8 вагонов."
    )
    st.dataframe(pd.DataFrame([
        {"Работа": "Обточка колёсных пар", "Где": "Только депо Обухово",
         "Чем": "Тандемный колёсный станок", "Сколько": "2 часа простоя на вагон"},
        {"Работа": "Замена колёсной пары", "Где": "Обухово или Москва",
         "Чем": "Ремонтная позиция, не станок", "Сколько": "Внеплановый ремонт"},
        {"Работа": "IS100–IS700, клапан, ремонт", "Где": "Стойла и домкраты Обухово",
         "Чем": "Не станок", "Сколько": "Отдельная очередь, не больше 5 составов"},
    ]), width="stretch", hide_index=True)
    st.caption(
        "Тандем значит, что за один простой вагона станок обрабатывает его колёсные пары, "
        "а не что на каждую пару заложены отдельные 2 часа. "
        "Весь состав — 8 × 2 = 16 часов. В ночь с 00:00 до 06:00 влезают 3 вагона. "
        "Обточка не идёт одновременно с другим обслуживанием того же состава."
    )
    st.markdown("**Хватает ли одного станка, когда шесть составов приходят вместе**")
    report = lathe_cohort_report()
    observed = None
    if plan and plan.get("lathe_deficit_hour") is not None:
        observed = plan["lathe_deficit_hour"] / 24
    st.write(lathe_recommendation_text(report, observed))
    kpi_row([
        ("Спрос первой волны", f"{report['demand_hours']:.0f} ч",
         f"{report['cohort_trains']} состава × {report['cars']} вагонов × 2 ч"),
        ("Ночная мощность окна", f"{report['capacity_hours']:.0f} ч",
         f"один станок, {report['window_days']:.0f} суток допуска"),
        ("Запас", f"{report['slack_hours']:.0f} ч", report["verdict"]),
        ("Дневная обточка в плане",
         f"{plan['day_lathe_hours']:.0f} ч" if plan else "—",
         "часы станка после 06:00 за сохранённый год"),
        ("Дневная обточка без плана",
         f"{react['day_lathe_hours']:.0f} ч" if react else "—",
         "тот же парк, если ждать порога"),
    ])
    st.caption(
        "Вопрос страницы только про этот станок: успевает ли он обточить вагоны ночью, "
        "пока допуск не выбран. Узлы отказов перенесены на вкладку «Аналитика» — "
        "к станку они не относятся."
    )
