
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from bson import json_util
from pypdf import PdfReader
from openai import OpenAI
from dotenv import load_dotenv
import os, json

class Backend:
    def __init__(self) -> None:
        load_dotenv(override=True)
        # database setup
        self.uri = "mongodb+srv://gterra06:Gt391299%21%21@cluster0.o458oeb.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
        self.client = MongoClient(self.uri, server_api=ServerApi('1'))
        self.db = self.client['LittleBrotherBlog']
        self.collection = self.db['Posts']
        self.context = self.load_context()
        self.name = "Gabriel Terrazas"
        self.openai = OpenAI()

    def sendToDB(self, formData):
        # inserting form data in db 
        self.collection.insert_one(formData)
        print("Hey I posted your data fam")
        return 

    async def getFromDB(self, category):
        if category in {'comics', 'music', 'games', 'sports', 'events', 'food'}:
            query = {'category': category}
            documents = list(self.collection.find(query))
            # Convert ObjectId to string for serialization
            serialized_documents = [json_util.dumps(doc) for doc in documents]
            return serialized_documents
        else:
            return []
        
    async def getShop(self):
        query = {'category' : 'shop'}
        documents = list(self.collection.find(query))
        # Convert ObjectId to string for serialization
        serialized_documents = [json_util.dumps(doc) for doc in documents]
        return serialized_documents
    
    # convert to set to remove duplicates
    async def getEmailCount(self):
        documents = list(self.collection.find({'email': {'$exists': True}}))
        unique_emails = set()
        for doc in documents:
            unique_emails.add(doc['email'])
        count = len(unique_emails)
        return count
    
    # convert to set to remove duplicates
    async def getEmailList(self):
        documents = list(self.collection.find({'email': {'$exists': True}}))
        unique_emails = set()
        for doc in documents:
            unique_emails.add(doc['email'])
        unique_emails = list(unique_emails)
        return unique_emails
    
    # convert to set to remove duplicates
    async def getCredentials(self):
        document = list(self.collection.find({'username': {'$exists': True}}))
        serialized_document = [json_util.dumps(document)]
        return serialized_document

    def load_context(self):
        context = {'summary':'','linkedin':''}

        # Build path relative to backend.py
        base_path = os.path.dirname(__file__)
        summary_path = os.path.join(base_path, "aboutme", "aboutme.txt")

        with open(summary_path, "r", encoding="utf-8") as f:
            summary = f.read()
        context['summary'] = summary

        linkedin_path = os.path.join(base_path, "aboutme", "Profile.pdf")
        reader = PdfReader(linkedin_path)
        linkedin = ""
        for page in reader.pages:
            text = page.extract_text()
            if text:
                linkedin += text

        context['linkedin'] = linkedin
        return context
    
    async def askQuestion(self, question):
        system_prompt = f"You are acting as {self.name}. You are answering questions on {self.name}'s website, \
        particularly questions related to {self.name}'s career, background, skills, experience, and interests relating to the website. \
        Your responsibility is to represent {self.name} for interactions on the website as faithfully as possible. \
        You are given a summary of {self.name}'s background and LinkedIn profile which you can use to answer questions. \
        Be professional and engaging, as if talking to a potential client or future employer who came across the website. \
        If you don't know the answer, say so. Do not share {self.name}'s phone number under any circumstances. \
        Don't make the responses super long, keep them short and don't respond to anything that wasn't asked. Also never use em dashes"

        system_prompt += f"\n\n## Summary:\n{self.context['summary']}\n\n## LinkedIn Profile:\n{self.context['linkedin']}\n\n"
        system_prompt += f"With this context, please chat with the user, always staying in character as {self.name}."

        response = self.openai.chat.completions.create(
            model="gpt-4.1-mini",
            messages = [
                {"role": "system", "content": system_prompt}] + [{"role": "user", "content": question}
            ],
            max_tokens=150
        )

        return response.choices[0].message.content
    

    async def getOdds(self, stats):
        system_prompt = (
            "You are an MLB betting assistant. "
            "Analyze the provided baseball game stats and return only valid JSON. "
            "Do not include markdown fences. "
            "Do not guarantee outcomes. "
            "Keep it concise and practical. "
            "Never use em dashes."
        )

        user_prompt = f"""
    Here are the game stats:

    {stats}

    Return valid JSON in this exact shape:
    {{
        "summary": "2-3 sentence summary",
        "bestBet": "short recommendation",
        "confidence": "Low, Medium, or High",
        "biggestRisk": "short risk note",
        "parlayAngle": "short parlay note"
    }}
    """

        response = self.openai.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=220
        )

        content = response.choices[0].message.content

        try:
            return json.loads(content)
        except Exception:
            return {
                "summary": content,
                "bestBet": "No structured recommendation returned",
                "confidence": "Low",
                "biggestRisk": "Model output was not valid JSON",
                "parlayAngle": "Use caution"
            }






