"""Conservative, shared extraction: quote actual asks, never invent a decision."""
import re

# Optional offers after a report do not block work. Explicit approvals win first.
COURTESY = re.compile(
    r"^(?:anything\b.*\byou want me to|(?:is there )?anything else\b|"
    r"(?:let me know|feel free)\b|(?:do you )?want me to (?:also|just)\b)", re.I)
DIRECT = re.compile(
    r"\b(?:should i|shall i|please (?:confirm|approve|advise|choose|pick)|"
    r"your call(?=\s*[:?—-])|awaiting your|waiting (?:on|for) your\b|"
    r"need(?:s)? your (?:decision|approval|input|answer|go|ok|sign)|"
    r"blocked on you|go/no[- ]go|(?:decision|approval|sign[- ]?off) needed|"
    r"need(?:s)? (?:a|your) (?:decision|approval))\b", re.I)
NEGATED = re.compile(
    r"\b(?:no|not|nothing|none|never|without)\b[^.!?]{0,60}?"
    r"\b(?:sign[- ]?off|approval|decision|input|answer|confirm|blocked)\b", re.I)


def decision_text(text: str) -> str:
    """Return complete source paragraphs containing an actual ask.

    Direct requests may precede supporting material. Bare questions count only
    in the final paragraph, excluding headings, quotes, code and closing offers.
    The source text (including Markdown/options) is returned verbatim.
    """
    text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.S)
    # Keep offsets/paragraphs stable while hiding code from classification.
    clean = re.sub(r'```.*?```|~~~.*?~~~', '', text, flags=re.S)
    paras = [p.strip() for p in re.split(r'\n\s*\n', clean) if p.strip()]
    found = []
    for i, para in enumerate(paras):
        lines = [ln for ln in para.splitlines() if not ln.lstrip().startswith(('>', '#', '|'))]
        probe = re.sub(r'`[^`\n]*`', '', '\n'.join(lines))
        probe = re.sub(r'[*_]', '', probe).strip()
        if not probe:
            continue
        sentences = re.split(r'(?<=[.!?])\s+', probe)
        direct = any(DIRECT.search(s) and not NEGATED.search(s) for s in sentences)
        # Explicit second-person sign-off; third-person handoff notes are reports.
        direct = direct or bool(re.search(r'\byour sign[- ]?off\b', probe, re.I)
                                and not NEGATED.search(probe))
        if direct:
            found.append(para)
        elif i == len(paras) - 1 and any('?' in s and not COURTESY.search(s.strip()) for s in sentences):
            # Question + answer in the same closing paragraph is explanatory.
            if not re.search(r'\?\s+(?:yes|no|because|it |they |the answer)\b', probe, re.I):
                found.append(para)
    return '\n\n'.join(found)


def decision_support(text: str, ask: str) -> str:
    """Verbatim explicit recommendation and adjacent choice lists, if supplied."""
    if not ask:
        return ""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    found = []
    for i, p in enumerate(parts):
        if p in ask:
            if i+1 < len(parts) and re.match(r"(?:[-*] |\d+[.)] )", parts[i+1]):
                found.append(parts[i+1])
        elif re.search(r"\b(?:I recommend|my recommendation|recommended option)\b", p, re.I):
            found.append(p)
    return "\n\n".join(dict.fromkeys(found))
