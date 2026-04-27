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

# ─── In-memory store (replace with Firebase Firestore in prod) ───────────────
guests = {}
emergencies = {}

ZONES = {
    "A": {"name": "Building A — Main Tower",    "lat": 48.8584, "lng": 2.2945, "entrance": "West Wing Service Road",   "exit": "Exit A (West Ground Floor)", "color": "#3B82F6"},
    "B": {"name": "Building B — Garden Wing",   "lat": 48.8590, "lng": 2.2960, "entrance": "Main Hotel Driveway",       "exit": "Main Exit (South Lobby)",    "color": "#10B981"},
    "C": {"name": "Building C — Beach Resort",  "lat": 48.8575, "lng": 2.2970, "entrance": "Beach Access Road",         "exit": "Exit C (East Beachfront)",   "color": "#F59E0B"},
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
        # Fallback instructions
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
