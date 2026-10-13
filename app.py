import asyncio
import aiohttp
import feedparser
from bs4 import BeautifulSoup
import re
import sqlite3
import os
import time
import threading
import urllib.request
import random
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from flask import Flask, render_template_string, request, jsonify

# Allow oauthlib to accept Google-modified scope sets on Workspace domains
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

# Google Classroom API imports
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

app = Flask(__name__)
DB_NAME = "broadsheet_archive.db"

# -------------------------------------------------------------
# DATABASE & CACHING LAYER
# -------------------------------------------------------------
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                guid TEXT PRIMARY KEY,
                category TEXT,
                title TEXT,
                summary TEXT,
                url TEXT,
                img_url TEXT,
                fetched_at TIMESTAMP
            )
        """)
        # Classroom notices (quizzes / vivas) the user has marked as read
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dismissed_notices (
                notice_id TEXT PRIMARY KEY,
                kind TEXT,
                course TEXT,
                title TEXT,
                dismissed_at TIMESTAMP
            )
        """)
init_db()

def get_dismissed_ids():
    try:
        with sqlite3.connect(DB_NAME) as conn:
            return {row[0] for row in conn.execute("SELECT notice_id FROM dismissed_notices")}
    except Exception:
        return set()

def count_dismissed():
    try:
        with sqlite3.connect(DB_NAME) as conn:
            return conn.execute("SELECT COUNT(*) FROM dismissed_notices").fetchone()[0]
    except Exception:
        return 0

def dismiss_notice(notice_id, kind, course="", title=""):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO dismissed_notices VALUES (?, ?, ?, ?, ?)",
            (notice_id, kind, course, title, datetime.now(timezone.utc).isoformat()),
        )

def restore_notice(notice_id=None):
    """Un-dismiss one notice, or all of them when notice_id is None."""
    with sqlite3.connect(DB_NAME) as conn:
        if notice_id:
            conn.execute("DELETE FROM dismissed_notices WHERE notice_id = ?", (notice_id,))
        else:
            conn.execute("DELETE FROM dismissed_notices")

# -------------------------------------------------------------
# TIMEZONE & DATE SETUP
# -------------------------------------------------------------
LOCAL_TZ = ZoneInfo("Asia/Karachi")
LOCAL_TZ_LABEL = "PKT"

# -------------------------------------------------------------
# GOOGLE CLASSROOM SERVICE (5E SECTION, QUIZZES, VIVAS & TASKS)
# -------------------------------------------------------------
CLASSROOM_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
]

# --- Options -------------------------------------------------
HIDE_PAST_VIVAS = True             # don't fetch/show vivas whose date (and time) has already passed
HIDE_PAST_QUIZZES = True           # same for quizzes
UNDATED_NOTICE_MAX_AGE_DAYS = 14   # notices with no readable date vanish after this many days (0 = never)

QUIZ_REGEX = re.compile(r"\b(quiz|quizzes)\b", re.IGNORECASE)
VIVA_REGEX = re.compile(r"\b(viva|vivas|oral\s*exam|presentation)\b", re.IGNORECASE)

_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_WEEKDAY = r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"

DATE_TIME_EXTRACTOR = re.compile(
    r"(?:(?:on|at|by)\s+)?(?:" + _WEEKDAY + r",?\s*)?"
    r"(?:\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MON + r"(?:,?\s*\d{4})?"
    r"|\b" + _MON + r"\s+\d{1,2}(?:st|nd|rd|th)?\b(?:,?\s*\d{4})?"
    r"|\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)"
    r"(?:\s*(?:at|@|,)?\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))?",
    re.IGNORECASE
)

TIME_ONLY_EXTRACTOR = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", re.IGNORECASE)

TOPIC_EXTRACTOR = re.compile(
    r"(?:topic[s]?|syllabus|content|chapter[s]?|include[s]?|covers?|portion)[:\s-]+([^\n.]+)",
    re.IGNORECASE
)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_DAY_MONTH = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:,?\s*(\d{4}))?",
    re.IGNORECASE)
_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s*(\d{4}))?",
    re.IGNORECASE)
_NUMERIC = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)

def parse_iso_datetime(iso_str):
    if not iso_str:
        return None
    try:
        clean = iso_str.replace("Z", "+00:00")
        # Classroom can return 9 fractional digits; older Pythons only accept 3 or 6
        clean = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], clean)
        return datetime.fromisoformat(clean).astimezone(LOCAL_TZ)
    except Exception:
        return None

def try_parse_scheduled_date(text, reference_dt):
    """
    Parse '15 Oct', '15th October 2026', 'October 15', '15/10' (day/month) and an optional
    time like '2 pm' into an aware datetime. With no time given, the event counts as ending
    at 23:59 so it stays visible all day. Returns None if no date can be read.
    """
    if not text:
        return None

    m = _DAY_MONTH.search(text)
    if m:
        day, month, year_s = int(m.group(1)), MONTHS[m.group(2).lower()], m.group(3)
    else:
        m = _MONTH_DAY.search(text)
        if m:
            month, day, year_s = MONTHS[m.group(1).lower()], int(m.group(2)), m.group(3)
        else:
            m = _NUMERIC.search(text)
            if not m:
                return None
            day, month, year_s = int(m.group(1)), int(m.group(2)), m.group(3)
            if year_s and len(year_s) == 2:
                year_s = "20" + year_s

    hour, minute = 23, 59
    t = _TIME.search(text)
    if t:
        hour = int(t.group(1)) % 12 + (12 if t.group(3).lower() == "pm" else 0)
        minute = int(t.group(2) or 0)

    year = int(year_s) if year_s else reference_dt.year
    try:
        dt = datetime(year, month, day, hour, minute, tzinfo=LOCAL_TZ)
        # "5 Jan" in an announcement posted in October means next January
        if not year_s and dt < reference_dt - timedelta(days=60):
            dt = dt.replace(year=year + 1)
        return dt
    except ValueError:
        return None

def notice_is_expired(schedule_text, raw_text, posted_dt, now_dt):
    """True when the quiz/viva date has passed (or, if undated, the notice is stale)."""
    ref = posted_dt or now_dt
    event_dt = try_parse_scheduled_date(schedule_text, ref) or try_parse_scheduled_date(raw_text, ref)
    if event_dt:
        return event_dt < now_dt
    if UNDATED_NOTICE_MAX_AGE_DAYS and posted_dt:
        return (now_dt - posted_dt) > timedelta(days=UNDATED_NOTICE_MAX_AGE_DAYS)
    return False

def extract_schedule_and_topics(text):
    date_match = DATE_TIME_EXTRACTOR.search(text)
    time_match = TIME_ONLY_EXTRACTOR.search(text)

    when_str = "Refer to notice details"
    if date_match:
        when_str = date_match.group(0).strip()
    elif time_match:
        when_str = f"At {time_match.group(0).strip()}"

    topic_match = TOPIC_EXTRACTOR.search(text)
    if topic_match:
        topics_str = topic_match.group(1).strip()
    else:
        clean_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        topics_str = " ".join(clean_lines[:2])
        if len(topics_str) > 130:
            topics_str = topics_str[:130] + "..."

    return when_str, topics_str

def get_classroom_service():
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    creds = None
    if os.path.exists("token.json"):
        try:
            creds = Credentials.from_authorized_user_file("token.json", CLASSROOM_SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        elif os.path.exists("credentials.json"):
            try:
                flow = InstalledAppFlow.from_client_secrets_file("credentials.json", CLASSROOM_SCOPES)
                creds = flow.run_local_server(port=0)
                with open("token.json", "w") as token_file:
                    token_file.write(creds.to_json())
            except Exception as e:
                print(f"[Classroom] Auth flow failed: {e}")
                return None
        else:
            return None

    try:
        return build("classroom", "v1", credentials=creds)
    except Exception as e:
        print(f"[Classroom] Service build failed: {e}")
        return None

def fetch_classroom_digest():
    hidden_count = count_dismissed()
    try:
        service = get_classroom_service()
        if not service:
            return {"assignments": [], "quizzes": [], "vivas": [], "connected": False, "hidden_count": hidden_count}

        now_dt = datetime.now(LOCAL_TZ)
        cutoff_date = now_dt - timedelta(days=30)
        dismissed = get_dismissed_ids()

        courses_res = service.courses().list(studentId="me", courseStates=["ACTIVE"]).execute()
        courses = courses_res.get("courses", [])

        # Filter strictly for 5E
        filtered_courses = [
            c for c in courses
            if re.search(r"\b5e\b", c.get("name", "") + " " + c.get("section", ""), re.IGNORECASE)
        ]
        if not filtered_courses:
            filtered_courses = [c for c in courses if "5e" in c.get("name", "").lower()]
        if not filtered_courses:
            filtered_courses = courses

        pending_assignments = []
        quiz_announcements = []
        viva_announcements = []

        for course in filtered_courses:
            c_id = course["id"]
            c_name = course.get("name", "Class")

            # 1. Assigned Coursework
            try:
                sub_res = (
                    service.courses()
                    .courseWork()
                    .studentSubmissions()
                    .list(courseId=c_id, courseWorkId="-", states=["NEW", "CREATED"])
                    .execute()
                )
                for sub in sub_res.get("studentSubmissions", []):
                    cw_id = sub.get("courseWorkId")
                    cw = service.courses().courseWork().get(courseId=c_id, id=cw_id).execute()

                    creation_dt = parse_iso_datetime(cw.get("creationTime"))
                    if creation_dt and creation_dt < cutoff_date:
                        continue

                    due_date_obj = cw.get("dueDate")
                    sort_key = 9999999999
                    due_display = "No explicit deadline"
                    priority_rank = 3
                    priority_badge = "UPCOMING"

                    if due_date_obj:
                        due_time_obj = cw.get("dueTime", {})
                        hr = due_time_obj.get("hours", 23)
                        mn = due_time_obj.get("minutes", 59)
                        due_dt = datetime(
                            year=due_date_obj["year"],
                            month=due_date_obj["month"],
                            day=due_date_obj["day"],
                            hour=hr,
                            minute=mn,
                            tzinfo=LOCAL_TZ
                        )
                        sort_key = due_dt.timestamp()
                        due_display = due_dt.strftime("%a %d %b, %I:%M %p")

                        diff_sec = (due_dt - now_dt).total_seconds()
                        if diff_sec < 0:
                            priority_rank = 0
                            priority_badge = "OVERDUE"
                        elif diff_sec <= 48 * 3600:
                            priority_rank = 1
                            priority_badge = "PRIORITY"
                        elif diff_sec <= 7 * 86400:
                            priority_rank = 2
                            priority_badge = "MEDIUM"

                    pending_assignments.append({
                        "course": c_name,
                        "title": cw.get("title", "Untitled Work"),
                        "due": due_display,
                        "priority_badge": priority_badge,
                        "priority_rank": priority_rank,
                        "timestamp": sort_key,
                        "link": cw.get("alternateLink", "#"),
                    })
            except Exception:
                pass

            # 2. Announcements
            try:
                ann_res = service.courses().announcements().list(courseId=c_id, pageSize=12).execute()
                for ann in ann_res.get("announcements", []):
                    notice_id = f"{c_id}:{ann.get('id', '')}"
                    if notice_id in dismissed:          # marked as read -> never shown again
                        continue

                    posted_dt = parse_iso_datetime(ann.get("creationTime")) or parse_iso_datetime(ann.get("updateTime"))
                    if posted_dt and posted_dt < cutoff_date:
                        continue

                    raw_text = ann.get("text", "")
                    link = ann.get("alternateLink", "#")
                    posted_str = posted_dt.strftime("%d %b") if posted_dt else "Recent"
                    first_line = next((ln.strip() for ln in raw_text.splitlines() if ln.strip()), "")[:80]

                    # Check for VIVA
                    if VIVA_REGEX.search(raw_text):
                        sched, topics = extract_schedule_and_topics(raw_text)
                        if HIDE_PAST_VIVAS and notice_is_expired(sched, raw_text, posted_dt, now_dt):
                            continue
                        viva_announcements.append({
                            "notice_id": notice_id,
                            "course": c_name,
                            "title": first_line,
                            "schedule": sched,
                            "topics": topics,
                            "posted_on": posted_str,
                            "link": link
                        })
                    # Check for QUIZ
                    elif QUIZ_REGEX.search(raw_text):
                        sched, topics = extract_schedule_and_topics(raw_text)
                        if HIDE_PAST_QUIZZES and notice_is_expired(sched, raw_text, posted_dt, now_dt):
                            continue
                        quiz_announcements.append({
                            "notice_id": notice_id,
                            "course": c_name,
                            "title": first_line,
                            "schedule": sched,
                            "topics": topics,
                            "posted_on": posted_str,
                            "link": link
                        })
            except Exception:
                pass

        pending_assignments.sort(key=lambda x: (x["priority_rank"], x["timestamp"]))

        return {
            "assignments": pending_assignments,
            "quizzes": quiz_announcements,
            "vivas": viva_announcements,
            "connected": True,
            "hidden_count": hidden_count,
        }
    except Exception as exc:
        print(f"[Classroom] Query error: {exc}")
        return {"assignments": [], "quizzes": [], "vivas": [], "connected": False, "hidden_count": hidden_count}

# -------------------------------------------------------------
# FEEDS CONFIGURATION
# -------------------------------------------------------------
FEEDS = {
    "tech": "https://techcrunch.com/feed/",
    "security": "https://feeds.feedburner.com/TheHackersNews",
    "f1": "https://www.formula1.com/content/fom-website/en/latest/all.xml",
    "football": [
        "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "https://www.theguardian.com/football/rss",
        "https://www.espn.com/espn/rss/soccer/news",
    ],
    "tennis": [
        "https://feeds.bbci.co.uk/sport/tennis/rss.xml",
        "https://www.theguardian.com/sport/tennis/rss",
        "https://www.atptour.com/en/media/rss/news",
    ],
    "pakistan": "https://www.dawn.com/feeds/pakistan/",
    "world": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "lobsters": "https://lobste.rs/rss",
    "lwn": "https://lwn.net/headlines/rss",
    "ars": "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "swift": "https://www.swift.org/atom.xml",
    "flutter": "https://medium.com/feed/flutter",
}

HN_API = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=12"
HN_RSS_FALLBACK = "https://hnrss.org/frontpage?count=10"

LIST_FEEDS = {
    "hn": (7, 0), "lobsters": (7, 0), "lwn": (5, 0), "ars": (4, 0),
    "swift": (3, 0), "flutter": (3, 0),
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# -------------------------------------------------------------
# KNOWLEDGE VAULTS
# -------------------------------------------------------------
CONCEPTS_VAULT = [
    {
        "domain": "NEURAL COMPILATION & AI",
        "title": "Kernel Fusion & FlashAttention Tiling",
        "text": "Traditional Multi-Head Attention incurs an O(N²) memory bandwidth cost moving tensors between GPU HBM and on-chip SRAM. FlashAttention leverages online softmax recalculation and GPU tile management, fusing matmul, mask, and softmax into a single kernel pass to maximize Tensor Core compute saturation."
    },
    {
        "domain": "OFFENSIVE SECURITY & MEMORY CORRUPTION",
        "title": "Use-After-Free (UAF) & Heap Feng Shui",
        "text": "When dynamically allocated heap memory is freed without clearing the associated pointer, a dangling pointer remains. By orchestrating deterministic allocation patterns (Heap Grooming), an attacker forces a controlled object payload into the freed memory block, redirecting function pointers or vtables during subsequent execution."
    },
    {
        "domain": "CRYPTOGRAPHIC PRIMITIVES",
        "title": "BLAKE3 Tree Hashing & SIMD Parallelism",
        "text": "Unlike Merkle-Damgård constructions (MD5, SHA-256), BLAKE3 employs a binary Merkle tree structure. This allows linear chunks to be hashed concurrently across CPU SIMD lanes (AVX-512) and multiple execution threads, providing immunity to Length Extension Attacks while running up to 5x faster than SHA-256."
    },
    {
        "domain": "EXPLOITATION MITIGATION BYPASS",
        "title": "Blind ROP (BROP) & Canary Probing",
        "text": "In target services that restart crashing child threads without re-randomizing stack addresses (forked network daemons), adversaries probe stack canary bytes byte-by-byte across 256 guesses. Once the 8-byte canary is recovered without triggering SIGABRT, arbitrary Return-Oriented chains execute silently."
    },
    {
        "domain": "OPERATING SYSTEMS",
        "title": "Virtual Memory, Page Tables & the TLB",
        "text": "Every process sees a private virtual address space. The MMU translates virtual to physical addresses by walking a multi-level page table (four levels on x86-64), which would be ruinously slow on every access. The Translation Lookaside Buffer caches recent translations; a TLB miss costs a page walk, which is why huge pages (2 MB / 1 GB) help memory-heavy workloads."
    },
    {
        "domain": "OPERATING SYSTEMS",
        "title": "io_uring: Asynchronous I/O Without Syscall Tax",
        "text": "io_uring gives Linux two shared ring buffers between user space and kernel: a submission queue and a completion queue. Applications batch many I/O requests, submit them with one syscall (or none, with SQ polling), and reap completions by reading memory, cutting context switch overhead."
    }
]

LEXICON_VAULT = [
    {
        "word": "NONCE",
        "phonetic": "/nɒns/",
        "meta": "Cryptography • Latin 'pro nones'",
        "def": "A single-use pseudorandom value inserted into authentication challenges to guarantee freshness and prevent replay attacks.",
        "usage": "Reusing a cryptographic nonce in Galois/Counter Mode (AES-GCM) destroys authenticity and leaks key streams."
    },
    {
        "word": "TAINT ANALYSIS",
        "phonetic": "/teɪnt əˈnæl.ə.sɪs/",
        "meta": "Static / Dynamic Analysis • Software Security",
        "def": "Tracking untrusted user-controlled inputs (sources) along execution graphs to ensure they do not reach sensitive execution sinks without sanitation.",
        "usage": "The compiler identified an unescaped SQL statement by tracing untrusted HTTP parameters through the taint graph."
    },
    {
        "word": "COLLISION RESISTANCE",
        "phonetic": "/kəˈlɪʒ.ən rɪˈzɪs.təns/",
        "meta": "Information Theory • Cryptanalysis",
        "def": "The computational infeasibility of finding any two distinct pre-images x and y such that H(x) = H(y).",
        "usage": "SHA-1 lost cryptanalytic collision resistance after SHAttered demonstrated dual PDFs with identical hash digests."
    },
    {
        "word": "IDEMPOTENCY",
        "phonetic": "/ˌaɪ.dɛm.poʊˈtɛn.si/",
        "meta": "Distributed Systems • API Design",
        "def": "A property of an operation whereby applying it many times has the same effect as applying it once.",
        "usage": "Payment endpoints accept an idempotency key so a retried request after a timeout cannot charge the customer twice."
    }
]

SYSTEM_DESIGN_VAULT = [
    {
        "name": "Design a Distributed Cache",
        "tag": "PERFORMANCE • CONSISTENT HASHING",
        "problem": "Reduce database load and latency by keeping hot data in memory across many nodes.",
        "design": [
            "Partition keys across nodes with consistent hashing and virtual nodes so node churn moves only a minimal share of keys.",
            "Eviction policy: LRU/LFU when memory bounds are hit; enforce TTLs for stale data.",
            "Cache-aside pattern: read cache, hit fallback DB on miss, re-populate cache.",
            "Write-through guarantees strict consistency; write-back buffers writes for lower latency.",
        ],
        "tradeoffs": [
            "Cache stampede: concurrent requests missing expired keys hammer backing stores; fix with request coalescing and jittered TTLs.",
            "Cache penetration: queries for non-existent keys can be eliminated with Bloom filters.",
        ],
        "estimate": "100K reads/s with a 90% hit rate reduces database throughput requirements from 100K to 10K queries/s.",
        "question": "How do you maintain DB-cache consistency post-write?",
        "answer": "Update the database, then invalidate (delete) the cache entry rather than updating it directly. Let subsequent reads lazily warm the cache.",
    },
    {
        "name": "Design a Rate Limiter",
        "tag": "RELIABILITY • API GATEWAY",
        "problem": "Protect an API from abuse and overload by limiting each user, IP or API key to N requests per time window.",
        "design": [
            "Run it at the API gateway or middleware level before requests hit downstream application services.",
            "Token bucket algorithm: each key refills at a fixed rate; requests consume tokens or return HTTP 429 with Retry-After headers.",
            "Store counters atomically in Redis using Lua scripts to prevent concurrent read-modify-write races.",
        ],
        "tradeoffs": [
            "Centralized Redis introduces latency and network dependencies: choose whether to fail open or closed.",
            "In-memory local approximations are faster but deviate across large multi-instance clusters.",
        ],
        "estimate": "10M active keys with 50 bytes of state each consumes ~500 MB RAM, easily fitting in single-node Redis cache clusters.",
        "question": "If your Redis limiter crashes, what behavior should execute?",
        "answer": "Fail open with conservative local fallback limiters for regular traffic. Fail closed on authentication or payment endpoints.",
    }
]

COMMANDS_VAULT = [
    {"cmd": "strace -c -f ./prog", "what": "Summarises every syscall a program and its child threads make, with counts and elapsed time.", "why": "Instantly determines whether performance bottlenecks originate from block I/O, futex locks, or excessive syscall transitions."},
    {"cmd": "lsof -i :8080", "what": "Lists the active process ID holding a network port.", "why": "Resolves 'bind: address already in use' socket collisions instantly."},
    {"cmd": "git bisect start / bad / good <sha>", "what": "Performs binary search across git commit histories to isolate regression bugs.", "why": "Pinpoints the offending commit out of 1,000 revisions in approximately 10 steps."},
    {"cmd": "perf stat -d ./prog", "what": "Measures hardware performance counters including IPC, branch mispredictions, and L1/LLC cache misses.", "why": "Analyzes low-level architectural efficiency and CPU cache locality in running code."},
]

PAPERS_VAULT = [
    {"title": "The UNIX Time-Sharing System", "authors": "Ritchie & Thompson", "year": 1974, "why": "Introduced universal file descriptors, pipes, and process separation that structured modern POSIX operating systems."},
    {"title": "Reflections on Trusting Trust", "authors": "Ken Thompson", "year": 1984, "why": "Demonstrates self-replicating compiler backdoors, providing foundational context for modern supply-chain verification."},
    {"title": "Attention Is All You Need", "authors": "Vaswani et al.", "year": 2017, "why": "Introduced the Transformer architecture, replacing recurrent networks with self-attention across sequence modeling."},
]

PUZZLES_VAULT = [
    {"q": "An array holds n+1 integers, each between 1 and n. Exactly one value repeats. Find it in O(n) time and O(1) space without mutating the array.", "a": "Treat the array as a pointer graph (index -> array[index]). The duplicate creates a cycle. Apply Floyd's tortoise-and-hare cycle-finding algorithm to locate the cycle entrance."},
    {"q": "Why is mid = (lo + hi) / 2 a latent bug in binary search with 32-bit signed integers?", "a": "lo + hi can exceed 2^31 - 1 when array size is large, causing integer overflow into a negative value. Use mid = lo + (hi - lo) // 2."},
]

def pick_knowledge():
    return {
        "concepts": random.sample(CONCEPTS_VAULT, min(2, len(CONCEPTS_VAULT))),
        "lexicons": random.sample(LEXICON_VAULT, min(2, len(LEXICON_VAULT))),
        "lesson": random.choice(SYSTEM_DESIGN_VAULT),
        "command": random.choice(COMMANDS_VAULT),
        "paper": random.choice(PAPERS_VAULT),
        "puzzle": random.choice(PUZZLES_VAULT),
    }

# arXiv Background Fetcher
ARXIV_CATS = {"arxiv_ai": "cs.LG", "arxiv_sec": "cs.CR", "arxiv_sys": "cs.DC", "arxiv_pl": "cs.PL"}
ARXIV_CACHE = {}

def refresh_arxiv_forever():
    while True:
        for key, cat in ARXIV_CATS.items():
            url = ("https://export.arxiv.org/api/query?search_query=cat:" + cat +
                   "&sortBy=submittedDate&sortOrder=descending&max_results=3")
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=20) as r:
                    parsed = feedparser.parse(r.read())
                items = top_items(parsed.entries, 3, 26)
                if items:
                    ARXIV_CACHE[key] = items
            except Exception as exc:
                print(f"[arxiv] {cat} failed: {exc}")
            time.sleep(3.5)
        time.sleep(3 * 3600 if len(ARXIV_CACHE) == len(ARXIV_CATS) else 600)

# -------------------------------------------------------------
# F1, IMAGES & METADATA HELPERS
# -------------------------------------------------------------
JOLPICA = "https://api.jolpi.ca/ergast/f1"

SOURCE_NAMES = {
    "theguardian.com": "The Guardian", "espn.com": "ESPN", "atptour.com": "ATP Tour",
    "formula1.com": "Formula1.com", "dawn.com": "Dawn", "techcrunch.com": "TechCrunch",
    "thehackernews.com": "The Hacker News", "feedburner.com": "The Hacker News",
}

def source_name(url):
    host = urlparse(url or "").netloc.lower().removeprefix("www.")
    if host.endswith("bbc.co.uk") or host.endswith("bbc.com"):
        return "BBC Sport" if "/sport" in url else "BBC News"
    for dom, name in SOURCE_NAMES.items():
        if host.endswith(dom):
            return name
    return host

def fmt_published(entry):
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return ""
    try:
        dt = datetime(*t[:6], tzinfo=timezone.utc).astimezone(LOCAL_TZ)
        return dt.strftime("%d %b, %H:%M") + f" {LOCAL_TZ_LABEL}"
    except Exception:
        return ""

def extract_image(entry):
    url = None
    for attr in ("media_content", "media_thumbnail"):
        for m in getattr(entry, attr, None) or []:
            if m.get("url"):
                url = m["url"]
                break
        if url:
            break
    if not url:
        for enc in getattr(entry, "enclosures", None) or []:
            href = enc.get("href") or ""
            if href and (enc.get("type", "").startswith("image")
                         or href.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp"))):
                url = href
                break
    if not url:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)', getattr(entry, "summary", "") or "")
        url = m.group(1) if m else None
    if url and "ichef.bbci.co.uk" in url:
        url = re.sub(r"/(240|320|480|640)/", "/800/", url, count=1)
    return url

SESSION_LABELS = [
    ("FirstPractice", "Practice 1"), ("SecondPractice", "Practice 2"), ("ThirdPractice", "Practice 3"),
    ("SprintQualifying", "Sprint Qualifying"), ("SprintShootout", "Sprint Shootout"),
    ("Sprint", "Sprint"), ("Qualifying", "Qualifying"),
]

def _utc(date, time_):
    try:
        t = (time_ or "00:00:00Z").replace("Z", "")
        return datetime.fromisoformat(f"{date}T{t}").replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _fmt_local(dt):
    return dt.astimezone(LOCAL_TZ).strftime("%a %d %b, %H:%M") + f" {LOCAL_TZ_LABEL}"

async def fetch_json(session, urls):
    for u in urls:
        try:
            async with session.get(u, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
        except Exception:
            continue
    return None

async def build_f1_panel(session):
    stand_json, sched_json = await asyncio.gather(
        fetch_json(session, [f"{JOLPICA}/current/driverstandings/", f"{JOLPICA}/current/driverstandings.json"]),
        fetch_json(session, [f"{JOLPICA}/current.json?limit=40", f"{JOLPICA}/current/races/?limit=40"]),
    )
    panel = {"season": None, "round_done": None, "standings": [], "next": None, "upcoming": []}

    try:
        lists = stand_json["MRData"]["StandingsTable"]["StandingsLists"]
        if lists:
            panel["season"] = lists[0].get("season")
            panel["round_done"] = lists[0].get("round")
            for d in lists[0]["DriverStandings"][:5]:
                drv = d["Driver"]
                panel["standings"].append({
                    "pos": d.get("position"),
                    "driver": f'{drv.get("givenName", "")} {drv.get("familyName", "")}'.strip(),
                    "team": (d.get("Constructors") or [{}])[-1].get("name", ""),
                    "points": d.get("points"),
                })
    except Exception:
        pass

    try:
        races = sched_json["MRData"]["RaceTable"]["Races"]
        now = datetime.now(timezone.utc)
        upcoming = []
        for r in races:
            start = _utc(r.get("date"), r.get("time"))
            if start and start + timedelta(hours=3) >= now:
                upcoming.append((start, r))
        upcoming.sort(key=lambda x: x[0])
        if upcoming:
            start, r = upcoming[0]
            sessions = [(start, "Race")]
            for key, label in SESSION_LABELS:
                sd = r.get(key)
                if sd:
                    dt = _utc(sd.get("date"), sd.get("time"))
                    if dt:
                        sessions.append((dt, label))
            sessions.sort(key=lambda x: x[0])
            loc = r.get("Circuit", {}).get("Location", {})
            panel["next"] = {
                "round": r.get("round"),
                "name": r.get("raceName"),
                "circuit": r.get("Circuit", {}).get("circuitName", ""),
                "place": ", ".join(x for x in [loc.get("locality"), loc.get("country")] if x),
                "start_iso": start.isoformat(),
                "sessions": [{"label": l, "when": _fmt_local(dt)} for dt, l in sessions],
            }
            panel["upcoming"] = [
                {"round": rr.get("round"), "name": rr.get("raceName"),
                 "place": rr.get("Circuit", {}).get("Location", {}).get("country", ""),
                 "when": _fmt_local(st)}
                for st, rr in upcoming[1:4]
            ]
    except Exception:
        pass
    return panel

# -------------------------------------------------------------
# ASYNC SCRAPING ENGINE
# -------------------------------------------------------------
def clean_html(text, limit=60):
    if not text:
        return ""
    stripped = re.sub(r"<[^>]+>", " ", text)
    stripped = re.sub(r"^\s*arXiv:\S+\s+Announce Type:\s*\S+\s+Abstract:\s*", "", stripped)
    tokens = stripped.split()
    return " ".join(tokens[:limit]) + ("..." if len(tokens) > limit else "")

def top_items(entries, n, blurb_words):
    out = []
    for e in entries[:n]:
        title = " ".join(getattr(e, "title", "").split())
        if not title:
            continue
        out.append({
            "title": title,
            "link": getattr(e, "link", "#"),
            "blurb": clean_html(getattr(e, "summary", ""), blurb_words) if blurb_words else "",
        })
    return out

async def fetch_rss(session, category, url):
    for u in (url if isinstance(url, list) else [url]):
        try:
            async with session.get(u, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    parsed = feedparser.parse(await resp.text())
                    if parsed.entries:
                        return category, parsed.entries
        except Exception:
            continue
    return category, []

async def fetch_hn(session):
    try:
        async with session.get(HN_API, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                data = await resp.json()
                entries = []
                for h in data.get("hits", []):
                    title = h.get("title")
                    if not title:
                        continue
                    link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
                    entries.append(SimpleNamespace(title=title, link=link, summary=""))
                if entries:
                    return "hn", entries
    except Exception:
        pass
    return await fetch_rss(session, "hn", HN_RSS_FALLBACK)

async def probe_og_image(session, page_url):
    if not page_url or not page_url.startswith("http"):
        return None
    try:
        async with session.get(page_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                og = soup.find("meta", property="og:image")
                if og and og.get("content"):
                    return og["content"]
    except Exception:
        pass
    return None

async def build_edition_async():
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_rss(session, cat, url) for cat, url in FEEDS.items()]
        tasks.append(fetch_hn(session))
        gathered = await asyncio.gather(*tasks, build_f1_panel(session))
        feeds_data = dict(gathered[:-1])

        now_dt = datetime.now(LOCAL_TZ)
        date_display = now_dt.strftime("%A, %B %d, %Y")
        time_display = now_dt.strftime("%I:%M %p")

        edition = {
            "date": date_display,
            "time_now": time_display,
            "f1": gathered[-1]
        }

        img_probes = []
        parsed_stories = {}

        for cat in ["tech", "security", "pakistan", "world", "f1", "football", "tennis"]:
            entries = feeds_data.get(cat, [])
            if entries:
                lead = entries[0]
                link = getattr(lead, "link", "#")
                img = extract_image(lead)

                parsed_stories[cat] = {
                    "title": getattr(lead, "title", "Dispatch Pending").strip(),
                    "summary": clean_html(getattr(lead, "summary", ""), limit=55),
                    "link": link,
                    "img": img,
                    "source": source_name(link),
                    "published": fmt_published(lead),
                    "bullets": [e.title.strip() for e in entries[1:4] if hasattr(e, "title")],
                }

                if not img and cat in ["tech", "security", "f1", "football", "tennis"]:
                    img_probes.append((cat, probe_og_image(session, link)))
            else:
                parsed_stories[cat] = {
                    "title": "Wire Service Delayed",
                    "summary": "Correspondents are transmitting telemetry cables shortly.",
                    "link": "#", "img": None, "bullets": [], "source": "", "published": "",
                }

        if img_probes:
            probe_results = await asyncio.gather(*[p[1] for p in img_probes])
            for (cat, _), resolved_img in zip(img_probes, probe_results):
                if resolved_img:
                    parsed_stories[cat]["img"] = resolved_img

        edition["lists"] = {
            key: top_items(feeds_data.get(key, []), n, words)
            for key, (n, words) in LIST_FEEDS.items()
        }

        edition["stories"] = parsed_stories
        return edition

# -------------------------------------------------------------
# RETRO NEWSPRINT HTML TEMPLATE
# -------------------------------------------------------------
TEMPLATE = """
{% macro source_tag(s) %}{% if s.source and s.link and s.link != "#" %}<div class="source">Source: <a href="{{ s.link }}" target="_blank" rel="noopener">{{ s.source }}</a>{% if s.published %} &bull; {{ s.published }}{% endif %}</div>{% endif %}{% endmacro %}
{% macro notice_item(n, kind, schedule_label, topics_label) %}
<li class="notice" style="margin-bottom: 8px;">
    <div class="notice-body">
        <strong style="text-transform: uppercase;">[{{ n.course }}]</strong>
        <div style="font-size: 10px; font-weight: bold; color: var(--accent-red); margin-top: 1px;">
            {{ schedule_label }}: {{ n.schedule }}
        </div>
        <div class="copy" style="font-size: 10px; margin-top: 2px;">
            <strong>{{ topics_label }}:</strong> {{ n.topics }}
        </div>
        <div class="blurb">
            <a href="{{ n.link }}" target="_blank" rel="noopener">Open in Classroom &rarr;</a> ({{ n.posted_on }})
        </div>
        <label class="mark-read">
            <input type="checkbox" class="notice-check"
                   data-id="{{ n.notice_id }}" data-kind="{{ kind }}"
                   data-course="{{ n.course }}" data-title="{{ n.title }}">
            Mark as read
        </label>
    </div>
    <div class="notice-undo" hidden>Marked as read. <a href="#" class="undo-link">Undo</a></div>
</li>
{% endmacro %}
{% macro sport_box(title, s, img_h=100) %}
<div class="panel-box">
    <div class="box-title">{{ title }}</div>
    {% if s.img %}
    <div class="photo-frame" style="height: {{ img_h }}px;">
        <img src="{{ s.img }}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.parentElement.style.display='none'">
    </div>
    {% endif %}
    <h3 class="section-headline"><a href="{{ s.link }}" target="_blank" rel="noopener">{{ s.title }}</a></h3>
    <p class="copy">{{ s.summary }}</p>
    {% if s.bullets %}
    <ul class="bullets-list">
        {% for b in s.bullets %}<li>{{ b }}</li>{% endfor %}
    </ul>
    {% endif %}
    {{ source_tag(s) }}
</div>
{% endmacro %}
{% macro feed_box(title, rows, blurbs=False) %}
<div class="panel-box">
    <div class="box-title">{{ title }}</div>
    {% if rows %}
    <ul class="bullets-list plain">
        {% for r in rows %}
        <li>
            <a href="{{ r.link }}" target="_blank" rel="noopener">{{ r.title }}</a>
            {% if blurbs and r.blurb %}<div class="blurb">{{ r.blurb }}</div>{% endif %}
        </li>
        {% endfor %}
    </ul>
    {% else %}
    <p class="copy"><em>Wire delayed. Try again shortly.</em></p>
    {% endif %}
</div>
{% endmacro %}
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
    <title>The Daily Pocket Broadsheet</title>

    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Cinzel+Decorative:wght@700;900&family=Playfair+Display:ital,wght@0,700;0,900;1,400&family=Old+Standard+TT:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">

    <style>
        :root {
            --bg-paper: #f4ecdc;
            --ink: #161311;
            --ink-faint: #433e38;
            --rule: #231f20;
            --box: rgba(0,0,0,0.035);
            --accent-red: #7b1616;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            background: #cbc1ad;
            display: flex;
            justify-content: center;
            padding: 10px;
            font-family: 'Old Standard TT', Georgia, serif;
            color: var(--ink);
            -webkit-font-smoothing: antialiased;
        }

        .broadsheet {
            background-color: var(--bg-paper);
            background-image:
                radial-gradient(rgba(0,0,0,0.035) 1px, transparent 0),
                radial-gradient(rgba(0,0,0,0.02) 1px, transparent 0);
            background-size: 8px 8px, 16px 16px;
            max-width: 820px;
            width: 100%;
            border: 2px solid var(--rule);
            box-shadow: 0 14px 40px rgba(0,0,0,0.35);
            padding: 16px;
            display: flex;
            flex-direction: column;
        }

        .ears-row {
            display: flex;
            justify-content: space-between;
            font-size: 9px;
            font-weight: 700;
            letter-spacing: 0.5px;
            border-bottom: 1px solid var(--rule);
            padding-bottom: 3px;
            margin-bottom: 6px;
            text-transform: uppercase;
        }

        .masthead {
            display: flex;
            align-items: center;
            justify-content: space-between;
            border-bottom: 2px solid var(--rule);
            padding-bottom: 4px;
        }
        .masthead-title {
            font-family: 'Cinzel Decorative', serif;
            font-size: clamp(22px, 5.8vw, 36px);
            font-weight: 900;
            text-align: center;
            flex-grow: 1;
            letter-spacing: 2px;
            text-transform: uppercase;
            line-height: 1;
        }

        .issue-bar {
            display: flex;
            justify-content: space-between;
            font-size: 9.5px;
            font-weight: 700;
            letter-spacing: 1px;
            border-top: 1px solid var(--rule);
            border-bottom: 2px solid var(--rule);
            padding: 3px 2px;
            margin-top: 4px;
            margin-bottom: 12px;
            text-transform: uppercase;
        }

        a { color: var(--ink); text-decoration: none; }
        a:hover { text-decoration: underline; }

        .section-banner {
            font-family: 'Playfair Display', serif;
            font-size: 12px;
            font-weight: 900;
            letter-spacing: 3px;
            text-align: center;
            text-transform: uppercase;
            border-top: 2px solid var(--rule);
            border-bottom: 1px solid var(--rule);
            padding: 3px 0;
            margin: 4px 0 10px 0;
        }

        .lead-headline {
            font-family: 'Playfair Display', serif;
            font-size: 19px;
            font-weight: 900;
            line-height: 1.2;
            margin-bottom: 6px;
            text-transform: uppercase;
        }

        .section-headline {
            font-family: 'Playfair Display', serif;
            font-size: 13.5px;
            font-weight: 900;
            line-height: 1.25;
            margin-bottom: 4px;
            text-transform: uppercase;
        }

        .copy {
            font-size: 11px;
            line-height: 1.45;
            text-align: justify;
        }

        .drop-cap::first-letter {
            font-family: 'Playfair Display', serif;
            font-size: 3.4em;
            float: left;
            line-height: 0.75;
            margin-right: 7px;
            margin-top: 4px;
            font-weight: 900;
        }

        .grid-2 {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            border-bottom: 1px solid var(--rule);
            padding-bottom: 12px;
            margin-bottom: 12px;
        }

        .grid-2 > *, .bottom-grid > * { min-width: 0; }
        .copy, .section-headline, .bullets-list li { overflow-wrap: anywhere; }

        .panel-box {
            border: 1.5px solid var(--rule);
            padding: 8px;
            background: var(--box);
        }

        .box-title {
            font-family: 'Playfair Display', serif;
            font-size: 11px;
            font-weight: 900;
            text-align: center;
            border-bottom: 1px solid var(--rule);
            padding-bottom: 2px;
            margin-bottom: 6px;
            letter-spacing: 1px;
            text-transform: uppercase;
        }

        .tech-dossier {
            border: 2px solid var(--rule);
            background: rgba(0, 0, 0, 0.035);
            padding: 9px 11px;
            margin-bottom: 12px;
            border-bottom: 2px double var(--rule);
        }
        .dossier-tag {
            font-size: 9px;
            font-weight: 700;
            color: var(--accent-red);
            letter-spacing: 1px;
            text-transform: uppercase;
        }
        .dossier-name {
            font-family: 'Playfair Display', serif;
            font-size: 15px;
            font-weight: 900;
            margin-bottom: 3px;
            text-transform: uppercase;
        }

        .photo-frame {
            border: 1px solid var(--rule);
            background: #ded7c6;
            height: 120px;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            margin-bottom: 4px;
        }
        .photo-frame img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            filter: grayscale(30%) contrast(108%);
        }

        .bullets-list {
            list-style: none;
            margin-top: 6px;
            border-top: 1px dashed #7c7467;
            padding-top: 4px;
        }
        .bullets-list.plain { margin-top: 0; border-top: none; padding-top: 0; }
        .bullets-list li {
            font-size: 10.5px;
            line-height: 1.35;
            margin-bottom: 4px;
            padding-left: 9px;
            position: relative;
        }
        .bullets-list li::before {
            content: "•";
            position: absolute;
            left: 0;
            font-weight: bold;
        }
        .blurb {
            font-size: 9.5px;
            font-style: italic;
            color: var(--ink-faint);
            line-height: 1.35;
            margin-top: 1px;
        }

        .paper-fold {
            border-top: 1px dashed rgba(35, 31, 32, 0.45);
            margin: 6px 0 12px 0;
        }

        .source {
            font-size: 9px;
            font-style: italic;
            letter-spacing: 0.3px;
            color: var(--ink-faint);
            margin-top: 5px;
            border-top: 1px dotted #7c7467;
            padding-top: 2px;
        }
        .source a { text-decoration: underline; }
        .tbl { width: 100%; border-collapse: collapse; font-size: 10.5px; margin: 4px 0; }
        .tbl th { text-align: left; font-size: 9px; letter-spacing: 1px; text-transform: uppercase; border-bottom: 1px solid var(--rule); padding: 2px 3px; }
        .tbl td { padding: 3px; border-bottom: 1px dotted #7c7467; vertical-align: top; }
        .tbl td:last-child, .tbl th:last-child { text-align: right; }
        .race-name { font-family: 'Playfair Display', serif; font-size: 15px; font-weight: 900; text-transform: uppercase; line-height: 1.2; }
        .countdown { font-size: 11px; font-weight: 700; color: var(--accent-red); letter-spacing: 0.5px; margin: 4px 0; }

        .mark-read {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            margin-top: 3px;
            font-size: 9.5px;
            font-weight: 700;
            letter-spacing: 0.5px;
            text-transform: uppercase;
            cursor: pointer;
            user-select: none;
        }
        .mark-read input { accent-color: var(--accent-red); cursor: pointer; }
        .notice-undo { font-size: 10px; font-style: italic; color: var(--ink-faint); }
        .notice-undo a { text-decoration: underline; }

        .sd-label {
            font-size: 9.5px;
            font-weight: 700;
            letter-spacing: 1.5px;
            text-transform: uppercase;
            color: var(--accent-red);
            margin: 8px 0 3px 0;
        }
        .mono {
            font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
            font-size: 10px;
            background: rgba(0,0,0,0.07);
            padding: 2px 5px;
            display: inline-block;
            margin-bottom: 4px;
            word-break: break-all;
        }
        .meta-line {
            font-size: 9.5px;
            font-style: italic;
            color: var(--ink-faint);
            margin: 2px 0 4px 0;
        }

        details.reveal { margin-top: 6px; }
        details.reveal summary {
            cursor: pointer;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 1px;
            text-transform: uppercase;
            color: var(--accent-red);
        }
        details.reveal p { margin-top: 4px; }

        .bottom-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }

        .btn-refresh {
            background: var(--ink);
            color: var(--bg-paper);
            border: none;
            padding: 6px 14px;
            font-family: inherit;
            font-size: 10px;
            letter-spacing: 1px;
            cursor: pointer;
            margin-top: 14px;
            display: block;
            width: 100%;
            text-transform: uppercase;
            font-weight: bold;
        }

        @media (max-width: 560px) {
            .grid-2, .bottom-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>

    <div class="broadsheet">
        <div class="ears-row">
            <div>ISLAMABAD &amp; GLOBAL WIRE &bull; {{ time_now }} {{ tz_label }}</div>
            <div>SYS: ZERO-DAY &amp; TELEMETRY MONITORS</div>
        </div>

        <div class="masthead">
            <span style="font-size: 26px;">🏛️</span>
            <h1 class="masthead-title">The Daily Dispatch</h1>
            <span style="font-size: 26px;">🏺</span>
        </div>

        <div class="issue-bar">
            <span>VOL. IV &bull; NO. 112</span>
            <span>DOMESTIC DISPATCH, MACHINE SYSTEMS &amp; ATHLETICS</span>
            <span>{{ date }}</span>
        </div>

        <!-- ============ ACADEMIC NOTICEBOARD (SECTION 5E: TASKS, QUIZZES & VIVAS) ============ -->
        <div class="section-banner">ACADEMIC NOTICEBOARD &bull; SECTION 5E DISPATCH</div>

        <!-- Row 1: Priority Tasks -->
        <div class="panel-box" style="margin-bottom: 12px;">
            <div class="box-title">ASSIGNED TASKS &bull; ORDERED BY PRIORITY</div>
            {% if classroom.assignments %}
            <ul class="bullets-list plain">
                {% for item in classroom.assignments %}
                <li style="margin-bottom: 7px;">
                    <span style="font-size: 8.5px; font-weight: bold; background: #e0d5c0; padding: 1px 4px; border: 1px solid var(--rule);">
                        {{ item.priority_badge }}
                    </span>
                    <strong style="margin-left: 3px;">[{{ item.course }}]</strong> &bull;
                    <a href="{{ item.link }}" target="_blank" rel="noopener">{{ item.title }}</a>
                    <div class="blurb" style="color: var(--accent-red); font-weight: bold; font-style: normal;">
                        Deadline: {{ item.due }}
                    </div>
                </li>
                {% endfor %}
            </ul>
            {% elif not classroom.connected %}
            <p class="copy"><em>Classroom API not linked. Place credentials.json in project root.</em></p>
            {% else %}
            <p class="copy"><em>All caught up. No pending submissions assigned for Section 5E in the last 30 days.</em></p>
            {% endif %}
        </div>

        <!-- Row 2: Split Quizzes & Vivas -->
        <div class="grid-2">
            <!-- Quiz Notices -->
            <div class="panel-box">
                <div class="box-title">UPCOMING QUIZ ASSESSMENTS</div>
                {% if classroom.quizzes %}
                <ul class="bullets-list plain">
                    {% for q in classroom.quizzes %}
                    {{ notice_item(q, "quiz", "Date", "Topics") }}
                    {% endfor %}
                </ul>
                {% elif not classroom.connected %}
                <p class="copy"><em>Classroom API not linked.</em></p>
                {% else %}
                <p class="copy"><em>No upcoming quizzes scheduled for Section 5E.</em></p>
                {% endif %}
            </div>

            <!-- Viva & Oral Exam Notices -->
            <div class="panel-box">
                <div class="box-title">ORAL EVALUATIONS &amp; VIVA SCHEDULE</div>
                {% if classroom.vivas %}
                <ul class="bullets-list plain">
                    {% for v in classroom.vivas %}
                    {{ notice_item(v, "viva", "Scheduled For", "Preparation Scope") }}
                    {% endfor %}
                </ul>
                {% elif not classroom.connected %}
                <p class="copy"><em>Classroom API not linked.</em></p>
                {% else %}
                <p class="copy"><em>No upcoming viva evaluations.</em></p>
                {% endif %}
            </div>
        </div>
        {% if classroom.hidden_count %}
        <div class="source" style="margin-top: -6px; margin-bottom: 12px;">
            {{ classroom.hidden_count }} notice{{ "s" if classroom.hidden_count != 1 }} marked as read &bull;
            <a href="#" id="restore-all">Restore all</a>
        </div>
        {% endif %}

        <!-- ============ FRONT PAGE: TECH & SECURITY ============ -->
        <div class="grid-2">
            <div>
                <div class="box-title">SILICON &amp; NEURAL AGENTS</div>
                <h2 class="section-headline">
                    <a href="{{ stories.tech.link }}" target="_blank">{{ stories.tech.title }}</a>
                </h2>
                {% if stories.tech.img %}
                <div class="photo-frame">
                    <img src="{{ stories.tech.img }}" onerror="this.style.display='none'">
                </div>
                {% endif %}
                <p class="copy drop-cap">{{ stories.tech.summary }}</p>
                {{ source_tag(stories.tech) }}
            </div>

            <div class="panel-box">
                <div class="box-title">CYBER DEFENSE &amp; ZERO-DAYS</div>
                <h3 class="section-headline">
                    <a href="{{ stories.security.link }}" target="_blank">{{ stories.security.title }}</a>
                </h3>
                <p class="copy">{{ stories.security.summary }}</p>
                {{ source_tag(stories.security) }}
                {% if stories.security.bullets %}
                <ul class="bullets-list">
                    {% for item in stories.security.bullets %}
                    <li>{{ item }}</li>
                    {% endfor %}
                </ul>
                {% endif %}
            </div>
        </div>

        <!-- ============ DOSSIERS ============ -->
        {% for c in concepts %}
        <div class="tech-dossier">
            <div class="dossier-tag">SPECIAL INVESTIGATIVE DOSSIER &bull; {{ c.domain }}</div>
            <div class="dossier-name">{{ c.title }}</div>
            <p class="copy">{{ c.text }}</p>
        </div>
        {% endfor %}

        <!-- ============ THE PROGRAMMERS' DESK ============ -->
        <div class="section-banner">THE PROGRAMMERS' DESK</div>
        <div class="grid-2">
            {{ feed_box("HACKER NEWS &bull; FRONT PAGE", lists.hn) }}
            {{ feed_box("LOBSTERS &bull; TECH DISCUSSION", lists.lobsters) }}
        </div>
        <div class="grid-2">
            {{ feed_box("LINUX & KERNEL &bull; LWN", lists.lwn) }}
            {{ feed_box("ARS TECHNICA &bull; TECH LAB", lists.ars) }}
        </div>

        <!-- ============ SYSTEM DESIGN SCHOOL ============ -->
        <div class="section-banner">SYSTEM DESIGN SCHOOL</div>
        <div class="panel-box" style="margin-bottom:12px;">
            <div class="box-title">HOW TO ATTACK ANY DESIGN QUESTION</div>
            <p class="copy"><strong>1. Clarify</strong> requirements: features, scale, read/write ratio, latency and consistency bounds.
            <strong>2. Estimate</strong> traffic and storage on the back of an envelope.
            <strong>3. Sketch</strong> high-level topology: clients, LB, services, database, cache, message broker.
            <strong>4. Deep-dive</strong> bottleneck areas: partitioning, replication, index layouts.
            <strong>5. Discuss trade-offs</strong> and edge failure modes out loud.</p>
        </div>

        <div class="tech-dossier">
            <div class="dossier-tag">SYSTEM DESIGN LESSON &bull; {{ lesson.tag }}</div>
            <div class="dossier-name">{{ lesson.name }}</div>
            <p class="copy"><strong>The problem:</strong> {{ lesson.problem }}</p>

            <div class="sd-label">Core design</div>
            <ul class="bullets-list plain">
                {% for d in lesson.design %}<li>{{ d }}</li>{% endfor %}
            </ul>

            <div class="sd-label">Trade-offs</div>
            <ul class="bullets-list plain">
                {% for t in lesson.tradeoffs %}<li>{{ t }}</li>{% endfor %}
            </ul>

            <div class="meta-line"><strong>Back-of-envelope:</strong> {{ lesson.estimate }}</div>

            <details class="reveal">
                <summary>Think like an interviewer</summary>
                <p class="copy"><strong>{{ lesson.question }}</strong></p>
                <p class="copy">{{ lesson.answer }}</p>
            </details>
        </div>

        <div class="section-banner">CRAFT &amp; PRACTICE</div>
        <div class="grid-2">
            <div class="panel-box">
                <div class="box-title">TERMINAL TRICK OF THE DAY</div>
                <span class="mono">$ {{ command.cmd }}</span>
                <p class="copy"><strong>What:</strong> {{ command.what }}</p>
                <p class="copy" style="margin-top:3px;"><strong>Why learn it:</strong> {{ command.why }}</p>
            </div>
            <div class="panel-box">
                <div class="box-title">BRAIN TEASER &bull; INTERVIEW DESK</div>
                <p class="copy">{{ puzzle.q }}</p>
                <details class="reveal">
                    <summary>Reveal answer</summary>
                    <p class="copy">{{ puzzle.a }}</p>
                </details>
            </div>
        </div>

        <!-- ============ RESEARCH DESK ============ -->
        <div class="section-banner">THE RESEARCH DESK &bull; FRESH FROM ARXIV</div>
        <div class="grid-2">
            {{ feed_box("MACHINE LEARNING (cs.LG)", lists.arxiv_ai, True) }}
            {{ feed_box("CRYPTO & SECURITY (cs.CR)", lists.arxiv_sec, True) }}
        </div>
        <div class="grid-2">
            {{ feed_box("DISTRIBUTED & PARALLEL (cs.DC)", lists.arxiv_sys, True) }}
            {{ feed_box("PROGRAMMING LANGUAGES (cs.PL)", lists.arxiv_pl, True) }}
        </div>

        <div class="tech-dossier">
            <div class="dossier-tag">CLASSIC PAPER &bull; READ THE ORIGINAL</div>
            <div class="dossier-name">{{ paper.title }}</div>
            <div class="meta-line">{{ paper.authors }} &bull; {{ paper.year }}</div>
            <p class="copy">{{ paper.why }}</p>
        </div>

        <!-- ============ MOBILE & PLATFORMS ============ -->
        <div class="section-banner">MOBILE &amp; PLATFORMS</div>
        <div class="grid-2">
            {{ feed_box("SWIFT.ORG BLOG", lists.swift) }}
            {{ feed_box("FLUTTER & DART", lists.flutter) }}
        </div>

        <div class="paper-fold"></div>

        <!-- ============ DOMESTIC & WORLD ============ -->
        <div class="grid-2">
            <div class="panel-box">
                <div class="box-title">PAKISTAN DOMESTIC WIRE (DAWN)</div>
                <h3 class="section-headline">
                    <a href="{{ stories.pakistan.link }}" target="_blank">{{ stories.pakistan.title }}</a>
                </h3>
                <p class="copy">{{ stories.pakistan.summary }}</p>
                {{ source_tag(stories.pakistan) }}
                {% if stories.pakistan.bullets %}
                <ul class="bullets-list">
                    {% for item in stories.pakistan.bullets %}
                    <li>{{ item }}</li>
                    {% endfor %}
                </ul>
                {% endif %}
            </div>

            <div class="panel-box">
                <div class="box-title">GLOBAL DISPATCH (WORLD WIRE)</div>
                <h3 class="section-headline">
                    <a href="{{ stories.world.link }}" target="_blank">{{ stories.world.title }}</a>
                </h3>
                <p class="copy">{{ stories.world.summary }}</p>
                {{ source_tag(stories.world) }}
                {% if stories.world.bullets %}
                <ul class="bullets-list">
                    {% for item in stories.world.bullets %}
                    <li>{{ item }}</li>
                    {% endfor %}
                </ul>
                {% endif %}
            </div>
        </div>

        <!-- ============ SPORTS ============ -->
        <div class="section-banner">THE SPORTS DESK</div>
        <div class="grid-2">
            {{ sport_box("FORMULA 1 GRAND PRIX", stories.f1, 110) }}

            <div class="panel-box">
                <div class="box-title">DRIVERS' CHAMPIONSHIP &bull; TOP 5{% if f1 and f1.season %} &bull; {{ f1.season }}{% endif %}</div>
                {% if f1 and f1.standings %}
                <table class="tbl">
                    <tr><th>#</th><th>Driver</th><th>Team</th><th>Pts</th></tr>
                    {% for r in f1.standings %}
                    <tr><td>{{ r.pos }}</td><td><strong>{{ r.driver }}</strong></td><td>{{ r.team }}</td><td>{{ r.points }}</td></tr>
                    {% endfor %}
                </table>
                <div class="source">Source: Jolpica F1 API (Ergast-compatible){% if f1.round_done %} &bull; after round {{ f1.round_done }}{% endif %}</div>
                {% else %}
                <p class="copy"><em>Standings unavailable right now.</em></p>
                {% endif %}
            </div>
        </div>

        <div class="grid-2">
            <div class="panel-box">
                <div class="box-title">NEXT RACE WEEKEND</div>
                {% if f1 and f1.next %}
                <div class="sd-label" style="margin-top:0;">Round {{ f1.next.round }}</div>
                <div class="race-name">{{ f1.next.name }}</div>
                <div class="meta-line">{{ f1.next.circuit }} &bull; {{ f1.next.place }}</div>
                <div class="countdown" id="f1-countdown" data-ts="{{ f1.next.start_iso }}"></div>
                <table class="tbl">
                    {% for x in f1.next.sessions %}
                    <tr><td>{{ x.label }}</td><td>{{ x.when }}</td></tr>
                    {% endfor %}
                </table>
                <div class="source">Source: Jolpica F1 API &bull; times shown in {{ tz_label }}</div>
                {% else %}
                <p class="copy"><em>No upcoming race found.</em></p>
                {% endif %}
            </div>

            <div class="panel-box">
                <div class="box-title">THE RUN-IN &bull; UPCOMING RACES</div>
                {% if f1 and f1.upcoming %}
                <table class="tbl">
                    {% for u in f1.upcoming %}
                    <tr><td><strong>R{{ u.round }}</strong> {{ u.name }}<div class="blurb">{{ u.place }}</div></td><td>{{ u.when }}</td></tr>
                    {% endfor %}
                </table>
                <div class="source">Source: Jolpica F1 API &bull; race start times in {{ tz_label }}</div>
                {% else %}
                <p class="copy"><em>No further races listed.</em></p>
                {% endif %}
            </div>
        </div>

        <div class="grid-2">
            {{ sport_box("FOOTBALL", stories.football, 100) }}
            {{ sport_box("TENNIS", stories.tennis, 100) }}
        </div>

        <!-- ============ LEXICON ============ -->
        <div class="bottom-grid">
            {% for lx in lexicons %}
            <div class="panel-box">
                <div class="box-title" style="text-align: left;">LEXICON OF THE DAY</div>
                <div style="font-family:'Playfair Display', serif; font-size:14px; font-weight:900;">{{ lx.word }}</div>
                <div style="font-size:9.5px; font-style:italic; color:#555;">{{ lx.phonetic }} • {{ lx.meta }}</div>
                <p class="copy" style="margin: 4px 0;">{{ lx.def }}</p>
                <div style="font-size:9.5px; font-style:italic; border-left:2px solid var(--rule); padding-left:6px;">"{{ lx.usage }}"</div>
            </div>
            {% endfor %}
        </div>

        <button class="btn-refresh" onclick="location.reload()">Fetch Next Edition</button>
    </div>

    <script>
    (function () {
        var el = document.getElementById('f1-countdown');
        if (!el) return;
        var t = Date.parse(el.dataset.ts);
        function tick() {
            var d = t - Date.now();
            if (isNaN(d)) { el.textContent = ''; return; }
            if (d <= 0) { el.textContent = 'Lights out! Race weekend is live.'; return; }
            var D = Math.floor(d / 864e5), H = Math.floor(d % 864e5 / 36e5), M = Math.floor(d % 36e5 / 6e4);
            el.textContent = 'Lights out in ' + D + 'd ' + H + 'h ' + M + 'm';
        }
        tick(); setInterval(tick, 30000);
    })();

    // ---- Classroom notices: mark as read / undo / restore all ----
    (function () {
        function post(url, body) {
            return fetch(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            }).then(function (r) { return r.json(); });
        }

        document.addEventListener('change', function (e) {
            var cb = e.target;
            if (!cb.classList || !cb.classList.contains('notice-check') || !cb.checked) return;
            var li = cb.closest('.notice');
            post('/notice/dismiss', {
                id: cb.dataset.id, kind: cb.dataset.kind,
                course: cb.dataset.course, title: cb.dataset.title
            }).then(function (res) {
                if (res.ok) {
                    li.querySelector('.notice-body').hidden = true;
                    li.querySelector('.notice-undo').hidden = false;
                } else { cb.checked = false; }
            }).catch(function () { cb.checked = false; });
        });

        document.addEventListener('click', function (e) {
            var t = e.target;
            if (t.classList && t.classList.contains('undo-link')) {
                e.preventDefault();
                var li = t.closest('.notice');
                var cb = li.querySelector('.notice-check');
                post('/notice/restore', {id: cb.dataset.id}).then(function () {
                    cb.checked = false;
                    li.querySelector('.notice-body').hidden = false;
                    li.querySelector('.notice-undo').hidden = true;
                });
            } else if (t.id === 'restore-all') {
                e.preventDefault();
                post('/notice/restore', {}).then(function () { location.reload(); });
            }
        });
    })();
    </script>
</body>
</html>
"""

# -------------------------------------------------------------
# NOTICE ROUTES (mark as read / restore)
# -------------------------------------------------------------
@app.post("/notice/dismiss")
def notice_dismiss():
    data = request.get_json(silent=True) or {}
    notice_id = str(data.get("id", ""))[:200]
    kind = data.get("kind")
    if not notice_id or kind not in ("quiz", "viva"):
        return jsonify(ok=False), 400
    dismiss_notice(notice_id, kind, str(data.get("course", ""))[:120], str(data.get("title", ""))[:200])
    return jsonify(ok=True, hidden=count_dismissed())

@app.post("/notice/restore")
def notice_restore():
    data = request.get_json(silent=True) or {}
    notice_id = str(data.get("id", ""))[:200] or None   # no id -> restore everything
    restore_notice(notice_id)
    return jsonify(ok=True, hidden=count_dismissed())

# News is cached for 30 minutes; learning content and classroom updates re-evaluate on reload
CACHE_STORAGE = {"ts": 0, "payload": None}

@app.route("/")
def index():
    now = datetime.now().timestamp()
    if CACHE_STORAGE["payload"] is None or (now - CACHE_STORAGE["ts"] > 1800):
        CACHE_STORAGE["payload"] = asyncio.run(build_edition_async())
        CACHE_STORAGE["ts"] = now

    context = dict(CACHE_STORAGE["payload"])
    context["lists"] = {**context["lists"], **{k: ARXIV_CACHE.get(k, []) for k in ARXIV_CATS}}
    context.update(pick_knowledge())

    # Live Classroom data fetched on request
    context["classroom"] = fetch_classroom_digest()

    # Fresh current date/time on reload
    now_dt = datetime.now(LOCAL_TZ)
    context["date"] = now_dt.strftime("%A, %B %d, %Y")
    context["time_now"] = now_dt.strftime("%I:%M %p")
    context["tz_label"] = LOCAL_TZ_LABEL

    return render_template_string(TEMPLATE, **context)

DEBUG = True

if __name__ == "__main__":
    if not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        threading.Thread(target=refresh_arxiv_forever, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=DEBUG)