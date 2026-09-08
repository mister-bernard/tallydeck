"""An active Codex thread must survive the hub merge alongside its siblings."""
import json
import time
from pathlib import Path

from tallydeck.hub import Hub
from tallydeck.signal import SUCCESS, WORKING
from tallydeck.sources.codex_sessions import CodexSessionsSource


def test_concurrent_threads_keep_distinct_keys_and_press_targets(tmp_path, monkeypatch):
    # Shared timestamp prefix AND shared suffix: neither end is an identity.
    ids = [
        '01a08020-6011-76d0-8aee-b1add4254771',
        '01a08020-6010-7a22-8fc8-01b3d4254771',
        '01a08020-6023-7682-8807-0501d4254771',
    ]
    files = []
    for i, uid in enumerate(ids):
        fp = tmp_path / f'rollout-{uid}.jsonl'
        records = [
            {'type': 'session_meta', 'payload': {'session_id': uid, 'cwd': str(tmp_path), 'originator': 'codex-tui'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started' if i == 0 else 'task_complete', 'last_agent_message': 'Done.'}},
        ]
        fp.write_text('\n'.join(json.dumps(r) for r in records) + '\n')
        files.append(fp)
    source = CodexSessionsSource(root=str(tmp_path), codex_home=str(tmp_path), dwell=0, sync_titles=False)
    panes = {uid: f'codex:1.{i+1}' for i, uid in enumerate(ids)}
    monkeypatch.setattr(source, '_recent_rollouts', lambda now: files)
    monkeypatch.setattr(source, '_codex_panes', lambda *args: panes)
    monkeypatch.setattr(source.state, 'title', lambda uid: '')
    monkeypatch.setattr(source.tmux, 'info', lambda pane: None)
    hub = Hub([source])
    signals = hub.poll()
    assert len(signals) == len(ids)
    by_thread = {s.meta['session']: s for s in signals}
    assert by_thread[ids[0]].state == WORKING
    assert all(by_thread[uid].state == SUCCESS for uid in ids[1:])
    for uid in ids:
        sig = by_thread[uid]
        assert sig.id == f'cx/{uid}'
        assert sig.action['argv'][1] == panes[uid]
        assert sig.action['argv'][-1] == sig.id
        assert hub._table[sig.id] is sig
