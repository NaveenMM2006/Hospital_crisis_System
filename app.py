from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room
import anthropic
import json
import os
import uuid
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "hcs-secret-2025")
socketio = SocketIO(app, cors_allowed_origins="*")

# ─── In-memory store ───
guests = {}
emergencies = {}

ZONES = {
    "A": {"name": "Building A — Main Tower",   "lat": 48.8584, "lng": 2.2945, "entrance": "West Wing Service Road", "exit": "Exit A (West Ground Floor)", "color": "#3B82F6"},
    "B": {"name": "Building B — Garden Wing",  "lat": 48.8590, "lng": 2.2960, "entrance": "Main Hotel Driveway",     "exit": "Main Exit (South Lobby)",    "color": "#10B981"},
    "C": {"name": "Building C — Beach Resort", "lat": 48.8575, "lng": 2.2970, "entrance": "Beach Access Road",       "exit": "Exit C (East Beachfront)",   "color": "#F59E0B"},
}

FIRE_STATION = {"lat": 48.8620, "lng": 2.2900, "name": "Central Fire Station"}

MOCK_GUESTS = [
    {"name": "Maria Schmidt", "room": "101", "zone": "A", "lang": "German"},
    {"name": "James Okoro",   "room": "102", "zone": "A", "lang": "English"},
    {"name": "Yuki Tanaka",   "room": "201", "zone": "B", "lang": "Japanese"},
    {"name": "Sophie Dupont", "room": "202", "zone": "B", "lang": "French"},
    {"name": "Carlos Rivera", "room": "301", "zone": "C", "lang": "Spanish"},
    {"name": "Aisha Patel",   "room": "302", "zone": "C", "lang": "English"},
    {"name": "Lena Fischer",  "room": "303", "zone": "C", "lang": "German"},
    {"name": "Marco Rossi",   "room": "103", "zone": "A", "lang": "English"},
]

def init_mock():
    for g in MOCK_GUESTS:
        gid = str(uuid.uuid4())
        guests[gid] = {**g, "id": gid, "status": "safe", "checkin_time": datetime.now().isoformat()}

init_mock()

# ─── Pages ───
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/checkin")
def checkin():
    return render_template("checkin.html")

@app.route("/admin")
def admin():
    return render_template("admin.html")

@app.route("/responder")
def responder():
    return render_template("responder.html")

# ─── REST API ───
@app.route("/api/guests", methods=["GET"])
def get_guests():
    return jsonify(list(guests.values()))

@app.route("/api/guests", methods=["POST"])
def add_guest():
    data = request.json
    gid = str(uuid.uuid4())
    guest = {
        "id": gid,
        "name": data["name"],
        "room": data["room"],
        "zone": data["zone"],
        "lang": data.get("lang", "English"),
        "status": "safe",
        "checkin_time": datetime.now().isoformat(),
    }
    guests[gid] = guest
    socketio.emit("guest_update", {"guests": list(guests.values())})
    return jsonify(guest), 201

@app.route("/api/zones", methods=["GET"])
def get_zones():
    zone_data = {}
    for z, info in ZONES.items():
        zone_data[z] = {
            **info,
            "count": sum(1 for g in guests.values() if g["zone"] == z),
            "guests": [g for g in guests.values() if g["zone"] == z],
            "alert": z in emergencies,
            "emergency": emergencies.get(z),
        }
    return jsonify(zone_data)


# ─── AGENT 1: Assessment — Claude DECIDES the response, code executes it ───
def assess_emergency(zone, etype):
    zones_ctx = {z: {"name": i["name"], "exit": i["exit"],
                     "count": sum(1 for g in guests.values() if g["zone"] == z)}
                 for z, i in ZONES.items()}
    prompt = f"""You are an emergency assessment agent for a hotel resort.
Decide the response. Emergency: {etype} originating in zone {zone}.
Zones (with guest counts and normal exits): {json.dumps(zones_ctx)}

Decide which zones are at risk (a fire/gas leak may threaten ADJACENT zones, not just the origin),
the severity, and the safest exit per affected zone (avoid routing through the origin zone).
Return ONLY JSON, no prose:
{{"affected_zones": ["A"], "severity": "high|medium|low",
  "zone_exits": {{"A": "Exit A (West Ground Floor)"}},
  "reasoning": "one short sentence on why"}}"""
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        msg = client.messages.create(model="claude-opus-4-5", max_tokens=400,
                                      messages=[{"role": "user", "content": prompt}])
        text = msg.content[0].text.strip().replace("```json", "").replace("```", "")
        return json.loads(text)
    except Exception:
        return {"affected_zones": [zone], "severity": "high",
                "zone_exits": {zone: ZONES[zone]["exit"]},
                "reasoning": "Fallback: origin zone only."}


# ─── AGENT 2: Generative broadcast — per guest, in their language ───
def generate_ai_instructions(zone, etype, lang, affected, exit_override=None):
    exit_route = exit_override or ZONES[zone]["exit"]
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        zone_info = ZONES[zone]
        guest_summary = f"{len(affected)} guests" if affected else "guests"
        prompt = f"""You are an emergency AI for a luxury hotel resort.

Emergency: {etype} in {zone_info['name']}
Time: {datetime.now().strftime('%H:%M')}
Affected: {guest_summary}
Exit route: {exit_route}

Write calm, numbered evacuation instructions in {lang}. Include:
1. Immediate action
2. Do NOT use elevators
3. Specific exit: {exit_route}
4. Assembly point: Main hotel parking area
5. Await staff

Max 100 words. Be calm and authoritative."""
        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text
    except Exception:
        fallbacks = {
            "English": f"EMERGENCY ALERT — {etype} in {ZONES[zone]['name']}.\n\n1. Stay calm and evacuate immediately.\n2. Do NOT use elevators — use stairwells only.\n3. Proceed to {exit_route}.\n4. Go to the main hotel parking area as assembly point.\n5. Await instructions from emergency staff.\n\nStay calm. Help is on the way.",
            "German":  f"NOTFALL — {etype} in {ZONES[zone]['name']}.\n\n1. Ruhig bleiben und sofort evakuieren.\n2. Aufzüge NICHT benutzen — nur Treppenhäuser.\n3. Zum {exit_route} gehen.\n4. Sammelplatz: Hauptparkplatz des Hotels.\n5. Anweisungen des Personals abwarten.",
            "French":  f"ALERTE — {etype} dans {ZONES[zone]['name']}.\n\n1. Restez calme et évacuez immédiatement.\n2. N'utilisez PAS les ascenseurs.\n3. Dirigez-vous vers {exit_route}.\n4. Point de rassemblement: parking principal.\n5. Attendez les instructions du personnel.",
            "Spanish": f"ALERTA — {etype} en {ZONES[zone]['name']}.\n\n1. Mantenga la calma y evacúe de inmediato.\n2. NO use los ascensores.\n3. Diríjase a {exit_route}.\n4. Punto de encuentro: estacionamiento principal.\n5. Espere instrucciones del personal.",
            "Japanese": f"緊急警報 — {ZONES[zone]['name']}で{etype}発生。\n\n1. 落ち着いて直ちに避難してください。\n2. エレベーターは使用しないでください。\n3. {exit_route}へ移動してください。\n4. 集合場所：ホテルメインパーキング。\n5. スタッフの指示を待ってください。",
        }
        return fallbacks.get(lang, fallbacks["English"])


# ─── Emergency trigger: perceive → decide (Agent 1) → act (Agent 2) ───
@app.route("/api/emergency", methods=["POST"])
def trigger_emergency():
    data = request.json
    zone, etype = data["zone"], data["type"]
    lang = data.get("lang", "English")

    decision = assess_emergency(zone, etype)                       # DECIDE
    socketio.emit("agent_log", {"agent": "Assessment",
        "msg": f"Severity {decision['severity']}; zones {decision['affected_zones']}. {decision['reasoning']}"})

    affected = [g for g in guests.values() if g["zone"] in decision["affected_zones"]]   # ACT
    for g in affected:
        g_exit = decision["zone_exits"].get(g["zone"], ZONES[g["zone"]]["exit"])
        g["instructions"] = generate_ai_instructions(g["zone"], etype, g["lang"], affected, g_exit)
        g["status"] = "alerted"
        g["alerted_at"] = datetime.now().isoformat()
        socketio.emit("agent_log", {"agent": "Broadcast",
            "msg": f"Alerted {g['name']} (Room {g['room']}, {g['lang']}) → {g_exit}"})

    z = ZONES[zone]
    emergency = {
        "zone": zone, "type": etype, "lang": lang,
        "time": datetime.now().isoformat(),
        "severity": decision["severity"],
        "affected_zones": decision["affected_zones"],
        "affected_count": len(affected),
        "affected_guests": affected,
        "zone_info": z,
        "fire_station": FIRE_STATION,
        "reasoning": decision["reasoning"],
        "maps_url": (
            f"https://www.google.com/maps/dir/?api=1"
            f"&origin={FIRE_STATION['lat']},{FIRE_STATION['lng']}"
            f"&destination={z['lat']},{z['lng']}"
            f"&travelmode=driving"
        ),
    }
    for zz in decision["affected_zones"]:
        emergencies[zz] = emergency

    socketio.emit("emergency_triggered", emergency)
    return jsonify(emergency)

@app.route("/api/emergency/<zone>", methods=["DELETE"])
def clear_emergency(zone):
    emergencies.pop(zone, None)
    for g in guests.values():
        if g.get("status") in ("alerted", "at_risk"):
            g["status"] = "safe"
    socketio.emit("emergency_cleared", {"zone": zone})
    return jsonify({"status": "cleared"})

@app.route("/api/emergencies", methods=["GET"])
def get_emergencies():
    return jsonify(emergencies)

@app.route("/api/ai-instructions", methods=["POST"])
def ai_instructions():
    data = request.json
    result = generate_ai_instructions(
        data["zone"], data["type"], data["lang"],
        [g for g in guests.values() if g["zone"] == data["zone"]]
    )
    return jsonify({"instructions": result})


# ─── AGENT 3: Tracking / re-alert loop ───
@app.route("/api/ack/<gid>", methods=["POST"])
def ack(gid):
    if gid in guests:
        guests[gid]["status"] = "safe"
        socketio.emit("agent_log", {"agent": "Tracking",
            "msg": f"{guests[gid]['name']} confirmed SAFE"})
    return jsonify({"ok": True})

def tracking_agent():
    while True:
        socketio.sleep(15)
        for g in guests.values():
            if g.get("status") == "alerted":            # PERCEIVE: not yet safe
                g["status"] = "at_risk"                  # DECIDE
                socketio.emit("agent_log", {"agent": "Tracking",
                    "msg": f"⚠ {g['name']} (Room {g['room']}) not responding — re-alerting + flagging responders"})
                socketio.emit("at_risk", {"guest": g})   # ACT


if __name__ == "__main__":
    socketio.start_background_task(tracking_agent)
    socketio.run(app, debug=True, port=5050)