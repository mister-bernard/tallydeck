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


def _two_codex_accounts(tmp_path, route_env, fake_bin):
    """A tokenburn config with both Codex accounts, as the live one has."""
    cfg = tmp_path / 'tokenburn.json'
    cfg.write_text(json.dumps({'accounts': [
        {'id': 'A', 'provider': 'anthropic', 'claude_bin': fake_bin},
        {'id': 'O', 'provider': 'codex', 'enabled': True, 'aliases': ['1', 'codex1'],
         'codex_bin': fake_bin, 'codex_home': '/home/openclaw/.codex'},
        {'id': 'O2', 'provider': 'codex', 'enabled': True, 'aliases': ['2', 'codex2'],
         'codex_bin': fake_bin, 'codex_home': '/home/openclaw/.codex-2'},
    ]}))
    return route_env | {'TALLY_ACCOUNTS_FILE': str(cfg)}


@pytest.mark.parametrize('account,want_id,want_home', [
    ('2', 'O2', '/home/openclaw/.codex-2'),
    ('O2', 'O2', '/home/openclaw/.codex-2'),
    ('codex2', 'O2', '/home/openclaw/.codex-2'),
    ('1', 'O', '/home/openclaw/.codex'),
    ('O', 'O', '/home/openclaw/.codex'),
])
def test_codex_account_aliases_route_to_their_own_home(
        tmp_path, route_env, account, want_id, want_home):
    """G calls the Codex accounts 1 and 2, so `tally spawn -a 2` has to land on
    O2 — and on ITS CODEX_HOME, or the session would run on account 1's login
    while every label said 2."""
    env = _two_codex_accounts(tmp_path, route_env, route_env['CODEX_BIN'])
    r = launch.resolve_launch(account, env=env)
    assert (r['harness'], r['account'], r['codex_home']) == ('codex', want_id, want_home)


def test_codex_default_skips_a_parked_account(tmp_path, route_env):
    """With account 1 parked (account-mode.sh codex single would do the
    reverse), a bare codex spawn must not land on a disabled lane."""
    env = _two_codex_accounts(tmp_path, route_env, route_env['CODEX_BIN'])
    cfg = Path(env['TALLY_ACCOUNTS_FILE'])
    data = json.loads(cfg.read_text())
    data['accounts'][1]['enabled'] = False        # park account 1
    cfg.write_text(json.dumps(data))
    assert launch.resolve_launch('', 'codex', env=env)['account'] == 'O2'


def test_codex_alias_never_overrides_a_real_id(tmp_path, route_env):
    """An account whose id IS "2" wins over anything aliasing to it."""
    cfg = tmp_path / 'tokenburn.json'
    cfg.write_text(json.dumps({'accounts': [
        {'id': '2', 'provider': 'codex', 'enabled': True,
         'codex_bin': route_env['CODEX_BIN'], 'codex_home': '/tmp/two'},
        {'id': 'O2', 'provider': 'codex', 'enabled': True, 'aliases': ['2'],
         'codex_bin': route_env['CODEX_BIN'], 'codex_home': '/tmp/o2'},
    ]}))
    r = launch.resolve_launch('2', env=route_env | {'TALLY_ACCOUNTS_FILE': str(cfg)})
    assert (r['account'], r['codex_home']) == ('2', '/tmp/two')


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


@pytest.mark.parametrize('account,harness', [('Z', ''), ('nonsense', ''), ('A', 'codex'), ('O', 'claude')])
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
               # These tests are about ROUTING, not admission. The memory gate
               # is a property of the machine, and on a box under the pressure
               # the gate exists for it refuses every spawn — which would make
               # this whole file fail for a reason it is not testing. The gate
               # has its own tests below.
               TALLY_SPAWN_NO_GATE='1')
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
    assert got['argv'][0] == ('--dangerously-bypass-approvals-and-sandbox' if harness == 'codex' else '--dangerously-skip-permissions')
    assert len(got['argv']) == 2
    prompt = got['argv'][1]
    assert prompt.startswith('You are already the dedicated worker for this task.')
    assert 'Do not spawn another session, subagent, or one-shot worker' in prompt
    assert 'do not\nretry, rename it, or bypass the memory gate' in prompt
    assert prompt.endswith('Task:\n' + task)
    assert Path(rec['task_file']).read_text() == prompt + '\n'
    assert got['cwd'] == str(state)
    assert got['env']['HOME'] == str(launch.operator_home())
    assert got['env']['TALLY_HARNESS'] == harness
    for k in ('CODEX_THREAD_ID', 'CLAUDECODE', 'CLAUDE_SESSION_ID'):
        assert k not in got['env']
    actual = subprocess.run(tmux + ['list-panes', '-t', 'demo', '-F', '#S:#I.#P'], capture_output=True, text=True).stdout.strip()
    assert rec['target'] == actual  # no assumption about tmux base-index
    if markers.get('CODEX_THREAD_ID'):
        assert rec['origin_session'] == 'parent-cx'


def test_offer_spawns_the_caller_route_without_a_question(isolated):
    state, env, tmux, capture = isolated
    r = subprocess.run([str(ROOT / 'contrib/tally-offer'), 'offered', 'A task', '-c', str(state), '-p', '-'],
                       input='Full brief', text=True, capture_output=True,
                       env=env | {'CODEX_THREAD_ID': 'parent-cx'}, timeout=30)
    assert r.returncode == 0, r.stderr
    got = wait_json(capture)
    assert got['env']['TALLY_HARNESS'] == 'codex'
    assert got['argv'][-1].startswith('You are already the dedicated worker for this task.')
    assert got['argv'][-1].endswith('Task:\nFull brief')
    rec = wait_json(state / 'spawned/offered.json')
    assert (rec['account'], rec['harness'], rec['origin_session']) == ('O', 'codex', 'parent-cx')
    assert 'codex' in r.stdout and 'account O' in r.stdout        # the route is reported, not negotiated
    assert not (state / 'signals/offer-offered.json').exists()    # nothing raised on the deck
    assert not (state / 'offers').exists()


def test_offer_hands_spawn_the_resolved_route_explicitly(isolated, monkeypatch):
    """Whatever the caller's own markers say, spawn is told the route that was
    resolved here — a second detection downstream could pick another harness."""
    state, env, _, _ = isolated
    loader = importlib.machinery.SourceFileLoader('test_offer_module', str(ROOT / 'contrib/tally-offer'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    # The test runner itself carries this session's harness markers; the fixture
    # env only omits them, it cannot unset them in our own process.
    for k in ('CLAUDECODE', 'CLAUDE_SESSION_ID', 'CLAUDE_CODE_SESSION_ID', 'TALLY_HARNESS', 'TALLY_ACCOUNT'):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env | {'CODEX_THREAD_ID': 'parent-cx'}).items():
        monkeypatch.setenv(k, str(v))
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, 'job', '')
    monkeypatch.setattr(mod.subprocess, 'run', fake_run)
    assert mod.main(['job', 'A task', '-c', str(state), 'brief text']) == 0
    launch = calls[0]
    assert launch[launch.index('--harness') + 1] == 'codex'
    assert launch[launch.index('-a') + 1] == 'O'


def test_offer_reports_a_failed_spawn_so_the_caller_can_run_it_inline(isolated, monkeypatch, capsys):
    state, env, _, _ = isolated
    loader = importlib.machinery.SourceFileLoader('test_offer_module_fail', str(ROOT / 'contrib/tally-offer'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setattr(mod.subprocess, 'run',
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, '', 'tmux new-session failed'))
    assert mod.main(['job', 'A task', '-c', str(state), 'brief text']) == 1
    assert 'run it here instead' in capsys.readouterr().err


# ── Admission control ────────────────────────────────────────────────────────
# `tally spawn` was unconditional, and a fan-out of unconditional spawns is how
# this box ran out of swap on 2026-09-09 and had nine processes OOM-killed
# across seven services. A spawn the machine cannot afford must be refused, in
# numbers, before it becomes someone else's incident.

def _gate_env(env, **over):
    """Run tally-spawn against a machine whose memory we describe."""
    e = dict(env)
    e.pop('TALLY_SPAWN_NO_GATE', None)
    e.update(over)
    return e


def _spawn(env, *args, meminfo=None, tmp_path=None):
    e = dict(env)
    if meminfo is not None:
        fake = tmp_path / 'meminfo'
        fake.write_text(meminfo)
        # tally-spawn reads /proc/meminfo directly; point a copy of the script
        # at the fake by way of a wrapper that shadows the path.
        e['TALLY_MEMINFO'] = str(fake)
    return subprocess.run([str(ROOT / 'contrib/tally-spawn'), *args],
                          capture_output=True, text=True, env=e, timeout=30)


def _meminfo(avail_mb, swap_free_mb, swap_total_mb=16000):
    return (f"MemTotal:       16000000 kB\n"
            f"MemAvailable:   {avail_mb * 1024} kB\n"
            f"SwapTotal:      {swap_total_mb * 1024} kB\n"
            f"SwapFree:       {swap_free_mb * 1024} kB\n")


def test_spawn_is_refused_when_ram_is_gone(isolated, tmp_path):
    state, env, _, _ = isolated
    r = _spawn(_gate_env(env), 'tight', '-c', str(state), 'task',
               meminfo=_meminfo(avail_mb=200, swap_free_mb=8000), tmp_path=tmp_path)
    assert r.returncode == 3, r.stderr
    assert 'REFUSED' in r.stderr
    assert '200MB' in r.stderr          # names the number it refused on
    assert 'Continue this task inline. Do not retry or bypass' in r.stderr
    assert 'Or override:' not in r.stderr


def test_spawn_is_refused_when_swap_is_gone(isolated, tmp_path):
    """Swap exhaustion, not RAM exhaustion, is what invoked the OOM killer."""
    state, env, _, _ = isolated
    r = _spawn(_gate_env(env), 'tight', '-c', str(state), 'task',
               meminfo=_meminfo(avail_mb=8000, swap_free_mb=10), tmp_path=tmp_path)
    assert r.returncode == 3, r.stderr
    assert 'free swap' in r.stderr


def test_a_healthy_box_still_spawns(isolated, tmp_path):
    state, env, tmux, _ = isolated
    r = _spawn(_gate_env(env), 'roomy', '-c', str(state), 'task',
               meminfo=_meminfo(avail_mb=8000, swap_free_mb=8000), tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == 'roomy'


def test_force_overrides_the_gate(isolated, tmp_path):
    state, env, _, _ = isolated
    r = _spawn(_gate_env(env), 'urgent', '-c', str(state), 'task', '--force',
               meminfo=_meminfo(avail_mb=10, swap_free_mb=0), tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr


def test_no_swap_configured_is_not_an_empty_swap(isolated, tmp_path):
    """A box with swap turned off has SwapTotal=0; that is not a reason to
    refuse every spawn on it forever."""
    state, env, _, _ = isolated
    r = _spawn(_gate_env(env), 'swapless', '-c', str(state), 'task',
               meminfo=_meminfo(avail_mb=8000, swap_free_mb=0, swap_total_mb=0),
               tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
