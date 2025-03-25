import json
import os

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
import fitz  # PyMuPDF for PDF extraction
import pytesseract  # OCR for images
from PIL import Image
import docx  # For DOCX files
app = FastAPI()
connection = sqlite3.connect(':memory:')
cursor = connection.cursor()

UPLOAD_FOLDER = "attachments"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)  # Ensure folder exists

cursor.execute("CREATE TABLE emails (id TEXT PRIMARY KEY, frm TEXT NOT NULL, subject TEXT NOT NULL, body INTEGER NOT NULL, attachment_path TEXT)")

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

    # Extract attachments
    attachment_paths = []
    attachment_texts = []
    for part in email_message.iter_attachments():
        filename = part.get_filename()
        if filename:
            file_path = os.path.join(UPLOAD_FOLDER, filename)
            with open(file_path, "wb") as f:
                f.write(part.get_payload(decode=True))  # Save attachment
            attachment_paths.append(file_path)
            # Parse the content of the attachment
            attachment_content = extract_attachment_content(file_path)
            if attachment_content:
                attachment_texts.append(attachment_content)

    # Combine extracted text with email body
    full_text = f"Email Body:\n{cleaned_body}\n\nAttachments:\n" + "\n\n".join(attachment_texts)

    # Insert some data
    cursor.execute('INSERT OR IGNORE INTO emails (id, frm, subject, body, attachment_path) VALUES (?, ?, ?, ?, ?)', (frm+subject, frm, subject, body, json.dumps(attachment_paths)))

    # Commit the changes
    connection.commit()

    # Close the connection
    #connection.close()

    response_text = classify_and_summarize(full_text)
    try:
        response_json = json.loads(response_text)  # Convert response string to JSON
    except json.JSONDecodeError:
        response_json = {"error": "Failed to parse AI response", "raw_response": response_text}
    return {
        "request_type": response_json.get("request_type", "Unknown"),
        "sub_request_type": response_json.get("sub_request_type", "Unknown"),
        "summary": response_json.get("summary", "No summary provided")
    }

def convert_to_binary_data(filename):
    with open(filename, 'rb') as file:
        blob_data = file.read()
    return blob_data

def ask_question(question):
    API_URL = "https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.1"
    HEADERS = {"Authorization": "Bearer "}

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

def extract_attachment_content(file_path):
    """Extracts text content from attachments based on file type."""
    _, ext = os.path.splitext(file_path)
    ext = ext.lower()

    if ext == ".pdf":
        return extract_text_from_pdf(file_path)
    elif ext == ".txt":
        return extract_text_from_txt(file_path)
    elif ext == ".docx":
        return extract_text_from_docx(file_path)
    elif ext in [".png", ".jpg", ".jpeg"]:
        return extract_text_from_image(file_path)
    else:
        return f"[Unsupported file type: {ext}]"

def extract_text_from_pdf(file_path):
    """Extracts text from a PDF using PyMuPDF (fitz)."""
    text = ""
    try:
        with fitz.open(file_path) as pdf:
            for page in pdf:
                text += page.get_text("text") + "\n"
    except Exception as e:
        return f"[Error reading PDF: {e}]"
    return text.strip()

def extract_text_from_txt(file_path):
    """Extracts text from a TXT file."""
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            return file.read().strip()
    except Exception as e:
        return f"[Error reading TXT file: {e}]"

def extract_text_from_docx(file_path):
    """Extracts text from a DOCX file using python-docx."""
    try:
        doc = docx.Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs]).strip()
    except Exception as e:
        return f"[Error reading DOCX file: {e}]"

def extract_text_from_image(file_path):
    """Extracts text from an image using OCR (Tesseract)."""
    try:
        image = Image.open(file_path)
        return pytesseract.image_to_string(image).strip()
    except Exception as e:
        return f"[Error reading image: {e}]"