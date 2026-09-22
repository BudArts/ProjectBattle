"""График оборота из примера заказчика должен сходиться сам с собой."""
from src.core.timetable import (EXAMPLE_SERVICE_IDS, OPERATING_DUTIES,
                                diagram_summary, home_city, revenue_legs,
                                timetable_rows)


def test_diagram_covers_the_whole_fleet():
    summary = diagram_summary()
    assert summary["duties"] == 19
    assert summary["revenue_trains"] == 70
    assert summary["running_consists"] == 38
    assert summary["service_consists"] == 5
    assert summary["fleet"] == 43
    assert summary["spb_need"] == 18
    assert summary["msk_need"] == 20
    # Утро и вечер сходятся: иначе оборот на следующий день некуда поставить.
    assert summary["spb_need"] == 2 * sum(1 for d in OPERATING_DUTIES if d["end_city"] == "spb")
    assert summary["msk_need"] == 2 * sum(1 for d in OPERATING_DUTIES if d["end_city"] == "msk")


def test_consist_numbers_are_a_partition():
    seen = []
    for duty in OPERATING_DUTIES:
        seen.extend(duty["consists"])
    seen.extend(EXAMPLE_SERVICE_IDS)
    assert sorted(seen) == list(range(1, 44))


def test_legs_chain_and_trains_are_unique():
    trains = []
    for duty in OPERATING_DUTIES:
        prev = None
        for leg in duty["legs"]:
            assert leg["arr"] > leg["dep"]
            assert leg["km"] == 679
            assert 2.0 <= (leg["arr"] - leg["dep"]) <= 2.5
            if prev is not None:
                assert leg["origin"] == prev["dest"]
                assert leg["dep"] >= prev["arr"] - 1e-9
                # Самая короткая стоянка в примере — 55 минут (оборот 27+28).
                assert leg["dep"] - prev["arr"] >= 0.9
            prev = leg
            trains.append(leg["train"])
        assert duty["start_city"] == duty["legs"][0]["origin"]
        assert duty["end_city"] == duty["legs"][-1]["dest"]
    assert len(trains) == len(set(trains)) == 70


def test_home_city_matches_morning():
    assert home_city(1) == "spb"
    assert home_city(3) == "msk"
    assert home_city(13) == "spb"
    assert home_city(43) == "spb"
    assert home_city(42) == "msk"


def test_russian_table_has_no_english_cells():
    banned = ("spb", "msk", "wheel", "operational", "scheduled", "train")
    for row in timetable_rows():
        for value in row.values():
            text = str(value).lower()
            assert not any(word in text for word in banned)
