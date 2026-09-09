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
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tallydeck import titles as titles_mod
from tallydeck.titles import (CodexState, PaneInfo, TitleSync, Tmux, _SEP,
                              authored_titles, claude_ai_title, headline,
                              record_renames, registry_target, resolve_label,
                              trim)


def pane(target="main-O:1.1", session="main-O", window="1", index="1",
         npanes=1, title="", title_auto="", window_auto="", authored=(),
         pane_id="%1"):
    return PaneInfo(target, pane_id, session, window, index, npanes, title,
                    title_auto, window_auto, frozenset(authored))


class TitleVersusProse(unittest.TestCase):
    """Two kinds of input, two ways to shorten, and they are not swappable.

    Claude Code generates a real title. Codex files the operator's first
    message under `threads.title`. Reducing a title to its content words is
    destruction: "Getting everything up and running" missed the pass-through
    by nine characters, lost every word that was a stopword, and G's key read
    `Running`.
    """

    def test_a_long_title_is_trimmed_not_reduced(self):
        self.assertEqual(trim("Getting everything up and running", 24),
                         "Getting everything up")
        self.assertEqual(
            resolve_label(harness_title="Getting everything up and running",
                          session_name="main"), "Getting everything up")

    def test_prose_is_still_reduced(self):
        self.assertEqual(
            resolve_label(harness_prose="So are you the best at 3D work, "
                                        "or what?", session_name="main-O"),
            "3D work")

    def test_a_trim_never_ends_on_a_dangling_connective(self):
        self.assertEqual(trim("Self-hosted storage vs rsync.net", 24),
                         "Self-hosted storage")

    def test_a_name_a_human_typed_is_never_reduced(self):
        # G types a title to be found again; shredding it loses the words he
        # chose to find it by.
        long_name = "Storage box expansion to 5 TB"
        self.assertEqual(
            resolve_label(pane=pane(title=long_name), session_name="main"),
            "Storage box expansion")


class ColdStartCost(unittest.TestCase):
    """Reading Codex's log must not cost seconds per run.

    The cron pass is a fresh process every minute. Codex's log reached 74 MB
    after one busy day, and the backward scan that finds each pid's thread
    cost ~2s of it every time — so what that scan learns is kept on disk and
    each run reads only the rows appended since.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "codex"
        self.home.mkdir()
        self.state_dir = Path(self.tmp.name) / "state"
        con = sqlite3.connect(self.home / "logs_2.sqlite")
        con.execute("create table logs (id integer primary key, "
                    "thread_id text, process_uuid text)")
        con.executemany("insert into logs values (?,?,?)",
                        [(i, f"thread-{i}", f"pid:{i}:x") for i in range(1, 51)])
        con.commit()
        con.close()
        os.environ["TALLYDECK_STATE"] = str(self.state_dir)

    def tearDown(self):
        os.environ.pop("TALLYDECK_STATE", None)
        self.tmp.cleanup()

    def test_a_foreign_codex_home_never_writes_the_shared_cache(self):
        # A test pointing at its own directory must not put that directory's
        # row ids in the file the live hub reads.
        CodexState(self.home).pid_threads()
        self.assertFalse((self.state_dir / "codex-pid-threads.json").exists())

    def test_the_cache_carries_across_processes(self):
        st = CodexState()                      # real home, caching enabled
        st.home = self.home                    # …pointed at the fixture
        st._cache = True
        st._pid_threads, st._last_log_id = {}, -1
        self.assertEqual(len(st.pid_threads()), 50)
        cache = self.state_dir / "codex-pid-threads.json"
        self.assertTrue(cache.exists())
        self.assertEqual(json.loads(cache.read_text())["last_id"], 50)

    def test_a_cache_from_another_database_is_ignored(self):
        cache = self.state_dir / "codex-pid-threads.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"db": "/some/other/logs_9.sqlite",
                                     "last_id": 9999, "pids": {"1": []}}))
        st = CodexState()
        self.assertEqual(st._last_log_id, -1,
                         "row ids from another database are not ours")
        self.assertEqual(st._pid_threads, {})

    def test_a_corrupt_cache_is_not_fatal(self):
        cache = self.state_dir / "codex-pid-threads.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("{not json")
        self.assertEqual(CodexState()._last_log_id, -1)


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

    def test_a_title_does_not_end_mid_thought(self):
        # What the packer produced when the other half of the phrase did not
        # fit: "Self-hosted storage vs" reads as damage, not as a topic. The
        # same word in the MIDDLE is the whole point of the title.
        self.assertEqual(
            headline("Self-hosted storage vs cloud backup options", 24),
            "Self-hosted storage")
        self.assertEqual(headline("Postgres vs SQLite for the ledger", 24),
                         "Postgres vs SQLite")

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


class OurOwnTitlesAreCorrectable(unittest.TestCase):
    """G's screenshot, 2026-09-08: main:1.4 read "Storage expansion" while the
    session in it had moved on to "Getting everything up and running", and
    nothing could ever fix it — a bulk retitle set @tally_title without the
    matching @tally_title_auto, so the value looked exactly like a name G had
    typed, and both resolve_label and TitleSync treat those as sacred.

    The rename ledger is the receipt that says otherwise."""

    LEDGER = [
        {"pane": "%276", "old_title": "Getting everything up and running",
         "old_source": "tallydeck", "new_title": "Storage expansion"},
        {"pane": "%277", "old_title": "Storage box expansion to 5 TB",
         "old_source": "tallydeck", "new_title": "Codex integration"},
        {"pane": "%385", "old_title": "chosen by hand", "old_source": "",
         "new_title": "Businesses easily vibe"},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        (self.state / "title-renames-20260908-085759.json").write_text(
            json.dumps(self.LEDGER))
        titles_mod._ledger["sig"] = None          # not another test's ledger

    def tearDown(self):
        titles_mod._ledger["sig"] = None
        self.tmp.cleanup()

    def authored(self):
        return authored_titles(self.state)

    def test_both_sides_of_a_record_are_ours(self):
        a = self.authored()
        self.assertIn("Storage expansion", a["%276"])       # what we wrote
        self.assertIn("Getting everything up and running", a["%276"])
        self.assertNotIn("chosen by hand", a["%385"],
                         "old_source was not us: that title is a human's")

    def test_a_title_we_wrote_no_longer_outranks_the_session(self):
        p = pane(target="main:1.4", session="main", window="claude", npanes=6,
                 pane_id="%276", title="Storage expansion",
                 authored=self.authored()["%276"])
        self.assertEqual(p.manual_title, "")
        # The label follows the SESSION now, not the title we left on the pane.
        # Asserted as the string G reads, not as `headline(ai)`: a harness
        # title is trimmed, never reduced, and pinning the mechanism here hid
        # the fact that reducing it produced the key `Running`.
        ai = "Getting everything up and running"
        self.assertEqual(resolve_label(harness_title=ai, pane=p,
                                       session_name="main"),
                         "Getting everything up")
        self.assertNotEqual(resolve_label(harness_title=ai, pane=p,
                                          session_name="main"),
                            "Storage expansion")

    def test_a_human_title_on_a_ledgered_pane_still_wins(self):
        # The ledger names TITLES, not panes: a pane we once retitled is not
        # a pane G may never rename.
        p = pane(pane_id="%276", title="pearl payout",
                 authored=self.authored()["%276"])
        self.assertEqual(p.manual_title, "pearl payout")
        self.assertEqual(resolve_label(harness_title="something else",
                                       pane=p), "pearl payout")

    def test_sync_overwrites_a_title_we_wrote(self):
        p = pane(target="main:1.4", session="main", window="claude", npanes=6,
                 pane_id="%276", title="Storage expansion",
                 authored=self.authored()["%276"])
        t = FakeTmux({"main:1.4": p})
        TitleSync(t, every=0).push([("main:1.4", "Getting everything running")],
                                   force=True)
        self.assertEqual(t.titles,
                         [("main:1.4", "Getting everything running")])

    def test_a_torn_or_absent_ledger_is_not_a_human_title(self):
        (self.state / "title-renames-broken.json").write_text("{oh no")
        titles_mod._ledger["sig"] = None
        self.assertIn("Storage expansion", authored_titles(self.state)["%276"])
        self.assertEqual(authored_titles(Path(self.tmp.name) / "gone"), {})

    def test_a_recorded_batch_reads_back(self):
        other = Path(self.tmp.name) / "fresh"
        record_renames([{"pane": "%9", "old_title": "", "old_source": "",
                         "new_title": "Deck alerts"}], other)
        self.assertEqual(authored_titles(other)["%9"], frozenset({"Deck alerts"}))


class FakeListPanes(Tmux):
    """A pane table without a tmux server. Rows are (target, pane_id)."""

    def __init__(self, rows):
        super().__init__(socket="/nonexistent")
        self.rows = rows

    def _run(self, *args, timeout=3):
        if args[:1] != ("list-panes",):
            return ""
        return "".join(
            _SEP.join((target, pane_id, target.split(":", 1)[0], "1", "6",
                       "", "", "", "claude")) + "\n"
            for target, pane_id in self.rows)


class PaneIdIsTheIdentity(unittest.TestCase):
    """The 08:57 retitle moved "Storage box expansion to 5 TB" off %277 and
    onto %276 — a whole window of sessions wearing each other's names. That is
    what remembering a pane as `main:1.4` buys you: the target is a POSITION,
    and closing any pane before it in that window shifts every name after it
    onto the wrong session."""

    ROWS = [("main:1.1", "%274"), ("main:1.2", "%7"), ("main:1.3", "%275"),
            ("main:1.4", "%276"), ("main:1.5", "%277")]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        (self.state / "panes").mkdir()
        titles_mod._ledger["sig"] = None

    def tearDown(self):
        titles_mod._ledger["sig"] = None
        self.tmp.cleanup()

    def record(self, sid8, **rec):
        (self.state / "panes" / f"{sid8}.json").write_text(json.dumps(rec))

    def test_an_id_resolves_to_wherever_that_pane_is_now(self):
        t = FakeListPanes(self.ROWS)
        self.assertEqual(t.target_for_id("%276"), "main:1.4")
        # %7 closed: every pane after it slides up one.
        t.rows = [("main:1.1", "%274"), ("main:1.2", "%275"),
                  ("main:1.3", "%276"), ("main:1.4", "%277")]
        t._ts = 0.0
        self.assertEqual(t.target_for_id("%276"), "main:1.3")
        self.assertEqual(t.target_for_id("%999"), "")

    def test_a_stale_recorded_target_is_corrected_by_the_id(self):
        self.record("d007fded", session="d007fded-…", tmux="main:1.4",
                    pane_id="%276")
        t = FakeListPanes([("main:1.1", "%274"), ("main:1.2", "%275"),
                           ("main:1.3", "%276")])
        self.assertEqual(registry_target("d007fded", t, self.state),
                         "main:1.3")

    def test_a_record_without_an_id_falls_back_to_its_target(self):
        # Records written before ids were kept. Best effort, and only while
        # that target still exists at all.
        self.record("aaaaaaaa", tmux="main:1.4")
        t = FakeListPanes(self.ROWS)
        self.assertEqual(registry_target("aaaaaaaa", t, self.state),
                         "main:1.4")
        t.rows = [("main:1.1", "%274")]
        t._ts = 0.0
        self.assertEqual(registry_target("aaaaaaaa", t, self.state), "")

    def test_a_dead_pane_resolves_to_nothing(self):
        self.record("bbbbbbbb", tmux="main:1.4", pane_id="%999")
        self.assertEqual(
            registry_target("bbbbbbbb", FakeListPanes(self.ROWS), self.state),
            "")

    def test_missing_registry_is_quiet(self):
        self.assertEqual(
            registry_target("nothere", FakeListPanes(self.ROWS), self.state),
            "")

    def test_the_ledger_travels_with_the_pane_id(self):
        (self.state / "title-renames-1.json").write_text(json.dumps(
            [{"pane": "%276", "old_title": "", "old_source": "",
              "new_title": "Storage expansion"}]))
        titles_mod._ledger["sig"] = None
        env = dict(os.environ)
        os.environ["TALLYDECK_STATE"] = str(self.state)
        try:
            t = FakeListPanes([("main:1.4", "%276")])
            info = t.info("main:1.4")
            self.assertIn("Storage expansion", info.authored)
        finally:
            os.environ.clear()
            os.environ.update(env)


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
