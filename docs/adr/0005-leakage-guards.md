# ADR 0005: Point-in-time features and censoring guards

Status: accepted (2026-10-19)

## Context
Lead and conversation models are easy to make look excellent by leaking outcomes (future orders, final message counts, still-open label windows).

## Decision
Lead features use only events and rooms strictly before the opt-in moment; outcome-type fields of earlier rooms only if the room had closed by then. Only leads whose 14-day label window ended before the data end are trained on; converted leads without an order date are excluded. Train/validation is time ordered with an embargo equal to the horizon. Room features exclude every outcome column; unanswered rooms inside the SLA window are censored; unanswered rooms past it count as breaches.

## Alternatives considered
Random k-fold CV (leaks time); using final room counts (leaks outcome).

## Consequences
Lower but honest metrics. Newly opted-in leads must be scored from their opt-in-time features (that is what the API does).
