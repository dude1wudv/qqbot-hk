#!/usr/bin/env python3
"""Provision a dedicated, bounded Sub2API identity for Hermes without printing secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import string
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = "http://127.0.0.1:8080"
EMAIL = "hermes-qqbot@local.invalid"
USERNAME = "hermes-qqbot"
GENERAL_GROUP_ID = 81
DEEPSEEK_GROUP_ID = 179
KEY_SPECS = (
    (Path("/opt/qqbot-hk-deploy/secrets/sub2api-api-key"), GENERAL_GROUP_ID, "hermes-qqbot-hk"),
    (Path("/opt/qqbot-hk-deploy/secrets/sub2api-deepseek-api-key"), DEEPSEEK_GROUP_ID, "hermes-qqbot-hk-deepseek"),
)
GENERAL_GROUP_EXCLUDED_MODELS = frozenset({"deepseek/deepseek-v4.1-flash"})


def request(method: str, path: str, *, headers: dict[str, str] | None = None, body=None):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    merged = {"Content-Type": "application/json"}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(BASE_URL + path, data=payload, headers=merged, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        message = f"HTTP {exc.code}"
        try:
            parsed = json.load(exc)
            public_message = parsed.get("message")
            if public_message:
                message += f": {public_message}"
        except Exception:
            pass
        raise RuntimeError(f"{method} {path}: {message}") from exc


def admin_key() -> str:
    result = subprocess.run(
        [
            "docker", "exec", "sub2api-postgres", "psql", "-U", "sub2api",
            "-d", "sub2api", "-Atc",
            "select value from settings where key='admin_api_key'",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value:
        raise RuntimeError("Sub2API admin key is unavailable")
    return value


def random_password(length: int = 36) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))




def reconcile_general_group_models(admin_headers: dict[str, str]) -> None:
    payload = request("GET", f"/api/v1/admin/groups/{GENERAL_GROUP_ID}", headers=admin_headers)
    allowlist = (payload.get("data") or {}).get("model_allowlist") or {}
    models = [str(model).strip() for model in (allowlist.get("models") or []) if str(model).strip()]
    reconciled = [model for model in models if model not in GENERAL_GROUP_EXCLUDED_MODELS]
    if reconciled == models:
        print("SUB2API_GENERAL_GROUP_MODELS=ready")
        return
    request(
        "PUT",
        f"/api/v1/admin/groups/{GENERAL_GROUP_ID}",
        headers=admin_headers,
        body={"model_allowlist": {"enabled": bool(allowlist.get("enabled")), "models": reconciled}},
    )
    print(f"SUB2API_GENERAL_GROUP_MODELS=updated COUNT={len(models) - len(reconciled)}")


def existing_key_is_valid(path: Path) -> bool:
    if not path.is_file():
        return False
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        return False
    try:
        payload = request("GET", "/v1/models", headers={"Authorization": f"Bearer {key}"})
    except Exception:
        return False
    return bool(payload.get("data"))

def ensure_api_key(
    auth_headers: dict[str, str],
    records: list[dict],
    path: Path,
    group_id: int,
    name: str,
) -> tuple[list[dict], int]:
    current_key = path.read_text(encoding="utf-8").strip() if existing_key_is_valid(path) else ""
    current_record = next((item for item in records if item.get("key") == current_key), None)
    current_record_ok = bool(
        current_record
        and current_record.get("status") == "active"
        and int(current_record.get("group_id") or 0) == group_id
        and float(current_record.get("quota") or 0) <= 100.0
    )
    if not current_record_ok:
        created = request(
            "POST",
            "/api/v1/keys",
            headers=auth_headers,
            body={"name": name, "group_id": group_id, "quota": 100.0},
        )
        current_key = str((created.get("data") or {}).get("key") or "").strip()
        if not current_key:
            raise RuntimeError("Sub2API API key creation returned no key")
        path.write_text(current_key + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        if not existing_key_is_valid(path):
            raise RuntimeError("New Sub2API key failed authentication validation")
        listed = request("GET", "/api/v1/keys?page=1&page_size=100", headers=auth_headers)
        records = ((listed.get("data") or {}).get("items") or [])
        print(f"SUB2API_KEY={name} created-and-validated")
    else:
        print(f"SUB2API_KEY={name} existing-valid")

    duplicates_disabled = 0
    for item in records:
        if item.get("name") == name and item.get("status") == "active" and item.get("key") != current_key:
            request(
                "PUT",
                f"/api/v1/keys/{int(item['id'])}",
                headers=auth_headers,
                body={"status": "inactive"},
            )
            duplicates_disabled += 1
    return records, duplicates_disabled


def main() -> int:
    for path, _, _ in KEY_SPECS:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)

    password = random_password()
    admin_headers = {"x-api-key": admin_key()}
    reconcile_general_group_models(admin_headers)
    query = urllib.parse.urlencode({"search": EMAIL, "page": 1, "page_size": 20})
    users_payload = request("GET", f"/api/v1/admin/users?{query}", headers=admin_headers)
    users = ((users_payload.get("data") or {}).get("items") or [])
    user = next((item for item in users if item.get("email") == EMAIL), None)

    allowed_groups = [GENERAL_GROUP_ID, DEEPSEEK_GROUP_ID]
    user_body = {
        "email": EMAIL,
        "password": password,
        "username": USERNAME,
        "notes": "Dedicated HK Hermes QQBot service account",
        "role": "user",
        "concurrency": 2,
        "rpm_limit": 30,
        "allowed_groups": allowed_groups,
        "restrict_public_groups": True,
    }
    if user is None:
        user_body["balance"] = 100.0
        created = request("POST", "/api/v1/admin/users", headers=admin_headers, body=user_body)
        user = created.get("data") or {}
        print("SUB2API_USER=created")
    else:
        user_id = int(user["id"])
        update_body = dict(user_body)
        update_body.pop("email", None)
        update_body.pop("role", None)
        updated = request("PUT", f"/api/v1/admin/users/{user_id}", headers=admin_headers, body=update_body)
        user = updated.get("data") or {}
        print("SUB2API_USER=existing-updated")

    if int(user.get("concurrency") or 0) != 2:
        raise RuntimeError("Sub2API user concurrency limit was not applied")
    if int(user.get("rpm_limit") or 0) != 30:
        raise RuntimeError("Sub2API user RPM limit was not applied")
    if not bool(user.get("restrict_public_groups")):
        raise RuntimeError("Sub2API public-group restriction was not applied")
    actual_groups = {int(value) for value in (user.get("allowed_groups") or [])}
    if not set(allowed_groups).issubset(actual_groups):
        raise RuntimeError("Sub2API allowed-group restriction was not applied")

    login = request("POST", "/api/v1/auth/login", body={"email": EMAIL, "password": password})
    access_token = (login.get("data") or {}).get("access_token")
    if not access_token:
        raise RuntimeError("Sub2API login did not return an access token")

    auth_headers = {"Authorization": f"Bearer {access_token}"}
    listed = request("GET", "/api/v1/keys?page=1&page_size=100", headers=auth_headers)
    records = ((listed.get("data") or {}).get("items") or [])
    duplicates_disabled = 0
    for path, group_id, name in KEY_SPECS:
        records, disabled = ensure_api_key(auth_headers, records, path, group_id, name)
        duplicates_disabled += disabled
    print(f"SUB2API_DUPLICATE_KEYS_DISABLED={duplicates_disabled}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
