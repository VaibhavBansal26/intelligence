# ADR 0004: Rank by shrunk rates and expected revenue uplift

Status: accepted (2026-10-19)

## Context
Raw conversion rates of low-traffic steps are noise: a step with 0 of 4 passes looks catastrophic. Ranking by rate gap would put noise at the top.

## Decision
Pass rate per step is shrunk with a per-role (entry, middle, final) Beta prior fitted by method of moments, optionally centred on a ridge prediction from funnel-structure content features. Expected uplift = visitors x max(0, benchmark - rate) x revenue per passer, with p10/p50/p90 from Beta-posterior draws (seeded). Funnels rank by their weakest step.

## Alternatives considered
Rank by raw gap (noise at the top); Wilson lower bounds only (conservative but no pooling); full hierarchical Bayes (heavier, little gain here).

## Consequences
Small real differences between thin steps are suppressed by design. With fewer than three peers in a role no benchmark and no uplift claim is made. Revenue per passer ignores orders not attributed to a step.
