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
    model_ids = {str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict)}
    return "gpt-5.6-luna" in model_ids


def main() -> int:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_FILE.parent, 0o700)

    if existing_key_is_valid():
        print("SUB2API_KEY=existing-valid")
        return 0

    password = random_password()
    admin_headers = {"x-api-key": admin_key()}
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
        request("PUT", f"/api/v1/admin/users/{user_id}", headers=admin_headers, body=update_body)
        print("SUB2API_USER=existing-updated")

    login = request("POST", "/api/v1/auth/login", body={"email": EMAIL, "password": password})
    login_data = login.get("data") or {}
    access_token = login_data.get("access_token")
    if not access_token:
        raise RuntimeError("Sub2API login did not return an access token")

    created_key = request(
        "POST",
        "/api/v1/keys",
        headers={"Authorization": f"Bearer {access_token}"},
        body={"name": "hermes-qqbot-hk", "group_id": GROUP_ID, "quota": 100.0},
    )
    key = str((created_key.get("data") or {}).get("key") or "").strip()
    if not key:
        raise RuntimeError("Sub2API API key creation returned no key")

    KEY_FILE.write_text(key + "\n", encoding="utf-8")
    os.chmod(KEY_FILE, 0o600)
    if not existing_key_is_valid():
        raise RuntimeError("New Sub2API key failed the model-list validation")
    print("SUB2API_KEY=created-and-validated")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
