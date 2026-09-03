"""QQBot constants override adding the documented QQ_SANDBOX switch.

Hermes Agent v0.21.0 documents QQ_SANDBOX but its bundled QQBot constants
still hard-code the production API host. Keep the upstream constants intact
apart from selecting the official sandbox API host when explicitly enabled.
"""

from __future__ import annotations

import os

QQBOT_VERSION = "1.1.0-hk-sandbox.1"

PORTAL_HOST = os.getenv("QQ_PORTAL_HOST", "q.qq.com")
_SANDBOX_ENABLED = os.getenv("QQ_SANDBOX", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
API_BASE = (
    "https://sandbox.api.sgroup.qq.com"
    if _SANDBOX_ENABLED
    else "https://api.sgroup.qq.com"
)
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL_PATH = "/gateway"

ONBOARD_CREATE_PATH = "/lite/create_bind_task"
ONBOARD_POLL_PATH = "/lite/poll_bind_result"
QR_URL_TEMPLATE = (
    "https://q.qq.com/qqbot/openclaw/connect.html"
    "?task_id={task_id}&_wv=2&source=hermes"
)

DEFAULT_API_TIMEOUT = 30.0
FILE_UPLOAD_TIMEOUT = 120.0
CONNECT_TIMEOUT_SECONDS = 20.0

RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
MAX_RECONNECT_ATTEMPTS = 100
RATE_LIMIT_DELAY = 60
QUICK_DISCONNECT_THRESHOLD = 5.0
MAX_QUICK_DISCONNECT_COUNT = 3

ONBOARD_POLL_INTERVAL = 2.0
ONBOARD_API_TIMEOUT = 10.0

MAX_MESSAGE_LENGTH = 4000
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 1000

MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2
MSG_TYPE_MEDIA = 7
MSG_TYPE_INPUT_NOTIFY = 6

MEDIA_TYPE_IMAGE = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_VOICE = 3
MEDIA_TYPE_FILE = 4
