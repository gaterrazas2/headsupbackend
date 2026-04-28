from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from bson import json_util
from pypdf import PdfReader
from openai import OpenAI
from dotenv import load_dotenv
from baseball_predictor import BaseballPredictor
import os
import json


class Backend:
    def __init__(self) -> None:
        load_dotenv(override=True)

        self.uri = "mongodb+srv://gterra06:Gt391299%21%21@cluster0.o458oeb.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
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
            self.collection.find(
                query,
                {
                    "_id": 0,
                    "title": 1
                }
            )
        )

        titles = []

        for doc in documents:
            if "title" in doc:
                titles.append(doc["title"])

        return titles

    def getBirdByName(self, bird_name):
        query = {
            "category": "bird",
            "title": bird_name
        }

        document = self.collection.find_one(
            query,
            {
                "_id": 0
            }
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

    async def getCredentials(self):
        document = list(self.collection.find({"username": {"$exists": True}}))
        serialized_document = [json_util.dumps(document)]
        return serialized_document

    def load_context(self):
        context = {
            "summary": "",
            "linkedin": ""
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
                "content": text
            })

        if not formatted_history or formatted_history[-1]["content"] != question:
            formatted_history.append({
                "role": "user",
                "content": question
            })

        response = self.openai.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                *formatted_history
            ],
            max_tokens=150
        )

        return response.choices[0].message.content

    async def getOdds(self, payload):
        probabilities = self.predictor.calculate_win_probability(payload)
        props = self.predictor.calculate_props(payload)

        enriched_payload = {
            **payload,
            "modelProbabilities": probabilities,
            "props": props,
        }

        system_prompt = (
            "You are an MLB betting assistant. "
            "Analyze live and pregame baseball data and return only valid JSON. "
            "Use all provided metrics, including team records, team hitting metrics, "
            "probable pitchers, pitcher handedness, batter handedness, batter AVG, OPS, OBP, SLG, "
            "pitcher ERA, WHIP, K/9, BB/9, current score, inning, outs, count, runners on base, "
            "the provided betting signals, the model win probabilities, and the calculated props. "
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
            max_tokens=500,
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
            "props": props,
        }

        try:
            parsed = json.loads(content)
            parsed["homeWinProbability"] = probabilities["homeWinProbability"]
            parsed["awayWinProbability"] = probabilities["awayWinProbability"]
            parsed["modelFavorite"] = probabilities["modelFavorite"]
            parsed["props"] = props
            return parsed
        except Exception:
            return fallback