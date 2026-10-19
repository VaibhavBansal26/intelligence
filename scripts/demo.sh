#!/usr/bin/env bash
# End-to-end demo against the fake contract apps. Requires: server on :8000 (ENV=dev ADMIN_API_KEY=demo-admin
# ALLOW_PRIVATE_SOURCES=true) and `python -m app.synthetic.fake_sources --port 9101` running.
set -euo pipefail
API=${API:-http://127.0.0.1:8000}
ADMIN=${ADMIN_API_KEY:-demo-admin}
PEPPER=${PEPPER:-shared-pepper-for-tests}
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }

KEY=$(curl -s -X POST "$API/admin/accounts" -H "X-Admin-Key: $ADMIN" -H 'content-type: application/json' -d '{"name":"Demo Co"}' | j 'd["api_key"]')
H="Authorization: Bearer $KEY"
curl -s -X POST "$API/v1/sources" -H "$H" -H 'content-type: application/json' \
  -d "{\"kind\":\"funnels\",\"name\":\"funnels\",\"base_url\":\"http://127.0.0.1:9101\",\"api_key\":\"fa_test_key\",\"identity_pepper\":\"$PEPPER\"}" >/dev/null
curl -s -X POST "$API/v1/sources" -H "$H" -H 'content-type: application/json' \
  -d "{\"kind\":\"conversations\",\"name\":\"conversations\",\"base_url\":\"http://127.0.0.1:9102\",\"api_key\":\"ca_test_key\",\"identity_pepper\":\"$PEPPER\"}" >/dev/null
echo "== sync";  curl -s -X POST "$API/v1/sync?wait=true" -H "$H" -H 'content-type: application/json' -d '{}' | j '{"status":d["status"],"errors":d["result"]["errors"]}'
echo "== train"; curl -s -X POST "$API/v1/models/train?wait=true" -H "$H" -H 'content-type: application/json' -d '{}' | j 'd["status"]'
echo "== join";  curl -s "$API/v1/join" -H "$H" | j '{k:d[k] for k in ("joined_contacts","coverage_of_smaller_side","warnings")}'
echo "== top steps"; curl -s "$API/v1/rankings/steps" -H "$H" | j '[(s["rank"],s["funnel_id"],s["step_id"],s["expected_uplift"],s["uplift_p10"],s["uplift_p90"]) for s in d["steps"][:3]]'
echo "== top leads"; curl -s "$API/v1/leads/scores?limit=3" -H "$H" | j '(d["method"],[(i["rank"],i["score"],i["top_factors"][0]["feature"]) for i in d["items"]])'
echo "== open conversations"; curl -s "$API/v1/rankings/conversations/open?limit=3" -H "$H" | j '[(i["rank"],i["room_id"],i["priority_score"]) for i in d["items"]]'
echo "== anomalies"; curl -s "$API/v1/anomalies?since_days=60" -H "$H" | j '[(a["metric"],a["scope"],a["direction"],a["start"]) for a in d["alerts"][:4]]'
echo "== segments"; curl -s "$API/v1/segments" -H "$H" | j '[(s["name"],s["size"]) for s in d["segments"]]'
echo "== insights"; curl -s "$API/v1/insights" -H "$H" | j 'd["narrative"]["summary"]'
echo "== ask"; curl -s -X POST "$API/v1/ask" -H "$H" -H 'content-type: application/json' -d '{"question":"Which funnel should I fix first and why?"}' | j 'd["answer"]'
echo "== models"; curl -s "$API/v1/models" -H "$H" | j '[(m["model_type"],m["version"],m["status"],m["method"],m["metrics"].get("auc")) for m in d]'
