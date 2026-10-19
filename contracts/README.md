# Contract JSON Schemas

Schemas for the Analytics API contract (`ANALYTICS-CONTRACT.md`, v1) as consumed by this service. They are
validated against the synthetic fixtures and the fake contract apps by `tests/contract/`. Unknown fields are
always allowed: consumers must ignore them (additive-only evolution within v1).
