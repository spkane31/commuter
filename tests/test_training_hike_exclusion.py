from datetime import date

from commuter.sheets import dashboard_data, summarize, zone_pie_data


def activity(identifier, sport, category, minutes):
    return {
        "activity_id": str(identifier),
        "sport_type": sport,
        "category": category,
        "activity_date": "2026-10-03",
        "week": "2026-09-28",
        "moving_s": minutes * 60,
        "elapsed_s": minutes * 60,
    }


def test_hikes_are_kept_in_all_activity_duration_but_excluded_from_training():
    rows = [
        activity(1, "Run", "run", 30),
        activity(2, "Ride", "bike_other", 40),
        activity(3, "Ride", "bike_commute", 10),
        activity(4, "Hike", "other", 110),
        activity(5, "Workout", "other", 15),
    ]
    weekly, daily = dashboard_data(rows, date(2026, 10, 3))
    week = weekly[-1]
    assert week["non_commute_minutes"] == 85
    assert week["other_minutes"] == 125
    assert sum(
        week[f"{c}_minutes"]
        for c in ("run", "virtual_bike", "bike_other", "bike_commute", "other")
    ) == 205
    assert daily[-1]["run_7d_minutes"] == 30
    assert daily[-1]["bike_7d_minutes"] == 40


def test_hike_sport_is_excluded_from_training_zones_even_with_category_override():
    rows = [activity(1, "Run", "run", 30), activity(2, "Hike", "run", 100)]
    zones = [
        {
            "activity_id": str(i),
            "week": "2026-09-28",
            "category": "run",
            "zone": "Z2",
            "settings_version": "run-v1",
            "seconds": seconds,
        }
        for i, seconds in ((1, 1800), (2, 6000))
    ]
    weekly, daily = dashboard_data(rows, date(2026, 10, 3))
    assert weekly[-1]["non_commute_minutes"] == 30
    assert daily[-1]["run_7d_minutes"] == 30
    pies = {r["zone"]: r for r in zone_pie_data(rows, zones, date(2026, 10, 3))}
    assert pies["Z2"]["run_minutes"] == 30
    _, summary = summarize(rows, zones)
    assert summary[0]["minutes"] == 30
