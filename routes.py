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
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
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


@app.get("/admin/posts/search")
@login_required
def search_admin_posts():
    query = request.args.get("q", "")
    if len(query) > 100:
        return jsonify({"error": "Search is too long"}), 400
    return jsonify(backend.search_posts(query))


@app.get("/admin/posts/<post_id>")
@login_required
def get_admin_post(post_id):
    post = backend.get_editable_post(post_id)
    if not post:
        return jsonify({"error": "Post not found"}), 404
    return jsonify(post)


@app.put("/admin/posts/<post_id>")
@login_required
def update_admin_post(post_id):
    csrf_error = require_csrf()
    if csrf_error:
        return csrf_error

    try:
        post = backend.update_post(post_id, request.get_json(silent=True) or {})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    if not post:
        return jsonify({"error": "Post not found"}), 404
    return jsonify({"post": post, "message": "Post updated"})


@app.delete("/admin/posts/<post_id>")
@login_required
def delete_admin_post(post_id):
    csrf_error = require_csrf()
    if csrf_error:
        return csrf_error
    if not backend.delete_post(post_id):
        return jsonify({"error": "Post not found"}), 404
    return jsonify({"message": "Post permanently deleted"})


@app.post("/guest-submissions")
@limiter.limit("5 per hour")
def create_guest_submission():
    try:
        submission_id = backend.create_guest_submission(request.get_json(silent=True) or {})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"id": submission_id, "message": "Submitted for review"}), 201


@app.get("/guest-posts")
def get_guest_posts():
    return jsonify(backend.get_published_guest_posts())


@app.get("/admin/guest-submissions")
@login_required
def get_guest_submissions():
    return jsonify(backend.list_pending_guest_submissions())


@app.put("/admin/guest-submissions/<post_id>")
@login_required
def update_guest_submission(post_id):
    csrf_error = require_csrf()
    if csrf_error:
        return csrf_error
    try:
        submission = backend.update_guest_submission(post_id, request.get_json(silent=True) or {})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    if not submission:
        return jsonify({"error": "Submission not found"}), 404
    return jsonify({"submission": submission, "message": "Draft saved"})


@app.post("/admin/guest-submissions/<post_id>/publish")
@login_required
def publish_guest_submission(post_id):
    csrf_error = require_csrf()
    if csrf_error:
        return csrf_error
    try:
        submission = backend.publish_guest_submission(post_id, request.get_json(silent=True) or {})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    if not submission:
        return jsonify({"error": "Submission not found"}), 404
    return jsonify({"post": submission, "message": "Guest post published"})

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


@app.get("/model-performance")
@limiter.limit("10 per minute")
def model_performance():
    try:
        response = jsonify(backend.get_model_performance())
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as error:
        print(f"Model performance error: {error}")
        return jsonify({"error": "Could not load model performance"}), 500


@app.get("/admin/fantasy/status")
@login_required
def fantasy_status():
    return jsonify({
        "configured": backend.fantasy.configured(),
        "leagues": [
            {"key": key, **config}
            for key, config in backend.fantasy.LEAGUES.items()
        ],
    })


@app.post("/admin/fantasy/leagues/<league_key>/recommendations")
@login_required
def fantasy_recommendations(league_key):
    csrf_error = require_csrf()
    if csrf_error:
        return csrf_error
    try:
        plan = backend.fantasy.save_plan(backend.fantasy.build_plan(league_key))
        return jsonify(plan)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        print(f"Fantasy recommendation error: {error}")
        return jsonify({"error": "Could not load ESPN recommendations"}), 502


@app.post("/admin/fantasy/recommendations/<plan_id>/<decision>")
@login_required
def review_fantasy_recommendation(plan_id, decision):
    csrf_error = require_csrf()
    if csrf_error:
        return csrf_error
    try:
        if decision == "approve":
            result = backend.fantasy.approve_plan(plan_id)
        elif decision == "deny":
            result = backend.fantasy.deny_plan(plan_id)
        else:
            return jsonify({"error": "Decision must be approve or deny"}), 400
        return jsonify(result)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        print(f"Fantasy approval error: {error}")
        return jsonify({"error": "ESPN could not complete that action. No further moves were attempted."}), 502

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
