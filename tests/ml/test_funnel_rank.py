from __future__ import annotations

import pandas as pd

from app.models.funnel_rank import rank_steps


def steps_df(rows: list[tuple[str, str, int, int, int, float]]) -> pd.DataFrame:
    """(funnel, step, order, unique_visits, orders, gross)"""
    return pd.DataFrame(
        [
            {
                "source_id": "s",
                "funnel_id": f,
                "step_id": st,
                "name": st,
                "ord": o,
                "unique_visits": uv,
                "orders": od,
                "gross": g,
                "visits": uv,
                "exit_rate": 0.0,
            }
            for f, st, o, uv, od, g in rows
        ]
    )


def funnels_df(*ids: str) -> pd.DataFrame:
    return pd.DataFrame(
        [{"source_id": "s", "funnel_id": f, "name": f.upper(), "gross": 1000.0, "unique_visits": 1000, "orders": 10} for f in ids]
    )


def test_tiny_unlucky_step_never_outranks_large_weak_step() -> None:
    rows = []
    for i in range(6):  # six healthy funnels: 40% middle pass, 5000 visitors
        rows += [(f"f{i}", "a", 0, 5000, 0, 0.0), (f"f{i}", "b", 1, 2000, 0, 0.0), (f"f{i}", "c", 2, 800, 100, 10000.0)]
    rows += [
        ("big", "a", 0, 5000, 0, 0.0),
        ("big", "b", 1, 2000, 0, 0.0),
        ("big", "c", 2, 500, 60, 6000.0),
    ]  # b: 25% on 2000 visitors
    rows += [("tiny", "a", 0, 10, 0, 0.0), ("tiny", "b", 1, 3, 0, 0.0), ("tiny", "c", 2, 0, 0, 0.0)]  # whole funnel: 10 visitors
    res = rank_steps(steps_df(rows), funnels_df(*[f"f{i}" for i in range(6)], "big", "tiny"))
    order = [(r["funnel_id"], r["step_id"]) for r in res["steps"]]
    assert order[0][0] == "big"
    tiny_b = next(r for r in res["steps"] if r["funnel_id"] == "tiny" and r["step_id"] == "b")
    assert tiny_b["confidence"] == "low" and tiny_b["rank"] > 3
    assert tiny_b["shrunk_rate"] > 0.1  # pulled away from the raw 0%


def test_ranking_is_deterministic_for_a_seed_and_intervals_are_ordered() -> None:
    rows = []
    for i in range(5):
        rows += [(f"f{i}", "a", 0, 1000, 0, 0.0), (f"f{i}", "b", 1, 400 - i * 30, 0, 0.0), (f"f{i}", "c", 2, 100, 20, 2000.0)]
    a = rank_steps(steps_df(rows), funnels_df(*[f"f{i}" for i in range(5)]), pd.DataFrame(), seed=5)
    b = rank_steps(steps_df(rows), funnels_df(*[f"f{i}" for i in range(5)]), pd.DataFrame(), seed=5)
    assert a["steps"] == b["steps"]
    for r in a["steps"]:
        assert r["uplift_p10"] <= r["uplift_p50"] <= r["uplift_p90"] and r["expected_uplift"] >= 0


def test_no_benchmark_without_peers_and_empty_input() -> None:
    res = rank_steps(steps_df([("f", "a", 0, 100, 0, 0.0), ("f", "b", 1, 40, 4, 100.0)]), funnels_df("f"))
    assert all(r["expected_uplift"] == 0 for r in res["steps"]) and res["notes"]
    empty = rank_steps(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    assert empty["steps"] == [] and empty["notes"]


def test_uplift_scales_with_traffic() -> None:
    def build(scale: int) -> dict:
        rows = []
        for i in range(4):
            rows += [
                (f"g{i}", "a", 0, 1000 * scale, 0, 0.0),
                (f"g{i}", "b", 1, 500 * scale, 0, 0.0),
                (f"g{i}", "c", 2, 250 * scale, 25 * scale, 2500.0 * scale),
            ]
        rows += [
            ("weak", "a", 0, 1000 * scale, 0, 0.0),
            ("weak", "b", 1, 500 * scale, 0, 0.0),
            ("weak", "c", 2, 100 * scale, 10 * scale, 1000.0 * scale),
        ]
        return rank_steps(steps_df(rows), funnels_df("weak", *[f"g{i}" for i in range(4)]))

    small, big = build(1), build(10)
    assert small["steps"][0]["funnel_id"] == big["steps"][0]["funnel_id"] == "weak"
    assert big["steps"][0]["expected_uplift"] > 5 * small["steps"][0]["expected_uplift"]


def test_confirmation_pages_are_left_out_and_single_form_pages_use_optins() -> None:
    """Real Funnels shapes: a lead magnet ends on a thank-you page (no orders, no opt-ins), a newsletter page is the only
    step and converts through opt-ins. Counting only orders ranked every thank-you page first with a 0% pass rate."""
    rows = []
    for i in range(4):
        rows += [(f"lm{i}", "land", 0, 2000, 0, 0.0), (f"lm{i}", "thanks", 1, 500 - 40 * i, 0, 0.0)]
    df = steps_df([*rows, ("news", "signup", 0, 800, 0, 0.0)])
    df["optins"] = 0
    df.loc[df["funnel_id"] == "news", "optins"] = 120
    res = rank_steps(df, funnels_df("lm0", "lm1", "lm2", "lm3", "news"))
    ids = {(r["funnel_id"], r["step_id"]) for r in res["steps"]}
    assert not any(step == "thanks" for _, step in ids)
    news = next(r for r in res["steps"] if r["funnel_id"] == "news")
    assert news["passed"] == 120 and news["raw_rate"] == 0.15


def test_a_final_checkout_with_visitors_but_no_orders_is_kept() -> None:
    rows = [("f", "land", 0, 1000, 0, 0.0), ("f", "pay", 1, 300, 0, 0.0)]
    structure = pd.DataFrame(
        [
            {
                "source_id": "s",
                "funnel_id": "f",
                "step_id": "pay",
                "step_type": None,
                "n_form": 0,
                "n_checkout": 1,
                "n_video": 0,
                "n_countdown": 0,
                "word_count": 50,
                "has_order_bump": False,
                "has_upsell": False,
            }
        ]
    )
    res = rank_steps(steps_df(rows), funnels_df("f"), structure)
    assert {r["step_id"] for r in res["steps"]} == {"land", "pay"}


def test_the_page_before_a_confirmation_page_becomes_the_final_step() -> None:
    rows = []
    for i in range(4):
        rows += [
            (f"s{i}", "offer", 0, 1000, 0, 0.0),
            (f"s{i}", "upsell", 1, 300 - 20 * i, 30 + i, 3000.0),
            (f"s{i}", "thanks", 2, 250 - 20 * i, 0, 0.0),
        ]
    res = rank_steps(steps_df(rows), funnels_df(*[f"s{i}" for i in range(4)]))
    roles = {(r["funnel_id"], r["step_id"]): r["role"] for r in res["steps"]}
    assert all(roles[(f"s{i}", "offer")] == "entry" and roles[(f"s{i}", "upsell")] == "final" for i in range(4))
    assert not any(step == "thanks" for _, step in roles)
    assert res["priors"]["final"]["steps"] == 4
    assert res["steps"][0]["expected_uplift"] > 0  # a benchmark exists for the four final steps
