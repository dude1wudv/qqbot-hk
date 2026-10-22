#!/usr/bin/env python3
"""Verify the installed Hermes plugin secret scope with temporary synthetic profiles.

Run from the Hermes installation root (for example, /opt/hermes/verify-hermes-plugin-scope.py).
The script imports the installed modules as-is; it does not patch source files or import repo tests.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types


SECRET_NAME = "QQBOT_PLUGIN_SCOPE_VERIFY_SECRET"


def _installation_root() -> Path:
    here = Path(__file__).resolve().parent
    if (here / "agent" / "secret_scope.py").is_file() and (here / "hermes_cli" / "plugins_loader.py").is_file():
        return here
    parent = here.parent
    if (parent / "agent" / "secret_scope.py").is_file() and (parent / "hermes_cli" / "plugins_loader.py").is_file():
        return parent
    raise RuntimeError("run this verifier from a Hermes installation containing agent/ and hermes_cli/")


def main() -> int:
    root = _installation_root()
    sys.path.insert(0, str(root))
    original_home = os.environ.get("HERMES_HOME")
    original_secret = os.environ.get(SECRET_NAME)
    checks = 0
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(name + (f": {detail}" if detail else ""))

    try:
        with tempfile.TemporaryDirectory(prefix="hermes-plugin-scope-verify-") as raw_temp:
            temp = Path(raw_temp)
            process_home = temp / "process"
            profile_a = temp / "profile-a"
            profile_b = temp / "profile-b"
            profile_missing = temp / "profile-missing"
            for home in (process_home, profile_a, profile_b, profile_missing):
                home.mkdir()
            (profile_a / ".env").write_text(f"{SECRET_NAME}=synthetic-a\n", encoding="utf-8")
            (profile_b / ".env").write_text(f"{SECRET_NAME}=synthetic-b\n", encoding="utf-8")
            os.environ["HERMES_HOME"] = str(process_home)
            os.environ[SECRET_NAME] = "poison-from-process-environment"

            import hermes_constants
            from agent import secret_scope
            from hermes_cli import plugins_loader

            installed_source = Path(plugins_loader.__file__).read_text(encoding="utf-8")
            check("patched_helper_loaded", "QQBOT_HK_PLUGIN_SCOPE_PATCH = \"v1\"" in installed_source
                  and "build_profile_secret_scope(home)" in inspect.getsource(plugins_loader._plugin_home_scope))
            check("initial_scope_empty", secret_scope.current_secret_scope() is None)
            prior_multiplex = secret_scope.is_multiplex_active()
            secret_scope.set_multiplex_active(True)
            try:
                def read_on_deadline(home: Path):
                    observed: dict[str, object] = {}

                    def register():
                        observed["secret"] = secret_scope.get_secret(SECRET_NAME)
                        observed["scope_home"] = secret_scope.current_secret_scope_home()
                        observed["worker"] = plugins_loader.in_plugin_load_worker()
                        return observed["secret"]

                    with plugins_loader._plugin_home_scope(home):
                        observed["override_home"] = str(hermes_constants.get_hermes_home_override())
                        value = plugins_loader.run_with_load_deadline(
                            f"scope-verify:{home.name}", types.SimpleNamespace(_abandon_load=lambda: None), register,
                        )
                    observed["restored_scope"] = secret_scope.current_secret_scope()
                    observed["restored_home"] = hermes_constants.get_hermes_home_override()
                    return value, observed

                for home, expected in ((profile_a, "synthetic-a"), (profile_b, "synthetic-b")):
                    try:
                        value, observed = read_on_deadline(home)
                        check(f"{home.name}_secret", value == expected and observed["secret"] == expected)
                        check(f"{home.name}_deadline_worker", observed["worker"] is True)
                        check(f"{home.name}_scope_home", Path(str(observed["scope_home"])).resolve() == home.resolve())
                        check(f"{home.name}_home_override", Path(str(observed["override_home"])).resolve() == home.resolve())
                        check(f"{home.name}_scope_restored", observed["restored_scope"] is None)
                        check(f"{home.name}_home_restored", observed["restored_home"] is None)
                    except Exception as exc:
                        check(f"{home.name}_deadline_registration", False, type(exc).__name__)

                try:
                    with plugins_loader._plugin_home_scope(profile_missing):
                        missing_value = secret_scope.get_secret(SECRET_NAME)
                    check("missing_profile_does_not_fall_back", missing_value is None)
                except Exception as exc:
                    check("missing_profile_does_not_fall_back", False, type(exc).__name__)

                try:
                    previous_override = hermes_constants.get_hermes_home_override()
                    try:
                        with plugins_loader._plugin_home_scope(profile_a):
                            raise RuntimeError("synthetic scope unwind")
                    except RuntimeError as exc:
                        check("synthetic_exception_seen", str(exc) == "synthetic scope unwind")
                    check("exception_restores_scope", secret_scope.current_secret_scope() is None)
                    check("exception_restores_home", hermes_constants.get_hermes_home_override() == previous_override)
                except Exception as exc:
                    check("exception_restores_home_and_scope", False, type(exc).__name__)

                dynamic = {SECRET_NAME: "same-home-dynamic"}
                token = secret_scope.set_secret_scope(dynamic, profile_home=str(profile_a))
                try:
                    with plugins_loader._plugin_home_scope(profile_a):
                        check("same_home_scope_reused", secret_scope.current_secret_scope() is dynamic
                              and secret_scope.get_secret(SECRET_NAME) == "same-home-dynamic")
                    check("dynamic_scope_survives_context", secret_scope.current_secret_scope() is dynamic)
                finally:
                    secret_scope.reset_secret_scope(token)

                try:
                    secret_scope.get_secret(SECRET_NAME)
                    raise AssertionError("unscoped multiplex read unexpectedly succeeded")
                except secret_scope.UnscopedSecretError:
                    check("external_read_fails_closed", True)
                except AssertionError:
                    check("external_read_fails_closed", False)

                barrier = threading.Barrier(2)

                def parallel_read(home: Path):
                    with plugins_loader._plugin_home_scope(home):
                        barrier.wait(timeout=4)
                        return plugins_loader.run_with_load_deadline(
                            f"parallel-scope-verify:{home.name}",
                            types.SimpleNamespace(_abandon_load=lambda: None),
                            lambda: secret_scope.get_secret(SECRET_NAME),
                        )

                try:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        future_a = pool.submit(parallel_read, profile_a)
                        future_b = pool.submit(parallel_read, profile_b)
                        parallel_values = (future_a.result(timeout=8), future_b.result(timeout=8))
                    check("parallel_contexts_isolated", parallel_values == ("synthetic-a", "synthetic-b"))
                except Exception as exc:
                    check("parallel_contexts_isolated", False, type(exc).__name__)
            finally:
                secret_scope.set_multiplex_active(prior_multiplex)
    except Exception as exc:
        failures.append(f"verifier_setup: {type(exc).__name__}: {exc}")
    finally:
        if original_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = original_home
        if original_secret is None:
            os.environ.pop(SECRET_NAME, None)
        else:
            os.environ[SECRET_NAME] = original_secret
        with contextlib.suppress(ValueError):
            sys.path.remove(str(root))

    report = {
        "status": "passed" if checks > 0 and not failures else "failed",
        "checks": checks,
        "passed": checks - len(failures),
        "failures": failures,
        "cli_end_to_end": "verified separately by gateway status checks in verify-server",
        "scope": "installed native helper and deadline worker; temporary synthetic profiles; no source patching",
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
