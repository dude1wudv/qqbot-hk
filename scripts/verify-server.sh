#!/usr/bin/env bash
set -euo pipefail

project_dir="/opt/qqbot-hk"

container_id="$(docker compose -f "$project_dir/docker-compose.yml" ps -q hermes-qqbot)"
test -n "$container_id"

state="$(docker inspect -f '{{.State.Status}}' "$container_id")"
health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
test "$state" = "running"
test "$health" = "healthy"

docker exec -i hermes-qqbot python - <<'PY'
import json
import os
import urllib.request

key = os.environ.get("SUB2API_API_KEY", "")
if not key:
    env_path = "/opt/data/.env"
    with open(env_path, encoding="utf-8") as handle:
        for raw in handle:
            if raw.startswith("SUB2API_API_KEY="):
                key = raw.split("=", 1)[1].strip()
                break
if not key:
    raise SystemExit("missing Sub2API key")

request = urllib.request.Request(
    "http://sub2api:8080/v1/models",
    headers={"Authorization": f"Bearer {key}"},
)
with urllib.request.urlopen(request, timeout=20) as response:
    payload = json.load(response)
models = {str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict)}
for required in ("deepseek-v4-flash-0731", "gemini-3.8-flash-high", "gpt-5.6-luna"):
    if required not in models:
        raise SystemExit(f"missing model: {required}")
print("SUB2API_MODELS=deepseek-primary,gemini-fallback-1,luna-fallback-2")
PY

docker exec hermes-qqbot hermes config check >/dev/null
docker exec hermes-qqbot hermes doctor >/tmp/hermes-qqbot-doctor.txt
echo "CONTAINER_STATE=$state"
echo "CONTAINER_HEALTH=$health"
echo "CONFIG_CHECK=passed"
echo "DOCTOR=completed"
