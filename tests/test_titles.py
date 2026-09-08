"""Session titles: what a key is called, and where that name comes from.

The failure this replaces: five Codex sessions in main-O, five keys, all
labelled "openclaw" — the directory they started in — and the same word on
all five pane borders in the manager. Claude sessions in a multiplexed window
had the same disease with the session name ("mainA" ×5).

Pinned here, because every one of them was arrived at by getting it wrong
first:

  * a human's name for a thing outranks any title a model produced
  * a window name only names a SESSION when the window holds one pane
  * a window name WE wrote is not a human's, or the label freezes forever
  * a title that has not changed writes nothing to tmux (churn is visible)
  * pid → thread comes from Codex's own log stamp, not from timing
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tallydeck.titles import (CodexState, PaneInfo, TitleSync,
                              claude_ai_title, headline, resolve_label)


def pane(target="main-O:1.1", session="main-O", window="1", index="1",
         npanes=1, title="", title_auto="", window_auto=""):
    return PaneInfo(target, "%1", session, window, index, npanes, title,
                    title_auto, window_auto)


class Headline(unittest.TestCase):
    def test_machine_tag_is_dropped(self):
        self.assertEqual(
            headline("[telegram reply <- gmacd, chat=39172309] fix the meter",
                     24), "fix the meter")

    def test_a_sentence_is_reduced_to_its_topic(self):
        # G, 2026-09-08: "we shouldn't use whole sentences — let's just make
        # them really concise and nice." The first 24 characters of a sentence
        # ("I'm just testing if this") name nothing at all.
        self.assertEqual(headline("So, tell me about the burn meter", 24),
                         "Burn meter")
        self.assertEqual(
            headline("I'm just testing if this shows up in the thingamajig "
                     "properly.", 24), "Testing thingamajig")

    def test_a_title_is_left_as_it_was_written(self):
        # Claude Code's ai-title, a spawn slug, a window a human named: short
        # and already about the subject. Do not "improve" them.
        for t in ("RAM problem fixes", "Session titles", "session-titles",
                  "C64 migration"):
            self.assertEqual(headline(t, 24), t)

    def test_cuts_on_a_word_boundary(self):
        out = headline("Automatic relevant session titles for the deck", 24)
        self.assertEqual(out, "Automatic relevant deck")
        self.assertFalse(out.endswith(" "))

    def test_stays_inside_the_key(self):
        for t in ("Read autopilot/PLAYBOOK.md in your working directory and "
                  "report back in four lines",
                  "[tally] decision for offer-session-titles (Run "
                  "separately?): 1 — 1 · Yes",
                  "Can you make it so the Stream Deck shows what each "
                  "session is doing?"):
            out = headline(t, 24)
            self.assertLessEqual(len(out), 24, out)
            self.assertGreaterEqual(len(out), 4, out)
            self.assertLessEqual(len(out.split()), 4, out)

    def test_code_span_is_not_a_title(self):
        self.assertEqual(headline("`rm -rf /` run this?", 24), "Run this")

    def test_first_sentence_wins_over_the_paragraph(self):
        self.assertEqual(
            headline("Fix the deck. Then go and do six other things.", 30),
            "Fix the deck")


class Precedence(unittest.TestCase):
    def test_human_pane_title_beats_everything(self):
        self.assertEqual(resolve_label(
            harness_title="Codex integration with TokenBurn",
            pane=pane(title="pearl payout"),
            session_name="pearlbridge"), "pearl payout")

    def test_our_own_pane_title_does_not_beat_the_harness(self):
        # Otherwise the first sync freezes the label: we would be reading our
        # own output back as if a human had typed it.
        self.assertEqual(resolve_label(
            harness_title="RAM problem fixes",
            pane=pane(title="Old subject", title_auto="Old subject"),
            session_name="mainB"), "RAM problem fixes")

    def test_window_name_names_the_session_only_when_alone(self):
        one = pane(window="Session titles", npanes=1)
        many = pane(window="Session titles", npanes=5)
        self.assertEqual(resolve_label(harness_title="deck work", pane=one),
                         "Session titles")
        self.assertEqual(resolve_label(harness_title="deck work", pane=many),
                         "deck work")

    def test_our_own_window_name_is_not_a_human_name(self):
        p = pane(window="deck work", npanes=1, window_auto="deck work")
        self.assertEqual(resolve_label(harness_title="newer subject", pane=p),
                         "newer subject")

    def test_chosen_session_name_beats_the_harness_title(self):
        # `tally spawn <slug>` names a session on purpose; the deck, the offer
        # log and G all refer to it by that slug.
        self.assertEqual(resolve_label(
            harness_title="Automatic relevant session titles",
            pane=pane(target="session-titles:1.1", session="session-titles",
                      window="claude"),
            session_name="session-titles"), "session-titles")

    def test_auto_session_names_defer_to_the_title(self):
        for name in ("main", "mainA", "mainB", "main-O", "main-2"):
            self.assertEqual(resolve_label(
                harness_title="Aviation logs analysis",
                pane=pane(session=name, window="claude", npanes=5),
                session_name=name), "Aviation logs analysis", name)

    def test_directory_is_the_last_resort(self):
        # A session with no pane (no tmux name to borrow) and no title: all
        # that is left is where it is working.
        self.assertEqual(resolve_label(cwd="/home/openclaw/projects/c64"),
                         "c64")

    def test_untitled_session_keeps_its_session_name(self):
        self.assertEqual(resolve_label(harness_title="", session_name="mainB",
                                       cwd="/home/openclaw"), "mainB")


class ClaudeTitle(unittest.TestCase):
    def test_last_ai_title_wins(self):
        lines = ['{"type":"ai-title","aiTitle":"First subject"}',
                 '{"type":"assistant","message":{"content":[]}}',
                 '{"type":"ai-title","aiTitle":"Current subject"}']
        self.assertEqual(claude_ai_title(lines), "Current subject")

    def test_placeholder_is_not_a_title(self):
        self.assertEqual(
            claude_ai_title(['{"type":"ai-title","aiTitle":"Claude Code"}']),
            "")

    def test_junk_lines_do_not_raise(self):
        self.assertEqual(claude_ai_title(["not json", '{"ai-title"']), "")


class FakeTmux:
    """Records what would be written; serves a fixed pane table."""

    def __init__(self, panes):
        self._panes = panes
        self.titles: list[tuple] = []
        self.renames: list[tuple] = []

    def panes(self, force=False):
        return self._panes

    def info(self, target):
        return self._panes.get(target)

    def set_pane_title(self, target, title):
        self.titles.append((target, title))

    def rename_window(self, session, index, name):
        self.renames.append((session, index, name))


class Sync(unittest.TestCase):
    def sync(self, panes):
        t = FakeTmux(panes)
        return t, TitleSync(t, every=0)

    def test_unchanged_title_writes_nothing(self):
        p = pane(title="RAM problem fixes", title_auto="RAM problem fixes",
                 window="RAM problem fixes", window_auto="RAM problem fixes")
        t, s = self.sync({"mainB:1.2": p})
        s.push([("mainB:1.2", "RAM problem fixes")], force=True)
        self.assertEqual((t.titles, t.renames), ([], []))

    def test_manual_title_is_never_overwritten(self):
        t, s = self.sync({"mainB:1.2": pane(title="mine")})
        s.push([("mainB:1.2", "RAM problem fixes")], force=True)
        self.assertEqual(t.titles, [])

    def test_multi_pane_window_is_not_renamed(self):
        t, s = self.sync({"main-O:1.1": pane(npanes=5)})
        s.push([("main-O:1.1", "deck work")], force=True)
        self.assertEqual(t.titles, [("main-O:1.1", "deck work")])
        self.assertEqual(t.renames, [],
                         "a five-session window must not be named after one")

    def test_single_pane_generic_window_is_renamed(self):
        t, s = self.sync({"abra:1.1": pane(target="abra:1.1", session="abra")})
        s.push([("abra:1.1", "C64 build status")], force=True)
        self.assertEqual(t.renames, [("abra", "1", "C64 build status")])

    def test_human_named_window_is_left_alone(self):
        t, s = self.sync({"main-O:1.1": pane(window="Session titles")})
        s.push([("main-O:1.1", "deck work")], force=True)
        self.assertEqual(t.renames, [])

    def test_throttle_holds_between_pushes(self):
        t = FakeTmux({"abra:1.1": pane(target="abra:1.1", session="abra")})
        s = TitleSync(t, every=600)
        self.assertEqual(s.push([("abra:1.1", "one")]), 2)
        self.assertEqual(s.push([("abra:1.1", "two")]), 0)

    def test_unknown_pane_is_skipped(self):
        t, s = self.sync({})
        s.push([("ghost:9.9", "nothing")], force=True)
        self.assertEqual(t.titles, [])


class CodexIdentity(unittest.TestCase):
    """pid → thread, from the `pid:<pid>:<uuid>` stamp Codex writes on every
    log row. This is what tells five sessions in one directory apart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        con = sqlite3.connect(self.home / "logs_2.sqlite")
        con.execute("create table logs (id integer primary key, "
                    "thread_id text, process_uuid text)")
        con.executemany("insert into logs values (?,?,?)", [
            (1, "thread-a", "pid:111:aaa"),
            (2, None, "pid:111:aaa"),
            (3, "thread-sub", "pid:111:aaa"),   # a subagent thread, not shown
            (4, "thread-b", "pid:222:bbb"),
        ])
        con.commit()
        con.close()
        con = sqlite3.connect(self.home / "state_5.sqlite")
        con.execute("create table threads (id text primary key, title text, "
                    "cwd text, updated_at integer)")
        con.executemany("insert into threads values (?,?,?,?)", [
            ("thread-a", "Wire the burn meter to Codex", "/home/openclaw", 2),
            ("thread-b", "Something else entirely", "/home/openclaw", 1)])
        con.commit()
        con.close()
        self.state = CodexState(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pid_names_its_thread(self):
        self.assertEqual(
            self.state.thread_for_pid(111, {"thread-a", "thread-b"}),
            "thread-a")
        self.assertEqual(
            self.state.thread_for_pid(222, {"thread-a", "thread-b"}),
            "thread-b")

    def test_threads_the_deck_is_not_showing_are_not_answers(self):
        # The newest thread a pid logged may be a subagent or a compaction
        # thread; only a rollout the deck lists can be routed to.
        self.assertEqual(self.state.thread_for_pid(111, {"thread-sub"}),
                         "thread-sub")
        self.assertEqual(self.state.thread_for_pid(111, {"thread-a"}),
                         "thread-a")

    def test_unknown_pid_resolves_to_nothing(self):
        self.assertEqual(self.state.thread_for_pid(999, {"thread-a"}), "")

    def test_incremental_read_sees_new_rows(self):
        self.assertEqual(self.state.thread_for_pid(333, {"thread-c"}), "")
        con = sqlite3.connect(self.home / "logs_2.sqlite")
        con.execute("insert into logs values (5,'thread-c','pid:333:ccc')")
        con.commit()
        con.close()
        self.assertEqual(self.state.thread_for_pid(333, {"thread-c"}),
                         "thread-c")

    def test_title_comes_from_codex_own_thread_table(self):
        self.assertEqual(self.state.title("thread-a"),
                         "Wire the burn meter to Codex")
        self.assertEqual(self.state.title("nope"), "")

    def test_missing_database_is_not_an_error(self):
        empty = CodexState(Path(self.tmp.name) / "gone")
        self.assertEqual(empty.title("thread-a"), "")
        self.assertEqual(empty.thread_for_pid(111, {"thread-a"}), "")


class TwoCodexSessionsInOneDirectory(unittest.TestCase):
    """The case the deck could not represent: main-O, five Codex panes, all
    started in /home/openclaw within the same second. cwd cannot tell them
    apart and neither can start order, so all five collapsed to one unroutable
    key called "openclaw". The pid stamp in Codex's log separates them."""

    A = "01a08020-6011-76d0-8aee-b1add4254771"
    B = "01a08020-6010-7a22-8fc8-01b3bb30cc48"

    def setUp(self):
        import json
        import os
        import time
        from datetime import datetime, timezone
        from tallydeck.sources import codex_sessions as cx

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        home = root / "codex"
        home.mkdir()
        day = root / "sessions" / "2026" / "09" / "08"
        day.mkdir(parents=True)
        self.start = time.time() - 600
        stamp = datetime.fromtimestamp(self.start + 60,
                                       timezone.utc).isoformat()
        for uuid in (self.A, self.B):
            fp = day / f"rollout-2026-09-08T08-26-40-{uuid}.jsonl"
            fp.write_text("\n".join(json.dumps(r) for r in [
                {"type": "session_meta", "payload": {
                    "session_id": uuid, "cwd": "/home/openclaw",
                    "originator": "codex-tui", "timestamp": stamp}},
                {"type": "event_msg", "payload": {
                    "type": "task_complete",
                    "last_agent_message": "Deck labels are wired up."}},
            ]) + "\n")
            os.utime(fp, None)

        con = sqlite3.connect(home / "logs_2.sqlite")
        con.execute("create table logs (id integer primary key, "
                    "thread_id text, process_uuid text)")
        con.executemany("insert into logs values (?,?,?)", [
            (1, self.A, "pid:1081626:aaa"), (2, self.B, "pid:1081634:bbb")])
        con.commit()
        con.close()
        con = sqlite3.connect(home / "state_5.sqlite")
        con.execute("create table threads (id text primary key, title text, "
                    "cwd text, updated_at integer)")
        con.executemany("insert into threads values (?,?,?,?)", [
            (self.A, "Automatic session titles for the deck",
             "/home/openclaw", 2),
            (self.B, "So are you the best at 3D work, or what?",
             "/home/openclaw", 1)])
        con.commit()
        con.close()

        self.src = cx.CodexSessionsSource(
            root=str(root / "sessions"), codex_home=str(home), dwell=0,
            sync_titles=False)
        self.src._live_codex_procs = lambda: [
            ("main-O:1.1", "/home/openclaw", self.start, 1081626),
            ("main-O:1.3", "/home/openclaw", self.start, 1081634)]
        self.src.tmux.info = lambda target: None      # no tmux in the test

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_session_resolves_to_its_own_pane(self):
        self.assertEqual(self.src._codex_panes(),
                         {self.A: "main-O:1.1", self.B: "main-O:1.3"})

    def test_each_key_is_named_after_its_own_thread(self):
        labels = {s.meta["session"]: s.label for s in self.src.poll()}
        self.assertEqual(labels[self.A], "Automatic session titles")
        self.assertEqual(labels[self.B], "3D work")
        self.assertNotIn("openclaw", labels.values())

    def test_each_session_gets_its_own_key(self):
        # Codex thread ids are UUIDv7 — the first half is a millisecond
        # timestamp. Five panes opened together share it, so keying on
        # uuid[:8] merged all five into one hub entry and four sessions never
        # reached the deck at all.
        from tallydeck.sources.codex_sessions import signal_id
        self.assertEqual(self.A[:8], self.B[:8], "the collision this guards")
        self.assertNotEqual(signal_id(self.A), signal_id(self.B))
        self.assertEqual(len({s.id for s in self.src.poll()}), 2)

    def test_the_press_target_matches_the_key_it_is_on(self):
        for s in self.src.poll():
            self.assertEqual((s.action or {})["argv"][-1], s.id)
            self.assertEqual((s.action or {})["argv"][1], s.meta["tmux"])

    def test_the_hub_keeps_both_of_them(self):
        # The merge is `fresh[sig.id] = sig`, so two sessions sharing an id is
        # not a cosmetic clash: the working session is silently overwritten by
        # whichever finished one is polled after it.
        from tallydeck.hub import Hub
        ids = {s.id for s in Hub([self.src]).poll()}
        self.assertEqual(len(ids), 2, ids)

    def test_a_press_can_route_to_either_of_them(self):
        panes = {s.meta["session"]: s.meta["tmux"] for s in self.src.poll()}
        self.assertEqual(len(set(panes.values())), 2)
        self.assertTrue(all(s.meta["exact_pane"] for s in self.src.poll()))


if __name__ == "__main__":
    unittest.main()
