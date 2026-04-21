#!/usr/bin/env python3
"""
granola_to_obsidian.py
Syncs today's Granola meetings → Obsidian.

- Class meetings   → appended as sessions to Classes/[ClassName].md
- All other meetings → individual files in Meetings/[GranolaFolder]/YYYY-MM-DD - Title.md

Runs daily at 9pm via launchd (com.jacob.granola-obsidian).

Techniques adapted from: https://github.com/dannymcc/Granola-to-Obsidian
- Auth: reads workos_tokens from ~/Library/Application Support/Granola/supabase.json
- Content: fetches HTML from /v1/get-document-panels, converts to Markdown
- Folder mirroring: maps Granola folders → Obsidian subdirectories
- Deduplication: granola_id in frontmatter / HTML comment per entry
"""

import asyncio
import json
import os
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

# ── Paths ──────────────────────────────────────────────────────────────────
OBSIDIAN_CLASSES  = Path.home() / "Documents/Obsidian Vault/Classes"
OBSIDIAN_MEETINGS = Path.home() / "Documents/Obsidian Vault/Meetings"
GRANOLA_AUTH_PATH = Path.home() / "Library/Application Support/Granola/supabase.json"
ENV_PATH          = Path(__file__).parent / ".env"

GRANOLA_API = "https://api.granola.ai"
CANVAS_BASE = "https://canvas.stanford.edu/api/v1"

# Class title matching — more specific patterns first
CLASS_PATTERNS = [
    ("stramgt 351", "STRAMGT351"),
    ("stramgt351",  "STRAMGT351"),
    ("stramgt 329", "STRAMGT329"),
    ("stramgt329",  "STRAMGT329"),
    ("hrmgt",       "HRMGT350"),
    ("finance 347", "Finance347"),
    ("finance347",  "Finance347"),
    ("polecon",     "PolEcon230"),
    ("acct",        "ACCT313"),
]


# ── Auth ───────────────────────────────────────────────────────────────────

def get_granola_token() -> str:
    data = json.loads(GRANOLA_AUTH_PATH.read_text())
    for key in ("workos_tokens", "cognito_tokens"):
        raw = data.get(key)
        if not raw:
            continue
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        token = parsed.get("access_token")
        if token:
            return token
    raise ValueError("Granola auth token not found in supabase.json")


def get_canvas_token() -> str:
    if os.getenv("CANVAS_API_TOKEN"):
        return os.getenv("CANVAS_API_TOKEN")
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("CANVAS_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise ValueError("CANVAS_API_TOKEN not found in .env")


def get_anthropic_key() -> Optional[str]:
    """Returns Anthropic API key if configured, else None (enrichment is optional)."""
    if os.getenv("ANTHROPIC_API_KEY"):
        return os.getenv("ANTHROPIC_API_KEY")
    try:
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


# ── Note enrichment ────────────────────────────────────────────────────────

_ENRICH_PROMPT = """\
You are enriching class notes from {course} at Stanford GSB. The student recorded \
these notes in class. Your job is to add depth without changing the original content.

RAW NOTES:
{notes}

Add a concise analysis in this exact markdown format (no intro, no commentary, \
just the three sections):

### Analysis

**Key Frameworks:**
- [1-2 established frameworks/mental models that organize the main concepts — \
name the framework, then one sentence on how it applies here]

**Big Takeaways:**
- [3-5 bullet points — the most important, actionable, or exam-worthy insights \
from this session, phrased as sharp principles not summaries]

**Outside Connections:**
- [1-2 connections to books, theories, real companies, or concepts from other \
courses that deepen understanding — be specific, not generic]
"""

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"


async def enrich_notes(ai_md: str, course_name: str, api_key: str) -> str:
    """
    Calls Claude to add frameworks, takeaways, and outside connections.
    Returns the Analysis block markdown, or "" on failure.
    """
    if not ai_md or not ai_md.strip():
        return ""
    prompt = _ENRICH_PROMPT.format(course=course_name, notes=ai_md)
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                ANTHROPIC_API,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 600,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if r.is_success:
                return r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  [enrichment failed: {e}]")
    return ""


# ── Class matching ─────────────────────────────────────────────────────────

def match_class(title: str) -> Optional[str]:
    t = title.lower()
    for pattern, classname in CLASS_PATTERNS:
        if pattern in t:
            return classname
    return None


# ── HTML → Markdown ────────────────────────────────────────────────────────

class _HtmlToMd(HTMLParser):
    """
    Tight HTML → Markdown.
    - Headings get a blank line before them (section break) but not after
    - List items are single-spaced with no blank lines between them
    - Nested lists indent without extra blank lines
    """
    def __init__(self):
        super().__init__()
        self._out: List[str] = []
        self._list_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self._out.append("\n\n" + "#" * level + " ")
        elif tag in ("ul", "ol"):
            self._list_depth += 1
        elif tag == "li":
            indent = "  " * (self._list_depth - 1)
            self._out.append(f"\n{indent}- ")
        elif tag == "p":
            if self._list_depth == 0:
                self._out.append("\n")
        elif tag in ("strong", "b"):
            self._out.append("**")
        elif tag in ("em", "i"):
            self._out.append("*")
        elif tag == "br":
            self._out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._out.append("\n")
        elif tag in ("ul", "ol"):
            self._list_depth -= 1
        elif tag in ("strong", "b"):
            self._out.append("**")
        elif tag in ("em", "i"):
            self._out.append("*")
        elif tag == "p" and self._list_depth == 0:
            self._out.append("\n")

    def handle_data(self, data):
        # Drop whitespace-only text nodes inside lists (newlines between <li> tags etc.)
        if self._list_depth > 0 and not data.strip():
            return
        # Strip trailing newlines from inline text in lists (prevents blank line before nested list)
        if self._list_depth > 0:
            data = data.rstrip("\n")
        self._out.append(data)

    def result(self) -> str:
        text = "".join(self._out).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)  # max one blank line between sections
        text = re.sub(r" +\n", "\n", text)       # no trailing spaces
        return text


def prosemirror_to_md(node: dict, indent: int = 0) -> str:
    """ProseMirror → tight Markdown (fallback for non-HTML panel content)."""
    t        = node.get("type", "")
    children = node.get("content", [])
    text     = node.get("text", "")
    if t == "text":
        return text
    elif t == "heading":
        level = node.get("attrs", {}).get("level", 2)
        return "\n\n" + "#" * level + " " + "".join(prosemirror_to_md(c) for c in children) + "\n"
    elif t == "paragraph":
        inner = "".join(prosemirror_to_md(c) for c in children)
        return (inner + "\n") if inner.strip() else ""
    elif t in ("bulletList", "orderedList"):
        return "".join(prosemirror_to_md(c, indent) for c in children)
    elif t == "listItem":
        pad   = "  " * indent
        parts = []
        for c in children:
            if c.get("type") == "paragraph":
                parts.append("".join(prosemirror_to_md(x) for x in c.get("content", [])).strip())
            else:
                parts.append(prosemirror_to_md(c, indent + 1))
        return pad + "- " + "\n".join(p for p in parts if p) + "\n"
    else:
        return "".join(prosemirror_to_md(c, indent) for c in children)


def html_to_md(html: str) -> str:
    if not html or not html.strip():
        return ""
    converter = _HtmlToMd()
    converter.feed(html)
    return converter.result()


# ── Granola API ────────────────────────────────────────────────────────────

async def fetch_todays_docs(token: str, today: date) -> List[dict]:
    """Get all documents created today."""
    today_str = today.isoformat()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{GRANOLA_API}/v2/get-documents",
            headers={"Authorization": f"Bearer {token}"},
            json={"limit": 100, "offset": 0,
                  "include_last_viewed_panel": True, "include_panels": True},
        )
        r.raise_for_status()
    return [d for d in r.json().get("docs", []) if d.get("created_at", "")[:10] == today_str]


async def fetch_panel_content(token: str, doc_id: str) -> Tuple[str, str]:
    """
    Returns (ai_notes_md, private_notes_md) for a document.
    ai_notes_md   — HTML content from the AI-generated panel, converted to MD
    private_notes_md — plain text of the user's private notes
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{GRANOLA_API}/v1/get-document-panels",
            headers={"Authorization": f"Bearer {token}"},
            json={"document_id": doc_id},
        )
        if not r.is_success:
            return "", ""
        panels = r.json()

    ai_md = ""
    if panels:
        panel = panels[0]
        raw = panel.get("content") or panel.get("original_content") or ""
        if isinstance(raw, dict):
            # ProseMirror node — extract plain text
            ai_md = prosemirror_to_md(raw)
        elif isinstance(raw, str):
            ai_md = html_to_md(raw)

    return ai_md, ""   # private notes pulled separately from doc.notes_plain


def get_private_notes(doc: dict) -> str:
    """Extract private notes from the document's notes_plain or notes fields."""
    plain = doc.get("notes_plain")
    if plain and plain.strip():
        return plain.strip()
    # Fall back to ProseMirror text extraction
    notes_pm = doc.get("notes")
    if isinstance(notes_pm, dict):
        texts = []
        def extract(node):
            if node.get("type") == "text" and node.get("text"):
                texts.append(node["text"])
            for child in node.get("content", []):
                extract(child)
        extract(notes_pm)
        result = " ".join(texts).strip()
        if result:
            return result
    return ""


# ── Granola folder map ────────────────────────────────────────────────────

async def fetch_folder_map(token: str) -> Dict[str, str]:
    """Returns {doc_id: folder_title} for all Granola folders."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{GRANOLA_API}/v1/get-document-lists-metadata",
            headers={"Authorization": f"Bearer {token}"},
            json={"include_document_ids": True, "include_only_joined_lists": False},
        )
        if not r.is_success:
            return {}
        folders = r.json().get("lists", [])

    doc_to_folder: Dict[str, str] = {}
    # API returns a dict keyed by folder ID, not a list
    if isinstance(folders, dict):
        folders = folders.values()
    for folder in folders:
        title = folder.get("title", "").strip()
        for doc_id in (folder.get("document_ids") or []):
            doc_to_folder[doc_id] = title
    return doc_to_folder


# ── Meeting categorization ────────────────────────────────────────────────

# Maps Granola folder names (lowercase) → Obsidian subfolder
_GRANOLA_FOLDER_MAP: Dict[str, str] = {
    "personal": "Personal",
}

# Title keyword → Obsidian subfolder (first match wins; more specific rules first)
_TITLE_CATEGORY_RULES: List[Tuple[str, str]] = [
    # Past courses (current courses are caught by CLASS_PATTERNS above)
    ("gsbgen 208",        "Courses/Ethics & Leadership (GSBGEN 208.5)"),
    ("gsbgen208",         "Courses/Ethics & Leadership (GSBGEN 208.5)"),
    ("leading with values","Courses/Ethics & Leadership (GSBGEN 208.5)"),
    ("finance 205",       "Courses/Accelerated Finance (FINANCE 205.1)"),
    ("finance205",        "Courses/Accelerated Finance (FINANCE 205.1)"),
    ("ob 206",            "Courses/Organizational Behavior (OB 206.5)"),
    ("ob206",             "Courses/Organizational Behavior (OB 206.5)"),
    ("ob 209",            "Courses/Managing Groups & Teams (OB 209.5)"),
    ("ob209",             "Courses/Managing Groups & Teams (OB 209.5)"),
    ("oit 245",           "Courses/Operations Management (OIT 245.1)"),
    ("oit245",            "Courses/Operations Management (OIT 245.1)"),
    ("oit 274",           "Courses/Technology Strategy (OIT 274.1)"),
    ("oit274",            "Courses/Technology Strategy (OIT 274.1)"),
    ("mgtecon",           "Courses/Economics (MGTECON 200.3)"),
    ("gsbgen 306",        "Courses/Real Estate Entrepreneurship (GSBGEN 306.2)"),
    ("gsbgen306",         "Courses/Real Estate Entrepreneurship (GSBGEN 306.2)"),
    # Real estate
    ("real estate",       "Real Estate"),
    ("realty",            "Real Estate"),
    ("broadreach",        "Real Estate"),
    ("moghadam",          "Real Estate"),
    # Speakers & Events
    ("fireside chat",          "Speakers & Events"),
    ("view from the top",      "Speakers & Events"),
    ("stanford investor summit","Speakers & Events"),
    ("investor summit",        "Speakers & Events"),
    # Career
    ("academic planning",     "Career"),
    ("registration advising", "Career"),
    ("summer plans",          "Career"),
    ("internship",            "Career"),
    ("recruiting",            "Career"),
    # Work
    ("hubspot",          "Work"),
    ("netsuite",         "Work"),
    ("billing",          "Work"),
    ("dossier",          "Work"),
    ("pricing strategy", "Work"),
    ("boost",            "Work"),
]


def categorize_meeting(title: str, granola_folder: str) -> str:
    """
    Returns the Obsidian subfolder for a non-class meeting.
    Priority: Granola folder map → title keyword rules → raw Granola folder → 'Other'
    """
    title_lower  = (title or "").lower()
    folder_lower = (granola_folder or "").lower().strip()

    # 1. Granola folder name → known taxonomy mapping
    if folder_lower in _GRANOLA_FOLDER_MAP:
        return _GRANOLA_FOLDER_MAP[folder_lower]

    # 2. Title keyword → taxonomy folder
    for keyword, target_folder in _TITLE_CATEGORY_RULES:
        if keyword in title_lower:
            return target_folder

    # 3. Non-empty Granola folder → use it as-is (respects user's own Granola organization)
    if granola_folder.strip():
        return sanitize(granola_folder)

    # 4. Fallback
    return "Other"


# ── Non-class meeting file writer ─────────────────────────────────────────

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def sanitize(name: Optional[str]) -> str:
    """Remove filesystem-invalid chars and collapse whitespace."""
    return _INVALID_CHARS.sub("", name or "").strip()


def _best_name(entry: dict) -> str:
    """Extract the best display name from a Granola person entry."""
    details = (entry.get("details") or {}).get("person") or {}
    pn      = details.get("name") or {}
    full    = pn.get("fullName") or f"{pn.get('givenName','')} {pn.get('familyName','')}".strip()
    return full or entry.get("name") or entry.get("email") or ""


def extract_attendees(doc: dict) -> List[str]:
    """Extract attendee display names. Handles both list and dict forms of doc.people."""
    seen:  set       = set()
    names: List[str] = []

    def add(name: str):
        key = name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            names.append(name.strip())

    people = doc.get("people") or []

    if isinstance(people, dict):
        # Format: {url, creator:{name,email,details}, attendees:[{email,details}]}
        creator = people.get("creator") or {}
        if creator:
            add(_best_name(creator))
        for att in people.get("attendees") or []:
            if isinstance(att, dict):
                add(_best_name(att))
    else:
        for person in people:
            if isinstance(person, dict):
                add(_best_name(person))

    # Supplement with calendar attendees (often have cleaner display names)
    cal = doc.get("google_calendar_event") or {}
    for att in cal.get("attendees") or []:
        dn = att.get("displayName") or att.get("email") or ""
        if dn:
            add(dn)

    return names


def attendee_tags(names: List[str], my_name: str = "Jacob Johnson") -> List[str]:
    """Convert names to person/first-last tags, excluding self."""
    tags = []
    for name in names:
        if name.lower().strip() == my_name.lower().strip():
            continue
        slug = re.sub(r"[^\w\s-]", "", name).strip().lower().replace(" ", "-")
        if slug:
            tags.append(f"person/{slug}")
    return tags


def write_meeting_file(doc: dict, ai_md: str, private_md: str,
                       folder_name: str, today: date,
                       analysis: str = "") -> Tuple[bool, str]:
    """
    Write/update a standalone meeting file: Meetings/[folder]/YYYY-MM-DD - Title.md

    - On first write: creates file with AI notes + blank My Notes section
    - On re-sync: if Granola updated_at changed, refreshes AI notes only; My Notes preserved
    Returns (changed: bool, filepath: str).
    """
    granola_id = doc.get("id", "")
    updated_at = (doc.get("updated_at") or doc.get("created_at") or "")[:19]
    title      = sanitize(doc.get("title", "Untitled"))
    created    = doc.get("created_at", today.isoformat())

    clean_folder = sanitize(folder_name) if folder_name else ""
    target_dir   = OBSIDIAN_MEETINGS / clean_folder if clean_folder else OBSIDIAN_MEETINGS
    target_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{today.strftime('%Y-%m-%d')} - {title}.md"
    filepath = target_dir / filename

    # Build attendee info
    attendees = extract_attendees(doc)
    tags      = attendee_tags(attendees)
    if clean_folder:
        # Tag by top-level category only (e.g. "Courses/Ethics & Leadership" → source/courses)
        top = clean_folder.split("/")[0].lower().replace(" ", "-").replace("&", "and")
        tags.append(f"source/{top}")
        # Topic tag for MOC connectivity
        _topic_map = {
            "courses":          "topic/course",
            "real-estate":      "topic/real-estate",
            "speakers-events":  "topic/speaker",
            "career":           "topic/career",
            "personal":         "topic/personal",
            "work":             "topic/work",
        }
        topic_key = top.replace(" ", "-").replace("&", "and")
        topic_tag = _topic_map.get(topic_key)
        if topic_tag:
            tags.append(topic_tag)
    others = [a for a in attendees if a.lower() not in ("jacob johnson", "jacbjohn")]

    def make_content(preserve_notes: Optional[str] = None) -> str:
        fm = ["---",
              f"granola_id: {granola_id}",
              f"updated_at: {updated_at}",
              f'title: "{title}"',
              f"created_at: {created[:10]}"]
        if tags:
            fm += ["tags:"] + [f"  - {t}" for t in tags]
        fm.append("---")

        body = ["\n".join(fm), f"\n# {title}"]
        if others:
            body.append(f"**With:** {', '.join(others)}")
        if ai_md:
            body += [f"\n## Notes\n{ai_md}"]
        if analysis:
            body += [f"\n{analysis}"]

        my_notes = preserve_notes if preserve_notes is not None else "*(none)*"
        body += [f"\n---\n{_MY_NOTES_MARKER}\n\n{my_notes}\n\n---"]
        return "\n".join(body) + "\n"

    if not filepath.exists():
        filepath.write_text(make_content())
        return True, str(filepath)

    existing = filepath.read_text()
    stored   = _extract_stored(existing, granola_id)

    if stored == updated_at:
        return False, str(filepath)  # up to date

    # Preserve existing My Notes content
    my_notes_pos = existing.find(f"{_MY_NOTES_MARKER}\n")
    end_pos      = existing.rfind("\n---")
    if my_notes_pos != -1 and end_pos > my_notes_pos:
        saved_notes = existing[my_notes_pos + len(_MY_NOTES_MARKER) + 1:end_pos].strip()
    else:
        saved_notes = None

    filepath.write_text(make_content(preserve_notes=saved_notes or "*(none)*"))
    return True, str(filepath)


# ── Canvas API ─────────────────────────────────────────────────────────────

async def fetch_canvas_context(token: str, class_name: str, today: date) -> dict:
    """Returns session_label and assignments_due[] from Canvas."""
    headers  = {"Authorization": f"Bearer {token}"}
    end_week = today + timedelta(days=7)
    context  = {"session_label": "", "assignments_due": []}

    async with httpx.AsyncClient(timeout=30) as client:
        # Find the matching Canvas course
        try:
            r = await client.get(f"{CANVAS_BASE}/courses", headers=headers,
                                 params={"enrollment_state": "active", "per_page": 50})
            courses = r.json() if r.is_success else []
        except Exception:
            return context

        course_id = None
        for course in courses:
            combined = (course.get("name", "") + " " + course.get("course_code", "")).lower()
            bare     = re.sub(r"[^a-z0-9]", "", class_name.lower())
            alpha    = re.sub(r"[0-9]", "", bare)
            numeric  = re.sub(r"[^0-9]", "", bare)
            if alpha in combined and (not numeric or numeric in combined):
                course_id = str(course["id"])
                break

        if not course_id:
            return context

        # Session label from today's calendar event
        try:
            r = await client.get(f"{CANVAS_BASE}/calendar_events", headers=headers,
                                 params={"type": "event",
                                         "start_date": today.isoformat(),
                                         "end_date": today.isoformat(),
                                         "context_codes[]": f"course_{course_id}",
                                         "per_page": 10})
            events = r.json() if r.is_success else []
            if events:
                context["session_label"] = events[0].get("title", "")
        except Exception:
            pass

        # Upcoming assignments due within 7 days
        try:
            r = await client.get(f"{CANVAS_BASE}/courses/{course_id}/assignments",
                                 headers=headers,
                                 params={"bucket": "upcoming", "per_page": 20,
                                         "order_by": "due_at"})
            for a in (r.json() if r.is_success else []):
                due_str = a.get("due_at")
                if not due_str:
                    continue
                due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00")).date()
                if today <= due_dt <= end_week:
                    context["assignments_due"].append({
                        "name": a.get("name", ""),
                        "due":  due_dt.strftime("%b %-d"),
                    })
        except Exception:
            pass

    return context


# ── Note assembly ──────────────────────────────────────────────────────────

_MY_NOTES_MARKER = "### My Notes"

def build_ai_block(doc: dict, ai_md: str, context: dict, today: date,
                   analysis: str = "") -> str:
    """
    The refreshable part of an entry — everything above the My Notes marker.
    Stored updated_at so we can detect Granola edits later.
    """
    granola_id    = doc.get("id", "")
    updated_at    = (doc.get("updated_at") or doc.get("created_at") or "")[:19]
    meeting_title = doc.get("title", "")
    session_label = context.get("session_label", "")
    assignments   = context.get("assignments_due", [])

    heading = f"## {today.strftime('%Y-%m-%d')}"
    if session_label and session_label.strip().lower() != meeting_title.strip().lower():
        heading += f" — {session_label}"

    lines = [
        heading,
        f"<!-- granola_id: {granola_id} updated_at: {updated_at} -->",
        f"*{meeting_title}*",
    ]

    if assignments:
        due_parts = ", ".join(f"{a['name']} ({a['due']})" for a in assignments)
        lines.append(f"**Due:** {due_parts}")

    if ai_md:
        lines += ["", ai_md]

    if analysis:
        lines += ["", analysis]

    return "\n".join(lines)


def build_entry(doc: dict, ai_md: str, context: dict, today: date,
                analysis: str = "") -> str:
    """Full entry = AI block + protected My Notes section."""
    ai_block = build_ai_block(doc, ai_md, context, today, analysis=analysis)
    return ai_block + f"\n\n---\n{_MY_NOTES_MARKER}\n\n*(none)*\n\n---\n"


# ── File writers ───────────────────────────────────────────────────────────

_GRANOLA_ID_RE = re.compile(
    r"<!-- granola_id: ([a-f0-9-]+) updated_at: ([^\s]+) -->"
)


def _extract_stored(content: str, granola_id: str) -> Optional[str]:
    """Return stored updated_at for this granola_id, or None if not found."""
    for m in _GRANOLA_ID_RE.finditer(content):
        if m.group(1) == granola_id:
            return m.group(2)
    return None


def sync_class_entry(class_name: str, doc: dict, ai_md: str,
                     context: dict, today: date, analysis: str = "") -> str:
    """
    Smart sync for class files. Returns: 'new' | 'updated' | 'skipped' | 'missing'.

    - new:     entry didn't exist → appended with blank My Notes placeholder
    - updated: Granola notes changed → AI block refreshed, My Notes preserved
    - skipped: already up to date
    - missing: class file doesn't exist (never creates new files)
    """
    filepath   = OBSIDIAN_CLASSES / f"{class_name}.md"
    granola_id = doc.get("id", "")
    updated_at = (doc.get("updated_at") or doc.get("created_at") or "")[:19]

    if not filepath.exists():
        return "missing"

    content = filepath.read_text()
    stored  = _extract_stored(content, granola_id)

    if stored is None:
        # Brand new entry
        entry = build_entry(doc, ai_md, context, today, analysis=analysis)
        with filepath.open("a") as f:
            f.write("\n" + entry)
        return "new"

    if stored == updated_at:
        return "skipped"

    # Granola notes have been updated — refresh AI block, preserve My Notes
    new_ai_block = build_ai_block(doc, ai_md, context, today, analysis=analysis)

    # Find the section: from the ## heading before granola_id to the end of the entry
    id_pos = content.find(f"<!-- granola_id: {granola_id}")
    # Walk back to find the ## heading
    section_start = content.rfind("\n## ", 0, id_pos)
    section_start = section_start + 1 if section_start != -1 else 0

    # Find the My Notes marker and preserve everything from it to end of this entry
    my_notes_pos = content.find(f"\n{_MY_NOTES_MARKER}", id_pos)
    # Find the end of this entry (next ## heading or EOF)
    next_entry = re.search(r"\n## ", content[id_pos + 1:])
    entry_end  = (id_pos + 1 + next_entry.start()) if next_entry else len(content)

    if my_notes_pos != -1 and my_notes_pos < entry_end:
        preserved = content[my_notes_pos:entry_end]
    else:
        preserved = f"\n\n---\n{_MY_NOTES_MARKER}\n\n*(none)*\n\n---\n"

    updated = content[:section_start] + new_ai_block + preserved + content[entry_end:]
    filepath.write_text(updated)
    return "updated"


# ── Main ───────────────────────────────────────────────────────────────────

async def main():
    today         = date.today()
    granola_token  = get_granola_token()
    canvas_token   = get_canvas_token()
    anthropic_key  = get_anthropic_key()

    if anthropic_key:
        print(f"[{datetime.now().strftime('%H:%M')}] Syncing Granola → Obsidian for {today}… (enrichment ON)")
    else:
        print(f"[{datetime.now().strftime('%H:%M')}] Syncing Granola → Obsidian for {today}… (enrichment OFF — add ANTHROPIC_API_KEY to .env)")

    docs, folder_map = await asyncio.gather(
        fetch_todays_docs(granola_token, today),
        fetch_folder_map(granola_token),
    )

    if not docs:
        print("No Granola meetings found today.")
        return

    class_synced   = []
    meeting_synced = []

    for doc in docs:
        granola_id = doc.get("id", "")
        title      = doc.get("title", "")
        class_name = match_class(title)

        ai_md, _   = await fetch_panel_content(granola_token, granola_id)
        private_md = get_private_notes(doc)

        # ── Optional Claude enrichment (frameworks + takeaways + connections)
        analysis = ""
        if anthropic_key and ai_md:
            course_label = class_name or title
            analysis = await enrich_notes(ai_md, course_label, anthropic_key)

        if class_name:
            # ── Class meeting → append/update session in Classes/[ClassName].md
            context = await fetch_canvas_context(canvas_token, class_name, today)
            result  = sync_class_entry(class_name, doc, ai_md, context, today,
                                       analysis=analysis)
            if result in ("new", "updated"):
                label = context.get("session_label", "")
                suffix = f" ({'updated' if result == 'updated' else label or 'new'})"
                class_synced.append(f"{class_name}{suffix}")
        else:
            # ── Non-class meeting → individual file in Meetings/[category]/
            granola_folder  = folder_map.get(granola_id, "")
            obsidian_folder = categorize_meeting(title, granola_folder)
            wrote, path     = write_meeting_file(doc, ai_md, private_md, obsidian_folder,
                                                 today, analysis=analysis)
            if wrote:
                meeting_synced.append(f"{obsidian_folder}/{title}")

    if class_synced:
        print(f"Classes  ({len(class_synced)}): {', '.join(class_synced)}")
    if meeting_synced:
        print(f"Meetings ({len(meeting_synced)}): {', '.join(meeting_synced)}")
    if not class_synced and not meeting_synced:
        print("No new meetings to sync.")


if __name__ == "__main__":
    asyncio.run(main())
