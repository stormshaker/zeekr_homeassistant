"""Send an RCS charge command the way the stock app does, and report the car's answer.

Local patch for stormshaker/home-assistant-config#1289, not upstream yet.

Two things the stock app does that zeekr_ev_api 0.1.15 does not (from borconi/openzeekr,
built from Frida captures of the app):

1. It marks itself as the account's online device with POST
   ms-app-online-manager/api/v1.0/app/hb right before each command. openzeekr found the
   car only executes cloud commands for the online device.
2. It confirms a limit change by re-reading getLatestSoc about 15 s later. It never
   calls queryProcessResult, which answers 079001 to this client. The library's
   do_remote_control returns only the gateway's `success`, which means "queued", not
   "done".

The outcome is fired as the `zeekr_ev_command_result` event and returned, so
automations can wait for it and the entity can show it.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from zeekr_ev_api import const as api_const
from zeekr_ev_api import network

_LOGGER = logging.getLogger(__name__)

EVENT_COMMAND_RESULT = "zeekr_ev_command_result"

HEARTBEAT_PATH = "ms-app-online-manager/api/v1.0/app/hb"


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


def send_with_heartbeat(client, vin: str, command: str, setting: dict) -> dict[str, Any]:
    """Heartbeat, then send. Quick; runs in an executor thread inside the service call."""
    hb = heartbeat(client, vin)
    sent_ts = time.time()
    sent = send_charge_control(client, vin, command, setting)
    return {
        "heartbeat_code": hb.get("code"),
        "heartbeat_variant": hb.get("variant"),
        "accepted": bool(sent.get("success")),
        "send_code": sent.get("code"),
        "send_msg": sent.get("msg"),
        "session_id": (sent.get("data") or {}).get("sessionId"),
        "sent_ts": sent_ts,
        "confirmed": None,
        "reported_value": None,
        "reported_at": None,
        "checks": 0,
    }


def _soc_time(text: str | None) -> float | None:
    """getLatestSoc's socTime is UTC, as "YYYY-MM-DD HH:MM:SS"."""
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def confirm_charging_limit(
    client, vin: str, outcome: dict[str, Any], value: float, delays: tuple[float, ...] = (15.0, 15.0)
) -> dict[str, Any]:
    """Confirm a limit change the way the official app does: re-read getLatestSoc.

    The app never asks queryProcessResult (it answers 079001 to this client anyway). It
    re-reads the limit about 15 s after the command. Confirmed means the reported limit
    matches the command AND its socTime is from after the send, so an old value that
    happens to match cannot pass for a new one. At most len(delays) reads.
    """
    if not outcome.get("accepted"):
        outcome["confirmed"] = False
        return outcome
    want = int(round(value * 10))
    for delay in delays:
        time.sleep(delay)
        outcome["checks"] += 1
        try:
            data = client.get_vehicle_charging_limit(vin) or {}
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Zeekr getLatestSoc confirm read failed: %s", err)
            continue
        soc, soc_text = data.get("soc"), data.get("socTime")
        at = _soc_time(soc_text)
        outcome["reported_value"] = int(soc) / 10 if str(soc).isdigit() else soc
        outcome["reported_at"] = soc_text
        fresh = at is not None and at >= outcome["sent_ts"] - 5
        _LOGGER.info(
            "Zeekr limit confirm %d: want %s, getLatestSoc %s at %s (%s)",
            outcome["checks"], want, soc, soc_text, "fresh" if fresh else "stale",
        )
        if str(soc) == str(want) and fresh:
            outcome["confirmed"] = True
            return outcome
    outcome["confirmed"] = False
    return outcome
