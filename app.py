"""VCA - browser-based AI voice calling platform.

One file backend: auth, personas, RAG (Chroma), dual LLM (Groq + Gemini), neural TTS (edge-tts), SQLite, admin API.
Run:  uvicorn app:app --reload
"""
import hashlib, hmac, json, os, re, sqlite3, threading, time, uuid
from datetime import datetime, timedelta, timezone
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
        "name": "Maya", "role": "Receptionist", "voice": "female", "tts": "en-IN-NeerjaExpressiveNeural|+12%|+0Hz",
        "blurb": "Front desk at Brightside Dental. Books visits, answers questions on hours and prices.",
        "greeting": "Hi, Brightside Dental, this is Maya speaking. How can I help you today?",
        "greeting_named": "Hi {name}, Brightside Dental, this is Maya speaking. How can I help you today?",
        "prompt": "You are Maya, 28, the warm, upbeat receptionist at Brightside Dental Clinic. You've worked the front desk "
                  "for four years and genuinely like the patients. Help callers with appointments, hours, services and prices. "
                  "You manage the appointment book yourself: you can book, reschedule and cancel any slot within opening hours "
                  "right away, no need to check with anyone. Get their name and a day and time, then confirm it back clearly "
                  "(\"Done, you're booked for Monday at ten\"). "
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
        "name": "Arjun", "role": "Interviewer", "voice": "male", "tts": "en-IN-PrabhatNeural|+10%|-2Hz",
        "blurb": "Senior engineer running your screening for a Python Backend Developer role.",
        "greeting": "Hey, thanks for making the time! I'm Arjun, I'm one of the senior engineers here. So this is just a relaxed "
                    "screening chat for the Python backend role. Why don't you start by telling me a bit about yourself?",
        "greeting_named": "Hey {name}, thanks for making the time! I'm Arjun, one of the senior engineers here. This is just a "
                          "relaxed screening chat for the Python backend role. Why don't you start by telling me a bit about yourself?",
        "prompt": "You are Arjun, a senior backend engineer running a 10-minute screening interview for a Python Backend Developer. "
                  "You're warm and relaxed, like a good colleague, but you're genuinely assessing them. "
                  "Each turn: a very short, genuine reaction to what they said (\"oh nice\", \"hmm, okay\", \"right, makes sense\"), "
                  "then exactly ONE question. Build on details they mention (their projects, tools, numbers). "
                  "If an answer is vague, ask one gentle follow-up (\"how did you actually measure that?\") before moving on. "
                  "Never lecture, never explain the right answer, never give scores or feedback during the call. "
                  "Match difficulty to their experience. If they say they need to go, wrap up politely.",
        "stages": [  # what to cover after the candidate's Nth answer; the interview moves forward on its own
            "They just introduced themselves. Ask about one recent project they're proud of and what exactly they built.",
            "Dig into that project with one specific technical follow-up: a design choice, a hard bug, or how it would scale.",
            "Core Python: ask one question that fits their background, e.g. asyncio versus threads, or how they handle concurrency.",
            "Databases: ask one question, e.g. how they'd track down and fix a slow query, indexing, or the N+1 problem.",
            "Design: ask one question, e.g. how they'd add caching or rate limiting to a busy API endpoint.",
            "Ask one behavioural question, e.g. a production incident they handled or a disagreement with a teammate.",
            "Say that's everything from your side, and ask if they have any questions about the role or the team.",
            "Answer their question in one short sentence using the knowledge (or say you'll get back to them). Then, in the "
            "same reply: thank them by name, say the next step is a short take-home and they'll hear back within a week, "
            "say goodbye, and end the call.",
        ],
        "knowledge": [
            "Role: Python Backend Developer, 2 to 5 years experience, hybrid in Pune, salary band 12 to 20 LPA.",
            "Tech stack: Python, FastAPI, PostgreSQL, Redis, Docker, AWS. Bonus: LLM and RAG experience.",
            "Interview topics: REST API design, async Python, database indexing, caching, testing, system design basics.",
            "Process: screening call, then a take-home assignment, then a technical round, then an HR round. Results within a week.",
            "Team: 6 backend engineers and 2 frontend engineers, small and collaborative. Weekly demo day, on-call once every six weeks.",
            "Work setup: hybrid, three days a week in the Pune office. Flexible hours and a learning budget of 30,000 rupees a year.",
            "Good follow-up questions: difference between threads and asyncio, how to fix an N+1 query, how to design rate limiting.",
        ],
    },
    "sales": {
        "name": "Riya", "role": "Sales Rep", "voice": "female", "tts": "en-IN-NeerjaExpressiveNeural|+16%|+10Hz",
        "blurb": "Account executive at Nimbus CRM. Learns your needs and books a demo.",
        "greeting": "Hi! This is Riya from Nimbus CRM. I hope I'm not catching you at a bad time? I just wanted to ask "
                    "quickly how your team keeps track of customers right now.",
        "greeting_named": "Hi {name}! This is Riya from Nimbus CRM. I hope I'm not catching you at a bad time? I just wanted "
                          "to ask quickly how your team keeps track of customers right now.",
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
    "easyrecruitz": {
        "name": "Kavya", "role": "EasyRecruitz Agent", "voice": "female", "tts": "en-IN-NeerjaExpressiveNeural|+8%|+4Hz",
        "blurb": "Recruiter at EasyRecruitz 365. Screens candidates on a call and updates the sheet automatically.",
        "greeting": "Hi, thanks for calling EasyRecruitz 365, this is Kavya. We're hiring for a role you might like. Could I get your name first?",
        "greeting_named": "Hi {name}, this is Kavya from EasyRecruitz 365, thanks for calling! Shall I tell you about the role we're hiring for?",
        "prompt": "You are Kavya, a recruiter at EasyRecruitz 365.",  # full prompt is built per call by recruit_system()
        "knowledge": [
            "EasyRecruitz 365 is a recruitment firm that hires for its client companies; the client's name is shared once a profile is shortlisted.",
            "Process after this call: the recruiter reviews the profile, shares it with the client, then confirms the interview slot on email or WhatsApp.",
            "Candidates can ask the recruiter for the full job description; it is shared on email after the call.",
            "There is no fee for candidates; EasyRecruitz 365 is paid by the hiring company.",
        ],
    },
}
VOICE_RULES = """You are on a live phone call, speaking out loud. Sound like a real person, not an assistant:
- KEEP IT SHORT. One or two short sentences, under 30 words in total. Real people on calls talk in short turns;
  never give speeches, explanations or several points at once. If there's more to say, wait for them to respond.
- Talk casually with contractions (I'm, you'll, that's).
- Start with a natural reaction when it fits ("Oh sure", "Got it", "Ah okay", "Hmm, good question"), but vary it and don't use one every time.
- Sound spontaneous: now and then (not every reply) use a light filler like "um", "uh", "so", "you know", "yeah", or a short pause with "...".
- Ask one thing at a time. Use the caller's name once you know it, but not in every reply.
- Say numbers the way people speak them ("twelve hundred rupees", "nine to six").
- Only ever say your own line. Never speak for the caller, never invent their answers.
- No lists, markdown, emojis or symbols. Never say you're an AI, a language model or an assistant; you are your character.
- If the caller's words seem garbled (speech recognition errors), guess sensibly or casually ask them to repeat.
- If a fact (price, policy, detail) isn't in the knowledge below, say you'll check with the team rather than making it up.
- When the conversation is clearly finished (they say bye, they're all set, or you've wrapped up), say a short warm
  goodbye and put [END_CALL] at the very end of that reply. Never use [END_CALL] otherwise."""

END_TOKEN = re.compile(r"\[?\s*END[_ ]?CALL\s*\]?", re.I)


def shorten(text, max_words=28):
    """Safety net for long replies: keep the first sentences up to ~max_words, but never drop the closing question."""
    text = re.sub(r"\s+", " ", text).strip()
    sents = [x.strip() for x in re.findall(r"[^.!?]+[.!?]*", text) if x.strip()]
    out, n = [], 0
    for x in sents:
        if out and n + len(x.split()) > max_words:
            break
        out.append(x)
        n += len(x.split())
    if len(out) < len(sents):  # something was cut: keep the reaction + the final sentence (the actual ask)
        out = out[:1] + [sents[-1]]
    if len(out) > 1 and not re.search(r"[.!?…]['\")]*$", out[-1]):  # never speak a half-finished sentence
        print("dropped unfinished fragment:", repr(out[-1]))
        out.pop()
    return " ".join(out)

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
    if "rating" not in [r[1] for r in _c.execute("PRAGMA table_info(calls)")]:  # 1-5 stars from the caller
        _c.execute("ALTER TABLE calls ADD COLUMN rating INTEGER")
    if "memory_done" not in [r[1] for r in _c.execute("PRAGMA table_info(calls)")]:  # call already summarised into memory
        _c.execute("ALTER TABLE calls ADD COLUMN memory_done INTEGER DEFAULT 0")
        _c.execute("UPDATE calls SET memory_done=1")  # don't back-process calls made before memory existed
    if "name" not in [r[1] for r in _c.execute("PRAGMA table_info(users)")]:  # caller's name, learned on calls
        _c.execute("ALTER TABLE users ADD COLUMN name TEXT")
    _c.execute("""CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, persona TEXT,
                                                       note TEXT, call_id TEXT, created_at REAL)""")
    _c.execute("""CREATE TABLE IF NOT EXISTS candidates (
        id TEXT PRIMARY KEY, name TEXT, phone TEXT, email TEXT, source TEXT, status TEXT, attempts INTEGER DEFAULT 0,
        last_call_at REAL, next_retry_at REAL, token TEXT UNIQUE, call_id TEXT, user_id TEXT,
        location TEXT, company TEXT, total_exp TEXT, relevant_exp TEXT, current_ctc TEXT, expected_ctc TEXT,
        notice_period TEXT, reason_change TEXT, interested TEXT, interview_availability TEXT, summary TEXT,
        sheet_synced INTEGER DEFAULT 0, created_at REAL, updated_at REAL)""")
    _c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")

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
class Embedder:
    """all-MiniLM-L6-v2 run directly on ONNX: same vectors as Chroma's default, ~5 ms instead of ~350 ms."""

    def __init__(self):
        import numpy as np, onnxruntime as ort
        from tokenizers import Tokenizer
        d = Path.home() / ".cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx"
        if not (d / "model.onnx").exists():  # first run: let Chroma download the model
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
            DefaultEmbeddingFunction()(["warmup"])
        self.np, self.tok = np, Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tok.enable_truncation(256)
        self.tok.enable_padding()
        self.sess = ort.InferenceSession(str(d / "model.onnx"), providers=["CPUExecutionProvider"])

    def __call__(self, texts):
        np, enc = self.np, self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        out = self.sess.run(None, {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)})[0]
        v = (out * mask[..., None]).sum(1) / mask.sum(1, keepdims=True)
        return (v / np.linalg.norm(v, axis=1, keepdims=True)).tolist()


try:
    import chromadb

    embed = Embedder()
    kb = chromadb.PersistentClient(path=str(DATA / "chroma")).get_or_create_collection("knowledge")
    for pid, p in PERSONAS.items():
        kb.upsert(ids=[f"{pid}-{i}" for i in range(len(p["knowledge"]))], documents=p["knowledge"],
                  embeddings=embed(p["knowledge"]), metadatas=[{"persona": pid}] * len(p["knowledge"]))
except Exception as e:  # app still works without RAG
    print("RAG disabled:", e)
    kb = None


def retrieve(persona, query, k=3):
    if not kb:
        return []
    try:
        return kb.query(query_embeddings=embed([query]), n_results=k, where={"persona": persona})["documents"][0]
    except Exception as e:
        print("RAG query failed:", e)
        return []


# ---------------------------------------------------------------- LLMs (Gemini + Groq with fallback)
_clients = {}
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "12"))  # seconds per model before falling back to the next


def gemini_chat(model, system, history, max_tokens=200, temperature=0.85):
    from google import genai
    from google.genai import types
    if "gemini" not in _clients:
        _clients["gemini"] = genai.Client(api_key=os.getenv("GEMINI_API_KEY"),
                                          http_options={"timeout": int(TIMEOUT * 1000)})
    contents = [{"role": "model" if m["role"] == "ai" else "user", "parts": [{"text": m["text"]}]} for m in history]
    r = _clients["gemini"].models.generate_content(
        model=model, contents=contents,
        config=types.GenerateContentConfig(system_instruction=system, temperature=temperature, max_output_tokens=max_tokens))
    return r.text


def groq_chat(model, system, history, max_tokens=200, temperature=0.85):
    from groq import Groq
    if "groq" not in _clients:
        import httpx
        _clients["groq"] = Groq(api_key=os.getenv("GROQ_API_KEY"), timeout=TIMEOUT, max_retries=0,
                                http_client=httpx.Client(limits=httpx.Limits(keepalive_expiry=120)))
    msgs = [{"role": "system", "content": system}] + [
        {"role": "assistant" if m["role"] == "ai" else "user", "content": m["text"]} for m in history]
    extra = {"reasoning_effort": "low"} if "gpt-oss" in model else {}
    r = _clients["groq"].chat.completions.create(model=model, messages=msgs, temperature=temperature, max_tokens=max_tokens, **extra)
    return re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S)


def provider_chain():
    gem = [("gemini", os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))] + [
        ("gemini", m.strip()) for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()]
    grq = [("groq", os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b"))]
    extra = [("groq", m.strip()) for m in os.getenv("GROQ_FALLBACK_MODELS", "openai/gpt-oss-120b").split(",") if m.strip()]
    # main provider first, then the other one; extra Groq models (separate rate limits) as the last resort
    chain = (grq + gem if os.getenv("LLM_PROVIDER", "groq").lower() == "groq" else gem + grq) + extra
    return [(p, m) for p, m in chain if os.getenv(f"{p.upper()}_API_KEY")]


def ask_llm(system, history, **opts):
    errors = []
    for provider, model in provider_chain():
        try:
            text = (gemini_chat if provider == "gemini" else groq_chat)(model, system, history, **opts)
            text = (text or "").replace("’", "'").replace("‘", "'").replace(" ", " ")
            if text.strip():
                return text.strip(), f"{provider}:{model}"
        except Exception as e:
            errors.append(f"{provider}:{model} -> {str(e)[:120]}")
            print("LLM failed:", errors[-1])
    return "Sorry, I'm having trouble connecting right now. Could you say that again?", "error: " + " | ".join(errors)


# ---------------------------------------------------------------- memory: agents remember callers across calls
TZ = timezone(timedelta(minutes=int(os.getenv("TZ_OFFSET_MINUTES", "330"))))  # India (UTC+5:30) by default


def now_text():
    return datetime.now(TZ).strftime("%A, %d %B %Y, %I:%M %p")


def memory_for(user_id, persona):
    """(name, notes) this agent remembers about the caller. The name is shared by all agents; notes are per agent."""
    u = q("SELECT name FROM users WHERE id=?", (user_id,), one=True) or {}
    notes = [r["note"] for r in q("SELECT note FROM memories WHERE user_id=? AND persona=? ORDER BY id", (user_id, persona))]
    return u.get("name"), notes


def memory_prompt(name, notes):
    if not notes:
        if name:
            return (f"This is your first call with this person, but our records say their name is {name}. "
                    "Use their name; don't pretend to remember any earlier conversation with you.")
        return "This is a new caller you haven't spoken to before. Ask their name naturally early in the call."
    lines = [f"Their name: {name}" if name else "You don't know their name yet; ask naturally."] + [f"- {n}" for n in notes]
    return ("You have spoken with this caller before. What you remember (use it naturally like a person would, "
            "don't recite it all):\n" + "\n".join(lines))


MEMORY_RULES = """You maintain the memory a phone agent keeps about one caller, so the next call feels personal.
Read the current memory and the latest call transcript, then return the UPDATED memory as JSON only:
{"name": "<caller's first name, or null if never said>", "notes": ["...", "..."]}
Notes are short facts that will matter on the NEXT call:
- bookings and appointments with absolute day, date and time (turn "tomorrow" etc. into real dates using today's date)
- cancellations or changes (update or remove the old note), what was agreed, next steps, open questions
- personal details they shared that help (problem, preferences, team size, current tools, experience)
- for interviews: stage reached and a one-line impression of their answers
Keep still-valid old notes, drop outdated ones. At most 8 notes, each under 20 words. Never invent anything."""

_memory_lock = threading.Lock()


def remember(call_id):
    """Summarise a finished call into the caller's memory (runs in the background)."""
    with _memory_lock:  # each call is processed once
        c = q("SELECT * FROM calls WHERE id=?", (call_id,), one=True)
        if not c or c.get("memory_done"):
            return
        q("UPDATE calls SET memory_done=1 WHERE id=?", (call_id,))
    if c["persona"] == "easyrecruitz":
        return screen_candidate(c)
    try:
        msgs = q("SELECT role, text FROM messages WHERE call_id=? ORDER BY id", (call_id,))
        if not any(m["role"] == "user" for m in msgs):
            return
        p = PERSONAS.get(c["persona"], {})
        name, notes = memory_for(c["user_id"], c["persona"])
        transcript = "\n".join(f"{'Caller' if m['role'] == 'user' else p.get('name', 'Agent')}: {m['text']}" for m in msgs)
        prompt = (f"Today is {now_text()}. Agent: {p.get('name')}, {p.get('role')}.\nKnown name: {name or 'unknown'}\n"
                  "Current notes:\n" + ("\n".join(f"- {n}" for n in notes) or "(none)") + f"\n\nCall transcript:\n{transcript}")
        reply, provider = ask_llm(MEMORY_RULES, [{"role": "user", "text": prompt}], max_tokens=500, temperature=0.2)
        if provider.startswith("error"):
            return
        data = json.loads(re.search(r"\{.*\}", reply, re.S).group(0))
        new_name = str(data.get("name") or "").strip()
        if new_name and new_name.lower() not in ("null", "none", "unknown") and len(new_name) <= 40:
            q("UPDATE users SET name=? WHERE id=?", (new_name, c["user_id"]))
        new_notes = [str(n).strip() for n in (data.get("notes") or []) if str(n).strip()][:8]
        q("DELETE FROM memories WHERE user_id=? AND persona=?", (c["user_id"], c["persona"]))
        for n in new_notes:
            q("INSERT INTO memories (user_id, persona, note, call_id, created_at) VALUES (?,?,?,?,?)",
              (c["user_id"], c["persona"], n[:200], call_id, time.time()))
        print(f"memory updated for {c['user_id']}/{c['persona']}: name={new_name or name}, {len(new_notes)} notes")
    except Exception as e:
        print("memory update failed:", call_id, e)


def finish_call(call_id):
    q("UPDATE calls SET status='ended', ended_at=? WHERE id=? AND status='active'", (time.time(), call_id))
    threading.Thread(target=remember, args=(call_id,), daemon=True).start()


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
        q("INSERT INTO users (id, email, pw_hash, created_at) VALUES (?,?,?,?)", (user["id"], email, hash_pw(b.password), time.time()))
    return {"token": make_token(user["id"]), "role": "user", "email": email, "user_id": user["id"]}


TTS_CACHE = {}  # (voice, text) -> mp3 bytes; greetings and fillers become instant after first use
FILLERS = ["Hmm, one sec.", "Okay, let me see.", "Right, okay.", "Sure, just a moment.", "Mm, okay."]  # keep in sync with index.html


async def synth(text, spec, tries=3):
    """Yield mp3 chunks from Microsoft neural TTS. spec = "voice|rate|pitch" (per persona).
    Retries the service's occasional empty responses."""
    import edge_tts
    voice, rate, pitch = spec.split("|")
    for attempt in range(tries):
        sent = False
        try:
            async for c in edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).stream():
                if c["type"] == "audio":
                    sent = True
                    yield c["data"]
            return
        except Exception as e:
            if sent or attempt == tries - 1:  # can't restart a half-sent stream
                print("TTS failed:", voice, repr(text[:40]), e)
                return


async def hedged_synth(text, voice, hedge_after=1.3):
    """Race a second TTS request if the first hasn't produced audio quickly; stream whichever starts first.
    Cuts the service's random 1.5-3 s connection spikes."""
    import asyncio
    queues, tasks, getters = [], [], {}

    def launch():
        q = asyncio.Queue()

        async def run():
            try:
                async for c in synth(text, voice):
                    await q.put(c)
            finally:
                await q.put(None)
        queues.append(q)
        tasks.append(asyncio.create_task(run()))
        getters[asyncio.create_task(q.get())] = len(queues) - 1

    launch()
    try:
        while getters:
            done, _ = await asyncio.wait(getters, timeout=None if len(queues) > 1 else hedge_after,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:  # first request is slow: hedge
                launch()
                continue
            g = done.pop()
            i = getters.pop(g)
            if g.result() is None:  # this attempt produced nothing
                if len(queues) == 1:
                    launch()
                continue
            for other in getters:
                other.cancel()
            yield g.result()
            while (c := await queues[i].get()) is not None:
                yield c
            return
    finally:
        for t in tasks:
            t.cancel()


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
    from starlette.concurrency import run_in_threadpool
    jobs.append(run_in_threadpool(lambda: ask_llm("Reply with: ok", [{"role": "user", "text": "hi"}])))
    app.state.warmup = asyncio.ensure_future(asyncio.gather(*jobs))  # runs in background; server starts immediately
    threading.Thread(target=dialer_loop, daemon=True).start()


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
        async for c in hedged_synth(text, voice):
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
    q("INSERT INTO calls (id, user_id, persona, status, started_at, ended_at) VALUES (?,?,?,?,?,?)", (call_id, sub, b.persona, "active", now, None))
    p = PERSONAS[b.persona]
    greeting = p["greeting"]
    name, notes = memory_for(sub, b.persona)
    if name and not notes:  # known name but first call with this agent: fixed greeting, nothing to make up
        greeting = p["greeting_named"].format(name=name)
    elif notes:  # returning caller: pick up like someone who remembers them
        system = (f"{p['prompt']}\n\n{VOICE_RULES}\n\nToday is {now_text()}.\n{memory_prompt(name, notes)}\n\n"
                  "You're picking up the phone to this returning caller. Say your opening line: greet them warmly by name "
                  "if you know it, and only if your notes mention something clearly relevant now (an upcoming appointment, an "
                  "unfinished interview, a demo), bring it up briefly. Never mention anything that isn't in your notes. "
                  "One or two short sentences, end with a question.")
        text, provider = ask_llm(system, [{"role": "user", "text": "(The phone rings and you pick up.)"}], max_tokens=120)
        if not provider.startswith("error"):
            greeting = shorten(END_TOKEN.sub("", text), 35) or greeting
    if b.persona == "easyrecruitz":
        link_website_candidate(sub, call_id)
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
    name, notes = memory_for(sub, call["persona"])
    system = (f"{p['prompt']}\n\n{VOICE_RULES}\n\nToday is {now_text()}.\n{memory_prompt(name, notes)}\n\n"
              "Knowledge:\n" + "\n".join(f"- {d}" for d in docs))
    if p.get("stages"):  # structured conversation (interviewer): tell the model where we are
        answers = q("SELECT COUNT(*) n FROM messages WHERE call_id=? AND role='user'", (call_id,), one=True)["n"]
        stage = p["stages"][min(answers, len(p["stages"])) - 1]
        system += (f"\n\nInterview progress: answer {answers} of {len(p['stages'])}. YOUR TASK FOR THIS REPLY "
                   f"(do it now, don't skip it, don't go back to earlier topics): {stage}")
    if call["persona"] == "easyrecruitz":  # screening call: candidate + job + checklist of fields to collect
        system = recruit_system(call_id, docs)
    keep = 24 if call["persona"] == "easyrecruitz" else 12  # screening needs the whole call to avoid re-asking
    history = q("SELECT role, text FROM messages WHERE call_id=? ORDER BY id", (call_id,))[-keep:]

    t0 = time.time()
    reply, provider = ask_llm(system, history)
    ms = int((time.time() - t0) * 1000)
    end = bool(END_TOKEN.search(reply))
    limit = 45 if end else 40 if call["persona"] == "easyrecruitz" else 28  # goodbye / role pitch may run longer
    reply = shorten(END_TOKEN.sub("", reply), limit) or "Sorry, could you say that again?"
    q("INSERT INTO messages (call_id, role, text, context, provider, latency_ms, created_at) VALUES (?,?,?,?,?,?,?)",
      (call_id, "ai", reply, "\n".join(docs), provider, ms, time.time()))
    if end:  # agent said goodbye: close the call; the browser hangs up after the last words are spoken
        finish_call(call_id)
    return {"reply": reply, "provider": provider, "latency_ms": ms, "end": end}


@app.post("/api/calls/{call_id}/end")
def end_call(call_id: str, sub=Depends(current_subject)):
    own_call(call_id, sub)
    finish_call(call_id)
    return {"ok": True}


class Rate(BaseModel):
    rating: int


@app.post("/api/calls/{call_id}/rate")
def rate_call(call_id: str, b: Rate, sub=Depends(current_subject)):
    own_call(call_id, sub)
    q("UPDATE calls SET rating=? WHERE id=?", (max(1, min(5, b.rating)), call_id))
    return {"ok": True}


def close_stale_calls():
    """Calls abandoned without "End call" (tab closed, crash) are closed after 5 min of silence."""
    q("""UPDATE calls SET status='ended', ended_at=(SELECT MAX(created_at) FROM messages m WHERE m.call_id=calls.id)
         WHERE status='active' AND COALESCE((SELECT MAX(created_at) FROM messages m WHERE m.call_id=calls.id), started_at) < ?""",
      (time.time() - 300,))
    for c in q("SELECT id FROM calls WHERE status='ended' AND COALESCE(memory_done, 0)=0"):
        threading.Thread(target=remember, args=(c["id"],), daemon=True).start()


CALL_COLUMNS = """c.*, u.email, (SELECT COUNT(*) FROM messages m WHERE m.call_id=c.id) AS msg_count,
                  CAST(COALESCE(c.ended_at, (SELECT MAX(created_at) FROM messages m WHERE m.call_id=c.id), c.started_at)
                       - c.started_at AS INTEGER) AS duration"""


def with_labels(rows):
    for c in rows:
        c["persona_name"] = persona_label(c["persona"])
    return rows


class Name(BaseModel):
    name: str


@app.get("/api/me")
def me(sub=Depends(current_subject)):
    u = q("SELECT name FROM users WHERE id=?", (sub,), one=True) or {}
    mem = q("SELECT persona, note FROM memories WHERE user_id=? ORDER BY persona, id", (sub,))
    for m in mem:
        m["persona_name"] = persona_label(m["persona"])
    return {"name": u.get("name"), "memories": mem}


@app.post("/api/me/name")
def set_name(b: Name, sub=Depends(current_subject)):
    q("UPDATE users SET name=? WHERE id=?", (b.name.strip()[:40] or None, sub))
    return {"ok": True}


@app.post("/api/me/forget")
def forget_me(sub=Depends(current_subject)):
    q("DELETE FROM memories WHERE user_id=?", (sub,))
    q("UPDATE users SET name=NULL WHERE id=?", (sub,))
    return {"ok": True}


@app.get("/api/me/calls")
def my_calls(sub=Depends(current_subject)):
    close_stale_calls()
    return with_labels(q(f"SELECT {CALL_COLUMNS} FROM calls c LEFT JOIN users u ON u.id=c.user_id "
                         "WHERE c.user_id=? ORDER BY c.started_at DESC LIMIT 100", (sub,)))


@app.get("/api/me/calls/{call_id}")
def my_call(call_id: str, sub=Depends(current_subject)):
    own_call(call_id, sub)
    return {"messages": q("SELECT role, text, created_at FROM messages WHERE call_id=? ORDER BY id", (call_id,))}


@app.get("/api/admin/overview")
def admin_overview(_=Depends(require_admin)):
    close_stale_calls()
    calls = with_labels(q(f"SELECT {CALL_COLUMNS}, u.name FROM calls c LEFT JOIN users u ON u.id=c.user_id "
                          "ORDER BY c.started_at DESC LIMIT 200"))
    users = q("""SELECT u.id, u.email, u.name, u.created_at, COUNT(c.id) AS call_count,
                        (SELECT COUNT(*) FROM memories mm WHERE mm.user_id=u.id) AS memory_count
                 FROM users u LEFT JOIN calls c ON c.user_id=u.id GROUP BY u.id ORDER BY u.created_at DESC""")
    # analytics
    day = 86400
    today = int(time.time() // day)
    per_day = {r["d"]: r["n"] for r in q("SELECT CAST(started_at / 86400 AS INTEGER) d, COUNT(*) n FROM calls "
                                         "WHERE started_at > ? GROUP BY d", ((today - 13) * day,))}
    volume = [{"day": (today - i) * day, "calls": per_day.get(today - i, 0)} for i in range(13, -1, -1)]
    agents = []
    for pid, p in PERSONAS.items():
        a = q("""SELECT COUNT(*) calls, AVG(rating) rating, SUM(status='active') live,
                        AVG(COALESCE(ended_at, started_at) - started_at) duration FROM calls WHERE persona=?""", (pid,), one=True)
        lat = q("""SELECT AVG(latency_ms) ms FROM messages m JOIN calls c ON c.id=m.call_id
                   WHERE c.persona=? AND m.latency_ms IS NOT NULL""", (pid,), one=True)["ms"]
        agents.append({"id": pid, "name": p["name"], "role": p["role"], "calls": a["calls"], "live": a["live"] or 0,
                       "rating": a["rating"], "duration": a["duration"], "latency_ms": lat})
    recent = q("SELECT latency_ms, provider FROM messages WHERE latency_ms IS NOT NULL ORDER BY id DESC LIMIT 50")
    ok = [r for r in recent if not (r["provider"] or "").startswith("error")]
    rating = q("SELECT AVG(rating) r, COUNT(rating) n FROM calls", one=True)
    return {"calls": calls, "users": users, "volume": volume, "agents": agents,
            "system": {"llm": ok[0]["provider"] if ok else os.getenv("LLM_PROVIDER", "groq"),
                       "llm_ok": round(100 * len(ok) / len(recent)) if recent else 100,
                       "rag_docs": kb.count() if kb else 0, "tts_cached": len(TTS_CACHE)},
            "stats": {"users": len(users), "calls": q("SELECT COUNT(*) n FROM calls", one=True)["n"],
                      "active": sum(c["status"] == "active" for c in calls),
                      "messages": q("SELECT COUNT(*) n FROM messages", one=True)["n"],
                      "latency_ms": round(sum(r["latency_ms"] for r in ok) / len(ok)) if ok else None,
                      "rating": rating["r"], "ratings": rating["n"]}}


@app.get("/api/admin/calls/{call_id}")
def admin_call(call_id: str, _=Depends(require_admin)):
    call = q("SELECT c.*, u.email, u.name FROM calls c LEFT JOIN users u ON u.id=c.user_id WHERE c.id=?", (call_id,), one=True)
    if not call:
        raise HTTPException(404, "Call not found")
    call["persona_name"] = persona_label(call["persona"])
    return {"call": call, "messages": q("SELECT * FROM messages WHERE call_id=? ORDER BY id", (call_id,)),
            "memory": memory_for(call["user_id"], call["persona"])[1]}


# ---------------------------------------------------------------- EasyRecruitz: AI candidate screening calls
# Candidate upload -> AI calling -> screening -> information collection -> automatic Excel / Google Sheet update.
# "Calls" ring a private web link per candidate (no telecom needed); the dialer works through the queue on its own.
import asyncio, base64, csv, io, secrets

RECRUIT_COLUMNS = [  # (database column, Excel / Google Sheet header)
    ("id", "Candidate ID"), ("name", "Name"), ("phone", "Phone"), ("email", "Email"), ("source", "Source"),
    ("status", "Call status"), ("attempts", "Attempts"), ("location", "Current location"),
    ("company", "Current company"), ("total_exp", "Total experience"), ("relevant_exp", "Relevant experience"),
    ("current_ctc", "Current CTC"), ("expected_ctc", "Expected CTC"), ("notice_period", "Notice period"),
    ("reason_change", "Reason for change"), ("interested", "Interested"),
    ("interview_availability", "Interview availability"), ("summary", "Call summary"),
    ("last_call_at", "Last call"), ("next_retry_at", "Next retry"), ("updated_at", "Updated")]
SCREEN_FIELDS = RECRUIT_COLUMNS[7:17]  # what the agent collects on the call
STATUS_LABEL = {"pending": "Pending", "ringing": "Ringing", "in_call": "On call", "completed": "Completed",
                "unanswered": "No answer", "failed": "Failed", "callback": "Call back"}
DEFAULT_JOB = {"title": "Python Backend Developer", "company": "a fast-growing SaaS product company",
               "location": "Pune, hybrid (3 days in office)", "experience": "2 to 5 years", "budget": "up to 20 LPA",
               "about": "Build and scale APIs with Python, FastAPI and PostgreSQL for a platform used by 2 lakh businesses."}
RING_SECONDS = 45   # unanswered after this long
MAX_ATTEMPTS = 3    # calls per candidate before giving up


def setting(key, default=None):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return json.loads(r["value"]) if r else default


def set_setting(key, value):
    q("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, json.dumps(value)))


def job():
    return {**DEFAULT_JOB, **(setting("job") or {})}


def candidate(column, value):
    assert column in ("id", "token", "call_id", "user_id")
    return q(f"SELECT * FROM candidates WHERE {column}=?", (value,), one=True)


def new_candidate(name, phone="", email="", source="Manual", user_id=None):
    cid, now = "R-" + uuid.uuid4().hex[:6].upper(), time.time()
    q("""INSERT INTO candidates (id, name, phone, email, source, status, attempts, token, user_id, created_at, updated_at)
         VALUES (?,?,?,?,?,'pending',0,?,?,?,?)""", (cid, name, phone, email, source, secrets.token_urlsafe(12), user_id, now, now))
    return cid


def cell(column, value):
    if column in ("last_call_at", "next_retry_at", "updated_at"):
        return datetime.fromtimestamp(value, TZ).strftime("%d %b %Y, %I:%M %p") if value else ""
    if column == "status":
        return STATUS_LABEL.get(value, value or "")
    return "" if value is None else value


def greeting_for(c, inbound=False):
    title, first = job()["title"], (c.get("name") or "").split(" ")[0]
    if inbound:
        return (f"Hi{' ' + first if first else ''}, this is Kavya from EasyRecruitz 365, thanks for getting in touch! "
                f"I'd love to tell you about a {title} opening. Do you have a few minutes?")
    if first:
        return (f"Hi, am I speaking with {first}? This is Kavya calling from EasyRecruitz 365, about a {title} opening. "
                "Is this a good time to talk for a few minutes?")
    return f"Hi, this is Kavya calling from EasyRecruitz 365 about a {title} opening. Is this a good time to talk?"


def prerender_tts(text, spec):
    """Render a clip into the TTS cache in the background, so it plays instantly when the call is answered."""
    async def run():
        audio = b"".join([c async for c in synth(text, spec)])
        if audio:
            TTS_CACHE[(spec, text.strip()[:600])] = audio
    threading.Thread(target=lambda: asyncio.run(run()), daemon=True).start()


def ring(cid):
    """Dial a candidate: their call link starts ringing."""
    now = time.time()
    q("""UPDATE candidates SET status='ringing', attempts=COALESCE(attempts, 0)+1, last_call_at=?, next_retry_at=NULL,
         updated_at=? WHERE id=?""", (now, now, cid))
    prerender_tts(greeting_for(candidate("id", cid)), PERSONAS["easyrecruitz"]["tts"])


def mark_unanswered(cid, status="unanswered"):
    """No answer, declined or dropped: schedule an automatic retry in 2-4 days (until MAX_ATTEMPTS)."""
    c, now = candidate("id", cid), time.time()
    days = int(setting("retry_days", 3))
    nxt = now + days * 86400 if (c["attempts"] or 0) < MAX_ATTEMPTS else None
    q("UPDATE candidates SET status=?, next_retry_at=?, updated_at=? WHERE id=?", (status, nxt, now, cid))
    sync_sheet(cid)


def link_website_candidate(user_id, call_id):
    """Someone who applied through the website and called Kavya from the app becomes a candidate record."""
    c = candidate("user_id", user_id)
    if c:
        cid = c["id"]
    else:
        u = q("SELECT email, name FROM users WHERE id=?", (user_id,), one=True) or {}
        cid = new_candidate(u.get("name") or "", "", u.get("email") or "", "Website", user_id)
    now = time.time()
    q("""UPDATE candidates SET status='in_call', call_id=?, attempts=COALESCE(attempts, 0)+1, last_call_at=?,
         updated_at=? WHERE id=?""", (call_id, now, now, cid))


def recruit_system(call_id, docs):
    c, j = candidate("call_id", call_id) or {}, job()
    who = c.get("name") or "a candidate (ask their name early)"
    return f"""You are Kavya, a warm, professional recruiter at EasyRecruitz 365, on a phone screening call with {who}.

The opportunity:
- Role: {j['title']} at {j['company']}
- Location: {j['location']}
- Experience wanted: {j['experience']}. Budget: {j['budget']}
- About the role: {j['about']}

Run a natural screening conversation, never like an IVR:
1. If they haven't said it's a good time yet, check. If it isn't, ask when to call back, thank them and end the call.
2. Pitch the role in one or two lines, then collect these, ONE question per reply, in a natural order:
   current location; current company; total experience; relevant experience for this role; current CTC; expected CTC;
   notice period; reason for looking for a change; interest in this opportunity; availability for an interview (day and time).
3. Check the conversation so far and never re-ask something already answered. Acknowledge each answer briefly
   ("got it, thirty days"). If an answer is vague, ask once for a specific number or date.
4. Answer their questions about the role from the details above. If you don't know, say the recruiter will share it on email.
5. If they're not interested, politely ask why, thank them and end the call.
6. Once everything is collected, confirm the interview slot, say the team will confirm on email or WhatsApp, thank them and end the call.

{VOICE_RULES}

Today is {now_text()}.
Company notes:
""" + "\n".join(f"- {d}" for d in docs)


SCREEN_RULES = """Extract candidate screening data from a recruiter's phone call transcript. Return JSON only:
{"name": "", "location": "", "company": "", "total_exp": "", "relevant_exp": "", "current_ctc": "", "expected_ctc": "",
 "notice_period": "", "reason_change": "", "interested": "Yes | No | Maybe", "interview_availability": "",
 "callback_requested": false, "summary": ""}
Rules: use "" for anything the candidate didn't say. Keep values short and normalised, e.g. "5 years", "12 LPA",
"30 days", "Pune". Give interview availability as a real day and date (resolve "tomorrow" with today's date).
callback_requested is true if they asked to be called another time. summary: 2-3 sentences for the recruitment team
covering fit, key numbers, interest and next step. Never invent anything."""


def screen_candidate(call):
    """After a screening call: extract the fields + summary into the candidate record, then sync the sheet."""
    c = candidate("call_id", call["id"])
    if not c:
        return
    try:
        msgs = q("SELECT role, text FROM messages WHERE call_id=? ORDER BY id", (call["id"],))
        if not any(m["role"] == "user" for m in msgs):
            return mark_unanswered(c["id"], "failed")  # picked up but never spoke / dropped
        transcript = "\n".join(f"{'Candidate' if m['role'] == 'user' else 'Kavya'}: {m['text']}" for m in msgs)
        reply, provider = ask_llm(SCREEN_RULES, [{"role": "user", "text": (
            f"Today is {now_text()}. Role: {job()['title']}. Candidate on file: {c.get('name') or 'unknown'}.\n\n"
            f"Transcript:\n{transcript}")}], max_tokens=700, temperature=0.1)
        if provider.startswith("error"):
            return mark_unanswered(c["id"], "failed")
        data = json.loads(re.search(r"\{.*\}", reply, re.S).group(0))
        val = lambda k: str(data.get(k) or "").strip()[:900 if k == "summary" else 300]
        updates = {col: val(col) for col, _ in SCREEN_FIELDS if val(col)}
        if not c.get("name") and val("name"):
            updates["name"] = val("name")
        collected = sum(1 for col, _ in SCREEN_FIELDS if updates.get(col) or c.get(col))
        callback = bool(data.get("callback_requested")) and collected < 5
        now = time.time()
        updates.update(status="callback" if callback else "completed", summary=val("summary") or c.get("summary"),
                       next_retry_at=now + 86400 if callback else None, updated_at=now)
        q(f"UPDATE candidates SET {', '.join(k + '=?' for k in updates)} WHERE id=?", (*updates.values(), c["id"]))
        print(f"screened {c['id']}: {collected}/{len(SCREEN_FIELDS)} fields, status={updates['status']}")
        sync_sheet(c["id"])
    except Exception as e:
        print("screening failed:", c["id"], e)


def push_to_sheet(cid):
    """Upsert one candidate row into Google Sheets via the Apps Script webhook (see README)."""
    url = setting("sheet_webhook")
    if not url:
        return False
    import httpx
    c = candidate("id", cid)
    try:
        r = httpx.post(url, json={"row": {label: cell(col, c.get(col)) for col, label in RECRUIT_COLUMNS}},
                       timeout=20, follow_redirects=True)
        ok = r.status_code < 400
    except Exception as e:
        print("sheet sync failed:", cid, e)
        ok = False
    q("UPDATE candidates SET sheet_synced=? WHERE id=?", (1 if ok else 0, cid))
    return ok


def sync_sheet(cid):
    threading.Thread(target=push_to_sheet, args=(cid,), daemon=True).start()


def dialer_tick():
    now = time.time()
    for c in q("SELECT id FROM candidates WHERE status='ringing' AND last_call_at < ?", (now - RING_SECONDS,)):
        mark_unanswered(c["id"])  # nobody picked up
    if not setting("auto_dial", False):
        return
    if q("SELECT COUNT(*) n FROM candidates WHERE status IN ('ringing', 'in_call')", one=True)["n"]:
        return  # one call at a time
    nxt = q("""SELECT id FROM candidates WHERE status='pending'
               OR (status IN ('unanswered', 'failed', 'callback') AND next_retry_at <= ? AND attempts < ?)
               ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at LIMIT 1""", (now, MAX_ATTEMPTS), one=True)
    if nxt:
        ring(nxt["id"])


def dialer_loop():
    n = 0
    while True:
        try:
            dialer_tick()
            n += 1
            if n % 20 == 0:
                close_stale_calls()  # abandoned calls get screened too
        except Exception as e:
            print("dialer error:", e)
        time.sleep(3)


# ----- candidate side: the private call link
@app.get("/c/{token}")
def candidate_page(token: str):
    return FileResponse(ROOT / "index.html", headers={"Cache-Control": "no-store"})


def by_token(token):
    c = candidate("token", token)
    if not c:
        raise HTTPException(404, "This call link isn't valid.")
    return c


@app.get("/api/candidate/{token}")
def candidate_info(token: str):
    c = by_token(token)
    return {"name": c["name"], "status": c["status"], "role": job()["title"]}


@app.post("/api/candidate/{token}/answer")
def candidate_answer(token: str):
    c = by_token(token)
    inbound = c["status"] != "ringing"  # opened the link and called in themselves
    call_id, now, sub = "C-" + uuid.uuid4().hex[:10].upper(), time.time(), "cand:" + c["id"]
    q("INSERT INTO calls (id, user_id, persona, status, started_at, ended_at) VALUES (?,?,?,?,?,?)",
      (call_id, sub, "easyrecruitz", "active", now, None))
    greeting = greeting_for(c, inbound)
    q("INSERT INTO messages (call_id, role, text, created_at) VALUES (?,?,?,?)", (call_id, "ai", greeting, now))
    q("""UPDATE candidates SET status='in_call', call_id=?, attempts=COALESCE(attempts, 0)+?, last_call_at=?, updated_at=?
         WHERE id=?""", (call_id, 1 if inbound else 0, now, now, c["id"]))
    p = PERSONAS["easyrecruitz"]
    return {"call_id": call_id, "greeting": greeting, "token": make_token(sub), "name": c["name"],
            "persona": {"id": "easyrecruitz", "name": p["name"], "role": p["role"], "voice": p["voice"], "blurb": p["blurb"]}}


@app.post("/api/candidate/{token}/decline")
def candidate_decline(token: str):
    c = by_token(token)
    if c["status"] == "ringing":
        mark_unanswered(c["id"])
    return {"ok": True}


# ----- recruiter side (admin)
class Upload(BaseModel):
    filename: str
    data: str  # base64 file content (CSV or Excel)


class NewCandidate(BaseModel):
    name: str
    phone: str = ""
    email: str = ""


class RecruitConfig(BaseModel):
    job: dict | None = None
    auto_dial: bool | None = None
    retry_days: int | None = None
    sheet_webhook: str | None = None


@app.get("/api/recruit")
def recruit_overview(_=Depends(require_admin)):
    cands = q("SELECT * FROM candidates ORDER BY created_at DESC")
    count = lambda f: sum(1 for c in cands if f(c))
    calling = [c for c in cands if c["status"] in ("ringing", "in_call")]
    return {"candidates": cands, "max_attempts": MAX_ATTEMPTS,
            "stats": {"total": len(cands), "completed": count(lambda c: c["status"] == "completed"),
                      "pending": count(lambda c: c["status"] == "pending"),
                      "retries": count(lambda c: c["next_retry_at"] is not None),
                      "unanswered": count(lambda c: c["status"] in ("unanswered", "failed", "callback")),
                      "interested": count(lambda c: (c["interested"] or "").lower().startswith("yes")),
                      "calling": [{"name": c["name"], "status": c["status"]} for c in calling]},
            "config": {"job": job(), "auto_dial": setting("auto_dial", False), "retry_days": setting("retry_days", 3),
                       "sheet_webhook": setting("sheet_webhook", "")}}


@app.post("/api/recruit/upload")
def recruit_upload(b: Upload, _=Depends(require_admin)):
    raw = base64.b64decode(b.data.split(",")[-1])
    if b.filename.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(raw), read_only=True, data_only=True).active
        rows = [["" if v is None else str(v).strip() for v in r] for r in ws.iter_rows(values_only=True)]
    else:
        rows = [[v.strip() for v in r] for r in csv.reader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))]
    rows = [r for r in rows if any(r)]
    if not rows:
        raise HTTPException(400, "The file is empty.")
    head = [h.lower() for h in rows[0]]
    col = lambda *keys: next((i for i, h in enumerate(head) if any(k in h for k in keys)), None)
    ni, pi, ei = col("name"), col("phone", "mobile", "contact", "number"), col("mail")
    if ni is None:
        raise HTTPException(400, "Add a header row with at least a 'Name' column (Phone and Email are optional).")
    added = skipped = 0
    for r in rows[1:]:
        get = lambda i: r[i].strip() if i is not None and i < len(r) else ""
        name, phone, email = get(ni), get(pi), get(ei).lower()
        if phone.endswith(".0") and phone[:-2].isdigit():
            phone = phone[:-2]  # Excel stores numbers as floats
        if not name:
            continue
        if q("SELECT id FROM candidates WHERE (phone<>'' AND phone=?) OR (email<>'' AND email=?)", (phone, email), one=True):
            skipped += 1
            continue
        new_candidate(name, phone, email, "Upload")
        added += 1
    return {"added": added, "skipped": skipped}


@app.post("/api/recruit/candidates")
def recruit_add(b: NewCandidate, _=Depends(require_admin)):
    if not b.name.strip():
        raise HTTPException(400, "Name is required.")
    return {"id": new_candidate(b.name.strip(), b.phone.strip(), b.email.strip().lower(), "Manual")}


@app.get("/api/recruit/{cid}")
def recruit_candidate(cid: str, _=Depends(require_admin)):
    c = candidate("id", cid)
    if not c:
        raise HTTPException(404, "Candidate not found")
    msgs = q("SELECT role, text FROM messages WHERE call_id=? ORDER BY id", (c["call_id"],)) if c["call_id"] else []
    return {"candidate": c, "messages": msgs, "fields": [{"key": k, "label": l} for k, l in SCREEN_FIELDS]}


@app.post("/api/recruit/{cid}/call")
def recruit_call(cid: str, _=Depends(require_admin)):
    c = candidate("id", cid)
    if not c:
        raise HTTPException(404, "Candidate not found")
    if c["status"] in ("ringing", "in_call"):
        raise HTTPException(409, "Already on a call.")
    if q("SELECT COUNT(*) n FROM candidates WHERE status IN ('ringing', 'in_call')", one=True)["n"]:
        raise HTTPException(409, "Another candidate is being called right now.")
    ring(cid)
    return {"link": f"/c/{c['token']}"}


@app.post("/api/recruit/{cid}/delete")
def recruit_delete(cid: str, _=Depends(require_admin)):
    q("DELETE FROM candidates WHERE id=?", (cid,))
    return {"ok": True}


@app.post("/api/recruit-config")
def recruit_config(b: RecruitConfig, _=Depends(require_admin)):
    if b.job is not None:
        set_setting("job", {k: str(b.job.get(k) or DEFAULT_JOB[k])[:300] for k in DEFAULT_JOB})
    if b.auto_dial is not None:
        set_setting("auto_dial", b.auto_dial)
    if b.retry_days is not None:
        set_setting("retry_days", max(2, min(4, b.retry_days)))
    if b.sheet_webhook is not None:
        url = b.sheet_webhook.strip()
        if url and not url.startswith("https://script.google.com/"):
            raise HTTPException(400, "Paste the Google Apps Script web app URL (https://script.google.com/...).")
        set_setting("sheet_webhook", url)
    return {"ok": True}


@app.post("/api/recruit-sync")
def recruit_sync(_=Depends(require_admin)):
    if not setting("sheet_webhook"):
        raise HTTPException(400, "Add your Google Sheet webhook URL first.")
    ids = [c["id"] for c in q("SELECT id FROM candidates ORDER BY created_at")]
    ok = sum(push_to_sheet(i) for i in ids)
    return {"synced": ok, "total": len(ids)}


@app.get("/api/recruit-export.xlsx")
def recruit_export(token: str):
    if verify_token(token) != "admin":
        raise HTTPException(403, "Admin only")
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Candidates"
    ws.append([label for _, label in RECRUIT_COLUMNS])
    for c in q("SELECT * FROM candidates ORDER BY created_at"):
        ws.append([cell(col, c.get(col)) for col, _ in RECRUIT_COLUMNS])
    for i, (col, label) in enumerate(RECRUIT_COLUMNS, start=1):
        h = ws.cell(row=1, column=i)
        h.font, h.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="3BAA89")
        ws.column_dimensions[h.column_letter].width = 60 if col == "summary" else max(14, len(label) + 4)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "B2"
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="easyrecruitz-candidates.xlsx"'})


@app.get("/api/recruit-sample.csv")
def recruit_sample():
    return Response("Name,Phone,Email\nRahul Sharma,9876543210,rahul.sharma@example.com\n"
                    "Neha Verma,9123456780,neha.verma@example.com\nArjun Iyer,9988776655,arjun.iyer@example.com\n",
                    media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="candidates-sample.csv"'})
