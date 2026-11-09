"""Owned Linux native-window/AT-SPI observations; never imported by product code.

Runs under the packaged driver's private Xvfb and dbus-run-session. It sends a
real WM_DELETE_WINDOW event and drives existing accessible controls. It never
writes browser storage, injects product hooks, or substitutes an API for a draft.
"""

from __future__ import annotations

import ctypes as C
import ctypes.util
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlsplit


def birth(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return (
            "linux:"
            + Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            + ":"
            + fields[19]
        )
    except FileNotFoundError:
        return None  # diagnostic-expected: the owned native incarnation exited.


def wait(predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise TimeoutError("native GUI observation deadline elapsed")


class Attributes(C.Structure):
    _fields_ = [
        ("x", C.c_int),
        ("y", C.c_int),
        ("width", C.c_int),
        ("height", C.c_int),
        ("border_width", C.c_int),
        ("depth", C.c_int),
        ("visual", C.c_void_p),
        ("root", C.c_ulong),
        ("window_class", C.c_int),
        ("bit_gravity", C.c_int),
        ("win_gravity", C.c_int),
        ("backing_store", C.c_int),
        ("backing_planes", C.c_ulong),
        ("backing_pixel", C.c_ulong),
        ("save_under", C.c_int),
        ("colormap", C.c_ulong),
        ("map_installed", C.c_int),
        ("map_state", C.c_int),
        ("all_event_masks", C.c_long),
        ("your_event_mask", C.c_long),
        ("do_not_propagate_mask", C.c_long),
        ("override_redirect", C.c_int),
        ("screen", C.c_void_p),
    ]


class MessageData(C.Union):
    _fields_ = [("b", C.c_char * 20), ("s", C.c_short * 10), ("l", C.c_long * 5)]


class ClientMessage(C.Structure):
    _fields_ = [
        ("type", C.c_int),
        ("serial", C.c_ulong),
        ("send_event", C.c_int),
        ("display", C.c_void_p),
        ("window", C.c_ulong),
        ("message_type", C.c_ulong),
        ("format", C.c_int),
        ("data", MessageData),
    ]


class Event(C.Union):
    _fields_ = [("message", ClientMessage), ("padding", C.c_long * 24)]


class X11:
    def __init__(self):
        self.lib = C.CDLL(ctypes.util.find_library("X11"))
        signatures = {
            "XOpenDisplay": (C.c_void_p, [C.c_char_p]),
            "XDefaultRootWindow": (C.c_ulong, [C.c_void_p]),
            "XInternAtom": (C.c_ulong, [C.c_void_p, C.c_char_p, C.c_int]),
            "XQueryTree": (
                C.c_int,
                [
                    C.c_void_p,
                    C.c_ulong,
                    C.POINTER(C.c_ulong),
                    C.POINTER(C.c_ulong),
                    C.POINTER(C.POINTER(C.c_ulong)),
                    C.POINTER(C.c_uint),
                ],
            ),
            "XGetWindowAttributes": (
                C.c_int,
                [C.c_void_p, C.c_ulong, C.POINTER(Attributes)],
            ),
            "XGetWindowProperty": (
                C.c_int,
                [
                    C.c_void_p,
                    C.c_ulong,
                    C.c_ulong,
                    C.c_long,
                    C.c_long,
                    C.c_int,
                    C.c_ulong,
                    C.POINTER(C.c_ulong),
                    C.POINTER(C.c_int),
                    C.POINTER(C.c_ulong),
                    C.POINTER(C.c_ulong),
                    C.POINTER(C.POINTER(C.c_ubyte)),
                ],
            ),
            "XGetWMProtocols": (
                C.c_int,
                [
                    C.c_void_p,
                    C.c_ulong,
                    C.POINTER(C.POINTER(C.c_ulong)),
                    C.POINTER(C.c_int),
                ],
            ),
            "XSendEvent": (
                C.c_int,
                [C.c_void_p, C.c_ulong, C.c_int, C.c_long, C.POINTER(Event)],
            ),
            "XFlush": (C.c_int, [C.c_void_p]),
            "XFree": (C.c_int, [C.c_void_p]),
            "XCloseDisplay": (C.c_int, [C.c_void_p]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self.lib, name)
            function.restype, function.argtypes = result, arguments
        self.display = self.lib.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("owned fixture X display is unavailable")
        self.root = self.lib.XDefaultRootWindow(self.display)
        self.pid_atom = self.atom("_NET_WM_PID")

    def atom(self, name):
        return self.lib.XInternAtom(self.display, name.encode(), 0)

    def children(self, window):
        root, parent, count, values = (
            C.c_ulong(),
            C.c_ulong(),
            C.c_uint(),
            C.POINTER(C.c_ulong)(),
        )
        if not self.lib.XQueryTree(
            self.display,
            window,
            C.byref(root),
            C.byref(parent),
            C.byref(values),
            C.byref(count),
        ):
            return []
        try:
            return list(values[: count.value])
        finally:
            if values:
                self.lib.XFree(values)

    def pid(self, window):
        kind, count, remaining, format_, payload = (
            C.c_ulong(),
            C.c_ulong(),
            C.c_ulong(),
            C.c_int(),
            C.POINTER(C.c_ubyte)(),
        )
        status = self.lib.XGetWindowProperty(
            self.display,
            window,
            self.pid_atom,
            0,
            1,
            0,
            6,
            C.byref(kind),
            C.byref(format_),
            C.byref(count),
            C.byref(remaining),
            C.byref(payload),
        )
        try:
            if status == 0 and format_.value == 32 and count.value == 1:
                return C.cast(payload, C.POINTER(C.c_ulong))[0]
        finally:
            if payload:
                self.lib.XFree(payload)
        return None

    def windows(self, pid):
        pending, selected, visited = [self.root], [], 0
        while pending:
            window = pending.pop()
            visited += 1
            if visited > 10000:
                raise RuntimeError("fixture X window tree exceeds its bound")
            attributes = Attributes()
            if (
                self.lib.XGetWindowAttributes(self.display, window, C.byref(attributes))
                and attributes.map_state == 2
                and self.pid(window) == pid
            ):
                selected.append(window)
            pending.extend(self.children(window))
        return selected

    def close_window(self, window):
        protocols, count = C.POINTER(C.c_ulong)(), C.c_int()
        delete = self.atom("WM_DELETE_WINDOW")
        if not self.lib.XGetWMProtocols(
            self.display, window, C.byref(protocols), C.byref(count)
        ):
            raise RuntimeError("owned window advertises no close protocol")
        try:
            if delete not in protocols[: count.value]:
                raise RuntimeError("owned window does not support WM_DELETE_WINDOW")
        finally:
            self.lib.XFree(protocols)
        event = Event()
        event.message.type, event.message.display, event.message.window = (
            33,
            self.display,
            window,
        )
        event.message.message_type, event.message.format = self.atom("WM_PROTOCOLS"), 32
        event.message.data.l[0], event.message.data.l[1] = delete, 0
        if not self.lib.XSendEvent(self.display, window, 0, 0, C.byref(event)):
            raise RuntimeError("native window close event was not delivered")
        self.lib.XFlush(self.display)


def descendants(app):
    pending, count = [app], 0
    while pending:
        node = pending.pop()
        if node is None:
            continue
        count += 1
        if count > 10000:
            raise RuntimeError("native accessibility tree exceeds its bound")
        yield node
        pending.extend(node[index] for index in range(node.childCount))


def attributes(node):
    return dict(item.split(":", 1) for item in node.getAttributes() if ":" in item)


def application(pid, atspi):
    return next(
        (node for node in atspi.Registry.getDesktop(0) if node.get_process_id() == pid),
        None,
    )


def visible(node, atspi):
    return node.getState().contains(atspi.STATE_SHOWING)


def named(app, name, atspi, *, contains=False, prefix=False):
    return next(
        (
            node
            for node in descendants(app)
            if visible(node, atspi)
            and node.getRoleName() in {"push button", "button", "link"}
            and (
                name in node.name
                if contains
                else node.name.startswith(name)
                if prefix
                else node.name == name
            )
            and not node.name.startswith("More actions")
        ),
        None,
    )


def activate(node):
    action = node.queryAction()
    if not action.nActions or not action.doAction(0):
        raise RuntimeError("actual native accessible control did not activate")


def project_selection_failure_state(app, project_name, atspi):
    """Bounded structure only: no entry values, arbitrary names or private URIs."""
    controls, documents = [], []
    labels = {
        "Open navigation": "navigation",
        "Switch project": "switcher",
        "Project switcher": "project-dialog",
    }
    expected_seen = False
    for index, node in enumerate(descendants(app)):
        if index >= 256:
            break
        role, name, state = node.getRoleName(), node.name, node.getState()
        expected = name.startswith(project_name)
        expected_seen = expected_seen or (expected and visible(node, atspi))
        label = labels.get(name) or ("expected-project" if expected else None)
        if label and len(controls) < 8:
            controls.append(
                {
                    "control": label,
                    "role": role[:40],
                    "showing": state.contains(atspi.STATE_SHOWING),
                    "expanded": state.contains(atspi.STATE_EXPANDED),
                    "enabled": state.contains(atspi.STATE_ENABLED),
                }
            )
        if "Document" in atspi.listInterfaces(node) and len(documents) < 2:
            location = {"showing": state.contains(atspi.STATE_SHOWING)}
            for item in node.queryDocument().getAttributes():
                if ":" not in item:
                    continue
                key, value = item.split(":", 1)
                if key.lower() not in {"uri", "url", "docurl"}:
                    continue
                uri = urlsplit(value)
                # Classify the workspace location without exporting its query,
                # fragment, hostname, credentials, filesystem path or entry text.
                first = uri.path.strip("/").split("/", 1)[0]
                location.update(
                    scheme=uri.scheme
                    if uri.scheme in {"http", "https", "tauri"}
                    else "other",
                    surface=first
                    if first
                    in {"", "index.html", "workbench", "projects", "settings", "work"}
                    else "other",
                )
            documents.append(location)
    return {
        "expected_project_visible": expected_seen,
        "controls": controls,
        "documents": documents,
    }


def select_project(app, project_name, atspi):
    try:
        control = wait(
            lambda: (
                named(app, "Open navigation", atspi)
                or named(app, "Switch project", atspi)
            )
        )
        if control.name == "Open navigation":
            activate(control)
        activate(wait(lambda: named(app, "Switch project", atspi)))
        activate(wait(lambda: named(app, project_name, atspi, prefix=True)))
    except TimeoutError as error:
        try:
            observed = project_selection_failure_state(app, project_name, atspi)
            detail = json.dumps(observed, separators=(",", ":"))
            while len(detail) > 1_000 and observed["controls"]:
                # Escaping can expand a structurally bounded snapshot. Preserve
                # the critical boolean within the driver's 1,500-character clip.
                observed["controls"].pop(0)
                observed["details_truncated"] = True
                detail = json.dumps(observed, separators=(",", ":"))
            if len(detail) > 1_000:
                present = "true" if observed["expected_project_visible"] else "false"
                detail = (
                    '{"details_truncated":true,"expected_project_visible":'
                    + present
                    + "}"
                )
        except Exception as capture_error:
            # diagnostic-expected: a failed diagnostic must preserve the original
            # selection failure and never export an arbitrary exception body.
            error_type = "".join(
                character
                for character in type(capture_error).__name__[:60]
                if character.isascii() and (character.isalnum() or character == "_")
            ) or "Error"
            detail = '{"capture_error_type":"' + error_type + '"}'
        raise RuntimeError(
            "native project selection did not match: " + detail
        ) from error


def composer(app, atspi):
    choices = []
    for node in descendants(app):
        state = node.getState()
        if (
            visible(node, atspi)
            and state.contains(atspi.STATE_EDITABLE)
            and state.contains(atspi.STATE_MULTI_LINE)
            and node.name not in {"Ask Butler a question", "Question for Nebula"}
        ):
            choices.append(node)
    return choices[0] if len(choices) == 1 else None


def observe(app, atspi):
    texts, uris = [], []
    for node in descendants(app):
        if not visible(node, atspi):
            continue
        interfaces = atspi.listInterfaces(node)
        if "Text" in interfaces:
            text = node.queryText().getText(0, -1)
            if text:
                texts.append(text[:32768])
        if "Document" in interfaces:
            document = node.queryDocument()
            for item in document.getAttributes():
                if ":" in item:
                    key, value = item.split(":", 1)
                    if key.lower() in {"uri", "url", "docurl"}:
                        uris.append(value)
    field = composer(app, atspi)
    return {
        "visible_text": "\n".join(texts)[:500000],
        "document_uris": uris,
        "draft": field.queryText().getText(0, -1) if field else None,
        "composer_attributes": attributes(field) if field else None,
    }


def run(request):
    pid, expected = request["pid"], request["process_birth"]
    if birth(pid) != expected:
        raise RuntimeError("owned native process incarnation changed")
    x11 = X11()
    try:
        windows = wait(lambda: x11.windows(pid))
        if len(windows) != 1:
            raise RuntimeError("native main window is ambiguous")
        window = windows[0]
        result = {
            "pid": pid,
            "process_birth": expected,
            "window_id": window,
            "operation": request["operation"],
            "window_observed": True,
        }
        if request["operation"] == "close":
            if birth(pid) != expected:
                raise RuntimeError("native process changed before close")
            x11.close_window(window)
            wait(lambda: not x11.windows(pid), timeout=30)
            return {**result, "close_event": "WM_DELETE_WINDOW", "window_closed": True}
        if request["operation"] == "ready":
            return result
        import pyatspi

        app = wait(lambda: application(pid, pyatspi))
        if request["operation"] in {"select", "select_and_write"}:
            select_project(app, request["project_name"], pyatspi)
            workbench = named(app, "Workbench", pyatspi)
            if workbench:
                activate(workbench)
            show = named(app, "Show conversations", pyatspi)
            if show:
                activate(show)
            activate(
                wait(
                    lambda: named(app, request["session_title"], pyatspi, contains=True)
                )
            )
        if request["operation"] == "select_and_write":
            field = wait(lambda: composer(app, pyatspi))
            if not field.queryEditableText().setTextContents(request["draft"]):
                raise RuntimeError("native composer rejected accessible text input")

        def confirmed():
            # A wrong initial route can have no composer. Keep observing that
            # document so failure evidence includes its URI and visible state.
            value = observe(app, pyatspi)
            same_session = any(
                parse_qs(urlsplit(uri).query).get("session") == [request["session_id"]]
                for uri in value["document_uris"]
            )
            return (
                value
                if same_session
                and value["draft"] == request["draft"]
                and request["prompt"] in value["visible_text"]
                and request["expected_text"] in value["visible_text"]
                else None
            )

        try:
            observed = wait(confirmed)
        except TimeoutError:
            raise RuntimeError(
                "native chat/draft observation did not match: "
                + json.dumps(observe(app, pyatspi))
            ) from None
        return {
            **result,
            **observed,
            "observed_session_id": request["session_id"],
            "input_method": "AT-SPI",
            "draft_sha256": hashlib.sha256(observed["draft"].encode()).hexdigest(),
        }
    finally:
        x11.lib.XCloseDisplay(x11.display)


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin))))
