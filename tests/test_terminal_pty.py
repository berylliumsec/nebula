"""Actual POSIX terminal input: paste, multiline, resize and interruption."""

from __future__ import annotations

import codecs
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from contextlib import contextmanager

import pyte


@contextmanager
def terminal(code, *, ready=b"You:", columns=80, rows=24):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm-256color", "PROMPT_TOOLKIT_NO_CPR": "1"},
        close_fds=True,
    )
    os.close(slave)
    output = bytearray()
    screen = pyte.Screen(columns, rows)
    screen_stream = pyte.Stream(screen)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def read_until(marker, timeout=8, *, rendered=False):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = "\n".join(screen.display) if rendered else bytes(output)
            if marker in current:
                return bytes(output)
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                screen_stream.feed(decoder.decode(chunk))
        assert marker in ("\n".join(screen.display) if rendered else bytes(output)), (
            output.decode(errors="replace")
        )
        return bytes(output)

    read_until.screen = screen
    try:
        read_until(ready)
        yield process, master, output, read_until
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        os.close(master)


CODE = """
import asyncio,json
from nebula.terminal.ui import TerminalUI
async def main():
    ui=TerminalUI()
    value=await ui.ask('You')
    print('VALUE:'+json.dumps(value), flush=True)
asyncio.run(main())
"""


def test_bracketed_multiline_paste_waits_for_explicit_send():
    with terminal(CODE) as (process, master, output, read):
        os.write(master, b"\x1b[200~first line\nsecond line\x1b[201~")
        # A round-trip screen update proves input has been processed before send.
        read(b"second line")
        assert b"VALUE:" not in output
        os.write(master, b"\r")
        result = read(b'VALUE:"first line\\nsecond line"')
        process.wait(timeout=3)
        assert process.returncode == 0, result


def test_escape_enter_adds_newline_and_unicode_survives_resize():
    with terminal(CODE) as (process, master, _output, read):
        os.write(master, "alpha Ω".encode() + b"\x1b\r")
        read("alpha Ω".encode())
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 40, 0, 0))
        os.write(master, b"beta\r")
        read(b'VALUE:"alpha \\u03a9\\nbeta"')
        process.wait(timeout=3)
        assert process.returncode == 0


def test_control_c_cancels_a_menu_without_submitting():
    code = """
import asyncio
from nebula.terminal.ui import TerminalUI,CancelledInput
async def main():
    try: await TerminalUI().ask('You')
    except CancelledInput: print('CANCELLED_MENU',flush=True)
asyncio.run(main())
"""
    with terminal(code) as (process, master, _output, read):
        os.write(master, b"unsent\x03")
        read(b"CANCELLED_MENU")
        process.wait(timeout=3)
        assert process.returncode == 0


TUI_CODE = """
import asyncio,json
from nebula.terminal.tui import TuiUI
async def main():
    ui=TuiUI()
    await ui.start()
    ui.open_view({'id':'project','name':'Project'},None)
    async def progress():
        await asyncio.sleep(0.2)
        ui.activity('Searching Core')
        ui.delta('Partial answer')
    asyncio.create_task(progress())
    value=await ui.ask('You',main=True)
    await ui.close()
    print('VALUE:'+json.dumps(value),flush=True)
asyncio.run(main())
"""


def test_full_screen_progress_paste_and_narrow_resize():
    with terminal(TUI_CODE, ready=b"Message or /command") as (
        process,
        master,
        output,
        read,
    ):
        read("Searching Core", rendered=True)
        read("Partial answer", rendered=True)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 42, 0, 0))
        os.write(master, b"\x1b[200~first line\nsecond line\x1b[201~")
        read(b"second line")
        assert b"VALUE:" not in output
        os.write(master, b"\r")
        read(b'VALUE:"first line\\nsecond line"')
        process.wait(timeout=3)
        assert process.returncode == 0


def test_full_screen_renders_markdown_and_toggles_original_source():
    code = """
import asyncio
from nebula.terminal.tui import TuiUI
async def main():
    ui=TuiUI()
    await ui.start()
    project={'id':'project','name':'Apple Bug Bounty'}
    session={'id':'butler','title':'Butler'}
    ui.open_view(project,session)
    ui.set_navigation('https://core.example:8765',[project],[session])
    ui.show_history([{'role':'assistant','content':'**Paused** [parent](/projects/project/workbench?view=chat&session=butler)\\n\\n| Goal | State |\\n|---|---|\\n| HEIF | Running |','metadata':{}}])
    await ui.ask('You',main=True)
    await ui.close()
asyncio.run(main())
"""
    with terminal(code, ready=b"Message or /command", columns=120, rows=30) as (
        _process,
        master,
        _output,
        read,
    ):
        read("Paused parent", rendered=True)
        display = "\n".join(read.screen.display)
        assert "› Butler" in display
        assert "HEIF" in display and "Running" in display
        assert "**Paused**" not in display
        assert "/projects/project/workbench" not in display
        os.write(master, b"\x1b[21~")  # F10
        read("**Paused**", rendered=True)
        assert "F10 Render" in "\n".join(read.screen.display)


def test_full_screen_searchable_keyboard_menu():
    code = """
import asyncio
from nebula.terminal.tui import TuiUI
async def main():
    ui=TuiUI()
    await ui.start()
    item=await ui.choose('Project',[{'id':'a','name':'Alpha'},{'id':'b','name':'Beta'}])
    await ui.close()
    print('PICK:'+item['id'],flush=True)
asyncio.run(main())
"""
    with terminal(code, ready=b"Search:") as (process, master, _output, read):
        os.write(master, b"Beta\r")
        read(b"PICK:b")
        process.wait(timeout=3)
        assert process.returncode == 0


def test_full_screen_command_shortcut_preserves_unsent_draft():
    code = """
import asyncio
from nebula.terminal.tui import TuiUI
async def main():
    ui=TuiUI()
    await ui.start()
    command=await ui.ask('You',main=True)
    draft=ui.take_preserved_draft()
    await ui.close()
    print('SHORTCUT:'+command+'|'+draft,flush=True)
asyncio.run(main())
"""
    with terminal(code, ready=b"Message or /command") as (
        process,
        master,
        _output,
        read,
    ):
        os.write(master, b"unsent draft\x1bOS")
        read(b"SHORTCUT:/commands|unsent draft")
        process.wait(timeout=3)
        assert process.returncode == 0
