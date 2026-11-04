"""Send an RCS charge command the way the stock app does, and report the car's answer.

Local patch for stormshaker/home-assistant-config#1289, not upstream yet.

Two things the stock app does that zeekr_ev_api 0.1.15 does not (from borconi/openzeekr,
built from Frida captures of the app):

1. It marks itself as the account's online device with POST
   ms-app-online-manager/api/v1.0/app/hb right before each command. openzeekr found the
   car only executes cloud commands for the online device.
2. It keeps the sessionId the command returns, and polls
   ms-remote-control/api/v1.0/remoteControl/queryProcessResult for the outcome. The
   library's do_remote_control returns only the gateway's `success`, which means
   "queued", not "done".

The outcome is fired as the `zeekr_ev_command_result` event and returned, so
automations can wait for it and the entity can show it.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from zeekr_ev_api import const as api_const
from zeekr_ev_api import network

_LOGGER = logging.getLogger(__name__)

EVENT_COMMAND_RESULT = "zeekr_ev_command_result"

HEARTBEAT_PATH = "ms-app-online-manager/api/v1.0/app/hb"
RESULT_PATHS = (
    "ms-remote-control/api/v1.0/remoteControl/queryProcessResult",
    "ms-remote-control/v1.0/remoteControl/queryProcessResult",
)


def _base(client) -> str:
    return (client.region_login_server or "").rstrip("/") + "/"


# Header and body variants tried in order until one answers 000000. From a capture of the
# official iOS app (SEA, v1.6.8, 4 Nov 2026): it sends X-VIN on app/hb, X-PROJECT-ID "ZEEKR"
# (the library sends "ZEEKR_SEA"), and hbType 4. Once a variant works it is remembered.
HB_VARIANTS = (
    ("vin", {}, 3),
    ("vin+project", {"X-PROJECT-ID": "ZEEKR"}, 3),
    ("vin+project+hbType4", {"X-PROJECT-ID": "ZEEKR"}, 4),
)
_hb_working: int | None = None


def heartbeat(client, vin: str | None = None) -> dict[str, Any]:
    """Mark this client as the account's online device. Best effort."""
    global _hb_working
    order = [_hb_working] if _hb_working is not None else range(len(HB_VARIANTS))
    resp: dict[str, Any] = {}
    for i in order:
        name, extra, hb_type = HB_VARIANTS[i]
        body = {
            "deviceId": client.logged_in_headers.get("x-device-id", ""),
            "deviceType": 1,
            "hbType": hb_type,
            "ts": int(time.time() * 1000),
        }
        headers = dict(extra)
        if vin:
            headers["X-VIN"] = client._get_encrypted_vin(vin)
        try:
            resp = network.appSignedPost(
                client,
                _base(client) + HEARTBEAT_PATH,
                json.dumps(body, separators=(",", ":")),
                extra_headers=headers,
            )
        except Exception as err:  # noqa: BLE001 - best effort, never block the command
            _LOGGER.warning("Zeekr app/hb (%s) failed: %s", name, err)
            continue
        _LOGGER.info("Zeekr app/hb (%s): code=%s msg=%s data=%s", name, resp.get("code"), resp.get("msg"), resp.get("data"))
        if resp.get("code") == "000000":
            _hb_working = i
            resp["variant"] = name
            return resp
    _hb_working = None
    return resp


def send_charge_control(client, vin: str, command: str, setting: dict) -> dict[str, Any]:
    """POST charge/control and return the whole response block (with sessionId)."""
    body = {"command": command, "serviceId": "RCS", "setting": setting}
    resp = network.appSignedPost(
        client,
        _base(client) + api_const.CHARGE_CONTROL_URL,
        json.dumps(body, separators=(",", ":")),
        extra_headers={"X-VIN": client._get_encrypted_vin(vin)},
    )
    _LOGGER.info(
        "Zeekr charge/control %s: code=%s msg=%s data=%s",
        command,
        resp.get("code"),
        resp.get("msg"),
        resp.get("data"),
    )
    return resp


def query_result(client, vin: str, session_id: str) -> dict[str, Any] | None:
    """One poll of the command's outcome. Tries the documented path, then the v1.0 form."""
    for extra in ({}, {"X-PROJECT-ID": "ZEEKR"}):
        resp = _query_result_once(client, vin, session_id, extra)
        if resp is not None and resp.get("code") != "079001":
            return resp
    return resp


def _query_result_once(client, vin: str, session_id: str, extra: dict) -> dict[str, Any] | None:
    headers = {"X-VIN": client._get_encrypted_vin(vin), **extra}
    for path in RESULT_PATHS:
        try:
            resp = network.appSignedGet(
                client, f"{_base(client)}{path}?sessionId={session_id}", headers=headers
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Zeekr queryProcessResult %s failed: %s", path, err)
            continue
        _LOGGER.info(
            "Zeekr queryProcessResult (%s %s): code=%s msg=%s data=%s",
            path,
            extra or "",
            resp.get("code"),
            resp.get("msg"),
            resp.get("data"),
        )
        if resp.get("code") not in ("00A01", "000010"):  # not "no such route"
            return resp
    return None


def _finished(resp: dict[str, Any] | None) -> bool:
    """True once the result looks final. The shape is not documented, so be generous."""
    if not resp:
        return False
    data = resp.get("data")
    if not resp.get("success") or not data:
        return True  # an error answer is final
    text = json.dumps(data).lower()
    return not any(w in text for w in ('"pending"', '"processing"', '"running"', '"sending"'))


def send_with_heartbeat(client, vin: str, command: str, setting: dict) -> dict[str, Any]:
    """Heartbeat, then send. Quick; runs in an executor thread inside the service call."""
    hb = heartbeat(client, vin)
    sent = send_charge_control(client, vin, command, setting)
    return {
        "heartbeat_code": hb.get("code"),
        "heartbeat_variant": hb.get("variant"),
        "accepted": bool(sent.get("success")),
        "send_code": sent.get("code"),
        "send_msg": sent.get("msg"),
        "session_id": (sent.get("data") or {}).get("sessionId"),
        "result": None,
        "result_code": None,
        "result_msg": None,
        "polls": 0,
    }


def poll_outcome(client, vin: str, outcome: dict[str, Any], polls: int = 6, gap: float = 5.0) -> dict[str, Any]:
    """Poll for the car's answer. Slow; runs in an executor thread after the call returns."""
    session_id = outcome.get("session_id")
    if not session_id:
        return outcome
    for i in range(polls):
        time.sleep(gap)
        resp = query_result(client, vin, session_id)
        outcome["polls"] = i + 1
        if resp is None:
            break
        outcome["result"] = resp.get("data")
        outcome["result_code"] = resp.get("code")
        outcome["result_msg"] = resp.get("msg")
        if _finished(resp):
            break
    return outcome
