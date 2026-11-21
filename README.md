# VCA: Voice Calling Agents Hub

**Live demo: [vca.anshu.tech](https://vca.anshu.tech)**

VCA is a browser-based platform for AI voice calls. Pick an AI agent and talk to it by voice, as if it were a phone call. It runs entirely over the web, so you don't need Twilio or a telecom account. Each agent has its own personality, knowledge base and natural voice. Agents remember callers between calls and hang up on their own when the conversation is over.

> Open [vca.anshu.tech](https://vca.anshu.tech) in **Chrome or Edge**, sign in with any email and password, and press **Start call**.

---

## Features

### For callers
- **One login for everyone.** A new email creates an account automatically, with its own User ID.
- **Three agents to call:**

  | Agent | Role | What it does |
  |---|---|---|
  | **Maya** | Receptionist, Brightside Dental | Answers questions about hours and prices, and books, moves or cancels appointments |
  | **Arjun** | Interviewer | Runs an 8-stage screening interview for a Python backend role: project, Python, databases, design, a behavioural question, then wrap-up |
  | **Riya** | Sales rep, Nimbus CRM | Asks about your team, handles objections and books a demo |
- **Natural voice calls.** You speak and the agent answers in about 1–1.5 s with a human neural voice. Replies are short and conversational. To interrupt, tap the agent's avatar.
- **Memory.** Agents remember your name, appointments and earlier conversations, and greet you personally next time. For example: *"Hi Neha! How's that tooth feeling? You're booked for Monday at four."*
- **Automatic hang-up.** Say "bye" and the agent says goodbye and ends the call.
- **Call history and ratings.** Read past transcripts and rate each call from 1 to 5 stars.
- **Privacy controls.** Settings shows what each agent remembers about you. **Forget me** deletes all of it.

### For admins
- **Analytics:** active agents, total calls, average response time, satisfaction score, a 14-day call-volume chart, system health and per-agent statistics.
- **Calls:** live and past calls with full transcripts, which AI model answered each turn, its latency, the knowledge (RAG) it used and what the agent remembers about the caller.
- **Users:** every account with name, number of calls and memory notes.

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | **Python 3.11** with **FastAPI** and Uvicorn |
| Frontend | Plain **HTML, CSS and JavaScript** in a single page, with no framework or build step |
| Speech-to-text | Browser **Web Speech API** |
| Text-to-speech | Microsoft neural voices through **edge-tts**, streamed, with the browser voice as fallback |
| LLM | **Groq** (`qwen/qwen3.8-27b`, about 0.2 s per reply) as primary, falling back to **Google Gemini** |
| RAG | **ChromaDB** vector database, with **all-MiniLM-L6-v2** embeddings run directly on ONNX Runtime |
| Database | **SQLite**: users, calls, messages, ratings and memory |
| Hosting | **AWS EC2** (Ubuntu), **Caddy** (automatic HTTPS) and **systemd** |
| CI/CD | **GitHub Actions**: deploys on every push, with a health check and automatic rollback |

---

## How a call works

```
 Browser                                   Server (FastAPI)
 ───────                                   ────────────────
 You speak
   │ Web Speech API → text
   ├──────── POST /api/calls/{id}/say ───▶ 1. RAG: top-3 knowledge chunks from ChromaDB (~5 ms)
   │                                       2. Prompt = persona + voice rules + today's date
   │                                          + caller memory + knowledge + last 12 messages
   │                                       3. LLM: Groq → Gemini fallback chain (~0.2 s)
   │                                       4. Trim to a short spoken reply, detect goodbye
   │◀─────────────── reply text ────────── 5. Save the turn to SQLite
   │
   ├──────── GET /api/tts ───────────────▶ Neural voice streamed as it's generated
   │◀─────────────── audio/mpeg ──────────
 Agent speaks, then the browser listens again

 Call ends → the call is summarised in the background into the caller's memory
             (name + short notes with real dates), used on the next call
```

### Performance work
- **About 1.4 s** from the moment you stop talking to the agent's voice, down from about 9 s in the first version.
- **Faster RAG.** Running the embedding model directly on ONNX takes about 5 ms per query instead of about 350 ms through Chroma's default wrapper, and gives identical vectors.
- **Streamed voice.** Audio plays as soon as the first chunk arrives. Greetings and filler phrases are rendered when the server starts, so they play instantly.
- **Hedged TTS requests.** If the voice service hasn't started sending audio after 1.3 s, a second request goes out and whichever answers first is used. This avoids the service's occasional slow responses.
- **Fallbacks everywhere.** The LLM chain moves on to the next model after a timeout, TTS requests are retried, and the browser's own voice takes over as a last resort.

---

## Run locally

Requirements: Python 3.11, and a free API key from [Groq](https://console.groq.com/keys) and/or [Google AI Studio](https://aistudio.google.com/apikey).

```bash
git clone https://github.com/anshuu-prajapati/vca.git
cd vca
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # macOS/Linux: cp .env.example .env, then add your API keys
uvicorn app:app --reload
```

Then open **http://localhost:8000**. On the first start, Chroma downloads its embedding model (about 80 MB, one time only).

- **Caller:** sign in with any email and password.
- **Admin:** sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD` from your `.env` file.

### Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `groq` or `gemini`. Whichever you don't choose is used as the fallback |
| `GROQ_API_KEY`, `GROQ_MODEL` | Groq key and model |
| `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_FALLBACK_MODELS` | Gemini key, main model and backup models |
| `LLM_TIMEOUT` | Seconds each model gets before the next one is tried |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Admin login. **Change these before you deploy** |
| `SECRET_KEY` | Signs login tokens. Use a long random string |
| `TZ_OFFSET_MINUTES` | Timezone the agents use for dates (`330` is India, UTC+5:30) |

### Customize the agents
Agents are defined in the `PERSONAS` dictionary in `app.py`. Each entry has:
- **Personality:** name, role and prompt.
- **Greeting:** the opening line.
- **Voice:** neural voice, speed and pitch.
- **Knowledge:** the facts the agent can answer from.

The knowledge is re-indexed into ChromaDB every time the server starts.

---

## Project structure

```
app.py                         Backend: auth, agents, RAG, LLM fallback chain, voice streaming,
                               memory, call/rating/history APIs, admin analytics
index.html                     The whole UI: login, dashboard, call screen, history, settings, admin pages
requirements.txt               Python dependencies
.env.example                   Configuration template (copy to .env)
.github/workflows/deploy.yml   CI/CD: auto-deploy to vca.anshu.tech on every push to main
.github/vca-deploy.sh          Server-side deploy script with health check and automatic rollback
```

The SQLite database and the ChromaDB index are created automatically in `data/`, which is git-ignored.

---

## Deployment

Live at **[vca.anshu.tech](https://vca.anshu.tech)** on AWS EC2:

```
Internet ──HTTPS──▶ Caddy (automatic TLS) ──▶ 127.0.0.1:8010 ──▶ vca.service (uvicorn, Python 3.11)
                                                                    /opt/vca: app + its own .env + data/
```

Every push to `main` that changes `app.py`, `index.html` or `requirements.txt` deploys automatically:
1. **GitHub Actions** checks the Python syntax, packs the app files and sends them over SSH.
2. The deploy key can **only** run the deploy script on the server: no shell, and no access to anything else.
3. The script installs the files, installs any new Python packages and restarts the service.
4. It then **health-checks** the app and **rolls back automatically** if the new version doesn't come up.

To redeploy by hand, go to **Actions → Deploy → Run workflow**.
