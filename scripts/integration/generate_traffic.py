#!/usr/bin/env python3
"""Drive the REAL Funnels and Conversations apps through their HTTP APIs to build a realistic history.

Nothing here touches the apps' databases. Funnels: `/api/public/events`, `/api/public/forms/...` and
`/api/public/checkout/...` (what a visitor's browser calls) plus the authenticated REST API for discovery.
Conversations: the widget API (`/public/sessions`, `/public/rooms`, ...) for visitors and the team REST API
(`/api/rooms/...`) for agents.

Time: the public APIs always stamp "now". To get history the harness starts both apps with `fakeclock.mjs`, which
shifts the apps' own clock by the offset stored in a file; this script rewrites that file before every request so
events land at the intended instant. The schedule is globally time-ordered, so each app's clock only moves forward.
At the end the offset is reset to 0.

The same set of people (same e-mail addresses) is used in both apps, so the cross-app `contact_key` join has overlap.

    python generate_traffic.py --funnels-url http://127.0.0.1:P1 --conversations-url http://127.0.0.1:P2 \
        --funnels-clock /tmp/f.clock --conversations-clock /tmp/c.clock --days 21 --out traffic.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

DEMO_EMAIL = "demo@example.test"
DEMO_PASSWORD = "demo-password-123"  # the apps' documented seed credentials (test data, not a secret)
UA = {
    "desktop": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "mobile": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
    "tablet": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
}
FIRST = [
    "Ada",
    "Grace",
    "Alan",
    "Linus",
    "Margaret",
    "Dennis",
    "Barbara",
    "Ken",
    "Radia",
    "Tim",
    "Hedy",
    "Claude",
    "Katherine",
    "Donald",
    "Frances",
    "Guido",
    "Sophie",
    "Omar",
    "Priya",
    "Mateo",
    "Ines",
    "Chen",
    "Yuki",
    "Noah",
]
LAST = [
    "Lovelace",
    "Hopper",
    "Turing",
    "Torvalds",
    "Hamilton",
    "Ritchie",
    "Liskov",
    "Thompson",
    "Perlman",
    "Lee",
    "Lamarr",
    "Shannon",
    "Johnson",
    "Knuth",
    "Allen",
    "Rossum",
    "Germain",
    "Haddad",
    "Nair",
    "Ruiz",
    "Costa",
    "Wei",
    "Tanaka",
    "Okafor",
]
SOURCES = [  # (utm source, medium, campaign, weight, opt-in propensity, buy propensity)
    ("google", "cpc", "brand", 0.28, 0.36, 0.20),
    ("facebook", "paid_social", "retarget", 0.24, 0.22, 0.09),
    ("newsletter", "email", "weekly", 0.18, 0.52, 0.30),
    ("partner", "referral", "affiliate", 0.10, 0.30, 0.16),
    (None, None, None, 0.20, 0.30, 0.12),
]
REFERRERS = {
    "google": "google.com",
    "facebook": "facebook.com",
    "newsletter": "mail.example.test",
    "partner": "partner.example.test",
}
INCIDENT_DAYS = 3
HOUR_W = [1, 1, 1, 1, 1, 2, 3, 5, 7, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 9, 9, 8, 5, 2]


@dataclass
class Person:
    idx: int
    first: str
    last: str
    email: str  # as typed by the person (may have different case)
    source: tuple[Any, ...]
    device: str
    p_buy: float
    in_funnels: bool = False
    in_conv: bool = False
    opted_in_at: float | None = None
    bought: bool = False
    visitor: str = ""

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}"

    @property
    def canon(self) -> str:
        return self.email.strip().lower()


class Clock:
    def __init__(self, path: str | None) -> None:
        self.path = path

    def at(self, t_s: float) -> None:
        if not self.path:
            return
        off = int(t_s * 1000) - int(time.time() * 1000)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            f.write(str(off))
        import os

        os.replace(tmp, self.path)
        time.sleep(0.006)

    def reset(self) -> None:
        if self.path:
            with open(self.path, "w") as f:
                f.write("0")
            time.sleep(0.02)


@dataclass
class Stats:
    calls: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    samples: dict[str, str] = field(default_factory=dict)

    def note(self, label: str, resp: httpx.Response) -> None:
        self.calls[label] += 1
        if resp.status_code >= 400:
            key = f"{label} {resp.status_code}"
            self.errors[key] += 1
            self.samples.setdefault(key, resp.text[:200])


def send(http: httpx.Client, method: str, url: str, **kw: Any) -> httpx.Response:
    """HTTP call that honours 429 Retry-After (real seconds, capped) a few times."""
    for attempt in range(4):
        r = http.request(method, url, **kw)
        if r.status_code != 429 or attempt == 3:
            return r
        time.sleep(min(float(r.headers.get("retry-after", "1") or 1), 3.0) + 0.05)
    return r


def make_people(n: int, rng: random.Random) -> list[Person]:
    people: list[Person] = []
    seen: set[str] = set()
    weights = [s[3] for s in SOURCES]
    i = 0
    while len(people) < n:
        first, last = rng.choice(FIRST), rng.choice(LAST)
        email = f"{first}.{last}{rng.randint(1, 999)}@example.com".lower()
        if email in seen:
            continue
        seen.add(email)
        src = rng.choices(SOURCES, weights)[0]
        device = rng.choices(["desktop", "mobile", "tablet"], [0.55, 0.38, 0.07])[0]
        # latent buying propensity: source and device effects plus noise
        z = -1.2 + 3.0 * (src[5] - 0.15) + (0.35 if device == "desktop" else -0.2) + rng.gauss(0, 0.6)
        p = 1 / (1 + math.exp(-z))
        shown = email
        if rng.random() < 0.06:  # typed with other case / stray space: the contact_key must still match
            shown = email.title()
        people.append(Person(i, first, last, shown, src, device, p, visitor=f"v{rng.getrandbits(48):012x}"))
        i += 1
    return people


def day_hour_time(day_start: float, rng: random.Random) -> float:
    h = rng.choices(range(24), HOUR_W)[0]
    return day_start + h * 3600 + rng.random() * 3600


# ------------------------------------------------------------------------------------------------------------
class Funnels:
    # templates the demo workspace may lack; they are created and published through the REST API
    EXTRA_TEMPLATES = ("product-launch", "newsletter-signup", "book-a-call")

    def __init__(self, base: str, clock: Clock, stats: Stats) -> None:
        self.base = base.rstrip("/")
        self.clock = clock
        self.stats = stats
        self.http = httpx.Client(timeout=30)
        self.funnels: list[dict[str, Any]] = []

    def login(self) -> dict[str, str]:
        r = self.http.post(f"{self.base}/api/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
        r.raise_for_status()
        return {"authorization": f"Bearer {r.json()['token']}"}

    def prepare(self, rng: random.Random) -> None:
        """Publish enough funnels (a key only sees `live` ones; the ranking needs several steps per role) and learn their blocks."""
        h = self.login()
        domains = self.http.get(f"{self.base}/api/domains", headers=h).json()["data"]
        domain_id = next(d["id"] for d in domains if d["status"] == "active")
        existing = self.http.get(f"{self.base}/api/funnels", params={"limit": 100}, headers=h).json()["data"]
        have = {f["name"] for f in existing}
        templates = {t["slug"]: t for t in self.http.get(f"{self.base}/api/templates", headers=h).json()["data"]}
        for slug in self.EXTRA_TEMPLATES:
            if slug in templates and templates[slug]["name"] not in have:
                r = self.http.post(f"{self.base}/api/templates/{templates[slug]['id']}/use", headers=h)
                self.stats.note("funnels.use_template", r)
        for f in self.http.get(f"{self.base}/api/funnels", params={"limit": 100}, headers=h).json()["data"]:
            if f["name"] == "Work in progress":
                continue  # stays a draft on purpose: a funnel nobody can see must not show up in exports
            if f["status"] != "live":
                r = self.http.patch(
                    f"{self.base}/api/funnels/{f['id']}", headers=h, json={"status": "live", "domain_id": domain_id}
                )
                self.stats.note("funnels.publish", r)
        for f in self.http.get(f"{self.base}/api/funnels", params={"limit": 100}, headers=h).json()["data"]:
            if f["status"] != "live":
                continue
            detail = self.http.get(f"{self.base}/api/funnels/{f['id']}", headers=h).json()
            detail = detail.get("funnel", detail)
            steps = []
            for s in detail["steps"]:
                page = self.http.get(f"{self.base}/api/funnels/{f['id']}/steps/{s['id']}/page", headers=h).json()
                doc = page["doc"]
                # GrapesJS project document (version 2): the server derives the form and checkout definitions
                # from the project, so they are read from `forms` / `products`; the project itself is never edited here
                if doc.get("version") != 2:
                    raise SystemExit(f"unexpected page document version {doc.get('version')!r}: expected 2 (GrapesJS project)")
                steps.append(
                    {
                        "id": s["id"],
                        "name": s["name"],
                        "forms": list(doc["forms"]),
                        "checkouts": list(doc["products"]),
                    }
                )
            n = len(steps)
            self.funnels.append(
                {
                    "id": f["id"],
                    "name": f["name"],
                    "steps": steps,
                    "weight": rng.lognormvariate(0, 0.5),
                    "opt_scale": rng.uniform(0.5, 1.3),
                    # per-step continuation probability; one deliberately weak step per funnel gives the ranking something to find
                    "cont": [rng.uniform(0.45, 0.85) for _ in range(max(0, n - 1))],
                }
            )
            if n > 1:
                self.funnels[-1]["cont"][rng.randrange(n - 1)] = rng.uniform(0.12, 0.25)
        # the funnel that gets a planted traffic collapse in the last days: one created here from a template, so the app's
        # own seed traffic does not mask it
        fresh = [f for f in self.funnels if f["name"] in ("Newsletter signup", "Book a call", "Product launch")]
        if fresh:
            fresh[0]["incident"] = True
            fresh[0]["weight"] = max(fresh[0]["weight"], 1.6)
        if not any(s["forms"] for f in self.funnels for s in f["steps"]) or not any(
            s["checkouts"] for f in self.funnels for s in f["steps"]
        ):
            raise SystemExit("the Funnels demo workspace has no live funnel with a form and a checkout")

    def sales_funnels(self) -> list[dict[str, Any]]:
        return [f for f in self.funnels if any(_main_checkout(s) for s in f["steps"])]

    # -- the calls a visitor's browser makes --------------------------------------------------------------
    def _post(self, label: str, path: str, body: dict[str, Any], device: str) -> httpx.Response:
        r = send(self.http, "POST", f"{self.base}{path}", json=body, headers={"user-agent": UA[device]})
        self.stats.note(label, r)
        return r

    def page_view(self, funnel: dict, step: int, visitor: str, device: str, src: tuple, t: float) -> None:
        self.clock.at(t)
        body: dict[str, Any] = {
            "funnelId": funnel["id"],
            "stepId": funnel["steps"][step]["id"],
            "type": "page_view",
            "visitorId": visitor,
        }
        utms = _utms(src)
        if utms:
            body["utms"] = utms
        if src[0] in REFERRERS:
            body["referrer"] = REFERRERS[src[0]]
        self._post("funnels.page_view", "/api/public/events", body, device)

    def optin(self, p: Person, f: dict, si: int, t: float) -> None:
        self.clock.at(t)
        step = f["steps"][si]
        block = step["forms"][0]
        fields = {}
        for fld in block["fields"]:
            if fld["type"] == "email":
                fields[fld["id"]] = p.email
            elif fld["type"] == "name":
                fields[fld["id"]] = p.name
            elif fld["type"] == "phone":
                fields[fld["id"]] = "+14155550" + f"{100 + p.idx % 900}"
            elif fld.get("options"):
                opt = fld["options"][p.idx % len(fld["options"])]
                fields[fld["id"]] = opt.get("value", opt.get("label")) if isinstance(opt, dict) else opt
            elif fld.get("required"):
                fields[fld["id"]] = "n/a"
        body = {
            "fields": fields,
            "consent": True,
            "visitorId": p.visitor,
            "submissionId": f"s{p.idx:06d}x{self.stats.calls['funnels.optin']:06d}",
            "utms": _utms(p.source) or {},
        }
        r = self._post("funnels.optin", f"/api/public/forms/{f['id']}/{step['id']}/{block['id']}", body, p.device)
        if r.status_code == 200:
            p.in_funnels = True
            p.opted_in_at = p.opted_in_at or t

    def purchase(self, p: Person, f: dict, si: int, t: float, bump: bool, upsell: bool) -> None:
        self.clock.at(t)
        step = f["steps"][si]
        main = _main_checkout(step)
        if main is None:
            return
        bumps = [c["id"] for c in step["checkouts"] if c.get("orderBump")] if bump else []
        body = {
            "email": p.email,
            "name": p.name,
            "bumpBlockIds": bumps,
            "visitorId": p.visitor,
            "submissionId": f"c{p.idx:06d}x{self.stats.calls['funnels.checkout']:06d}",
            "utms": _utms(p.source) or {},
        }
        r = self._post("funnels.checkout", f"/api/public/checkout/{f['id']}/{step['id']}/{main['id']}", body, p.device)
        if r.status_code != 200 or not r.json().get("orderId"):
            return
        p.in_funnels = True
        p.bought = True
        if not upsell or si + 1 >= len(f["steps"]):
            return
        up_step = f["steps"][si + 1]
        up = next((c for c in up_step["checkouts"] if c.get("oneClickUpsell")), None)
        if not up:
            return
        self.page_view(f, si + 1, p.visitor, p.device, p.source, t + 40)
        self.clock.at(t + 55)
        self._post(
            "funnels.upsell",
            f"/api/public/checkout/{f['id']}/{up_step['id']}/{up['id']}",
            {
                "previousOrderId": r.json()["orderId"],
                "visitorId": p.visitor,
                "submissionId": f"u{p.idx:06d}x{self.stats.calls['funnels.upsell']:06d}",
            },
            p.device,
        )


def _main_checkout(step: dict[str, Any]) -> dict[str, Any] | None:
    return next((c for c in step["checkouts"] if not c.get("orderBump") and not c.get("oneClickUpsell")), None)


def _utms(src: tuple) -> dict[str, str] | None:
    if src[0] is None:
        return None
    return {"source": src[0], "medium": src[1], "campaign": src[2]}


def build_funnels_schedule(
    fun: Funnels, people: list[Person], rng: random.Random, days: int, scale: float, now_s: float
) -> list[tuple[float, Callable[[], None]]]:
    sched: list[tuple[float, Callable[[], None]]] = []
    today = datetime.fromtimestamp(now_s, UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    pool = list(people)
    rng.shuffle(pool)
    cursor = 0
    leads: list[Person] = []  # people with a planned opt-in
    weights = [f["weight"] for f in fun.funnels]

    def take() -> Person:
        nonlocal cursor
        p = pool[cursor % len(pool)]
        cursor += 1
        return p

    def plan_session(f: dict, t: float, p: Person | None, src: tuple) -> None:
        visitor = p.visitor if p else f"v{rng.getrandbits(48):012x}"
        device = p.device if p else rng.choices(["desktop", "mobile", "tablet"], [0.55, 0.38, 0.07])[0]
        i, tt = 0, t
        n = len(f["steps"])
        while i < n:
            step = f["steps"][i]
            sched.append((tt, lambda ff=f, ii=i, v=visitor, dv=device, sr=src, x=tt: fun.page_view(ff, ii, v, dv, sr, x)))
            converted = False
            if (
                p
                and step["forms"]
                and p.opted_in_at is None
                and rng.random() < src[4] * f["opt_scale"] * (1.1 if device == "desktop" else 0.9)
            ):
                tt += rng.uniform(25, 150)
                sched.append((tt, lambda pp=p, ff=f, ii=i, x=tt: fun.optin(pp, ff, ii, x)))
                p.opted_in_at = tt  # planned (the real flag is set when the call succeeds)
                leads.append(p)
                converted = True
            elif p and _main_checkout(step) and not p.bought and rng.random() < 0.05 * src[5] / 0.15:
                tt += rng.uniform(40, 200)
                sched.append(
                    (
                        tt,
                        lambda pp=p, ff=f, ii=i, x=tt, b=rng.random() < 0.3, u=rng.random() < 0.3: fun.purchase(
                            pp, ff, ii, x, b, u
                        ),
                    )
                )
                p.bought = True
                converted = True
            if i + 1 < n and ((converted and rng.random() < 0.95) or rng.random() < f["cont"][i]):
                i += 1
                tt += rng.uniform(5, 60)
            else:
                break

    for d in range(days, -1, -1):
        ds = today - d * 86400
        wd = datetime.fromtimestamp(ds, UTC).weekday()
        factor = (0.8 if wd >= 5 else 1.0) * (1 + 0.01 * (days - d))
        incident = d <= INCIDENT_DAYS  # planted anomaly: one funnel's traffic collapses for the last days
        n_sessions = max(10, int(rng.gauss(110, 14) * factor * scale))
        for _ in range(n_sessions):
            t = min(day_hour_time(ds, rng), now_s - 120)
            f = rng.choices(fun.funnels, weights)[0]
            if incident and f.get("incident") and rng.random() < 0.88:
                continue
            if rng.random() < 0.5:
                p = take()
                src = p.source
            else:
                p, src = None, rng.choices(SOURCES, [s[3] for s in SOURCES])[0]
            plan_session(f, t, p, src)
        # returning leads: a visit to a funnel with a checkout, later; the buying hazard follows the latent propensity
        for p in list(leads):
            if p.bought or p.opted_in_at is None or p.opted_in_at > ds + 86400 - 3600 * 6 or (ds - p.opted_in_at) / 86400 < 0.2:
                continue
            if rng.random() < p.p_buy * 0.12:
                t = min(ds + rng.uniform(8, 22) * 3600, now_s - 120)
                if t <= p.opted_in_at + 300:
                    continue
                f = rng.choice(fun.sales_funnels())
                si = next(i for i, s in enumerate(f["steps"]) if _main_checkout(s))
                for i in range(si + 1):
                    sched.append(
                        (
                            t + i * 20,
                            lambda ff=f, ii=i, pp=p, tt=t + i * 20: fun.page_view(ff, ii, pp.visitor, pp.device, pp.source, tt),
                        )
                    )
                sched.append(
                    (
                        t + si * 20 + rng.uniform(40, 200),
                        lambda pp=p, ff=f, ii=si, tt=t + si * 20 + 120, b=rng.random() < 0.3, u=rng.random() < 0.25: fun.purchase(
                            pp, ff, ii, tt, b, u
                        ),
                    )
                )
                p.bought = True  # planned
    for p in people:  # the executed calls set the real flags
        p.opted_in_at = None
        p.bought = False
    return sched


# ------------------------------------------------------------------------------------------------------------
MESSAGES = {
    "complaint": [
        "This is unacceptable, my order never arrived and I am furious!!!",
        "Terrible service. I want a refund, the product is broken",
        "Worst experience ever, I am really disappointed with the delivery",
    ],
    "urgent": [
        "URGENT: the checkout is down and we cannot take orders right now",
        "Our production site shows an error, please fix this asap",
        "Critical issue, cannot log in at all, need help immediately",
    ],
    "support": [
        "I need help, the download link is not working",
        "There is an issue with my account, I cannot reset my password",
        "The app crashed when I tried to open the course",
    ],
    "purchase": [
        "What is the price of the premium plan?",
        "I would like to upgrade, can I get a quote for 5 seats?",
        "Do you offer a demo before I buy?",
    ],
    "question": ["How long does shipping take to Canada?", "What are your opening hours?", "Where can I find my invoice?"],
    "thanks": ["Thanks, great product, I love it", "Thank you so much, that was perfect and the team was awesome"],
}
AGENT_REPLIES = [
    "Hi, thanks for reaching out. I am looking into this now.",
    "Thanks for your patience, here is what I found.",
    "I have fixed that on our side, could you try again?",
    "Happy to help with that, I have sent the details by email.",
]
KINDS = [("complaint", 0.12), ("urgent", 0.07), ("support", 0.28), ("purchase", 0.18), ("question", 0.25), ("thanks", 0.10)]


class Conversations:
    def __init__(self, base: str, clock: Clock, stats: Stats) -> None:
        self.base = base.rstrip("/")
        self.clock = clock
        self.stats = stats
        self.http = httpx.Client(timeout=30)
        self.token = ""
        self.members: list[dict[str, Any]] = []
        self.member_tokens: dict[str, dict[str, str]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.rooms: dict[str, str] = {}
        self.logged_in_at = 0.0
        self.session_at: dict[str, float] = {}

    def login(self, email: str) -> dict[str, str]:
        r = send(self.http, "POST", f"{self.base}/api/auth/login", json={"email": email, "password": DEMO_PASSWORD})
        r.raise_for_status()
        return {"authorization": f"Bearer {r.json()['token']}"}

    def _fresh_tokens(self, t: float) -> None:
        """Sessions last 30 days of the (shifted) clock: log everybody in again every 10 days."""
        if t - self.logged_in_at < 10 * 86400:
            return
        self.clock.at(t)
        self.owner = self.login(DEMO_EMAIL)
        for m in self.members:
            try:
                self.member_tokens[m["id"]] = self.login(m["email"])
            except httpx.HTTPError:
                continue
        self.logged_in_at = t

    def setup(self, t0: float) -> None:
        self.logged_in_at = t0
        self.owner = self.login(DEMO_EMAIL)
        self.members = self.http.get(f"{self.base}/api/members", headers=self.owner).json()["data"]
        self.owner_id = next(m["id"] for m in self.members if m["email"] == DEMO_EMAIL)
        for m in self.members:
            try:
                self.member_tokens[m["id"]] = self.login(m["email"])
            except httpx.HTTPError:
                continue
        # the public widget needs an embed token bound to a domain
        r = self.http.post(f"{self.base}/api/tokens", headers=self.owner, json={"domain": "shop.example.test"})
        r.raise_for_status()
        self.embed = r.json()["token"]
        # make the apps' own AI triage (offline heuristic provider) fill ai_intent / ai_sentiment / ai_urgency
        r = self.http.patch(f"{self.base}/api/workspace", headers=self.owner, json={"settings": {"ai_triage_enabled": True}})
        self.stats.note("conv.enable_triage", r)
        self.inboxes = [i["id"] for i in self.http.get(f"{self.base}/api/inboxes", headers=self.owner).json()["data"]]

    def _wh(self, p: Person, session: str | None = None) -> dict[str, str]:
        h = {
            "x-api-key": self.embed,
            "origin": "https://shop.example.test",
            "x-device-id": "d" + hashlib.sha256(p.canon.encode()).hexdigest()[:20],
            "user-agent": UA[p.device],
        }
        if session:
            h["x-support-session"] = session
        return h

    def visitor_message(self, p: Person, room_key: str, text: str, t: float, new_room: bool) -> None:
        self.clock.at(t)
        if p.canon in self.sessions and t - self.session_at.get(p.canon, 0) > 20 * 86400:
            del self.sessions[p.canon]
        if p.canon not in self.sessions:
            r = send(
                self.http, "POST", f"{self.base}/public/sessions", headers=self._wh(p), json={"name": p.name, "email": p.email}
            )
            self.stats.note("conv.session", r)
            if r.status_code >= 400:
                return
            self.sessions[p.canon] = r.json()
            self.session_at[p.canon] = t
            p.in_conv = True
        sess = self.sessions[p.canon]["session_token"]
        if new_room:
            r = send(self.http, "POST", f"{self.base}/public/rooms", headers=self._wh(p, sess), json={})
            self.stats.note("conv.room", r)
            if r.status_code >= 400:
                return
            self.rooms[room_key] = (r.json().get("room") or r.json())["id"]
            self.clock.at(t + 4)
        rid = self.rooms.get(room_key)
        if not rid:
            return
        r = send(
            self.http,
            "POST",
            f"{self.base}/public/rooms/{rid}/messages",
            headers=self._wh(p, sess),
            json={"content": text, "client_msg_id": f"cm{hashlib.sha256((room_key + text + str(t)).encode()).hexdigest()[:20]}"},
        )
        self.stats.note("conv.visitor_msg", r)

    def agent(
        self, member_id: str, method: str, path: str, t: float, body: dict[str, Any] | None = None, label: str = "agent"
    ) -> httpx.Response | None:
        self._fresh_tokens(t)
        self.clock.at(t)
        h = self.member_tokens.get(member_id) or self.owner
        r = send(self.http, method, f"{self.base}{path}", headers=h, json=body)
        self.stats.note(label, r)
        return r

    def csat(self, p: Person, room_key: str, score: int, t: float) -> None:
        self.clock.at(t)
        rid, sess = self.rooms.get(room_key), self.sessions.get(p.canon)
        if rid and sess:
            r = send(
                self.http,
                "POST",
                f"{self.base}/public/rooms/{rid}/csat",
                headers=self._wh(p, sess["session_token"]),
                json={"score": score},
            )
            self.stats.note("conv.csat", r)


def build_conversations_schedule(
    conv: Conversations, people: list[Person], rng: random.Random, days: int, scale: float, now_s: float
) -> list[tuple[float, Callable[[], None]]]:
    sched: list[tuple[float, Callable[[], None]]] = []
    today = datetime.fromtimestamp(now_s, UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    funnel_people = [p for p in people if p.in_funnels]
    conv_only = [p for p in people if not p.in_funnels]
    mids = [m["id"] for m in conv.members if m.get("role") != "owner"] or [m["id"] for m in conv.members]
    room_n = 0
    for d in range(days, -1, -1):
        ds = today - d * 86400
        wd = datetime.fromtimestamp(ds, UTC).weekday()
        n = max(4, int(rng.gauss(24, 4) * (0.6 if wd >= 5 else 1.0) * scale))
        backlog_incident = d <= 2  # planted anomaly: slow replies in the last days -> backlog grows
        for _ in range(n):
            # 65% of the support traffic comes from people who are also funnel leads / customers
            p = rng.choice(funnel_people) if funnel_people and rng.random() < 0.65 else rng.choice(conv_only or people)
            t0 = min(day_hour_time(ds, rng), now_s - 600)
            kind = rng.choices([k for k, _ in KINDS], [w for _, w in KINDS])[0]
            room_n += 1
            key = f"r{room_n}"
            text = rng.choice(MESSAGES[kind])
            sched.append((t0, lambda pp=p, k=key, tx=text, tt=t0: conv.visitor_message(pp, k, tx, tt, True)))
            if rng.random() < 0.45:
                t1 = t0 + rng.uniform(20, 300)
                sched.append(
                    (
                        t1,
                        lambda pp=p, k=key, tx=rng.choice(MESSAGES["question"]), tt=t1: conv.visitor_message(
                            pp, k, tx, tt, False
                        ),
                    )
                )
            if rng.random() < 0.05:
                continue  # never touched by an agent: stays open and unassigned
            mid = rng.choice(mids)
            ta = t0 + rng.uniform(30, 600)
            urgent = kind in ("urgent", "complaint")
            sched.append((ta, lambda m=mid, k=key, tt=ta: _assign(conv, m, k, tt)))
            if urgent and rng.random() < 0.7:
                sched.append((ta + 5, lambda m=mid, k=key, tt=ta + 5: _prio(conv, m, k, tt)))
            fr_min = rng.lognormvariate(math.log(14 if urgent else 38), 0.9) * (3.0 if backlog_incident else 1.0)
            if rng.random() < 0.08:
                fr_min += rng.uniform(300, 1200)  # forgotten for hours
            tr = max(t0 + max(60, fr_min * 60), ta + 30)
            via = rng.choices(["dm", "email"], [0.88, 0.12])[0]
            sched.append((tr, lambda m=mid, k=key, tt=tr, v=via, pp=p: _reply(conv, m, k, tt, v, pp)))
            if rng.random() < 0.55:
                t2 = tr + rng.uniform(120, 1800)
                sched.append(
                    (
                        t2,
                        lambda pp=p, k=key, tx=rng.choice(MESSAGES["thanks"] + MESSAGES["support"]), tt=t2: conv.visitor_message(
                            pp, k, tx, tt, False
                        ),
                    )
                )
                t3 = t2 + rng.lognormvariate(math.log(12 * 60), 0.7)
                sched.append((t3, lambda m=mid, k=key, tt=t3, pp=p: _reply(conv, m, k, tt, "dm", pp)))
                tr = t3
            if rng.random() < 0.05:
                ts = tr + 60
                sched.append((ts, lambda m=mid, k=key, tt=ts: _snooze(conv, m, k, tt, tt + 1800)))
            if rng.random() < 0.82:
                tc = tr + rng.lognormvariate(math.log(25 * 60), 0.8)
                sched.append((tc, lambda m=mid, k=key, tt=tc: _close(conv, m, k, tt)))
                if rng.random() < 0.35:
                    score = rng.choices([1, 2, 3, 4, 5], [0.06, 0.07, 0.15, 0.32, 0.40])[0]
                    sched.append((tc + 90, lambda pp=p, k=key, s=score, tt=tc + 90: conv.csat(pp, k, s, tt)))
                if rng.random() < 0.07:
                    tre = tc + rng.uniform(3600, 6 * 3600)
                    sched.append(
                        (
                            tre,
                            lambda pp=p, k=key, tt=tre: conv.visitor_message(
                                pp, k, "Sorry, it is happening again, can you check?", tt, False
                            ),
                        )
                    )
                    tcl = tre + rng.uniform(600, 7200)
                    sched.append((tcl, lambda m=mid, k=key, tt=tcl: _close(conv, m, k, tt)))
    return sched


def _assign(conv: Conversations, mid: str, key: str, t: float) -> None:
    if rid := conv.rooms.get(key):
        conv.agent(conv.owner_id, "POST", f"/api/rooms/{rid}/assign", t, {"member_ids": [mid]}, "agent.assign")


def _prio(conv: Conversations, mid: str, key: str, t: float) -> None:
    if rid := conv.rooms.get(key):
        conv.agent(conv.owner_id, "PATCH", f"/api/rooms/{rid}", t, {"priority": 1}, "agent.priority")


def _reply(conv: Conversations, mid: str, key: str, t: float, via: str, p: Person) -> None:
    rid = conv.rooms.get(key)
    if not rid:
        return
    body: dict[str, Any] = {
        "kind": "text",
        "source": "dm",
        "content": random.choice(AGENT_REPLIES),
        "client_msg_id": f"am{hashlib.sha256((key + str(t)).encode()).hexdigest()[:20]}",
    }
    if via == "email":
        body.update({"source": "email", "subject": "Re: your question"})
    conv.agent(mid, "POST", f"/api/rooms/{rid}/messages", t, body, f"agent.reply.{via}")


def _close(conv: Conversations, mid: str, key: str, t: float) -> None:
    if rid := conv.rooms.get(key):
        conv.agent(mid, "PATCH", f"/api/rooms/{rid}", t, {"status": "closed"}, "agent.close")


def _snooze(conv: Conversations, mid: str, key: str, t: float, until: float) -> None:
    if rid := conv.rooms.get(key):
        iso = datetime.fromtimestamp(until, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        conv.agent(mid, "POST", f"/api/rooms/{rid}/snooze", t, {"until": iso}, "agent.snooze")


# ------------------------------------------------------------------------------------------------------------
def run(sched: list[tuple[float, Callable[[], None]]], label: str) -> None:
    sched.sort(key=lambda x: x[0])
    started = time.time()
    for i, (_, fn) in enumerate(sched):
        fn()
        if i and i % 1000 == 0:
            print(f"  {label}: {i}/{len(sched)} steps ({time.time() - started:.0f}s)", flush=True)
    print(f"  {label}: {len(sched)} steps in {time.time() - started:.0f}s", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--funnels-url", required=True)
    ap.add_argument("--conversations-url", required=True)
    ap.add_argument("--funnels-clock", help="file read by fakeclock.mjs in the Funnels process")
    ap.add_argument("--conversations-clock", help="file read by fakeclock.mjs in the Conversations process")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--scale", type=float, default=1.0, help="traffic multiplier")
    ap.add_argument("--people", type=int, default=900)
    ap.add_argument("--seed", type=int, default=20261020)
    ap.add_argument("--out", help="write a JSON summary (people and counts) here")
    a = ap.parse_args()

    rng = random.Random(a.seed)
    random.seed(a.seed)
    stats = Stats()
    now_s = time.time()
    people = make_people(a.people, rng)
    fclock, cclock = Clock(a.funnels_clock), Clock(a.conversations_clock)
    # Move each app's clock to the start of the window BEFORE the first request: rate-limiter state created at real
    # time would otherwise be "in the future" once the clock jumps back and block everything.
    start_s = (
        datetime.fromtimestamp(now_s, UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() - (a.days + 1) * 86400
    )
    try:
        fclock.at(start_s)
        print("funnels: discovering funnels", flush=True)
        fun = Funnels(a.funnels_url, fclock, stats)
        fun.prepare(rng)
        run(build_funnels_schedule(fun, people, rng, a.days, a.scale, now_s), "funnels")
    finally:
        fclock.reset()
    try:
        cclock.at(start_s)
        print("conversations: setup", flush=True)
        conv = Conversations(a.conversations_url, cclock, stats)
        conv.setup(start_s)
        # re-seed ordering: people who really ended up in Funnels come first in the support pool
        run(build_conversations_schedule(conv, people, rng, a.days, a.scale, now_s), "conversations")
    finally:
        cclock.reset()

    summary = {
        "people": a.people,
        "in_funnels": sum(p.in_funnels for p in people),
        "in_conversations": sum(p.in_conv for p in people),
        "in_both": sum(p.in_funnels and p.in_conv for p in people),
        "buyers": sum(p.bought for p in people),
        "calls": dict(stats.calls),
        "errors": dict(stats.errors),
        "incident": next(
            ({"funnel_id": f["id"], "funnel_name": f["name"], "days": INCIDENT_DAYS} for f in fun.funnels if f.get("incident")),
            None,
        ),
        "error_samples": stats.samples,
        "contacts": [
            {"email": p.email, "funnels": p.in_funnels, "conversations": p.in_conv, "bought": p.bought}
            for p in people
            if p.in_funnels or p.in_conv
        ],
    }
    print(json.dumps({k: v for k, v in summary.items() if k != "contacts"}, indent=2))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(summary, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
