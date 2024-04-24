import pymongo
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from bson import json_util

class Backend:
    def __init__(self) -> None:
        # database setup
        self.uri = "mongodb+srv://gterra06:Gt391299%21%21@cluster0.o458oeb.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
        self.client = MongoClient(self.uri, server_api=ServerApi('1'))
        self.db = self.client['LittleBrotherBlog']
        self.collection = self.db['Posts']

    def sendToDB(self, formData):

        # inserting form data in db 
        self.collection.insert_one(formData)
        print("Hey I posted your data fam")
        return  


    async def getFromDB(self, category):
        if category in {'comics', 'music', 'games', 'sports', 'events'}:
            query = {'category': category}
            documents = list(self.collection.find(query))
            # Convert ObjectId to string for serialization
            serialized_documents = [json_util.dumps(doc) for doc in documents]
            return serialized_documents
        else:
            return []
    
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




