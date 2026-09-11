"""A background fleet is ONE key, and it never asks for anything.

Fixture is the real thing: the four c64 autopilot workers that were running in
the `oneshot` tmux session on 2026-09-08, plus the unrelated jobs that were
running beside them. The invariants pinned here are the ones that make the key
worth having — collapse to one, keep the count honest, route to the worker that
moved last — and the one that makes it safe: a status key must never wear an
alarm state, because a key that flashes for something nobody can answer teaches
the operator to ignore the deck.

Plain unittest: the rest of the suite needs pytest, which is not installed on
the host this runs on.
"""
import unittest

from tallydeck.signal import ATTENTION, BLOCKED, IDLE, WORKING
from tallydeck.sources.fleet import BackgroundFleetSource

NOW = 1788903571.0

# target, window name, seconds since that window last produced output
LIVE = [
    ("oneshot:1.1", "job-1788890942-4169991-dc-bt-site-i020", 7772),
    ("oneshot:2.1", "job-1788902390-811372-c64-B-fx-reel-handover-framework", 60),
    ("oneshot:3.1", "job-1788902391-811634-c64-B2-music-original-dxm-flavour", 55),
    ("oneshot:4.1", "job-1788902392-811823-c64-A-medium-art-pipeline", 50),
    ("oneshot:5.1", "job-1788902393-812114-c64-A2-fx-layered-composite", 10),
    ("oneshot:6.1", "job-1788902742-848745-session-memefi-sum", 768),
    ("oneshot:7.1", "job-1788902948-861726-session-abra", 0),
]


def source(**opts) -> BackgroundFleetSource:
    src = BackgroundFleetSource(burn=False, **opts)
    src._runner_panes = lambda: [
        {"target": t, "window": w, "activity": NOW - quiet,
         "pid": "0", "cwd": "/home/openclaw/projects/c64"}
        for t, w, quiet in LIVE]
    src._account = lambda pid: ""          # /proc is not a fixture
    return src


def by_id(sigs) -> dict:
    return {s.id: s for s in sigs}


class OneKeyPerFleet(unittest.TestCase):
    def setUp(self):
        self.sigs = by_id(source().poll())

    def test_four_workers_collapse_into_one_key(self):
        c64 = [s for s in self.sigs.values() if s.meta["fleet"] == "c64"]
        self.assertEqual(len(c64), 1, "four workers, one key")
        self.assertEqual(c64[0].meta["workers"], 4)
        self.assertIn("4 jobs", c64[0].sublabel)
        self.assertEqual(c64[0].label, "c64 fleet")
        # The word is config, because it has to FIT: "c64 background" is
        # broken across two lines mid-word at 96px.
        self.assertEqual(by_id(source(label_word="background").poll())
                         ["fleet/c64"].label, "c64 background")

    def test_the_fleet_is_derived_not_hardcoded(self):
        """`session-*` jobs group the same way c64 does — by label prefix."""
        self.assertIn("fleet/session", self.sigs)
        self.assertEqual(self.sigs["fleet/session"].meta["workers"], 2)

    def test_a_lone_job_is_not_a_fleet(self):
        """dc-bt-site was the only dc job running: no key, no noise."""
        self.assertNotIn("fleet/dc", self.sigs)
        self.assertEqual(source(min_workers=1).poll().__len__(), 3)

    def test_reviewer_suffix_does_not_split_a_fleet(self):
        """garage-review-opus and garage-review-sonnet are one fleet."""
        src = source()
        src._runner_panes = lambda: [
            {"target": "oneshot:2.1", "window": "job-1-1-garage-review-sonnet",
             "activity": NOW, "pid": "0", "cwd": "/tmp"},
            {"target": "oneshot:3.1", "window": "job-1-2-garage-review-opus",
             "activity": NOW, "pid": "0", "cwd": "/tmp"},
            {"target": "oneshot:4.1", "window": "job-1-3-nn-lead-audit-opus",
             "activity": NOW, "pid": "0", "cwd": "/tmp"},
        ]
        got = {s.meta["fleet"]: s for s in src.poll()}
        self.assertEqual(got["garage"].meta["workers"], 2)
        self.assertNotIn("nn", got)

    def test_it_never_asks_for_anything(self):
        for s in self.sigs.values():
            self.assertIn(s.state, (WORKING, IDLE))
            self.assertNotIn(s.state, (ATTENTION, BLOCKED))
            self.assertFalse(s.wants_flash, "a fleet key must never flash")

    def test_it_outranks_the_oneshot_floor_but_not_a_busy_session(self):
        # claude-sessions pins one-shots to -10 and gives a live session its
        # burn rate in bytes/sec, which is comfortably above the default here.
        for s in self.sigs.values():
            self.assertGreater(s.priority, -10)
            self.assertLess(s.priority, 100)

    def test_the_press_opens_the_worker_that_moved_last(self):
        c64 = self.sigs["fleet/c64"]
        self.assertEqual(c64.meta["tmux"], "oneshot:5.1")
        argv = (c64.action or {}).get("argv") or []
        self.assertTrue(argv, "the key must route somewhere")
        self.assertTrue(argv[0].endswith("tally-popup-route"),
                        "reuse the routing every session key already uses")
        self.assertEqual(argv[1], "oneshot:5.1")
        self.assertEqual(argv[7], "fleet/c64")

    def test_the_other_workers_are_named_not_hidden(self):
        d = self.sigs["fleet/c64"].detail
        for target in ("oneshot:2.1", "oneshot:3.1", "oneshot:4.1"):
            self.assertIn(target, d)
        self.assertIn("attach -t oneshot", d)

    def test_quiet_workers_read_idle_rather_than_working(self):
        old = source()
        old._runner_panes = lambda: [
            {"target": t, "window": w, "activity": NOW - 3600, "pid": "0",
             "cwd": "/tmp"} for t, w, _ in LIVE if "c64" in w]
        self.assertEqual(old.poll()[0].state, IDLE)

    def test_off_unless_enabled(self):
        self.assertEqual(source(enabled=False).poll(), [])

    def test_ignored_fleets_get_no_key(self):
        self.assertNotIn("fleet/c64", by_id(source(ignore=["c64"]).poll()))


if __name__ == "__main__":
    unittest.main()
