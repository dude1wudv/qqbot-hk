#!/usr/bin/env python3
"""Reconcile fixed QQ group announcements with Hermes' native cron store.

The declaration and allow-list are intentionally kept outside this repository at
runtime.  This module does not implement a cron scheduler: it uses the Hermes
``cron.jobs`` API that owns the profile's cron store.  The small, explicit
``reconcile`` arguments make the same code usable by the image-local unittest
suite without importing a second cron implementation.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

DEFAULT_DATA_DIR = Path("/opt/data")
DEFAULT_SCHEDULES_PATH = DEFAULT_DATA_DIR / "smart-group-schedules.yaml"
DEFAULT_ENV_PATH = DEFAULT_DATA_DIR / ".env"
DEFAULT_GENERATED_DIR = DEFAULT_DATA_DIR / "scripts" / "generated"
REL_GENERATED_DIR = Path("generated")
OWNED_PREFIX = "smart-group-qq::"
MAX_TEXT_LENGTH = 4000
MAX_SCHEDULE_ID_LENGTH = 96

_ID_RE = re.compile(r"^[a-z0-9_-]+$")
_GENERATED_RE = re.compile(
    r"^smart-group-qq--[a-z0-9_-]+--[0-9a-f]{12}\.py$"
)
_REQUIRED_FIELDS = frozenset(("id", "cron", "target", "text"))
_ALLOWED_FIELDS = frozenset(("id", "enabled", "cron", "target", "text"))

PathLike = Union[str, os.PathLike]


class ScheduleConfigError(ValueError):
    """Raised when a declaration or allow-list is unsafe to reconcile."""


def _yaml_load(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - the Hermes image includes PyYAML
        raise ScheduleConfigError("YAML support is unavailable") from exc

    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ScheduleConfigError("schedule declaration cannot be read") from exc


def _validate_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ScheduleConfigError("schedule text must be a string")
    if not value.strip():
        raise ScheduleConfigError("schedule text must not be empty")
    if len(value) > MAX_TEXT_LENGTH:
        raise ScheduleConfigError("schedule text is too long")
    # Do not put terminal control sequences (or NUL) into a generated source
    # file or a QQ message. Newlines/tabs are useful in fixed announcements.
    if any((ord(char) < 0x20 and char not in "\r\n\t") or ord(char) == 0x7F for char in value):
        raise ScheduleConfigError("schedule text contains unsupported control characters")
    return value


def _validate_cron(value: Any) -> str:
    if not isinstance(value, str):
        raise ScheduleConfigError("cron must be a string")
    cron = value.strip()
    # This feature deliberately accepts cron expressions, not Hermes' natural
    # language/interval/one-shot schedule forms. Hermes remains the authority
    # for field grammar and croniter validation.
    if len(cron.split()) not in (5, 6):
        raise ScheduleConfigError("cron must have five or six fields")
    try:
        from cron.jobs import parse_schedule
    except ImportError:
        # Unit tests outside the fixed Hermes image still reject malformed
        # cron tokens. Production always delegates final validation to Hermes.
        if any(re.fullmatch(r"[0-9A-Za-z*/?,#LW-]+", field) is None for field in cron.split()):
            raise ScheduleConfigError("cron expression is invalid")
        return cron
    try:
        parsed = parse_schedule(cron)
    except Exception as exc:
        raise ScheduleConfigError("cron expression is invalid") from exc
    if not isinstance(parsed, Mapping) or parsed.get("kind") != "cron":
        raise ScheduleConfigError("cron expression is invalid")
    return cron



def validate_schedule_declaration(raw: Any) -> Dict[str, Any]:
    """Validate one YAML schedule and return its normalized safe values."""
    if not isinstance(raw, Mapping):
        raise ScheduleConfigError("each schedule must be a mapping")
    keys = set(raw)
    if not _REQUIRED_FIELDS.issubset(keys):
        raise ScheduleConfigError("schedule is missing a required field")
    if not keys.issubset(_ALLOWED_FIELDS):
        raise ScheduleConfigError("schedule contains an unknown field")

    schedule_id = raw.get("id")
    if (
        not isinstance(schedule_id, str)
        or not schedule_id
        or len(schedule_id) > MAX_SCHEDULE_ID_LENGTH
        or _ID_RE.fullmatch(schedule_id) is None
    ):
        raise ScheduleConfigError("schedule id is invalid")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ScheduleConfigError("schedule enabled must be boolean")

    target = raw.get("target")
    if target != "allowed_groups":
        raise ScheduleConfigError("schedule target is invalid")

    return {
        "id": schedule_id,
        "enabled": enabled,
        "cron": _validate_cron(raw.get("cron")),
        "target": target,
        "text": _validate_text(raw.get("text")),
    }


def load_schedules(path: PathLike = DEFAULT_SCHEDULES_PATH) -> List[Dict[str, Any]]:
    """Load and validate the complete schedule declaration before any mutation."""
    document = _yaml_load(Path(path))
    if not isinstance(document, Mapping) or set(document) != {"schedules"}:
        raise ScheduleConfigError("schedule document must contain only schedules")
    raw_schedules = document.get("schedules")
    if not isinstance(raw_schedules, list):
        raise ScheduleConfigError("schedules must be a list")

    result: List[Dict[str, Any]] = []
    seen = set()
    for raw in raw_schedules:
        schedule = validate_schedule_declaration(raw)
        if schedule["id"] in seen:
            raise ScheduleConfigError("schedule ids must be unique")
        seen.add(schedule["id"])
        result.append(schedule)
    return result


def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def load_allowed_groups(path: PathLike = DEFAULT_ENV_PATH) -> Tuple[str, ...]:
    """Read QQ_GROUP_ALLOWED_USERS without ever logging its values."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ScheduleConfigError("QQ group allow-list cannot be read") from exc

    found = False
    raw_value: Optional[str] = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "QQ_GROUP_ALLOWED_USERS":
            if found:
                raise ScheduleConfigError("QQ group allow-list is duplicated")
            found = True
            raw_value = _unquote_env_value(value)
            continue

    if raw_value is None:
        raise ScheduleConfigError("QQ group allow-list is required")
    groups: List[str] = []
    for group in raw_value.split(","):
        group = group.strip()
        if not group:
            raise ScheduleConfigError("QQ group allow-list contains an empty entry")
        if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in group):
            raise ScheduleConfigError("QQ group allow-list contains an invalid entry")
        if group not in groups:
            groups.append(group)
    if not groups:
        raise ScheduleConfigError("QQ group allow-list must not be empty")
    return tuple(groups)


def group_digest(group_openid: str) -> str:
    """Return the non-reversible identifier used in names and filenames."""
    return hashlib.sha256(group_openid.encode("utf-8")).hexdigest()[:12]


def owned_job_name(schedule_id: str, group_openid: str) -> str:
    return f"{OWNED_PREFIX}{schedule_id}::{group_digest(group_openid)}"


def generated_script_name(schedule_id: str, group_openid: str) -> str:
    return f"smart-group-qq--{schedule_id}--{group_digest(group_openid)}.py"


def generated_script_relpath(schedule_id: str, group_openid: str) -> str:
    return (REL_GENERATED_DIR / generated_script_name(schedule_id, group_openid)).as_posix()


def _script_source(text: str) -> str:
    # repr() is source-code escaping, not interpolation. sys.stdout.write avoids
    # print's extra newline so stdout equals the configured text byte-for-byte
    # after UTF-8 decoding.
    return "#!/usr/bin/env python3\nimport sys\nsys.stdout.write(" + repr(text) + ")\n"


def write_generated_script(path: PathLike, text: str) -> None:
    """Atomically install one fixed-output script with executable permissions."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = _script_source(text).encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(
        prefix="." + destination.name + ".", suffix=".tmp", dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        os.replace(str(temporary), str(destination))
        try:
            directory_fd = os.open(str(destination.parent), os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _cron_api(api: Any = None) -> Any:
    if api is None:
        try:
            from cron import jobs
        except Exception as exc:  # pragma: no cover - only production import path
            raise RuntimeError("Hermes cron API is unavailable") from exc
        return jobs
    # A caller may inject either cron.jobs itself or a small namespace exposing
    # the jobs object as ``.jobs`` (matching Hermes' package shape).
    if not hasattr(api, "list_jobs") and hasattr(api, "jobs"):
        api = api.jobs
    for method in ("list_jobs", "create_job", "update_job", "remove_job"):
        if not callable(getattr(api, method, None)):
            raise TypeError("cron API is missing a required method")
    return api


def _schedule_expr(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        expression = value.get("expr")
        if isinstance(expression, str):
            return expression.strip()
    return None


def _deliver_matches(actual: Any, expected: str) -> bool:
    if isinstance(actual, str):
        return actual == expected
    if isinstance(actual, (list, tuple)):
        return actual == [expected] or actual == (expected,)
    return False


def _job_needs_update(job: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    if job.get("name") != desired["name"]:
        return True
    if _schedule_expr(job.get("schedule")) != desired["cron"]:
        return True
    if not _deliver_matches(job.get("deliver"), desired["deliver"]):
        return True
    if job.get("script") != desired["script"]:
        return True
    if job.get("no_agent") is not True:
        return True
    if job.get("enabled", True) is not True:
        return True
    if job.get("state") == "paused":
        return True
    return False


def _desired_jobs(schedules: Iterable[Mapping[str, Any]], groups: Iterable[str]) -> List[Dict[str, Any]]:
    result = []
    for schedule in schedules:
        if not schedule["enabled"]:
            continue
        for group in groups:
            name = owned_job_name(schedule["id"], group)
            result.append(
                {
                    "name": name,
                    "cron": schedule["cron"],
                    "deliver": "qqbot:" + group,
                    "script": generated_script_relpath(schedule["id"], group),
                    "text": schedule["text"],
                }
            )
    return result


def _remove_job_and_ignore_missing(api: Any, job: Mapping[str, Any]) -> None:
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("owned Hermes cron job has no id")
    api.remove_job(job_id)


def _remove_stale_scripts(generated_dir: Path, expected_names: Iterable[str]) -> int:
    expected = set(expected_names)
    removed = 0
    if not generated_dir.exists():
        return 0
    for path in generated_dir.iterdir():
        if not path.is_file() or _GENERATED_RE.fullmatch(path.name) is None:
            continue
        if path.name not in expected:
            path.unlink()
            removed += 1
    return removed


def reconcile(
    *,
    data_dir: PathLike = DEFAULT_DATA_DIR,
    schedules_path: Optional[PathLike] = None,
    env_path: Optional[PathLike] = None,
    generated_dir: Optional[PathLike] = None,
    cron_api: Any = None,
) -> Dict[str, int]:
    """Make Hermes' owned jobs exactly match the validated declaration.

    All declaration and allow-list validation happens before generated files or
    cron jobs are changed.  The returned counters contain no target values and
    are suitable for a deployment log.
    """
    data_root = Path(data_dir)
    schedules = load_schedules(schedules_path or data_root / "smart-group-schedules.yaml")
    groups = load_allowed_groups(env_path or data_root / ".env")
    desired = _desired_jobs(schedules, groups)
    desired_by_name = {job["name"]: job for job in desired}
    generated_root = Path(generated_dir or data_root / "scripts" / "generated")
    api = _cron_api(cron_api)

    existing = api.list_jobs(include_disabled=True)
    if not isinstance(existing, list):
        raise RuntimeError("Hermes cron API returned an invalid job list")
    owned: Dict[str, List[Mapping[str, Any]]] = {}
    for job in existing:
        if not isinstance(job, Mapping):
            continue
        name = job.get("name")
        if isinstance(name, str) and name.startswith(OWNED_PREFIX):
            owned.setdefault(name, []).append(job)

    # Generate desired scripts before touching Hermes. Invalid declarations have
    # already failed above; each write is itself atomic.
    for job in desired:
        write_generated_script(generated_root / Path(job["script"]).name, job["text"])

    created = updated = removed = 0
    for name, jobs in owned.items():
        desired_job = desired_by_name.get(name)
        if desired_job is None:
            for job in jobs:
                _remove_job_and_ignore_missing(api, job)
                removed += 1
            continue

        # A previous interrupted reconcile can leave duplicate owned names.
        # Keep the first Hermes record, converge it, and remove the extras.
        primary = jobs[0]
        if _job_needs_update(primary, desired_job):
            updates = {
                "name": desired_job["name"],
                "schedule": desired_job["cron"],
                "deliver": desired_job["deliver"],
                "script": desired_job["script"],
                "no_agent": True,
                "enabled": True,
            }
            if primary.get("state") == "paused":
                updates.update({"state": "scheduled", "paused_at": None, "paused_reason": None})
            job_id = primary.get("id")
            if not isinstance(job_id, str) or not job_id:
                raise RuntimeError("owned Hermes cron job has no id")
            api.update_job(job_id, updates)
            updated += 1
        for duplicate in jobs[1:]:
            _remove_job_and_ignore_missing(api, duplicate)
            removed += 1

    for name, desired_job in desired_by_name.items():
        if name in owned:
            continue
        api.create_job(
            prompt=None,
            schedule=desired_job["cron"],
            name=desired_job["name"],
            deliver=desired_job["deliver"],
            script=desired_job["script"],
            no_agent=True,
        )
        created += 1

    expected_scripts = (Path(job["script"]).name for job in desired)
    removed_scripts = _remove_stale_scripts(generated_root, expected_scripts)
    return {
        "created": created,
        "updated": updated,
        "removed": removed,
        "scripts_removed": removed_scripts,
        "desired": len(desired),
    }


def main() -> int:
    try:
        result = reconcile()
    except ScheduleConfigError:
        print("smart-group cron reconcile failed: invalid configuration", file=sys.stderr)
        return 2
    except Exception:
        # Deliberately do not print exception text: Hermes errors can echo a
        # delivery target, and neither targets nor announcement bodies belong
        # in deployment logs.
        print("smart-group cron reconcile failed: Hermes API error", file=sys.stderr)
        return 1
    print(
        "smart-group cron reconciled: "
        f"desired={result['desired']} created={result['created']} "
        f"updated={result['updated']} removed={result['removed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
