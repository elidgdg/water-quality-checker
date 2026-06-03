import unittest
from datetime import datetime, timedelta

import wild_swim_checker as checker


NOW = datetime(2026, 5, 30, 7, 0, tzinfo=checker.LONDON_TZ)


def site(name, status="Not discharging", past48=False, start=None, stop=None):
    return checker.SiteStatus(
        name=name,
        alert_status=status,
        alert_past_48_hours=past48,
        start=start,
        stop=stop,
        receiving_water_course="River Test",
    )


def base_sites():
    return {name: site(name) for name in checker.MONITORED_SITES}


class DecisionTests(unittest.TestCase):
    def test_no_discharges_returns_ok(self):
        decision = checker.evaluate(base_sites(), NOW)

        self.assertEqual(decision.status, "OK")

    def test_witney_recent_discharge_cancels(self):
        self.assert_critical_site_cancels("Witney")

    def test_cassington_recent_discharge_cancels(self):
        self.assert_critical_site_cancels("Cassington")

    def assert_critical_site_cancels(self, critical_site):
        sites = base_sites()
        sites[critical_site] = site(critical_site, past48=True)

        decision = checker.evaluate(sites, NOW)

        self.assertEqual(decision.status, "CANCEL")
        self.assertIn(critical_site, decision.reasons[0])

    def test_one_upstream_site_under_four_hours_is_ok(self):
        sites = base_sites()
        sites["Combe"] = site(
            "Combe",
            past48=True,
            start=NOW - timedelta(hours=2),
            stop=NOW - timedelta(hours=1),
        )

        decision = checker.evaluate(sites, NOW)

        self.assertEqual(decision.status, "OK")

    def test_two_upstream_sites_cancel(self):
        sites = base_sites()
        sites["Combe"] = site("Combe", past48=True)
        sites["Woodstock"] = site("Woodstock", past48=True)

        decision = checker.evaluate(sites, NOW)

        self.assertEqual(decision.status, "CANCEL")
        self.assertIn("Two or more upstream sites", decision.reasons[0])

    def test_one_upstream_site_at_four_hours_cancels(self):
        sites = base_sites()
        sites["Standlake"] = site(
            "Standlake",
            past48=True,
            start=NOW - timedelta(hours=5),
            stop=NOW - timedelta(hours=1),
        )

        decision = checker.evaluate(sites, NOW)

        self.assertEqual(decision.status, "CANCEL")
        self.assertIn("Standlake", decision.reasons[0])
        self.assertIn("4h", decision.reasons[0])

    def test_currently_discharging_duration_uses_now_when_stop_missing(self):
        sites = base_sites()
        sites["South Leigh"] = site(
            "South Leigh",
            status="Discharging",
            past48=False,
            start=NOW - timedelta(hours=4, minutes=30),
            stop=None,
        )

        decision = checker.evaluate(sites, NOW)

        self.assertEqual(decision.status, "CANCEL")
        self.assertIn("4h 30m", decision.reasons[0])

    def test_offline_status_warns_but_does_not_cancel(self):
        sites = base_sites()
        sites["Woodstock"] = site("Woodstock", status="Offline")

        decision = checker.evaluate(sites, NOW)

        self.assertEqual(decision.status, "OK")
        self.assertEqual(
            decision.warnings,
            ("Woodstock monitor is offline/unknown; verify manually if concerned.",),
        )


class ParsingTests(unittest.TestCase):
    def test_missing_site_requires_manual_check(self):
        features = [
            {
                "attributes": {
                    "LocationName": name,
                    "AlertStatus": "Not discharging",
                    "AlertPast48Hours": "false",
                    "MostRecentDischargeAlertStart": None,
                    "MostRecentDischargeAlertStop": None,
                    "ReceivingWaterCourse": "River Test",
                }
            }
            for name in checker.MONITORED_SITES
            if name != "Witney"
        ]

        with self.assertRaisesRegex(checker.ManualCheckRequired, "Missing monitored site"):
            checker.parse_sites(features)


class ScheduleTests(unittest.TestCase):
    def test_due_runs_match_fixed_schedule(self):
        sunday_evening = datetime(2026, 5, 31, 19, 0, tzinfo=checker.LONDON_TZ)
        runs = checker.due_runs(sunday_evening)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].swim_key, "monday")
        self.assertEqual(runs[0].check_type, "evening")

    def test_due_runs_allow_late_github_actions_run_before_swim(self):
        delayed_wednesday_evening = datetime(2026, 6, 3, 21, 40, tzinfo=checker.LONDON_TZ)
        runs = checker.due_runs(delayed_wednesday_evening)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].swim_key, "thursday")
        self.assertEqual(runs[0].check_type, "evening")

    def test_due_runs_skip_sent_check(self):
        delayed_wednesday_evening = datetime(2026, 6, 3, 21, 40, tzinfo=checker.LONDON_TZ)
        sent_run = checker.due_runs(delayed_wednesday_evening)[0]
        runs = checker.due_runs(delayed_wednesday_evening, {sent_run.id(): "2026-06-03T21:40:00+01:00"})

        self.assertEqual(runs, [])

    def test_due_runs_stop_when_swim_starts(self):
        swim_started = datetime(2026, 6, 4, 8, 0, tzinfo=checker.LONDON_TZ)
        runs = checker.due_runs(swim_started)

        self.assertEqual(runs, [])

    def test_due_runs_do_not_treat_old_monday_check_as_due_later_in_week(self):
        friday = datetime(2026, 6, 5, 12, 0, tzinfo=checker.LONDON_TZ)
        runs = checker.due_runs(friday)

        self.assertEqual(runs, [])


if __name__ == "__main__":
    unittest.main()
