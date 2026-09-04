import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "reconcile-smart-group-cron.py"
_SPEC = importlib.util.spec_from_file_location("smart_group_cron", MODULE_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("cannot load reconciler")
cron_reconciler = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cron_reconciler)


class FakeCronJobs:
    """Only the real cron.jobs CRUD surface used by the reconciler."""

    def __init__(self, jobs=()):
        self.jobs = [dict(job) for job in jobs]
        self.calls = []
        self._next_id = 1

    def list_jobs(self, include_disabled=False):
        self.calls.append(("list", include_disabled))
        if include_disabled:
            return [dict(job) for job in self.jobs]
        return [dict(job) for job in self.jobs if job.get("enabled", True)]

    def create_job(self, **kwargs):
        self.calls.append(("create", dict(kwargs)))
        job = {
            "id": "fake-%d" % self._next_id,
            "name": kwargs["name"],
            "schedule": kwargs["schedule"],
            "deliver": kwargs["deliver"],
            "script": kwargs["script"],
            "no_agent": kwargs["no_agent"],
            "enabled": True,
            "state": "scheduled",
        }
        self._next_id += 1
        self.jobs.append(job)
        return dict(job)

    def update_job(self, job_id, updates):
        self.calls.append(("update", job_id, dict(updates)))
        for job in self.jobs:
            if job["id"] == job_id:
                job.update(updates)
                return dict(job)
        return None

    def remove_job(self, job_id):
        self.calls.append(("remove", job_id))
        before = len(self.jobs)
        self.jobs[:] = [job for job in self.jobs if job["id"] != job_id]
        return len(self.jobs) != before


class SmartGroupCronTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data = Path(self.tempdir.name)
        self.groups = ("group-openid-alpha", "group-openid-beta")
        self.write_declaration(
            [
                {
                    "id": "daily-reminder",
                    "enabled": True,
                    "cron": "0 9 * * *",
                    "target": "allowed_groups",
                    "text": "固定群公告",
                }
            ]
        )
        (self.data / ".env").write_text(
            "QQ_GROUP_ALLOWED_USERS=" + ",".join(self.groups) + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def write_declaration(self, schedules):
        import yaml

        (self.data / "smart-group-schedules.yaml").write_text(
            yaml.safe_dump({"schedules": schedules}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def reconcile(self, api):
        return cron_reconciler.reconcile(data_dir=self.data, cron_api=api)

    def test_idempotent_and_uses_one_owned_job_per_unique_group(self):
        api = FakeCronJobs()
        first = self.reconcile(api)
        second = self.reconcile(api)

        self.assertEqual(first["created"], 2)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["removed"], 0)
        self.assertEqual(len(api.jobs), 2)
        self.assertEqual(
            {job["name"] for job in api.jobs},
            {
                cron_reconciler.owned_job_name("daily-reminder", group)
                for group in self.groups
            },
        )
        self.assertTrue(all(call[1] is True for call in api.calls if call[0] == "list"))

    def test_schedule_and_text_change_updates_without_duplication(self):
        api = FakeCronJobs()
        self.reconcile(api)
        self.write_declaration(
            [
                {
                    "id": "daily-reminder",
                    "enabled": True,
                    "cron": "30 10 * * *",
                    "target": "allowed_groups",
                    "text": "已更新的固定公告",
                }
            ]
        )

        result = self.reconcile(api)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(len(api.jobs), 2)
        self.assertTrue(all(job["schedule"] == "30 10 * * *" for job in api.jobs))
        self.assertEqual(
            {Path(job["script"]).name for job in api.jobs},
            {
                cron_reconciler.generated_script_name("daily-reminder", group)
                for group in self.groups
            },
        )

    def test_group_change_removes_old_owned_jobs_and_keeps_manual_job(self):
        manual = {
            "id": "manual-1",
            "name": "operator-job",
            "schedule": "0 8 * * *",
            "deliver": "local",
            "enabled": True,
        }
        api = FakeCronJobs([manual])
        self.reconcile(api)
        self.write_declaration(
            [
                {
                    "id": "daily-reminder",
                    "enabled": True,
                    "cron": "0 9 * * *",
                    "target": "allowed_groups",
                    "text": "固定群公告",
                }
            ]
        )
        (self.data / ".env").write_text(
            "QQ_GROUP_ALLOWED_USERS=group-openid-beta,group-openid-gamma\n",
            encoding="utf-8",
        )

        result = self.reconcile(api)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["removed"], 1)
        self.assertEqual({job["id"] for job in api.jobs}, {"manual-1", "fake-2", "fake-3"})
        self.assertEqual(api.jobs[0]["name"], "operator-job")
        self.assertFalse(any("group-openid-alpha" in name for name in {job["name"] for job in api.jobs}))

    def test_disabled_schedule_removes_owned_job_and_script(self):
        api = FakeCronJobs()
        self.reconcile(api)
        self.write_declaration(
            [
                {
                    "id": "daily-reminder",
                    "enabled": False,
                    "cron": "0 9 * * *",
                    "target": "allowed_groups",
                    "text": "固定群公告",
                }
            ]
        )

        result = self.reconcile(api)
        self.assertEqual(result["desired"], 0)
        self.assertEqual(result["removed"], 2)
        self.assertEqual(api.jobs, [])
        self.assertEqual(list((self.data / "scripts" / "generated").glob("*.py")), [])

    def test_validation_fails_closed_before_crud_or_file_changes(self):
        api = FakeCronJobs()
        (self.data / "smart-group-schedules.yaml").write_text(
            "schedules:\n  - id: bad id\n    enabled: true\n    cron: '0 9 * * *'\n    target: allowed_groups\n    text: nope\n",
            encoding="utf-8",
        )

        with self.assertRaises(cron_reconciler.ScheduleConfigError):
            self.reconcile(api)
        self.assertEqual(api.calls, [])
        self.assertFalse((self.data / "scripts").exists())

    def test_empty_allow_list_is_fail_closed(self):
        api = FakeCronJobs()
        (self.data / ".env").write_text("QQ_GROUP_ALLOWED_USERS=\n", encoding="utf-8")
        with self.assertRaises(cron_reconciler.ScheduleConfigError):
            self.reconcile(api)
        self.assertEqual(api.calls, [])

    def test_generated_script_stdout_is_exact_and_filename_has_no_openid(self):
        api = FakeCronJobs()
        self.reconcile(api)
        job = api.jobs[0]
        script = self.data / "scripts" / "generated" / Path(job["script"]).name
        output = subprocess.check_output([sys.executable, str(script)])

        self.assertEqual(output.decode("utf-8"), "固定群公告")
        self.assertNotIn(self.groups[0], script.name)
        self.assertNotIn(self.groups[0], str(script))

    def test_invalid_schedule_fields_fail_closed(self):
        invalid_schedules = [
            {
                "id": "duplicate",
                "enabled": True,
                "cron": "0 9 * * *",
                "target": "allowed_groups",
                "text": "one",
            },
            {
                "id": "duplicate",
                "enabled": True,
                "cron": "0 10 * * *",
                "target": "allowed_groups",
                "text": "two",
            },
        ]
        invalid_cases = [
            invalid_schedules,
            [{"id": "bad-target", "enabled": True, "cron": "0 9 * * *", "target": "all", "text": "x"}],
            [{"id": "bad-cron", "enabled": True, "cron": "not cron", "target": "allowed_groups", "text": "x"}],
            [{"id": "empty-text", "enabled": True, "cron": "0 9 * * *", "target": "allowed_groups", "text": "  "}],
            [{"id": "long-text", "enabled": True, "cron": "0 9 * * *", "target": "allowed_groups", "text": "x" * (cron_reconciler.MAX_TEXT_LENGTH + 1)}],
        ]
        for schedules in invalid_cases:
            with self.subTest(schedules=schedules):
                self.write_declaration(schedules)
                api = FakeCronJobs()
                with self.assertRaises(cron_reconciler.ScheduleConfigError):
                    self.reconcile(api)
                self.assertEqual(api.calls, [])


    def test_owned_prefix_is_the_only_removal_boundary(self):
        api = FakeCronJobs(
            [
                {
                    "id": "manual-owned-looking",
                    "name": "smart-group-qqx::not-owned",
                    "schedule": "0 1 * * *",
                    "enabled": True,
                },
                {
                    "id": "stale-owned",
                    "name": "smart-group-qq::removed::deadbeefdead",
                    "schedule": "0 1 * * *",
                    "enabled": True,
                },
            ]
        )
        self.write_declaration([])

        result = self.reconcile(api)
        self.assertEqual(result["removed"], 1)
        self.assertEqual([job["id"] for job in api.jobs], ["manual-owned-looking"])


if __name__ == "__main__":
    unittest.main()
