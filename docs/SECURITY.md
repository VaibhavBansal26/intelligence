# Security

## Threat model summary
| Asset | Threat | Mitigation |
|---|---|---|
| Source API keys and identity peppers | Database leak | Fernet encryption at rest with a KEK from the environment; rotation via comma separated keys; never in responses or logs |
| Contact e-mails in a bridge list | Retention or leakage of personal data | `POST /v1/identity/bridge` hashes each address in memory with every source's pepper and keeps only the resulting HMAC pairs (`contact_links`); addresses are never stored; request bodies are not logged (the request log has method, path, status and timing only); body size is capped by `MAX_BODY_BYTES` (1 MB, about 25,000 addresses per call); `sources:manage` permission required |
| Tenant data | Cross-account reads | Every service call takes the principal's `account_id`; ids from another account return 404 (not 403); covered by `tests/api/test_api.py::test_cross_account_access_is_404_everywhere` |
| Service as a proxy | SSRF through source URLs | Scheme allow-list, DNS resolution and private/loopback/link-local/multicast/reserved (including IPv4-mapped IPv6) rejection, redirects never followed, https required in production, re-validated on every update |
| API keys | Theft or brute force | Only SHA-256 stored; shown once; scopes narrow a role; expiry, revocation, last-used; per-key and per-IP token buckets |
| Admin plane | Tenant escalation | Separate `X-Admin-Key`, constant-time compare; tenant keys never work on `/admin` |
| LLM | Prompt injection via funnel/step names or the question | Free text is sanitised (`< > ` { } [ ]` removed, length-capped), delimited in `<untrusted>` tags and declared as data; digits inside untrusted text never legitimise numbers; tools are read-only and account-bound; output must pass schema + NumberGuard |
| LLM cost | Runaway spend | Input budget, identical-prompt cache, per-account daily call and token caps |
| Model artifacts | Tampering (pickle execution) | sha256 recorded at registration and verified before every load; the model directory must be writable only by the service |
| Logs and metrics | Secret or PII leakage | JSON logger redacts secret-looking fields; metrics labels are route templates, not ids; optional `METRICS_TOKEN` |
| HTTP surface | Oversize bodies, header issues | Body size limit, security headers (`nosniff`, CSP `default-src 'none'`, no-store), CORS allow-list |

## Data handled
Opaque ids, HMAC contact keys, UTM labels, device class, country code, order amounts, conversation metadata
(timing, counts, AI labels). Never message text, names, emails or phone numbers ([contract](../contracts/README.md) forbids them).

## Authentication and authorization
Roles `admin | analyst | viewer` map to permissions in `app/security/rbac.py`; each route declares the permission it
needs (`require(...)`). A test enumerates every `/v1` route in the OpenAPI document and asserts 401 without credentials,
and an RBAC matrix test covers each role against representative routes.

## Known gaps (honest list)
- Rate limiting is per instance and in memory.
- DNS rebinding between validation and connection is mitigated (no redirects, validation at each update) but not eliminated;
  use an egress allow-list at the network layer in production.
- There is no automatic data retention job and no audit log table yet.
- Pickled model artifacts are only as trustworthy as the volume they live on.
- Dependencies are not yet scanned in CI.

## Reporting issues
Email the maintainers (see repository settings) with a description and reproduction. Do not open public issues for vulnerabilities.
