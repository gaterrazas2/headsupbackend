import json
from flask import Flask, jsonify, request
from flask_cors import CORS
from urllib.parse import unquote
from backend import Backend



app = Flask(__name__)

CORS(app)
backend = Backend()

# Send to DB
@app.route("/post", methods=['POST'])
def print_request():
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
@app.route("/signin")
async def get_credentials():
    result = await backend.getCredentials()
    return jsonify(result)

# Get number of emails added
@app.route("/getEmailCount")
async def get_email_count():
    result = await backend.getEmailCount()
    return str(result)

@app.route("/getEmailList")
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
