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
GROUP_ID = 81
KEY_FILE = Path("/opt/qqbot-hk-deploy/secrets/sub2api-api-key")
REQUIRED_GROUP_MODELS = ("deepseek/deepseek-v4.1-flash",)


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

def ensure_group_models(admin_headers: dict[str, str]) -> None:
    payload = request("GET", f"/api/v1/admin/groups/{GROUP_ID}", headers=admin_headers)
    group = payload.get("data") or {}
    allowlist = group.get("model_allowlist") or {}
    models = [str(model).strip() for model in (allowlist.get("models") or []) if str(model).strip()]
    missing = [model for model in REQUIRED_GROUP_MODELS if model not in models]
    if not missing:
        print("SUB2API_GROUP_MODELS=ready")
        return
    request(
        "PUT",
        f"/api/v1/admin/groups/{GROUP_ID}",
        headers=admin_headers,
        body={"model_allowlist": {"enabled": bool(allowlist.get("enabled")), "models": models + missing}},
    )
    print(f"SUB2API_GROUP_MODELS=updated COUNT={len(missing)}")



def existing_key_is_valid() -> bool:
    if not KEY_FILE.is_file():
        return False
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        return False
    try:
        payload = request("GET", "/v1/models", headers={"Authorization": f"Bearer {key}"})
    except Exception:
        return False
    return bool(payload.get("data"))


def main() -> int:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_FILE.parent, 0o700)

    key_is_valid = existing_key_is_valid()

    password = random_password()
    admin_headers = {"x-api-key": admin_key()}
    ensure_group_models(admin_headers)
    query = urllib.parse.urlencode({"search": EMAIL, "page": 1, "page_size": 20})
    users_payload = request("GET", f"/api/v1/admin/users?{query}", headers=admin_headers)
    users = ((users_payload.get("data") or {}).get("items") or [])
    user = next((item for item in users if item.get("email") == EMAIL), None)

    user_body = {
        "email": EMAIL,
        "password": password,
        "username": USERNAME,
        "notes": "Dedicated HK Hermes QQBot service account",
        "role": "user",
        "concurrency": 2,
        "rpm_limit": 30,
        "allowed_groups": [GROUP_ID],
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
    if GROUP_ID not in {int(value) for value in (user.get("allowed_groups") or [])}:
        raise RuntimeError("Sub2API allowed-group restriction was not applied")

    login = request("POST", "/api/v1/auth/login", body={"email": EMAIL, "password": password})
    login_data = login.get("data") or {}
    access_token = login_data.get("access_token")
    if not access_token:
        raise RuntimeError("Sub2API login did not return an access token")

    auth_headers = {"Authorization": f"Bearer {access_token}"}
    current_key = KEY_FILE.read_text(encoding="utf-8").strip() if key_is_valid else ""
    listed = request("GET", "/api/v1/keys?page=1&page_size=100", headers=auth_headers)
    records = ((listed.get("data") or {}).get("items") or [])
    current_record = next((item for item in records if item.get("key") == current_key), None)
    current_record_ok = bool(
        current_record
        and current_record.get("status") == "active"
        and int(current_record.get("group_id") or 0) == GROUP_ID
        and float(current_record.get("quota") or 0) <= 100.0
    )

    if not current_record_ok:
        created_key = request(
            "POST",
            "/api/v1/keys",
            headers=auth_headers,
            body={"name": "hermes-qqbot-hk", "group_id": GROUP_ID, "quota": 100.0},
        )
        current_key = str((created_key.get("data") or {}).get("key") or "").strip()
        if not current_key:
            raise RuntimeError("Sub2API API key creation returned no key")
        KEY_FILE.write_text(current_key + "\n", encoding="utf-8")
        os.chmod(KEY_FILE, 0o600)
        if not existing_key_is_valid():
            raise RuntimeError("New Sub2API key failed authentication validation")
        listed = request("GET", "/api/v1/keys?page=1&page_size=100", headers=auth_headers)
        records = ((listed.get("data") or {}).get("items") or [])
        print("SUB2API_KEY=created-and-validated")
    else:
        print("SUB2API_KEY=existing-valid")

    duplicates_disabled = 0
    for item in records:
        if (
            item.get("name") == "hermes-qqbot-hk"
            and item.get("status") == "active"
            and item.get("key") != current_key
        ):
            request(
                "PUT",
                f"/api/v1/keys/{int(item['id'])}",
                headers=auth_headers,
                body={"status": "inactive"},
            )
            duplicates_disabled += 1
    print(f"SUB2API_DUPLICATE_KEYS_DISABLED={duplicates_disabled}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
