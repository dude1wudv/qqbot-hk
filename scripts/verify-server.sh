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
import urllib.error
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

def post(path, body):
    request = urllib.request.Request(
        "http://sub2api:8080" + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{body.get('model')}: HTTP {exc.code}") from exc


for model, effort in (
    ("deepseek-v4-flash-0731", "low"),
    ("gemini-3.8-flash-high", "high"),
):
    payload = post(
        "/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply only OK"}],
            "reasoning_effort": effort,
            "stream": False,
            "max_tokens": 256,
        },
    )
    choices = payload.get("choices") or []
    content = (((choices[0] if choices else {}).get("message") or {}).get("content") or "").strip()
    if "OK" not in content.upper():
        raise SystemExit(f"{model}: unexpected response")
    print(f"MODEL={model} EFFORT={effort} RESULT=OK")

luna = post(
    "/v1/responses",
    {
        "model": "gpt-5.6-luna",
        "input": "Reply only OK",
        "reasoning": {"effort": "medium"},
        "stream": False,
        "max_output_tokens": 256,
    },
)
if not luna.get("id") or luna.get("error"):
    raise SystemExit("gpt-5.6-luna: invalid Responses payload")
print("MODEL=gpt-5.6-luna EFFORT=medium RESULT=OK")
PY

docker exec hermes-qqbot hermes config check >/dev/null
docker exec hermes-qqbot hermes doctor >/tmp/hermes-qqbot-doctor.txt
echo "CONTAINER_STATE=$state"
echo "CONTAINER_HEALTH=$health"
echo "CONFIG_CHECK=passed"
echo "DOCTOR=completed"
