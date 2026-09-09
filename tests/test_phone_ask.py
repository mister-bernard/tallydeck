"""A pressed key with no terminal behind it: the phone lane.

2026-09-08 05:19Z. c64 raised ask-9c89816a, the key went red, G pressed it
from a device with no terminal on the box. `tally-popup-route` had nowhere to
put the popup, logged "no attached client", exited 1 — and that was the whole
response: no popup, nothing in chat, route-taken empty. Another session read
the question out of the pane and relayed it by hand.

Pinned here, because the split these tests protect is easy to "simplify" away:

  * a press with no attached client relays instead of failing
  * the question comes from the PANE — the signal file only ever says
    "Claude is waiting for your input"
  * the answer goes back INTO that pane. A session parked at its own prompt
    is not unblocked by anything we record; recording one and reporting
    success would leave it blocked and G believing he had answered it
  * the pane is addressed by pane ID, so a pane closed elsewhere in that
    window cannot land the answer in a different agent's prompt
  * a second press of the same unchanged prompt is not a second question
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    """contrib/ scripts are dash-named, so import them by path."""
    loader = importlib.machinery.SourceFileLoader(
        name.replace("-", "_"), str(ROOT / "contrib" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


CAPTURE = """\
╭──────────────────────────────────────────────────────╮
│ Bash command                                         │
│                                                      │
│   rm -rf /tmp/build                                  │
│   Clear the build directory                          │
│                                                      │
│ Do you want to proceed?                              │
│ ❯ 1. Yes                                             │
│   2. Yes, and don't ask again for rm commands        │
│   3. No, and tell Claude what to do differently      │
│                                                      │
╰──────────────────────────────────────────────────────╯
  ? for shortcuts
"""


class ReadingTheAsk(unittest.TestCase):
    """The prompt only exists as text on a screen."""

    def setUp(self):
        self.ap = load("tally-ask-phone")
        self.ap._tmux = lambda *a, **k: CAPTURE if a[:1] == ("capture-pane",) else ""

    def test_options_come_off_the_screen(self):
        q, opts, _ = self.ap.read_prompt("c64:1.1")
        self.assertEqual(opts, ["Yes",
                                "Yes, and don't ask again for rm commands",
                                "No, and tell Claude what to do differently"])
        self.assertIn("Do you want to proceed?", q)
        self.assertIn("rm -rf /tmp/build", q)
        self.assertNotIn("╭", q)
        self.assertNotIn("│", q)
        self.assertNotIn("for shortcuts", q, "chrome is not the question")

    def test_a_prompt_with_no_options_still_relays_its_words(self):
        self.ap._tmux = lambda *a, **k: (
            "> Which region should I deploy to?\n" if a[:1] == ("capture-pane",)
            else "")
        q, opts, _ = self.ap.read_prompt("c64:1.1")
        self.assertEqual(opts, [])
        self.assertIn("Which region", q)

    def test_the_message_carries_the_numbers_he_answers_with(self):
        q, opts, _ = self.ap.read_prompt("c64:1.1")
        msg = self.ap.compose("c64", "%51", q, opts)
        self.assertIn("1 · Yes", msg)
        self.assertIn("2 · Yes, and don't ask again for rm commands", msg)
        self.assertIn("c64", msg)

    def test_one_lane_per_session_whichever_key_was_pressed(self):
        uuid = "9c89816a-1111-2222-3333-444455556666"
        self.assertEqual(self.ap.lane_id("sig/ask-9c89816a", uuid),
                         "ask-9c89816a")
        self.assertEqual(self.ap.lane_id("cc/B-9c89816a", uuid),
                         "ask-9c89816a")
        self.assertEqual(self.ap.lane_id("cc/B-9c89816a", ""), "B-9c89816a")


class RelayingIt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        os.environ["TALLYDECK_STATE"] = str(self.state)
        self.ap = load("tally-ask-phone")
        self.sent: list[tuple] = []
        self.ap._tmux = lambda *a, **k: (
            CAPTURE if a[:1] == ("capture-pane",) else "c64:1.1|%51\n")
        self.ap.send = lambda text, label, target, pid, dry: (
            self.sent.append((text, label, target, pid)) or True)

    def tearDown(self):
        os.environ.pop("TALLYDECK_STATE", None)
        self.tmp.cleanup()

    def relay(self):
        return self.ap.main(["sig/ask-9c89816a", "c64:1.1",
                             "9c89816a-1111-2222-3333-444455556666", "c64"])

    def test_it_writes_the_lane_an_answer_can_come_back_through(self):
        self.assertEqual(self.relay(), 0)
        rec = json.loads((self.state / "pending" / "ask-9c89816a.json").read_text())
        self.assertEqual(rec["kind"], "session-ask")
        self.assertEqual(rec["pane_id"], "%51")
        self.assertEqual(rec["pane"], "c64:1.1")
        self.assertEqual(len(rec["options"]), 3)
        self.assertTrue(rec["sent_at"])

    def test_a_second_press_of_the_same_prompt_says_nothing_again(self):
        self.relay()
        self.assertEqual(self.relay(), 0)
        self.assertEqual(len(self.sent), 1, "one question, one message")

    def test_a_changed_prompt_is_a_new_question(self):
        self.relay()
        self.ap._tmux = lambda *a, **k: (
            "Deploy to prod?\n> 1. Yes\n  2. No\n"
            if a[:1] == ("capture-pane",) else "c64:1.1|%51\n")
        self.assertEqual(self.relay(), 0)
        self.assertEqual(len(self.sent), 2)

    def test_a_dead_pane_relays_nothing(self):
        self.ap._tmux = lambda *a, **k: ""
        self.assertEqual(self.relay(), 3)
        self.assertEqual(self.sent, [])
        self.assertFalse((self.state / "pending" / "ask-9c89816a.json").exists())

    def test_a_failed_send_is_not_a_question_he_has(self):
        self.ap.send = lambda *a, **k: False
        self.assertEqual(self.relay(), 1)
        rec = json.loads((self.state / "pending" / "ask-9c89816a.json").read_text())
        self.assertIsNone(rec["sent_at"], "nothing may score a reply against it")


class NoClientFallsBackToThePhone(unittest.TestCase):
    """The press path itself: `tally-popup-route` with nobody attached."""

    def test_the_press_reaches_the_relay(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "relay"
            out = Path(tmp) / "argv"
            stub.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > "
                            f"{out}\nexit 0\n")
            stub.chmod(0o755)
            env = {**os.environ, "TALLY_ASK_PHONE": str(stub),
                   "TALLY_TMUX_SOCKET": str(Path(tmp) / "no-such-socket"),
                   "TALLY_POPUP_LOG": str(Path(tmp) / "log"),
                   "TALLYDECK_STATE": tmp}
            r = subprocess.run(
                [str(ROOT / "contrib" / "tally-popup-route"), "c64:1.1",
                 "9c89816a-1111", "/home/openclaw", "c64", "blocked", "B",
                 "sig/ask-9c89816a"],
                env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(out.read_text().split("\n")[:5],
                             ["sig/ask-9c89816a", "c64:1.1", "9c89816a-1111",
                              "c64", "B"])
            self.assertIn("relaying", (Path(tmp) / "log").read_text())


class AnsweringFromChat(unittest.TestCase):
    """A bare "2" in Telegram, and where it must land."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        (self.state / "signals").mkdir()
        (self.state / "pending").mkdir()
        os.environ["TALLYDECK_STATE"] = str(self.state)
        os.environ["TALLY_DECISION_LOG"] = str(self.state / "decisions.log")
        self.ans = load("tally-answer")
        self.pasted: list[tuple] = []
        self.ans._pane_target = lambda q: "c64:1.1"
        self.ans._paste_into_pane = lambda t, x: (
            self.pasted.append((t, x)) or True)
        self.lane("2")

    def tearDown(self):
        for k in ("TALLYDECK_STATE", "TALLY_DECISION_LOG"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def lane(self, _unused=None):
        (self.state / "pending" / "ask-9c89816a.json").write_text(json.dumps({
            "id": "ask-9c89816a", "label": "c64", "kind": "session-ask",
            "options": ["Yes", "Yes, and don't ask again", "No"],
            "pane": "c64:1.1", "pane_id": "%51", "sent_at": time.time(),
            "session": "9c89816a-1111"}))
        (self.state / "signals" / "ask-9c89816a.json").write_text(json.dumps({
            "label": "c64", "state": "blocked", "detail": "needs your input",
            "meta": {"session": "9c89816a-1111"}}))

    def reply(self, text, channel="telegram"):
        return self.ans.resolve({"text": text, "channel": channel,
                                 "userId": "39172309"})

    def test_the_number_is_typed_into_the_waiting_pane(self):
        r = self.reply("2")
        self.assertTrue(r["handled"], r)
        self.assertEqual(self.pasted, [("c64:1.1", "2")],
                         "the pane wants the NUMBER, not the option text")
        self.assertIn("sent to c64", r["reply"])

    def test_it_is_recorded_and_stops_flashing(self):
        self.reply("2")
        rec = json.loads((self.state / "answers" / "ask-9c89816a.json").read_text())
        self.assertEqual(rec["kind"], "session-ask")
        self.assertIn("2 — Yes, and don't ask again", rec["answer"])
        self.assertFalse((self.state / "signals" / "ask-9c89816a.json").exists())
        self.assertFalse((self.state / "pending" / "ask-9c89816a.json").exists())
        self.assertIn("pane c64:1.1",
                      (self.state / "decisions.log").read_text())

    def test_words_reach_the_pane_when_they_name_the_lane(self):
        # Words need the id (or a quote): an unrelated sentence that happens
        # to start with a slug is a message, not an answer.
        r = self.reply("ask-9c89816a: use us-west-2")
        self.assertTrue(r["handled"], r)
        self.assertEqual(self.pasted, [("c64:1.1", "use us-west-2")])

    def test_a_number_with_no_such_option_is_not_an_answer(self):
        self.assertFalse(self.reply("7")["handled"])
        self.assertEqual(self.pasted, [])

    def test_chat_that_is_not_an_answer_is_left_alone(self):
        self.assertFalse(self.reply("check the deploy log when you get a sec")["handled"])
        self.assertEqual(self.pasted, [])

    def test_a_failed_paste_is_not_an_answer(self):
        self.ans._paste_into_pane = lambda t, x: False
        r = self.reply("2")
        self.assertFalse(r["handled"])
        self.assertIn("could not type", r["reply"])
        self.assertFalse((self.state / "answers" / "ask-9c89816a.json").exists(),
                         "an unanswered session must be answerable again")

    def test_a_lane_whose_pane_died_is_dropped_not_answered(self):
        # The pane IS the question. With it gone there is nothing to answer,
        # and a "✓ recorded" would be a lie about an unblocked session.
        self.ans._pane_target = lambda q: ""
        self.assertFalse(self.reply("2")["handled"])
        self.assertEqual(self.pasted, [])
        self.assertFalse((self.state / "pending" / "ask-9c89816a.json").exists())

    def test_the_answer_is_never_swallowed_on_telegram(self):
        # G is replying in a chat the session is also reading; the matcher
        # only observes there (existing rule, kept for the new lane).
        self.assertFalse(self.reply("2").get("swallow"))

    def test_signal_replies_work_the_same_way(self):
        r = self.reply("2", channel="signal")
        self.assertTrue(r["handled"], r)
        self.assertEqual(self.pasted, [("c64:1.1", "2")])

    def test_the_formatted_option_is_reduced_to_the_keystroke(self):
        self.assertEqual(self.ans._typed("2 — Yes, and don't ask again"), "2")
        self.assertEqual(self.ans._typed("use us-west-2"), "use us-west-2")


@unittest.skipIf(not shutil.which("tmux"), "tmux not installed")
class IntoARealPane(unittest.TestCase):
    """The paste, against a real tmux server: buffer, bracketed paste, Enter.

    Mocks are the wrong tool for the one step that has to physically work.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock = str(Path(self.tmp.name) / "sock")
        self.out = Path(self.tmp.name) / "typed"
        subprocess.run(["tmux", "-S", self.sock, "new-session", "-d", "-s",
                        "t", f"cat > {self.out}"], check=True, timeout=10)
        os.environ["TALLY_TMUX_SOCKET"] = self.sock
        self.ans = load("tally-answer")
        # base-index is a matter of local taste; ask tmux where the pane is.
        r = subprocess.run(["tmux", "-S", self.sock, "list-panes", "-a", "-F",
                            "#{session_name}:#{window_index}.#{pane_index} "
                            "#{pane_id}"], capture_output=True, text=True,
                           timeout=10)
        self.target, self.pane_id = r.stdout.split()[:2]

    def tearDown(self):
        subprocess.run(["tmux", "-S", self.sock, "kill-server"],
                       capture_output=True, timeout=10)
        os.environ.pop("TALLY_TMUX_SOCKET", None)
        self.tmp.cleanup()

    def test_the_pane_receives_the_line(self):
        target = self.ans._pane_target({"pane_id": "", "pane": self.target})
        self.assertEqual(target, self.target)
        self.assertTrue(self.ans._paste_into_pane(target, "2"))
        for _ in range(40):
            if self.out.exists() and self.out.read_text().strip():
                break
            time.sleep(0.05)
        self.assertEqual(self.out.read_text().strip(), "2")

    def test_a_pane_id_addresses_the_pane_wherever_it_sits(self):
        self.assertEqual(self.ans._pane_target({"pane_id": self.pane_id}),
                         self.target)
        self.assertEqual(self.ans._pane_target({"pane_id": "%999",
                                                "pane": "gone:9.9"}), "")


if __name__ == "__main__":
    sys.exit(unittest.main())
