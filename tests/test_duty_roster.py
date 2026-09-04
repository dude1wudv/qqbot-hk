from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.duty_roster import duty_roster_for, duty_roster_text


class DutyRosterTests(unittest.TestCase):
    def test_before_start_returns_first_week_preview(self):
        now = datetime(2026, 9, 12, 15, 59, tzinfo=timezone.utc)
        self.assertEqual(
            duty_roster_text(now),
            "【值日表预告｜2026年9月13日—9月19日】\n"
            "将于北京时间 2026年9月13日（周日）开始\n"
            "孙：轮休\n常：洗手台\n黄：拖地\n张：厕所",
        )

    def test_first_week_starts_at_beijing_midnight(self):
        now = datetime(2026, 9, 12, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(
            duty_roster_text(now),
            "【本周值日表｜2026年9月13日—9月19日】\n"
            "孙：轮休\n常：洗手台\n黄：拖地\n张：厕所",
        )

    def test_second_week_rotates_each_assignment_forward(self):
        saturday = duty_roster_for(datetime(2026, 9, 19, 15, 59, tzinfo=timezone.utc))
        sunday = duty_roster_for(datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc))

        self.assertEqual(saturday.assignments[0], ("孙", "轮休"))
        self.assertEqual(
            sunday.assignments,
            (("孙", "洗手台"), ("常", "拖地"), ("黄", "厕所"), ("张", "轮休")),
        )
        self.assertEqual(sunday.week_start.isoformat(), "2026-09-20")
        self.assertEqual(sunday.week_end.isoformat(), "2026-09-26")

    def test_rotation_repeats_after_four_weeks(self):
        roster = duty_roster_for(datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(
            roster.assignments,
            (("孙", "轮休"), ("常", "洗手台"), ("黄", "拖地"), ("张", "厕所")),
        )

    def test_cross_year_range_includes_both_years(self):
        text = duty_roster_text(datetime(2026, 12, 27, 0, 0, tzinfo=timezone.utc))
        self.assertTrue(text.startswith("【本周值日表｜2026年12月27日—2027年1月2日】"))

    def test_explicit_time_must_be_timezone_aware(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            duty_roster_for(datetime(2026, 9, 13, 0, 0))


if __name__ == "__main__":
    unittest.main()
