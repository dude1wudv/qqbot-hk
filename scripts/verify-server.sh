#!/usr/bin/env bash
set -euo pipefail

project_dir="${QQBOT_PROJECT_DIR:-/opt/qqbot-hk}"


container_id="$(docker compose -f "$project_dir/docker-compose.yml" ps -q hermes-qqbot)"
test -n "$container_id"

state="$(docker inspect -f '{{.State.Status}}' "$container_id")"
health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
test "$state" = "running"
test "$health" = "healthy"

# A healthy gateway process alone does not prove the plugin timer was started.
started_at="$(docker inspect -f '{{.State.StartedAt}}' "$container_id")"
docker exec -i "$container_id" python - "$started_at" <<'PY'
import datetime
import sqlite3
import sys
import time

started = datetime.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")).timestamp()
db = sqlite3.connect("file:/opt/data/plugin-data/smart_group_qq/data.db?mode=ro", uri=True)
deadline = time.monotonic() + 60
while True:
    if db.execute("SELECT 1 FROM audit_events WHERE event_type='maintenance_ready' AND created_at>=? LIMIT 1", (started,)).fetchone():
        print("smart_group_qq maintenance cycle: OK")
        break
    if time.monotonic() >= deadline:
        raise SystemExit("smart_group_qq maintenance cycle did not complete after startup")
    time.sleep(2)
db.close()
PY

image_id="$(docker inspect -f '{{.Image}}' "$container_id")"
base_digest_label="$(docker image inspect -f '{{index .Config.Labels "io.qqbot-hk.hermes-base-digest"}}' "$image_id")"
audio_patch_label="$(docker image inspect -f '{{index .Config.Labels "io.qqbot-hk.audio-patch"}}' "$image_id")"
chat_reasoning_patch_label="$(docker image inspect -f '{{index .Config.Labels "io.qqbot-hk.chat-reasoning-patch"}}' "$image_id")"
compression_recovery_patch_label="$(docker image inspect -f '{{index .Config.Labels "io.qqbot-hk.compression-recovery-patch"}}' "$image_id")"
qq_help_patch_label="$(docker image inspect -f '{{index .Config.Labels "io.qqbot-hk.qq-help-patch"}}' "$image_id")"
qq_output_patch_label="$(docker image inspect -f '{{index .Config.Labels "io.qqbot-hk.qq-output-patch"}}' "$image_id")"
test "$base_digest_label" = "sha256:9469b3e78b9545b6d576eb8887a95352e9a0ea83730eaf31431cf862ca1010e1"
test "$audio_patch_label" = "v2"
test "$chat_reasoning_patch_label" = "v2"
test "$compression_recovery_patch_label" = "v1"
test "$qq_help_patch_label" = "v1"
test "$qq_output_patch_label" = "v1"

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
dialogue_key = os.environ.get("SUB2API_DIALOGUE_API_KEY") or env.get("SUB2API_DIALOGUE_API_KEY", "")
if not key or not deepseek_key or not dialogue_key:
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

muse = post(
    "/v1/chat/completions",
    {
        "model": "meta/muse-spark-1.3-contributor",
        "messages": [{"role": "user", "content": "Reply only OK"}],
        "reasoning_effort": "xhigh",
        "stream": False,
        "max_tokens": 1024,
    },
    dialogue_key,
)
muse_choices = muse.get("choices") or []
muse_text = (((muse_choices[0] if muse_choices else {}).get("message") or {}).get("content") or "")
if "OK" not in muse_text.upper():
    raise SystemExit("meta/muse-spark-1.3-contributor: unexpected response")
print("MODEL=meta/muse-spark-1.3-contributor API_MODE=chat_completions EFFORT=xhigh RESULT=OK")

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
docker exec hermes-qqbot python /opt/hermes/verify-hermes-compression-recovery.py >/dev/null
docker exec hermes-qqbot python /opt/hermes/verify-hermes-qq-commands.py --config /opt/data/config.yaml >/dev/null
docker exec hermes-qqbot python /opt/hermes/verify-hermes-qq-output.py >/dev/null
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
groups = tuple(dict.fromkeys(item.strip() for item in env.get("QQ_SCHEDULE_GROUPS", "").split(",") if item.strip()))
if not groups:
    raise SystemExit("QQ schedule target list is empty")
if "QQ_GROUP_ALLOWED_USERS" in env:
    raise SystemExit("deprecated QQ_GROUP_ALLOWED_USERS must be absent")
for name in ("SUB2API_API_KEY", "SUB2API_DEEPSEEK_API_KEY", "SUB2API_DIALOGUE_API_KEY"):
    if not env.get(name):
        raise SystemExit(f"required Sub2API secret missing: {name}")
for name in (
    "QQ_STT_PREFER_BUILTIN", "QQ_STT_API_KEY", "QQ_STT_BASE_URL", "QQ_STT_MODEL",
    "VOICE_TOOLS_OPENAI_KEY",
):
    if env.get(name):
        raise SystemExit(f"disabled voice environment variable must be absent: {name}")

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
if model_config.get("provider") != "sub2api_dialogue":
    raise SystemExit("primary model provider must be sub2api_dialogue")
if model_config.get("default") != "meta/muse-spark-1.3-contributor":
    raise SystemExit("primary model must be meta/muse-spark-1.3-contributor")
providers = config.get("providers") or {}
deepseek_provider = providers.get("sub2api_deepseek") or {}
if deepseek_provider.get("key_env") != "SUB2API_DEEPSEEK_API_KEY":
    raise SystemExit("DeepSeek provider key wiring is invalid")
if deepseek_provider.get("api_mode") != "chat_completions":
    raise SystemExit("DeepSeek provider must use chat_completions")
agent_config = config.get("agent")
if not isinstance(agent_config, Mapping) or agent_config.get("image_input_mode") != "native":
    raise SystemExit("image input must use native content parts")
if agent_config.get("reasoning_effort") != "low":
    raise SystemExit("agent.reasoning_effort must be low")
reasoning_overrides = agent_config.get("reasoning_overrides") or {}
if reasoning_overrides.get("deepseek/deepseek-v4.1-flash") != "medium":
    raise SystemExit("DeepSeek reasoning override must be medium")
for model in ("meta/muse-spark-1.3-contributor", "xiaomi/mimo-v2.6-flash"):
    if reasoning_overrides.get(model) != ("low" if model == "meta/muse-spark-1.3-contributor" else "xhigh"):
        raise SystemExit("new model reasoning override is invalid")
    if model not in (providers.get("sub2api_dialogue", {}).get("models") or {}):
        raise SystemExit("new model provider registration missing")
if providers.get("sub2api_dialogue", {}).get("key_env") != "SUB2API_DIALOGUE_API_KEY":
    raise SystemExit("dialogue provider key wiring is invalid")
if config.get("fallback_providers"):
    raise SystemExit("automatic fallback providers must be disabled")

compression_config = config.get("compression")
if not isinstance(compression_config, Mapping):
    raise SystemExit("compression config is missing")
if compression_config.get("enabled") is not True:
    raise SystemExit("compression.enabled must be true")
if (
    type(compression_config.get("threshold_tokens")) is not int
    or compression_config.get("threshold_tokens") != 100000
):
    raise SystemExit("compression.threshold_tokens must be exactly 100000")

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
    "COMPRESSION_CONFIG=enabled THRESHOLD_TOKENS=100000 "
    "MODEL=deepseek/deepseek-v4.1-flash API_MODE=chat_completions REASONING_EFFORT=low"
)
tools = (((config.get("platform_toolsets") or {}).get("qqbot") or []))
if "terminal" not in tools or "file" not in tools:
    raise SystemExit("QQ terminal/file toolset is not enabled")
if {"code", "computer", "tts"}.intersection(tools):
    raise SystemExit("QQ toolset exposes an unintended execution tool")
qq_extra = (((config.get("platforms") or {}).get("qqbot") or {}).get("extra") or {})
if qq_extra.get("dm_policy") != "pairing":
    raise SystemExit("QQ DM policy is not pairing")
if qq_extra.get("group_policy") != "allowlist":
    raise SystemExit("QQ group policy is not allowlist")
if qq_extra.get("group_allow_from") != ["*"]:
    raise SystemExit("QQ adapter group_allow_from must be exactly ['*']")
if qq_extra.get("group_allowed_chats") != ["*"]:
    raise SystemExit("gateway group_allowed_chats must be exactly ['*']")
if qq_extra.get("auto_new_on_compression_ineffective") is not True:
    raise SystemExit("QQ compression breaker auto-new recovery must be enabled")
if qq_extra.get("voice_input_enabled") is not False:
    raise SystemExit("QQ voice input must be disabled")
if qq_extra.get("voice_output_enabled") is not False:
    raise SystemExit("QQ voice output must be disabled")
if not isinstance(qq_extra.get("stt"), Mapping) or qq_extra["stt"].get("enabled") is not False:
    raise SystemExit("QQ STT must be explicitly disabled")
if config.get("tts"):
    raise SystemExit("TTS provider config must be absent")


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
    "compact_after_messages", "summary_min_interval_seconds",
    "idle_min_pending_messages", "summary_input_char_budget",
    "max_compaction_batches", "max_history_rows", "recent_context_messages",
    "context_char_budget", "ambient_retention_days", "addressed_retention_days",
    "audit_retention_days", "claim_retention_days",
):
    try:
        if int(memory_config.get(name, 0)) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise SystemExit(f"smart_group_qq memory config is invalid: {name}")
section_chars = memory_config.get("context_section_chars")
if not isinstance(section_chars, Mapping) or {
    "recent": 1600, "summary": 800, "member": 600, "knowledge": 1000
} != {name: section_chars.get(name) for name in ("recent", "summary", "member", "knowledge")}:
    raise SystemExit("smart_group_qq memory.context_section_chars is invalid")
if int(memory_config.get("compact_after_messages", 0)) != 40:
    raise SystemExit("smart_group_qq memory.compact_after_messages must be 40")
ambient_config = plugin_settings.get("ambient")
if not isinstance(ambient_config, Mapping):
    raise SystemExit("smart_group_qq ambient config is missing")
if not isinstance(ambient_config.get("enabled"), bool):
    raise SystemExit("smart_group_qq ambient enabled flag is invalid")
if not isinstance(ambient_config.get("analyze_images"), bool):
    raise SystemExit("smart_group_qq ambient analyze_images flag is invalid")
if ambient_config.get("analyze_images") is not False:
    raise SystemExit("smart_group_qq ambient.analyze_images must be false")
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
if int(ambient_config.get("context_window_messages", 0)) != 20:
    raise SystemExit("smart_group_qq ambient.context_window_messages must be 20")
participation_config = ambient_config.get("participation")
if not isinstance(participation_config, Mapping):
    raise SystemExit("smart_group_qq ambient.participation config is missing")
if participation_config.get("enabled") is not True:
    raise SystemExit("smart_group_qq ambient.participation.enabled must be true")
try:
    if int(participation_config.get("cooldown_seconds", 0)) != 0:
        raise ValueError
except (TypeError, ValueError):
    raise SystemExit("smart_group_qq ambient.participation.cooldown_seconds must be 0")
try:
    if int(participation_config.get("debounce_seconds", 0)) != 2:
        raise ValueError
    if int(participation_config.get("max_wait_seconds", 0)) != 5:
        raise ValueError
    if "batch_seconds" in participation_config:
        raise ValueError
except (TypeError, ValueError):
    raise SystemExit("smart_group_qq participation debounce/max-wait config is invalid")
try:
    for name in ("max_age_seconds", "timeout_seconds"):
        if int(participation_config.get(name, 0)) <= 0:
            raise ValueError
except (TypeError, ValueError):
    raise SystemExit("smart_group_qq ambient.participation age/timeout config is invalid")
try:
    participation_confidence = float(participation_config.get("min_confidence"))
    if abs(participation_confidence - 0.55) > 1e-9:
        raise ValueError
except (TypeError, ValueError):
    raise SystemExit("smart_group_qq ambient.participation.min_confidence must be 0.55")
wake_words = participation_config.get("wake_words")
if not isinstance(wake_words, list) or not any(str(item).strip() for item in wake_words):
    raise SystemExit("smart_group_qq ambient.participation.wake_words must be a non-empty list")
display_qq = (((config.get("display") or {}).get("platforms") or {}).get("qqbot") or {})
expected_display = {
    "streaming": False,
    "tool_progress": "off",
    "interim_assistant_messages": False,
    "thinking_progress": False,
    "show_reasoning": False,
    "long_running_notifications": False,
    "busy_ack_detail": False,
}
if any(display_qq.get(name) != value for name, value in expected_display.items()):
    raise SystemExit("QQ unverified intermediate output must be disabled")
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
    if schema_version != 4:
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
        "group_memory_epochs", "character_state", "character_items",
        "character_relations", "character_commands",
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
from gateway.config import Platform
from gateway.platforms.qqbot.adapter import QQAdapter
from gateway.run import GatewayRunner, load_gateway_config_for_runner
from gateway.session import SessionSource


# Exercise the real adapter and central GatewayRunner gates with a synthetic source.
# The profile loader resolves secrets in the normal scope, but no secret is printed.
loaded_config = load_gateway_config_for_runner()
qq_config = loaded_config.platforms[Platform.QQBOT]
runner = GatewayRunner(loaded_config)
adapter = QQAdapter(qq_config)
runner.adapters[Platform.QQBOT] = adapter
unknown_group = "synthetic-group-not-in-env"
group_source = SessionSource(
    Platform.QQBOT,
    unknown_group,
    chat_type="group",
    user_id="synthetic-member",
)
dm_source = SessionSource(
    Platform.QQBOT,
    "synthetic-unapproved-dm",
    chat_type="dm",
    user_id="synthetic-unapproved-dm",
)
if unknown_group in groups:
    raise SystemExit("synthetic group unexpectedly appears in QQ_SCHEDULE_GROUPS")
if adapter._is_group_allowed(unknown_group, group_source.user_id) is not True:
    raise SystemExit("QQ adapter wildcard did not authorize the synthetic group")
if runner._is_user_authorized(group_source) is not True:
    raise SystemExit("GatewayRunner did not authorize the synthetic group")
if adapter._is_dm_allowed(dm_source.user_id) is not False:
    raise SystemExit("QQ adapter admitted an unapproved DM")
if runner._is_user_authorized(dm_source) is not False:
    raise SystemExit("GatewayRunner admitted an unapproved DM")
print("QQ_AUTH_SMOKE=passed GROUP=adapter_acl+central_authorized DM=unapproved_refused")


gateway = json.loads(Path("/opt/data/gateway_state.json").read_text(encoding="utf-8"))
qq = gateway.get("platforms", {}).get("qqbot", {})
if gateway.get("gateway_state") != "running" or qq.get("state") != "connected":
    raise SystemExit("QQ gateway is not connected")
print(f"DM_POLICY=pairing API_BASE=production AUTO_PAIR={auto_pair_state} UNTIL_UTC={until_raw}")
print("GROUP_ACCESS=all_groups")
print(f"SCHEDULE_TARGET_COUNT={len(groups)}")
print(f"PLUGIN_STATUS={plugin.get('status')}")
print(f"OWNED_CRON_COUNT={len(owned)}")
print(f"PLUGIN_DB_INTEGRITY=ok AUDIT_COUNT={audit_count} SCHEMA_VERSION={schema_version}")
print("PLUGIN_MEMORY_KB_SCHEMA=ok")
print("MEMBER_MEMORY_SCHEMA=ok")
print("MEMBER_MEMORY_CONFIG=ok")
print("COMPACTION_SCHEMA=ok")
print("QQ_GATEWAY=connected")
print("VOICE_INPUT=disabled VOICE_OUTPUT=disabled AUDIO_ENV=absent")
PY
docker exec hermes-qqbot hermes doctor >/tmp/hermes-qqbot-doctor.txt
echo "CONTAINER_STATE=$state"
echo "CONTAINER_HEALTH=$health"
echo "HERMES_BASE_DIGEST=verified"
echo "HERMES_AUDIO_PATCH=verified"
echo "HERMES_CHAT_REASONING_PATCH=verified"
echo "HERMES_COMPRESSION_RECOVERY_PATCH=verified"
echo "HERMES_QQ_HELP_PATCH=verified"
echo "HERMES_QQ_OUTPUT_PATCH=verified"
echo "QQ_NATIVE_COMMANDS=verified"
echo "CHAT_COMPLETIONS_ROUTE=verified"
echo "CONFIG_CHECK=passed"
echo "VOICE_POLICY=disabled"
echo "PLUGIN_CHECK=passed"
echo "DOCTOR=completed"
