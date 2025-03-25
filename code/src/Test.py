import json
from fastapi import FastAPI, File, UploadFile, HTTPException
from email import message_from_bytes
from email.policy import default
from bs4 import BeautifulSoup
import re
import sqlite3
from sqlite3 import Error as Err
from langchain_community.llms import HuggingFacePipeline
from langchain.prompts import PromptTemplate
import requests
app = FastAPI()
connection = sqlite3.connect(':memory:')
cursor = connection.cursor()

cursor.execute("CREATE TABLE emails (id TEXT PRIMARY KEY, frm TEXT NOT NULL, subject TEXT NOT NULL, body INTEGER NOT NULL, attachment BLOB)")

# Request and Sub-Request Types stored in a list
request_data = [
    ("Account Management", "Update Contact Details"),
    ("Account Management", "Close Account"),
    ("Transaction Issues", "Failed Transaction"),
    ("Transaction Issues", "Disputed Transaction"),
    ("Loan Services", "Apply for Loan"),
    ("Loan Services", "Loan Repayment Issues"),
    ("Credit Card Services", "Lost or Stolen Card"),
    ("Credit Card Services", "Request Credit Limit Increase")
]

@app.post("/add_request")
async def add_request(request: dict):
    category = request.get("category")
    request_type = request.get("request_type")

    if not category or not request_type:
        raise HTTPException(status_code=400, detail="Invalid input. Provide 'category' and 'request_type'.")

    # Append new request
    request_data.append((category, request_type))

    return {"message": "Request added successfully", "updated_data": request_data}

@app.get("/get_requests")
async def get_requests():
    return request_data

@app.post("/read")
async def read_mail(file: UploadFile = File(...)):
    content = await file.read()
    email_message = message_from_bytes(content, policy=default)

    # Extracting some basic information from the email
    subject = email_message['subject']
    frm = email_message['from']
    to = email_message['to']
    body = email_message.get_body(preferencelist=('plain')).get_content()

    cleaned_body = clean_email_body(body)
    # Insert some data
    cursor.execute('INSERT OR IGNORE INTO emails (id, frm, subject, body) VALUES (?, ?, ?, ?)', (frm+subject, frm, subject, body))

    # Commit the changes
    connection.commit()

    # Query the data
    cursor.execute('SELECT * FROM emails')
    rows = cursor.fetchall()

    # Close the connection
    #connection.close()

    response_text = classify_and_summarize(body)
    try:
        response_json = json.loads(response_text)  # Convert response string to JSON
    except json.JSONDecodeError:
        response_json = {"error": "Failed to parse AI response", "raw_response": response_text}
    return {
        "request_type": response_json.get("request_type", "Unknown"),
        "sub_request_type": response_json.get("sub_request_type", "Unknown"),
        "summary": response_json.get("summary", "No summary provided")
    }

def clean_email_body(body):
    soup = BeautifulSoup(body, "html.parser")
    text = soup.get_text()  # Remove HTML tags
    text = re.sub(r'\s+', ' ', text)  # Remove extra spaces
    return text.strip()

def convert_to_binary_data(filename):
    with open(filename, 'rb') as file:
        blob_data = file.read()
    return blob_data

def ask_question(question):
    API_URL = "https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.1"
    HEADERS = {"Authorization": "Bearer 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"}

    payload = {"inputs": question}

    try:
        response = requests.post(API_URL, headers=HEADERS, json=payload)
        response.raise_for_status()
        result = response.json()
        return result[0]['generated_text']
    except requests.exceptions.RequestException as e:
        print("Error:", e)
        return None

def classify_and_summarize(body):
    request_data_str = '\n'.join([f"- {r[0]}: {r[1]}" for r in request_data])

    prompt_template = PromptTemplate(
        input_variables=["request_type", "sub_request_type"],
        template="""
        Classify the following bank customer query into a request type and sub-request type using the available categories below. Then generate a brief summary.
        Available Request Types and Sub-Requests: {request_data}
        
        Query: {body}
        Provide the response in JSON format with keys: request_type, sub_request_type, summary.
        """
    )


    question = prompt_template.format(request_data=request_data_str, body=body)
    response = ask_question(question)
    extracted_json = extract_json(response)
    return extracted_json

def extract_json(response_text):
    try:
        # Extract the JSON object using regex
        match = re.search(r'\{[^{}]*\}', response_text, re.DOTALL)
        if match:
            return match.group(0)
        else:
            return "No JSON found in response."
    except Exception as e:
        print(f"Error extracting JSON: {e}")
        return None