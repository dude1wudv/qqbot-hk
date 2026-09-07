#!/usr/bin/env bash
set -euo pipefail

project_dir="${QQBOT_PROJECT_DIR:-/opt/qqbot-hk}"

container_id="$(docker compose -f "$project_dir/docker-compose.yml" ps -q hermes-qqbot)"
test -n "$container_id"

state="$(docker inspect -f '{{.State.Status}}' "$container_id")"
health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
test "$state" = "running"
test "$health" = "healthy"

image_id="$(docker inspect -f '{{.Image}}' "$container_id")"
base_digest_label="$(docker image inspect -f '{{index .Config.Labels "io.qqbot-hk.hermes-base-digest"}}' "$image_id")"
audio_patch_label="$(docker image inspect -f '{{index .Config.Labels "io.qqbot-hk.audio-patch"}}' "$image_id")"
test "$base_digest_label" = "sha256:76d5d17a201bb623268c02d43e397925e8f0127eb29b2e00fc48632d74945b05"
test "$audio_patch_label" = "v1"

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


def assert_audio_route(path, content_type):
    request = urllib.request.Request(
        "http://sub2api:8080" + path,
        data=b"{}",
        headers={"Authorization": f"Bearer {key}", "Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 405}:
            raise SystemExit(f"audio route missing: {path}") from exc
        if exc.code >= 500:
            raise SystemExit(f"audio route unhealthy: {path} HTTP {exc.code}") from exc


assert_audio_route("/v1/audio/speech", "application/json")
assert_audio_route("/v1/audio/transcriptions", "application/json")
print("AUDIO_ROUTES=reachable")


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
docker exec hermes-qqbot python /opt/hermes/verify-hermes-audio.py --config /opt/data/config.yaml >/dev/null
docker exec -i hermes-qqbot python - <<'PY'
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from collections.abc import Mapping
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
if env.get("QQ_STT_PREFER_BUILTIN", "").lower() != "false":
    raise SystemExit("QQ built-in STT preference must be disabled")
for name in ("SUB2API_API_KEY", "QQ_STT_API_KEY", "VOICE_TOOLS_OPENAI_KEY"):
    if not env.get(name):
        raise SystemExit(f"required audio secret missing: {name}")
if env["QQ_STT_API_KEY"] != env["SUB2API_API_KEY"] or env["VOICE_TOOLS_OPENAI_KEY"] != env["SUB2API_API_KEY"]:
    raise SystemExit("audio secret wiring mismatch")
if env.get("QQ_STT_BASE_URL") or env.get("QQ_STT_MODEL"):
    raise SystemExit("QQ STT base URL/model must come from config.yaml, not environment")

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

plugin_settings = (
    (((config.get("plugins") or {}).get("entries") or {}).get("smart_group_qq") or {})
    .get("settings")
    or {}
)
if not isinstance(plugin_settings, Mapping):
    raise SystemExit("smart_group_qq settings are invalid")
memory_config = plugin_settings.get("memory")
if not isinstance(memory_config, Mapping):
    raise SystemExit("smart_group_qq memory config is missing")
for name in (
    "compact_after_messages", "max_history_rows", "recent_context_messages",
    "recent_context_seconds", "context_char_budget",
    "ambient_retention_days", "addressed_retention_days",
    "audit_retention_days", "claim_retention_days",
):
    try:
        if int(memory_config.get(name, 0)) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise SystemExit(f"smart_group_qq memory config is invalid: {name}")
ambient_config = plugin_settings.get("ambient")
if not isinstance(ambient_config, Mapping):
    raise SystemExit("smart_group_qq ambient config is missing")
if not isinstance(ambient_config.get("enabled"), bool):
    raise SystemExit("smart_group_qq ambient enabled flag is invalid")
if not isinstance(ambient_config.get("analyze_images"), bool):
    raise SystemExit("smart_group_qq ambient analyze_images flag is invalid")
for name in (
    "max_text_chars", "queue_max_size", "per_group_concurrency",
    "flush_interval_seconds", "context_window_messages", "context_window_seconds",
):
    try:
        if int(ambient_config.get(name, 0)) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise SystemExit(f"smart_group_qq ambient config is invalid: {name}")
if int(ambient_config.get("per_group_concurrency", 0)) != 1:
    raise SystemExit("smart_group_qq currently requires per_group_concurrency=1")
member_memory_config = plugin_settings.get("member_memory")
if not isinstance(member_memory_config, Mapping):
    raise SystemExit("smart_group_qq member_memory config is missing")
for name in ("enabled", "auto_extract", "extract_from_ambient"):
    if not isinstance(member_memory_config.get(name), bool):
        raise SystemExit(f"smart_group_qq member_memory {name} flag is invalid")
try:
    confidence = float(member_memory_config.get("min_confidence"))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError
except (TypeError, ValueError):
    raise SystemExit("smart_group_qq member_memory min_confidence is invalid")
for name in ("fact_retention_days", "max_profile_facts"):
    try:
        if int(member_memory_config.get(name, 0)) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise SystemExit(f"smart_group_qq member_memory config is invalid: {name}")
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
    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if schema_version < 2:
        raise SystemExit("plugin database schema version is too old")
    foreign_key_errors = list(connection.execute("PRAGMA foreign_key_check"))
    if foreign_key_errors:
        raise SystemExit("plugin database foreign-key check failed")
    audit_count = int(connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    required_tables = {
        "group_memories", "group_history", "knowledge_documents", "knowledge_chunks",
        "compaction_jobs", "group_members", "member_memory_facts",
    }
    if not required_tables.issubset(tables):
        raise SystemExit("plugin memory/knowledge schema is incomplete")
    memory_columns = {row[1] for row in connection.execute("PRAGMA table_info(group_memories)")}
    if not {"structured_json", "last_history_id", "model", "version"}.issubset(memory_columns):
        raise SystemExit("plugin AI memory migration is incomplete")
    compaction_columns = {row[1] for row in connection.execute("PRAGMA table_info(compaction_jobs)")}
    if not {"group_id", "from_history_id", "to_history_id", "status", "next_retry_at", "created_at", "updated_at"}.issubset(compaction_columns):
        raise SystemExit("compaction job schema is incomplete")
    profile_columns = {row[1] for row in connection.execute("PRAGMA table_info(group_members)")}
    if not {"group_id", "member_ref", "member_digest", "consent_status", "first_seen_at", "last_seen_at"}.issubset(profile_columns):
        raise SystemExit("group member schema is incomplete")
    fact_columns = {row[1] for row in connection.execute("PRAGMA table_info(member_memory_facts)")}
    if not {"group_id", "member_ref", "category", "fact_key", "fact_value", "confidence", "explicitness", "expires_at", "status", "created_at", "updated_at"}.issubset(fact_columns):
        raise SystemExit("member memory fact schema is incomplete")
    indexes = {
        row[1]
        for table in ("group_history", "group_memories", "compaction_jobs", "group_members", "member_memory_facts")
        for row in connection.execute(f"PRAGMA index_list({table})")
    }
    required_indexes = {
        "idx_compaction_jobs_ready", "idx_compaction_jobs_group", "idx_group_members_seen",
        "idx_member_facts_active", "idx_member_facts_key",
    }
    if not required_indexes.issubset(indexes):
        raise SystemExit("member memory indexes are incomplete")

gateway = json.loads(Path("/opt/data/gateway_state.json").read_text(encoding="utf-8"))
qq = gateway.get("platforms", {}).get("qqbot", {})
if gateway.get("gateway_state") != "running" or qq.get("state") != "connected":
    raise SystemExit("QQ gateway is not connected")
print(f"DM_POLICY=pairing API_BASE=production AUTO_PAIR={auto_pair_state} UNTIL_UTC={until_raw}")
print(f"GROUP_ALLOWLIST_COUNT={len(groups)}")
print(f"PLUGIN_STATUS={plugin.get('status')}")
print(f"OWNED_CRON_COUNT={len(owned)}")
print(f"PLUGIN_DB_INTEGRITY=ok AUDIT_COUNT={audit_count} SCHEMA_VERSION={schema_version}")
print("PLUGIN_MEMORY_KB_SCHEMA=ok")
print("MEMBER_MEMORY_SCHEMA=ok")
print("MEMBER_MEMORY_CONFIG=ok")
print("COMPACTION_SCHEMA=ok")
print("QQ_GATEWAY=connected")
print("AUDIO_ENV_WIRING=ok")
PY
docker exec hermes-qqbot hermes doctor >/tmp/hermes-qqbot-doctor.txt
echo "CONTAINER_STATE=$state"
echo "CONTAINER_HEALTH=$health"
echo "HERMES_BASE_DIGEST=verified"
echo "HERMES_AUDIO_PATCH=verified"
echo "CONFIG_CHECK=passed"
echo "AUDIO_SMOKE=passed"
echo "PLUGIN_CHECK=passed"
echo "DOCTOR=completed"
