import os
import re
import json
from flask import Flask, request, jsonify, render_template
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


def scrape_text(url: str) -> str:
    """Fetch a URL and return cleaned, readable text content from the page."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; QuizMasterBot/1.0)"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strip elements that aren't real page content
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "iframe", "svg", "form"]):
        tag.decompose()

    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()

    # Cap length to keep the AI prompt a reasonable size / cost
    return text[:8000]


def generate_quiz_from_text(text: str, num_questions: int = 5) -> list:
    """Call the Google Gemini API to turn page text into structured MCQ quiz data."""
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "Server is missing a GEMINI_API_KEY environment variable. "
            "Set it in your hosting provider's dashboard."
        )

    prompt = f"""You are creating a multiple-choice quiz based on webpage content.

Content:
\"\"\"{text}\"\"\"

Create exactly {num_questions} multiple-choice questions based ONLY on facts present in the content above.
Each question must have exactly 4 answer options with only one correct answer.
Keep questions clear and unambiguous. Do not invent facts not present in the content.

Respond with ONLY valid JSON (no markdown fences, no extra text) in this exact structure:
[
  {{
    "question": "string",
    "options": ["string", "string", "string", "string"],
    "correct_index": 0,
    "explanation": "one short sentence explaining the correct answer"
  }}
]"""

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6},
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

    # Strip markdown code fences if the model added them anyway
    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```(json)?", "", raw_text).strip()
        raw_text = re.sub(r"```$", "", raw_text).strip()

    questions = json.loads(raw_text)

    # Basic shape validation so a malformed AI response fails loudly, not silently
    for q in questions:
        if not all(k in q for k in ("question", "options", "correct_index")):
            raise ValueError("AI response was missing required quiz fields.")
        if len(q["options"]) != 4:
            raise ValueError("AI response did not return exactly 4 options for a question.")

    return questions


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/generate-quiz", methods=["POST"])
def api_generate_quiz():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    try:
        num_questions = int(data.get("num_questions", 5))
    except (TypeError, ValueError):
        num_questions = 5
    num_questions = max(3, min(num_questions, 10))

    if not url.startswith("http://") and not url.startswith("https://"):
        return jsonify({"error": "Please enter a valid URL starting with http:// or https://"}), 400

    try:
        text = scrape_text(url)
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Could not reach that page: {e}"}), 400

    if len(text) < 200:
        return jsonify({"error": "That page doesn't have enough readable text to build a quiz from."}), 400

    try:
        questions = generate_quiz_from_text(text, num_questions)
    except json.JSONDecodeError:
        return jsonify({"error": "The AI response could not be parsed. Please try again."}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"questions": questions, "source_url": url})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
