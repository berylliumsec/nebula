"""Keep release crash checks scoped to their own desktop invocation."""

from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from scripts import test_desktop_crash_cleanup as cleanup


@pytest.mark.skipif(not Path('/proc/self/environ').exists(), reason='Linux procfs required')
def test_only_core_from_this_run_matches():
    processes = []
    try:
        for _ in range(3):
            processes.append(subprocess.Popen(
                [sys.executable, '-c', 'import time; time.sleep(30)', 'serve'],
                start_new_session=True,
            ))
        assert cleanup.matching_core_processes(Path(sys.executable), processes[0].pid) == [
            processes[0].pid
        ]
        assert cleanup.matching_core_processes(Path(sys.executable), -1) == []
    finally:
        for process in processes:
            process.kill()
            process.wait()


@pytest.mark.parametrize('final_matches', [[], [123456]])
def test_deadline_rechecks_before_reporting_orphans(tmp_path, monkeypatch, final_matches):
    desktop = tmp_path / 'nebula-ui'
    desktop.touch()
    desktop.with_name('nebula-core').touch()
    monkeypatch.setattr(sys, 'argv', ['crash-check', str(desktop)])
    process = Mock(pid=654321)
    process.poll.return_value = None
    launch = Mock(return_value=process)
    monkeypatch.setattr(cleanup.subprocess, 'Popen', launch)
    scans = Mock(side_effect=[[123456], final_matches])
    monkeypatch.setattr(cleanup, 'matching_core_processes', scans)
    ticks = iter([0, 0, 0, 16])
    monkeypatch.setattr(cleanup.time, 'monotonic', lambda: next(ticks))
    kill = Mock()
    monkeypatch.setattr(cleanup.os, 'kill', kill)

    if final_matches:
        with pytest.raises(RuntimeError, match='orphaned Nebula Core'):
            cleanup.main()
        assert kill.call_count == 2
    else:
        assert cleanup.main() == 0
        kill.assert_called_once_with(process.pid, cleanup.signal.SIGKILL)
    assert launch.call_args.kwargs['start_new_session'] is True
    assert all(call.args[1] == process.pid for call in scans.call_args_list)


@pytest.mark.skipif(not Path('/proc/self/environ').exists(), reason='Linux procfs required')
def test_crash_check_runs_with_isolated_session(tmp_path):
    """Exercise launch, session inheritance, SIGKILL, and observed child exit."""
    core = tmp_path / 'nebula-core'
    core.symlink_to(sys.executable)
    desktop = tmp_path / 'nebula-ui'
    desktop.write_text(
        f'#!{sys.executable}\n'
        'import os, subprocess, time\n'
        f'core = {str(core)!r}\n'
        'parent = os.getpid()\n'
        'code = f"import os,time\\nwhile os.getppid() == {parent}: time.sleep(0.01)"\n'
        'subprocess.Popen([core, "-c", code, "serve"], env={}, process_group=0)\n'
        'time.sleep(30)\n'
    )
    desktop.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(Path(cleanup.__file__).resolve()), str(desktop)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
