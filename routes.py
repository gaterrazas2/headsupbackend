import json
import os
import secrets
from datetime import timedelta
from flask import Flask, jsonify, request, session
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from urllib.parse import unquote
from backend import Backend



app = Flask(__name__)
is_development = os.getenv("FLASK_ENV", "production") == "development"

app.config.update(
    SECRET_KEY=os.environ["FLASK_SECRET_KEY"],
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=not is_development,
    # The frontend and backend are hosted on different sites.
    SESSION_COOKIE_SAMESITE="Lax" if is_development else "None",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=2),
)

allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:3000,https://lilbrother.herokuapp.com",
    ).split(",")
    if origin.strip()
]

CORS(app, origins=allowed_origins, supports_credentials=True)
limiter = Limiter(get_remote_address, app=app, default_limits=[])
login_manager = LoginManager(app)
backend = Backend()


class AdminUser(UserMixin):
    id = "admin"


@login_manager.user_loader
def load_user(user_id):
    return AdminUser() if user_id == "admin" else None


@login_manager.unauthorized_handler
def unauthorized():
    return jsonify({"error": "Authentication required"}), 401


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def require_csrf():
    supplied = request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(supplied, expected):
        return jsonify({"error": "Invalid CSRF token"}), 403
    return None


@app.get("/auth/csrf")
def get_csrf():
    return jsonify({"csrfToken": csrf_token()})


@app.post("/auth/login")
@limiter.limit("5 per minute")
def login():
    csrf_error = require_csrf()
    if csrf_error:
        return csrf_error

    data = request.get_json(silent=True) or {}
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))
    if not backend.authenticate_admin(username, password):
        return jsonify({"error": "Invalid username or password"}), 401

    session.clear()
    login_user(AdminUser(), remember=False, fresh=True)
    session.permanent = True
    return jsonify({"authenticated": True, "csrfToken": csrf_token()})


@app.post("/auth/logout")
@login_required
def logout():
    csrf_error = require_csrf()
    if csrf_error:
        return csrf_error
    logout_user()
    session.clear()
    return jsonify({"authenticated": False})


@app.get("/auth/me")
def auth_status():
    response = jsonify({"authenticated": current_user.is_authenticated})
    response.headers["Cache-Control"] = "no-store"
    return response

# Send to DB
@app.route("/post", methods=['POST'])
@login_required
def print_request():
    csrf_error = require_csrf()
    if csrf_error:
        return csrf_error
    if request.method == 'POST':
        data = request.get_data()
        data_dict = json.loads(data)
        backend.sendToDB(data_dict)
    return "got it!" , 200

@app.route("/askquestion", methods=['POST', 'OPTIONS'])
async def ask_question():
    if request.method == "OPTIONS":
        return '', 200

    try:
        data = request.get_json()

        if not data:
            return jsonify({"error": "No data provided"}), 400

        question = data.get('message')
        history = data.get('history', [])

        if not question:
            return jsonify({"error": "No message provided"}), 400

        print(f"Received question: {question}")
        print(f"History length: {len(history)}")

        result = await backend.askQuestion(question, history)

        return jsonify({"response": result}), 200

    except Exception as e:
        print(f"Backend Error: {e}")
        return jsonify({"error": str(e)}), 500
    
@app.route("/getodds", methods=['POST', 'OPTIONS'])
async def get_odds():
    if request.method == "OPTIONS":
        return '', 200

    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
            
        print(f"Received payload: {data}")
        
        result = await backend.getOdds(data)
        
        return jsonify({"response": result})
    except Exception as e:
        print(f"Backend Error: {e}")
        return jsonify({"error": str(e)}), 500

# Get number of emails added
@app.route("/getEmailCount")
@login_required
async def get_email_count():
    result = await backend.getEmailCount()
    return str(result)

@app.route("/getEmailList")
@login_required
async def get_email_list():
    result = await backend.getEmailList()
    return jsonify(result)

# Get from DB
@app.route("/getComics")
async def get_comics():
    result = await backend.getFromDB('comics')
    return jsonify(result)

@app.route("/getSports")
async def get_sports():
    result = await backend.getFromDB('sports')
    return jsonify(result)

@app.route("/getMusic")
async def get_music():
    result = await backend.getFromDB('music')
    return jsonify(result)

@app.route("/getEvents")
async def get_events():
    result = await backend.getFromDB('events')
    return jsonify(result)

@app.route("/getGames")
async def get_games():
    result = await backend.getFromDB('games')
    return jsonify(result)

@app.route("/getFood")
async def get_food():
    result = await backend.getFromDB('food')
    return jsonify(result)

@app.route("/getShop")
async def get_shop():
    result = await backend.getShop()
    return jsonify(result)

# Get bird titles only (fast)
@app.route("/getBirdTitles", methods=["GET"])
def get_bird_titles():
    try:
        bird_titles = backend.getBirdTitles()

        return jsonify(bird_titles), 200

    except Exception as e:
        print(f"Error getting bird titles: {e}")
        return jsonify({"error": str(e)}), 500


# Get one bird by title (handles spaces safely)
@app.route("/getBirds/<path:bird_name>", methods=["GET"])
def get_bird_by_name(bird_name):
    try:
        # Decode URL encoding just to be safe
        bird_name = unquote(bird_name)

        bird = backend.getBirdByName(bird_name)

        if not bird:
            return jsonify({"error": "Bird not found"}), 404

        return jsonify(bird), 200

    except Exception as e:
        print(f"Error getting bird by name: {e}")
        return jsonify({"error": str(e)}), 500
