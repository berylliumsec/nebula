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
import pytest


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


@pytest.mark.parametrize("columns,rows", [(120, 30), (52, 18)])
def test_full_screen_scrolls_from_composer_and_keeps_position_during_output(
    columns, rows
):
    code = """
import asyncio,json
from nebula.terminal.tui import TuiUI
async def main():
    ui=TuiUI()
    await ui.start()
    ui.open_view({'id':'project','name':'Project'},{'id':'chat','title':'Scroll chat'})
    ui.show_history([{'role':'assistant','content':'\\n\\n'.join(f'History line {i:03}' for i in range(100)),'metadata':{}}])
    async def update():
        while ui.composer.text != 'draft!':
            await asyncio.sleep(0.01)
        ui.delta('\\n\\nLIVE TAIL')
        ui._render()
        ui.activity('Update received')
        while ui.composer.text != 'draft!!':
            await asyncio.sleep(0.01)
        ui.delta('\\n\\nFOLLOWED TAIL')
        ui._render()
    task=asyncio.create_task(update())
    value=await ui.ask('You',main=True)
    await task
    await ui.close()
    print('VALUE:'+json.dumps(value),flush=True)
asyncio.run(main())
"""
    with terminal(code, ready=b"Message or /command", columns=columns, rows=rows) as (
        process,
        master,
        output,
        read,
    ):
        read("History line 099", rendered=True)
        os.write(master, b"draft")
        read("> draft", rendered=True)
        # SGR wheel events over the transcript must work with composer focus.
        os.write(master, b"\x1b[<64;45;8M" * 12)
        read(
            "History line 085" if columns >= 110 else "History line 090", rendered=True
        )
        assert "History line 099" not in "\n".join(read.screen.display)
        os.write(master, b"\x1b[5~")  # Page Up targets the transcript directly.
        read("PgUp", rendered=True)
        os.write(master, b"\x1b[1;5H")  # Ctrl-Home.
        read("History line 000", rendered=True)
        os.write(master, b"\t!")  # Return to the draft and trigger streamed output.
        read("Update received", rendered=True)
        display = "\n".join(read.screen.display)
        assert "History line 000" in display
        assert "LIVE TAIL" not in display
        os.write(master, b"\x1b[1;5F")  # Ctrl-End resumes following output.
        read("LIVE TAIL", rendered=True)
        os.write(master, b"\x1b[5~\x1b[6~")
        read("LIVE TAIL", rendered=True)
        os.write(master, b"\x1b[24~")  # F12 allows native terminal selection.
        read("F12 Scroll", rendered=True)
        os.write(master, b"\x1b[24~")
        read("F12 Copy", rendered=True)
        os.write(master, b"\x1b[<64;45;8M" * 6)
        read("History line 095", rendered=True)
        os.write(master, b"\x1b[<65;45;8M" * 6)
        read("LIVE TAIL", rendered=True)
        os.write(master, b"\t!")
        read("FOLLOWED TAIL", rendered=True)
        os.write(master, b"\r")
        read(b'VALUE:"draft!!"')
        process.wait(timeout=3)
        assert process.returncode == 0
        assert b"\x1b[?1000h" in output  # Real mouse reporting was enabled.


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


def test_full_screen_spinner_changes_during_a_core_read_and_stops_afterward():
    code = """
import asyncio
from nebula.terminal.tui import TuiUI
async def main():
    ui=TuiUI()
    await ui.start()
    ui.open_core('https://core.example:8765')
    ui.begin_loading('Opening conversation…')
    await asyncio.sleep(1.5)
    ui.clear_loading()
    await asyncio.sleep(0.5)
    await ui.close()
    print('ANIMATION_DONE',flush=True)
asyncio.run(main())
"""
    with terminal(code, ready=b"Message or /command", columns=120, rows=30) as (
        process,
        _master,
        output,
        read,
    ):
        read("OPENING", rendered=True)
        assert "Opening conversation" in "\n".join(read.screen.display)
        rendered = read(b"ANIMATION_DONE").decode(errors="replace")
        assert len({glyph for glyph in "◐◓◑◒" if glyph in rendered}) >= 2
        assert "READY" in rendered
        process.wait(timeout=3)
        assert process.returncode == 0


def test_full_screen_wide_layout_fills_pane_beside_rail():
    code = """
import asyncio
from nebula.terminal.tui import TuiUI
async def main():
    ui=TuiUI()
    await ui.start()
    ui.open_core('https://core.example:8765')
    project={'id':'project','name':'Apple Bug Bounty'}
    chat={'id':'chat','title':'Simulate open memory lane with a very long generated title ' * 5}
    idle={'id':'idle','title':'Idle neighbor'}
    ui.open_view(project,chat)
    ui.set_navigation('https://core.example:8765',[project],[chat,idle])
    ui.set_conversation_activity({'chat':'working','idle':'idle'})
    ui.say('plan: '+'long content ' * 35)
    ui._render()
    await ui.ask('You',main=True)
    await ui.close()
asyncio.run(main())
"""
    with terminal(code, ready=b"Message or /command", columns=202, rows=30) as (
        _process,
        _master,
        _output,
        read,
    ):
        read("plan:", rendered=True)
        # PTY writes can split one redraw across reads, especially on macOS.
        # Wait for the navigation state before taking the complete screen snapshot.
        read("● Running", rendered=True)
        read("○ Idle", rendered=True)
        rows = read.screen.display
        assert any("● Running" in row for row in rows)
        assert any("○ Idle" in row for row in rows)
        assert any("plan:" in row for row in rows)
        assert max(row.count("long content") for row in rows) >= 9
        assert "Simulate open memory lane" in "\n".join(rows)
        assert any(
            "Message or /command" in row and row.rfind("┐") >= 198
            for row in rows[-6:-1]
        )


def test_full_screen_chat_roles_and_repeated_requests_have_visual_hierarchy():
    code = """
import asyncio
from nebula.terminal.tui import TuiUI
async def main():
    ui=TuiUI()
    await ui.start()
    ui.open_view({'id':'project','name':'Project'},{'id':'chat','title':'Long chat'})
    request=('Review the active Nebula goal and its completion criteria. '
             'Continue concrete work now. If all criteria are met, give an '
             'evidence-backed completion summary. Do not stop merely to report status.')
    ui.show_history([
        {'role':'user','content':request,'metadata':{}},
        {'role':'assistant','content':'First response with a clear result.','metadata':{}},
        {'role':'user','content':request,'metadata':{}},
        {'role':'assistant','content':'Second response with a clear result.','metadata':{}},
    ])
    await ui.ask('You',main=True)
    await ui.close()
asyncio.run(main())
"""
    with terminal(code, ready=b"Message or /command", columns=202, rows=30) as (
        _process,
        master,
        _output,
        read,
    ):
        read("YOU · REPEATED", rendered=True)
        # A PTY can yield while prompt_toolkit is still painting later rows.
        read("Second response with a clear result.", rendered=True)
        rows = read.screen.display
        display = "\n".join(rows)
        assert "◆  YOU" in display
        assert "✦  ASSISTANT" in display
        assert "│  First response" in display
        assert "│  Second response" in display
        assert "Same request as earlier" in display
        assert not any(row.strip() in {"t", "r"} for row in rows)
        os.write(master, b"\x1b[21~")  # F10 shows each full original request.
        read("F10 Render", rendered=True)
        assert "YOU · REPEATED" not in "\n".join(read.screen.display)


def test_full_screen_goal_timeout_stays_open_and_requires_state_readback():
    code = """
import asyncio,httpx
from pathlib import Path
from nebula.terminal.shell import Shell
from nebula.terminal.state import View
from nebula.terminal.transport import Settings
from nebula.terminal.tui import TuiUI
async def main():
    ui=TuiUI()
    await ui.start()
    settings=Settings(Path('/tmp/unused-nebula-pairing.json'),{'cores':{}},'https://core.example')
    shell=Shell(settings,ui=ui)
    attempts=[]
    async def connect(_selected):
        ui.open_core('https://core.example')
    async def select_project(_selected=None,chat=None):
        shell.view=View(project={'id':'project','name':'Project'},session={'id':'chat','title':'Chat'})
        ui.open_view(shell.view.project,shell.view.session)
    async def command(value):
        if value=='/goal start':
            attempts.append(value)
            raise httpx.ReadTimeout('')
        if value=='/quit':
            shell.running=False
    shell.connect=connect
    shell.select_project=select_project
    shell.command=command
    await shell.run()
    await ui.close()
    print('CLOSED:'+str(len(attempts)),flush=True)
asyncio.run(main())
"""
    with terminal(code, ready=b"Message or /command", columns=120, rows=30) as (
        process,
        master,
        output,
        read,
    ):
        os.write(master, b"/goal start\r")
        read("The goal may have changed", rendered=True)
        read("Use /goal show", rendered=True)
        assert "Use /goal show" in "\n".join(read.screen.display)
        os.write(master, b"/quit\r")
        read(b"CLOSED:1")
        process.wait(timeout=3)
        assert process.returncode == 0
        assert b"IndexError" not in output


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


def test_full_screen_slash_menu_appears_and_selects_a_context_command():
    code = """
import asyncio,json
from nebula.terminal.tui import TuiUI
async def main():
    ui=TuiUI()
    await ui.start()
    ui.open_view({'id':'project','name':'Project'},None)
    ui.set_commands(['status','stop'])
    value=await ui.ask('You',main=True)
    await ui.close()
    print('VALUE:'+json.dumps(value),flush=True)
asyncio.run(main())
"""
    with terminal(code, ready=b"Message or /command") as (
        process,
        master,
        output,
        read,
    ):
        os.write(master, b"/")
        read("/status", rendered=True)
        display = "\n".join(read.screen.display)
        assert "/stop" in display
        assert "Tab Insert" in display
        assert b"VALUE:" not in output
        os.write(master, b"\x1b[B\x1b[B\r")
        read(b'VALUE:"/stop"')
        process.wait(timeout=3)
        assert process.returncode == 0


def test_full_screen_tab_inserts_slash_choice_before_running_it():
    code = """
import asyncio,json
from nebula.terminal.tui import TuiUI
async def main():
    ui=TuiUI()
    await ui.start()
    ui.open_view({'id':'project','name':'Project'},None)
    ui.set_commands(['projects','status'])
    value=await ui.ask('You',main=True)
    await ui.close()
    print('VALUE:'+json.dumps(value),flush=True)
asyncio.run(main())
"""
    with terminal(code, ready=b"Message or /command") as (
        process,
        master,
        output,
        read,
    ):
        os.write(master, b"/s")
        read("/status", rendered=True)
        os.write(master, b"\t")
        assert b"VALUE:" not in output
        os.write(master, b"\r")
        read(b'VALUE:"/status"')
        process.wait(timeout=3)
        assert process.returncode == 0
