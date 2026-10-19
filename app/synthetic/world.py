"""Seeded synthetic world for tests and demos, with KNOWN planted ground truth.

Two apps (Funnels + Conversations) share a customer's contacts through HMAC contact keys. Planted signals:

* the weak step  : webinar funnel "Sales Page" passes ~12% of visitors vs ~45% for comparable steps
* high intent    : lead conversion rises with utm_source in {email, webinar}, repeat visits before opt-in,
                   prior purchase-intent conversations and positive prior sentiment (night/hour has no effect)
* SLA breach     : first responses at night, at weekends, in the billing inbox and on email are slow
* anomalies      : webinar-funnel traffic collapses on day `anomaly_traffic_day`; the support backlog spikes
                   on day `anomaly_backlog_day`
* VIPs           : a few contacts with large revenue and open, angry, urgent, overdue rooms
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from app.security.crypto import contact_key

SOURCES = ["email", "webinar", "facebook", "google", "organic"]
SRC_EFFECT = {"email": 1.0, "webinar": 0.8, "facebook": -0.3, "google": 0.2, "organic": 0.0}
SRC_WEIGHTS = [0.18, 0.12, 0.30, 0.20, 0.20]
DEVICES = ["desktop", "mobile", "tablet"]
INBOXES = ["inbox_sales", "inbox_support", "inbox_billing"]
INTENTS = ["support", "billing", "purchase", "other"]
SENTIMENTS = ["negative", "neutral", "positive"]
URGENCIES = ["low", "medium", "high"]
CHANNELS = ["dm", "sms", "email"]

PLANTED = {
    "weak_step": ("fn_webinar", "st_web_sales"),
    "tiny_funnel": "fn_pilot",
    "high_intent_features": ["utm_source", "n_page_views_pre", "prior_intent_purchase"],
    "no_effect_features": ["optin_hour", "optin_dow"],
}

# (step_id, name, type, base logit to pass to next step / order, word_count, blocks)
FUNNEL_DEFS: list[dict[str, Any]] = [
    {
        "id": "fn_webinar",
        "name": "Webinar Funnel",
        "weight": 0.52,
        "price": 197.0,
        "steps": [
            ("st_web_reg", "Registration Page", "optin", -0.55, 320, {"form": 1, "video": 0, "countdown": 0}, False, False),
            ("st_web_conf", "Webinar Room", "webinar", 0.9, 150, {"form": 0, "video": 1, "countdown": 1}, False, False),
            ("st_web_sales", "Sales Page", "sales", -2.45, 3400, {"form": 0, "video": 0, "countdown": 0}, False, False),
            (
                "st_web_checkout",
                "Checkout",
                "checkout",
                -0.15,
                260,
                {"form": 1, "checkout": 1, "video": 0, "countdown": 0},
                False,
                False,
            ),
        ],
    },
    {
        "id": "fn_magnet",
        "name": "Lead Magnet Funnel",
        "weight": 0.47,
        "price": 47.0,
        "steps": [
            ("st_mag_land", "Landing", "optin", -0.75, 280, {"form": 1, "video": 0, "countdown": 0}, False, False),
            (
                "st_mag_thanks",
                "Thank You + Tripwire",
                "thankyou",
                0.1,
                400,
                {"form": 0, "video": 1, "countdown": 1},
                False,
                False,
            ),
            ("st_mag_offer", "Offer Page", "sales", -0.2, 900, {"form": 0, "video": 1, "countdown": 1}, False, False),
            (
                "st_mag_checkout",
                "Checkout",
                "checkout",
                -0.3,
                240,
                {"form": 1, "checkout": 1, "video": 0, "countdown": 0},
                True,
                True,
            ),
        ],
    },
    {
        "id": "fn_pilot",
        "name": "Pilot Funnel (tiny)",
        "weight": 0.004,
        "price": 97.0,
        "steps": [
            ("st_pil_land", "Landing", "optin", -0.4, 300, {"form": 1, "video": 0, "countdown": 0}, False, False),
            ("st_pil_offer", "Offer", "sales", -1.2, 700, {"form": 0, "video": 0, "countdown": 0}, False, False),
            (
                "st_pil_checkout",
                "Checkout",
                "checkout",
                -0.3,
                220,
                {"form": 1, "checkout": 1, "video": 0, "countdown": 0},
                False,
                False,
            ),
        ],
    },
]


_HW = np.array([1, 1, 1, 1, 1, 2, 3, 5, 7, 8, 8, 8, 7, 7, 7, 7, 7, 7, 7, 8, 8, 6, 3, 2], float)
HOUR_W = _HW / _HW.sum()


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def iso(ts: float | int) -> str:
    return datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class WorldConfig:
    seed: int = 7
    n_days: int = 120
    end: datetime = datetime(2026, 10, 1, tzinfo=UTC)
    visitors_per_day: float = 150.0
    pepper: str = "shared-pepper-for-tests"
    conversations_pepper: str | None = None  # set to simulate a pepper mismatch
    include_funnels: bool = True
    include_conversations: bool = True
    plant_anomalies: bool = True
    anomaly_traffic_day: int = 95  # days since start
    anomaly_backlog_day: int = 100
    support_only_contacts: int = 900


@dataclass
class World:
    config: WorldConfig
    start_ts: int
    end_ts: int
    events: list[dict[str, Any]] = field(default_factory=list)
    leads: list[dict[str, Any]] = field(default_factory=list)
    rooms: list[dict[str, Any]] = field(default_factory=list)
    messages_meta: list[dict[str, Any]] = field(default_factory=list)
    structure: list[dict[str, Any]] = field(default_factory=list)
    truth: dict[str, Any] = field(default_factory=dict)

    # --- derived views used by the fake apps ---------------------------------------------------------
    def funnels_table(self) -> list[dict[str, Any]]:
        ev = pd.DataFrame(self.events)
        out = []
        for fd in FUNNEL_DEFS:
            if ev.empty:
                continue
            e = ev[ev.funnel_id == fd["id"]]
            first = fd["steps"][0][0]
            pv = e[(e.type == "page_view") & (e.step_id == first)]
            orders = e[e.type == "order"]
            gross = float(orders.amount.sum())
            uv = int(pv.unique_visit.sum())
            out.append(
                {
                    "id": fd["id"],
                    "name": fd["name"],
                    "status": "published",
                    "steps_count": len(fd["steps"]),
                    "visits": len(pv),
                    "unique_visits": uv,
                    "optins": int((e.type == "optin").sum()),
                    "optin_rate": round(float((e.type == "optin").sum()) / max(uv, 1), 4),
                    "orders": len(orders),
                    "gross": round(gross, 2),
                    "currency": "USD",
                    "earnings_per_view": round(gross / max(len(pv), 1), 4),
                    "created_at": iso(self.start_ts),
                }
            )
        return out

    def steps_table(self, funnel_id: str) -> list[dict[str, Any]]:
        fd = next((f for f in FUNNEL_DEFS if f["id"] == funnel_id), None)
        if fd is None or not self.events:
            return []
        ev = pd.DataFrame(self.events)
        e = ev[ev.funnel_id == funnel_id]
        rows: list[dict[str, Any]] = []
        uniq: list[int] = []
        for i, st in enumerate(fd["steps"]):
            s = e[e.step_id == st[0]]
            pv = s[s.type == "page_view"]
            orders = s[s.type == "order"]
            uniq.append(int(pv.unique_visit.sum()))
            rows.append(
                {
                    "step_id": st[0],
                    "name": st[1],
                    "order": i,
                    "visits": len(pv),
                    "unique_visits": uniq[-1],
                    "optins": int((s.type == "optin").sum()),
                    "optin_rate": 0.0,
                    "orders": len(orders),
                    "gross": round(float(orders.amount.sum()), 2),
                    "exit_rate": 0.0,
                    "avg_time_on_page_s": None,
                }
            )
        for i, r in enumerate(rows):
            if i + 1 < len(rows):
                nxt = uniq[i + 1]
                r["exit_rate"] = round(1 - nxt / max(uniq[i], 1), 4)
            else:
                r["exit_rate"] = round(1 - r["orders"] / max(uniq[i], 1), 4)
            r["optin_rate"] = round(r["optins"] / max(uniq[i], 1), 4)
        return rows


def generate_world(cfg: WorldConfig | None = None) -> World:
    cfg = cfg or WorldConfig()
    rng = np.random.default_rng(cfg.seed)
    end_ts = int(cfg.end.timestamp())
    start_ts = end_ts - cfg.n_days * 86400
    world = World(config=cfg, start_ts=start_ts, end_ts=end_ts)
    conv_pepper = cfg.conversations_pepper or cfg.pepper

    contact_counter = 0
    contacts_email: dict[int, str] = {}

    def new_contact() -> int:
        nonlocal contact_counter
        contact_counter += 1
        contacts_email[contact_counter] = f"user{contact_counter}@example.test"
        return contact_counter

    def ck(cid: int, pepper: str) -> str:
        return contact_key(pepper, contacts_email[cid])

    # visitor-level arrays across funnels
    ev_rows: list[pd.DataFrame] = []
    lead_rows: list[dict[str, Any]] = []
    prior_room_specs: list[dict[str, Any]] = []  # rooms to create before opt-in
    contact_revenue: dict[int, float] = {}
    visitor_counter = 0
    day_idx = np.arange(cfg.n_days)
    dow = (start_ts // 86400 + 4 + day_idx) % 7  # 1970-01-01 was Thursday (3 in Mon=0) -> approx; only shape matters
    weekly = np.where(np.isin(dow, [5, 6]), 0.62, 1.0)
    trend = 1.0 + 0.002 * day_idx
    hour_w = HOUR_W

    if cfg.include_funnels:
        for fd in FUNNEL_DEFS:
            lam = cfg.visitors_per_day * fd["weight"] * weekly * trend
            if cfg.plant_anomalies and fd["id"] == "fn_webinar":
                lam = lam.copy()
                lam[cfg.anomaly_traffic_day : cfg.anomaly_traffic_day + 3] *= 0.15
            counts = rng.poisson(lam)
            n = int(counts.sum())
            if n == 0:
                continue
            day_of = np.repeat(day_idx, counts)
            hour_of = rng.choice(24, size=n, p=hour_w)
            arrival = start_ts + day_of * 86400 + hour_of * 3600 + rng.integers(0, 3600, size=n)
            src = rng.choice(len(SOURCES), size=n, p=SRC_WEIGHTS)
            device = rng.choice(3, size=n, p=[0.55, 0.38, 0.07])
            n_visits = 1 + np.minimum(rng.geometric(0.55, size=n) - 1, 4)
            prior_flag = rng.random(n) < 0.25
            prior_purchase = prior_flag & (rng.random(n) < 0.45)
            prior_sent = np.where(prior_flag, np.clip(rng.normal(0.1 + 0.3 * prior_purchase, 0.5), -1, 1), 0.0)
            z = rng.normal(0, 1, n)
            u = (
                np.array([SRC_EFFECT[SOURCES[i]] for i in src])
                + 0.5 * (n_visits - 1)
                + 1.5 * prior_purchase
                + 0.5 * prior_sent
                - 0.4 * (device == 1)
                + 0.7 * z
            )
            steps = fd["steps"]
            k = len(steps)
            reach = np.zeros((n, k + 1), dtype=bool)  # reach[:, j] = viewed step j ; reach[:, k] = ordered in funnel
            reach[:, 0] = True
            for j in range(k):
                coef = 0.45 if j == 0 else 0.9
                passed = rng.random(n) < _sigmoid(steps[j][3] + coef * u)
                reach[:, j + 1] = reach[:, j] & passed
            vids = np.arange(visitor_counter, visitor_counter + n)
            visitor_counter += n
            # repeat visits before opt-in: visit times
            gaps = rng.uniform(0.4, 6.0, size=(n, 5)) * 86400
            gaps[:, 0] = 0.0
            visit_ts = arrival[:, None] + np.cumsum(gaps, axis=1)
            idx = np.arange(5)[None, :]
            visit_mask = idx < n_visits[:, None]
            last_visit = visit_ts[np.arange(n), n_visits - 1]
            optin = reach[:, 1]
            optin_ts = last_visit + rng.integers(20, 300, size=n)
            # contact assignment
            cids = np.zeros(n, dtype=int)
            for i in np.flatnonzero(optin):
                cids[i] = new_contact()
            # step timestamps after optin
            step_delay = np.cumsum(
                rng.exponential(1.0, size=(n, k)) * np.array([0, 900, 0.5 * 86400, 3600, 3600][:k])[None, :], axis=1
            )
            step_ts = optin_ts[:, None] + step_delay  # step j (>=1) view ts ; col 0 unused for j=0 (visit time)
            frame_parts: list[pd.DataFrame] = []
            # page views on step 0: each visit
            for v in range(5):
                m = visit_mask[:, v]
                if not m.any():
                    continue
                frame_parts.append(
                    pd.DataFrame(
                        {
                            "ts": visit_ts[m, v],
                            "type": "page_view",
                            "funnel_id": fd["id"],
                            "step_id": steps[0][0],
                            "vi": vids[m],
                            "cid": 0,
                            "src": src[m],
                            "dev": device[m],
                            "unique_visit": v == 0,
                        }
                    )
                )
            frame_parts.append(
                pd.DataFrame(
                    {
                        "ts": optin_ts[optin],
                        "type": "optin",
                        "funnel_id": fd["id"],
                        "step_id": steps[0][0],
                        "vi": vids[optin],
                        "cid": cids[optin],
                        "src": src[optin],
                        "dev": device[optin],
                        "unique_visit": False,
                    }
                )
            )
            for j in range(1, k):
                m = reach[:, j]
                frame_parts.append(
                    pd.DataFrame(
                        {
                            "ts": step_ts[m, j],
                            "type": "page_view",
                            "funnel_id": fd["id"],
                            "step_id": steps[j][0],
                            "vi": vids[m],
                            "cid": cids[m],
                            "src": src[m],
                            "dev": device[m],
                            "unique_visit": True,
                        }
                    )
                )
            ordered_in = reach[:, k]
            amount_in = np.round(fd["price"] * np.exp(rng.normal(0, 0.12, n)), 2)
            frame_parts.append(
                pd.DataFrame(
                    {
                        "ts": step_ts[ordered_in, k - 1] + rng.integers(60, 900, size=int(ordered_in.sum())),
                        "type": "order",
                        "funnel_id": fd["id"],
                        "step_id": steps[k - 1][0],
                        "vi": vids[ordered_in],
                        "cid": cids[ordered_in],
                        "src": src[ordered_in],
                        "dev": device[ordered_in],
                        "unique_visit": False,
                        "amount": amount_in[ordered_in],
                    }
                )
            )
            # late orders (email follow-ups etc.) within 14 days of opt-in, not tied to a step
            late_p = _sigmoid(-3.0 + 1.2 * u)
            late = optin & ~ordered_in & (rng.random(n) < late_p)
            late_ts = optin_ts + rng.uniform(0.5, 13.5, n) * 86400
            frame_parts.append(
                pd.DataFrame(
                    {
                        "ts": late_ts[late],
                        "type": "order",
                        "funnel_id": fd["id"],
                        "step_id": None,
                        "vi": vids[late],
                        "cid": cids[late],
                        "src": src[late],
                        "dev": device[late],
                        "unique_visit": False,
                        "amount": amount_in[late],
                    }
                )
            )
            fr = pd.concat(frame_parts, ignore_index=True)
            fr["vkey"] = [f"v{v:07d}" for v in fr["vi"]]
            ev_rows.append(fr)
            # leads
            order_df = fr[fr.type == "order"].sort_values("ts").drop_duplicates("vi")
            first_order = dict(zip(order_df.vi, order_df.ts, strict=True))
            order_amt = dict(zip(order_df.vi, order_df.amount, strict=True))
            for i in np.flatnonzero(optin):
                if optin_ts[i] > end_ts:
                    continue
                vi = int(vids[i])
                fo = first_order.get(vi)
                if fo is not None and fo > end_ts:
                    fo = None
                cid = int(cids[i])
                conv = fo is not None
                if conv:
                    contact_revenue[cid] = contact_revenue.get(cid, 0.0) + float(order_amt[vi])
                lead_rows.append(
                    {
                        "id": f"lead_{vi}",
                        "created_at": iso(optin_ts[i]),
                        "funnel_id": fd["id"],
                        "step_id": steps[0][0],
                        "contact_key": ck(cid, cfg.pepper),
                        "source": SOURCES[src[i]],
                        "utm_source": SOURCES[src[i]],
                        "utm_campaign": f"camp_{SOURCES[src[i]]}_{int(rng.integers(1, 4))}",
                        "converted_to_order": conv,
                        "order_total": float(order_amt[vi]) if conv else 0.0,
                        "first_order_at": iso(fo) if (conv and fo is not None) else None,
                        "_cid": cid,
                    }
                )
                if prior_flag[i] and arrival[i] > start_ts + 10 * 86400:  # earlier history lies before the data window
                    prior_room_specs.append(
                        {
                            "cid": cid,
                            "before": float(optin_ts[i]),
                            "purchase": bool(prior_purchase[i]),
                            "sent": float(prior_sent[i]),
                        }
                    )

        ev = pd.concat(ev_rows, ignore_index=True)
        ev = ev[ev.ts <= end_ts].copy()
        # opt-in contact keys on every event after opt-in (cid != 0 only on post-opt-in rows)
        cid_by_v = ev[ev.cid > 0].groupby("vi").cid.first()
        ev["cid"] = ev["vi"].map(cid_by_v).fillna(0).astype(int)
        opt_ts_by_v = ev[ev.type == "optin"].groupby("vi").ts.min()
        ev["opt_ts"] = ev["vi"].map(opt_ts_by_v)
        # events before the opt-in moment are anonymous
        anon = ev.opt_ts.isna() | (ev.ts < ev.opt_ts)
        ev["contact_key"] = [ck(int(c), cfg.pepper) if (c > 0 and not a) else None for c, a in zip(ev.cid, anon, strict=True)]
        ev = ev.sort_values(["ts", "type"], kind="stable").reset_index(drop=True)
        n_ev = len(ev)
        ev["id"] = [f"evt_{i:08d}" for i in range(n_ev)]
        cc = {"campaign": [f"camp_{SOURCES[s]}_{(i % 3) + 1}" for i, s in enumerate(ev.src)]}
        ev["utm_campaign"] = cc["campaign"]
        ev["utm_source"] = [SOURCES[s] for s in ev.src]
        ev["utm_medium"] = np.where(ev.src == 0, "email", "cpc")
        ev["device"] = [DEVICES[d] for d in ev.dev]
        ev["country"] = rng.choice(["US", "GB", "DE", "IN", None], size=n_ev, p=[0.5, 0.15, 0.1, 0.15, 0.1])
        ev["referrer_host"] = None
        if "amount" not in ev.columns:
            ev["amount"] = np.nan
        ev["currency"] = np.where(ev.type == "order", "USD", "")
        recs = []
        for r in ev.itertuples(index=False):
            recs.append(
                {
                    "id": r.id,
                    "ts": iso(r.ts),
                    "type": r.type,
                    "funnel_id": r.funnel_id,
                    "step_id": None if (r.step_id is None or r.step_id != r.step_id) else r.step_id,
                    "visitor_key": r.vkey,
                    "contact_key": r.contact_key if isinstance(r.contact_key, str) else None,
                    "utm_source": r.utm_source,
                    "utm_medium": r.utm_medium,
                    "utm_campaign": r.utm_campaign,
                    "device": r.device,
                    "country": r.country if isinstance(r.country, str) else None,
                    "referrer_host": None,
                    "amount": None if r.amount != r.amount else float(r.amount),
                    "currency": r.currency if isinstance(r.currency, str) else None,
                    "unique_visit": bool(r.unique_visit),
                }
            )
        world.events = recs
        world.leads = sorted(lead_rows, key=lambda x: (x["created_at"], x["id"]))
        for lead in world.leads:
            lead.pop("_cid", None)
        for fd in FUNNEL_DEFS:
            for j, st in enumerate(fd["steps"]):
                world.structure.append(
                    {
                        "funnel_id": fd["id"],
                        "funnel_name": fd["name"],
                        "step_id": st[0],
                        "name": st[1],
                        "order": j,
                        "type": st[2],
                        "blocks": st[5],
                        "word_count": st[4],
                        "has_order_bump": st[6],
                        "has_upsell": st[7],
                    }
                )

    # ----------------------------------------------------------------------------------------------------
    if cfg.include_conversations:
        _generate_conversations(world, rng, cfg, conv_pepper, contacts_email, contact_revenue, prior_room_specs, new_contact)
    world.truth = {
        "weak_step": PLANTED["weak_step"],
        "tiny_funnel": PLANTED["tiny_funnel"],
        "traffic_anomaly": {"funnel_id": "fn_webinar", "metric": "visits", "day": cfg.anomaly_traffic_day},
        "backlog_anomaly_day": cfg.anomaly_backlog_day,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "vip_keys": world.truth.get("vip_keys", []),
        "planted_open_rooms": world.truth.get("planted_open_rooms", []),
        "email_for_contact": dict(list(contacts_email.items())[:3]),
        "first_contact_email": contacts_email.get(1),
    }
    return world


def _generate_conversations(
    world: World,
    rng: np.random.Generator,
    cfg: WorldConfig,
    pepper: str,
    emails: dict[int, str],
    contact_revenue: dict[int, float],
    prior_specs: list[dict[str, Any]],
    new_contact: Any,
) -> None:
    start_ts, end_ts = world.start_ts, world.end_ts
    rows: list[dict[str, Any]] = []

    def ckey(cid: int) -> str:
        return contact_key(pepper, emails[cid])

    # (a) prior conversations of existing contacts (planted: purchase intent / sentiment precede opt-in)
    specs: list[dict[str, Any]] = []
    for s in prior_specs:
        t = s["before"] - float(rng.uniform(1.0, 9.0)) * 86400
        specs.append(
            {
                "cid": s["cid"],
                "ts": max(t, start_ts + 3600),
                "intent": "purchase" if s["purchase"] else str(rng.choice(["support", "billing", "other"])),
                "sent": s["sent"],
            }
        )
    # (b) post-opt-in chatter for ~12% of leads, (c) support-only contacts
    lead_cids = {ld["contact_key"]: None for ld in world.leads}
    del lead_cids
    n_support = cfg.support_only_contacts
    support_cids = [new_contact() for _ in range(n_support)]
    for cid in support_cids:
        for _ in range(1 + int(rng.poisson(0.8))):
            specs.append(
                {
                    "cid": cid,
                    "ts": float(rng.uniform(start_ts, end_ts)),
                    "intent": str(rng.choice(INTENTS, p=[0.5, 0.2, 0.1, 0.2])),
                    "sent": float(np.clip(rng.normal(-0.1, 0.5), -1, 1)),
                }
            )
    # extra baseline traffic by day with weekly pattern
    n_days = cfg.n_days
    day_idx = np.arange(n_days)
    dow_arr = (start_ts // 86400 + 3 + day_idx) % 7
    lam = 38 * np.where(np.isin(dow_arr, [5, 6]), 0.6, 1.0) * (1 + 0.002 * day_idx)
    counts = rng.poisson(lam)
    extra_cids = [new_contact() for _ in range(int(counts.sum()) // 2 + 1)]
    for d, c in enumerate(counts):
        for _ in range(int(c)):
            hour = int(rng.choice(24, p=HOUR_W))
            specs.append(
                {
                    "cid": int(rng.choice(extra_cids)),
                    "ts": start_ts + d * 86400 + hour * 3600 + float(rng.integers(0, 3600)),
                    "intent": str(rng.choice(INTENTS, p=[0.45, 0.2, 0.15, 0.2])),
                    "sent": float(np.clip(rng.normal(0, 0.5), -1, 1)),
                }
            )
    specs = [s for s in specs if s["ts"] < end_ts - 5]
    specs.sort(key=lambda s: s["ts"])
    n = len(specs)
    ts: Any = np.array([s["ts"] for s in specs])
    hour = ((ts // 3600) % 24).astype(int)
    dow_r = (((ts // 86400) + 3) % 7).astype(int)
    inbox = rng.choice(3, size=n, p=[0.4, 0.45, 0.15])
    channel = rng.choice(3, size=n, p=[0.5, 0.2, 0.3])
    priority = (rng.random(n) < 0.1).astype(int)
    intent = np.array([s["intent"] for s in specs])
    sent = np.array([s["sent"] for s in specs])
    sent_lab = np.where(sent < -0.25, "negative", np.where(sent > 0.25, "positive", "neutral"))
    urg = np.where(
        (sent_lab == "negative") & (rng.random(n) < 0.5),
        "high",
        rng.choice(URGENCIES, size=n, p=[0.5, 0.35, 0.15]),
    )
    night = (hour >= 22) | (hour < 6)
    weekend = np.isin(dow_r, [5, 6])
    breach_logit = -1.3 + 1.4 * night + 0.8 * weekend + 1.0 * (inbox == 2) + 0.6 * (channel == 2) - 0.3 * priority
    breach = rng.random(n) < _sigmoid(breach_logit)
    sla = 3600.0
    first_resp = np.where(breach, sla * (1 + rng.exponential(1.5, n)), sla * rng.uniform(0.03, 0.9, n))
    resolution = first_resp + rng.lognormal(8.5, 0.8, n)
    slow_tail = rng.random(n) < 0.05  # a few rooms stay open for days
    resolution = np.where(slow_tail, resolution + rng.uniform(1.0, 3.0, n) * 86400, resolution)
    if cfg.plant_anomalies:  # incident: rooms opened that day are not closed for days -> backlog spike
        day_of = ((ts - start_ts) // 86400).astype(int)
        incident = day_of == cfg.anomaly_backlog_day
        resolution = np.where(incident, 4 * 86400 + rng.uniform(0, 86400, n), resolution)
    closed_at = ts + resolution
    waiting = ts + first_resp > end_ts
    open_room = closed_at > end_ts
    csat = np.where(
        rng.random(n) < 0.4,
        np.clip(np.round(4.3 - 1.2 * breach - 0.6 * (sent_lab == "negative") + rng.normal(0, 0.7, n)), 1, 5),
        np.nan,
    )
    csat = np.where(open_room, np.nan, csat)
    for i, s in enumerate(specs):
        cid = s["cid"]
        mix = [0, 0, 0]
        m_in = int(1 + rng.poisson(2.5))
        m_out = int(0 if waiting[i] else 1 + rng.poisson(2.0))
        mix[channel[i]] = m_in + m_out
        rows.append(
            {
                "id": f"room_{i:06d}",
                "created_at": iso(ts[i]),
                "closed_at": None if open_room[i] else iso(closed_at[i]),
                "contact_key": ckey(cid),
                "channel_mix": {"dm": mix[0], "sms": mix[1], "email": mix[2]},
                "inbox_id": INBOXES[inbox[i]],
                "assignee_key": None if waiting[i] else f"agent_{i % 5}",
                "priority": int(priority[i]),
                "status": "open" if open_room[i] else "closed",
                "messages_in": m_in,
                "messages_out": m_out,
                "first_response_s": None if waiting[i] else float(round(first_resp[i], 1)),
                "resolution_s": None if open_room[i] else float(round(resolution[i], 1)),
                "reopen_count": int(rng.poisson(0.1)),
                "snoozed_count": int(rng.poisson(0.05)),
                "ai_intent": str(intent[i]),
                "ai_sentiment": str(sent_lab[i]),
                "ai_urgency": str(urg[i]),
                "csat": None if np.isnan(csat[i]) else float(csat[i]),
                "hour_of_day_created": int(hour[i]),
                "dow_created": int(dow_r[i]),
            }
        )
    # planted VIP / critical open rooms (as of end)
    vips: list[str] = []
    planted: list[str] = []
    for j in range(4):
        cid = new_contact()
        vkey = ckey(cid)
        vips.append(vkey)
        # give VIPs revenue through funnel orders when funnels exist
        if world.events or cfg.include_funnels:
            for o in range(5):
                t = end_ts - (40 - o * 5) * 86400
                world.events.append(
                    {
                        "id": f"evt_vip_{j}_{o}",
                        "ts": iso(t),
                        "type": "order",
                        "funnel_id": "fn_webinar",
                        "step_id": "st_web_checkout",
                        "visitor_key": f"vip{j}",
                        "contact_key": ckey(cid) if pepper == cfg.pepper else contact_key(cfg.pepper, emails[cid]),
                        "utm_source": "email",
                        "utm_medium": "email",
                        "utm_campaign": "camp_vip",
                        "device": "desktop",
                        "country": "US",
                        "referrer_host": None,
                        "amount": 250.0,
                        "currency": "USD",
                        "unique_visit": False,
                    }
                )
        rid = f"room_planted_{j}"
        planted.append(rid)
        created = end_ts - (5 + j) * 3600 * 6
        rows.append(
            {
                "id": rid,
                "created_at": iso(created),
                "closed_at": None,
                "contact_key": vkey,
                "channel_mix": {"dm": 3, "sms": 0, "email": 0},
                "inbox_id": "inbox_billing",
                "assignee_key": None,
                "priority": 1,
                "status": "open",
                "messages_in": 3,
                "messages_out": 0,
                "first_response_s": None,
                "resolution_s": None,
                "reopen_count": 2,
                "snoozed_count": 0,
                "ai_intent": "billing",
                "ai_sentiment": "negative",
                "ai_urgency": "high",
                "csat": None,
                "hour_of_day_created": int((created // 3600) % 24),
                "dow_created": int(((created // 86400) + 3) % 7),
            }
        )
    # a calm, recent, low-risk open room to contrast
    rows.append(
        {
            "id": "room_calm",
            "created_at": iso(end_ts - 1800),
            "closed_at": None,
            "contact_key": ckey(extra_cids[0]),
            "channel_mix": {"dm": 1, "sms": 0, "email": 0},
            "inbox_id": "inbox_sales",
            "assignee_key": None,
            "priority": 0,
            "status": "open",
            "messages_in": 1,
            "messages_out": 0,
            "first_response_s": None,
            "resolution_s": None,
            "reopen_count": 0,
            "snoozed_count": 0,
            "ai_intent": "other",
            "ai_sentiment": "positive",
            "ai_urgency": "low",
            "csat": None,
            "hour_of_day_created": int(((end_ts - 1800) // 3600) % 24),
            "dow_created": int((((end_ts - 1800) // 86400) + 3) % 7),
        }
    )
    world.events.sort(key=lambda e: (e["ts"], e["id"]))
    world.rooms = sorted(rows, key=lambda r: (r["created_at"], r["id"]))
    world.truth["vip_keys"] = vips
    world.truth["planted_open_rooms"] = planted
    # message meta
    msgs: list[dict[str, Any]] = []
    for r in world.rooms[::3]:
        t0 = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).timestamp()
        for m in range(r["messages_in"] + r["messages_out"]):
            msgs.append(
                {
                    "id": f"msg_{r['id']}_{m}",
                    "room_id": r["id"],
                    "ts": iso(t0 + m * 120),
                    "direction": "in" if m < r["messages_in"] else "out",
                    "channel": "dm",
                    "kind": "text",
                    "length_chars": int(rng.integers(5, 400)),
                    "delivery_status": "delivered",
                    "sender_type": "contact" if m < r["messages_in"] else "agent",
                }
            )
    world.messages_meta = sorted(msgs, key=lambda m: (m["ts"], m["id"]))
