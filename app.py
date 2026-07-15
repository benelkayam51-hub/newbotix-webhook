"""
Newbotix — KEENON + Schindler Elevator Webhook Server
Reads credentials from Railway environment variables
"""

from flask import Flask, request, jsonify
import requests, json, time, os, logging, tempfile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# ── KEENON Credentials ──────────────────────
KEENON_BASE_URL      = "https://es.robotkeenon.com"
KEENON_CLIENT_ID     = "9sdb0sPeSH5MJOHe"
KEENON_CLIENT_SECRET = "lDZBqDKlXO3GEnGE"
KEENON_STORE_ID      = "C00002423"

# ── Schindler Credentials (from Railway env vars) ──
SCHINDLER_BASE_URL        = "https://api.schindler.com"
SCHINDLER_SUBSCRIPTION_KEY = os.environ.get("SCHINDLER_KEY", "")
SCHINDLER_CERT_CONTENT    = os.environ.get("SCHINDLER_CERT", "")
SCHINDLER_KEY_CONTENT     = os.environ.get("SCHINDLER_PRIVATE_KEY", "")
SCHINDLER_CA_CONTENT      = os.environ.get("SCHINDLER_CA", "")

# ── Token cache ──────────────────────────────
_keenon_token     = None
_keenon_token_exp = 0
_schindler_jwt    = None
_schindler_jwt_exp = 0

# Active calls: taskNo → schindler callId
active_calls = {}


def write_temp_file(content, suffix):
    """Write string content to a temp file, return path."""
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix=suffix, delete=False)
    tmp.write(content)
    tmp.close()
    return tmp.name


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
    logger.info("✅ KEENON token refreshed")
    return _keenon_token


def schindler_get_jwt():
    """Authenticate with Schindler using cert from env vars."""
    global _schindler_jwt, _schindler_jwt_exp

    if _schindler_jwt and time.time() < _schindler_jwt_exp - 60:
        return _schindler_jwt

    if not SCHINDLER_CERT_CONTENT or not SCHINDLER_KEY_CONTENT:
        logger.warning("⚠️  Schindler credentials not set in env vars")
        return None

    # Write cert and key to temp files
    cert_path = write_temp_file(SCHINDLER_CERT_CONTENT, ".pem")
    key_path  = write_temp_file(SCHINDLER_KEY_CONTENT,  ".key")

    try:
        res = requests.post(
            f"{SCHINDLER_BASE_URL}/deviceidentities/v1/token",
            headers={
                "Ocp-Apim-Subscription-Key": SCHINDLER_SUBSCRIPTION_KEY,
                "X-ARR-ClientCert": SCHINDLER_CERT_CONTENT,
                "User-Agent": "RobotAuth/1.0"
            },
            cert=(cert_path, key_path),
            timeout=15
        )
        res.raise_for_status()
        body = res.json()

        # Decrypt JWE token
        from jose import jwe
        jwt = jwe.decrypt(body["accessToken"], SCHINDLER_KEY_CONTENT).decode()
        _schindler_jwt = jwt
        _schindler_jwt_exp = body.get("accessExpiresIn", time.time() + 3600)
        logger.info("✅ Schindler JWT obtained")
        return jwt

    except Exception as e:
        logger.error(f"❌ Schindler auth failed: {e}")
        return None
    finally:
        os.unlink(cert_path)
        os.unlink(key_path)


def schindler_headers():
    jwt = schindler_get_jwt()
    return {
        "Authorization": f"Bearer {jwt}" if jwt else "",
        "Ocp-Apim-Subscription-Key": SCHINDLER_SUBSCRIPTION_KEY,
        "Content-Type": "application/json"
    }


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
        json=payload, timeout=10
    )
    logger.info(f"→ KEENON {action}: {res.status_code}")
    return res.json()


# ── Main webhook endpoint ────────────────────
@app.route("/elevator-callback", methods=["POST"])
def callback():
    payload  = request.get_json(force=True)
    logger.info(f"📥 Received: {json.dumps(payload)}")
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

            # Evaluate with Schindler
            requests.post(f"{SCHINDLER_BASE_URL}/lift/v1/calls",
                headers=schindler_headers(),
                json={"requestType": "Evaluate", "startFloor": source, "endFloor": target},
                timeout=10)

            # Request trip
            res = requests.post(f"{SCHINDLER_BASE_URL}/lift/v1/calls",
                headers=schindler_headers(),
                json={"requestType": "Request", "startFloor": source, "endFloor": target},
                timeout=10)
            call_id = res.json().get("callId", f"mock-{task_no}")
            active_calls[task_no] = call_id

            keenon_send("RespondElevatorTask",    task_no, {"result": 1})
            keenon_send("ReturnSchedulingResult", task_no, {"result": 1, "elevatorId": 1})

        elif action == "ArriveWaitPoint":
            keenon_send("ArriveWaitPointAck", task_no)
            keenon_send("RespondOpenDoor", task_no, {
                "result": 0, "hardTime": 30,
                "floor": inner.get("sourceFloorNum", 1)
            })

        elif action == "RespondOpenDoorAck":
            logger.info(f"Door ack received for task {task_no}")

        elif action == "RobotMotionStatus":
            call_id = active_calls.get(task_no)
            if call_id:
                if inner.get("inElevator"):
                    requests.patch(f"{SCHINDLER_BASE_URL}/lift/v1/calls/{call_id}",
                        headers=schindler_headers(),
                        json={"callStatus": "InElevator"}, timeout=10)
                    logger.info(f"✅ Boarding confirmed: {call_id}")
                else:
                    requests.patch(f"{SCHINDLER_BASE_URL}/lift/v1/calls/{call_id}",
                        headers=schindler_headers(),
                        json={"callStatus": "Done"}, timeout=10)
                    active_calls.pop(task_no, None)
                    logger.info(f"✅ Exit confirmed: {call_id}")

        elif action == "TerminateTask":
            call_id = active_calls.pop(task_no, None)
            if call_id:
                requests.delete(f"{SCHINDLER_BASE_URL}/lift/v1/calls/{call_id}",
                    headers=schindler_headers(), timeout=10)

        elif action == "ElevatorStatus":
            keenon_send("ElevatorStatusAck", task_no)

        else:
            logger.warning(f"Unhandled action: {action}")

    except Exception as e:
        logger.error(f"❌ Error in {action}: {e}")
        return jsonify({"code": 500, "msg": str(e)}), 500

    return jsonify({"code": 610000, "msg": "success"})


# ── Health & Status ──────────────────────────
@app.route("/", methods=["GET"])
def health():
    schindler_ready = bool(SCHINDLER_CERT_CONTENT and SCHINDLER_KEY_CONTENT)
    return jsonify({
        "status":          "running",
        "service":         "Newbotix Elevator Webhook",
        "active_calls":    len(active_calls),
        "schindler_ready": schindler_ready,
        "keenon_store":    KEENON_STORE_ID
    })


@app.route("/test-schindler", methods=["GET"])
def test_schindler():
    """Test Schindler authentication."""
    jwt = schindler_get_jwt()
    if jwt:
        return jsonify({"status": "✅ Schindler auth OK", "jwt_preview": jwt[:40] + "..."})
    return jsonify({"status": "❌ Schindler auth failed — check env vars"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
