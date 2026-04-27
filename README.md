# Hospitality Crisis Sync (HCS)
## Python/Flask MVP — Local Setup Guide

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure API Keys
Create a `.env` file in the project root:
```
ANTHROPIC_API_KEY=your_anthropic_key_here
GOOGLE_MAPS_API_KEY=your_google_maps_key_here
SECRET_KEY=any-random-secret
```

**Anthropic API key** → https://console.anthropic.com
**Google Maps API key** → https://console.cloud.google.com
  - Enable: Maps Embed API, Directions API, Maps JavaScript API

### 3. Add Google Maps key to responder page
In `templates/responder.html`, line 3 of the `<script>` block:
```js
const GOOGLE_MAPS_API_KEY = 'your_key_here';
```

### 4. Run the server
```bash
python app.py
```

Open http://localhost:5050

---

### Pages
| URL | Role |
|-----|------|
| `/` | Overview & system status |
| `/checkin` | Receptionist guest check-in |
| `/admin` | Admin emergency trigger + AI instructions |
| `/responder` | First responder live heat map + routing |

### Architecture
- **Flask** — web server & REST API
- **Flask-SocketIO** — real-time WebSocket push to all clients
- **Anthropic Claude API** — multilingual AI evacuation instructions
- **Google Maps Embed/Directions API** — responder routing
- **In-memory store** — replace with Firebase Firestore in production

### Production Upgrade Path
1. Replace in-memory `guests` dict with Firebase Firestore
2. Replace `socketio.emit()` with Firebase Cloud Messaging (FCM) topic sends
3. Deploy to Google Cloud Run or App Engine
4. Add Firebase Auth for role-based access (receptionist / admin / responder)
5. Integrate Vertex AI (Gemini) to replace Anthropic for full Google stack
