"""The /api/deck contract shared with orchestream-v2 and py-livedeck."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus

import pytest

from py_intercom.client import deck, remote
from py_intercom.client.remote import RemoteCommand, RemoteControlServer, RemoteReply
from py_intercom.common.buses import PLATEAU_BUS_ID, REGIE_BUS_ID, VMIX_BUS_ID

KEY_FIELDS = {"id", "group", "command", "args", "title", "subtitle", "active", "tone", "available", "reason"}


def _status(*, online: bool = True, regie: bool = False, modes: dict | None = None) -> dict:
    modes = {"regie": "ptt", "plateau": "toggle", "vmix": "always_on", **(modes or {})}
    return {
        "state": "En ligne" if online else "Déconnecté",
        "operational": "online" if online else "disconnected",
        "detail": "",
        "online": online,
        "buses": {
            "regie": {"mode": modes["regie"], "active": regie},
            "plateau": {"mode": modes["plateau"], "active": False},
            "vmix": {"mode": modes["vmix"], "active": online and modes["vmix"] == "always_on"},
        },
    }


# --- State and keys ----------------------------------------------------------------


def test_state_has_sections_commands_keys_and_token() -> None:
    state = deck.build_state(_status(), token_set=False)

    assert state["token"] == {"name": "accès libre", "permissions": []}
    assert state["client"] == {"state": "online", "label": "En ligne", "online": True, "detail": None}
    assert [b["id"] for b in state["buses"]] == ["regie", "plateau", "vmix"]
    assert {c["id"] for c in state["commands"]} == {"ptt.toggle", "ptt.on", "ptt.off", "ptt.allOff"}
    assert all(set(c) == {"id", "label", "argNames", "available", "reason"} for c in state["commands"])
    assert state["updatedAt"].endswith("Z")


def test_keys_have_exact_fields_and_stable_ids() -> None:
    keys = deck.build_keys(_status())

    assert [k["id"] for k in keys] == [
        "client.status",
        "ptt.toggle:regie",
        "ptt.toggle:plateau",
        "ptt.toggle:vmix",
        "ptt.allOff",
    ]
    assert all(set(k) == KEY_FIELDS for k in keys)
    status_key = keys[0]
    assert status_key["command"] is None and status_key["group"] == "État" and status_key["tone"] == "success"
    regie = keys[1]
    assert regie["command"] == "ptt.toggle" and regie["args"] == {"bus": "regie"}
    assert regie["title"] == "Régie" and regie["tone"] == "blue" and regie["available"] is True


def test_a_talking_bus_is_live() -> None:
    regie = deck.build_keys(_status(regie=True))[1]

    assert regie["active"] is True and regie["tone"] == "live" and regie["subtitle"] == "Tu parles"


def test_keys_are_greyed_with_a_reason() -> None:
    online = {k["id"]: k for k in deck.build_keys(_status())}
    assert online["ptt.toggle:vmix"]["available"] is False
    assert online["ptt.toggle:vmix"]["reason"] == deck.ALWAYS_ON

    offline = {k["id"]: k for k in deck.build_keys(_status(online=False))}
    assert offline["ptt.toggle:regie"]["reason"] == deck.NOT_ONLINE
    assert offline["ptt.allOff"]["available"] is True  # cutting is always possible
    commands = {c["id"]: c for c in deck.build_commands(_status(online=False))}
    assert commands["ptt.on"]["available"] is False and commands["ptt.off"]["available"] is True


def test_commands_list_bus_values() -> None:
    commands = {c["id"]: c for c in deck.build_commands(_status(), with_values=True)}

    assert commands["ptt.toggle"]["args"] == [
        {
            "name": "bus",
            "type": "enum",
            "values": [
                {"value": "regie", "label": "Régie"},
                {"value": "plateau", "label": "Plateau"},
                {"value": "vmix", "label": "VMix"},
            ],
        }
    ]
    assert commands["ptt.allOff"]["args"] == []


# --- Patches -----------------------------------------------------------------------


def test_patch_is_none_when_nothing_changes() -> None:
    assert deck.build_patch(deck.build_state(_status(), False), deck.build_state(_status(), False)) is None


def test_patch_carries_changed_sections_and_keys() -> None:
    old = deck.build_state(_status(), False)
    new = deck.build_state(_status(regie=True), False)

    patch = deck.build_patch(old, new)

    assert set(patch["changed"]) == {"buses"}
    assert [k["id"] for k in patch["keys"]] == ["ptt.toggle:regie"]
    assert patch["removedKeys"] == []


# --- Commands ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command_id", "args", "expected"),
    [
        ("ptt.toggle", {"bus": "regie"}, RemoteCommand("toggle", REGIE_BUS_ID)),
        ("ptt.on", {"bus": "PLATEAU"}, RemoteCommand("on", PLATEAU_BUS_ID)),
        ("ptt.off", {"bus": "2"}, RemoteCommand("off", VMIX_BUS_ID)),
        ("ptt.allOff", None, RemoteCommand("off", None)),
    ],
)
def test_commands_map_to_ptt(command_id, args, expected) -> None:
    assert deck.resolve_command(command_id, args)[0] == expected


@pytest.mark.parametrize(
    ("command_id", "args", "status", "code"),
    [
        ("nope", {}, 404, "COMMAND_NOT_FOUND"),
        ("ptt.toggle", {}, 422, "VALIDATION_FAILED"),
        ("ptt.toggle", {"bus": "cuisine"}, 422, "VALIDATION_FAILED"),  # never a bare NOT_FOUND
        ("ptt.toggle", "regie", 422, "VALIDATION_FAILED"),
    ],
)
def test_command_errors(command_id, args, status, code) -> None:
    with pytest.raises(deck.DeckError) as info:
        deck.resolve_command(command_id, args)
    assert (info.value.status, info.value.code) == (status, code)
    if code == "VALIDATION_FAILED":
        assert next(iter(info.value.body()["error"]["details"])).startswith("args.")


def test_a_refused_command_is_a_409_with_its_reason() -> None:
    with pytest.raises(deck.DeckError) as info:
        deck.command_result("Parler", RemoteReply(HTTPStatus.CONFLICT, {"error": "PTT désarmé : pas en ligne"}))
    assert info.value.status == 409 and info.value.message == "PTT désarmé : pas en ligne"


# --- Over HTTP ---------------------------------------------------------------------


class _Window:
    """Stands in for the Qt window: a status, and the PTT commands it received."""

    def __init__(self) -> None:
        self.status = _status()
        self.commands: list[RemoteCommand] = []

    def __call__(self, command: RemoteCommand) -> RemoteReply:
        if command.action == "status":
            return RemoteReply(HTTPStatus.OK, json.loads(json.dumps(self.status)))
        self.commands.append(command)
        if command.bus_id == REGIE_BUS_ID:
            active = self.status["buses"]["regie"]["active"]
            self.status["buses"]["regie"]["active"] = (
                (not active) if command.action == "toggle" else command.action == "on"
            )
        return RemoteReply(HTTPStatus.OK, self.status)


@pytest.fixture
def deck_server(request):
    token = getattr(request, "param", "")
    window = _Window()
    server = RemoteControlServer(window, port=0, token=token)
    server.start()
    yield f"http://127.0.0.1:{server.port}", window, server
    server.stop()


def _call(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    request = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_http_state_commands_and_run(deck_server) -> None:
    base, window, _ = deck_server

    status, state = _call(f"{base}/api/deck/state")
    assert status == 200 and len(state["keys"]) == 5

    status, listing = _call(f"{base}/api/deck/commands")
    assert status == 200 and listing["commands"][0]["args"][0]["type"] == "enum"

    status, answer = _call(f"{base}/api/deck/commands/ptt.toggle", "POST", {"args": {"bus": "regie"}})
    assert status == 200 and answer["ok"] is True and answer["message"] == "Basculer la parole (Régie) : fait"
    assert window.commands == [RemoteCommand("toggle", REGIE_BUS_ID)]

    status, answer = _call(f"{base}/api/deck/commands/ptt.allOff", "POST")  # empty body
    assert status == 200 and window.commands[-1] == RemoteCommand("off", None)


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/api/deck/nope", 404, "ROUTE_NOT_FOUND"),
        ("POST", "/api/deck/state", 405, "METHOD_NOT_ALLOWED"),
        ("GET", "/api/deck/commands/ptt.toggle", 405, "METHOD_NOT_ALLOWED"),
        ("POST", "/api/deck/commands/nope", 404, "COMMAND_NOT_FOUND"),
        ("POST", "/api/deck/commands/ptt.on", 422, "VALIDATION_FAILED"),
    ],
)
def test_http_errors_are_structured(deck_server, method, path, status, code) -> None:
    base, window, _ = deck_server

    got, body = _call(f"{base}{path}", method)

    assert (got, body["error"]["code"]) == (status, code)
    assert body["error"]["message"]
    assert window.commands == []


def test_unreadable_json_is_422(deck_server) -> None:
    base, _, _ = deck_server
    request = urllib.request.Request(f"{base}/api/deck/commands/ptt.allOff", method="POST", data=b"{nope")
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=5)
    assert info.value.code == 422


@pytest.mark.parametrize("deck_server", ["s3cret"], indirect=True)
def test_token_protects_every_route(deck_server) -> None:
    base, window, _ = deck_server

    status, body = _call(f"{base}/api/deck/state")
    assert status == 401 and body["error"]["code"] == "INVALID_TOKEN"
    assert _call(f"{base}/api/deck/state", headers={"Authorization": "Bearer nope"})[0] == 401
    assert window.commands == []

    status, state = _call(f"{base}/api/deck/state", headers={"Authorization": "Bearer s3cret"})
    assert status == 200 and state["token"]["name"] == "jeton API"
    assert _call(f"{base}/api/deck/state?token=s3cret")[0] == 401  # never read from the URL


@pytest.mark.parametrize("path", ["/ptt/regie/on", "/status", "/"])
def test_old_routes_are_gone(deck_server, path) -> None:
    base, window, _ = deck_server

    for method in ("GET", "POST"):
        status, body = _call(f"{base}{path}", method)
        assert (status, body["error"]["code"]) == (404, "ROUTE_NOT_FOUND")
    assert window.commands == []


def test_browser_requests_are_refused(deck_server) -> None:
    base, window, _ = deck_server

    status, body = _call(f"{base}/api/deck/commands/ptt.allOff", "POST", headers={"Origin": "https://evil.example"})

    assert status == 403 and body["error"]["code"] == "FORBIDDEN" and window.commands == []


def _read_event(stream) -> tuple[str, dict | None]:
    """The next event of an SSE stream (a comment comes back as ``(": ...", None)``)."""
    name, data = "", None
    while True:
        line = stream.readline().decode("utf-8")
        if line == "":
            raise EOFError
        line = line.rstrip("\n")
        if line == "":
            if name or data is not None:
                return name, data
            continue
        if line.startswith(":"):
            stream.readline()
            return line, None
        field, _, value = line.partition(": ")
        if field == "event":
            name = value
        elif field == "data":
            data = json.loads(value)


def test_sse_sends_state_then_a_patch_after_a_command(deck_server, monkeypatch) -> None:
    monkeypatch.setattr(remote, "DECK_POLL_S", 30.0)  # only the command can wake the stream
    base, window, _ = deck_server
    port = int(base.rsplit(":", 1)[1])

    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"GET /api/deck/events HTTP/1.1\r\nHost: x\r\n\r\n")
        stream = sock.makefile("rb")
        assert stream.readline().startswith(b"HTTP/1.0 200")
        headers = []
        while (line := stream.readline()) not in (b"\r\n", b""):
            headers.append(line.decode().lower())
        assert any(h.startswith("content-type: text/event-stream") for h in headers)

        name, state = _read_event(stream)
        assert name == "state" and state["keys"][1]["active"] is False

        started = time.monotonic()
        _call(f"{base}/api/deck/commands/ptt.toggle", "POST", {"args": {"bus": "regie"}})
        name, patch = _read_event(stream)
        assert name == "patch" and time.monotonic() - started < 2.0
        assert [k["id"] for k in patch["keys"]] == ["ptt.toggle:regie"]
        assert patch["keys"][0]["active"] is True and patch["changed"]["buses"][0]["active"] is True


def test_sse_sends_keepalive_when_idle(deck_server, monkeypatch) -> None:
    monkeypatch.setattr(remote, "DECK_POLL_S", 0.05)
    monkeypatch.setattr(remote, "DECK_KEEPALIVE_S", 0.1)
    base, _, _ = deck_server
    with urllib.request.urlopen(f"{base}/api/deck/events", timeout=5) as response:
        assert _read_event(response)[0] == "state"
        assert _read_event(response)[0] == ": keepalive"


def test_sse_streams_are_limited(deck_server, monkeypatch) -> None:
    monkeypatch.setattr(remote, "DECK_MAX_STREAMS", 1)
    base, _, _ = deck_server
    with urllib.request.urlopen(f"{base}/api/deck/events", timeout=5) as first:
        assert _read_event(first)[0] == "state"
        status, body = _call(f"{base}/api/deck/events")
        assert status == 429 and body["error"]["code"] == "TOO_MANY_STREAMS"


def test_stop_ends_open_streams(deck_server, monkeypatch) -> None:
    monkeypatch.setattr(remote, "DECK_POLL_S", 30.0)
    base, _, server = deck_server
    response = urllib.request.urlopen(f"{base}/api/deck/events", timeout=5)
    assert _read_event(response)[0] == "state"

    stopper = threading.Thread(target=server.stop)
    stopper.start()
    stopper.join(timeout=5)

    assert not stopper.is_alive()
    with pytest.raises(EOFError):
        _read_event(response)
    response.close()
