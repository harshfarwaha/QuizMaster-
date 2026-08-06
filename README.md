# QuizMaster

Paste a webpage link in, get an AI-generated multiple-choice quiz out.

## How it works
1. You paste a URL into the site.
2. The Flask backend fetches that page and strips it down to readable text.
3. That text is sent to Claude (Anthropic's AI) with instructions to write multiple-choice
   questions based only on what's actually on the page.
4. The frontend runs you through the quiz with a timer, then shows your score and a
   review of right/wrong answers.

## Before you deploy: get an API key
This project calls the Anthropic API to generate questions, which needs a key that is
**yours** and **kept secret** (never put it in the frontend code or commit it to GitHub).

1. Go to https://console.anthropic.com and sign up / log in.
2. Go to **API Keys** and create a new key.
3. Anthropic API usage is pay-as-you-go — you'll need to add billing details. Each quiz
   generation costs a fraction of a cent, but it isn't free, so keep an eye on usage if
   you share the link publicly.
4. Copy the key somewhere safe — you'll paste it into your hosting provider's dashboard,
   not into any file in this project.

## Running it on your own computer (to test)
You'll need Python 3.10+ installed.

```bash
cd QuizMaster-web
pip install -r requirements.txt

# set your API key for this terminal session
# on Windows (PowerShell):
$env:ANTHROPIC_API_KEY="your-key-here"
# on Mac/Linux:
export ANTHROPIC_API_KEY="your-key-here"

python app.py
```

Then open http://localhost:5000 in your browser.

## Deploying it as a real, shareable website (Render.com — free tier)
GitHub Pages (where your portfolio lives) can't run this — it only hosts static files,
and this needs a live backend to call the AI safely. Render's free tier works well for
a small Flask app like this.

1. **Push this project to a new GitHub repo** (separate from your Portfolio repo):
   - Go to github.com → **+ → New repository** → name it `QuizMaster` → Public → Create
   - **Add file → Upload files** → drag in every file from this project → Commit

2. **Create a Render account** at https://render.com (free, sign up with GitHub is easiest)

3. **New Web Service**:
   - Click **New +** → **Web Service**
   - Connect your `QuizMaster` GitHub repo
   - Runtime: **Python 3**
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`

4. **Add your API key**:
   - In the service's **Environment** tab, add a variable:
     - Key: `ANTHROPIC_API_KEY`
     - Value: (paste the key you created earlier)

5. Click **Create Web Service**. Render will build and deploy it — takes a few minutes.
   You'll get a live URL like `https://quizmaster-xxxx.onrender.com`.

Note: Render's free tier "sleeps" after inactivity, so the first request after a while
takes ~30 seconds to wake up. That's normal, not a bug.

## Project structure
```
app.py                  Flask backend (scraping + AI call + API route)
templates/index.html    Frontend (single page, browser-based quiz UI)
requirements.txt        Python dependencies
Procfile                Tells the host how to start the app
.env.example            Template showing what env variable is needed
```
