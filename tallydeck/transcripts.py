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


def pending_ask(path: Path, kind: str):
    """Only the current blocking tool, never a historical unanswered-looking tool."""
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
            if rec.get('type') == 'event_msg' and p.get('type') in ('task_complete', 'task_started', 'user_message'): return ''
            if rec.get('type') == 'response_item' and p.get('type') == 'function_call_output':
                return ''
            if rec.get('type') == 'response_item' and p.get('type') == 'function_call':
                if 'request_user_input' in p.get('name', ''):
                    raw = p.get('arguments') or '{}'
                    try: args = json.loads(raw)
                    except ValueError: return str(raw)
                    return '**Answer in the session**\n\n' + '\n\n'.join(
                        str(q.get('question') or q.get('title') or '') + '\n' + '\n'.join(
                            '- '+(str(o.get('label', '')) + ': '+str(o.get('description','')) if isinstance(o,dict) else str(o))
                            for o in q.get('options', [])) for q in args.get('questions', []))
                return ''
    return ''
