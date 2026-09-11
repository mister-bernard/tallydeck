"""Read complete recent JSONL records, including records larger than tail windows."""
import json
from pathlib import Path


def records_backwards(path: Path):
    try:
        with path.open('rb') as f:
            f.seek(0, 2); pos = f.tell(); pending = b''
            while pos:
                size = min(pos, 65536); pos -= size; f.seek(pos)
                parts = (f.read(size) + pending).split(b'\n')
                pending = parts[0]
                for line in reversed(parts[1:]):
                    try:
                        rec = json.loads(line)
                        if isinstance(rec, dict): yield rec
                    except (ValueError, UnicodeError): pass
            if pending:
                try:
                    rec = json.loads(pending)
                    if isinstance(rec, dict): yield rec
                except (ValueError, UnicodeError): pass
    except OSError:
        return


def recent_texts(path: Path, kind: str, want=2):
    texts = []
    for rec in records_backwards(path):
        text = ''
        if kind == 'claude' and rec.get('type') == 'assistant':
            blocks = (rec.get('message') or {}).get('content', [])
            if isinstance(blocks, list):
                text = '\n\n'.join(str(b.get('text') or '') for b in blocks
                                   if isinstance(b, dict) and b.get('type') == 'text')
        elif kind == 'codex':
            p = rec.get('payload') or {}
            if rec.get('type') == 'event_msg' and p.get('type') == 'task_complete':
                text = p.get('last_agent_message') or ''
            elif rec.get('type') == 'response_item' and p.get('role') == 'assistant':
                text = '\n\n'.join(str(b.get('text') or '') for b in p.get('content', []) if isinstance(b, dict))
        elif kind == 'grok':
            if rec.get('type') == 'assistant':
                c = rec.get('content')
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    text = '\n\n'.join(
                        str(b.get('text') or '') for b in c
                        if isinstance(b, dict) and b.get('type') == 'text')
        text = text.strip()
        if text and (not texts or texts[-1] != text): texts.append(text)
        if len(texts) >= want: break
    return texts


def is_codex_user_input(name: str) -> bool:
    return str(name or "").split(".")[-1] in (
        "request_user_input", "request_user_input_async")


def codex_ask_answered(output) -> bool:
    """True when the user actually answered, not when the TUI merely queued it.

    Live Codex writes `{"accepted":true}` the instant an async ask is shown,
    then keeps running tools. That is not an answer.
    """
    if output is None:
        return False
    raw = output if isinstance(output, str) else json.dumps(output)
    s = raw.strip()
    if not s or s in ("{}", "null"):
        return False
    try:
        d = json.loads(s)
    except ValueError:
        return True
    if not isinstance(d, dict):
        return True
    if set(d.keys()) <= {"accepted"} and d.get("accepted") is True:
        return False
    return True


def format_codex_ask(arguments) -> str:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments or {})
    try:
        args = json.loads(raw) if isinstance(raw, str) else (arguments or {})
    except ValueError:
        return str(raw)
    if not isinstance(args, dict):
        return str(raw)
    parts = ["**Answer in the session**"]
    for q in args.get("questions") or []:
        if not isinstance(q, dict):
            parts.append(str(q))
            continue
        parts.append(str(q.get("question") or q.get("title") or ""))
        for o in q.get("options") or []:
            if isinstance(o, dict):
                parts.append(f"- {o.get('label', '')}: {o.get('description', '')}".rstrip(": "))
            else:
                parts.append(f"- {o}")
    return "\n\n".join(p for p in parts if p and p != "- ")


def pending_ask(path: Path, kind: str):
    """Only the current blocking tool, never a historical unanswered-looking tool."""
    outputs: dict = {}
    for rec in records_backwards(path):
        if kind == 'claude':
            if rec.get('type') == 'user': return ''
            if rec.get('type') != 'assistant': continue
            content = (rec.get('message') or {}).get('content') or []
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') in ('AskUserQuestion', 'ExitPlanMode'):
                    args = b.get('input') or {}
                    parts = [f"**{b['name']} — answer in the session**"]
                    for q in args.get('questions') or []:
                        parts.append(str(q.get('question') or ''))
                        for o in q.get('options') or []:
                            parts.append(f"- {o.get('label', '')}: {o.get('description', '')}")
                    if args.get('plan'): parts.append(str(args['plan']))
                    if len(parts) == 1: parts.append(json.dumps(args, ensure_ascii=False, indent=2))
                    return '\n\n'.join(parts)
            return ''
        else:
            p = rec.get('payload') or {}
            if rec.get('type') == 'event_msg' and p.get('type') == 'turn_aborted':
                return ''
            if rec.get('type') == 'response_item' and p.get('type') == 'message' \
                    and p.get('role') == 'user':
                return ''
            if rec.get('type') == 'response_item' and p.get('type') == 'function_call_output':
                outputs[str(p.get('call_id') or '')] = p.get('output')
                continue
            if rec.get('type') == 'response_item' and p.get('type') == 'function_call':
                if is_codex_user_input(p.get('name', '')):
                    cid = str(p.get('call_id') or p.get('id') or '')
                    if cid in outputs and codex_ask_answered(outputs[cid]):
                        return ''
                    return format_codex_ask(p.get('arguments') or '{}')
                continue
    return ''
