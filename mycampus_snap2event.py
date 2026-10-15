import os
import re
import io
import sqlite3
import requests
from datetime import datetime, date, time, timedelta
from contextlib import contextmanager

import streamlit as st

# Optional document/image extraction dependencies. Install from requirements.txt.
try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

# Optional MySQL support. The app works with SQLite if MySQL credentials are not set.
try:
    import mysql.connector
except ImportError:
    mysql = None

st.set_page_config(
    page_title="MyCampus | Your Event Journey",
    page_icon="🎒",
    layout="wide",
    initial_sidebar_state="expanded",
)


def set_flash(message, kind="success"):
    """Keep action feedback visible after Streamlit reruns the page."""
    st.session_state["mycampus_flash"] = {"message": message, "kind": kind}



# ---------- Snap 2 Event: OCR + TinyFish verification ----------
TINYFISH_SEARCH_URL = "https://api.search.tinyfish.ai"
TINYFISH_FETCH_URL = "https://api.fetch.tinyfish.ai"

def extract_uploaded_text(uploaded_file):
    """Extract text from an uploaded image or PDF. OCR requires the Tesseract binary."""
    if uploaded_file is None:
        return ""
    name = (uploaded_file.name or "").lower()
    raw = uploaded_file.getvalue()

    if name.endswith(".pdf"):
        if PdfReader is None:
            raise RuntimeError("PDF support is missing. Install the pypdf package.")
        reader = PdfReader(io.BytesIO(raw))
        pages = []
        for page in reader.pages[:10]:
            pages.append(page.extract_text() or "")
        text = "\\n".join(pages).strip()
        if text:
            return text
        raise RuntimeError(
            "This PDF appears to be scanned or image-only. Please upload its pages "
            "as images, or use a searchable PDF."
        )

    if Image is None or pytesseract is None:
        raise RuntimeError(
            "Image OCR dependencies are missing. Install Pillow and pytesseract, "
            "and install the Tesseract OCR system application."
        )
    try:
        image = Image.open(io.BytesIO(raw))
        return pytesseract.image_to_string(image).strip()
    except Exception as exc:
        raise RuntimeError(
            "Could not read this image. Check that it is a valid PNG/JPG and that "
            "Tesseract OCR is installed."
        ) from exc


def tinyfish_headers():
    """Read the TinyFish key from Streamlit secrets or environment, never hard-code it."""
    key = os.getenv("TINYFISH_API_KEY", "").strip()
    try:
        key = key or str(st.secrets.get("TINYFISH_API_KEY", "")).strip()
    except Exception:
        pass
    if not key:
        raise RuntimeError(
            "TinyFish API key is not configured. Add TINYFISH_API_KEY to "
            "Streamlit secrets or your environment variables."
        )
    return {"X-API-Key": key}


def normalize_web_url(url):
    url = (url or "").strip()
    if url and not url.startswith(("https://", "http://")):
        url = "https://" + url
    return url


def tinyfish_search_event(ocr_text, max_results=6):
    """Search the live web for independent sources related to the uploaded event."""
    # Keep the query concise and avoid sending an entire uploaded document.
    cleaned = re.sub(r"\s+", " ", ocr_text).strip()
    query = "verify event announcement " + cleaned[:420]
    response = requests.get(
        TINYFISH_SEARCH_URL,
        headers=tinyfish_headers(),
        params={"query": query, "location": "IN", "language": "en"},
        timeout=35,
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results", [])[:max_results]
    for item in results:
        item["url"] = normalize_web_url(item.get("url", ""))
    return results


def tinyfish_fetch_sources(urls):
    """Fetch clean page content from the most relevant search results."""
    if not urls:
        return []
    response = requests.post(
        TINYFISH_FETCH_URL,
        headers={**tinyfish_headers(), "Content-Type": "application/json"},
        json={"urls": urls[:4], "format": "markdown"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def tinyfish_agent_verify(source_url, ocr_text):
    """Optional deeper check against one selected page; Agent usage may be metered."""
    goal = (
        "You are checking a campus event announcement for accuracy, not assuming it is fake. "
        "Open and inspect this website. Compare only facts supported by the page with the "
        "user-provided poster text below. Return valid JSON with keys: "
        "verdict (one of 'corroborated', 'partially_corroborated', 'contradicted', "
        "'unable_to_verify'), summary, matching_claims (array), conflicting_claims (array), "
        "unknown_claims (array), source_title, source_url. Do not invent facts. "
        "A page that does not mention a claim means unknown, not contradicted. "
        "Poster text:\\n" + ocr_text[:1800]
    )
    response = requests.post(
        "https://agent.tinyfish.ai/v1/automation/run",
        headers={**tinyfish_headers(), "Content-Type": "application/json"},
        json={"url": source_url, "goal": goal, "browser_profile": "lite"},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") not in (None, "COMPLETED"):
        raise RuntimeError(
            "TinyFish Agent did not complete successfully: "
            + str(payload.get("error") or payload.get("status"))
        )
    result = payload.get("result")
    if result is None:
        raise RuntimeError("TinyFish Agent returned no structured result.")
    return result


def event_evidence_summary(ocr_text, search_results, fetched_pages):
    """Produce a conservative evidence label, not a definitive fake/real judgment."""
    stop_words = {
        "this", "that", "with", "from", "your", "will", "have", "more", "into",
        "when", "where", "what", "which", "their", "there", "about", "register",
        "registration", "event", "join", "free", "click", "here", "for", "the",
        "and", "are", "you", "our", "all", "not", "can", "has", "was", "is",
        "in", "on", "at", "to", "of", "by", "or", "as", "be", "an", "a"
    }
    terms = {
        token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9&'-]{3,}", ocr_text)
        if token.lower() not in stop_words
    }
    # OCR often contains footer text and logos; cap the set to keep the score useful.
    terms = set(sorted(terms)[:100])
    evidence_text = " ".join(
        [
            str(item.get("title", "")) + " " + str(item.get("snippet", ""))
            for item in search_results
        ] + [
            str(item.get("title", "")) + " " + str(item.get("text", ""))
            for item in fetched_pages
        ]
    ).lower()
    matched = sorted(term for term in terms if term in evidence_text)
    ratio = len(matched) / max(1, len(terms))

    if len(matched) >= 8 and ratio >= 0.18:
        label = "Some corroborating evidence found"
        detail = "Several terms from the announcement appear in the retrieved web evidence. Review the official sources below before relying on the details."
    elif len(matched) >= 3 and ratio >= 0.07:
        label = "Partially corroborated"
        detail = "Some details overlap with the retrieved web evidence, but the event or its key details are not fully confirmed."
    else:
        label = "Unable to verify"
        detail = "The search did not find enough matching evidence. This does not prove the announcement is fake; it may be new, private, or poorly indexed."

    return label, detail, matched[:15]


def extract_event_signals(text):
    """Show simple, transparent hints from OCR without pretending to understand every field."""
    lines = [re.sub(r"\s+", " ", line).strip(" •|:-") for line in text.splitlines()]
    lines = [line for line in lines if line]
    title_guess = next((line for line in lines if 4 <= len(line) <= 100), "Not detected confidently")
    dates = re.findall(
        r"\b(?:\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
        r"\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{2,4}|"
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?)\b",
        text,
        flags=re.IGNORECASE,
    )
    emails = sorted(set(re.findall(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", text)))
    urls = re.findall(r"https?://[^\s)]+", text)
    phones = re.findall(r"(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)", text)
    return {
        "Likely title": title_guess,
        "Date text": ", ".join(dict.fromkeys(dates)) or "Not detected",
        "Email": ", ".join(emails[:3]) or "Not detected",
        "Phone": ", ".join(dict.fromkeys(phones[:3])) or "Not detected",
        "Links": urls[:5],
    }


def render_snap2event():
    st.subheader("📸 Snap 2 Event")
    st.write(
        "Upload or scan a campus-event poster, screenshot, or document. "
        "We'll extract its details and look for independent evidence on the live web."
    )
    st.markdown(
        "<div class='soft-note'><strong>Trust, but verify.</strong> "
        "We compare the announcement with web sources. A missing result is not proof of fraud.</div>",
        unsafe_allow_html=True,
    )

    upload_tab, scan_tab = st.tabs(["📁 Upload a file", "📷 Scan with camera"])
    uploaded = None
    with upload_tab:
        uploaded = st.file_uploader(
            "Choose a poster or document",
            type=["png", "jpg", "jpeg", "webp", "pdf"],
            help="Images are read with OCR. PDFs should contain selectable text.",
            key="snap2event_upload",
        )
    with scan_tab:
        camera_image = st.camera_input("Take a picture of the event poster", key="snap2event_camera")
        if camera_image is not None:
            uploaded = camera_image

    if uploaded is not None:
        if uploaded.type and uploaded.type.startswith("image/"):
            st.image(uploaded, caption="Uploaded event announcement", use_container_width=True)
        if st.button("🔎 Extract & verify event", type="primary", use_container_width=True):
            try:
                with st.spinner("Reading the announcement..."):
                    extracted = extract_uploaded_text(uploaded)
                if len(extracted.strip()) < 15:
                    st.warning("I couldn't read enough text. Try a sharper, well-lit image with the full poster visible.")
                    return

                st.session_state["snap2event_text"] = extracted
                st.session_state.pop("snap2event_sources", None)
                st.session_state.pop("snap2event_pages", None)

                with st.expander("📝 Text detected from your upload", expanded=True):
                    st.text_area("Extracted text", value=extracted, height=180, key="snap2event_extracted_display")

                with st.spinner("TinyFish is searching the live web for corroborating sources..."):
                    results = tinyfish_search_event(extracted)
                    pages = tinyfish_fetch_sources(
                        [item.get("url", "") for item in results if item.get("url")][:4]
                    )
                st.session_state["snap2event_sources"] = results
                st.session_state["snap2event_pages"] = pages
                st.session_state["snap2event_verdict"] = event_evidence_summary(extracted, results, pages)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "unknown"
                st.error(f"TinyFish request failed (HTTP {status}). Check your API key, rate limit, and API response.")
            except requests.RequestException:
                st.error("Couldn't reach TinyFish. Check your internet connection and try again.")
            except Exception as exc:
                st.error(str(exc))

    extracted = st.session_state.get("snap2event_text")
    if extracted and st.session_state.get("snap2event_verdict"):
        label, detail, matched = st.session_state["snap2event_verdict"]
        st.markdown("### Verification result")
        st.info(f"**{label}**\n\n{detail}")
        st.caption("This is an evidence-based screening result, not a guarantee that an event is genuine or fraudulent.")

        signals = extract_event_signals(extracted)
        st.markdown("#### 🗂️ Details detected")
        d1, d2 = st.columns(2)
        d1.markdown(f"**Likely title**\n\n{signals['Likely title']}")
        d2.markdown(f"**Date text**\n\n{signals['Date text']}")
        d3, d4 = st.columns(2)
        d3.markdown(f"**Email**\n\n{signals['Email']}")
        d4.markdown(f"**Phone**\n\n{signals['Phone']}")

        with st.expander("🧾 Extracted announcement", expanded=False):
            st.text_area("Recognized text", value=extracted, height=180, key="snap2event_result_text")
            urls = re.findall(r"https?://[^\s)]+", extracted)
            if urls:
                st.markdown("**Links printed on the poster**")
                for url in urls[:5]:
                    st.markdown(f"- [{url}]({url})")

        if matched:
            st.write("**Terms also found in web evidence:** " + ", ".join(matched))

        st.markdown("### 🌐 Sources checked")
        results = st.session_state.get("snap2event_sources", [])
        pages = st.session_state.get("snap2event_pages", [])
        page_by_url = {p.get("url"): p for p in pages}
        if not results:
            st.warning("TinyFish returned no search results for this announcement.")
        for i, item in enumerate(results, 1):
            title = item.get("title") or item.get("site_name") or "Web result"
            url = item.get("url", "")
            snippet = item.get("snippet", "")
            with st.container(border=True):
                st.markdown(f"**{i}. {title}**")
                if url:
                    st.markdown(f"[Open source]({url})")
                if snippet:
                    st.write(snippet)
                page = page_by_url.get(url)
                if page and page.get("text"):
                    with st.expander("Preview fetched page"):
                        st.markdown(str(page["text"])[:2500])

        st.markdown("### 🧠 Deeper claim check (optional)")
        st.caption("Uses a TinyFish browser agent to compare the poster against one selected page. Agent usage may be metered.")
        source_map = {
            f"{item.get('title') or item.get('site_name') or 'Web result'} — {item.get('url', '')}": item.get("url", "")
            for item in results if item.get("url")
        }
        source_options = list(source_map)
        if source_options:
            chosen_source = st.selectbox(
                "Choose a page to inspect",
                source_options,
                key="snap2event_agent_source",
            )
            if st.button("Compare poster with selected source", key="snap2event_agent_check"):
                selected_url = source_map[chosen_source]
                try:
                    with st.spinner("TinyFish Agent is inspecting the selected page..."):
                        agent_result = tinyfish_agent_verify(selected_url, extracted)
                    st.markdown("#### Agent comparison")
                    if isinstance(agent_result, (dict, list)):
                        st.json(agent_result)
                    else:
                        st.write(str(agent_result))
                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else "unknown"
                    st.error(f"TinyFish Agent request failed (HTTP {status}). Check your plan, key, and rate limits.")
                except requests.RequestException:
                    st.error("TinyFish Agent could not reach the selected page.")
                except Exception as exc:
                    st.error(str(exc))

        st.caption("Tip: prioritize official organizer, college, venue, or registration pages over reposts and social forwards.")

# ---------- Small bits of personality ----------
CAMPUS_NOTE = "A little less scrolling. A few more things worth showing up for."
CATEGORIES = ["Tech", "AI & Data", "Entrepreneurship", "Design", "Culture", "Sports", "Research", "Community"]

def now_local():
    return datetime.now()

def as_dt(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None

def pretty_dt(value, fmt="%a, %d %b · %I:%M %p"):
    dt = as_dt(value)
    return dt.strftime(fmt) if dt else "Time to be announced"

def connect_db():
    """Use MySQL when MYSQL_HOST is configured; otherwise use local SQLite."""
    host = os.getenv("MYSQL_HOST", "").strip()
    if host:
        if mysql is None:
            raise RuntimeError("MySQL is configured, but mysql-connector-python is missing. Install requirements.txt.")
        return mysql.connector.connect(
            host=host,
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", ""),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DATABASE", ""),
        )
    return sqlite3.connect("mycampus.db", check_same_thread=False)

def is_mysql(conn):
    return conn.__class__.__module__.startswith("mysql")

@contextmanager
def db_cursor(conn):
    cur = conn.cursor(dictionary=True) if is_mysql(conn) else conn.cursor()
    try:
        yield cur
    finally:
        cur.close()

def execute(conn, sql, params=()):
    """Run SQL with ? placeholders in app code; adapt placeholders for MySQL."""
    if is_mysql(conn):
        sql = sql.replace("?", "%s")
    with db_cursor(conn) as cur:
        cur.execute(sql, params)
        try:
            conn.commit()
        except Exception:
            pass
        return cur

def fetchall(conn, sql, params=()):
    if is_mysql(conn):
        sql = sql.replace("?", "%s")
    with db_cursor(conn) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    if is_mysql(conn):
        return rows
    names = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(names, row)) for row in rows]

def fetchone(conn, sql, params=()):
    rows = fetchall(conn, sql, params)
    return rows[0] if rows else None

def init_db(conn):
    if is_mysql(conn):
        statements = [
            """CREATE TABLE IF NOT EXISTS users (
                user_id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(190) NOT NULL UNIQUE,
                year_of_study VARCHAR(30) DEFAULT 'Any year',
                interests TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS events (
                event_id INT AUTO_INCREMENT PRIMARY KEY,
                title VARCHAR(180) NOT NULL,
                category VARCHAR(60) NOT NULL,
                description TEXT,
                venue VARCHAR(180),
                start_time DATETIME NOT NULL,
                end_time DATETIME NOT NULL,
                deadline DATETIME,
                capacity INT DEFAULT 100,
                tags TEXT,
                organizer VARCHAR(120) DEFAULT 'Campus Crew'
            )""",
            """CREATE TABLE IF NOT EXISTS registrations (
                registration_id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL,
                event_id INT NOT NULL,
                status VARCHAR(40) DEFAULT 'Registered',
                attended INT DEFAULT 0,
                UNIQUE KEY unique_registration (user_id, event_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
            )""",
        ]
        for statement in statements:
            execute(conn, statement)
    else:
        statements = [
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                year_of_study TEXT DEFAULT 'Any year',
                interests TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT,
                venue TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                deadline TEXT,
                capacity INTEGER DEFAULT 100,
                tags TEXT,
                organizer TEXT DEFAULT 'Campus Crew'
            )""",
            """CREATE TABLE IF NOT EXISTS registrations (
                registration_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                status TEXT DEFAULT 'Registered',
                attended INTEGER DEFAULT 0,
                UNIQUE(user_id, event_id),
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE CASCADE
            )""",
        ]
        for statement in statements:
            execute(conn, statement)

    # Add a friendly demo profile and sample events on first run.
    demo_email = "student@mycampus.demo"
    user = fetchone(conn, "SELECT user_id FROM users WHERE email = ?", (demo_email,))
    if not user:
        execute(
            conn,
            "INSERT INTO users (name, email, year_of_study, interests) VALUES (?, ?, ?, ?)",
            ("Aarav Mehta", demo_email, "2nd year", "AI & Data,Tech,Entrepreneurship,Design"),
        )
    count = fetchone(conn, "SELECT COUNT(*) AS n FROM events")
    if not count or int(count["n"]) == 0:
        base_time = now_local().replace(second=0, microsecond=0)
        sample_events = [
            ("Build Night: Tiny AI Projects", "AI & Data",
             "Bring a laptop, one curious question, and a teammate if you have one. We'll make small AI ideas feel less intimidating.",
             "Labs 1–2", base_time + timedelta(days=1, hours=2), base_time + timedelta(days=1, hours=4),
             base_time + timedelta(hours=18), 80, "AI & Data,Tech,Python", "Tech Society"),
            ("Pitch It, Don't Perfect It", "Entrepreneurship",
             "A low-pressure pitch practice evening. Share an idea, get kind feedback, and leave with one next step.",
             "Seminar Hall", base_time + timedelta(days=2, hours=3), base_time + timedelta(days=2, hours=5),
             base_time + timedelta(days=1, hours=20), 60, "Entrepreneurship,Startups,Ideas", "E-Cell"),
            ("Design for Real People", "Design",
             "A hands-on session on making digital products clearer and kinder to use. No design software experience needed.",
             "Studio Space", base_time + timedelta(days=3, hours=1), base_time + timedelta(days=3, hours=3),
             base_time + timedelta(days=2, hours=16), 45, "Design,UX,Community", "Design Club"),
            ("Campus Open Mic 🎤", "Culture",
             "Poetry, music, jokes, unfinished songs — the mic is open. Come perform or just cheer from the back row.",
             "Amphitheatre", base_time + timedelta(days=4, hours=4), base_time + timedelta(days=4, hours=6),
             base_time + timedelta(days=3, hours=18), 120, "Culture,Music,Community", "Cultural Committee"),
            ("Sunday Morning Run Club", "Sports",
             "An easy-paced run or walk around campus. All speeds welcome; the only rule is to be nice to your knees.",
             "Main Gate", base_time + timedelta(days=5, hours=1), base_time + timedelta(days=5, hours=2),
             base_time + timedelta(days=4, hours=19), 40, "Sports,Fitness,Community", "Run Club"),
            ("Research: From Question to Experiment", "Research",
             "A practical chat about turning a broad curiosity into a testable question, with examples from student projects.",
             "Lecture Room 3", base_time + timedelta(days=6, hours=2), base_time + timedelta(days=6, hours=4),
             base_time + timedelta(days=5, hours=17), 70, "Research,AI & Data,Tech", "Research Forum"),
            ("Python Peer Help Desk", "Tech",
             "Stuck on a loop, a SQL query, or a project bug? Bring it along. We solve problems together, not silently judge them.",
             "Library Collaboration Zone", base_time + timedelta(days=2, hours=1), base_time + timedelta(days=2, hours=2),
             base_time + timedelta(days=1, hours=18), 35, "Tech,Python,Community", "Peer Learning Circle"),
            ("Make Your First Portfolio Page", "Design",
             "A friendly mini-workshop to help you collect projects, write a short intro, and make your work easier to share.",
             "Computer Lab 4", base_time + timedelta(days=7, hours=2), base_time + timedelta(days=7, hours=4),
             base_time + timedelta(days=6, hours=17), 50, "Design,Tech,Portfolio", "Creative Computing Club"),
        ]
        for ev in sample_events:
            execute(
                conn,
                """INSERT INTO events
                (title, category, description, venue, start_time, end_time, deadline, capacity, tags, organizer)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ev[0], ev[1], ev[2], ev[3], ev[4].isoformat(timespec="minutes"),
                 ev[5].isoformat(timespec="minutes"), ev[6].isoformat(timespec="minutes"),
                 ev[7], ev[8], ev[9]),
            )

def get_current_user(conn, email):
    user = fetchone(conn, "SELECT * FROM users WHERE email = ?", (email,))
    return user

def create_or_update_user(conn, name, email, year, interests):
    existing = fetchone(conn, "SELECT user_id FROM users WHERE email = ?", (email,))
    if existing:
        execute(conn, "UPDATE users SET name = ?, year_of_study = ?, interests = ? WHERE email = ?",
                (name, year, ", ".join(interests), email))
    else:
        execute(conn, "INSERT INTO users (name, email, year_of_study, interests) VALUES (?, ?, ?, ?)",
                (name, email, year, ", ".join(interests)))
    return fetchone(conn, "SELECT * FROM users WHERE email = ?", (email,))

def all_events(conn):
    return fetchall(conn, "SELECT * FROM events ORDER BY start_time ASC")

def registration_count(conn, event_id):
    row = fetchone(conn, "SELECT COUNT(*) AS n FROM registrations WHERE event_id = ?", (event_id,))
    return int(row["n"]) if row else 0

def is_registered(conn, user_id, event_id):
    return bool(fetchone(conn, "SELECT registration_id FROM registrations WHERE user_id = ? AND event_id = ?",
                         (user_id, event_id)))

def match_score(user_interests, event):
    interests = {x.strip().lower() for x in user_interests if x.strip()}
    tags = {x.strip().lower() for x in (event.get("tags") or "").split(",")}
    category = (event.get("category") or "").lower()
    if not interests:
        return 35, "A chance to try something new"
    overlap = interests.intersection(tags | {category})
    score = round(100 * len(overlap) / max(1, len(interests)))
    if score:
        reason = "Matches " + ", ".join(sorted(overlap))
    else:
        score, reason = 25, "A little outside your usual lane — could be worth a look"
    return min(score, 100), reason

def clashes_with_registered(conn, user_id, event):
    registered = fetchall(
        conn,
        """SELECT e.title, e.start_time, e.end_time FROM registrations r
           JOIN events e ON e.event_id = r.event_id
           WHERE r.user_id = ? AND r.status = 'Registered'""",
        (user_id,),
    )
    new_start, new_end = as_dt(event["start_time"]), as_dt(event["end_time"])
    clashes = []
    for item in registered:
        old_start, old_end = as_dt(item["start_time"]), as_dt(item["end_time"])
        if new_start and new_end and old_start and old_end and new_start < old_end and old_start < new_end:
            clashes.append(item["title"])
    return clashes

def register_for_event(conn, user_id, event):
    if is_registered(conn, user_id, event["event_id"]):
        return False, "You're already on the list for this one."
    if registration_count(conn, event["event_id"]) >= int(event.get("capacity") or 0):
        return False, "This event is full for now."
    deadline = as_dt(event.get("deadline"))
    if deadline and deadline < now_local():
        return False, "Registration has closed for this event."
    clashes = clashes_with_registered(conn, user_id, event)
    execute(conn, "INSERT INTO registrations (user_id, event_id, status) VALUES (?, ?, 'Registered')",
            (user_id, event["event_id"]))
    if clashes:
        return True, "You're registered! Heads-up: this overlaps with " + ", ".join(clashes) + "."
    return True, "You're on the list. See you there! 🎉"

# ---------- Connection / initialization ----------
try:
    conn = connect_db()
    init_db(conn)
except Exception as exc:
    st.error("Couldn't connect to the database. Check the setup notes in README.md.")
    st.exception(exc)
    st.stop()

# ---------- Styling ----------
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@500;600;700;800&display=swap');
:root { --purple:#6C4CF1; --blue:#2F80ED; --coral:#FF6B6B; --yellow:#FFD166; --ink:#24213A; --muted:#706C86; --paper:#F8F7FC; }
html, body, [class*="css"] { font-family:'DM Sans',sans-serif; }
.stApp { background:radial-gradient(circle at 8% 0%,rgba(108,76,241,.09),transparent 25rem),radial-gradient(circle at 96% 12%,rgba(47,128,237,.08),transparent 24rem),var(--paper); color:var(--ink); }
h1,h2,h3,h4 { font-family:'Manrope',sans-serif; letter-spacing:-.035em; color:var(--ink); }
.block-container { padding-top:1.4rem; padding-bottom:3rem; max-width:1280px; }
/* Calm lavender sidebar instead of the harsh red/dark accent */
[data-testid="stSidebar"] { background:linear-gradient(180deg,#F4F0FF 0%,#F8F7FC 60%,#EEF4FF 100%); border-right:1px solid #E3DCF8; }
[data-testid="stSidebar"] * { color:#292447; }
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { color:#625B82; }
[data-testid="stSidebar"] hr { border-color:#E2DCF2; }
[data-testid="stSidebar"] [data-testid="stRadio"] label { color:#292447 !important; }
[data-testid="stSidebar"] [data-testid="stRadio"] [aria-checked="true"] { background:#E7E0FF; border-radius:10px; }
[data-testid="stSidebar"] [data-testid="stRadio"] [data-checked="true"] { color:#5137C8 !important; }

/* Keep sidebar form fields readable while typing */
[data-testid="stSidebar"] input,
[data-testid="stSidebar"] textarea {
  color:#24213A !important;
  -webkit-text-fill-color:#24213A !important;
  caret-color:#6C4CF1 !important;
  background:#FFFFFF !important;
  border:1px solid #DCD6F1 !important;
}
[data-testid="stSidebar"] input::placeholder,
[data-testid="stSidebar"] textarea::placeholder {
  color:#77738A !important;
  -webkit-text-fill-color:#77738A !important;
  opacity:1 !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] > div {
  color:#24213A !important;
  background:#FFFFFF !important;
  border-color:#DCD6F1 !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] input {
  color:#24213A !important;
  -webkit-text-fill-color:#24213A !important;
  background:#FFFFFF !important;
}
[data-testid="stSidebar"] [data-baseweb="tag"] {
  background:#EEE9FF !important;
  color:#4932B8 !important;
}
[data-testid="stSidebar"] [data-baseweb="tag"] * {
  color:#4932B8 !important;
}
[data-testid="stSidebar"] [data-testid="stFormSubmitButton"] button {
  color:#FFFFFF !important;
  background:linear-gradient(100deg,#6C4CF1,#4D65E8) !important;
}
.hero { position:relative; overflow:hidden; padding:1.8rem 2rem; border-radius:26px; background:linear-gradient(115deg,#21164B 0%,#4932B8 54%,#6C4CF1 100%); color:white; margin-bottom:1.35rem; box-shadow:0 18px 42px rgba(67,48,156,.20); }
.hero:after { content:"✦"; position:absolute; right:3%; top:-42%; font-size:13rem; color:rgba(255,255,255,.10); transform:rotate(12deg); }
.hero h1 { color:white; font-size:clamp(2rem,4vw,2.8rem); margin:.25rem 0 .45rem; }
.hero p { color:#E9E5FF; margin:0; font-size:1.04rem; max-width:720px; }
.eyebrow { color:#FFD166; font-size:.76rem; font-weight:800; letter-spacing:.14em; text-transform:uppercase; }
.tag { display:inline-block; border-radius:999px; padding:.28rem .68rem; background:#EEE9FF; color:#5B3BD4; font-size:.76rem; font-weight:800; }
.soft-note { border-radius:16px; padding:1rem 1.1rem; background:linear-gradient(110deg,#EEE9FF,#E8F2FF); border:1px solid #DDD5FF; }
.small-muted,.meta { font-size:.86rem; color:var(--muted); }
div[data-testid="stMetric"] { background:white; border:1px solid #E8E3FA; padding:1rem 1.1rem; border-radius:18px; box-shadow:0 7px 22px rgba(42,32,89,.05); }
div[data-testid="stMetricLabel"] { color:#706C86; font-weight:700; }
div[data-testid="stMetricValue"] { color:#4932B8; font-family:'Manrope',sans-serif; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-color:#E6E0F7 !important; border-radius:19px !important; background:rgba(255,255,255,.90); box-shadow:0 7px 22px rgba(42,32,89,.045); }
.stButton > button,[data-testid="stFormSubmitButton"] button { border:0; border-radius:12px; font-weight:800; background:linear-gradient(100deg,#6C4CF1,#4D65E8); color:white; min-height:2.65rem; box-shadow:0 6px 14px rgba(108,76,241,.18); transition:transform .16s ease,box-shadow .16s ease,filter .16s ease; }
.stButton > button:hover,[data-testid="stFormSubmitButton"] button:hover { color:white; filter:brightness(1.06); transform:translateY(-1px); box-shadow:0 9px 20px rgba(108,76,241,.26); }
[data-testid="stTextInput"] input,[data-testid="stTextArea"] textarea,[data-testid="stNumberInput"] input,[data-testid="stDateInput"] input,[data-testid="stTimeInput"] input { border-radius:11px; border-color:#DCD6F1; background:white; }
[data-testid="stTextInput"] input:focus,[data-testid="stTextArea"] textarea:focus { border-color:#6C4CF1; box-shadow:0 0 0 2px rgba(108,76,241,.12); }
[data-baseweb="select"] > div { border-radius:11px; border-color:#DCD6F1; }
[data-testid="stTabs"] button[role="tab"] { border-radius:10px 10px 0 0; font-weight:700; }
[data-testid="stProgressBar"] > div > div { background:linear-gradient(90deg,#6C4CF1,#2F80ED,#FF6B6B); }
div[data-testid="stAlert"] { border-radius:14px; }
hr { border-color:#E8E3F5; }
a { color:#5B3BD4 !important; }
@media (max-width:768px) { .block-container{padding-top:1rem;} .hero{padding:1.35rem;border-radius:20px;} .hero h1{font-size:1.9rem;} }
</style>
""", unsafe_allow_html=True)

# ---------- Sidebar profile ----------
with st.sidebar:
    st.markdown("## 🎒 MyCampus")
    st.caption("Your event journey, in one place.")
    st.markdown("---")
    st.markdown("### Your little corner")
    default_user = fetchone(conn, "SELECT * FROM users WHERE email = ?", ("student@mycampus.demo",))
    default_name = default_user["name"] if default_user else "Aarav Mehta"
    default_email = default_user["email"] if default_user else "student@mycampus.demo"
    with st.form("profile_form"):
        student_name = st.text_input("Name", value=default_name)
        student_email = st.text_input("Email", value=default_email)
        year = st.selectbox("Year of study", ["1st year", "2nd year", "3rd year", "4th year", "Postgraduate", "Any year"],
                            index=1 if (default_user and default_user.get("year_of_study") == "2nd year") else 0)
        current_interests = (default_user.get("interests") or "").split(",") if default_user else ["AI & Data", "Tech"]
        selected_interests = st.multiselect("I'm into…", CATEGORIES, default=[x.strip() for x in current_interests if x.strip() in CATEGORIES])
        save_profile = st.form_submit_button("Save my details", use_container_width=True)
    if save_profile:
        if not student_name.strip() or "@" not in student_email:
            st.error("Add your name and a valid-looking email first.")
        else:
            create_or_update_user(conn, student_name.strip(), student_email.strip().lower(), year, selected_interests)
            st.session_state["active_email"] = student_email.strip().lower()
            set_flash("Profile saved. Your feed is now a little more you.")
            st.rerun()
    active_email = st.session_state.get("active_email", default_email)
    user = get_current_user(conn, active_email)
    if not user:
        user = create_or_update_user(conn, default_name, default_email, "2nd year", ["AI & Data", "Tech"])
        active_email = default_email
    st.markdown("---")
    st.caption("Prototype mode · sample campus events")

# ---------- Hero ----------
st.markdown(f"""
<div class="hero">
  <div class="eyebrow">YOUR CAMPUS, YOUR CALL</div>
  <h1>Make room for good things.</h1>
  <p>{CAMPUS_NOTE}</p>
</div>
""", unsafe_allow_html=True)

flash = st.session_state.pop("mycampus_flash", None)
if flash:
    flash_kind = flash.get("kind", "success")
    getattr(st, flash_kind if flash_kind in {"success", "info", "warning", "error"} else "success")(flash["message"])

events = all_events(conn)
registered_rows = fetchall(
    conn,
    """SELECT e.*, r.status, r.attended FROM registrations r
       JOIN events e ON e.event_id = r.event_id
       WHERE r.user_id = ? ORDER BY e.start_time ASC""",
    (user["user_id"],),
)
registered_ids = {int(r["event_id"]) for r in registered_rows}
upcoming_registered = [e for e in registered_rows if as_dt(e["end_time"]) and as_dt(e["end_time"]) >= now_local()]
closing_soon = []
for event in events:
    deadline = as_dt(event.get("deadline"))
    if deadline and timedelta(0) <= deadline - now_local() <= timedelta(days=2) and int(event["event_id"]) not in registered_ids:
        closing_soon.append(event)

# ---------- Navigation ----------
page = st.sidebar.radio("Go to", ["✨ For you", "🗓️ My event journey", "🧭 Explore events", "📸 Snap 2 Event", "❓ FAQs & Help", "🛠️ Organizer corner"], label_visibility="visible")

if page == "✨ For you":
    st.subheader(f"Hey {user['name'].split()[0]} — here's what caught our eye 👀")
    st.caption("A few ideas based on what you're into. No mysterious black box here.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Upcoming events", len([e for e in events if as_dt(e["start_time"]) and as_dt(e["start_time"]) >= now_local()]))
    c2.metric("You're signed up for", len(upcoming_registered))
    c3.metric("Deadlines coming up", len(closing_soon))
    if closing_soon:
        with st.container(border=True):
            st.markdown("#### ⏳ A gentle nudge")
            for event in closing_soon[:3]:
                st.write(f"**{event['title']}** — registration closes {pretty_dt(event['deadline'])}")
    ranked = []
    interests = [x.strip() for x in (user.get("interests") or "").split(",") if x.strip()]
    for event in events:
        if int(event["event_id"]) in registered_ids:
            continue
        start = as_dt(event["start_time"])
        if not start or start < now_local():
            continue
        score, reason = match_score(interests, event)
        ranked.append((score, event, reason))
    ranked.sort(key=lambda x: (x[0], as_dt(x[1]["start_time"]) or now_local()), reverse=True)
    st.markdown("### Picked for you")
    if not ranked:
        st.info("You've seen everything in the demo feed. Try Explore events or change your interests.")
    else:
        for start_idx in range(0, min(len(ranked), 6), 2):
            cols = st.columns(2, gap="large")
            for col, item in zip(cols, ranked[start_idx:start_idx+2]):
                score, event, reason = item
                with col:
                    with st.container(border=True):
                        st.markdown(f"<span class='tag'>{event['category']}</span> &nbsp; <span class='small-muted'>{score}% match</span>", unsafe_allow_html=True)
                        st.markdown(f"### {event['title']}")
                        st.write(event["description"])
                        st.caption(f"📍 {event['venue']}  ·  🗓️ {pretty_dt(event['start_time'])}")
                        st.caption("💡 " + reason)
                        clashes = clashes_with_registered(conn, user["user_id"], event)
                        if clashes:
                            st.warning("Time clash with: " + ", ".join(clashes))
                        if st.button("Count me in →", key=f"rec_{event['event_id']}", use_container_width=True):
                            ok, message = register_for_event(conn, user["user_id"], event)
                            set_flash(message, "success" if ok else "warning")
                            st.rerun()

elif page == "🗓️ My event journey":
    st.subheader("🗓️ My event journey")
    st.write("Your plans, your sign-ups, and the small things that make showing up easier.")
    m1, m2, m3 = st.columns(3)
    m1.metric("On your list", len(upcoming_registered))
    m2.metric("Completed", len([e for e in registered_rows if as_dt(e["end_time"]) and as_dt(e["end_time"]) < now_local()]))
    m3.metric("Interests saved", len([x for x in (user.get("interests") or "").split(",") if x.strip()]))
    if not registered_rows:
        st.markdown("""
        <div class="soft-note">
        <strong>Your timeline is looking a little quiet.</strong><br>
        Find something that sounds fun in <em>Explore events</em>, then hit “Count me in”. We'll keep it here for you.
        </div>
        """, unsafe_allow_html=True)
    for event in registered_rows:
        start = as_dt(event["start_time"])
        end = as_dt(event["end_time"])
        is_past = bool(end and end < now_local())
        with st.container(border=True):
            left, right = st.columns([4, 1])
            with left:
                st.markdown(f"#### {'☑️' if is_past else '📌'} {event['title']}")
                st.caption(f"{pretty_dt(event['start_time'])} · {event['venue']} · Hosted by {event['organizer']}")
                st.write(event["description"])
                if is_past:
                    st.caption("That one's in the books. Hope it was a good one.")
                else:
                    deadline = as_dt(event.get("deadline"))
                    if deadline and deadline >= now_local():
                        st.caption(f"Registration deadline: {pretty_dt(event['deadline'])}")
            with right:
                st.markdown("**" + ("Completed" if is_past else event.get("status", "Registered")) + "**")
                if not is_past and st.button("Cancel sign-up", key=f"cancel_{event['event_id']}"):
                    execute(conn, "DELETE FROM registrations WHERE user_id = ? AND event_id = ?",
                            (user["user_id"], event["event_id"]))
                    set_flash("You're off the list. You can sign up again any time.", "info")
                    st.rerun()
    if upcoming_registered:
        st.markdown("### Schedule check")
        for i, first in enumerate(upcoming_registered):
            for second in upcoming_registered[i+1:]:
                a, b = as_dt(first["start_time"]), as_dt(first["end_time"])
                c, d = as_dt(second["start_time"]), as_dt(second["end_time"])
                if a and b and c and d and a < d and c < b:
                    st.warning(f"⚠️ **{first['title']}** overlaps with **{second['title']}**. Future-you may appreciate choosing one.")

elif page == "🧭 Explore events":
    st.subheader("🧭 Find your next thing")
    st.write("Big plans, tiny workshops, or just something different after class.")
    col1, col2, col3 = st.columns([2, 1, 1])
    search = col1.text_input("Search events", placeholder="Try Python, music, pitch…")
    category = col2.selectbox("Category", ["All categories"] + CATEGORIES)
    availability = col3.selectbox("Show", ["Upcoming", "All events", "I'm registered for"])
    filtered = []
    for event in events:
        start = as_dt(event["start_time"])
        if availability == "Upcoming" and (not start or start < now_local()):
            continue
        if availability == "I'm registered for" and int(event["event_id"]) not in registered_ids:
            continue
        if category != "All categories" and event["category"] != category:
            continue
        haystack = " ".join([event.get("title") or "", event.get("description") or "", event.get("tags") or "", event.get("venue") or ""]).lower()
        if search and search.lower() not in haystack:
            continue
        filtered.append(event)
    st.caption(f"{len(filtered)} event{'s' if len(filtered) != 1 else ''} in view")
    if not filtered:
        st.info("No events match these filters yet. Try a shorter keyword, choose **All categories**, or switch **Show** to **All events**.")
        st.caption("Tip: search checks event titles, descriptions, tags, and venues.")
    for start_idx in range(0, len(filtered), 2):
        cols = st.columns(2, gap="large")
        for col, event in zip(cols, filtered[start_idx:start_idx+2]):
            with col:
                with st.container(border=True):
                    st.markdown(f"<span class='tag'>{event['category']}</span>", unsafe_allow_html=True)
                    st.markdown(f"### {event['title']}")
                    st.write(event["description"])
                    st.caption(f"📍 {event['venue']}")
                    st.caption(f"🗓️ {pretty_dt(event['start_time'])} – {pretty_dt(event['end_time'], '%I:%M %p')}")
                    deadline = as_dt(event.get("deadline"))
                    if deadline:
                        st.caption(f"⏰ Sign-up closes {pretty_dt(event['deadline'])}")
                    count = registration_count(conn, event["event_id"])
                    capacity = int(event.get("capacity") or 0)
                    st.progress(min(count / capacity, 1.0) if capacity else 0.0, text=f"{count} / {capacity} spots filled")
                    if int(event["event_id"]) in registered_ids:
                        st.success("You're registered")
                        if st.button("Remove from my journey", key=f"unreg_{event['event_id']}"):
                            execute(conn, "DELETE FROM registrations WHERE user_id = ? AND event_id = ?",
                                    (user["user_id"], event["event_id"]))
                            set_flash("Registration removed. You can sign up again any time.", "info")
                            st.rerun()
                    else:
                        clashes = clashes_with_registered(conn, user["user_id"], event)
                        if clashes:
                            st.warning("Clashes with: " + ", ".join(clashes))
                        if st.button("Count me in →", key=f"explore_{event['event_id']}", use_container_width=True):
                            ok, message = register_for_event(conn, user["user_id"], event)
                            set_flash(message, "success" if ok else "warning")
                            st.rerun()

elif page == "📸 Snap 2 Event":
    render_snap2event()

elif page == "❓ FAQs & Help":
    st.subheader("❓ Frequently asked questions")
    st.write("Quick answers to help you discover campus events, manage sign-ups, and verify event announcements.")
    faq_search = st.text_input("Search FAQs", placeholder="Try registration, poster, event clash, organizer…")

    faq_items = [
        ("Discovering events", "How do I find events that interest me?", "Open **Explore events** to browse upcoming events. Search by keyword, filter by category, or choose to see events you're registered for. The **For you** page also recommends upcoming events based on your saved interests."),
        ("Discovering events", "How are events recommended to me?", "Recommendations compare your saved interests with each event's category, description, and tags. They are suggestions, so you can still explore every event."),
        ("Registration", "How do I register for an event?", "Find an upcoming event on **For you** or **Explore events**, then select **Count me in**. If registration succeeds, the event appears in **My event journey**."),
        ("Registration", "Where can I see my registered events?", "Open **My event journey** to review your sign-ups, event details, and upcoming plans."),
        ("Registration", "Can I cancel a registration?", "Yes. In **My event journey**, use **Cancel sign-up** for an event. You can also remove a registration from an event card in **Explore events**."),
        ("Registration", "What does a time-clash warning mean?", "It means the event overlaps with another event you're registered for. Review the dates and times before deciding which one to attend."),
        ("Event verification", "What is Snap 2 Event?", "Snap 2 Event lets you upload or capture an event announcement, extract its text, and look for online sources that may support the details on the poster."),
        ("Event verification", "Does Snap 2 Event prove that a poster is genuine?", "No. Search results can provide supporting evidence, but they cannot guarantee authenticity. Prefer official college, organizer, venue, or registration pages, and confirm important details with the organizer."),
        ("Event verification", "Why can't my uploaded poster be read?", "Image OCR needs Pillow, pytesseract, and the Tesseract OCR application installed. Make sure you're running the app in the same Python environment where the packages were installed. For PDFs, selectable text is supported; scanned/image-only PDFs may need to be uploaded as images instead."),
        ("Event verification", "Why does verification show few or no sources?", "The event may not be indexed online, its details may be too general, or the search service may be unavailable. Try searching the organizer or event name separately and check official campus channels."),
        ("Organizers", "How can an organizer add an event?", "Open **Organizer corner**, select **Add an event**, complete the event details, and choose **Publish event**. Check that the end time is after the start time and the registration deadline is not later than the event start."),
        ("Privacy & prototype", "Is MyCampus ready for real student data?", "This is a prototype. The app notes that it does not have role-based login yet, so avoid entering sensitive or private student information."),
    ]
    if faq_search.strip():
        term = faq_search.strip().lower()
        visible_faqs = [item for item in faq_items if term in " ".join(item).lower()]
    else:
        visible_faqs = faq_items

    st.caption(f"Showing {len(visible_faqs)} of {len(faq_items)} questions")
    if not visible_faqs:
        st.info("No FAQs match that search. Try a broader keyword, such as 'event' or 'registration'.")
    else:
        categories = list(dict.fromkeys(item[0] for item in visible_faqs))
        for category_name in categories:
            st.markdown(f"### {category_name}")
            for _, question, answer in [item for item in visible_faqs if item[0] == category_name]:
                with st.expander(question):
                    st.markdown(answer)
    st.markdown("---")
    st.markdown("**Still need help?** For event-specific changes or confirmation, contact the organizer or check your college's official channels.")

elif page == "🛠️ Organizer corner":
    st.subheader("🛠️ Organizer corner")
    st.write("For the people behind the posters, spreadsheets, and last-minute room changes.")
    tab1, tab2 = st.tabs(["➕ Add an event", "📊 Registrations"])
    with tab1:
        st.caption("A few clear details help students decide quickly. Required fields are event name, description, venue, dates, and a valid registration deadline.")
        with st.form("event_form", clear_on_submit=True):
            title = st.text_input("Event name", placeholder="e.g. Intro to Robotics")
            category = st.selectbox("Category", CATEGORIES)
            description = st.text_area("A short, human description", placeholder="What will people do, learn, or experience?")
            venue = st.text_input("Venue", placeholder="e.g. Seminar Hall")
            col_a, col_b = st.columns(2)
            start_date = col_a.date_input("Start date", value=date.today() + timedelta(days=8))
            start_time = col_b.time_input("Start time", value=time(16, 0))
            col_c, col_d = st.columns(2)
            end_date = col_c.date_input("End date", value=date.today() + timedelta(days=8))
            end_time = col_d.time_input("End time", value=time(18, 0))
            deadline_date = st.date_input("Registration deadline", value=date.today() + timedelta(days=7))
            deadline_time = st.time_input("Deadline time", value=time(20, 0))
            capacity = st.number_input("Capacity", min_value=1, max_value=10000, value=50)
            tags = st.text_input("Tags (comma-separated)", placeholder="AI & Data, Python, beginner-friendly")
            organizer = st.text_input("Club / organizer name", value="Campus Crew")
            submitted = st.form_submit_button("Publish event", use_container_width=True)
        if submitted:
            start_dt = datetime.combine(start_date, start_time)
            end_dt = datetime.combine(end_date, end_time)
            deadline_dt = datetime.combine(deadline_date, deadline_time)
            if not title.strip() or not venue.strip() or not description.strip():
                st.error("Add an event name, venue, and a short description.")
            elif end_dt <= start_dt:
                st.error("The end time should be after the start time.")
            elif deadline_dt > start_dt:
                st.error("The registration deadline should be before the event starts.")
            else:
                execute(
                    conn,
                    """INSERT INTO events
                    (title, category, description, venue, start_time, end_time, deadline, capacity, tags, organizer)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (title.strip(), category, description.strip(), venue.strip(),
                     start_dt.isoformat(timespec="minutes"), end_dt.isoformat(timespec="minutes"),
                     deadline_dt.isoformat(timespec="minutes"), int(capacity), tags, organizer.strip() or "Campus Crew"),
                )
                set_flash("Event published. It's officially out in the world 🌱")
                st.rerun()
    with tab2:
        rows = fetchall(
            conn,
            """SELECT e.event_id, e.title, e.category, e.start_time, e.capacity,
                      COUNT(r.registration_id) AS registrations,
                      SUM(CASE WHEN r.attended = 1 THEN 1 ELSE 0 END) AS attended
               FROM events e LEFT JOIN registrations r ON e.event_id = r.event_id
               GROUP BY e.event_id, e.title, e.category, e.start_time, e.capacity
               ORDER BY e.start_time""",
        )
        if rows:
            st.dataframe(
                [{"Event": r["title"], "Category": r["category"], "Starts": pretty_dt(r["start_time"]),
                  "Registrations": int(r["registrations"] or 0), "Capacity": int(r["capacity"] or 0),
                  "Attendance marked": int(r["attended"] or 0)} for r in rows],
                use_container_width=True, hide_index=True
            )
            st.caption("Prototype note: this demo has no login roles yet. Don't use it for real private student data.")
        else:
            st.info("No events yet. Add the first one in the other tab.")

st.markdown("---")
left, right = st.columns([3, 1])
with left:
    st.caption("Made for campus life — because the best opportunities shouldn't get lost in the group chat.")
with right:
    st.caption("MyCampus · Prototype")
