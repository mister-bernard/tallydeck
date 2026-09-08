"""The signals directory is a drop box, so treat its contents as hostile.

Anything able to write a file into ~/.tallydeck/signals/ gets a key on the deck.
Two fields must therefore be decided by the filesystem and never by the file:

  action  a file-supplied {"type":"cmd","argv":[...]} would be executed by the hub
          on the next press — arbitrary command execution from a dropped file.
  id      setdefault() kept an id from inside the file, so
          {"id":"sig/../../../tmp/x"} walked out of the answers directory.

Both were found by Opus during openclaw-a5's audit of the Signal-answer path and are
pinned here because neither is visible in normal use: the deck looks completely
healthy either way.

Plain unittest: the rest of the suite needs pytest, which is not installed on the
host this runs on.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tallydeck.sources.watchdir import WatchDirSource


class DropBoxIsHostile(unittest.TestCase):
    def poll(self, files: dict) -> list:
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        for name, body in files.items():
            (d / name).write_text(json.dumps(body))
        return WatchDirSource(path=str(d)).poll()

    def test_file_supplied_action_is_ignored(self):
        sigs = self.poll({"evil.json": {
            "label": "evil", "state": "attention", "detail": "x",
            "action": {"type": "cmd", "argv": ["/bin/sh", "-c", "touch /tmp/PWNED"]}}})
        argv = (sigs[0].action or {}).get("argv", [])
        self.assertNotIn("/bin/sh", argv,
                         "a dropped file must not be able to define what a press runs")

    def test_id_comes_from_the_filename_not_the_file(self):
        sigs = self.poll({"safe.json": {
            "id": "sig/../../../tmp/escaped", "label": "e", "state": "attention",
            "detail": "y"}})
        self.assertEqual(sigs[0].id, "sig/safe")
        self.assertNotIn("..", sigs[0].id)

    def test_a_normal_question_still_gets_its_answer_action(self):
        """The hardening must not disarm the feature it protects."""
        sigs = self.poll({"good.json": {
            "label": "Good", "state": "attention",
            "sublabel": "a question", "detail": "a question"}})
        argv = (sigs[0].action or {}).get("argv", [])
        self.assertTrue(argv, "a question should still be answerable from the deck")
        self.assertTrue(argv[0].endswith("tally-popup-decide"))
        self.assertEqual(argv[1], "good", "the stem, and only the stem")

    def test_a_live_session_ask_gets_no_popup(self):
        """ask-<sid> belongs to a pane; a popup cannot answer a blocked session."""
        sigs = self.poll({"ask-deadbeef.json": {
            "label": "sess", "state": "blocked",
            "sublabel": "Claude is waiting for your input",
            "detail": "Claude is waiting for your input"}})
        self.assertTrue(sigs[0].action and sigs[0].action['argv'][0].endswith('tally-popup-route'), 'a session ask opens the ROUTER (to its pane), never the decide popup')

    def tearDown(self):
        if getattr(self, "tmp", None):
            self.tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
