# VCA: AI Voice Call Agent

A browser-based AI voice calling platform. You talk to AI personas (receptionist, interviewer, sales rep) over the web. It needs no Twilio and no telecom account.

## Stack
| Layer | Choice |
|---|---|
| Speech-to-text | Browser Web Speech API (Chrome/Edge) |
| Voice (TTS) | Microsoft neural voices via `edge-tts` (free, no key), streamed; browser voice as fallback |
| Backend | Python, FastAPI |
| LLM | Groq `qwen/qwen3.8-27b` (~0.2 s) with Gemini fallback, configured in `.env` |
| RAG | ChromaDB (local, persisted in `data/chroma`) |
| Database | SQLite (`data/vca.db`): users, calls, messages |

## Files
```
app.py          backend: auth, personas, RAG, LLM fallback chain, DB, admin API
index.html      the whole UI: login, persona picker, live call, admin dashboard
.env            your keys (copy from .env.example)
requirements.txt
```

## Run
```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # then fill in the API keys
uvicorn app:app --reload
```
Open http://localhost:8000.

- **User:** sign in with any email and password. A new email is registered automatically and gets a User ID.
- **Admin:** `admin@gmail.com` / `1234` opens the dashboard: live and past calls, users, full transcripts, the RAG context retrieved for each turn, and the model and latency of each reply.

The first start downloads Chroma's embedding model (~80 MB, one time only).

## How a turn works
1. The browser transcribes your speech and sends the text to `POST /api/calls/{id}/say`.
2. The server pulls the top 3 knowledge chunks for that persona from Chroma.
3. It builds a prompt from the persona, the voice rules, the knowledge and the last 12 messages, then calls the LLM. Each model in the chain gets `LLM_TIMEOUT` seconds before the next one is tried.
4. The reply and its metadata are saved to SQLite and sent back.
5. The browser streams the reply audio from `GET /api/tts`, which uses a neural voice per persona. Greetings and fillers are pre-rendered at startup, so they play instantly.

## Customize
To add or edit personas and their knowledge, change the `PERSONAS` dictionary in `app.py`. The knowledge is re-indexed into Chroma on startup.
