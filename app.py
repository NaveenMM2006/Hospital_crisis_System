from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room
import anthropic
import json
import os
import uuid
from datetime import datetime
import threading
import time

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "hcs-secret-2025")
socketio = SocketIO(app, cors_allowed_origins="*")

# ─── In-memory store (replace with Firebase Firestore in prod) ───────────────
guests = {}
emergencies = {}
safe_guests = {}        # { emergency_zone: [guest_id, ...] }
incident_reports = []   # stores post-incident summaries

ZONES = {
    "A": {
        "name": "Building A — Main Tower",
        "lat": 48.8584, "lng": 2.2945,
        "entrance": "West Wing Service Road",
        "exit": "Exit A (West Ground Floor)",
        "color": "#3B82F6",
        "floor": "1-15",
        "risk_notes": "High-rise floors above 8 are far from ground exit. West Wing Service Road is the only vehicle access.",
    },
    "B": {
        "name": "Building B — Garden Wing",
        "lat": 48.8590, "lng": 2.2960,
        "entrance": "Main Hotel Driveway",
        "exit": "Main Exit (South Lobby)",
        "color": "#10B981",
        "floor": "1-5",
        "risk_notes": "Low-rise, easy evacuation. South Lobby exit is wide. Garden paths may be slippery in rain.",
    },
    "C": {
        "name": "Building C — Beach Resort",
        "lat": 48.8575, "lng": 2.2970,
        "entrance": "Beach Access Road",
        "exit": "Exit C (East Beachfront)",
        "color": "#F59E0B",
        "floor": "1-3",
        "risk_notes": "Beachfront zone — flood-prone during high tide or storms. East Beachfront exit may be blocked during coastal emergencies.",
    },
}

FIRE_STATION = {"lat": 48.8620, "lng": 2.2900, "name": "Central Fire Station"}

MOCK_GUESTS = [
    {"name": "Maria Schmidt",  "room": "101", "zone": "A", "lang": "German"},
    {"name": "James Okoro",    "room": "102", "zone": "A", "lang": "English"},
    {"name": "Yuki Tanaka",    "room": "201", "zone": "B", "lang": "Japanese"},
    {"name": "Sophie Dupont",  "room": "202", "zone": "B", "lang": "French"},
    {"name": "Carlos Rivera",  "room": "301", "zone": "C", "lang": "Spanish"},
    {"name": "Aisha Patel",    "room": "302", "zone": "C", "lang": "English"},
    {"name": "Lena Fischer",   "room": "303", "zone": "C", "lang": "German"},
    {"name": "Marco Rossi",    "room": "103", "zone": "A", "lang": "English"},
]

def init_mock():
    for g in MOCK_GUESTS:
        gid = str(uuid.uuid4())
        guests[gid] = {**g, "id": gid, "checkin_time": datetime.now().isoformat()}

init_mock()


# ════════════════════════════════════════════════════════════
# AGENT 1 — CHECK-IN RISK AGENT
# Runs inside add_guest() after guest is saved.
# Looks at zone risk profile + room number → returns a short
# risk note shown to the receptionist.
# ════════════════════════════════════════════════════════════
def checkin_risk_agent(guest, zone_info):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        prompt = f"""You are a hotel safety AI. A guest just checked in.

Guest: {guest['name']}, Room {guest['room']}, Zone: {zone_info['name']}
Zone risk profile: {zone_info['risk_notes']}
Floor range in zone: {zone_info.get('floor', 'unknown')}

Write ONE short sentence (max 15 words) flagging any safety consideration 
the receptionist should know. If no risk, reply: "No special risk noted."
Do not greet. Just the note."""

        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=60,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception:
        return zone_info.get("risk_notes", "No special risk noted.")


# ════════════════════════════════════════════════════════════
# AGENT 2 — EXIT ROUTE AGENT
# Called via POST /api/route (guest-facing).
# Takes room number + active blocked zones →
# returns clearest walking route to nearest safe exit.
# ════════════════════════════════════════════════════════════
def exit_route_agent(room, zone, blocked_zones):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        zone_info = ZONES[zone]

        blocked_exits = [
            ZONES[z]["exit"] for z in blocked_zones if z in ZONES
        ]
        blocked_text = (
            f"These exits are currently BLOCKED: {', '.join(blocked_exits)}."
            if blocked_exits else "No exits are currently blocked."
        )

        prompt = f"""You are a hotel emergency navigation AI.

Guest is in Room {room}, located in {zone_info['name']}.
Primary exit for this zone: {zone_info['exit']}
{blocked_text}
All zone exits available:
- Zone A: Exit A (West Ground Floor)
- Zone B: Main Exit (South Lobby)
- Zone C: Exit C (East Beachfront)
Assembly point: Main hotel parking area (front of building).

Give calm, clear step-by-step walking directions from Room {room} to the 
nearest SAFE exit. Use simple language. Max 5 steps. No elevator use.
Do not mention blocked exits as options."""

        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception:
        zone_info = ZONES.get(zone, {})
        return (
            f"Go to the nearest stairwell and descend to ground floor. "
            f"Proceed to {zone_info.get('exit', 'the nearest exit')}. "
            f"Assemble at the main hotel parking area. Do not use elevators."
        )


# ════════════════════════════════════════════════════════════
# AGENT 3 — HEADCOUNT AGENT
# Background thread that runs every 60 seconds during an
# active emergency. Compares safe_guests list against all
# affected guests → pushes unaccounted list via SocketIO
# to the /responder page.
# ════════════════════════════════════════════════════════════
def headcount_agent_loop():
    while True:
        time.sleep(60)
        if not emergencies:
            continue

        for zone, emergency in list(emergencies.items()):
            affected = emergency.get("affected_guests", [])
            confirmed_safe = safe_guests.get(zone, [])

            unaccounted = [
                g for g in affected
                if g["id"] not in confirmed_safe
            ]

            if unaccounted:
                summary = {
                    "zone": zone,
                    "zone_name": ZONES[zone]["name"],
                    "total_affected": len(affected),
                    "safe_count": len(confirmed_safe),
                    "unaccounted_count": len(unaccounted),
                    "unaccounted": [
                        {"name": g["name"], "room": g["room"]}
                        for g in unaccounted
                    ],
                    "timestamp": datetime.now().isoformat(),
                }
                socketio.emit("headcount_update", summary)

# Start headcount background thread
headcount_thread = threading.Thread(target=headcount_agent_loop, daemon=True)
headcount_thread.start()


# ─── Pages ───────────────────────────────────────────────────────────────────
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


# ─── REST API ─────────────────────────────────────────────────────────────────
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
        "checkin_time": datetime.now().isoformat(),
    }
    guests[gid] = guest

    # ── AGENT 1: Check-in Risk Agent ──────────────────────────
    zone_info = ZONES.get(data["zone"], {})
    risk_note = checkin_risk_agent(guest, zone_info)
    guest["risk_note"] = risk_note
    # ─────────────────────────────────────────────────────────

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

@app.route("/api/emergency", methods=["POST"])
def trigger_emergency():
    data = request.json
    zone = data["zone"]
    etype = data["type"]
    lang = data.get("lang", "English")

    affected = [g for g in guests.values() if g["zone"] == zone]
    emergency = {
        "zone": zone,
        "type": etype,
        "lang": lang,
        "time": datetime.now().isoformat(),
        "affected_count": len(affected),
        "affected_guests": affected,
        "zone_info": ZONES[zone],
        "fire_station": FIRE_STATION,
    }
    emergencies[zone] = emergency

    # Initialise safe list for this zone
    safe_guests[zone] = []

    # Build Google Maps directions URL (fire brigade inbound)
    z = ZONES[zone]
    maps_url = (
        f"https://www.google.com/maps/dir/?api=1"
        f"&origin={FIRE_STATION['lat']},{FIRE_STATION['lng']}"
        f"&destination={z['lat']},{z['lng']}"
        f"&travelmode=driving"
    )
    emergency["maps_url"] = maps_url

    # Generate AI instructions
    instructions = generate_ai_instructions(zone, etype, lang, affected)
    emergency["ai_instructions"] = instructions

    socketio.emit("emergency_triggered", emergency)
    return jsonify(emergency)

@app.route("/api/emergency/<zone>", methods=["DELETE"])
def clear_emergency(zone):
    emergencies.pop(zone, None)
    safe_guests.pop(zone, None)
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


# ── AGENT 2: Exit Route endpoint ──────────────────────────────────────────────
@app.route("/api/route", methods=["POST"])
def get_exit_route():
    """
    Guest-facing endpoint.
    Body: { "room": "101", "zone": "A" }
    Returns step-by-step route to nearest safe exit.
    """
    data = request.json
    room = data.get("room")
    zone = data.get("zone")

    if not room or not zone or zone not in ZONES:
        return jsonify({"error": "Provide a valid room and zone (A/B/C)."}), 400

    blocked_zones = list(emergencies.keys())
    route = exit_route_agent(room, zone, blocked_zones)
    return jsonify({
        "room": room,
        "zone": zone,
        "blocked_zones": blocked_zones,
        "route": route,
    })


# ── AGENT 3: Mark guest safe (Headcount Agent input) ─────────────────────────
@app.route("/api/safe", methods=["POST"])
def mark_safe():
    """
    Called when a guest confirms they are safe.
    Body: { "guest_id": "...", "zone": "A" }
    """
    data = request.json
    guest_id = data.get("guest_id")
    zone = data.get("zone")

    if not guest_id or not zone:
        return jsonify({"error": "guest_id and zone required"}), 400

    if zone not in safe_guests:
        safe_guests[zone] = []

    if guest_id not in safe_guests[zone]:
        safe_guests[zone].append(guest_id)

    affected = emergencies.get(zone, {}).get("affected_guests", [])
    unaccounted = [
        {"name": g["name"], "room": g["room"]}
        for g in affected
        if g["id"] not in safe_guests[zone]
    ]

    # Push live update immediately to responder page
    socketio.emit("headcount_update", {
        "zone": zone,
        "zone_name": ZONES[zone]["name"],
        "total_affected": len(affected),
        "safe_count": len(safe_guests[zone]),
        "unaccounted_count": len(unaccounted),
        "unaccounted": unaccounted,
        "timestamp": datetime.now().isoformat(),
    })

    return jsonify({"status": "marked safe", "unaccounted": unaccounted})


@app.route("/api/headcount/<zone>", methods=["GET"])
def get_headcount(zone):
    """
    Returns current safe vs unaccounted breakdown for a zone.
    Used by the /responder page on load.
    """
    if zone not in emergencies:
        return jsonify({"error": "No active emergency in this zone"}), 404

    affected = emergencies[zone].get("affected_guests", [])
    confirmed = safe_guests.get(zone, [])
    unaccounted = [
        {"name": g["name"], "room": g["room"]}
        for g in affected
        if g["id"] not in confirmed
    ]

    return jsonify({
        "zone": zone,
        "zone_name": ZONES[zone]["name"],
        "total_affected": len(affected),
        "safe_count": len(confirmed),
        "unaccounted_count": len(unaccounted),
        "unaccounted": unaccounted,
    })


# ─── Original AI instructions (unchanged) ────────────────────────────────────
def generate_ai_instructions(zone, etype, lang, affected):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        zone_info = ZONES[zone]
        guest_summary = f"{len(affected)} guests" if affected else "guests"
        prompt = f"""You are an emergency AI for a luxury hotel resort.

Emergency: {etype} in {zone_info['name']}
Time: {datetime.now().strftime('%H:%M')}
Affected: {guest_summary}
Exit route: {zone_info['exit']}

Write calm, numbered evacuation instructions in {lang}. Include:
1. Immediate action
2. Do NOT use elevators  
3. Specific exit: {zone_info['exit']}
4. Assembly point: Main hotel parking area
5. Await staff

Max 100 words. Be calm and authoritative."""

        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text
    except Exception as e:
        fallbacks = {
            "English": f"EMERGENCY ALERT — {etype} in {ZONES[zone]['name']}.\n\n1. Stay calm and evacuate immediately.\n2. Do NOT use elevators — use stairwells only.\n3. Proceed to {ZONES[zone]['exit']}.\n4. Go to the main hotel parking area as assembly point.\n5. Await instructions from emergency staff.\n\nStay calm. Help is on the way.",
            "German":  f"NOTFALL — {etype} in {ZONES[zone]['name']}.\n\n1. Ruhig bleiben und sofort evakuieren.\n2. Aufzüge NICHT benutzen — nur Treppenhäuser.\n3. Zum {ZONES[zone]['exit']} gehen.\n4. Sammelplatz: Hauptparkplatz des Hotels.\n5. Anweisungen des Personals abwarten.",
            "French":  f"ALERTE — {etype} dans {ZONES[zone]['name']}.\n\n1. Restez calme et évacuez immédiatement.\n2. N'utilisez PAS les ascenseurs.\n3. Dirigez-vous vers {ZONES[zone]['exit']}.\n4. Point de rassemblement: parking principal.\n5. Attendez les instructions du personnel.",
            "Spanish": f"ALERTA — {etype} en {ZONES[zone]['name']}.\n\n1. Mantenga la calma y evacúe de inmediato.\n2. NO use los ascensores.\n3. Diríjase a {ZONES[zone]['exit']}.\n4. Punto de encuentro: estacionamiento principal.\n5. Espere instrucciones del personal.",
            "Japanese": f"緊急警報 — {ZONES[zone]['name']}で{etype}発生。\n\n1. 落ち着いて直ちに避難してください。\n2. エレベーターは使用しないでください。\n3. {ZONES[zone]['exit']}へ移動してください。\n4. 集合場所：ホテルメインパーキング。\n5. スタッフの指示を待ってください。",
        }
        return fallbacks.get(lang, fallbacks["English"])


if __name__ == "__main__":
    socketio.run(app, debug=True, port=5050)