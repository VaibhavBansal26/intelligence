#!/usr/bin/env python3
"""Assertions for the end-to-end run: real Funnels + real Conversations + the Intelligence service.

Called by run.sh once all three are up (apps on the real clock with their DEFAULT rate limits). Exit code 1 when any
hard check fails. Findings that belong to the Node apps are collected as "deviations" and written to
`<workdir>/verify-report.json` (and printed); they do not fail the run, because this repo cannot fix them.

Never touches the apps' databases: keys, peppers and extra traffic are created through their HTTP APIs. The
Intelligence service's own SQLite file is opened read-only to compare landed rows with the apps' exports.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
REPOS = ROOT.parent
DEMO_EMAIL = "demo@example.test"
DEMO_PASSWORD = "demo-password-123"  # documented seed credentials of the apps (test data)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.deviations: list[dict[str, Any]] = []
        self.facts: dict[str, Any] = {}

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""), flush=True)
        if ok and detail:
            print(f"         {detail}", flush=True)
        return bool(ok)

    def deviation(self, owner: str, endpoint: str, summary: str, detail: str = "") -> None:
        if any(d["owner"] == owner and d["endpoint"] == endpoint and d["summary"] == summary for d in self.deviations):
            return
        self.deviations.append({"owner": owner, "endpoint": endpoint, "summary": summary, "detail": detail})
        print(f"  [DEVIATION:{owner}] {endpoint}: {summary}", flush=True)

    def fact(self, key: str, value: Any) -> None:
        self.facts[key] = value
        print(f"  [fact] {key} = {json.dumps(value, default=str)[:240]}", flush=True)


R = Report()


def section(title: str) -> None:
    print(f"\n## {title}", flush=True)


def schema_validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(json.loads(path.read_text()), format_checker=FormatChecker())


def validation_errors(v: Draft202012Validator, instance: Any, limit: int = 3) -> list[str]:
    out: list[str] = []
    for e in v.iter_errors(instance):
        out.append(f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message[:140]}")
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------------------------------------------------------------
class App:
    """A real Node app: session login (for provisioning) and API-key calls."""

    def __init__(self, name: str, base: str) -> None:
        self.name = name
        self.base = base.rstrip("/")
        self.http = httpx.Client(timeout=120)
        self.session: dict[str, str] = {}

    def login(self, email: str = DEMO_EMAIL) -> dict[str, str]:
        r = self.http.post(f"{self.base}/api/auth/login", json={"email": email, "password": DEMO_PASSWORD})
        r.raise_for_status()
        return {"authorization": f"Bearer {r.json()['token']}"}

    def api(self, key: str, path: str, params: dict[str, Any] | None = None, retry429: bool = True, **kw: Any) -> httpx.Response:
        """GET an analytics endpoint; a 429 is waited out (Retry-After) unless the caller is testing the limiter."""
        for _ in range(8):
            r = self.http.get(
                f"{self.base}/api/v1/analytics{path}",
                params=params,
                headers={"authorization": f"Bearer {key}", **kw.get("headers", {})},
            )
            if r.status_code != 429 or not retry429:
                return r
            time.sleep(min(float(r.headers.get("retry-after", "5")), 70) + 0.3)
        return r

    def export_all(
        self, key: str, path: str, limit: int, params: dict[str, Any] | None = None, max_pages: int = 10_000
    ) -> tuple[list[dict[str, Any]], int, list[httpx.Response]]:
        """Follow X-Next-Cursor to the end (or `max_pages`), waiting for the rate limiter. Returns rows, pages, responses."""
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        resps: list[httpx.Response] = []
        pages = 0
        while True:
            q = {**(params or {}), "limit": limit}
            if cursor:
                q["cursor"] = cursor
            r = self.api(key, path, q)
            r.raise_for_status()
            resps.append(r)
            pages += 1
            rows.extend(json.loads(line) for line in r.text.splitlines() if line.strip())
            cursor = r.headers.get("x-next-cursor")
            if not cursor or pages >= max_pages:
                return rows, pages, resps


def lead_emails(fun: App, headers: dict[str, str]) -> list[str]:
    """Ground truth: the leads' e-mail addresses as the Funnels app itself lists them (session API)."""
    out: list[str] = []
    page = 1
    while True:
        r = fun.http.get(f"{fun.base}/api/leads", params={"limit": 100, "page": page}, headers=headers)
        r.raise_for_status()
        body = r.json()
        out += [x["email"] for x in body["data"] if x.get("email")]
        pg = body.get("pagination") or {}
        if page >= int(pg.get("totalPages", 1)) or not body["data"]:
            return out
        page += 1


def contact_emails(conv: App, headers: dict[str, str]) -> list[str]:
    out: list[str] = []
    cursor: str | None = None
    while True:
        r = conv.http.get(
            f"{conv.base}/api/contacts", params={"limit": 100, **({"cursor": cursor} if cursor else {})}, headers=headers
        )
        r.raise_for_status()
        body = r.json()
        out += [x["email"] for x in body["data"] if x.get("email")]
        cursor = body.get("next_cursor") or (body.get("pagination") or {}).get("next_cursor")
        if not cursor:
            return out


def key_hash(pepper: str, email: str) -> str:
    return hmac.new(pepper.encode(), email.strip().lower().encode(), hashlib.sha256).hexdigest()


BACKFILL_DAYS = 180  # Settings.backfill_days default: the window the first sync reads


def backfill_from() -> str:
    return datetime.fromtimestamp(time.time() - BACKFILL_DAYS * 86400, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(v: str) -> float:
    return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC).timestamp()


# ----------------------------------------------------------------------------------------------------------------
def provision(fun: App, conv: App, rep: Report) -> dict[str, Any]:
    section("provision keys and peppers through the apps' own APIs")
    out: dict[str, Any] = {}
    fh, ch = fun.login(), conv.login()
    out["fh"], out["ch"] = fh, ch

    def fkey(name: str, scopes: list[str], headers: dict[str, str], **extra: Any) -> dict[str, Any]:
        r = fun.http.post(f"{fun.base}/api/v1/api-keys", headers=headers, json={"name": name, "scopes": scopes, **extra})
        r.raise_for_status()
        return r.json()

    def ckey(name: str, scopes: list[str], headers: dict[str, str], **extra: Any) -> dict[str, Any]:
        r = conv.http.post(f"{conv.base}/api/v1/api-keys", headers=headers, json={"name": name, "scopes": scopes, **extra})
        r.raise_for_status()
        return r.json()

    both = ["analytics:read", "analytics:export"]
    out["f_key"] = fkey("intelligence", both, fh)["key"]
    out["c_key"] = ckey("intelligence", both, ch)["key"]
    out["f_probe"], out["c_probe"] = fkey("probe", both, fh)["key"], ckey("probe", both, ch)["key"]
    out["f_bridge"], out["c_bridge"] = fkey("bridge", both, fh)["key"], ckey("bridge", both, ch)["key"]
    out["c_mixed1"], out["c_mixed2"] = ckey("mixed-1", both, ch)["key"], ckey("mixed-2", both, ch)["key"]
    out["f_rl"], out["c_rl"] = fkey("rate-limited", both, fh)["key"], ckey("rate-limited", both, ch)["key"]
    out["f_read"] = fkey("read-only", ["analytics:read"], fh)["key"]
    out["c_read"] = ckey("read-only", ["analytics:read"], ch)["key"]
    rev_f, rev_c = fkey("to-revoke", both, fh), ckey("to-revoke", both, ch)
    fun.http.delete(f"{fun.base}/api/v1/api-keys/{rev_f['apiKey']['id']}", headers=fh).raise_for_status()
    conv.http.delete(f"{conv.base}/api/v1/api-keys/{rev_c['id']}", headers=ch).raise_for_status()
    out["f_revoked"], out["c_revoked"] = rev_f["key"], rev_c["key"]
    soon = datetime.fromtimestamp(time.time() + 3, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    out["f_expiring"] = fkey("expiring", both, fh, expiresAt=soon)["key"]
    out["c_expiring"] = ckey("expiring", both, ch, expires_at=soon)["key"]
    out["expiry_set_at"] = time.time()
    # identity peppers: session-authenticated endpoints (API keys can not read them)
    r = fun.http.get(f"{fun.base}/api/v1/analytics/identity-pepper", headers=fh)
    out["f_pepper"] = r.json()["pepper"]
    r = conv.http.get(f"{conv.base}/api/analytics/identity-pepper", headers=ch)
    out["c_pepper"] = r.json()["pepper"]
    pep_errs = validation_errors(
        schema_validator(REPOS / "conversations" / "docs" / "analytics-api" / "identity-pepper.schema.json"), r.json()
    )
    rep.check("conversations: identity-pepper response satisfies its published schema", not pep_errs, "; ".join(pep_errs))
    rep.check("both apps issued API keys and an identity pepper", all(out[k] for k in ("f_key", "c_key", "f_pepper", "c_pepper")))

    # a second workspace in each app: other tenant (wrong-workspace key) and an EMPTY workspace
    def register(app: App, body: dict[str, Any]) -> dict[str, str]:
        r = app.http.post(f"{app.base}/api/auth/register", json=body)
        if r.status_code >= 400:  # re-run against a kept workdir: the second workspace already exists
            r = app.http.post(f"{app.base}/api/auth/login", json={"email": body["email"], "password": body["password"]})
        r.raise_for_status()
        return {"authorization": f"Bearer {r.json()['token']}"}

    fh2 = register(fun, {"email": "other-tenant@example.test", "password": "correct horse battery", "name": "Other"})
    ch2 = register(
        conv,
        {
            "email": "other-tenant@example.test",
            "password": "correct horse battery",
            "name": "Other",
            "workspace_name": "Other Co",
        },
    )
    out["f_other"] = fkey("other", both, fh2)["key"]
    out["c_other"] = ckey("other", both, ch2)["key"]
    out["f_other_pepper"] = fun.http.get(f"{fun.base}/api/v1/analytics/identity-pepper", headers=fh2).json()["pepper"]
    out["c_other_pepper"] = conv.http.get(f"{conv.base}/api/analytics/identity-pepper", headers=ch2).json()["pepper"]
    # a Conversations key that may read the pepper over the API
    r = conv.http.post(
        f"{conv.base}/api/v1/api-keys",
        headers=ch,
        json={"name": "identity", "scopes": ["analytics:export", "analytics_identity:manage"]},
    )
    out["c_identity_key"] = r.json().get("key") if r.status_code < 300 else None
    rep.fact("peppers_differ_between_apps", out["f_pepper"] != out["c_pepper"])
    return out


# ----------------------------------------------------------------------------------------------------------------
def contract_probe(fun: App, conv: App, keys: dict[str, Any], rep: Report, record: dict[str, Any] | None) -> dict[str, Any]:
    section("contract conformance of the real responses (app schemas + intelligence contracts/ schemas)")
    fs = REPOS / "funnels" / "docs" / "analytics-api"
    cs = REPOS / "conversations" / "docs" / "analytics-api"
    ic = ROOT / "contracts"
    fk, ck = keys["f_probe"], keys["c_probe"]
    truth: dict[str, Any] = {}

    def validate(owner: str, endpoint: str, instance: Any, schemas: list[tuple[str, Path]]) -> None:
        for label, path in schemas:
            if not path.exists():
                rep.deviation("docs", endpoint, f"schema file missing: {label}", str(path))
                continue
            errs = validation_errors(schema_validator(path), instance)
            if errs:
                rep.deviation(
                    owner if label == "own" else "contract-doc",
                    endpoint,
                    f"fails the {label} schema {path.name}",
                    "; ".join(errs),
                )
        return None

    # -- funnels ------------------------------------------------------------------------------------------
    r = fun.api(fk, "/funnels", {"limit": 2})
    page1 = r.json()
    rep.check(
        "funnels: list is {data,next_cursor} and paginates with an opaque cursor",
        isinstance(page1, dict) and "data" in page1 and (page1["next_cursor"] is not None),
    )
    allf: list[dict[str, Any]] = []
    cur = None
    while True:
        q = {"limit": 2, **({"cursor": cur} if cur else {})}
        pg = fun.api(fk, "/funnels", q).json()
        allf += pg["data"]
        cur = pg.get("next_cursor")
        if not cur:
            break
    truth["funnels"] = allf
    rep.check(
        "funnels: paging with limit=2 returns every funnel once",
        len({f["id"] for f in allf}) == len(allf) and len(allf) == len(fun.api(fk, "/funnels").json()["data"]),
        f"{len(allf)} funnels",
    )
    validate("funnels", "GET /funnels", {"data": allf, "next_cursor": None}, [("own", fs / "funnels.schema.json")])
    for f in allf:
        if not validate_funnel_item(ic, f):
            rep.deviation(
                "contract-doc", "GET /funnels item", "does not satisfy contracts/funnel.schema.json", json.dumps(f)[:200]
            )
            break
    steps_by_funnel: dict[str, Any] = {}
    for f in allf:
        steps = fun.api(fk, f"/funnels/{f['id']}/steps").json()
        steps_by_funnel[f["id"]] = steps
        validate("funnels", "GET /funnels/{id}/steps", steps, [("own", fs / "steps.schema.json")])
        rows = steps["data"] if isinstance(steps, dict) else steps
        for srow in rows:
            errs = validation_errors(schema_validator(ic / "funnel_step.schema.json"), srow)
            if errs:
                rep.deviation(
                    "contract-doc", "GET /funnels/{id}/steps item", "fails contracts/funnel_step.schema.json", "; ".join(errs)
                )
                break
    truth["steps"] = steps_by_funnel
    fid = allf[0]["id"]
    for metric in ("visits", "unique_visits", "optins", "orders", "gross"):
        for interval in ("hour", "day", "week", "month"):
            r = fun.api(
                fk,
                f"/funnels/{fid}/timeseries",
                {"metric": metric, "interval": interval, "from": "2026-10-10", "to": "2026-10-12"},
            )
            if r.status_code != 200:
                rep.check(f"funnels: timeseries {metric}/{interval} answers 200", False, f"HTTP {r.status_code} {r.text[:100]}")
                continue
            ts = r.json()
            validate(
                "funnels",
                "GET /funnels/{id}/timeseries",
                ts,
                [("own", fs / "timeseries.schema.json"), ("intelligence", ic / "timeseries.schema.json")],
            )
    ts = fun.api(
        fk,
        f"/funnels/{fid}/timeseries",
        {"metric": "visits", "interval": "day", "tz": "America/New_York", "from": "2026-10-01", "to": "2026-10-03"},
    ).json()
    rep.check(
        "funnels: tz only buckets (day starts 04:00Z/05:00Z in New York)",
        ts["points"][0]["t"].endswith(("T04:00:00.000Z", "T05:00:00.000Z")),
        ts["points"][0]["t"],
    )
    sch = fun.api(fk, "/schema").json()
    rep.check(
        "funnels: GET /schema returns integer schema_version",
        isinstance(sch.get("schema_version"), int),
        str(sch.get("schema_version")),
    )
    validate("funnels", "GET /schema", sch, [("intelligence", ic / "schema_info.schema.json")])
    st = fun.api(fk, "/export/funnel-structure")
    structure = st.json()
    truth["structure"] = structure
    validate("funnels", "GET /export/funnel-structure", structure, [("own", fs / "funnel-structure.schema.json")])
    items = structure["data"] if isinstance(structure, dict) else structure
    shape = "{data:[...], next_cursor}" if isinstance(structure, dict) else "bare list"
    rep.fact("funnel_structure_shape", shape)
    errs = validation_errors(schema_validator(ic / "funnel_structure.schema.json"), items[0])
    if errs:
        rep.deviation(
            "intelligence-contract",
            "GET /export/funnel-structure",
            "real shape (funnel `id`, no step `type`) is not what contracts/funnel_structure.schema.json describes; the connector tolerates it",
            "; ".join(errs),
        )

    win = {"from": backfill_from()}
    ev_rows, ev_pages, ev_resps = fun.export_all(fk, "/export/events", 5000, win)
    ld_rows, _, _ = fun.export_all(fk, "/export/leads", 5000, win)
    truth["events"], truth["leads"] = ev_rows, ld_rows
    rep.fact("funnels_export_counts", {"events": len(ev_rows), "leads": len(ld_rows), "event_pages_at_limit_5000": ev_pages})
    ev_v = schema_validator(fs / "export-event.schema.json")
    lead_v = schema_validator(fs / "export-lead.schema.json")
    ice = schema_validator(ic / "event.schema.json")
    icl = schema_validator(ic / "lead.schema.json")
    bad_own = bad_ic = 0
    ic_msgs: Counter[str] = Counter()
    for row in ev_rows[:: max(1, len(ev_rows) // 3000)]:
        if not ev_v.is_valid(row):
            bad_own += 1
        e = validation_errors(ice, row, 1)
        if e:
            bad_ic += 1
            ic_msgs[e[0]] += 1
    rep.check("funnels: events satisfy the app's own export-event schema", bad_own == 0, f"{bad_own} bad rows")
    if bad_ic:
        rep.deviation(
            "intelligence-contract", "GET /export/events", "rows fail contracts/event.schema.json", str(ic_msgs.most_common(3))
        )
    bl = [validation_errors(lead_v, row, 1) for row in ld_rows[:500]]
    rep.check("funnels: leads satisfy the app's own export-lead schema", not any(bl))
    il = Counter(e[0] for row in ld_rows[:500] if (e := validation_errors(icl, row, 1)))
    if il:
        rep.deviation(
            "intelligence-contract", "GET /export/leads", "rows fail contracts/lead.schema.json", str(il.most_common(3))
        )
    # ordering and uniqueness (keyset pagination)
    keys_seq = [(r["ts"], r["id"]) for r in ev_rows]
    rep.check(
        "funnels: events are unique and ordered by (ts,id) across pages",
        len({r["id"] for r in ev_rows}) == len(ev_rows) and keys_seq == sorted(keys_seq),
    )
    rep.check(
        "funnels: export pages carry X-Next-Cursor and X-Row-Count",
        all("x-row-count" in x.headers for x in ev_resps)
        and all(x.headers.get("x-next-cursor") for x in ev_resps[:-1])
        and not ev_resps[-1].headers.get("x-next-cursor"),
    )
    small_rows, _, small_resps = fun.export_all(fk, "/export/events", 700, win, max_pages=3)
    rep.check(
        "funnels: paging with limit=700 yields exactly the leading rows of the 5,000-row paging",
        [r["id"] for r in small_rows] == [r["id"] for r in ev_rows[: len(small_rows)]],
        f"{len(small_rows)} rows",
    )
    big_rows, big_pages, big_resps = fun.export_all(fk, "/export/events", 20000, win)
    rep.check(
        "funnels: a 20,000-row page gives the same rows as 5,000-row paging",
        [r["id"] for r in big_rows] == [r["id"] for r in ev_rows],
        f"{big_pages} page(s)",
    )
    big = big_resps[0]
    rep.check(
        "funnels: gzip is honoured for large pages",
        big.headers.get("content-encoding") == "gzip" or len(big.content) < 1024,
        f"content-encoding={big.headers.get('content-encoding')}",
    )
    resumed = fun.api(fk, "/export/events", {**win, "limit": 700, "cursor": small_resps[0].headers["x-next-cursor"]})
    rep.check("funnels: a cursor can be replayed (resumable)", resumed.status_code == 200 and resumed.text == small_resps[1].text)

    # -- conversations -------------------------------------------------------------------------------------
    ov = conv.api(ck, "/overview", {"from": win["from"][:10]}).json()
    truth["overview"] = ov
    validate("conversations", "GET /overview", ov, [("own", cs / "overview.schema.json")])
    for metric in ("rooms_opened", "messages_in", "messages_out", "first_response_p50", "resolution_p50", "backlog"):
        r = conv.api(ck, "/timeseries", {"metric": metric, "interval": "day", "from": "2026-10-10", "to": "2026-10-12"})
        if r.status_code != 200:
            rep.check(f"conversations: timeseries {metric} answers 200", False, f"HTTP {r.status_code} {r.text[:100]}")
            continue
        validate(
            "conversations",
            "GET /timeseries",
            r.json(),
            [("own", cs / "timeseries.schema.json"), ("intelligence", ic / "timeseries.schema.json")],
        )
    csch = conv.api(ck, "/schema").json()
    rep.check("conversations: GET /schema returns integer schema_version", isinstance(csch.get("schema_version"), int))
    published = json.loads((cs / "schema-version.json").read_text())
    rep.check(
        "conversations: schema_version equals the one in docs/analytics-api/schema-version.json",
        csch.get("schema_version") == published["schema_version"],
        f"{csch.get('schema_version')} vs {published['schema_version']}",
    )
    rep.check(
        "both apps embed JSON Schemas in GET /schema",
        bool(csch.get("schemas")) and bool(sch.get("schemas")),
        f"conversations keys {sorted(csch.get('schemas', {}))[:6]}, funnels keys {sorted(sch.get('schemas', {}))[:6]}",
    )
    rm_rows, _, rm_resps = conv.export_all(ck, "/export/rooms", 5000, win)
    mm_rows, _, _ = conv.export_all(ck, "/export/messages-meta", 5000, win)
    truth["rooms"], truth["messages_meta"] = rm_rows, mm_rows
    rep.fact(
        "conversations_export_counts",
        {"rooms": len(rm_rows), "messages_meta": len(mm_rows), "overview_rooms_opened": ov["rooms_opened"]},
    )
    rv, mv = (
        schema_validator(cs / "export-rooms-line.schema.json"),
        schema_validator(cs / "export-messages-meta-line.schema.json"),
    )
    rep.check("conversations: rooms satisfy the app's own export schema", all(rv.is_valid(r) for r in rm_rows[::3]))
    rep.check("conversations: messages-meta satisfy the app's own export schema", all(mv.is_valid(r) for r in mm_rows[::10]))
    irm, imm = schema_validator(ic / "room.schema.json"), schema_validator(ic / "message_meta.schema.json")
    msgs = Counter(e[0] for r in rm_rows if (e := validation_errors(irm, r, 1)))
    if msgs:
        rep.deviation(
            "intelligence-contract", "GET /export/rooms", "rows fail contracts/room.schema.json", str(msgs.most_common(3))
        )
    msgs = Counter(e[0] for r in mm_rows[::10] if (e := validation_errors(imm, r, 1)))
    if msgs:
        rep.deviation(
            "intelligence-contract",
            "GET /export/messages-meta",
            "rows fail contracts/message_meta.schema.json",
            str(msgs.most_common(3)),
        )
    rk = [(r["created_at"], r["id"]) for r in rm_rows]
    rep.check(
        "conversations: rooms are unique and ordered by (created_at,id)",
        len({r["id"] for r in rm_rows}) == len(rm_rows) and rk == sorted(rk),
    )
    rep.check(
        "conversations: rooms_opened in /overview equals the number of exported rooms",
        ov["rooms_opened"] == len(rm_rows),
        f"{ov['rooms_opened']} vs {len(rm_rows)}",
    )
    if rm_resps and rm_resps[0].headers.get("content-type", "").startswith("application/x-ndjson"):
        rep.check("conversations: NDJSON content-type", True)
    rep.fact(
        "ai_field_encodings",
        {
            "ai_sentiment": sorted({str(r["ai_sentiment"]) for r in rm_rows}),
            "ai_urgency_sample": sorted({str(r["ai_urgency"]) for r in rm_rows})[:8],
            "ai_intent": sorted({str(r["ai_intent"]) for r in rm_rows}),
            "dow_created_values": sorted({r["dow_created"] for r in rm_rows}),
            "hour_values_max": max(r["hour_of_day_created"] for r in rm_rows),
        },
    )
    # extra field not in the contract
    extra = sorted(set().union(*(r.keys() for r in rm_rows[:50])) - set(irm.schema.get("properties", {})))  # type: ignore[union-attr]
    if extra:
        rep.deviation(
            "conversations",
            "GET /export/rooms",
            f"additional field(s) not in the contract: {extra} (harmless: consumers ignore unknown fields)",
        )

    if record is not None:
        record["funnels_list"] = allf
        record["funnel_steps"] = steps_by_funnel
        record["funnel_structure"] = structure
        record["funnel_timeseries"] = ts
        record["funnels_schema"] = {"schema_version": sch.get("schema_version")}
        record["overview"] = ov
        record["events"] = ev_rows
        record["leads"] = ld_rows
        record["rooms"] = rm_rows
        record["messages_meta"] = mm_rows
        record["conv_timeseries"] = conv.api(
            ck, "/timeseries", {"metric": "rooms_opened", "interval": "day", "from": "2026-10-10", "to": "2026-10-12"}
        ).json()
    return truth


def validate_funnel_item(ic: Path, f: dict[str, Any]) -> bool:
    return schema_validator(ic / "funnel.schema.json").is_valid(f)


# ----------------------------------------------------------------------------------------------------------------
def auth_and_limits(fun: App, conv: App, keys: dict[str, Any], rep: Report, record: dict[str, Any] | None) -> None:
    section("API-key failure modes: revoked, expired, wrong workspace, missing scope, malformed")
    fs, cs = REPOS / "funnels" / "docs" / "analytics-api", REPOS / "conversations" / "docs" / "analytics-api"
    wait = keys["expiry_set_at"] + 4.5 - time.time()
    if wait > 0:
        time.sleep(wait)
    for app, prefix, err_schema in ((fun, "f", fs / "error.schema.json"), (conv, "c", cs / "error.schema.json")):
        ev = schema_validator(err_schema)
        for label, key, want in (
            ("revoked key", keys[f"{prefix}_revoked"], 401),
            ("expired key", keys[f"{prefix}_expiring"], 401),
            ("malformed key", "not-a-key", 401),
        ):
            r = app.api(key, "/funnels" if prefix == "f" else "/overview")
            ok = r.status_code == want and not validation_errors(ev, r.json())
            rep.check(f"{app.name}: {label} -> {want} with the contract error body", ok, f"HTTP {r.status_code} {r.text[:120]}")
        r = app.api(keys[f"{prefix}_read"], "/export/events" if prefix == "f" else "/export/rooms")
        rep.check(f"{app.name}: read-only key on an export -> 403", r.status_code == 403, f"HTTP {r.status_code}")
        r = app.api(keys[f"{prefix}_other"], "/funnels" if prefix == "f" else "/overview")
        rep.check(
            f"{app.name}: another workspace's key answers 200 with ITS (empty) data",
            r.status_code == 200,
            f"HTTP {r.status_code}",
        )
        r = app.http.get(
            f"{app.base}/api/v1/analytics/{'funnels' if prefix == 'f' else 'overview'}",
            headers=keys["fh" if prefix == "f" else "ch"],
        )
        rep.check(
            f"{app.name}: a user session token is not accepted by the analytics API",
            r.status_code in (401, 403),
            f"HTTP {r.status_code}",
        )
    fid = fun.api(keys["f_key"], "/funnels").json()["data"][0]["id"]
    r = fun.api(keys["f_other"], f"/funnels/{fid}/steps")
    rep.check(
        "funnels: another workspace's funnel id -> 404 (existence not confirmed)", r.status_code == 404, f"HTTP {r.status_code}"
    )
    r = fun.api(keys["f_key"], "/funnels", {"from": "2020-01-01", "to": "2026-10-01"})
    rep.check("funnels: range over 366 days is refused with 400", r.status_code == 400, f"HTTP {r.status_code} {r.text[:100]}")
    r = conv.api(keys["c_key"], "/overview", {"from": "2020-01-01", "to": "2026-10-01"})
    rep.check(
        "conversations: range over 366 days is refused with 400", r.status_code == 400, f"HTTP {r.status_code} {r.text[:100]}"
    )
    r = fun.api(keys["f_key"], "/export/events", {"limit": 99999})
    rep.fact("funnels_export_limit_over_max", f"HTTP {r.status_code}")
    r = conv.api(keys["c_key"], "/export/rooms", {"limit": 99999})
    rep.fact("conversations_export_limit_over_max", f"HTTP {r.status_code} {r.text[:120]}")
    r = conv.api(keys["c_key"], "/export/rooms", {"limit": 5000})
    rep.check(
        "conversations: exports accept limit=5000 (above the contract's 1000 for lists)",
        r.status_code == 200,
        f"HTTP {r.status_code}",
    )
    # pepper endpoint access
    r = fun.http.get(f"{fun.base}/api/v1/analytics/identity-pepper", headers={"authorization": f"Bearer {keys['f_key']}"})
    rep.check("funnels: an API key can NOT read the identity pepper", r.status_code in (401, 403), f"HTTP {r.status_code}")
    if keys.get("c_identity_key"):
        r = conv.http.get(
            f"{conv.base}/api/v1/analytics/identity-pepper", headers={"authorization": f"Bearer {keys['c_identity_key']}"}
        )
        rep.fact("conversations_pepper_via_api_key", f"HTTP {r.status_code}")
        rep.check(
            "conversations: API key with analytics_identity:manage reads the same pepper as the session endpoint",
            r.status_code == 200 and r.json().get("pepper") == keys["c_pepper"],
            f"HTTP {r.status_code} {r.text[:100]}",
        )
        rep.check(
            "conversations: the API-key pepper response satisfies its published schema",
            r.status_code == 200 and not validation_errors(schema_validator(cs / "identity-pepper.schema.json"), r.json()),
            r.text[:100],
        )
    rep.deviation(
        "funnels+conversations",
        "identity-pepper",
        "the pepper is generated per workspace and neither app can SET it, only rotate it to a new random value; two apps therefore never share a pepper, so the contract's join (same pepper -> same contact_key) is impossible without a Node-side change",
        "Funnels: services/analyticsapi.ts identityPepper/rotatePepper (randomBytes(32)); Conversations: services/analytics.ts getPepper/rotatePepper. Needs an import/set endpoint.",
    )
    rep.deviation(
        "conversations",
        "GET /api/analytics/identity-pepper",
        "the contract names GET /api/v1/analytics/identity-pepper for the pepper; Conversations serves the session-authenticated one at /api/analytics/identity-pepper and the /api/v1 path needs an API key with analytics_identity:manage (a scope Funnels does not have); response shapes differ ({pepper,rotatedAt} vs {pepper,version,algorithm})",
    )

    section("rate limits: exports 10 per minute per key, 429 + Retry-After")
    for app, key, path in ((fun, keys["f_other"], "/export/events"), (conv, keys["c_other"], "/export/rooms")):
        codes: list[int] = []
        last429: httpx.Response | None = None
        seen_headers: dict[str, str] = {}
        for _ in range(14):
            r = app.api(key, path, {"limit": 10}, retry429=False)
            codes.append(r.status_code)
            if r.status_code == 200 and not seen_headers:
                seen_headers = {k: v for k, v in r.headers.items() if "ratelimit" in k}
            if r.status_code == 429:
                last429 = r
                break
        n_ok = codes.count(200)
        rep.fact(f"{app.name}_requests_before_first_429_on_a_used_key", n_ok)
        rep.check(
            f"{app.name}: the export limiter answers 429 within a burst of 14 requests",
            429 in codes and n_ok <= 11,
            f"codes={codes}",
        )
        if last429 is not None:
            ra = last429.headers.get("retry-after", "")
            body = last429.json()
            rep.check(
                f"{app.name}: 429 has an integer Retry-After and RATE_LIMITED body with retry_after_s and policy",
                ra.isdigit()
                and body["error"]["code"] == "RATE_LIMITED"
                and "retry_after_s" in body["error"]
                and "policy" in body["error"],
                f"Retry-After={ra} body={json.dumps(body)[:160]}",
            )
            rep.fact(
                f"{app.name}_429",
                {
                    "retry_after": ra,
                    "body": body,
                    "headers": {k: v for k, v in last429.headers.items() if "ratelimit" in k or k == "retry-after"},
                    "ok_headers": seen_headers,
                },
            )
            if record is not None:
                record[f"{app.name}_429"] = {
                    "status": 429,
                    "headers": {k: v for k, v in last429.headers.items() if "ratelimit" in k or k == "retry-after"},
                    "body": body,
                    "ok_headers": seen_headers,
                }
            # does the penalty box escalate? hit again immediately
            again = app.api(key, path, {"limit": 10}, retry429=False)
            rep.fact(f"{app.name}_second_429_retry_after", again.headers.get("retry-after"))
    return None


# ----------------------------------------------------------------------------------------------------------------
class Intel:
    def __init__(self, base: str, admin_key: str) -> None:
        self.base = base.rstrip("/")
        self.http = httpx.Client(timeout=900)
        self.admin = {"X-Admin-Key": admin_key}

    def account(self, name: str) -> dict[str, str]:
        r = self.http.post(f"{self.base}/admin/accounts", headers=self.admin, json={"name": name})
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['api_key']}"}

    def get(self, h: dict[str, str], path: str, **params: Any) -> httpx.Response:
        return self.http.get(f"{self.base}{path}", headers=h, params=params)

    def post(self, h: dict[str, str], path: str, body: dict[str, Any] | None = None, **params: Any) -> httpx.Response:
        return self.http.post(f"{self.base}{path}", headers=h, json=body or {}, params=params)


def db_counts(path: Path, account_id: str) -> dict[str, int]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {}
        for t in ("events", "leads", "rooms", "messages_meta", "funnels", "funnel_steps", "funnel_structure", "rollups"):
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t} WHERE account_id = ?", (account_id,)).fetchone()[0]
        return out
    finally:
        con.close()


def intelligence_flow(
    fun: App,
    conv: App,
    intel: Intel,
    keys: dict[str, Any],
    truth: dict[str, Any],
    people: list[dict[str, Any]],
    workdir: Path,
    rep: Report,
    incident: dict[str, Any] | None = None,
) -> None:
    section("intelligence: register sources, sync against the real apps, check what landed")
    h = intel.account("Real Co")
    acct_id = intel.get(h, "/v1/account").json()["id"]
    db = workdir / "intel" / "intelligence.db"
    for kind, name, app, key, pepper in (
        ("funnels", "funnels", fun, keys["f_key"], keys["f_pepper"]),
        ("conversations", "conversations", conv, keys["c_key"], keys["c_pepper"]),
    ):
        r = intel.post(
            h, "/v1/sources", {"kind": kind, "name": name, "base_url": app.base, "api_key": key, "identity_pepper": pepper}
        )
        rep.check(f"register {kind} source", r.status_code == 201, r.text[:160])
    t0 = time.time()
    r = intel.post(h, "/v1/sync", {}, wait="true")
    sync_s = time.time() - t0
    job = r.json()
    rep.check(
        "first full sync succeeded",
        job.get("status") == "succeeded"
        and not job["result"]["errors"]
        and all(x["error"] is None for s in job["result"]["sources"] for x in s["resources"]),
        json.dumps(job)[:400],
    )
    rep.fact("first_sync_seconds", round(sync_s, 1))
    per_res = {(s["kind"], x["resource"]): x for s in job["result"]["sources"] for x in s["resources"]}
    rep.fact(
        "sync_pages_retries",
        {f"{k[0]}.{k[1]}": {"rows": v["rows"], "pages": v["pages"], "retries": v["retries"]} for k, v in per_res.items()},
    )
    cnt = db_counts(db, acct_id)
    rep.check(
        "landed events == exported events", cnt["events"] == len(truth["events"]), f"{cnt['events']} vs {len(truth['events'])}"
    )
    rep.check("landed leads == exported leads", cnt["leads"] == len(truth["leads"]), f"{cnt['leads']} vs {len(truth['leads'])}")
    rep.check("landed rooms == exported rooms", cnt["rooms"] == len(truth["rooms"]), f"{cnt['rooms']} vs {len(truth['rooms'])}")
    rep.check(
        "landed messages_meta == exported",
        cnt["messages_meta"] == len(truth["messages_meta"]),
        f"{cnt['messages_meta']} vs {len(truth['messages_meta'])}",
    )
    n_struct = sum(
        len(f["steps"]) for f in (truth["structure"]["data"] if isinstance(truth["structure"], dict) else truth["structure"])
    )
    rep.check("funnel structure landed (steps)", cnt["funnel_structure"] == n_struct, f"{cnt['funnel_structure']} vs {n_struct}")
    rep.check(
        "funnels and steps landed",
        cnt["funnels"] == len(truth["funnels"])
        and cnt["funnel_steps"] == sum(len(v["data"] if isinstance(v, dict) else v) for v in truth["steps"].values()),
        str(cnt),
    )
    status = intel.get(h, "/v1/sync/status").json()
    rep.check(
        "sync status reports rows for every resource",
        all(s["rows_total"] > 0 for s in status["sources"] if s["resource"] not in ("rollups",))
        and all(s["last_status"] == "ok" for s in status["sources"]),
        json.dumps([(s["resource"], s["rows_total"]) for s in status["sources"]]),
    )
    # timestamps: an event's ts must land as the same UTC instant
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    sample = truth["events"][len(truth["events"]) // 2]
    got = con.execute("SELECT ts FROM events WHERE account_id = ? AND ext_id = ?", (acct_id, sample["id"])).fetchone()
    rep.check(
        "event timestamp (ISO with milliseconds, Z) lands as the same UTC epoch second",
        bool(got) and abs(got[0] - parse_ts(sample["ts"])) < 1.0,
        f"{got} vs {parse_ts(sample['ts'])}",
    )
    roomS = truth["rooms"][len(truth["rooms"]) // 2]
    got = con.execute(
        "SELECT created_at, closed_at, hour_of_day_created, dow_created, ai_urgency, ai_sentiment FROM rooms WHERE account_id = ? AND ext_id = ?",
        (acct_id, roomS["id"]),
    ).fetchone()
    rep.check(
        "room timestamps land as the same UTC epoch second",
        bool(got) and abs(got[0] - parse_ts(roomS["created_at"])) < 1.0 and (got[1] is None) == (roomS["closed_at"] is None),
    )
    con.close()

    section("idempotence and incremental sync with NEW traffic on the real apps")
    r = intel.post(h, "/v1/sync", {}, wait="true")
    again = db_counts(db, acct_id)
    rep.check(
        "a second sync adds nothing (idempotent upserts)",
        all(again[k] == cnt[k] for k in ("events", "leads", "rooms", "messages_meta")),
        f"{cnt} -> {again}",
    )
    stats0 = {(s["resource"]): s for s in intel.get(h, "/v1/sync/status").json()["sources"]}
    fid = truth["funnels"][0]["id"]
    step0 = (
        truth["steps"][fid]["data"][0]["step_id"] if isinstance(truth["steps"][fid], dict) else truth["steps"][fid][0]["step_id"]
    )
    new_ev = 0
    for i in range(12):
        rr = httpx.post(
            f"{fun.base}/api/public/events",
            json={
                "funnelId": fid,
                "stepId": step0,
                "type": "page_view",
                "visitorId": f"vinc{i:04d}",
                "utms": {"source": "google", "medium": "cpc", "campaign": "brand"},
            },
            headers={"user-agent": UA},
        )
        new_ev += int(rr.status_code == 202 and rr.json().get("recorded"))
    time.sleep(0.6)  # the app buffers events for EVENT_BUFFER_MS
    r = intel.post(h, "/v1/sync", {}, wait="true")
    after = db_counts(db, acct_id)
    rep.check(
        "incremental sync lands exactly the new events",
        after["events"] == cnt["events"] + new_ev and new_ev > 0,
        f"{cnt['events']} + {new_ev} -> {after['events']}",
    )
    del stats0

    section("models, rankings, scores, segments, anomalies, insights, ask on the real data")
    t0 = time.time()
    r = intel.post(h, "/v1/models/train", {}, wait="true")
    tj = r.json()
    rep.check("training job succeeded", tj.get("status") == "succeeded", json.dumps(tj)[:300])
    rep.fact("train_seconds", round(time.time() - t0, 1))
    models = intel.get(h, "/v1/models").json()
    rep.fact(
        "models",
        [
            {
                "type": m["model_type"],
                "method": m["method"],
                "status": m["status"],
                "auc": m["metrics"].get("auc"),
                "rows": m["trained_rows"],
                "cold_start": m["metrics"].get("cold_start_reason"),
            }
            for m in models
        ],
    )
    by_type = {m["model_type"]: m for m in models}
    rep.check(
        "lead_conversion trained as gbm (enough matured leads in 45 days of real data)",
        by_type.get("lead_conversion", {}).get("method") == "gbm",
        str(by_type.get("lead_conversion", {}).get("metrics", {}).get("cold_start_reason")),
    )
    rep.check("sla_breach model present", "sla_breach" in by_type)
    for path in (
        "/v1/rankings/funnels",
        "/v1/rankings/steps",
        "/v1/leads/scores?limit=5",
        "/v1/rankings/conversations/open?limit=5",
        "/v1/segments",
        "/v1/anomalies?since_days=60",
        "/v1/insights",
        "/v1/join",
    ):
        r = intel.get(h, path)
        rep.check(
            f"GET {path} -> 200 with valid JSON",
            r.status_code == 200 and r.json() is not None,
            f"HTTP {r.status_code} {r.text[:200]}",
        )
    rf = intel.get(h, "/v1/rankings/funnels").json()
    confirmation = {"Thank you", "Confirmation", "Thank you page"}
    rep.check(
        "ranking leaves out the confirmation (thank-you) pages and has a benchmark for the entry steps",
        not any(x["step_name"] in confirmation for x in rf["steps"])
        and any(x["role"] == "entry" and x["benchmark_rate"] is not None for x in rf["steps"]),
        json.dumps([(x["step_name"], x["role"]) for x in rf["steps"]][:12]),
    )
    rep.fact("ranking_notes", rf.get("notes"))
    rep.fact(
        "top_ranked_steps",
        [(s["funnel_id"][-4:], s["step_name"], s["expected_uplift"], s["confidence"]) for s in rf["steps"][:4]],
    )
    sc = intel.get(h, "/v1/leads/scores", limit=5).json()
    rep.fact("lead_scores_method", sc.get("method"))
    rep.check("lead scores returned with factors", len(sc["items"]) > 0 and all("top_factors" in i for i in sc["items"]))
    oc = intel.get(h, "/v1/rankings/conversations/open", limit=5).json()
    rep.check("open conversations ranked", len(oc["items"]) > 0, json.dumps(oc)[:200])
    an = intel.get(h, "/v1/anomalies", since_days=60).json()
    rep.fact(
        "anomalies",
        {
            "status": an["status"],
            "series_checked": an["series_checked"],
            "skipped": an["series_skipped_insufficient_history"],
            "alerts": [(a["metric"], a["scope"][-6:], a["direction"]) for a in an["alerts"]][:8],
        },
    )
    rep.check(
        "anomaly detector checked series (history is long enough)",
        an["series_checked"] > an["series_skipped_insufficient_history"],
        str(an),
    )
    rep.fact(
        "anomaly_alerts_all",
        [
            (
                a["metric"],
                a["scope"],
                a["direction"],
                datetime.fromtimestamp(a["start_t"], UTC).strftime("%m-%d"),
                datetime.fromtimestamp(a["end_t"], UTC).strftime("%m-%d"),
            )
            for a in an["alerts"]
        ],
    )
    if incident:
        hit = [a for a in an["alerts"] if a["scope"] == incident["funnel_id"] and a["direction"] == "drop"]
        rep.check(
            f"planted traffic collapse of funnel '{incident['funnel_name']}' (last {incident['days']} days) is reported as a drop",
            bool(hit),
            f"alerts for that funnel: {[(a['metric'], a['direction']) for a in an['alerts'] if a['scope'] == incident['funnel_id']]}",
        )
    ins = intel.get(h, "/v1/insights").json()
    rep.check(
        "insights narrative produced (offline template)", bool(ins["narrative"]["summary"]), json.dumps(ins["narrative"])[:200]
    )
    a1 = intel.post(h, "/v1/ask", {"question": "Which funnel should I fix first and why?"}).json()
    rep.check(
        "/v1/ask answers from tool results (offline fallback)",
        bool(a1.get("answer")) and a1.get("tools_used"),
        json.dumps(a1)[:200],
    )

    section("identity: same pepper -> same contact_key; cross-app join")
    app_emails_f = lead_emails(fun, keys["fh"])
    app_emails_c = contact_emails(conv, keys["ch"])
    f_ground = {key_hash(keys["f_pepper"], e) for e in app_emails_f}
    c_ground = {key_hash(keys["c_pepper"], e) for e in app_emails_c}
    exported_f = {r["contact_key"] for r in truth["leads"]}
    exported_c = {r["contact_key"] for r in truth["rooms"] if r["contact_key"]}
    exported_ev = {r["contact_key"] for r in truth["events"] if r["contact_key"]}
    rep.fact(
        "identity_ground_truth",
        {"funnels_leads_in_app": len(app_emails_f), "conversation_contacts_with_email_in_app": len(app_emails_c)},
    )
    rep.check(
        "every exported Funnels lead/event contact_key equals HMAC-SHA256(funnels pepper, lower(trim(email))) of a lead e-mail read from the app's own API",
        exported_f <= f_ground and exported_ev <= f_ground,
        f"{len(exported_f - f_ground)} unexplained lead keys, {len(exported_ev - f_ground)} unexplained event keys",
    )
    rep.check(
        "every exported Conversations room contact_key equals HMAC(conversations pepper, email) of a contact e-mail read from the app",
        exported_c <= c_ground,
        f"{len(exported_c - c_ground)} unexplained of {len(exported_c)}",
    )
    odd = [p["email"] for p in people if p["funnels"] and p["email"] != p["email"].strip().lower()]
    rep.check(
        "mixed-case / padded e-mails are normalised before hashing (same key as the lower-cased address)",
        bool(odd) and all(key_hash(keys["f_pepper"], e) in exported_f for e in odd),
        f"{len(odd)} such people",
    )
    known_overlap = len({p["email"].strip().lower() for p in people if p["funnels"] and p["conversations"]})
    rep.fact("people_in_both_apps_ground_truth", known_overlap)
    j = intel.get(h, "/v1/join").json()
    rep.fact(
        "join_with_REAL_distinct_peppers",
        {k: j[k] for k in ("funnel_contacts", "conversation_contacts", "joined_contacts", "warnings")},
    )
    pepper_scenario(fun, conv, intel, keys, people, rep, known_overlap)
    one = next(p for p in people if p["funnels"])
    r = intel.post(h, "/v1/leads/score", {"email": one["email"]})
    rep.check(
        "POST /v1/leads/score by email finds the contact (hashed in memory with the source pepper)",
        r.status_code == 200,
        f"HTTP {r.status_code} {r.text[:200]}",
    )

    section("failure modes at the Intelligence side: revoked key, expired key, rate limit during sync, empty workspace")
    for label, fkey_, good_c in (
        ("revoked", keys["f_revoked"], keys["c_mixed1"]),
        ("expired", keys["f_expiring"], keys["c_mixed2"]),
    ):
        h2 = intel.account(f"{label} funnels key")
        r1 = intel.post(
            h2,
            "/v1/sources",
            {"kind": "funnels", "name": "f", "base_url": fun.base, "api_key": fkey_, "identity_pepper": keys["f_pepper"]},
        )
        r2 = intel.post(
            h2,
            "/v1/sources",
            {"kind": "conversations", "name": "c", "base_url": conv.base, "api_key": good_c, "identity_pepper": keys["c_pepper"]},
        )
        j = intel.post(h2, "/v1/sync", {}, wait="true").json()
        srcs = {s["kind"]: s for s in (j.get("result") or {}).get("sources", [])}
        f_errs = [x["error"] for x in srcs.get("funnels", {}).get("resources", [])]
        c_rows = {x["resource"]: x["rows"] for x in srcs.get("conversations", {}).get("resources", [])}
        rep.check(
            f"{label} Funnels key: that source reports an auth error per resource",
            r1.status_code == 201
            and r2.status_code == 201
            and len(f_errs) >= 4
            and all(e and ("credentials" in e or "Auth" in e) for e in f_errs),
            json.dumps(j)[:300],
        )
        rep.check(
            f"{label} Funnels key: the healthy Conversations source is still synced in the same job",
            j.get("status") == "succeeded" and c_rows.get("rooms", 0) > 0,
            json.dumps(j)[:300],
        )
        st = intel.get(h2, "/v1/sync/status").json()
        rep.check(
            f"{label} Funnels key: /v1/sync/status shows the error on the funnels resources",
            any(x["last_status"] == "error" and x["error"] for x in st["sources"]),
            json.dumps(st["sources"])[:200],
        )
    h2 = intel.account("all keys revoked")
    intel.post(
        h2,
        "/v1/sources",
        {"kind": "funnels", "name": "f", "base_url": fun.base, "api_key": keys["f_revoked"], "identity_pepper": keys["f_pepper"]},
    )
    intel.post(
        h2,
        "/v1/sources",
        {
            "kind": "conversations",
            "name": "c",
            "base_url": conv.base,
            "api_key": keys["c_revoked"],
            "identity_pepper": keys["c_pepper"],
        },
    )
    j = intel.post(h2, "/v1/sync", {}, wait="true").json()
    rep.check(
        "every key revoked: the job fails with a message naming the rejected credentials",
        j.get("status") == "failed" and "credentials" in (j.get("error") or ""),
        json.dumps(j)[:300],
    )
    rate_limited_sync(fun, conv, intel, keys, truth, rep)
    h3 = intel.account("Empty workspaces")
    intel.post(
        h3,
        "/v1/sources",
        {
            "kind": "funnels",
            "name": "f",
            "base_url": fun.base,
            "api_key": keys["f_other"],
            "identity_pepper": keys["f_other_pepper"],
        },
    )
    intel.post(
        h3,
        "/v1/sources",
        {
            "kind": "conversations",
            "name": "c",
            "base_url": conv.base,
            "api_key": keys["c_other"],
            "identity_pepper": keys["c_other_pepper"],
        },
    )
    j = intel.post(h3, "/v1/sync", {}, wait="true").json()
    rep.check(
        "empty (other-tenant) workspaces sync with zero rows and no error",
        j["status"] == "succeeded" and not j["result"]["errors"],
        json.dumps(j)[:300],
    )
    t = intel.post(h3, "/v1/models/train", {}, wait="true").json()
    rep.check("training on empty data succeeds with cold-start/skip results", t["status"] == "succeeded", json.dumps(t)[:300])
    for path in (
        "/v1/rankings/funnels",
        "/v1/rankings/steps",
        "/v1/leads/scores",
        "/v1/rankings/conversations/open",
        "/v1/segments",
        "/v1/anomalies",
        "/v1/insights",
        "/v1/join",
    ):
        r = intel.get(h3, path)
        rep.check(f"empty state: GET {path} -> 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:160]}")
    r = intel.post(h3, "/v1/ask", {"question": "What should I fix first?"})
    rep.check("empty state: /v1/ask answers", r.status_code == 200, f"HTTP {r.status_code} {r.text[:160]}")


def rate_limited_sync(fun: App, conv: App, intel: Intel, keys: dict[str, Any], truth: dict[str, Any], rep: Report) -> None:
    """Burn the export budget of a fresh key, then sync with it: the connector must wait (Retry-After / pacing) and finish."""
    h = intel.account("Rate limited")
    for kind, app, key, pepper, path in (
        ("funnels", fun, keys["f_rl"], keys["f_pepper"], "/export/leads"),
        ("conversations", conv, keys["c_rl"], keys["c_pepper"], "/export/rooms"),
    ):
        codes = [app.api(key, path, {"limit": 1}, retry429=False).status_code for _ in range(14)]
        n_ok = codes.index(429) if 429 in codes else len(codes)
        rep.fact(f"{kind}_fresh_key_successes_before_first_429", n_ok)
        rep.check(
            f"{kind}: a fresh key gets 429 after about 10 export requests per minute",
            429 in codes and 8 <= n_ok <= 11,
            str(codes),
        )
        intel.post(
            h, "/v1/sources", {"kind": kind, "name": kind, "base_url": app.base, "api_key": key, "identity_pepper": pepper}
        )
    t0 = time.time()
    j = intel.post(h, "/v1/sync", {}, wait="true").json()
    took = time.time() - t0
    rows = {(s["kind"], x["resource"]): x for s in (j.get("result") or {}).get("sources", []) for x in s["resources"]}
    ok = j.get("status") == "succeeded" and not j["result"]["errors"]
    rep.check("sync with a rate-limited key waits and completes without errors", ok, json.dumps(j)[:400])
    rep.fact(
        "rate_limited_sync",
        {
            "seconds": round(took, 1),
            "retries": {f"{k[0]}.{k[1]}": v["retries"] for k, v in rows.items() if v["retries"]},
            "rows": {f"{k[0]}.{k[1]}": v["rows"] for k, v in rows.items()},
        },
    )
    if ok:
        rep.check(
            "the rate-limited sync landed every event, lead, room and message",
            rows[("funnels", "events")]["rows"] >= len(truth["events"])
            and rows[("funnels", "leads")]["rows"] == len(truth["leads"])
            and rows[("conversations", "rooms")]["rows"] == len(truth["rooms"]),
            json.dumps({f"{k[1]}": v["rows"] for k, v in rows.items()}),
        )


def pepper_scenario(
    fun: App, conv: App, intel: Intel, keys: dict[str, Any], people: list[dict[str, Any]], rep: Report, known_overlap: int
) -> None:
    """The identity bridge: join contacts across apps whose peppers differ (which is what the real apps produce)."""
    h = intel.account("Bridge Co")
    for kind, app, key, pepper in (
        ("funnels", fun, keys["f_bridge"], keys["f_pepper"]),
        ("conversations", conv, keys["c_bridge"], keys["c_pepper"]),
    ):
        intel.post(
            h, "/v1/sources", {"kind": kind, "name": kind, "base_url": app.base, "api_key": key, "identity_pepper": pepper}
        )
    intel.post(h, "/v1/sync", {}, wait="true")
    j0 = intel.get(h, "/v1/join").json()
    rep.check(
        "without a bridge the real apps' join is empty and the service says why",
        j0["joined_contacts"] == 0 and any("pepper" in w for w in j0["warnings"]),
        json.dumps(j0)[:300],
    )
    emails = [p["email"] for p in people if p["funnels"] or p["conversations"]]
    r = intel.post(h, "/v1/identity/bridge", {"emails": emails})
    rep.check(
        "POST /v1/identity/bridge accepts a customer-supplied contact list",
        r.status_code == 200,
        f"HTTP {r.status_code} {r.text[:200]}",
    )
    if r.status_code != 200:
        return
    j1 = intel.get(h, "/v1/join").json()
    rep.fact(
        "join_after_bridge",
        {
            k: j1[k]
            for k in ("funnel_contacts", "conversation_contacts", "joined_contacts", "coverage_of_smaller_side", "warnings")
        },
    )
    rep.check(
        "after the bridge the join recovers the overlap of the two apps",
        j1["joined_contacts"] >= 0.95 * known_overlap,
        f"{j1['joined_contacts']} vs ground truth {known_overlap}",
    )


# ----------------------------------------------------------------------------------------------------------------
def write_fixtures(record: dict[str, Any]) -> None:
    out = ROOT / "tests" / "fixtures" / "real"
    out.mkdir(parents=True, exist_ok=True)

    def nd(name: str, rows: list[dict[str, Any]]) -> None:
        (out / name).write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))

    def js(name: str, obj: Any) -> None:
        (out / name).write_text(json.dumps(obj, indent=1) + "\n")

    def pick(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
        """n rows spread evenly over the whole export (first and last included)."""
        if len(rows) <= n:
            return list(rows)
        return [rows[round(i * (len(rows) - 1) / (n - 1))] for i in range(n)]

    nd("funnels_events.ndjson", pick(record["events"], 400))
    nd("funnels_leads.ndjson", pick(record["leads"], 120))
    # rooms: an even sample plus every kind of null the real app produces (rooms its AI triage never labelled)
    unlabelled = [r for r in record["rooms"] if r["ai_intent"] is None][:40]
    sample = {r["id"]: r for r in pick(record["rooms"], 140) + unlabelled}
    nd("conversations_rooms.ndjson", sorted(sample.values(), key=lambda r: (r["created_at"], r["id"])))
    nd("conversations_messages_meta.ndjson", pick(record["messages_meta"], 200))
    js("funnels_list.json", {"data": record["funnels_list"], "next_cursor": None})
    first = next(iter(record["funnel_steps"]))
    js("funnel_steps.json", record["funnel_steps"][first])
    js("funnel_structure.json", record["funnel_structure"])
    js("funnel_timeseries.json", record["funnel_timeseries"])
    js("conversations_overview.json", record["overview"])
    js("conversations_timeseries.json", record["conv_timeseries"])
    js("funnels_schema_info.json", record["funnels_schema"])
    for k in ("funnels_429", "conversations_429"):
        if k in record:
            js(f"{k}.json", record[k])
    print(f"  fixtures written to {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--funnels-url", required=True)
    ap.add_argument("--conversations-url", required=True)
    ap.add_argument("--intelligence-url", required=True)
    ap.add_argument("--admin-key", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--traffic", required=True)
    ap.add_argument("--record", action="store_true", help="store scrubbed real responses under tests/fixtures/real/")
    a = ap.parse_args()
    workdir = Path(a.workdir)
    traffic = json.loads(Path(a.traffic).read_text())
    people = traffic["contacts"]
    fun, conv = App("funnels", a.funnels_url), App("conversations", a.conversations_url)
    intel = Intel(a.intelligence_url, a.admin_key)
    record: dict[str, Any] | None = {} if a.record else None
    stages = [
        lambda: provision(fun, conv, R),
    ]
    keys = stages[0]()
    truth = contract_probe(fun, conv, keys, R, record)
    intelligence_flow(fun, conv, intel, keys, truth, people, workdir, R, traffic.get("incident"))
    auth_and_limits(fun, conv, keys, R, record)  # last: it spends the rate-limit budget of the 'other' keys only
    if record is not None:
        write_fixtures(record)
    failed = [c for c in R.checks if not c["ok"]]
    (workdir / "verify-report.json").write_text(
        json.dumps({"checks": R.checks, "deviations": R.deviations, "facts": R.facts}, indent=1, default=str)
    )
    print(f"\n{len(R.checks) - len(failed)}/{len(R.checks)} checks passed; {len(R.deviations)} deviations recorded")
    for c in failed:
        print(f"  FAILED: {c['name']}  {c['detail'][:300]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
