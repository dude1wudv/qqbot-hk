#!/usr/bin/env bash
set -euo pipefail

project_dir="${QQBOT_PROJECT_DIR:-/opt/qqbot-hk}"

container_id="$(docker compose -f "$project_dir/docker-compose.yml" ps -q hermes-qqbot)"
test -n "$container_id"

state="$(docker inspect -f '{{.State.Status}}' "$container_id")"
health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
test "$state" = "running"
test "$health" = "healthy"

docker exec -i hermes-qqbot python - <<'PY'
import json
import base64
import io
import os
import urllib.error
import urllib.request
from PIL import Image

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

image_buffer = io.BytesIO()
Image.new("RGB", (16, 16), (0, 120, 255)).save(image_buffer, format="PNG")
image_url = "data:image/png;base64," + base64.b64encode(image_buffer.getvalue()).decode("ascii")

gemini_vision = post(
    "/v1/chat/completions",
    {
        "model": "gemini-3.8-flash-high",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Reply only IMAGE_OK if you can inspect this image."},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]}],
        "stream": False,
        "max_tokens": 128,
    },
)
if not (gemini_vision.get("choices") or []):
    raise SystemExit("gemini-3.8-flash-high: invalid vision response")
print("VISION=gemini-3.8-flash-high RESULT=OK")

luna_vision = post(
    "/v1/responses",
    {
        "model": "gpt-5.6-luna",
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": "Reply only IMAGE_OK if you can inspect this image."},
            {"type": "input_image", "image_url": image_url},
        ]}],
        "stream": False,
        "max_output_tokens": 128,
    },
)
if not luna_vision.get("id") or luna_vision.get("error"):
    raise SystemExit("gpt-5.6-luna: invalid vision response")
print("VISION=gpt-5.6-luna RESULT=OK")
PY

docker exec hermes-qqbot hermes config check >/dev/null
docker exec hermes-qqbot hermes plugins doctor /opt/data/plugins/smart_group_qq --ci >/dev/null
docker exec -i hermes-qqbot python - <<'PY'
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from datetime import datetime, timezone

required = [
    Path("/opt/data/SOUL.md"),
    Path("/opt/data/plugins/smart_group_qq/plugin.yaml"),
    Path("/opt/data/plugins/smart_group_qq/__init__.py"),
    Path("/opt/data/smart-group-schedules.yaml"),
    Path("/opt/data/scripts/reconcile-smart-group-cron.py"),
]
for path in required:
    if not path.is_file():
        raise SystemExit("required runtime file missing")
    stat = path.stat()
    if stat.st_uid != 10000 or stat.st_gid != 10000:
        raise SystemExit("runtime file ownership mismatch")

env = {}
for raw in Path("/opt/data/.env").read_text(encoding="utf-8").splitlines():
    if "=" in raw and not raw.lstrip().startswith("#"):
        name, value = raw.split("=", 1)
        env[name.strip()] = value.strip()
groups = tuple(dict.fromkeys(item.strip() for item in env.get("QQ_GROUP_ALLOWED_USERS", "").split(",") if item.strip()))
if not groups:
    raise SystemExit("QQ group allow-list is empty")

plugins = json.loads(subprocess.check_output(["hermes", "plugins", "list", "--json", "--no-bundled"], text=True))
items = plugins if isinstance(plugins, list) else plugins.get("plugins", [])
plugin = next((item for item in items if item.get("name") == "smart_group_qq"), None)
if plugin is None or plugin.get("status") not in {"enabled", "loaded"}:
    raise SystemExit("smart_group_qq is not enabled")

import yaml
config = yaml.safe_load(Path("/opt/data/config.yaml").read_text(encoding="utf-8")) or {}
qq_extra = (((config.get("platforms") or {}).get("qqbot") or {}).get("extra") or {})
if qq_extra.get("dm_policy") != "pairing":
    raise SystemExit("QQ DM policy is not pairing")
if qq_extra.get("group_policy") != "allowlist":
    raise SystemExit("QQ group policy is not allowlist")
if "QQ_SANDBOX" in env:
    raise SystemExit("deprecated QQ sandbox routing must not be enabled")
if env.get("QQ_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"}:
    raise SystemExit("QQ allow-all bypass must be disabled while pairing is active")

from gateway.platforms.qqbot.constants import API_BASE
if API_BASE.rstrip("/") != "https://api.sgroup.qq.com":
    raise SystemExit("QQ API base is not the production gateway")

auto_pair = (((config.get("plugins") or {}).get("entries") or {}).get("smart_group_qq") or {}).get("settings", {}).get("auto_pair", {})
until_raw = str(auto_pair.get("until_utc") or "")
try:
    until = datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
except ValueError as exc:
    raise SystemExit("QQ auto-pair deadline is invalid") from exc
if until.tzinfo is None or not auto_pair.get("enabled"):
    raise SystemExit("QQ auto-pair window is not configured")
auto_pair_state = "active" if datetime.now(timezone.utc) < until.astimezone(timezone.utc) else "expired"

declaration = yaml.safe_load(Path("/opt/data/smart-group-schedules.yaml").read_text(encoding="utf-8")) or {}
enabled_ids = {
    item.get("id") for item in declaration.get("schedules", [])
    if isinstance(item, dict) and item.get("enabled") is True
}
from cron import jobs as cron_jobs
owned = [job for job in cron_jobs.list_jobs(include_disabled=True) if str(job.get("name", "")).startswith("smart-group-qq::")]
expected = len(enabled_ids) * len(groups)
if len(owned) != expected:
    raise SystemExit("owned cron count mismatch")
if any(job.get("no_agent") is not True or not str(job.get("deliver", "")).startswith("qqbot:") for job in owned):
    raise SystemExit("owned cron safety mismatch")

db_path = Path("/opt/data/plugin-data/smart_group_qq/data.db")
if not db_path.is_file():
    raise SystemExit("plugin database is missing")
with sqlite3.connect(db_path) as connection:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("plugin database integrity check failed")
    audit_count = int(connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    required_tables = {"group_memories", "group_history", "knowledge_documents", "knowledge_chunks"}
    if not required_tables.issubset(tables):
        raise SystemExit("plugin memory/knowledge schema is incomplete")
    memory_columns = {row[1] for row in connection.execute("PRAGMA table_info(group_memories)")}
    if not {"structured_json", "last_history_id", "model", "version"}.issubset(memory_columns):
        raise SystemExit("plugin AI memory migration is incomplete")

gateway = json.loads(Path("/opt/data/gateway_state.json").read_text(encoding="utf-8"))
qq = gateway.get("platforms", {}).get("qqbot", {})
if gateway.get("gateway_state") != "running" or qq.get("state") != "connected":
    raise SystemExit("QQ gateway is not connected")
print(f"DM_POLICY=pairing API_BASE=production AUTO_PAIR={auto_pair_state} UNTIL_UTC={until_raw}")
print(f"GROUP_ALLOWLIST_COUNT={len(groups)}")
print(f"PLUGIN_STATUS={plugin.get('status')}")
print(f"OWNED_CRON_COUNT={len(owned)}")
print(f"PLUGIN_DB_INTEGRITY=ok AUDIT_COUNT={audit_count}")
print("PLUGIN_MEMORY_KB_SCHEMA=ok")
print("QQ_GATEWAY=connected")
PY
docker exec hermes-qqbot hermes doctor >/tmp/hermes-qqbot-doctor.txt
echo "CONTAINER_STATE=$state"
echo "CONTAINER_HEALTH=$health"
echo "CONFIG_CHECK=passed"
echo "PLUGIN_CHECK=passed"
echo "DOCTOR=completed"
