#!/usr/bin/env bash
# One-command local setup: starts the stack, loads credentials and workflows,
# publishes them and creates the editor owner account. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || cp .env.example .env
set -a; . ./.env; set +a
N8N="http://localhost:${N8N_HOST_PORT:-5678}"

echo "→ starting n8n and the mock services"
docker compose up -d

# /healthz answers before the database migrations finish; /healthz/readiness waits for them.
wait_for_n8n() {
  for _ in $(seq 1 90); do curl -fs "$N8N/healthz/readiness" >/dev/null 2>&1 && return 0; sleep 2; done
  echo "n8n did not become ready"; exit 1
}

echo "→ waiting for n8n"
wait_for_n8n

echo "→ loading credentials (values come from .env, never from the workflow files)"
creds=$(mktemp)
trap 'rm -f "$creds"' EXIT
python3 - "$creds" <<'PY'
import json, os, sys
def cred(cid, name, token):
    return {"id": cid, "name": name, "type": "httpHeaderAuth", "data": {"name": "Authorization", "value": f"Bearer {token}"}}
json.dump([
    cred("cpCredLlmKey0001", "LLM API key", os.environ.get("LLM_API_KEY", "mock-key")),
    cred("cpCredSlackBot01", "Slack bot token", os.environ.get("SLACK_BOT_TOKEN", "xoxb-mock")),
    cred("cpCredNotion0001", "Notion integration token", os.environ.get("NOTION_TOKEN", "secret_mock")),
], open(sys.argv[1], "w"))
PY
docker compose cp "$creds" n8n:/tmp/creds.json >/dev/null
docker compose exec -T n8n n8n import:credentials --input=/tmp/creds.json
docker compose exec -T -u root n8n rm -f /tmp/creds.json

echo "→ importing and publishing the workflows"
docker compose exec -T n8n n8n import:workflow --input=/workflows/error-handler.json
docker compose exec -T n8n n8n import:workflow --input=/workflows/content-pipeline.json
docker compose exec -T n8n n8n import:workflow --input=/workflows/api-intake.json
docker compose exec -T n8n n8n import:workflow --input=/workflows/ops-digest.json
for id in cpErrorHandler01 cpMainPipeline01 cpApiIntake00001 cpOpsDigest00001; do
  docker compose exec -T n8n n8n publish:workflow --id=$id
done
docker compose restart n8n >/dev/null
wait_for_n8n
for _ in $(seq 1 45); do  # webhooks register a moment after readiness; an empty request gets a 400
  [ "$(curl -s -o /dev/null -w "%{http_code}" -X POST "$N8N/webhook/content-request" -H 'Content-Type: application/json' -d '{}')" = 400 ] && break
  sleep 2
done

echo "→ creating the editor owner account (skipped if it already exists)"
owner_json="{\"email\":\"${N8N_OWNER_EMAIL:-demo@example.com}\",\"firstName\":\"Demo\",\"lastName\":\"Owner\",\"password\":\"${N8N_OWNER_PASSWORD:-ChangeMe-123}\"}"
owner="not created"
for _ in $(seq 1 15); do
  # n8n can answer 200 with its HTML app shell while the API is still starting: check the JSON body.
  body=$(curl -s -X POST "$N8N/rest/owner/setup" -H 'Content-Type: application/json' -d "$owner_json")
  case "$body" in
    *'"email"'*) owner="created"; break ;;
    *"already setup"*|*"already set up"*) owner="already exists"; break ;;
  esac
  sleep 2
done
echo "   owner account: $owner"

cat <<MSG

Ready.
  Request form     $N8N/form/new-content-request
  API endpoint     POST $N8N/webhook/content-request
  Daily digest     every weekday at 9:00, or now: curl -X POST $N8N/webhook/ops-digest
  n8n editor       $N8N   (${N8N_OWNER_EMAIL:-demo@example.com})
  Slack mock       http://localhost:${MOCK_HOST_PORT:-8080}/slack
  Tracker board    http://localhost:${MOCK_HOST_PORT:-8080}/board
MSG
