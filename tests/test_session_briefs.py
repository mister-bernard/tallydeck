"""Readable decisions, complete supporting text, and routing without model jobs."""
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from tallydeck import brief
from tallydeck.decisions import decision_text
from tallydeck.sources.claude_sessions import classify, _tail_lines
from tallydeck.sources.codex_sessions import classify as codex_classify

ROOT = Path(__file__).resolve().parents[1]
IDS = ['01a08020-6011-76d0-8aee-b1add4254771', '01a08020-6010-7a22-8fc8-01b3d4254771']


def assistant(text):
    return {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}


def log(tmp_path, uid, text, kind='claude'):
    folder = tmp_path / ('project' if kind == 'claude' else '2026/09/08')
    folder.mkdir(parents=True, exist_ok=True)
    fp = folder / (uid + '.jsonl' if kind == 'claude' else 'rollout-test-' + uid + '.jsonl')
    records = [assistant(text)] if kind == 'claude' else [
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type':'output_text','text':text}]}},
        {'type':'event_msg','payload':{'type':'task_complete','last_agent_message':text}}]
    fp.write_text('\n'.join(json.dumps(r) for r in records)+'\n')
    return fp


@pytest.mark.parametrize('kind', ['claude', 'codex'])
def test_screenshot_shape_is_optional_offer_not_blocked_decision(tmp_path, kind):
    text = '**Autopilot right now:**\n\n' + '\n'.join('- **worker** — healthy and running' for _ in range(80))
    text += '\n\nSystemd sweep is green (0 issues, no transitions).\n\n'
    text += 'Anything on that list you want me to just go handle, or is this a "know the state" check?'
    fp = log(tmp_path, IDS[0], text, kind)
    classifier = classify if kind == 'claude' else codex_classify
    assert classifier(_tail_lines(fp))[0] == 'success'
    assert not decision_text(text)
    doc = brief.document(IDS[0], '', roots=[tmp_path], codex_roots=[tmp_path], state='success')
    assert text in [t for _, t in doc['sections']]
    assert not any(h.startswith('THE ASK') for h, _ in doc['sections'])


@pytest.mark.parametrize('kind', ['claude', 'codex'])
def test_giant_record_question_at_end_is_first_and_complete(tmp_path, kind):
    ask = 'Please approve option B before I deploy.\nThe full approval condition must survive.'
    text = '**Status**\n\n'+ ('- Work complete.\n' * 35000) + '\n' + ask
    fp = log(tmp_path, IDS[0], text, kind)
    classifier = classify if kind == 'claude' else codex_classify
    assert classifier(_tail_lines(fp))[0] == 'attention'
    doc = brief.document(IDS[0], '', roots=[tmp_path], codex_roots=[tmp_path], state='attention')
    assert doc['sections'][0][1] == ask
    assert text in [t for _, t in doc['sections']]
    read = brief._last_texts(fp) if kind == 'claude' else brief._last_texts_codex(fp)
    assert read == [text]


@pytest.mark.parametrize('text,expected', [
    ('Please approve the plan.\n\nSupporting evidence follows.', True),
    ('I need your sign-off.\n\nMeanwhile the tests are green.', True),
    ('Please choose A or B.\n\n1. A\n2. B\n\nI recommend B.', True),
    ('No approval needed. All done.', False),
    ('Awaiting your approval to continue.', True),
    ('He recorded his approval yesterday.', False),
    ('Done.\n\nWant me to also tidy the docs?', False),
    ('Done.\n\n> Should I deploy?', False),
    ('## Why this design?\n\nIt is simpler.', False),
    ('Is it healthy? Yes, all checks pass.', False),
    ('Which environment should I deploy to?', True),
    ('```sh\necho "Should I deploy?"\n```', False),
    ('waiting for your next prompt', False),
])
def test_decision_extraction(text, expected):
    assert bool(decision_text(text)) is expected


def test_full_identity_label_and_width_never_reuse_old_render(tmp_path, monkeypatch):
    pytest.importorskip('rich')
    monkeypatch.setattr(brief, 'CACHE_DIR', tmp_path/'cache')
    for uid, text in zip(IDS, ['First session content.', 'Other session content.']):
        fp = log(tmp_path, uid, text, 'codex')
        os.utime(fp, (100,100))
    for uid, label, width, expected, unwanted in [
        (IDS[0], 'First', 40, 'First session content.', 'Other session content.'),
        (IDS[1], 'Other', 160, 'Other session content.', 'First session content.'),
        (IDS[0], 'Renamed', 100, 'First session content.', 'Other session content.')]:
        out = brief.build_cached(uid, '', label, roots=[tmp_path], codex_roots=[tmp_path], width=width)
        assert label in out and expected in out and unwanted not in out
    assert brief._codex_session_file(IDS[0][:8], [tmp_path]) is None


def test_permission_tool_and_explicit_ask_preserved(tmp_path):
    fp = log(tmp_path, IDS[0], 'Old status: everything is finished.')
    tool = {'type':'assistant','message':{'content':[{'type':'tool_use','name':'AskUserQuestion','input':{
        'questions':[{'question':'Which database?', 'options':[{'label':'A','description':'Existing'}, {'label':'B','description':'New'}]}]}}]}}
    with fp.open('a') as f: f.write(json.dumps(tool)+'\n')
    assert classify(_tail_lines(fp)) == ('attention', True)
    doc = brief.document(IDS[0], '', roots=[tmp_path], state='attention')
    assert 'Which database?' in doc['sections'][0][1] and 'B: New' in doc['sections'][0][1]
    ask = 'Permission required\n\n' + 'Full command detail\n'*500 + 'END OF PERMISSION'
    doc = brief.document(IDS[0], '', roots=[tmp_path], state='blocked', ask=ask)
    assert doc['sections'][0][1] == ask
    with fp.open('a') as f: f.write(json.dumps({'type':'user','message':{'content':[]}})+'\n')
    from tallydeck.transcripts import pending_ask
    assert pending_ask(fp, 'claude') == ''


@pytest.mark.parametrize('width,height', [(32,16), (74,30), (160,50)])
def test_markdown_table_code_and_viewport_keep_all_content(width, height):
    pytest.importorskip('rich')
    from tallydeck.popup import Console, THEME, body, Viewport
    raw = '# Plan\n\n| Choice | Details | Final column |\n|---|---|---|\n'
    raw += '| A | ' + 'long detailed explanation '*20 + '| FINALCELL |\n\n'
    raw += '| A | B | C |\n|---|---|---|\n| '+ 'a'*150+'AAA | '+ 'b'*150+'BBB | '+ 'c'*150+'CCC |\n\n'
    raw += '```text\n'+'x'*300+'CODEEND\n```\n\n'+'\n\n'.join('paragraph '+str(i) for i in range(80))+'\n\nENDMARK'
    out = io.StringIO(); c = Console(file=out,width=width,height=height,theme=THEME,color_system=None)
    rendered = body(raw, 'Full supporting text', width)
    c.print(rendered)
    plain = out.getvalue()
    assert all(marker in plain for marker in ('FINALCELL', 'CODEEND', 'ENDMARK', 'AAA', 'BBB', 'CCC'))
    assert all(len(line) <= width for line in plain.splitlines())
    out.seek(0);out.truncate();v=Viewport(c);v.draw(rendered,'Enter open · t tab · s split · space mute')
    first = out.getvalue()
    assert 'ENDMARK' not in first and 'lines' in first
    assert v.scroll('G')
    out.seek(0);out.truncate();v.draw(rendered,'Enter open · t tab · s split · space mute')
    assert 'ENDMARK' in out.getvalue()
    assert v.scroll('g') and v.offset == 0
    assert not v.scroll('t')


def load_helper(name):
    loader=importlib.machinery.SourceFileLoader(name,str(ROOT/'contrib'/name))
    spec=importlib.util.spec_from_loader(name,loader);module=importlib.util.module_from_spec(spec);loader.exec_module(module)
    return module


def test_explicit_decision_shares_layout_and_exclusive_answer(tmp_path):
    pytest.importorskip('rich')
    m=load_helper('tally-decide')
    d={'label':'Decision','state':'attention','meta':{'markdown':'# Choose\n\nLast condition', 'options':['A','B (recommended)']}}
    with m.console.capture() as cap:m.console.print(m.screen(d,d['meta']['options']))
    assert 'Last condition' in cap.get() and 'B (recommended)' in cap.get()
    fp=tmp_path/'answer.json'
    assert m._write_atomic(fp,{'answer':'A'},exclusive=True)
    assert not m._write_atomic(fp,{'answer':'B'},exclusive=True)
    assert json.loads(fp.read_text())['answer']=='A'


@pytest.mark.parametrize('key,expected', [('t','link-window'),('s','join-pane'),('\n','attach'),(' ','')])
def test_session_route_actions_fake_executables(tmp_path,key,expected):
    # Stub ONLY external boundaries; run the actual shell router.
    fake=tmp_path/'bin';fake.mkdir(); package=tmp_path/'pkg'/'tallydeck';package.mkdir(parents=True)
    (package/'__init__.py').write_text('')
    (package/'cli.py').write_text("import os,sys\nfrom pathlib import Path\na=sys.argv\nif '--action-file' in a: Path(a[a.index('--action-file')+1]).write_text(os.environ['TEST_KEY'])\n")
    tmux=fake/'tmux';tmux.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_LOG"
case "$*" in
  *list-sessions*|*list-windows*) exit 0;;
  *has-session*) exit 1;;
  *client_session*) echo here;;
  *window_id*) echo '@1';;
  *pane_id*) echo '%1';;
esac
exit 0
''');tmux.chmod(0o755)
    env=dict(os.environ,PATH=str(fake)+':'+os.environ['PATH'],TALLYDECK_HOME=str(package.parent),
             TALLYDECK_STATE=str(tmp_path/'state'),TEST_KEY=key,TEST_LOG=str(tmp_path/'calls'))
    args=[str(ROOT/'contrib/tally-route'),'tty','here','work:1.1',IDS[0],str(tmp_path),'Brief','attention','O','cx/'+IDS[0],'']
    r=subprocess.run(args,env=env,cwd=tmp_path,text=True,capture_output=True,timeout=8)
    assert r.returncode==0, r.stderr
    if expected: assert expected in (tmp_path/'calls').read_text()
    else:
        assert (tmp_path/'state/acked'/IDS[0]).exists()
        assert not (tmp_path/'state/acked'/IDS[0][:8]).exists()


def test_explicit_route_is_direct_without_intermediate_brief(tmp_path):
    fake=tmp_path/'bin';fake.mkdir(); t=fake/'tmux';t.write_text('#!/bin/sh\nexit 0\n');t.chmod(0o755)
    decide=fake/'decide';decide.write_text('#!/bin/sh\nprintf "%s" "$1" > "$TEST_LOG"\n');decide.chmod(0o755)
    env=dict(os.environ,PATH=str(fake)+':'+os.environ['PATH'],TALLY_DECIDE=str(decide),TEST_LOG=str(tmp_path/'result'))
    r=subprocess.run([str(ROOT/'contrib/tally-route'),'tty','here','','','','Decision','attention','','sig/choice','Full ask'],
                     env=env,capture_output=True,text=True,timeout=5)
    assert r.returncode==0 and (tmp_path/'result').read_text()=='choice' and not r.stdout


def test_codex_links_and_results(tmp_path):
    fp=log(tmp_path,IDS[0],'Result: https://example.org/report', 'codex')
    assert 'https://example.org/report' in load_helper('tally-link').collect(fp)
    assert 'https://example.org/report' in load_helper('tally-results').gather(fp)[2]


def test_real_terminal_scroll_resize_and_query_replies(tmp_path):
    pytest.importorskip('rich')
    import pty, select, fcntl, termios, struct, sys
    master, slave = pty.openpty()
    def size(w,h): fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',h,w,0,0))
    size(74,24)
    runner=tmp_path/'view.py'
    runner.write_text('''from tallydeck.popup import choose, body
from pathlib import Path
import sys
text = 'Please approve B.\\n\\n' + '\\n\\n'.join('Status '+str(i) for i in range(100)) + '\\n\\nENDMARK'
key=choose(lambda width: body(text,'THE ASK',width), 'Enter open · t tab · space mute', timeout=10)
Path(sys.argv[1]).write_text(key)
''')
    result=tmp_path/'key'
    env=dict(os.environ,PYTHONPATH=str(ROOT)+':'+os.environ.get('PYTHONPATH',''),TERM='xterm-256color')
    proc=subprocess.Popen([sys.executable,str(runner),str(result)],stdin=slave,stdout=slave,stderr=slave,env=env)
    def until(needle):
        data=b''; deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if select.select([master],[],[],.1)[0]:
                data+=os.read(master,65536)
                if needle in data:return data
        raise AssertionError(data[-2000:])
    try:
        first=until(b'Please approve B.')
        # DA1 input should never choose an action, nor should scrolling.
        os.write(master,b'\x1b[?1;2c');time.sleep(.1)
        assert proc.poll() is None and not result.exists()
        os.write(master,b'G');until(b'ENDMARK')
        assert not result.exists()
        size(32,16);os.write(master,b'g');until(b'Please approve B.')
        assert not result.exists()
        os.write(master,b't');proc.wait(timeout=3)
        assert result.read_text()=='t' and proc.returncode==0
    finally:
        if proc.poll() is None:proc.kill();proc.wait()
        os.close(master);os.close(slave)


def test_answer_delivery_keeps_winning_answer_only(tmp_path, monkeypatch):
    pytest.importorskip('rich')
    m=load_helper('tally-decide')
    monkeypatch.setattr(m,'SIGNALS',tmp_path/'signals');m.SIGNALS.mkdir()
    monkeypatch.setattr(m,'ANSWERS',tmp_path/'answers');m.ANSWERS.mkdir()
    monkeypatch.setattr(m,'LOG',tmp_path/'log')
    monkeypatch.setattr(m,'TALLY_BIN',str(tmp_path/'no-command'))
    (m.SIGNALS/'choice.json').write_text(json.dumps({'meta':{'raiser_pane':'test:1.1'}}))
    (m.ANSWERS/'choice.json').write_text(json.dumps({'at':'stamp','answer':'B'}))
    sent=[]
    monkeypatch.setattr(m,'_pane_alive',lambda p:True)
    monkeypatch.setattr(m,'_paste_into_pane',lambda pane,text:sent.append((pane,text)) or True)
    assert m.deliver('choice','Choice','A','stamp')==1
    assert not sent and (m.SIGNALS/'choice.json').exists()
    assert m.deliver('choice','Choice','B','stamp')==0
    assert len(sent)==1 and sent[0][0]=='test:1.1'
    assert not (m.SIGNALS/'choice.json').exists()


def test_codex_mute_is_full_uuid_and_rearms(tmp_path, monkeypatch):
    from tallydeck.sources.codex_sessions import CodexSessionsSource
    monkeypatch.setenv('TALLYDECK_STATE',str(tmp_path/'state'))
    files=[log(tmp_path,uid,'Please approve B.', 'codex') for uid in IDS]
    for fp in files:os.utime(fp,(time.time()-30,time.time()-30))
    ack=tmp_path/'state/acked'/IDS[0];ack.parent.mkdir(parents=True);ack.touch()
    src=CodexSessionsSource(root=str(tmp_path),sync_titles=False,dwell=0,codex_home=str(tmp_path),socket='/nonexistent')
    monkeypatch.setattr(src,'_recent_rollouts',lambda now:files)
    monkeypatch.setattr(src,'_codex_panes',lambda *a:{})
    states={s.meta['session']:s.state for s in src.poll()}
    assert states=={IDS[0]:'idle',IDS[1]:'attention'}
    os.utime(ack,(0,0))
    assert all(s.state=='attention' for s in src.poll())
    assert not ack.exists()


def test_verbatim_choices_recommendation(tmp_path):
    text='Please choose A or B.\n\n1. A: fastest\n2. B: simplest\n\nI recommend B because there is less to maintain.'
    log(tmp_path,IDS[0],text)
    doc=brief.document(IDS[0],'',roots=[tmp_path],state='attention')
    assert doc['sections'][1][1]=='1. A: fastest\n2. B: simplest\n\nI recommend B because there is less to maintain.'
    log(tmp_path,IDS[0],'Should I proceed?')
    doc=brief.document(IDS[0],'',roots=[tmp_path],state='attention')
    assert not any(h.startswith('CHOICES') for h,t in doc['sections'])


def test_hook_brief_uses_full_permission_signal(tmp_path, monkeypatch):
    pytest.importorskip('rich')
    import argparse
    from tallydeck import cli, popup
    monkeypatch.setenv('TALLYDECK_STATE',str(tmp_path))
    folder=tmp_path/'signals';folder.mkdir()
    detail='Permission to execute this command:\n\n'+('command detail '*500)+'FINALCLAUSE'
    (folder/'ask-test.json').write_text(json.dumps({'state':'blocked','detail':detail}))
    captured=[]
    def choose(render, actions, **kwargs):
        c=popup.Console(file=io.StringIO(),width=100)
        c.print(render(100));captured.append(c.file.getvalue());return 't'
    monkeypatch.setattr(popup,'choose',choose)
    args=argparse.Namespace(session='',project='',label='Permission',state='blocked',ask='',interactive=True,
                            signal='sig/ask-test',actions='t tab',taken='',action_file=str(tmp_path/'key'))
    cli.cmd_brief({'sources':[]},args)
    assert 'FINALCLAUSE' in captured[0] and (tmp_path/'key').read_text()=='t'


def test_paneless_codex_resume_keeps_harness_uuid(tmp_path):
    fake=tmp_path/'bin';fake.mkdir();package=tmp_path/'pkg/tallydeck';package.mkdir(parents=True)
    (package/'__init__.py').write_text('')
    (package/'cli.py').write_text("import sys\nfrom pathlib import Path\na=sys.argv\nPath(a[a.index('--action-file')+1]).write_text('t')\n")
    tmux=fake/'tmux';tmux.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_LOG"\nexit 0\n');tmux.chmod(0o755)
    env=dict(os.environ,PATH=str(fake)+':'+os.environ['PATH'],TALLYDECK_HOME=str(package.parent),TALLYDECK_STATE=str(tmp_path/'state'),TEST_LOG=str(tmp_path/'calls'))
    r=subprocess.run([str(ROOT/'contrib/tally-route'),'tty','here','',IDS[0],str(tmp_path),'Brief','attention','O','cx/'+IDS[0],''],env=env,cwd=tmp_path,capture_output=True,text=True,timeout=5)
    assert r.returncode==0, r.stderr
    calls=(tmp_path/'calls').read_text()
    assert 'codex resume --dangerously-bypass-approvals-and-sandbox '+IDS[0] in calls
    assert 'claude --' not in calls


def test_status_table_question_marks_do_not_flash():
    assert not decision_text('All jobs healthy.\n\n| Job | ETA |\n|---|---|\n| worker | ? |')
    assert not decision_text('Shipped. Anything else you want in the v2?')


def test_details_summary_and_fenced_table_remain_visible():
    pytest.importorskip('rich')
    from tallydeck.popup import Console,body
    out=io.StringIO();c=Console(file=out,width=32)
    c.print(body('<details>\n<summary>Critical condition</summary>\n\nRequired evidence\n</details>\n\n```text\n| A | B |\n|---|---|\n| X | Y |\n```','Ask',32))
    assert 'Critical condition' in out.getvalue() and 'Required evidence' in out.getvalue()
    assert '|---|---|' in out.getvalue()


def test_codex_structured_question_stays_attention_until_answer(tmp_path):
    tool={'type':'response_item','payload':{'type':'function_call','name':'functions.request_user_input','arguments':json.dumps({'questions':[{'question':'Which option?', 'options':[{'label':'A','description':'existing'},{'label':'B','description':'new'}]}]})}}
    fp=log(tmp_path,IDS[0],'Status: preparing.', 'codex')
    with fp.open('a') as f:f.write(json.dumps(tool)+'\n')
    assert codex_classify(_tail_lines(fp))[0]=='attention'
    doc=brief.document(IDS[0],'',roots=[tmp_path],codex_roots=[tmp_path],state='attention')
    assert 'Which option?' in doc['sections'][0][1] and 'B: new' in doc['sections'][0][1]
    with fp.open('a') as f:f.write(json.dumps({'type':'response_item','payload':{'type':'function_call_output','output':'A'}})+'\n')
    assert codex_classify(_tail_lines(fp))[0]=='working'
    from tallydeck.transcripts import pending_ask
    assert pending_ask(fp, 'codex') == ''
