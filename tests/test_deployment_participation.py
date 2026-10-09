"""Run the real deployment gate against the shipped participation policy."""
from pathlib import Path
import unittest
from collections.abc import Mapping
import yaml

ROOT = Path(__file__).resolve().parents[1]


class DeploymentParticipationTests(unittest.TestCase):
    def setUp(self):
        config = yaml.safe_load((ROOT / "config/hermes-config.yaml").read_text(encoding="utf-8"))
        self.ambient = config["plugins"]["entries"]["smart_group_qq"]["settings"]["ambient"]
        script = (ROOT / "scripts/verify-server.sh").read_text(encoding="utf-8")
        self.gate = script.split('participation_config = ambient_config.get("participation")', 1)[1].split('display_qq =', 1)[0]
        self.gate = 'participation_config = ambient_config.get("participation")' + self.gate

    def check_policy(self, ambient):
        exec(compile(self.gate, "verify-server.sh participation gate", "exec"),
             {"ambient_config": ambient, "Mapping": Mapping})

    def test_shipped_policy_passes(self):
        self.check_policy(self.ambient)

    def test_legacy_or_unbounded_policy_rejected(self):
        for key, value in (("cooldown_seconds", 0), ("min_confidence", 0.55),
                           ("max_interjections_per_minute", 0), ("unanswered_pause_seconds", 0)):
            with self.subTest(key=key):
                policy = {**self.ambient["participation"], key: value}
                with self.assertRaises(SystemExit):
                    self.check_policy({**self.ambient, "participation": policy})
