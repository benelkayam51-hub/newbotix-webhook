"""
Newbotix — KEENON Elevator Webhook Server
Receives elevator events from KEENON and bridges to Schindler API
"""

from flask import Flask, request, jsonify
import requests
import json
import time
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── KEENON Credentials ──────────────────────
KEENON_BASE_URL      = "https://es.robotkeenon.com"
KEENON_CLIENT_ID     = "9sdb0sPeSH5MJOHe"
KEENON_CLIENT_SECRET = "lDZBqDKlXO3GEnGE"
KEENON_STORE_ID      = "C00002423"

# ── Schindler Credentials (fill after Oz sends) ──
SCHINDLER_BASE_URL        = "https://api.schindler.com"
SCHINDLER_SUBSCRIPTION_KEY = os.environ.get("SCHINDLER_KEY", "")
SCHINDLER_JWT             = ""   # refreshed automatically

# ── Token cache ──────────────────────────────
_keenon_token = None
_keenon_token_expires = 0

# Active calls: taskNo → schindler callId
active_calls = {}


def keenon_get_token():
    global _keenon_token, _keenon_token_expires
    if _keenon_token and time.time() < _keenon_token_expires - 60:
        return _keenon_token
    url = f"{KEENON_BASE_URL}/api/open/oauth/token"
    res = requests.post(url, data={
        "client_id":     KEENON_CLIENT_ID,
        "client_secret": KEENON_CLIENT_SECRET,
        "grant_type":    "client_credentials"
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})
    res.raise_for_status()
    body = res.json()
    _keenon_token = body["access_token"]
    _keenon_token_expires = time.time() + body.get("expires_in", 7200)
    logger.info("✅ KEENON token refreshed")
    return _keenon_token


def keenon_send(action, task_no, data=None):
    token = keenon_get_token()
    payload = {
        "action":    action,
        "taskNo":    task_no,
        "messageId": int(time.time() * 1000),
    }
    if data:
        payload["data"] = data
    res = requests.post(
        f"{KEENON_BASE_URL}/api/open/data/v2/elevator-to-device",
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=10
    )
    logger.info(f"→ KEENON {action}: {res.status_code}")
    return res.json()


def schindler_headers():
    return {
        "Authorization": f"Bearer {SCHINDLER_JWT}",
        "Ocp-Apim-Subscription-Key": SCHINDLER_SUBSCRIPTION_KEY,
        "Content-Type": "application/json"
    }


def schindler_evaluate(from_floor, to_floor):
    res = requests.post(
        f"{SCHINDLER_BASE_URL}/lift/v1/calls",
        headers=schindler_headers(),
        json={"requestType": "Evaluate", "startFloor": from_floor, "endFloor": to_floor},
        timeout=10
    )
    logger.info(f"Schindler Evaluate: {res.status_code}")
    return res.json()


def schindler_request(from_floor, to_floor):
    res = requests.post(
        f"{SCHINDLER_BASE_URL}/lift/v1/calls",
        headers=schindler_headers(),
        json={"requestType": "Request", "startFloor": from_floor, "endFloor": to_floor},
        timeout=10
    )
    res.raise_for_status()
    call_id = res.json()["callId"]
    logger.info(f"Schindler Request: callId={call_id}")
    return call_id


def schindler_confirm_boarding(call_id):
    requests.patch(
        f"{SCHINDLER_BASE_URL}/lift/v1/calls/{call_id}",
        headers=schindler_headers(),
        json={"callStatus": "InElevator"},
        timeout=10
    )
    logger.info(f"Schindler Boarding confirmed: {call_id}")


def schindler_confirm_exit(call_id):
    requests.patch(
        f"{SCHINDLER_BASE_URL}/lift/v1/calls/{call_id}",
        headers=schindler_headers(),
        json={"callStatus": "Done"},
        timeout=10
    )
    logger.info(f"Schindler Exit confirmed: {call_id}")


def schindler_cancel(call_id):
    requests.delete(
        f"{SCHINDLER_BASE_URL}/lift/v1/calls/{call_id}",
        headers=schindler_headers(),
        timeout=10
    )
    logger.info(f"Schindler Cancelled: {call_id}")


# ── Main webhook endpoint ────────────────────
@app.route("/elevator-callback", methods=["POST"])
def elevator_callback():
    """KEENON sends elevator events here."""
    payload = request.get_json(force=True)
    logger.info(f"📥 Received: {json.dumps(payload, indent=2)}")

    data     = payload.get("data", {})
    action   = data.get("action", "")
    task_no  = str(data.get("taskNo", int(time.time())))
    inner    = data.get("data", {})

    try:
        if action == "QueryElevatorGroup":
            keenon_send("RespondElevatorGroup", task_no, {
                "dstId":          inner.get("dstId", 1),
                "sourceFloorNum": inner.get("sourceFloorNum", 1),
                "targetFloorNum": inner.get("targetFloorNum", 2)
            })

        elif action == "RequestElevatorTask":
            source = str(inner.get("sourceFloorNum", "L"))
            target = str(inner.get("targetFloorNum", "1"))

            schindler_evaluate(source, target)
            call_id = schindler_request(source, target)
            active_calls[task_no] = call_id

            keenon_send("RespondElevatorTask", task_no, {"result": 1})
            keenon_send("ReturnSchedulingResult", task_no, {
                "result": 1,
                "elevatorId": 1,
                "callId": call_id
            })

        elif action == "ArriveWaitPoint":
            keenon_send("ArriveWaitPointAck", task_no)
            # Notify robot door is open
            keenon_send("RespondOpenDoor", task_no, {
                "result": 0,
                "hardTime": 30,
                "floor": inner.get("sourceFloorNum", 1)
            })

        elif action == "RespondOpenDoorAck":
            logger.info(f"Robot acknowledged door open for task {task_no}")

        elif action == "RobotMotionStatus":
            call_id = active_calls.get(task_no) or inner.get("callId")
            in_elevator = inner.get("inElevator", False)
            if call_id:
                if in_elevator:
                    schindler_confirm_boarding(call_id)
                else:
                    schindler_confirm_exit(call_id)
                    active_calls.pop(task_no, None)

        elif action == "TerminateTask":
            call_id = active_calls.pop(task_no, None) or inner.get("callId")
            if call_id:
                schindler_cancel(call_id)

        elif action == "ElevatorStatus":
            keenon_send("ElevatorStatusAck", task_no)

        else:
            logger.warning(f"Unhandled action: {action}")

    except Exception as e:
        logger.error(f"❌ Error handling {action}: {e}")
        return jsonify({"code": 500, "msg": str(e)}), 500

    return jsonify({"code": 610000, "msg": "success"})


# ── Health check ─────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "running",
        "service": "Newbotix Elevator Webhook",
        "active_calls": len(active_calls)
    })


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "keenon_store": KEENON_STORE_ID,
        "active_calls": active_calls,
        "schindler_connected": bool(SCHINDLER_JWT)
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
