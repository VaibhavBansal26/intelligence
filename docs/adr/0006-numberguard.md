# ADR 0006: LLM text must be grounded: NumberGuard and template fallback

Status: accepted (2026-10-19)

## Context
A prioritised action plan with an invented figure is actively harmful, and the model sees untrusted names from customer systems.

## Decision
The LLM receives structured evidence and may only restate its numbers. Output must parse against a schema, reference known finding ids, and every number must be derivable from the evidence (exact, rounded, ratio-as-percent, k/M abbreviation, ids, ranks up to the number of findings). Digits inside untrusted names do not count. Any failure (timeout, 5xx, refusal, empty, malformed, invented number) returns the deterministic TemplateNarrator with the reason.

## Alternatives considered
Trust the model; ask it to cite sources only; verify manually. All weaker or not automatic.

## Consequences
Occasionally rejects a correct but derived number (for example a ratio the model computed). The template is intentionally plain.
