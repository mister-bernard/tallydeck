"""Operator contracts: shown choices, literal delivery, safe parking and legibility."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from tallydeck.decisions import next_steps
from tallydeck import brief, cli, park, popup
from tallydeck.render.keycard import _wrap_n, draw_key
from tallydeck.render import theme
from tallydeck.signal import Signal
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
UID = '01234567-89ab-4cde-8fab-0123456789ab'


@pytest.mark.parametrize('text,explicit,expected', [
    ('NEXT STEPS\n1. Review the shortlist\n2. Refine the search', False,
     {'1': 'Review the shortlist', '2': 'Refine the search'}),
    ('1) Review\n2) Refine', True, {'1': 'Review', '2': 'Refine'}),
    ('## Next steps\n\n1. **Review**\n   Keep the existing filters.\n2. Refine', False,
     {'1': '**Review**\nKeep the existing filters.', '2': 'Refine'}),
    ('Completed:\n1. Fixed the bug\n2. Ran tests', False, {}),
    ('```\nNEXT STEPS\n1. Ignore this\n```', False, {}),
    ('NEXT STEPS\n1. A\n1. B', False, {}),
    ('NEXT STEPS\n1. A\n3. C', False, {}),
    ('NEXT STEPS\n1. A\n\nEvidence:\n1. historical step', False, {}),
    ('NEXT STEPS\n1. A\n\nOPTIONS\n1. B', False, {}),
])
def test_choice_extraction(text, explicit, expected):
    assert next_steps(text, explicit=explicit) == expected


def test_choice_payload_is_exactly_what_the_popup_showed(tmp_path, monkeypatch):
    logdir = tmp_path / 'project'; logdir.mkdir()
    message = 'The shortlist is ready.\n\n## Next steps\n1. Review\n2. Refine\n   Keep the budget unchanged.'
    (logdir / (UID + '.jsonl')).write_text(json.dumps({'type': 'assistant', 'message': {
        'content': [{'type': 'text', 'text': message}]}}) + '\n')
    def choose(render, actions, **kwargs):
        c = popup.Console(file=io.StringIO(), width=80)
        c.print(render(80))
        assert 'Keep the budget unchanged' in c.file.getvalue()
        assert '1/2 send choice' in actions and '1-9' not in actions
        assert '3' not in kwargs['valid_keys']
        return '2'
    monkeypatch.setattr(popup, 'choose', choose)
    args = argparse.Namespace(session=UID, project='', label='Hunt', state='success', ask='',
                              interactive=True, signal='cc/' + UID, actions='1-9 next step · i type',
                              taken='', action_file=str(tmp_path / 'action'))
    cli.cmd_brief({'sources': [{'kind':'claude-sessions', 'root':str(tmp_path)}]}, args)
    assert (tmp_path / 'action').read_text() == '2Refine\nKeep the budget unchanged.'


def test_tool_ask_never_becomes_a_paste_shortcut(tmp_path):
    d = tmp_path / 'project'; d.mkdir()
    (d / (UID + '.jsonl')).write_text(json.dumps({'type':'assistant','message':{'content':[
        {'type':'tool_use','name':'AskUserQuestion','input':{'questions':[{'question':'Choose?',
          'options':[{'label':'A'}, {'label':'B'}]}]}}]}}) + '\n')
    doc = brief.document(UID, '', roots=[tmp_path], ask='1. A\n2. B')
    assert doc['steps'] == {}


def test_numbered_route_pastes_snapshot_and_reports_failure(tmp_path):
    fake = tmp_path / 'bin'; fake.mkdir()
    pkg = tmp_path / 'pkg/tallydeck'; pkg.mkdir(parents=True)
    (pkg / '__init__.py').write_text('')
    (pkg / 'cli.py').write_text("import sys\nfrom pathlib import Path\na=sys.argv\nPath(a[a.index('--action-file')+1]).write_text('2Review $(unchanged)\\nKeep filters')\n")
    tmux = fake / 'tmux'
    tmux.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_CALLS"
case "$*" in
 *pane_id*) echo %1;;
 *client_session*) echo here;;
 *load-buffer*) cat > "$TEST_TEXT";;
 *paste-buffer*) exit "${TEST_FAIL:-0}";;
esac
'''); tmux.chmod(0o755)
    env = dict(os.environ, PATH=str(fake)+':'+os.environ['PATH'], TALLYDECK_HOME=str(pkg.parent),
               TALLYDECK_STATE=str(tmp_path / 'state'), TEST_CALLS=str(tmp_path/'calls'), TEST_TEXT=str(tmp_path/'text'))
    args = [str(ROOT/'contrib/tally-route'),'tty','here','hunt:1.1',UID,str(tmp_path),'Hunt','success','X','gk/'+UID,
            '1. Old menu\n2. Wrong choice']
    result = subprocess.run(args, env=env, cwd=tmp_path, text=True, capture_output=True, timeout=5)
    assert 'sent.' in result.stdout
    assert (tmp_path/'text').read_text() == 'Do this next: Review $(unchanged)\nKeep filters'
    assert 'paste-buffer -p -d' in (tmp_path/'calls').read_text()
    (tmp_path/'state/route-taken'/('gk_'+UID)).unlink()
    (tmp_path/'calls').write_text('')
    result = subprocess.run(args, env=env | {'TEST_FAIL':'1'}, cwd=tmp_path, text=True, capture_output=True, timeout=5)
    assert 'Could not send' in result.stdout and '  sent.' not in result.stdout
    assert 'send-keys' not in (tmp_path/'calls').read_text()


@pytest.mark.parametrize('text', ['averylongunbrokentoken'*10, 'Which market should we search next? '*20])
def test_question_lines_fit_and_truncation_is_visible(text):
    draw = ImageDraw.Draw(Image.new('RGB', (192,192)))
    font = theme.font('regular', 26)
    lines = _wrap_n(draw, text, font, 140, 12)
    assert all(draw.textlength(line, font=font) <= 140 for line in lines)
    assert len(lines) == 12 and lines[-1].endswith('…')


@pytest.mark.parametrize('account,harness', [('A','claude'),('O','codex'),('O2','codex'),('X','grok')])
@pytest.mark.parametrize('px', [72,96])
def test_questions_do_not_overpaint_footer(account, harness, px):
    meta = {'account':account,'harness':harness}
    # The footer must render identically with or without a long question.
    sig = Signal(id='review',label='Long project name',state='attention',progress=.6,meta=meta)
    plain = draw_key(sig, px)
    sig.sublabel = 'averylongword'*10 + ' Which direction should we take? '*5
    face = draw_key(sig, px)
    footer = (0, int(px * (.75 if harness == 'grok' else .84)), px, px)
    # Page dots intentionally live bottom-left; the badge area cannot change.
    badge = (int(px*.67), footer[1], px, px)
    assert plain.crop(badge).tobytes() == face.crop(badge).tobytes()


def test_park_refusal_does_not_hide_or_stop(tmp_path, monkeypatch):
    monkeypatch.setenv('TALLYDECK_STATE', str(tmp_path))
    def refuse(*args): raise ValueError('Still working')
    monkeypatch.setattr(park, '_candidate', refuse)
    monkeypatch.setattr(park.os, 'pidfd_open', lambda *_: pytest.fail('must not stop'))
    result = park.park(slug='hunt', uuid=UID, harness='grok', target='hunt:1.1')
    assert result['status'] == 'refused'
    assert not park.is_parked(uuid=UID, slug='hunt')


@pytest.mark.parametrize('changed', [False,True])
def test_park_records_before_stop_and_rechecks_ownership(tmp_path, monkeypatch, changed):
    monkeypatch.setenv('TALLYDECK_STATE', str(tmp_path))
    transcript = tmp_path/'history'; transcript.write_text('completed')
    base = dict(pids=[123], cwd='/work', account='X', transcript=str(transcript))
    calls = []
    def candidate(*args):
        calls.append('check')
        return base | {'pids':[456] if changed and len(calls)>1 else [123]}
    monkeypatch.setattr(park, '_candidate', candidate)
    monkeypatch.setattr(park.os, 'pidfd_open', lambda p: 90)
    monkeypatch.setattr(park.os, 'close', lambda fd: None)
    monkeypatch.setattr(park.select, 'select', lambda *a: ([90],[],[]))
    monkeypatch.setattr(park, '_tmux', lambda *a: pytest.fail('never kill the tmux session'))
    def stop(fd, sig):
        assert json.loads((tmp_path/'reaped'/(UID+'.resume.json')).read_text())['resume']
        assert not park.is_parked(uuid=UID)
        calls.append('stop')
    monkeypatch.setattr(park.signal, 'pidfd_send_signal', stop)
    result = park.park(slug='hunt', uuid=UID, harness='grok', target='hunt:1.1')
    assert result['status'] == ('refused' if changed else 'parked')
    assert ('stop' in calls) is not changed
    assert park.is_parked(uuid=UID) is not changed
    if not changed:
        transcript.write_text('resumed with a new prompt')
        assert not park.is_parked(uuid=UID)
        assert not park.is_parked(slug='hunt')


def test_resume_command_quotes_shell_metacharacters(monkeypatch):
    import shlex
    monkeypatch.setenv('GROK_BIN', '/bin/grok odd')
    result = park.resume_cmd('grok','X',"/work/it's $(private)",UID)
    args = shlex.split(result)
    assert args[1] == "/work/it's $(private)" and '/bin/grok odd' in args


@pytest.mark.parametrize('state,uuid,panes,children,error', [
    ('working', UID, 'hunt:1.1', [], 'still active'),
    ('idle', '00000000-0000-0000-0000-000000000000', 'hunt:1.1', [], 'identities'),
    ('idle', UID, 'hunt:1.1\nhunt:2.1', [], 'shared'),
    ('idle', UID, 'hunt:1.1', [999], 'child process'),
    ('idle', UID, 'hunt:1.1', [], ''),
])
def test_park_candidate_proves_identity_activity_and_exclusive_pane(
        tmp_path, monkeypatch, state, uuid, panes, children, error):
    # Execute the real candidate validation with an inert reaper module.
    helper = tmp_path/'reaper'
    rec = dict(slug='hunt',uuid=uuid,pids=[123],transcript='/history',state=state,idle_h=1,harness='claude')
    helper.write_text('PROTECTED=set()\n'
                      'def telegraph_sessions(): return set()\n'
                      f'def collect(): return [{rec!r}]\n'
                      f'def fd_uuid(pid): return {uuid!r}\n'
                      f'def descendants(pid): return {children!r}\n')
    from tallydeck import paths
    monkeypatch.setattr(paths, 'contrib_bin', lambda _: str(helper))
    monkeypatch.setattr(park, '_tmux', lambda *a: panes)
    monkeypatch.setattr(park, '_claude_complete', lambda _: True)
    if error:
        with pytest.raises(ValueError, match=error):
            park._candidate('hunt', UID, 'claude', 'hunt:1.1')
    else:
        assert park._candidate('hunt',UID,'claude','hunt:1.1')['pids'] == [123]


def test_parking_requires_a_completed_turn_not_a_quiet_log(tmp_path):
    fp = tmp_path/'chat.jsonl'
    done = {'type':'system','subtype':'turn_duration'}
    fp.write_text(json.dumps(done)+'\n')
    assert park._claude_complete(fp)
    with fp.open('a') as f:
        f.write(json.dumps({'type':'user','message':{'content':'continue'}})+'\n')
    assert not park._claude_complete(fp)
    fp.write_text(json.dumps({'type':'assistant','message':{'content':[]}})+'\n')
    assert not park._claude_complete(fp)
