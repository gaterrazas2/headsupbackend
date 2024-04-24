import json
from flask import Flask, jsonify, request
from flask_cors import CORS
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
