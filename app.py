import json
import os
import re
from io import BytesIO
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload limit

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
OPENROUTER_API_KEYS = [
    key.strip()
    for key in os.environ.get("OPENROUTER_API_KEYS", os.environ.get("OPENROUTER_API_KEY", "")).split(",")
    if key.strip()
]
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL")
APP_NAME = os.environ.get("APP_NAME", "QuizMaster")
MAX_SOURCE_CHARS = 18000
ALLOWED_DOCUMENT_EXTENSIONS = {"txt", "md", "csv", "pdf"}
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


class AIProviderError(RuntimeError):
    """Raised when one AI provider cannot generate a quiz."""

    def __init__(self, provider: str, message: str, retryable: bool = True):
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


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
        from pypdf import PdfReader

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


def build_quiz_prompt(text: str, num_questions: int, source_type: str) -> str:
    """Create the shared prompt used by every configured AI provider."""
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

    return f'''You are an expert exam paper setter creating a professional mock-test quiz.

Source material or topic:
"""{text}"""

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
]'''


def parse_questions(raw_text: str, num_questions: int) -> list:
    """Parse and validate quiz JSON returned by an AI provider."""
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```(json)?", "", raw_text).strip()
        raw_text = re.sub(r"```$", "", raw_text).strip()

    questions = json.loads(raw_text)
    if isinstance(questions, dict) and "questions" in questions:
        questions = questions["questions"]
    validate_questions(questions, num_questions)
    return questions


def is_retryable_response(resp: requests.Response) -> bool:
    """Return True when a provider response should fall through to the next API key/provider."""
    return resp.status_code in RETRYABLE_STATUS_CODES


def request_gemini_quiz(prompt: str, num_questions: int) -> list:
    """Call Google Gemini directly."""
    if not GEMINI_API_KEY:
        raise AIProviderError("Gemini", "GEMINI_API_KEY is not configured.")

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.45, "maxOutputTokens": 8192},
    }
    resp = requests.post(GEMINI_API_URL, params={"key": GEMINI_API_KEY}, json=body, timeout=60)
    if not resp.ok:
        raise AIProviderError("Gemini", f"Gemini returned HTTP {resp.status_code}.", is_retryable_response(resp))

    data = resp.json()
    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise AIProviderError("Gemini", "Gemini did not return a usable response.") from exc

    return parse_questions(raw_text, num_questions)


def request_openrouter_quiz(prompt: str, num_questions: int, api_key: str, index: int) -> list:
    """Call OpenRouter with a Gemini model."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": APP_NAME,
    }
    if APP_PUBLIC_URL:
        headers["HTTP-Referer"] = APP_PUBLIC_URL

    body = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.45,
        "max_tokens": 8192,
    }
    resp = requests.post(OPENROUTER_API_URL, headers=headers, json=body, timeout=60)
    provider = f"OpenRouter key #{index}"
    if not resp.ok:
        raise AIProviderError(provider, f"OpenRouter returned HTTP {resp.status_code}.", is_retryable_response(resp))

    data = resp.json()
    try:
        raw_text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise AIProviderError(provider, "OpenRouter did not return a usable response.") from exc

    return parse_questions(raw_text, num_questions)


def generate_quiz_from_text(text: str, num_questions: int = 10, source_type: str = "text") -> list:
    """Generate structured quiz data using Gemini first, then OpenRouter fallbacks."""
    prompt = build_quiz_prompt(text, num_questions, source_type)
    provider_errors = []

    providers = []
    if GEMINI_API_KEY:
        providers.append(("Gemini", lambda: request_gemini_quiz(prompt, num_questions)))
    providers.extend(
        (
            f"OpenRouter key #{index}",
            lambda key=key, index=index: request_openrouter_quiz(prompt, num_questions, key, index),
        )
        for index, key in enumerate(OPENROUTER_API_KEYS, start=1)
    )

    if not providers:
        raise RuntimeError(
            "No AI API keys are configured. Set GEMINI_API_KEY, OPENROUTER_API_KEY, "
            "or comma-separated OPENROUTER_API_KEYS in your hosting provider's environment variables."
        )

    for _, provider_call in providers:
        try:
            return provider_call()
        except (AIProviderError, requests.exceptions.RequestException, json.JSONDecodeError, ValueError) as exc:
            provider_errors.append(str(exc))
            if isinstance(exc, AIProviderError) and not exc.retryable:
                continue

    raise RuntimeError(
        "All configured AI providers failed or hit limits. Last errors: " + " | ".join(provider_errors[-3:])
    )


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
