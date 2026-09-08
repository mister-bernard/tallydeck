"""Exercise routing without invoking a real model or the live tmux server."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from tallydeck import launch

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def route_env(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, 'ancestor_harness', lambda: '')
    fake = tmp_path / 'agent'
    fake.write_text('#!/bin/sh\nexit 0\n')
    fake.chmod(0o755)
    return {'TALLY_ACCOUNTS_FILE': str(tmp_path / 'missing.json'),
            'CLAUDE_BIN': str(fake), 'CLAUDE_BIN_B': str(fake), 'CODEX_BIN': str(fake)}


@pytest.mark.parametrize('markers,want', [
    ({'CODEX_THREAD_ID': 'cx'}, ('codex', 'O')),
    ({'CLAUDECODE': '1'}, ('claude', 'A')),
    ({'CLAUDE_SESSION_ID': 'cc', 'HOME': '/x/.claude-b'}, ('claude', 'B')),
    ({'CLAUDECODE': '1', 'CLAUDE_CONFIG_DIR': '/x/.claude-b/.claude'}, ('claude', 'B')),
    ({'TALLY_HARNESS': 'codex', 'TALLY_ACCOUNT': 'O'}, ('codex', 'O')),
    ({}, ('claude', 'A')),
])
def test_inherit(route_env, markers, want):
    r = launch.resolve_launch(env=route_env | markers)
    assert (r['harness'], r['account']) == want


@pytest.mark.parametrize('account,harness,want', [
    ('A', '', ('claude', 'A')), ('B', '', ('claude', 'B')),
    ('O', '', ('codex', 'O')), ('', 'claude', ('claude', 'A')),
    ('', 'codex', ('codex', 'O')),
])
def test_explicit_override(route_env, account, harness, want):
    r = launch.resolve_launch(account, harness, env=route_env | {'CODEX_THREAD_ID': 'cx'})
    assert (r['harness'], r['account']) == want


def test_conflicting_markers_use_nearest_harness(route_env, monkeypatch):
    monkeypatch.setattr(launch, 'ancestor_harness', lambda: 'claude')
    r = launch.resolve_launch(env=route_env | {'CODEX_THREAD_ID': 'stale', 'CLAUDECODE': '1'})
    assert r['harness'] == 'claude'


@pytest.mark.parametrize('account,harness', [('X', ''), ('nonsense', ''), ('A', 'codex'), ('O', 'claude')])
def test_bad_overrides_fail_before_launch(route_env, account, harness):
    with pytest.raises(ValueError):
        launch.resolve_launch(account, harness, env=route_env)


def test_missing_codex_does_not_fall_back_to_claude(route_env):
    with pytest.raises(ValueError, match='codex binary not executable'):
        launch.resolve_launch(env=route_env | {'CODEX_THREAD_ID': 'cx', 'CODEX_BIN': '/missing/codex'})


@pytest.fixture
def isolated(tmp_path):
    capture = tmp_path / 'capture.json'
    fake = tmp_path / 'fake agent'
    fake.write_text('#!/usr/bin/python3\nimport json,os,sys,time\n'
                    'selected = {k:os.environ[k] for k in ("HOME","CODEX_HOME","TALLY_HARNESS","TALLY_ACCOUNT","CODEX_THREAD_ID","CLAUDECODE","CLAUDE_SESSION_ID") if k in os.environ}\n'
                    'json.dump({"argv":sys.argv[1:],"env":selected,"cwd":os.getcwd()},open('
                    f'{str(capture)!r},"w"))\n'
                    'time.sleep(15)\n')
    fake.chmod(0o755)
    sock = str(tmp_path / 'tmux.sock')
    env = dict(os.environ)
    for k in ('TMUX', 'TMUX_PANE', 'CLAUDECODE', 'CLAUDE_SESSION_ID', 'CLAUDE_CODE_SESSION_ID',
              'CODEX_THREAD_ID', 'TALLY_HARNESS', 'TALLY_ACCOUNT', 'CLAUDE_CONFIG_DIR'):
        env.pop(k, None)
    env.update(TALLYDECK_STATE=str(tmp_path), TALLY_TMUX_SOCKET=sock,
               TALLY_ACCOUNTS_FILE=str(tmp_path / 'missing.json'),
               CLAUDE_BIN=str(fake), CLAUDE_BIN_B=str(fake), CODEX_BIN=str(fake),
               TALLY_BIN=str(ROOT / 'contrib/tally'), TALLY_SPAWN_BIN=str(ROOT / 'contrib/tally-spawn'),
               TALLY_OFFER_TIMEOUT='10')
    tmux = ['tmux', '-S', sock]
    subprocess.run(tmux + ['-f', '/dev/null', 'new-session', '-d', '-s', 'bootstrap'], env=env, check=True)
    yield tmp_path, env, tmux, capture
    subprocess.run(tmux + ['kill-server'], capture_output=True)


def wait_json(path):
    for _ in range(80):
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            time.sleep(.1)
    pytest.fail(f'no JSON at {path}')


@pytest.mark.parametrize('markers,options,harness,account', [
    ({'CODEX_THREAD_ID': 'parent-cx'}, [], 'codex', 'O'),
    ({'CLAUDECODE': '1'}, [], 'claude', 'A'),
    ({'CLAUDECODE': '1', 'HOME': '/x/.claude-b'}, [], 'claude', 'B'),
    ({'CODEX_THREAD_ID': 'parent-cx'}, ['-a', 'B'], 'claude', 'B'),
    ({'CLAUDECODE': '1'}, ['--harness', 'codex'], 'codex', 'O'),
])
def test_real_tmux_launch(isolated, markers, options, harness, account):
    state, env, tmux, capture = isolated
    task = 'Keep quotes " and `backticks` and $(literal) intact.\nSecond line.'
    r = subprocess.run([str(ROOT / 'contrib/tally-spawn'), 'demo', '-c', str(state), *options, '-p', '-'],
                       input=task, text=True, capture_output=True, env=env | markers, timeout=15)
    assert r.returncode == 0, r.stderr
    got = wait_json(capture)
    rec = wait_json(state / 'spawned/demo.json')
    assert (rec['harness'], rec['account']) == (harness, account)
    assert got['argv'] == [('--dangerously-bypass-approvals-and-sandbox' if harness == 'codex' else '--dangerously-skip-permissions'), task]
    assert got['cwd'] == str(state)
    assert got['env']['HOME'] == str(launch.operator_home())
    assert got['env']['TALLY_HARNESS'] == harness
    for k in ('CODEX_THREAD_ID', 'CLAUDECODE', 'CLAUDE_SESSION_ID'):
        assert k not in got['env']
    actual = subprocess.run(tmux + ['list-panes', '-t', 'demo', '-F', '#S:#I.#P'], capture_output=True, text=True).stdout.strip()
    assert rec['target'] == actual  # no assumption about tmux base-index
    if markers.get('CODEX_THREAD_ID'):
        assert rec['origin_session'] == 'parent-cx'


def test_offer_freezes_codex_route_until_yes(isolated):
    state, env, tmux, capture = isolated
    r = subprocess.run([str(ROOT / 'contrib/tally-offer'), 'offered', 'A task', '-c', str(state), '-p', '-'],
                       input='Full brief', text=True, capture_output=True,
                       env=env | {'CODEX_THREAD_ID': 'parent-cx'}, timeout=15)
    assert r.returncode == 0, r.stderr
    offer = wait_json(state / 'offers/offered.json')
    assert (offer['harness'], offer['account'], offer['origin_session']) == ('codex', 'O', 'parent-cx')
    signal = wait_json(state / 'signals/offer-offered.json')
    assert 'codex' in signal['detail'] and 'account O' in signal['detail']
    (state / 'answers').mkdir(exist_ok=True)
    time.sleep(.3)
    (state / 'answers/offer-offered.json').write_text(json.dumps({'answer': '1 — Yes', 'at': time.time()}))
    got = wait_json(capture)
    assert got['env']['TALLY_HARNESS'] == 'codex'
    assert got['argv'][-1] == 'Full brief'
    rec = wait_json(state / 'spawned/offered.json')
    assert rec['account'] == 'O'


def test_waiter_passes_stored_override_even_under_other_harness(isolated, monkeypatch):
    state, env, _, _ = isolated
    loader = importlib.machinery.SourceFileLoader('test_offer_module', str(ROOT / 'contrib/tally-offer'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    monkeypatch.setenv('TALLYDECK_STATE', str(state))
    monkeypatch.setenv('CLAUDECODE', '1')
    (state / 'offers').mkdir()
    (state / 'offers/job.json').write_text(json.dumps({'pane': '', 'cwd': str(state), 'task': 'brief', 'harness': 'codex', 'account': 'O'}))
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({'answer': '1'}) if 'wait' in argv else 'job', '')
    monkeypatch.setattr(mod.subprocess, 'run', fake_run)
    monkeypatch.setattr(mod, 'paste', lambda *a: True)
    assert mod.waiter('job') == 0
    assert calls[1][calls[1].index('--harness') + 1] == 'codex'
    assert calls[1][calls[1].index('-a') + 1] == 'O'
