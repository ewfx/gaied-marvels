import json
import os
import hashlib
from typing import List, Tuple, Optional, Dict, Any

from fastapi import FastAPI, File, UploadFile, HTTPException
from email import message_from_bytes
from email.policy import default
import sqlite3

import requests
import fitz  # PyMuPDF for PDF extraction
import pytesseract  # OCR for images
from PIL import Image
import docx  # For DOCX files

from langchain_community.llms import HuggingFacePipeline
from langchain.prompts import PromptTemplate

class EmailProcessor:
    """Main class to handle email processing and database interactions."""
    
    def __init__(self, database_path: str = ':memory:'):
        """
        Initialize the email processor with database connection.
        
        Args:
            database_path (str): Path to SQLite database. Defaults to in-memory database.
        """
        self.connection = sqlite3.connect(database_path)
        self.cursor = self.connection.cursor()
        self._create_tables()
        self._create_upload_folders()
        self.request_types = self._initialize_request_types()

    def _create_tables(self):
        """Create necessary database tables."""
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS emails (
                id TEXT PRIMARY KEY,
                frm TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT,
                attachment_path TEXT,
                hash TEXT,
                request_type TEXT,
                sub_request_type TEXT,
                summary TEXT
            )
        """)
        self.connection.commit()

    def _create_upload_folders(self):
        """Create necessary upload and static folders."""
        os.makedirs("attachments", exist_ok=True)
        os.makedirs("static", exist_ok=True)

    def _initialize_request_types(self) -> List[Tuple[str, str]]:
        """Initialize default request types."""
        return [
            ("Account Management", "Update Contact Details"),
            ("Account Management", "Close Account"),
            ("Transaction Issues", "Failed Transaction"),
            ("Transaction Issues", "Disputed Transaction"),
            ("Loan Services", "Apply for Loan"),
            ("Loan Services", "Loan Repayment Issues"),
            ("Credit Card Services", "Lost or Stolen Card"),
            ("Credit Card Services", "Request Credit Limit Increase")
        ]

    @staticmethod
    def generate_hash(text: str) -> str:
        """Generate a SHA-256 hash for the given text."""
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def generate_email_hash(self, body: str, attachments: List[str]) -> str:
        """Generate a hash for an email by combining body and attachment texts."""
        combined_text = body + "\n".join(attachments)
        return self.generate_hash(combined_text)

    def add_request_type(self, category: str, request_type: str) -> None:
        """Add a new request type to the list."""
        self.request_types.append((category, request_type))

    def classify_and_summarize(self, body: str) -> str:
        """Classify and summarize an email body."""
        request_data_str = '\n'.join([f"- {r[0]}: {r[1]}" for r in self.request_types])

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
        response = self.ask_question(question)
        return self.extract_json(response)

    def ask_question(self, question: str) -> Optional[str]:
        """Make a question request to Hugging Face API."""
        API_URL = "https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.1"
        HEADERS = {"Authorization": "Bearer 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"}

        try:
            response = requests.post(API_URL, headers=HEADERS, json={"inputs": question})
            response.raise_for_status()
            result = response.json()
            return result[0]['generated_text']
        except requests.exceptions.RequestException as e:
            print(f"Error in API request: {e}")
            return None

    @staticmethod
    def extract_json(response_text: str) -> str:
        """Extract JSON from response text using regex."""
        import re
        try:
            match = re.search(r'\{[^{}]*\}', response_text, re.DOTALL)
            return match.group(0) if match else "No JSON found in response."
        except Exception as e:
            print(f"Error extracting JSON: {e}")
            return "JSON extraction failed"

    def process_email(self, file: UploadFile) -> Dict[str, Any]:
        """Process an uploaded email file."""
        if not file.filename.endswith('.eml'):
            raise HTTPException(status_code=400, detail="Invalid file type. Please upload a .eml file.")
        
        content = file.file.read()
        email_message = message_from_bytes(content, policy=default)

        subject = email_message.get('subject', 'No Subject')
        frm = email_message.get('from', 'Unknown Sender')

        body = email_message.get_body(preferencelist=('plain')).get_content()

        attachment_paths, attachment_texts = self._process_attachments(email_message)
        full_text = f"Email Body:\n{body}\n\nAttachments:\n" + "\n\n".join(attachment_texts)

        email_hash = self.generate_email_hash(body, attachment_texts)

        # Check for duplicates
        duplicate = self._check_duplicate(email_hash)
        if duplicate:
            return {
                "message": "Duplicate email detected.",
                "previous_email": duplicate
            }

        response_text = self.classify_and_summarize(full_text)
        response_json = json.loads(response_text)

        self._save_email_to_database(frm, subject, body, attachment_paths, email_hash, response_json)

        return {
            "sender": frm,
            "subject": subject,
            "request_type": response_json.get("request_type", "Unknown"),
            "sub_request_type": response_json.get("sub_request_type", "Unknown"),
            "summary": response_json.get("summary", "No summary provided")
        }

    def _process_attachments(self, email_message: Any) -> Tuple[List[str], List[str]]:
        """Extract and process email attachments."""
        attachment_paths = []
        attachment_texts = []
        
        for part in email_message.iter_attachments():
            filename = part.get_filename()
            if filename:
                file_path = os.path.join("attachments", filename)
                with open(file_path, "wb") as f:
                    f.write(part.get_payload(decode=True))
                attachment_paths.append(file_path)
                
                attachment_content = self.extract_attachment_content(file_path)
                if attachment_content:
                    attachment_texts.append(attachment_content)
        
        return attachment_paths, attachment_texts

    def _check_duplicate(self, email_hash: str) -> Optional[Dict[str, str]]:
        """Check if an email is a duplicate in the database."""
        self.cursor.execute(
            "SELECT frm, subject, request_type, sub_request_type, summary FROM emails WHERE hash = ?", 
            (email_hash,)
        )
        result = self.cursor.fetchone()
        
        return {
            "sender": result[0],
            "subject": result[1],
            "request_type": result[2],
            "sub_request_type": result[3],
            "summary": result[4]
        } if result else None

    def _save_email_to_database(self, frm: str, subject: str, body: str, 
                                 attachment_paths: List[str], email_hash: str, 
                                 response_json: Dict[str, str]) -> None:
        """Save processed email to the database."""
        self.cursor.execute(
            'INSERT OR IGNORE INTO emails (id, frm, subject, body, attachment_path, hash, request_type, sub_request_type, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                frm + subject, 
                frm, 
                subject, 
                body, 
                json.dumps(attachment_paths), 
                email_hash,
                response_json.get("request_type", "Unknown"),
                response_json.get("sub_request_type", "Unknown"),
                response_json.get("summary", "No summary provided")
            )
        )
        self.connection.commit()

    @staticmethod
    def extract_attachment_content(file_path: str) -> str:
        """Extract text from various file types."""
        ext = os.path.splitext(file_path)[1].lower()
        extractors = {
            ".pdf": EmailProcessor._extract_text_from_pdf,
            ".txt": EmailProcessor._extract_text_from_txt,
            ".docx": EmailProcessor._extract_text_from_docx,
            ".png": EmailProcessor._extract_text_from_image,
            ".jpg": EmailProcessor._extract_text_from_image,
            ".jpeg": EmailProcessor._extract_text_from_image
        }
        
        extractor = extractors.get(ext, lambda _: f"[Unsupported file type: {ext}]")
        return extractor(file_path)

    @staticmethod
    def _extract_text_from_pdf(file_path: str) -> str:
        try:
            with fitz.open(file_path) as pdf:
                return "\n".join(page.get_text("text") for page in pdf)
        except Exception as e:
            return f"[Error reading PDF: {e}]"

    @staticmethod
    def _extract_text_from_txt(file_path: str) -> str:
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                return file.read().strip()
        except Exception as e:
            return f"[Error reading TXT file: {e}]"

    @staticmethod
    def _extract_text_from_docx(file_path: str) -> str:
        try:
            doc = docx.Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception as e:
            return f"[Error reading DOCX file: {e}]"

    @staticmethod
    def _extract_text_from_image(file_path: str) -> str:
        try:
            image = Image.open(file_path)
            return pytesseract.image_to_string(image).strip()
        except Exception as e:
            return f"[Error reading image: {e}]"

def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    app = FastAPI()
    email_processor = EmailProcessor()

    @app.get("/")
    async def read_root():
        return {"message": "Email Processing Service"}

    @app.post("/requests")
    async def add_request(request: dict):
        category = request.get("category")
        request_type = request.get("request_type")

        if not category or not request_type:
            raise HTTPException(status_code=400, detail="Invalid input. Provide 'category' and 'request_type'.")

        email_processor.add_request_type(category, request_type)
        return {
            "message": "Request added successfully", 
            "updated_data": email_processor.request_types
        }

    @app.get("/requests")
    async def get_requests():
        return email_processor.request_types

    @app.post("/process_email")
    async def read_mail(file: UploadFile = File(...)):
        return email_processor.process_email(file)

    return app

app = create_app()