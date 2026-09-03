#!/usr/bin/env bash
set -euo pipefail

project_dir="/opt/qqbot-hk"
deploy_dir="/opt/qqbot-hk-deploy"
data_dir="$deploy_dir/hermes-data"
secrets_dir="$deploy_dir/secrets"

test "$(id -u)" -eq 0 || { echo "ERROR: run as root" >&2; exit 1; }
test -f "$project_dir/docker-compose.yml"
test -f "$project_dir/config/hermes-config.yaml"
test -f "$secrets_dir/qqbot.env"
test -f "$secrets_dir/sub2api-api-key"

install -d -m 0755 "$data_dir"
install -m 0644 "$project_dir/config/hermes-config.yaml" "$data_dir/config.yaml"

python3 - "$secrets_dir/qqbot.env" "$secrets_dir/sub2api-api-key" "$data_dir/.env" <<'PY'
from pathlib import Path
import os
import sys

qq_source = Path(sys.argv[1])
key_source = Path(sys.argv[2])
target = Path(sys.argv[3])

qq_values = {}
for raw in qq_source.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    qq_values[name.strip()] = value.strip()

required = ("QQ_APP_ID", "QQ_CLIENT_SECRET")
missing = [name for name in required if not qq_values.get(name)]
if missing:
    raise SystemExit("missing required QQ variables: " + ", ".join(missing))

sub2api_key = key_source.read_text(encoding="utf-8").strip()
if not sub2api_key:
    raise SystemExit("Sub2API key is empty")

content = (
    f"QQ_APP_ID={qq_values['QQ_APP_ID']}\n"
    f"QQ_CLIENT_SECRET={qq_values['QQ_CLIENT_SECRET']}\n"
    f"SUB2API_API_KEY={sub2api_key}\n"
)
target.write_text(content, encoding="utf-8")
os.chmod(target, 0o600)
PY

chown -R 10000:10000 "$data_dir"
chmod 0700 "$data_dir"
chmod 0600 "$data_dir/.env"

docker network inspect sub2api_sub2api-network >/dev/null
docker compose -f "$project_dir/docker-compose.yml" config --quiet
docker compose -f "$project_dir/docker-compose.yml" up -d --remove-orphans
