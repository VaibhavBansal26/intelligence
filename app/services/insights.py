"""Assemble ranked, structured findings from the ML outputs (the only input the narrator may use)."""

from __future__ import annotations

from typing import Any

from app.llm.guard import sanitize_untrusted
from app.llm.narrator import Finding, LLMNarrator
from app.services.analytics import AnalyticsService


def build_findings(analytics: AnalyticsService, account_id: str) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    raw: dict[str, Any] = {}
    ranks = analytics.funnel_rankings(account_id)
    raw["funnels"] = ranks["funnels"][:5]
    raw["steps"] = ranks["steps"][:5]
    for s in ranks["steps"][:3]:
        if s["expected_uplift"] <= 0:
            continue
        name = sanitize_untrusted(s["step_name"])
        fname = next((f["funnel_name"] for f in ranks["funnels"] if f["funnel_id"] == s["funnel_id"]), s["funnel_id"])
        pass_pct = round(s["shrunk_rate"] * 100, 1)
        bench_pct = round((s["benchmark_rate"] or 0) * 100, 1)
        m = {
            "visitors": s["visitors"],
            "pass_rate_pct": pass_pct,
            "benchmark_pct": bench_pct,
            "expected_uplift": s["expected_uplift"],
            "uplift_p10": s["uplift_p10"],
            "uplift_p90": s["uplift_p90"],
        }
        findings.append(
            Finding(
                id=f"step:{s['funnel_id']}:{s['step_id']}",
                kind="step",
                title=f"{sanitize_untrusted(fname)} / {name}",
                metrics=m,
                template=f"Improve step '{name}' in funnel '{sanitize_untrusted(fname)}': {s['visitors']} visitors, {pass_pct}% pass vs a {bench_pct}% benchmark; closing the gap is worth about {s['expected_uplift']} in revenue (range {s['uplift_p10']} to {s['uplift_p90']}).",
                hint_codes=[h["code"] for h in s.get("content_hints", [])],
            )
        )
    an = analytics.detect_anomalies(account_id)
    raw["anomalies"] = an["alerts"][:5]
    for a in an["alerts"][:2]:
        m = {
            "start": a["start"],
            "end": a["end"],
            "severity": a["severity"],
            "days": a["detail"]["days"],
            "observed_mean": a["detail"]["observed_mean"],
            "expected_mean": a["detail"]["expected_mean"],
        }
        findings.append(
            Finding(
                id=f"anomaly:{a['fingerprint'][:12]}",
                kind="anomaly",
                title=f"{a['metric']} {a['direction']} ({sanitize_untrusted(a['scope'])})",
                metrics=m,
                template=f"Investigate the {a['direction']} in {a['metric']} ({sanitize_untrusted(a['scope'])}) from {a['start']} to {a['end']}: observed {m['observed_mean']} vs expected {m['expected_mean']}.",
            )
        )
    conv = analytics.open_conversations(account_id, limit=3)
    raw["conversations"] = conv["items"]
    if conv["items"]:
        top = conv["items"][0]
        m = {
            "total_open": conv["total_open"],
            "priority_score": top["priority_score"],
            "waiting_hours": top["waiting_hours"],
            "sla_breach_probability": top["sla_breach_probability"],
        }
        findings.append(
            Finding(
                id=f"conversation:{top['room_id']}",
                kind="conversation",
                title="Most urgent open conversation",
                metrics=m,
                template=f"Reply first to the top-ranked conversation: priority {top['priority_score']}, waiting {top['waiting_hours']} hours, SLA breach probability {top['sla_breach_probability']}; {conv['total_open']} conversations are open.",
            )
        )
    leads = analytics.lead_scores(account_id, limit=5, explain=False)
    raw["leads"] = {"total": leads["total"], "method": leads["method"], "top": leads["items"][:3]}
    if leads["items"]:
        top_score = leads["items"][0]["score"]
        m = {"open_leads": leads["total"], "top_score": top_score, "horizon_days": leads["horizon_days"]}
        findings.append(
            Finding(
                id="leads:top",
                kind="leads",
                title="Highest-intent open leads",
                metrics=m,
                template=f"Follow up with the {leads['total']} open leads starting from the highest scores (top score {top_score}, {leads['horizon_days']}-day purchase horizon, {leads['method']} model).",
            )
        )
    join = analytics.join(account_id)
    raw["join"] = join
    if join["warnings"]:
        findings.append(
            Finding(
                id="data:join",
                kind="data_quality",
                title="Identity join problem",
                metrics={"joined_contacts": join["joined_contacts"]},
                template=f"Check that both apps use the same identity pepper: only {join['joined_contacts']} contacts could be linked across them.",
            )
        )
    return findings, raw


def insights(analytics: AnalyticsService, narrator: LLMNarrator, account_id: str, *, use_llm: bool = True) -> dict[str, Any]:
    findings, raw = build_findings(analytics, account_id)
    extra = {"currency": "see metrics"}
    narrative = narrator.narrate(account_id, findings, extra) if use_llm else narrator.template(findings)
    return {
        "findings": [
            {"id": f.id, "kind": f.kind, "title": f.title, "metrics": f.metrics, "hints": f.hint_codes} for f in findings
        ],
        "narrative": narrative,
        "evidence": raw,
    }
