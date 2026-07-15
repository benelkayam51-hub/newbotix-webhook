"""
Newbotix — KEENON + Schindler On-Site Robot API
Webhook Server v3.0
Based on: RobotOpenAPISpecification_onsite.yaml v1.1.0
Server: https://<port-gateway-address>/robots/v1
"""

from flask import Flask, request, jsonify
import requests, json, time, os, logging, tempfile, ssl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# ── KEENON Credentials ──────────────────────
KEENON_BASE_URL      = "https://es.robotkeenon.com"
KEENON_CLIENT_ID     = "9sdb0sPeSH5MJOHe"
KEENON_CLIENT_SECRET = "lDZBqDKlXO3GEnGE"
KEENON_STORE_ID      = "C00002423"

# ── Schindler On-Site API ───────────────────
# URL = https://<port-gateway-address>/robots/v1
SCHINDLER_GATEWAY    = os.environ.get("SCHINDLER_GATEWAY", "https://sandbox.schindler.com")
SCHINDLER_BASE_URL   = f"{SCHINDLER_GATEWAY}/robots/v1"

# Certificate from env vars (Railway Variables)
SCHINDLER_CERT_PEM   = os.environ.get("SCHINDLER_CERT", "")
SCHINDLER_KEY_PEM    = os.environ.get("SCHINDLER_PRIVATE_KEY", "")
SCHINDLER_CA_PEM     = os.environ.get("SCHINDLER_CA", "")

# Robot identity in Schindler PORT system
ROBOT_IDENTITY_TYPE  = "email"
ROBOT_IDENTITY_ID    = "colab@schindler.com"

# Equipment number (elevator ID) — set per building
EQUIPMENT_NUMBER     = os.environ.get("SCHINDLER_EQUIPMENT_NUMBER", "")

# ── Token cache ──────────────────────────────
_keenon_token     = None
_keenon_token_exp = 0
active_calls      = {}  # taskNo → callId


def write_temp(content, suffix):
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix=suffix, delete=False)
    tmp.write(content)
    tmp.close()
    return tmp.name


def get_cert_files():
    """Write cert and key to temp files, return (cert_path, key_path, ca_path)."""
    cert_path = write_temp(SCHINDLER_CERT_PEM, ".pem") if SCHINDLER_CERT_PEM else None
    key_path  = write_temp(SCHINDLER_KEY_PEM,  ".key") if SCHINDLER_KEY_PEM  else None
    ca_path   = write_temp(SCHINDLER_CA_PEM,   ".crt") if SCHINDLER_CA_PEM   else None
    return cert_path, key_path, ca_path


def schindler_request(method, path, **kwargs):
    """Make authenticated request to Schindler On-Site Robot API using mutual TLS."""
    cert_path, key_path, ca_path = get_cert_files()
    try:
        url = f"{SCHINDLER_BASE_URL}{path}"
        response = requests.request(
            method, url,
            cert=(cert_path, key_path) if cert_path and key_path else None,
            verify=ca_path if ca_path else True,
            timeout=15,
            **kwargs
        )
        logger.info(f"Schindler {method} {path}: {response.status_code}")
        return response
    finally:
        for p in [cert_path, key_path, ca_path]:
            if p:
                try: os.unlink(p)
                except: pass


def schindler_evaluate(equipment_number, entry_floor, exit_floor):
    """Check elevator availability without booking."""
    payload = {
        "equipmentNumber": equipment_number,
        "requestType": "Evaluate",
        "entry": {"floorNumber": entry_floor, "entranceSide": "None"},
        "exit":  {"floorNumber": exit_floor,  "entranceSide": "None"},
        "identity": {"type": ROBOT_IDENTITY_TYPE, "textId": ROBOT_IDENTITY_ID}
    }
    res = schindler_request("POST", "/calls", json=payload)
    return res.json()


def schindler_request_call(equipment_number, entry_floor, exit_floor):
    """Book elevator — returns callId."""
    payload = {
        "equipmentNumber": equipment_number,
        "requestType": "Request",
        "entry": {"floorNumber": entry_floor, "entranceSide": "None"},
        "exit":  {"floorNumber": exit_floor,  "entranceSide": "None"},
        "identity": {"type": ROBOT_IDENTITY_TYPE, "textId": ROBOT_IDENTITY_ID}
    }
    res = schindler_request("POST", "/calls", json=payload)
    data = res.json()
    call_id = data.get("callId")
    call_status = data.get("callStatus")
    logger.info(f"Schindler call: callId={call_id} status={call_status}")
    return call_id, call_status


def schindler_enter(call_id):
    """Robot confirms it entered the elevator. callerState=Enter"""
    res = schindler_request("PATCH", f"/calls/{call_id}", params={"callerState": "Enter"})
    logger.info(f"Schindler Enter confirmed: {res.status_code}")
    return res.json()


def schindler_exit(call_id):
    """Robot confirms it exited the elevator. callerState=Exit"""
    res = schindler_request("PATCH", f"/calls/{call_id}", params={"callerState": "Exit"})
    logger.info(f"Schindler Exit confirmed: {res.status_code}")
    return res.json()


def schindler_cancel(call_id):
    """Cancel active call."""
    res = schindler_request("DELETE", f"/calls/{call_id}")
    logger.info(f"Schindler Cancel: {res.status_code}")


def schindler_get_floors(equipment_number):
    """Get floor details for an elevator."""
    res = schindler_request("GET", f"/elevators/{equipment_number}/floors")
    return res.json()


# ── KEENON ──────────────────────────────────
def keenon_get_token():
    global _keenon_token, _keenon_token_exp
    if _keenon_token and time.time() < _keenon_token_exp - 60:
        return _keenon_token
    res = requests.post(f"{KEENON_BASE_URL}/api/open/oauth/token", data={
        "client_id":     KEENON_CLIENT_ID,
        "client_secret": KEENON_CLIENT_SECRET,
        "grant_type":    "client_credentials"
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
    res.raise_for_status()
    body = res.json()
    _keenon_token = body["access_token"]
    _keenon_token_exp = time.time() + body.get("expires_in", 7200)
    logger.info("KEENON token refreshed")
    return _keenon_token


def keenon_send(action, task_no, data=None):
    token = keenon_get_token()
    payload = {"action": action, "taskNo": task_no, "messageId": int(time.time() * 1000)}
    if data:
        payload["data"] = data
    res = requests.post(
        f"{KEENON_BASE_URL}/api/open/data/v2/elevator-to-device",
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json"},
        json=payload, timeout=10
    )
    logger.info(f"KEENON {action}: {res.status_code}")
    return res.json()


# ── Webhook ──────────────────────────────────
@app.route("/elevator-callback", methods=["POST"])
def callback():
    payload  = request.get_json(force=True)
    logger.info(f"Received: {json.dumps(payload)}")
    data     = payload.get("data", {})
    action   = data.get("action", "")
    task_no  = str(data.get("taskNo", int(time.time())))
    inner    = data.get("data", {})

    # Use equipment number from env or from robot request
    eq_num = EQUIPMENT_NUMBER or inner.get("equipmentNumber", "")

    try:
        if action == "QueryElevatorGroup":
            keenon_send("RespondElevatorGroup", task_no, {
                "dstId":          inner.get("dstId", 1),
                "sourceFloorNum": inner.get("sourceFloorNum", 1),
                "targetFloorNum": inner.get("targetFloorNum", 2)
            })

        elif action == "RequestElevatorTask":
            entry_floor = inner.get("sourceFloorNum", 1)
            exit_floor  = inner.get("targetFloorNum", 2)

            # Evaluate first
            eval_result = schindler_evaluate(eq_num, entry_floor, exit_floor)
            logger.info(f"Evaluate: {eval_result.get('callStatus')}")

            # Request call
            call_id, status = schindler_request_call(eq_num, entry_floor, exit_floor)
            if call_id:
                active_calls[task_no] = call_id

            keenon_send("RespondElevatorTask", task_no, {"result": 1})
            keenon_send("ReturnSchedulingResult", task_no, {
                "result": 1, "elevatorId": 1, "callId": call_id
            })

        elif action == "ArriveWaitPoint":
            keenon_send("ArriveWaitPointAck", task_no)
            keenon_send("RespondOpenDoor", task_no, {
                "result": 0, "hardTime": 30,
                "floor": inner.get("sourceFloorNum", 1)
            })

        elif action == "RespondOpenDoorAck":
            logger.info(f"Door ack for task {task_no}")

        elif action == "RobotMotionStatus":
            call_id = active_calls.get(task_no)
            if call_id:
                if inner.get("inElevator"):
                    schindler_enter(call_id)
                else:
                    schindler_exit(call_id)
                    active_calls.pop(task_no, None)

        elif action == "TerminateTask":
            call_id = active_calls.pop(task_no, None)
            if call_id:
                schindler_cancel(call_id)

        elif action == "ElevatorStatus":
            keenon_send("ElevatorStatusAck", task_no)

    except Exception as e:
        logger.error(f"Error in {action}: {e}")
        return jsonify({"code": 500, "msg": str(e)}), 500

    return jsonify({"code": 610000, "msg": "success"})


# ── Health & Test ────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status":         "running",
        "service":        "Newbotix Elevator Webhook v3",
        "active_calls":   len(active_calls),
        "gateway":        SCHINDLER_GATEWAY,
        "cert_loaded":    bool(SCHINDLER_CERT_PEM),
        "equipment":      EQUIPMENT_NUMBER or "not set"
    })


@app.route("/test-connection", methods=["GET"])
def test_connection():
    """Test direct connection to Schindler Gateway."""
    try:
        if not EQUIPMENT_NUMBER:
            return jsonify({"status": "⚠️  Set SCHINDLER_EQUIPMENT_NUMBER first"}), 400

        floors = schindler_get_floors(EQUIPMENT_NUMBER)
        return jsonify({
            "status": "✅ Connected to Schindler Gateway!",
            "floors": floors
        })
    except Exception as e:
        return jsonify({"status": f"❌ Error: {str(e)}"}), 500


@app.route("/test-evaluate", methods=["GET"])
def test_evaluate():
    """Test elevator availability."""
    try:
        if not EQUIPMENT_NUMBER:
            return jsonify({"status": "⚠️  Set SCHINDLER_EQUIPMENT_NUMBER first"}), 400

        entry = int(request.args.get("entry", 1))
        exit  = int(request.args.get("exit", 2))
        result = schindler_evaluate(EQUIPMENT_NUMBER, entry, exit)
        return jsonify({"status": "✅ Evaluate OK", "result": result})
    except Exception as e:
        return jsonify({"status": f"❌ Error: {str(e)}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
