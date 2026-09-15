"""Launch tests use the installed ownership algorithm with disposable state."""
from pathlib import Path
import shlex
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLAIM = Path('/home/openclaw/scripts/work-claim.py')


@pytest.fixture
def private_claims(tmp_path):
    # Fail if the real helper is unavailable: a success stub or skip would hide
    # loss of ownership coverage. Loading it does not run main or touch REG.
    assert CLAIM.is_file(), f'required ownership helper missing: {CLAIM}'
    registry = tmp_path / 'private-claims'
    helper = tmp_path / 'work-claim'
    helper.write_text(
        f'#!{sys.executable}\n'
        'import pathlib, runpy, sys\n'
        f'namespace = runpy.run_path({str(CLAIM)!r}, run_name="private_claim_test")\n'
        'main = namespace["main"]\n'
        f'main.__globals__["REG"] = pathlib.Path({str(registry)!r})\n'
        'sys.exit(main())\n'
    )
    helper.chmod(0o755)
    # Relocate only the helper binding in a disposable launcher. Production
    # retains its fixed shared registry; no new override/bypass is introduced.
    source = (ROOT / 'contrib/tally-spawn').read_text()
    binding = f'CLAIM={CLAIM}'
    assert source.count(binding + '\n') == 1
    spawn = tmp_path / 'tally-spawn'
    spawn.write_text(source.replace(binding + '\n',
                                    f'CLAIM={shlex.quote(str(helper))}\n'))
    spawn.chmod(0o755)
    return {'helper': helper, 'registry': registry, 'spawn': spawn,
            'env': {'TALLY_SPAWN_BIN': str(spawn), 'TALLYDECK_HOME': str(ROOT)}}
