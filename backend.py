from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from bson import json_util
from bson.objectid import ObjectId
from bson.errors import InvalidId
from pypdf import PdfReader
from openai import OpenAI
from dotenv import load_dotenv
from baseball_predictor import BaseballPredictor
from werkzeug.security import check_password_hash, generate_password_hash
import os
import json
import re


class Backend:
    EDITABLE_POST_FIELDS = {
        "title", "category", "image", "backImage", "stockImage",
        "takenPhoto", "audioFile", "description", "link", "price",
        "status", "blocks", "ingredients", "steps", "isRecorded",
    }
    POST_CATEGORIES = {
        "comics", "music", "games", "sports", "events", "food",
        "bird", "shop",
    }
    GUEST_EDITABLE_FIELDS = {"name", "title", "image", "link", "blocks"}
    def __init__(self) -> None:
        load_dotenv(override=True)

        self.uri = os.environ["MONGODB_URI"]
        self.client = MongoClient(self.uri, server_api=ServerApi("1"))

        self.db = self.client["LittleBrotherBlog"]
        self.collection = self.db["Posts"]

        self.context = self.load_context()
        self.name = "Gabriel Terrazas"
        self.openai = OpenAI()
        self.predictor = BaseballPredictor()

    def sendToDB(self, formData):
        self.collection.insert_one(formData)
        print("Hey I posted your data fam")
        return

    def search_posts(self, title_query, limit=10):
        query = str(title_query or "").strip()
        if not query:
            return []

        documents = self.collection.find(
            {
                "title": {"$regex": re.escape(query), "$options": "i"},
                "category": {"$in": list(self.POST_CATEGORIES)},
            },
            {"title": 1, "category": 1},
        ).limit(min(max(int(limit), 1), 20))

        return [
            {
                "id": str(document["_id"]),
                "title": document.get("title", "Untitled"),
                "category": document.get("category", ""),
            }
            for document in documents
        ]

    def get_editable_post(self, post_id):
        try:
            object_id = ObjectId(post_id)
        except (InvalidId, TypeError):
            return None

        document = self.collection.find_one(
            {"_id": object_id, "category": {"$in": list(self.POST_CATEGORIES)}}
        )
        if not document:
            return None

        editable = {
            key: value
            for key, value in document.items()
            if key in self.EDITABLE_POST_FIELDS
        }
        return {"id": str(document["_id"]), **editable}

    def update_post(self, post_id, changes):
        try:
            object_id = ObjectId(post_id)
        except (InvalidId, TypeError):
            return None

        safe_changes = {
            key: value
            for key, value in (changes or {}).items()
            if key in self.EDITABLE_POST_FIELDS
        }
        title = safe_changes.get("title")
        category = safe_changes.get("category")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("A title is required")
        if category not in self.POST_CATEGORIES:
            raise ValueError("Invalid post category")

        result = self.collection.update_one(
            {"_id": object_id, "category": {"$in": list(self.POST_CATEGORIES)}},
            {"$set": safe_changes},
        )
        if result.matched_count == 0:
            return None
        return self.get_editable_post(post_id)

    def _validate_guest_submission(self, data):
        safe = {
            key: value
            for key, value in (data or {}).items()
            if key in self.GUEST_EDITABLE_FIELDS
        }
        for field in ("name", "title", "image"):
            if not isinstance(safe.get(field), str) or not safe[field].strip():
                raise ValueError(f"{field.capitalize()} is required")

        if not safe["image"].startswith("data:image/"):
            raise ValueError("Main image must be an uploaded image")

        blocks = safe.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise ValueError("At least one content block is required")
        clean_blocks = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") not in {"text", "image"}:
                raise ValueError("Invalid content block")
            value = block.get("value")
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Every content block is required")
            if block["type"] == "image" and not value.startswith("data:image/"):
                raise ValueError("Image blocks must use uploaded images")
            clean_blocks.append({"type": block["type"], "value": value})

        safe["name"] = safe["name"].strip()
        safe["title"] = safe["title"].strip()
        safe["link"] = str(safe.get("link", "")).strip()
        safe["blocks"] = clean_blocks
        return safe

    def create_guest_submission(self, data):
        submission = self._validate_guest_submission(data)
        submission.update({"category": "guest", "submissionStatus": "pending"})
        result = self.collection.insert_one(submission)
        return str(result.inserted_id)

    def list_pending_guest_submissions(self):
        documents = self.collection.find(
            {"category": "guest", "submissionStatus": "pending"}
        ).sort("_id", -1)
        return [
            {"id": str(document.pop("_id")), **document}
            for document in documents
        ]

    def update_guest_submission(self, post_id, changes):
        try:
            object_id = ObjectId(post_id)
        except (InvalidId, TypeError):
            return None
        safe = self._validate_guest_submission(changes)
        result = self.collection.update_one(
            {"_id": object_id, "category": "guest", "submissionStatus": "pending"},
            {"$set": safe},
        )
        if result.matched_count == 0:
            return None
        return {"id": post_id, **safe, "category": "guest", "submissionStatus": "pending"}

    def publish_guest_submission(self, post_id, changes):
        updated = self.update_guest_submission(post_id, changes)
        if not updated:
            return None
        self.collection.update_one(
            {"_id": ObjectId(post_id), "category": "guest", "submissionStatus": "pending"},
            {"$set": {"submissionStatus": "published"}},
        )
        updated["submissionStatus"] = "published"
        return updated

    def get_published_guest_posts(self):
        documents = self.collection.find(
            {"category": "guest", "submissionStatus": "published"},
            {"submissionStatus": 0},
        ).sort("_id", -1)
        return [
            {"id": str(document.pop("_id")), **document}
            for document in documents
        ]

    async def getFromDB(self, category):
        if category in {"comics", "music", "games", "sports", "events", "food", "bird"}:
            query = {"category": category}
            documents = list(self.collection.find(query))
            serialized_documents = [json_util.dumps(doc) for doc in documents]
            return serialized_documents

        return []

    def getBirdTitles(self):
        query = {"category": "bird"}

        documents = list(
            self.collection.find(query, {"title": 1})  # keep _id
            .sort("_id", -1) 
        )

        titles = [doc["title"] for doc in documents]
        return titles

    def getBirdByName(self, bird_name):
        query = {
            "category": "bird",
            "title": bird_name,
        }

        document = self.collection.find_one(
            query,
            {
                "_id": 0,
            },
        )

        if not document:
            return None

        return document

    async def getShop(self):
        query = {"category": "shop"}
        documents = list(self.collection.find(query))
        serialized_documents = [json_util.dumps(doc) for doc in documents]
        return serialized_documents

    async def getEmailCount(self):
        documents = list(self.collection.find({"email": {"$exists": True}}))

        unique_emails = set()

        for doc in documents:
            unique_emails.add(doc["email"])

        return len(unique_emails)

    async def getEmailList(self):
        documents = list(self.collection.find({"email": {"$exists": True}}))

        unique_emails = set()

        for doc in documents:
            unique_emails.add(doc["email"])

        return list(unique_emails)

    def authenticate_admin(self, username, password):
        """Verify an admin without ever returning credentials to the browser.

        Existing plaintext records are upgraded to a password hash after their
        next successful login. This keeps the current username/password working
        while removing the plaintext password from MongoDB.
        """
        document = self.collection.find_one({"username": username})
        if not document:
            return False

        password_hash = document.get("password_hash")
        if password_hash:
            return check_password_hash(password_hash, password)

        legacy_password = document.get("password")
        if not legacy_password or legacy_password != password:
            return False

        self.collection.update_one(
            {"_id": document["_id"]},
            {
                "$set": {"password_hash": generate_password_hash(password)},
                "$unset": {"password": ""},
            },
        )
        return True

    def load_context(self):
        context = {
            "summary": "",
            "linkedin": "",
        }

        base_path = os.path.dirname(__file__)

        summary_path = os.path.join(base_path, "aboutme", "aboutme.txt")

        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                context["summary"] = f.read()
        except Exception as e:
            print(f"Could not load aboutme.txt: {e}")

        linkedin_path = os.path.join(base_path, "aboutme", "Profile.pdf")

        try:
            reader = PdfReader(linkedin_path)
            linkedin = ""

            for page in reader.pages:
                text = page.extract_text()
                if text:
                    linkedin += text

            context["linkedin"] = linkedin
        except Exception as e:
            print(f"Could not load Profile.pdf: {e}")

        return context

    async def askQuestion(self, question, history=None):
        if history is None:
            history = []

        system_prompt = f"""
            You are acting as {self.name}. You are answering questions on {self.name}'s website,
            particularly questions related to {self.name}'s career, background, skills, experience,
            and interests relating to the website.

            Your responsibility is to represent {self.name} for interactions on the website as faithfully as possible.

            You are given a summary of {self.name}'s background and LinkedIn profile which you can use to answer questions.

            Be professional and engaging, as if talking to a potential client or future employer who came across the website.

            Rules:
            - If you don't know the answer, say so.
            - Do not share {self.name}'s phone number under any circumstances.
            - Keep responses short.
            - Do not respond to anything that was not asked.
            - Never use em dashes.

            ## Summary:
            {self.context["summary"]}

            ## LinkedIn Profile:
            {self.context["linkedin"]}

            With this context, please chat with the user, always staying in character as {self.name}.
        """

        formatted_history = []

        for msg in history[-10:]:
            frontend_role = msg.get("role")
            text = msg.get("text", "")

            if not text:
                continue

            if frontend_role == "bot":
                openai_role = "assistant"
            else:
                openai_role = "user"

            formatted_history.append({
                "role": openai_role,
                "content": text,
            })

        if not formatted_history or formatted_history[-1]["content"] != question:
            formatted_history.append({
                "role": "user",
                "content": question,
            })

        response = self.openai.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                *formatted_history,
            ],
            max_tokens=150,
        )

        return response.choices[0].message.content

    def calculate_nrfi_probability(self, payload):
        stats = payload.get("stats", {})
        pitchers = stats.get("probablePitchers", {})
        offense = stats.get("teamOffense", {})

        home_pitcher = pitchers.get("home", {})
        away_pitcher = pitchers.get("away", {})
        home_offense = offense.get("home", {})
        away_offense = offense.get("away", {})

        def safe_float(value, fallback):
            try:
                if value is None or value == "N/A" or value == "":
                    return fallback
                return float(value)
            except Exception:
                return fallback

        home_era = safe_float(home_pitcher.get("era"), 4.50)
        away_era = safe_float(away_pitcher.get("era"), 4.50)
        home_whip = safe_float(home_pitcher.get("whip"), 1.35)
        away_whip = safe_float(away_pitcher.get("whip"), 1.35)
        home_k9 = safe_float(home_pitcher.get("strikeoutsPer9Inn"), 8.50)
        away_k9 = safe_float(away_pitcher.get("strikeoutsPer9Inn"), 8.50)
        home_bb9 = safe_float(home_pitcher.get("walksPer9Inn"), 3.20)
        away_bb9 = safe_float(away_pitcher.get("walksPer9Inn"), 3.20)

        home_ops = safe_float(home_offense.get("ops"), 0.720)
        away_ops = safe_float(away_offense.get("ops"), 0.720)
        home_obp = safe_float(home_offense.get("obp"), 0.315)
        away_obp = safe_float(away_offense.get("obp"), 0.315)
        home_slg = safe_float(home_offense.get("slg"), 0.400)
        away_slg = safe_float(away_offense.get("slg"), 0.400)

        pitcher_score = 0

        pitcher_score += (4.50 - home_era) * 2.5
        pitcher_score += (4.50 - away_era) * 2.5
        pitcher_score += (1.35 - home_whip) * 12
        pitcher_score += (1.35 - away_whip) * 12
        pitcher_score += (home_k9 - 8.50) * 0.6
        pitcher_score += (away_k9 - 8.50) * 0.6
        pitcher_score += (3.20 - home_bb9) * 1.2
        pitcher_score += (3.20 - away_bb9) * 1.2

        offense_score = 0

        offense_score += (home_ops - 0.720) * 35
        offense_score += (away_ops - 0.720) * 35
        offense_score += (home_obp - 0.315) * 45
        offense_score += (away_obp - 0.315) * 45
        offense_score += (home_slg - 0.400) * 25
        offense_score += (away_slg - 0.400) * 25

        raw_nrfi = 52 + pitcher_score - offense_score

        nrfi_probability = max(35, min(75, raw_nrfi))

        return round(nrfi_probability)

    async def getOdds(self, payload):
        probabilities = self.predictor.calculate_win_probability(payload)
        props = self.predictor.calculate_props(payload)

        nrfi_probability = self.calculate_nrfi_probability(payload)

        enriched_payload = {
            **payload,
            "modelProbabilities": probabilities,
            "props": props,
            "nrfiProbability": nrfi_probability,
        }

        system_prompt = (
            "You are an MLB betting assistant. "
            "Analyze live and pregame baseball data and return only valid JSON. "
            "Use all provided metrics, including team records, team hitting metrics, "
            "probable pitchers, pitcher handedness, batter handedness, batter AVG, OPS, OBP, SLG, "
            "pitcher ERA, WHIP, K/9, BB/9, current score, inning, outs, count, runners on base, "
            "the provided betting signals, the model win probabilities, the NRFI probability, "
            "and the calculated props. "
            "Do not use or assume sportsbook lines. "
            "Do not include markdown fences. "
            "Do not guarantee outcomes. "
            "The props already provided are the only ones that should be recommended. "
            "Use the current at bat as context only, not as the main driver of the prediction. "
            "Be concise, practical, and grounded only in the provided data. "
            "Never use em dashes."
        )

        user_prompt = f"""
            Here is the live game package:

            {json.dumps(enriched_payload, indent=2)}

            Return valid JSON in this exact shape:
            {{
                "summary": "2-3 sentence betting summary using the metrics provided",
                "bestBet": "short recommendation",
                "confidence": "Low, Medium, or High",
                "biggestRisk": "short risk note",
                "parlayAngle": "short parlay note",
                "homeWinProbability": 0,
                "awayWinProbability": 0,
                "modelFavorite": "team name",
                "nrfiProbability": 0,
                "props": [
                    {{
                        "type": "batter_hit",
                        "player": "player name",
                        "recommendation": "To record a hit",
                        "estimatedValue": 0,
                        "probability": 0,
                        "valueScore": 0,
                        "reason": "short reason"
                    }}
                ]
            }}

            Rules:
            - homeWinProbability and awayWinProbability must match the supplied model probabilities exactly
            - modelFavorite must match the supplied model favorite exactly
            - nrfiProbability must match the supplied NRFI probability exactly
            - props must match the supplied calculated props exactly
            - do not invent extra fields
            - if a prop does not have estimatedValue, it is okay for it to be absent
            - if there are no props, return an empty array
            """

        response = self.openai.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=600,
            temperature=0.3,
        )

        content = response.choices[0].message.content

        fallback = {
            "summary": "Model-generated game analysis is available, but the AI explanation was not returned in valid JSON.",
            "bestBet": f"{probabilities['modelFavorite']} side",
            "confidence": "Low",
            "biggestRisk": "Model output was not valid JSON",
            "parlayAngle": "Use caution",
            "homeWinProbability": probabilities["homeWinProbability"],
            "awayWinProbability": probabilities["awayWinProbability"],
            "modelFavorite": probabilities["modelFavorite"],
            "nrfiProbability": nrfi_probability,
            "props": props,
        }

        try:
            parsed = json.loads(content)

            parsed["homeWinProbability"] = probabilities["homeWinProbability"]
            parsed["awayWinProbability"] = probabilities["awayWinProbability"]
            parsed["modelFavorite"] = probabilities["modelFavorite"]
            parsed["nrfiProbability"] = nrfi_probability
            parsed["props"] = props

            return parsed
        except Exception:
            return fallback
