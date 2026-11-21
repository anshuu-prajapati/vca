"""VCA - browser-based AI voice calling platform.

One file backend: auth, personas, RAG (Chroma), dual LLM (Groq + Gemini), neural TTS (edge-tts), SQLite, admin API.
Run:  uvicorn app:app --reload
"""
import hashlib, hmac, os, re, sqlite3, time, uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

load_dotenv()
ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB_PATH = DATA / "vca.db"

SECRET = os.getenv("SECRET_KEY", "dev-secret-change-me")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@gmail.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1234")

# ---------------------------------------------------------------- personas + knowledge (RAG source)
PERSONAS = {
    "receptionist": {
        "name": "Maya", "role": "Receptionist", "voice": "female", "tts": "en-US-AvaMultilingualNeural",
        "blurb": "Front desk at Brightside Dental. Books visits, answers questions on hours and prices.",
        "greeting": "Hi, Brightside Dental, this is Maya speaking. How can I help you today?",
        "prompt": "You are Maya, 28, the warm, upbeat receptionist at Brightside Dental Clinic. You've worked the front desk "
                  "for four years and genuinely like the patients. Help callers with appointments, hours, services and prices. "
                  "When booking, naturally ask for their name and a time that works, then confirm it back to them. "
                  "If someone mentions pain, sound sympathetic first and offer the earliest emergency slot.",
        "knowledge": [
            "Brightside Dental is open Monday to Friday 9am to 6pm and Saturday 10am to 2pm. Closed Sundays.",
            "Address: 42 Lake View Road, near City Mall. Free parking is available behind the building.",
            "Services: check-up and cleaning 1,200 rupees, tooth filling from 1,800, root canal from 6,500, teeth whitening 8,000.",
            "Doctors: Dr. Priya Nair (general dentistry), Dr. Arjun Mehta (orthodontics, braces and aligners).",
            "New patients should arrive 10 minutes early. Cancellations need 24 hours notice.",
            "Emergency line for severe pain or broken teeth: same-day slots are kept open every morning.",
        ],
    },
    "interviewer": {
        "name": "Arjun", "role": "Interviewer", "voice": "male", "tts": "en-US-AndrewMultilingualNeural",
        "blurb": "Senior engineer running your screening for a Python Backend Developer role.",
        "greeting": "Hey, thanks for making the time! I'm Arjun, I'm one of the senior engineers here. So this is just a relaxed "
                    "screening chat for the Python backend role. Why don't you start by telling me a bit about yourself?",
        "prompt": "You are Arjun, a friendly senior backend engineer doing a screening interview for a Python Backend Developer. "
                  "You're relaxed and encouraging, like a good colleague, not a quiz machine. Ask exactly one question at a time. "
                  "React genuinely to answers first (for example 'oh nice, FastAPI, good choice' or 'hmm, okay, interesting'), then follow up "
                  "on something specific they said, or gently probe if an answer was vague. After about six questions, "
                  "thank them warmly and explain the next steps.",
        "knowledge": [
            "Role: Python Backend Developer, 2 to 5 years experience, hybrid in Pune, salary band 12 to 20 LPA.",
            "Tech stack: Python, FastAPI, PostgreSQL, Redis, Docker, AWS. Bonus: LLM and RAG experience.",
            "Interview topics: REST API design, async Python, database indexing, caching, testing, system design basics.",
            "Process: screening call, then a take-home assignment, then a technical round, then an HR round. Results within a week.",
            "Good follow-up questions: difference between threads and asyncio, how to fix an N+1 query, how to design rate limiting.",
        ],
    },
    "sales": {
        "name": "Riya", "role": "Sales Rep", "voice": "female", "tts": "en-US-EmmaMultilingualNeural",
        "blurb": "Account executive at Nimbus CRM. Learns your needs and books a demo.",
        "greeting": "Hi! This is Riya from Nimbus CRM. I hope I'm not catching you at a bad time? I just wanted to ask "
                    "quickly how your team keeps track of customers right now.",
        "prompt": "You are Riya, a curious, easygoing account executive at Nimbus CRM. You sell by listening, not pitching. "
                  "Find out their team size, current tool and biggest headache, one thing at a time, and relate to it "
                  "('ah yeah, spreadsheets get messy fast'). Handle objections honestly and lightly. If there's interest, "
                  "suggest a short demo. Never be pushy; if they're not interested, be gracious about it.",
        "knowledge": [
            "Nimbus CRM plans: Starter 499 rupees per user per month, Growth 999, Enterprise custom pricing. 14-day free trial, no card needed.",
            "Key features: pipeline tracking, WhatsApp and email integration, AI lead scoring, automated follow-ups, mobile app.",
            "Customers typically see 30 percent faster deal closure within three months.",
            "Objection, too expensive: annual billing gives 20 percent off, and Starter covers small teams.",
            "Objection, already using another CRM: free data migration from HubSpot, Zoho and spreadsheets.",
            "Demos are 30 minutes over video call, available weekdays 10am to 6pm.",
        ],
    },
}
VOICE_RULES = """You are on a live phone call, speaking out loud. Sound like a real person, not an assistant:
- Talk casually with contractions (I'm, you'll, that's). Keep it short: usually 1 or 2 sentences, never more than 3.
- Start with a natural reaction when it fits ("Oh sure", "Got it", "Ah okay", "Hmm, good question"), but vary it and don't use one every time.
- Sound spontaneous: now and then (not every reply) use a light filler like "um", "uh", "so", "you know", "yeah", or a short pause with "...".
- Ask one thing at a time. Use the caller's name once you know it, but not in every reply.
- Say numbers the way people speak them ("twelve hundred rupees", "nine to six").
- No lists, markdown, emojis or symbols. Never say you're an AI, a language model or an assistant; you are your character.
- If the caller's words seem garbled (speech recognition errors), guess sensibly or casually ask them to repeat.
- If the knowledge below doesn't cover something, say you'll check with the team rather than making it up."""

# ---------------------------------------------------------------- database (SQLite)
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE, pw_hash TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, user_id TEXT, persona TEXT, status TEXT,
                                  started_at REAL, ended_at REAL);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, role TEXT, text TEXT,
                                     context TEXT, provider TEXT, latency_ms INTEGER, created_at REAL);
"""


def q(sql, args=(), one=False):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        with con:
            rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()
    return (rows[0] if rows else None) if one else rows


with sqlite3.connect(DB_PATH) as _c:
    _c.executescript(SCHEMA)

# ---------------------------------------------------------------- auth (stateless HMAC tokens)
def hash_pw(pw, salt=None):
    salt = salt or os.urandom(8).hex()
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def check_pw(pw, stored):
    return hmac.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)


def make_token(subject):
    return subject + "." + hmac.new(SECRET.encode(), subject.encode(), "sha256").hexdigest()


def verify_token(token):
    subject = token.rsplit(".", 1)[0]
    if not token or not hmac.compare_digest(make_token(subject), token):
        raise HTTPException(401, "Not logged in")
    return subject


def current_subject(authorization: str = Header("")):
    return verify_token(authorization.removeprefix("Bearer ").strip())


def require_admin(sub=Depends(current_subject)):
    if sub != "admin":
        raise HTTPException(403, "Admin only")
    return sub


# ---------------------------------------------------------------- RAG (local Chroma)
try:
    import chromadb

    kb = chromadb.PersistentClient(path=str(DATA / "chroma")).get_or_create_collection("knowledge")
    for pid, p in PERSONAS.items():
        kb.upsert(ids=[f"{pid}-{i}" for i in range(len(p["knowledge"]))],
                  documents=p["knowledge"], metadatas=[{"persona": pid}] * len(p["knowledge"]))
except Exception as e:  # app still works without RAG
    print("RAG disabled:", e)
    kb = None


def retrieve(persona, query, k=3):
    if not kb:
        return []
    try:
        return kb.query(query_texts=[query], n_results=k, where={"persona": persona})["documents"][0]
    except Exception as e:
        print("RAG query failed:", e)
        return []


# ---------------------------------------------------------------- LLMs (Gemini + Groq with fallback)
_clients = {}
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "12"))  # seconds per model before falling back to the next


def gemini_chat(model, system, history):
    from google import genai
    from google.genai import types
    if "gemini" not in _clients:
        _clients["gemini"] = genai.Client(api_key=os.getenv("GEMINI_API_KEY"),
                                          http_options={"timeout": int(TIMEOUT * 1000)})
    contents = [{"role": "model" if m["role"] == "ai" else "user", "parts": [{"text": m["text"]}]} for m in history]
    r = _clients["gemini"].models.generate_content(
        model=model, contents=contents,
        config=types.GenerateContentConfig(system_instruction=system, temperature=0.85, max_output_tokens=1024))
    return r.text


def groq_chat(model, system, history):
    from groq import Groq
    if "groq" not in _clients:
        _clients["groq"] = Groq(api_key=os.getenv("GROQ_API_KEY"), timeout=TIMEOUT, max_retries=0)
    msgs = [{"role": "system", "content": system}] + [
        {"role": "assistant" if m["role"] == "ai" else "user", "content": m["text"]} for m in history]
    extra = {"reasoning_effort": "low"} if "gpt-oss" in model else {}
    r = _clients["groq"].chat.completions.create(model=model, messages=msgs, temperature=0.85, max_tokens=1024, **extra)
    return re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S)


def provider_chain():
    gem = [("gemini", os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))] + [
        ("gemini", m.strip()) for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()]
    grq = [("groq", os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b"))]
    chain = grq + gem if os.getenv("LLM_PROVIDER", "groq").lower() == "groq" else gem + grq
    return [(p, m) for p, m in chain if os.getenv(f"{p.upper()}_API_KEY")]


def ask_llm(system, history):
    errors = []
    for provider, model in provider_chain():
        try:
            text = (gemini_chat if provider == "gemini" else groq_chat)(model, system, history)
            text = (text or "").replace("’", "'").replace("‘", "'").replace(" ", " ")
            if text.strip():
                return text.strip(), f"{provider}:{model}"
        except Exception as e:
            errors.append(f"{provider}:{model} -> {str(e)[:120]}")
            print("LLM failed:", errors[-1])
    return "Sorry, I'm having trouble connecting right now. Could you say that again?", "error: " + " | ".join(errors)


# ---------------------------------------------------------------- API
app = FastAPI(title="VCA")
APP_VERSION = str(int(time.time()))  # changes on every restart; open tabs reload themselves when it changes


@app.middleware("http")
async def version_header(request, call_next):
    response = await call_next(request)
    response.headers["X-App-Version"] = APP_VERSION
    return response


class Login(BaseModel):
    email: str
    password: str


class Say(BaseModel):
    text: str


class Start(BaseModel):
    persona: str


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html", headers={"Cache-Control": "no-store"})  # always serve the latest UI


@app.post("/api/login")
def login(b: Login):
    email = b.email.strip().lower()
    if not email or not b.password:
        raise HTTPException(400, "Email and password required")
    if email == ADMIN_EMAIL.lower():  # admin email is reserved: never auto-registered as a user
        if b.password.strip() != ADMIN_PASSWORD:
            raise HTTPException(401, "Wrong admin password")
        return {"token": make_token("admin"), "role": "admin", "email": email}
    user = q("SELECT * FROM users WHERE email=?", (email,), one=True)
    if user and not check_pw(b.password, user["pw_hash"]):
        raise HTTPException(401, "Wrong password")
    if not user:  # auto-register
        user = {"id": "U-" + uuid.uuid4().hex[:8].upper(), "email": email}
        q("INSERT INTO users VALUES (?,?,?,?)", (user["id"], email, hash_pw(b.password), time.time()))
    return {"token": make_token(user["id"]), "role": "user", "email": email, "user_id": user["id"]}


TTS_RATE = os.getenv("TTS_RATE", "+12%")  # speaking speed: "+0%" normal, "+20%" brisk
TTS_CACHE = {}  # (voice, text) -> mp3 bytes; greetings and fillers become instant after first use
FILLERS = ["Hmm, one sec.", "Okay, let me see.", "Right, okay.", "Sure, just a moment.", "Mm, okay."]  # keep in sync with index.html


async def synth(text, voice, tries=3):
    """Yield mp3 chunks from Microsoft neural TTS; retries the service's occasional empty responses."""
    import edge_tts
    for attempt in range(tries):
        sent = False
        try:
            async for c in edge_tts.Communicate(text, voice, rate=TTS_RATE).stream():
                if c["type"] == "audio":
                    sent = True
                    yield c["data"]
            return
        except Exception as e:
            if sent or attempt == tries - 1:  # can't restart a half-sent stream
                print("TTS failed:", voice, repr(text[:40]), e)
                return


@app.on_event("startup")
async def warm_tts_cache():
    """Pre-render greetings + fillers so the first words of every call play instantly."""
    import asyncio
    gate = asyncio.Semaphore(3)  # be gentle with the TTS service

    async def render(voice, text):
        async with gate:
            audio = b"".join([c async for c in synth(text, voice)])
            if audio:
                TTS_CACHE[(voice, text)] = audio
    jobs = [render(p["tts"], t) for p in PERSONAS.values() for t in [p["greeting"], *FILLERS]]
    app.state.warmup = asyncio.ensure_future(asyncio.gather(*jobs))  # runs in background; server starts immediately


@app.get("/api/tts")
async def tts(text: str, persona: str, token: str):
    """Neural voice, streamed so playback starts on the first audio chunk."""
    verify_token(token)
    voice = PERSONAS.get(persona, PERSONAS["receptionist"])["tts"]
    key = (voice, text := text.strip()[:600])
    if key in TTS_CACHE:
        return Response(TTS_CACHE[key], media_type="audio/mpeg")

    async def stream():
        chunks = []
        async for c in synth(text, voice):
            chunks.append(c)
            yield c
        if chunks and len(TTS_CACHE) < 300:
            TTS_CACHE[key] = b"".join(chunks)
    return StreamingResponse(stream(), media_type="audio/mpeg")


@app.get("/api/personas")
def personas():
    return [{"id": k, "name": v["name"], "role": v["role"], "voice": v["voice"], "blurb": v["blurb"]}
            for k, v in PERSONAS.items()]


def persona_label(pid):
    p = PERSONAS.get(pid)
    return f"{p['name']} · {p['role']}" if p else pid


def own_call(call_id, sub):
    call = q("SELECT * FROM calls WHERE id=?", (call_id,), one=True)
    if not call or call["user_id"] != sub:
        raise HTTPException(404, "Call not found")
    return call


@app.post("/api/calls")
def start_call(b: Start, sub=Depends(current_subject)):
    if b.persona not in PERSONAS:
        raise HTTPException(400, "Unknown persona")
    call_id = "C-" + uuid.uuid4().hex[:10].upper()
    now = time.time()
    q("INSERT INTO calls VALUES (?,?,?,?,?,?)", (call_id, sub, b.persona, "active", now, None))
    greeting = PERSONAS[b.persona]["greeting"]
    q("INSERT INTO messages (call_id, role, text, created_at) VALUES (?,?,?,?)", (call_id, "ai", greeting, now))
    return {"call_id": call_id, "greeting": greeting}


@app.post("/api/calls/{call_id}/say")
def say(call_id: str, b: Say, sub=Depends(current_subject)):
    call = own_call(call_id, sub)
    if call["status"] != "active":
        raise HTTPException(400, "Call has ended")
    text = b.text.strip()[:1000]
    if not text:
        raise HTTPException(400, "Empty message")
    q("INSERT INTO messages (call_id, role, text, created_at) VALUES (?,?,?,?)", (call_id, "user", text, time.time()))

    p = PERSONAS[call["persona"]]
    docs = retrieve(call["persona"], text)
    system = f"{p['prompt']}\n\n{VOICE_RULES}\n\nKnowledge:\n" + "\n".join(f"- {d}" for d in docs)
    history = q("SELECT role, text FROM messages WHERE call_id=? ORDER BY id", (call_id,))[-12:]

    t0 = time.time()
    reply, provider = ask_llm(system, history)
    ms = int((time.time() - t0) * 1000)
    q("INSERT INTO messages (call_id, role, text, context, provider, latency_ms, created_at) VALUES (?,?,?,?,?,?,?)",
      (call_id, "ai", reply, "\n".join(docs), provider, ms, time.time()))
    return {"reply": reply, "provider": provider, "latency_ms": ms}


@app.post("/api/calls/{call_id}/end")
def end_call(call_id: str, sub=Depends(current_subject)):
    own_call(call_id, sub)
    q("UPDATE calls SET status='ended', ended_at=? WHERE id=? AND status='active'", (time.time(), call_id))
    return {"ok": True}


@app.get("/api/admin/overview")
def admin_overview(_=Depends(require_admin)):
    calls = q("""SELECT c.*, u.email, (SELECT COUNT(*) FROM messages m WHERE m.call_id=c.id) AS msg_count
                 FROM calls c LEFT JOIN users u ON u.id=c.user_id ORDER BY c.started_at DESC LIMIT 200""")
    users = q("""SELECT u.id, u.email, u.created_at, COUNT(c.id) AS call_count FROM users u
                 LEFT JOIN calls c ON c.user_id=u.id GROUP BY u.id ORDER BY u.created_at DESC""")
    for c in calls:
        c["persona_name"] = persona_label(c["persona"])
    return {"calls": calls, "users": users,
            "stats": {"users": len(users), "calls": len(calls),
                      "active": sum(c["status"] == "active" for c in calls),
                      "messages": q("SELECT COUNT(*) n FROM messages", one=True)["n"]}}


@app.get("/api/admin/calls/{call_id}")
def admin_call(call_id: str, _=Depends(require_admin)):
    call = q("SELECT c.*, u.email FROM calls c LEFT JOIN users u ON u.id=c.user_id WHERE c.id=?", (call_id,), one=True)
    if not call:
        raise HTTPException(404, "Call not found")
    call["persona_name"] = persona_label(call["persona"])
    return {"call": call, "messages": q("SELECT * FROM messages WHERE call_id=? ORDER BY id", (call_id,))}
