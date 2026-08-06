import json
import os
import re
from io import BytesIO
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

try:
    from pypdf import PdfReader
except ImportError:  # pypdf is optional at import time, required only for PDF uploads
    PdfReader = None

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload limit

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
MAX_SOURCE_CHARS = 18000
ALLOWED_DOCUMENT_EXTENSIONS = {"txt", "md", "csv", "pdf"}


def clean_text(text: str) -> str:
    """Normalize whitespace and trim source text to a prompt-safe size."""
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:MAX_SOURCE_CHARS]


def is_valid_web_url(url: str) -> bool:
    """Return True when the string is a normal http(s) URL with a hostname."""
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def scrape_text(url: str) -> str:
    """Fetch a URL and return cleaned, readable text content from the page."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; QuizMasterBot/1.0)"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strip elements that aren't real page content
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "iframe", "svg", "form"]):
        tag.decompose()

    return clean_text(soup.get_text(separator=" "))


def extract_text_from_upload(upload) -> str:
    """Extract readable text from an uploaded PDF or plain-text style document."""
    if not upload or not upload.filename:
        return ""

    extension = upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
    if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValueError("Unsupported document type. Upload a PDF, TXT, MD, or CSV file.")

    raw = upload.read()
    if not raw:
        return ""

    if extension == "pdf":
        if PdfReader is None:
            raise RuntimeError("PDF support is not installed. Add pypdf to your environment.")
        reader = PdfReader(BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages[:30]]
        return clean_text("\n".join(pages))

    return clean_text(raw.decode("utf-8", errors="ignore"))


def build_source_material(source_type: str, source_value: str, upload=None) -> tuple[str, str]:
    """Build prompt-ready source material from a topic, URL, pasted text, or upload."""
    source_value = (source_value or "").strip()

    if source_type == "url":
        if not is_valid_web_url(source_value):
            raise ValueError("Please enter a valid website URL starting with http:// or https://.")
        return scrape_text(source_value), source_value

    if source_type == "document":
        text = extract_text_from_upload(upload)
        return text, upload.filename if upload else "Uploaded document"

    if source_type == "text":
        return clean_text(source_value), "Pasted notes"

    if source_type == "topic":
        return clean_text(source_value), f"Topic prompt: {source_value}"

    raise ValueError("Choose a valid source type: topic, url, text, or document.")


def generate_quiz_from_text(text: str, num_questions: int = 10, source_type: str = "text") -> list:
    """Call the Google Gemini API to turn source material into structured quiz data."""
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "Server is missing a GEMINI_API_KEY environment variable. "
            "Set it in your hosting provider's dashboard."
        )

    if source_type == "topic":
        source_instruction = (
            "The user provided a topic prompt rather than source notes. Create a useful mock-test style quiz "
            "for that topic using standard educational knowledge."
        )
    else:
        source_instruction = (
            "Create questions based ONLY on facts present in the source material. "
            "Do not invent details that are not supported by the source."
        )

    prompt = f"""You are an expert exam paper setter creating a professional mock-test quiz.

Source material or topic:
\"\"\"{text}\"\"\"

Instructions:
- {source_instruction}
- Create exactly {num_questions} multiple-choice questions.
- Each question must have exactly 4 answer options with only one correct answer.
- Mix difficulty across easy, medium, and hard.
- Keep questions clear, exam-like, and unambiguous.
- Include a concise explanation for the correct answer.

Respond with ONLY valid JSON (no markdown fences, no extra text) in this exact structure:
[
  {{
    "question": "string",
    "options": ["string", "string", "string", "string"],
    "correct_index": 0,
    "explanation": "one short sentence explaining the correct answer",
    "difficulty": "Easy"
  }}
]"""

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.45, "maxOutputTokens": 8192},
    }

    resp = requests.post(
        GEMINI_API_URL,
        params={"key": GEMINI_API_KEY},
        json=body,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        raise RuntimeError("Gemini did not return a usable response. Please try again.")

    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```(json)?", "", raw_text).strip()
        raw_text = re.sub(r"```$", "", raw_text).strip()

    questions = json.loads(raw_text)
    validate_questions(questions, num_questions)
    return questions


def validate_questions(questions: list, expected_count: int) -> None:
    """Validate AI-generated quiz structure before sending it to the browser."""
    if not isinstance(questions, list) or not questions:
        raise ValueError("AI response did not return a list of questions.")

    if len(questions) != expected_count:
        raise ValueError("AI response did not return the requested number of questions.")

    for q in questions:
        if not isinstance(q, dict):
            raise ValueError("AI response included an invalid question object.")
        if not all(k in q for k in ("question", "options", "correct_index", "explanation")):
            raise ValueError("AI response was missing required quiz fields.")
        if not isinstance(q["question"], str) or not q["question"].strip():
            raise ValueError("AI response included an empty question.")
        if not isinstance(q["options"], list) or len(q["options"]) != 4:
            raise ValueError("AI response did not return exactly 4 options for a question.")
        if not all(isinstance(option, str) and option.strip() for option in q["options"]):
            raise ValueError("AI response included an empty answer option.")
        if not isinstance(q["correct_index"], int) or q["correct_index"] not in range(4):
            raise ValueError("AI response included an invalid correct answer index.")


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/generate-quiz", methods=["POST"])
def api_generate_quiz():
    if request.form:
        data = request.form
        upload = request.files.get("document")
    else:
        data = request.get_json(force=True, silent=True) or {}
        upload = None

    source_type = (data.get("source_type") or "topic").strip().lower()
    source_value = (data.get("source") or data.get("url") or "").strip()

    try:
        num_questions = int(data.get("num_questions", 10))
    except (TypeError, ValueError):
        num_questions = 10
    num_questions = max(3, min(num_questions, 50))

    try:
        source_text, source_label = build_source_material(source_type, source_value, upload)
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Could not reach that page: {e}"}), 400
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    if source_type == "topic" and len(source_text) < 3:
        return jsonify({"error": "Please provide a topic so the AI can build a useful quiz."}), 400

    if source_type != "topic" and len(source_text) < 200:
        return jsonify({"error": "That source does not have enough readable text to build a quiz from."}), 400

    try:
        questions = generate_quiz_from_text(source_text, num_questions, source_type)
    except json.JSONDecodeError:
        return jsonify({"error": "The AI response could not be parsed. Please try again."}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"questions": questions, "source_label": source_label, "source_type": source_type})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
