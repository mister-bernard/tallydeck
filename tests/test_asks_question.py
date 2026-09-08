"""Does a finished turn get painted amber?

G, 2026-09-08: "it's putting that in orange when it says 'done for this turn'. It
should go green instead." The session in question ended with

    "...stage the TWAP config for G's sign-off, since starting the sell is a
     value-movement step that stays human-attended."

which is a handoff note the assistant wrote to itself. `sign-off` matched, the key
went amber, and pressing it opened a resume brief for a session that wanted nothing.

An amber key means "a human must act now". Every false one costs G a press and
teaches him the colour means less than it does, so these cases are worth pinning.

Plain unittest on purpose: the rest of the suite needs pytest, which is not
installed on the box this runs on, so those tests cannot be executed here at all.
"""
import unittest

from tallydeck.sources.claude_sessions import asks_question


class AsksQuestion(unittest.TestCase):
    def assertAsk(self, text, expected, why):
        self.assertEqual(asks_question(text), expected, f"{why}: {text[:70]!r}")

    # ── genuine asks: these MUST stay amber ────────────────────────────────
    def test_direct_request_for_signoff(self):
        self.assertAsk("I need your sign-off before I push this to main.", True,
                       "second person, addressed to the reader")

    def test_question_mark_in_final_paragraph(self):
        self.assertAsk("Should I proceed with the anode design?", True,
                       "a question mark in the tail is an ask")

    def test_blocked_on_you(self):
        self.assertAsk("This is blocked on you until you approve the spend.", True,
                       "explicitly blocked on the reader")

    def test_awaiting_your_approval(self):
        self.assertAsk("The deploy is awaiting your approval.", True, "awaiting your")

    def test_decision_needed(self):
        self.assertAsk("Decision needed: pick option 1 or 2.", True,
                       "unambiguous decision request")

    # ── reports and narration: these MUST stay green ──────────────────────
    def test_third_person_handoff_note(self):
        """The exact text that caused the report."""
        self.assertAsk(
            "Done for this turn. Next inbound to expect: G's tx hash tonight. "
            "When it arrives, verify the credit and stage the TWAP config for "
            "G's sign-off, since starting the sell stays human-attended.",
            False, "narration ABOUT G, not a request TO him")

    def test_reporting_that_it_already_asked(self):
        self.assertAsk("I asked G for the tx hash and will confirm when it lands.",
                       False, "past-tense report of an ask made elsewhere")

    def test_negated_decision(self):
        self.assertAsk("Summary: 12 tests pass, nothing needs a decision.", False,
                       "the ask phrase appears inside its own negation")

    def test_negated_approval(self):
        self.assertAsk("No approval needed; I already had it.", False, "negated")

    def test_third_person_pronouns(self):
        self.assertAsk("He said his approval would come later; I recorded it.",
                       False, "third-person pronouns are narration")


if __name__ == "__main__":
    unittest.main(verbosity=2)
