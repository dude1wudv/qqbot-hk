#!/usr/bin/env bash
set -euo pipefail

project_dir="/opt/qqbot-hk"
deploy_dir="/opt/qqbot-hk-deploy"
data_dir="$deploy_dir/hermes-data"
secrets_dir="$deploy_dir/secrets"
service="hermes-qqbot"

test "$(id -u)" -eq 0 || { echo "ERROR: run as root" >&2; exit 1; }
for path in \
  "$project_dir/docker-compose.yml" \
  "$project_dir/config/hermes-config.yaml" \
  "$project_dir/config/SOUL.md" \
  "$project_dir/config/scheduled-messages.yaml" \
  "$project_dir/plugins/smart_group_qq/plugin.yaml" \
  "$project_dir/scripts/reconcile-smart-group-cron.py" \
  "$secrets_dir/qqbot.env" \
  "$secrets_dir/sub2api-api-key"; do
  test -e "$path" || { echo "ERROR: required deployment input missing" >&2; exit 1; }
done

install -d -o 10000 -g 10000 -m 0700 "$data_dir"
install -d -o 10000 -g 10000 -m 0755 "$data_dir/plugins" "$data_dir/scripts" "$data_dir/plugin-data"
install -d -o 10000 -g 10000 -m 0700 "$data_dir/plugin-data/smart_group_qq"
stage_dir="$(mktemp -d "$data_dir/.smart-group-install.XXXXXX")"
trap 'rm -rf "$stage_dir"' EXIT

python3 - \
  "$secrets_dir/qqbot.env" \
  "$secrets_dir/sub2api-api-key" \
  "$project_dir/config/hermes-config.yaml" \
  "$stage_dir/.env" \
  "$stage_dir/config.yaml" <<'PY'
from pathlib import Path
import json
import os
import re
import sys

qq_source, key_source, config_source, env_target, config_target = map(Path, sys.argv[1:])
qq_values = {}
for raw in qq_source.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    qq_values[name.strip()] = value.strip()

required = ("QQ_APP_ID", "QQ_CLIENT_SECRET", "QQ_GROUP_ALLOWED_USERS")
missing = [name for name in required if not qq_values.get(name)]
if missing:
    raise SystemExit("missing required QQ variables: " + ", ".join(missing))

groups = []
for item in qq_values["QQ_GROUP_ALLOWED_USERS"].split(","):
    value = item.strip()
    if not value or value == "*" or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value) is None:
        raise SystemExit("QQ_GROUP_ALLOWED_USERS contains an invalid group OpenID")
    if value not in groups:
        groups.append(value)
if not groups:
    raise SystemExit("QQ_GROUP_ALLOWED_USERS is empty")

sandbox = qq_values.get("QQ_SANDBOX", "true").strip().lower()
if sandbox not in {"true", "false"}:
    raise SystemExit("QQ_SANDBOX must be true or false")

sub2api_key = key_source.read_text(encoding="utf-8").strip()
if not sub2api_key:
    raise SystemExit("Sub2API key is empty")

config = config_source.read_text(encoding="utf-8")
sentinel = "      group_allow_from: []"
if config.count(sentinel) != 1:
    raise SystemExit("group allow-list config sentinel is missing or duplicated")
config = config.replace(sentinel, "      group_allow_from: " + json.dumps(groups), 1)
config_target.write_text(config, encoding="utf-8")

env_target.write_text(
    f"QQ_APP_ID={qq_values['QQ_APP_ID']}\n"
    f"QQ_CLIENT_SECRET={qq_values['QQ_CLIENT_SECRET']}\n"
    f"QQ_SANDBOX={sandbox}\n"
    f"QQ_GROUP_ALLOWED_USERS={','.join(groups)}\n"
    f"SUB2API_API_KEY={sub2api_key}\n",
    encoding="utf-8",
)
os.chmod(env_target, 0o600)
PY

install -m 0644 "$project_dir/config/SOUL.md" "$stage_dir/SOUL.md"
install -m 0644 "$project_dir/config/scheduled-messages.yaml" "$stage_dir/smart-group-schedules.yaml"
install -m 0755 "$project_dir/scripts/reconcile-smart-group-cron.py" "$stage_dir/reconcile-smart-group-cron.py"
cp -a "$project_dir/plugins/smart_group_qq" "$stage_dir/smart_group_qq"
chown -R 10000:10000 "$stage_dir"

install -o 10000 -g 10000 -m 0644 "$stage_dir/config.yaml" "$data_dir/.config.yaml.new"
install -o 10000 -g 10000 -m 0600 "$stage_dir/.env" "$data_dir/.env.new"
install -o 10000 -g 10000 -m 0644 "$stage_dir/SOUL.md" "$data_dir/.SOUL.md.new"
install -o 10000 -g 10000 -m 0644 "$stage_dir/smart-group-schedules.yaml" "$data_dir/.smart-group-schedules.yaml.new"
install -o 10000 -g 10000 -m 0755 "$stage_dir/reconcile-smart-group-cron.py" "$data_dir/scripts/.reconcile-smart-group-cron.py.new"
mv -f "$data_dir/.config.yaml.new" "$data_dir/config.yaml"
mv -f "$data_dir/.env.new" "$data_dir/.env"
mv -f "$data_dir/.SOUL.md.new" "$data_dir/SOUL.md"
mv -f "$data_dir/.smart-group-schedules.yaml.new" "$data_dir/smart-group-schedules.yaml"
mv -f "$data_dir/scripts/.reconcile-smart-group-cron.py.new" "$data_dir/scripts/reconcile-smart-group-cron.py"
rm -rf "$data_dir/plugins/smart_group_qq.old"
if test -d "$data_dir/plugins/smart_group_qq"; then
  mv "$data_dir/plugins/smart_group_qq" "$data_dir/plugins/smart_group_qq.old"
fi
mv "$stage_dir/smart_group_qq" "$data_dir/plugins/smart_group_qq"
rm -rf "$data_dir/plugins/smart_group_qq.old"
chown -R 10000:10000 "$data_dir/plugins/smart_group_qq" "$data_dir/plugin-data/smart_group_qq" "$data_dir/scripts"
chmod 0700 "$data_dir"
chmod 0600 "$data_dir/.env"

docker network inspect sub2api_sub2api-network >/dev/null
docker compose -f "$project_dir/docker-compose.yml" config --quiet
docker compose -f "$project_dir/docker-compose.yml" up -d --force-recreate --no-deps "$service"

container_id="$(docker compose -f "$project_dir/docker-compose.yml" ps -q "$service")"
test -n "$container_id"
for _ in $(seq 1 60); do
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
  test "$health" = healthy && break
  test "$health" != unhealthy || { echo "ERROR: QQbot became unhealthy" >&2; exit 1; }
  sleep 2
done
test "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")" = healthy || {
  echo "ERROR: QQbot health timeout" >&2
  exit 1
}

docker exec "$service" python /opt/data/scripts/reconcile-smart-group-cron.py
