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
chat_reasoning_patch_label="$(docker image inspect -f '{{index .Config.Labels "io.qqbot-hk.chat-reasoning-patch"}}' "$image_id")"
qq_help_patch_label="$(docker image inspect -f '{{index .Config.Labels "io.qqbot-hk.qq-help-patch"}}' "$image_id")"
test "$base_digest_label" = "sha256:9469b3e78b9545b6d576eb8887a95352e9a0ea83730eaf31431cf862ca1010e1"
test "$audio_patch_label" = "v1"
test "$chat_reasoning_patch_label" = "v1"
test "$qq_help_patch_label" = "v1"

docker exec -i hermes-qqbot python - <<'PY'
import json
import base64
import io
import os
import urllib.error
import urllib.request
from PIL import Image

env = {}
with open("/opt/data/.env", encoding="utf-8") as handle:
    for raw in handle:
        if "=" in raw and not raw.lstrip().startswith("#"):
            name, value = raw.split("=", 1)
            env[name.strip()] = value.strip()
key = os.environ.get("SUB2API_API_KEY") or env.get("SUB2API_API_KEY", "")
deepseek_key = os.environ.get("SUB2API_DEEPSEEK_API_KEY") or env.get("SUB2API_DEEPSEEK_API_KEY", "")
if not key or not deepseek_key:
    raise SystemExit("missing Sub2API key")

def post(path, body, api_key):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    request = urllib.request.Request(
        "http://sub2api:8080" + path,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
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

deepseek = post(
    "/v1/chat/completions",
    {
        "model": "deepseek/deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "Reply only OK"}],
        "reasoning_effort": "medium",
        "stream": False,
        "max_tokens": 256,
    },
    deepseek_key,
)
deepseek_choices = deepseek.get("choices") or []
deepseek_text = (
    ((deepseek_choices[0] if deepseek_choices else {}).get("message") or {}).get("content") or ""
).strip()
if "OK" not in deepseek_text.upper():
    raise SystemExit("deepseek/deepseek-v4.1-flash: unexpected response")
print("MODEL=deepseek/deepseek-v4.1-flash API_MODE=chat_completions EFFORT=medium RESULT=OK")

gemini = post(
    "/v1/chat/completions",
    {
        "model": "gemini-3.8-flash-high",
        "messages": [{"role": "user", "content": "Reply only OK"}],
        "reasoning_effort": "high",
        "stream": False,
        "max_tokens": 256,
    },
    key,
)
choices = gemini.get("choices") or []
content = (((choices[0] if choices else {}).get("message") or {}).get("content") or "").strip()
if "OK" not in content.upper():
    raise SystemExit("gemini-3.8-flash-high: unexpected response")
print("MODEL=gemini-3.8-flash-high EFFORT=high RESULT=OK")

image_buffer = io.BytesIO()
Image.new("RGB", (16, 16), (0, 120, 255)).save(image_buffer, format="PNG")
image_base64 = base64.b64encode(image_buffer.getvalue()).decode("ascii")
deepseek_vision = post(
    "/v1/chat/completions",
    {
        "model": "deepseek/deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Reply only IMAGE_OK if you can inspect this image."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}},
        ]}],
        "reasoning_effort": "medium",
        "stream": False,
        "max_tokens": 128,
    },
    deepseek_key,
)
vision_choices = deepseek_vision.get("choices") or []
vision_content = (
    ((vision_choices[0] if vision_choices else {}).get("message") or {}).get("content") or ""
).strip()
if not vision_content:
    raise SystemExit("deepseek/deepseek-v4.1-flash: invalid vision response")
print("VISION=deepseek/deepseek-v4.1-flash API_MODE=chat_completions RESULT=OK")
PY

docker exec hermes-qqbot hermes config check >/dev/null
docker exec hermes-qqbot hermes plugins doctor /opt/data/plugins/smart_group_qq --ci >/dev/null
docker exec hermes-qqbot python /opt/hermes/verify-hermes-audio.py --config /opt/data/config.yaml >/dev/null
docker exec hermes-qqbot python /opt/hermes/verify-hermes-chat-reasoning.py >/dev/null
docker exec hermes-qqbot python /opt/hermes/verify-hermes-qq-commands.py --config /opt/data/config.yaml >/dev/null
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
for name in ("SUB2API_API_KEY", "SUB2API_DEEPSEEK_API_KEY", "QQ_STT_API_KEY", "VOICE_TOOLS_OPENAI_KEY"):
    if not env.get(name):
        raise SystemExit(f"required Sub2API secret missing: {name}")
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
model_config = config.get("model")
if not isinstance(model_config, Mapping):
    raise SystemExit("model config is missing")
if model_config.get("provider") != "sub2api_deepseek":
    raise SystemExit("primary model provider must be sub2api_deepseek")
if model_config.get("default") != "deepseek/deepseek-v4.1-flash":
    raise SystemExit("primary model must be deepseek/deepseek-v4.1-flash")
providers = config.get("providers") or {}
deepseek_provider = providers.get("sub2api_deepseek") or {}
if deepseek_provider.get("key_env") != "SUB2API_DEEPSEEK_API_KEY":
    raise SystemExit("DeepSeek provider key wiring is invalid")
if deepseek_provider.get("api_mode") != "chat_completions":
    raise SystemExit("DeepSeek provider must use chat_completions")
agent_config = config.get("agent")
if not isinstance(agent_config, Mapping) or agent_config.get("image_input_mode") != "native":
    raise SystemExit("DeepSeek image input must use native content parts")
if agent_config.get("reasoning_effort") != "medium":
    raise SystemExit("agent.reasoning_effort must be medium")
reasoning_overrides = agent_config.get("reasoning_overrides") or {}
if reasoning_overrides.get("deepseek/deepseek-v4.1-flash") != "medium":
    raise SystemExit("DeepSeek reasoning override must be medium")
if config.get("fallback_providers"):
    raise SystemExit("automatic fallback providers must be disabled")

compression_config = config.get("compression")
if not isinstance(compression_config, Mapping):
    raise SystemExit("compression config is missing")
if compression_config.get("enabled") is not True:
    raise SystemExit("compression.enabled must be true")
if (
    type(compression_config.get("threshold_tokens")) is not int
    or compression_config.get("threshold_tokens") != 80000
):
    raise SystemExit("compression.threshold_tokens must be exactly 80000")

auxiliary = config.get("auxiliary")
if not isinstance(auxiliary, Mapping):
    raise SystemExit("auxiliary config is missing")
compression_route = auxiliary.get("compression")
if not isinstance(compression_route, Mapping):
    raise SystemExit("auxiliary.compression route is missing")
if compression_route.get("provider") != "custom":
    raise SystemExit("auxiliary.compression.provider must be custom")
if compression_route.get("model") != "deepseek/deepseek-v4.1-flash":
    raise SystemExit("auxiliary.compression.model must be deepseek/deepseek-v4.1-flash")
if compression_route.get("base_url") != "http://sub2api:8080/v1":
    raise SystemExit("auxiliary.compression.base_url is invalid")
if compression_route.get("key_env") != "SUB2API_DEEPSEEK_API_KEY":
    raise SystemExit("auxiliary.compression.key_env must be SUB2API_DEEPSEEK_API_KEY")
if compression_route.get("api_mode") != "chat_completions":
    raise SystemExit("auxiliary.compression.api_mode must be chat_completions")
if compression_route.get("reasoning_effort") != "low":
    raise SystemExit("auxiliary.compression.reasoning_effort must be low")
if auxiliary.get("vision"):
    raise SystemExit("auxiliary vision fallback must be disabled")
print(
    "COMPRESSION_CONFIG=enabled THRESHOLD_TOKENS=80000 "
    "MODEL=deepseek/deepseek-v4.1-flash API_MODE=chat_completions REASONING_EFFORT=low"
)
tools = (((config.get("platform_toolsets") or {}).get("qqbot") or []))
if "terminal" not in tools or "file" not in tools:
    raise SystemExit("QQ terminal/file toolset is not enabled")
if {"code", "computer"}.intersection(tools):
    raise SystemExit("QQ toolset exposes an unintended execution tool")
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
    if schema_version != 3:
        raise SystemExit("plugin database schema version does not match this release")
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
        "group_memory_epochs",
    }
    if not required_tables.issubset(tables):
        raise SystemExit("plugin memory/knowledge schema is incomplete")
    memory_columns = {row[1] for row in connection.execute("PRAGMA table_info(group_memories)")}
    if not {"structured_json", "last_history_id", "model", "version"}.issubset(memory_columns):
        raise SystemExit("plugin AI memory migration is incomplete")
    epoch_columns = {row[1] for row in connection.execute("PRAGMA table_info(group_memory_epochs)")}
    if not {"group_id", "epoch"}.issubset(epoch_columns):
        raise SystemExit("group memory invalidation schema is incomplete")
    compaction_columns = {row[1] for row in connection.execute("PRAGMA table_info(compaction_jobs)")}
    if not {"group_id", "from_history_id", "to_history_id", "status", "next_retry_at", "created_at", "updated_at"}.issubset(compaction_columns):
        raise SystemExit("compaction job schema is incomplete")
    profile_columns = {row[1] for row in connection.execute("PRAGMA table_info(group_members)")}
    if not {"group_id", "member_ref", "member_digest", "consent_status", "first_seen_at", "last_seen_at"}.issubset(profile_columns):
        raise SystemExit("group member schema is incomplete")
    fact_columns = {row[1] for row in connection.execute("PRAGMA table_info(member_memory_facts)")}
    if not {"group_id", "member_ref", "category", "fact_key", "fact_value", "confidence", "explicitness", "source_history_id", "evidence", "expires_at", "status", "created_at", "updated_at"}.issubset(fact_columns):
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
echo "HERMES_CHAT_REASONING_PATCH=verified"
echo "HERMES_QQ_HELP_PATCH=verified"
echo "QQ_NATIVE_COMMANDS=verified"
echo "CHAT_COMPLETIONS_ROUTE=verified"
echo "CONFIG_CHECK=passed"
echo "AUDIO_SMOKE=passed"
echo "PLUGIN_CHECK=passed"
echo "DOCTOR=completed"
