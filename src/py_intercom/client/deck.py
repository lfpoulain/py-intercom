"""The Stream Deck contract shared with orchestream-v2 (``/api/deck``).

py-livedeck speaks one generic protocol to every app of the ecosystem:

* ``GET /api/deck/state`` — the whole state, plus ``commands`` and ``keys``;
* ``GET /api/deck/events`` — Server-Sent Events: ``state`` once, then
  ``patch`` (``changed`` sections, modified ``keys``, ``removedKeys``);
* ``GET /api/deck/commands`` — what can be run, with argument values;
* ``POST /api/deck/commands/<id>`` with ``{"args": {...}}`` (or an empty
  body) — answers ``{"ok", "message", "data"}``.

The client describes every key, ready to draw, so the deck has nothing
hard-coded. Everything here is pure (the window's status payload in, dicts
out): commands become :class:`RemoteCommand` and run on the Qt thread through
the same path as the keyboard, so every PTT safety rule still applies.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from http import HTTPStatus

from ..common.buses import FIXED_BUSES
from .remote import RemoteCommand, RemoteReply

# Bus colours as deck tones (see buses.FIXED_BUSES).
_BUS_TONES = {"regie": "blue", "plateau": "amber", "vmix": "violet"}
_STATE_TONES = {
    "online": "success",
    "connecting": "warning",
    "reconnecting": "warning",
    "degraded": "danger",
    "disconnected": "neutral",
}
NOT_ONLINE = "Le poste n'est pas en ligne."
ALWAYS_ON = "Bus en mode Permanent : changez son mode dans le poste."

COMMANDS: list[dict] = [
    {"id": "ptt.toggle", "label": "Basculer la parole", "argNames": ["bus"], "action": "toggle"},
    {"id": "ptt.on", "label": "Parler", "argNames": ["bus"], "action": "on"},
    {"id": "ptt.off", "label": "Arrêter de parler", "argNames": ["bus"], "action": "off"},
    {"id": "ptt.allOff", "label": "Tout couper", "argNames": [], "action": "off"},
]
_BY_ID = {c["id"]: c for c in COMMANDS}


class DeckError(Exception):
    """An answer the deck can show as is: HTTP status, machine code, French message."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.details = details

    def body(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _key(
    key_id: str,
    group: str,
    command: str | None,
    args: dict,
    title: str,
    subtitle: str | None,
    active: bool,
    tone: str,
    reason: str | None = None,
) -> dict:
    return {
        "id": key_id,
        "group": group,
        "command": command,
        "args": args,
        "title": title,
        "subtitle": subtitle or None,
        "active": bool(active),
        "tone": tone,
        "available": reason is None,
        "reason": reason,
    }


def _buses(status: dict) -> list[dict]:
    """The buses of the status, in their fixed order, with name and mode."""
    raw = status.get("buses") or {}
    out = []
    for definition in FIXED_BUSES:
        bus = raw.get(definition.key)
        if bus is None:
            continue
        out.append(
            {
                "id": definition.key,
                "name": definition.name,
                "mode": str(bus.get("mode", "ptt")),
                "active": bool(bus.get("active")),
            }
        )
    return out


def _bus_reason(bus: dict, online: bool) -> str | None:
    if not online:
        return NOT_ONLINE
    if bus["mode"] == "always_on":
        return ALWAYS_ON
    return None


def _bus_subtitle(bus: dict, online: bool) -> str:
    if not online:
        return "Hors ligne"
    if bus["active"]:
        return "Permanent" if bus["mode"] == "always_on" else "Tu parles"
    return "Micro fermé" if bus["mode"] == "toggle" else "Prêt"


def build_keys(status: dict) -> list[dict]:
    """Every key the deck can place, in display order."""
    online = bool(status.get("online"))
    state = str(status.get("operational", "disconnected"))
    keys = [
        _key(
            "client.status",
            "État",
            None,
            {},
            "Intercom",
            status.get("state"),
            online,
            _STATE_TONES.get(state, "neutral"),
        ),
    ]
    for bus in _buses(status):
        keys.append(
            _key(
                f"ptt.toggle:{bus['id']}",
                "Parole",
                "ptt.toggle",
                {"bus": bus["id"]},
                bus["name"],
                _bus_subtitle(bus, online),
                bus["active"],
                "live" if bus["active"] else _BUS_TONES.get(bus["id"], "neutral"),
                _bus_reason(bus, online),
            )
        )
    keys.append(_key("ptt.allOff", "Parole", "ptt.allOff", {}, "Tout couper", None, False, "danger"))
    return keys


def _command_reason(command: dict, status: dict) -> str | None:
    if command["action"] != "off" and not status.get("online"):
        return NOT_ONLINE
    return None


def build_commands(status: dict, with_values: bool = False) -> list[dict]:
    """Commands with their availability; with ``with_values``, the allowed argument values."""
    out = []
    for command in COMMANDS:
        item = {"id": command["id"], "label": command["label"], "argNames": list(command["argNames"])}
        reason = _command_reason(command, status)
        item["available"], item["reason"] = reason is None, reason
        if with_values:
            item["args"] = [
                {
                    "name": "bus",
                    "type": "enum",
                    "values": [{"value": b["id"], "label": b["name"]} for b in _buses(status)],
                }
                for _ in command["argNames"]
            ]
        out.append(item)
    return out


def build_state(status: dict, token_set: bool) -> dict:
    """``GET /api/deck/state``: the status sections, plus commands and keys."""
    online = bool(status.get("online"))
    return {
        "token": {"name": "jeton API" if token_set else "accès libre", "permissions": []},
        "client": {
            "state": str(status.get("operational", "disconnected")),
            "label": status.get("state"),
            "online": online,
            "detail": status.get("detail") or None,
        },
        "buses": copy.deepcopy(_buses(status)),  # a snapshot: the next patch compares against it
        "commands": build_commands(status),
        "keys": build_keys(status),
        "updatedAt": _now(),
    }


def build_patch(old: dict, new: dict) -> dict | None:
    """What changed between two states, as an SSE ``patch`` (None if nothing did)."""
    changed = {k: v for k, v in new.items() if k not in ("keys", "updatedAt") and old.get(k) != v}
    old_keys = {k["id"]: k for k in old.get("keys", [])}
    new_keys = {k["id"]: k for k in new.get("keys", [])}
    keys = [k for kid, k in new_keys.items() if old_keys.get(kid) != k]
    removed = [kid for kid in old_keys if kid not in new_keys]
    if not (changed or keys or removed):
        return None
    return {"changed": changed, "keys": keys, "removedKeys": removed, "updatedAt": new.get("updatedAt") or _now()}


# -- running a command ------------------------------------------------------


def _invalid(arg: str, message: str) -> DeckError:
    return DeckError(422, "VALIDATION_FAILED", message, {f"args.{arg}": message})


def resolve_command(command_id: str, args: object) -> tuple[RemoteCommand, str]:
    """The PTT command behind ``POST /api/deck/commands/<id>``, and its label."""
    command = _BY_ID.get(command_id)
    if command is None:
        raise DeckError(404, "COMMAND_NOT_FOUND", f"commande « {command_id} » inconnue")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise _invalid("args", "« args » doit être un objet")
    if not command["argNames"]:
        return RemoteCommand(command["action"], None), command["label"]
    value = args.get("bus")
    if value is None or str(value).strip() == "":
        raise _invalid("bus", "argument « bus » manquant")
    token = str(value).strip().lower()
    for definition in FIXED_BUSES:
        if token in (definition.key, str(definition.bus_id)):
            return RemoteCommand(command["action"], int(definition.bus_id)), f"{command['label']} ({definition.name})"
    known = ", ".join(d.key for d in FIXED_BUSES)
    # An unknown bus is a bad argument, never a 404: py-livedeck reads that
    # as "this server has no /api/deck".
    raise _invalid("bus", f"bus « {value} » inconnu (attendus : {known})")


def command_result(label: str, reply: RemoteReply) -> dict:
    """``{"ok", "message", "data"}`` from the window's reply, or the matching DeckError."""
    if reply.status == HTTPStatus.OK:
        return {"ok": True, "message": f"{label} : fait", "data": reply.body}
    message = str((reply.body or {}).get("error") or "commande refusée")
    if reply.status == HTTPStatus.CONFLICT:
        raise DeckError(409, "COMMAND_REFUSED", message[:1].upper() + message[1:])
    if reply.status == HTTPStatus.SERVICE_UNAVAILABLE:
        raise DeckError(503, "BUSY", "Interface occupée, réessayez.")
    raise DeckError(int(reply.status), "INTERNAL", message)
