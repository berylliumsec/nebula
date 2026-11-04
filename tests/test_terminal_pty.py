"""Actual POSIX terminal input: paste, multiline, resize and interruption."""

from __future__ import annotations

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


@contextmanager
def terminal(code):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
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

    def read_until(marker, timeout=8):
        deadline = time.monotonic() + timeout
        while marker not in output and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
        assert marker in output, output.decode(errors="replace")
        return bytes(output)

    try:
        read_until(b"You:")
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
    with terminal(CODE) as (process, master, output, read):
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
    with terminal(code) as (process, master, output, read):
        os.write(master, b"unsent\x03")
        read(b"CANCELLED_MENU")
        process.wait(timeout=3)
        assert process.returncode == 0
