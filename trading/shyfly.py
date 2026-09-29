"""The Shy Fly children's book generation agent.

A completely separate feature from the trading system - lives in its own
page (trading/web.py's /shyfly routes), its own GCS storage namespace
(shyfly/...), and its own generation pipeline: Claude drafts the story text
and per-page illustration descriptions, Gemini generates the actual
illustrations, and nothing is ever auto-published - every book sits as a
"draft" until explicitly approved on the page.

Character consistency across the series works in two layers:
1. Text: a persistent "character bible" (shyfly/characters.json) with each
   recurring character's visual description, established once and reused
   in every future book's story/illustration prompts.
2. Image: each character's very first generated portrait is kept as a
   reference image (shyfly/characters/<name>.png) and passed back into
   Gemini as actual image input (not just re-described in text) for every
   page that character appears in - much stronger consistency than text
   alone, since Gemini's image models accept mixed image+text input.
"""
import json
import os
import threading
from datetime import datetime, timezone

import anthropic
from google import genai
from google.genai import types

from . import storage
from .agents._parsing import strip_json_fence

CHARACTERS_BLOB = "shyfly/characters.json"
BOOKS_DIR = "shyfly/books"
IMAGE_DIR = "shyfly/images"

STORY_MODEL = "claude-sonnet-4-5"
IMAGE_MODEL = "gemini-2.5-flash-image"
MAX_STORY_TOKENS = 4000

_lock = threading.Lock()
_anthropic_client = None
_gemini_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _gemini_client


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# --- character bible (persistent, established once, reused every book) ---

def _read_characters_raw():
    text = storage.read_text(CHARACTERS_BLOB)
    if text is None:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def get_character_bible():
    with _lock:
        return _read_characters_raw()


CHARACTER_BIBLE_PROMPT = (
    "You are designing recurring characters for a children's picture book "
    "series for early readers (ages 5-7), based on this first book's plot:\n\n"
    "{theme}\n\n"
    "Identify the main recurring characters (there should be exactly two "
    "unless the plot clearly requires otherwise) and write a detailed, "
    "consistent VISUAL description for each - specific enough that an "
    "illustrator could draw the same character the same way every single "
    "time (species/body shape, colors, clothing/accessories, facial "
    "expression style, size, any distinguishing features). This is a "
    "reference sheet, not the story itself - describe appearance only, in "
    "1-3 sentences per character. "
    'Respond as JSON only: {{"characters": [{{"name": "...", '
    '"visual_description": "..."}}]}}'
)


def _draft_character_bible(theme):
    response = _get_anthropic_client().messages.create(
        model=STORY_MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": CHARACTER_BIBLE_PROMPT.format(theme=theme)}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    parsed = json.loads(strip_json_fence(text))
    return parsed.get("characters", [])


def _generate_character_reference_image(name, visual_description):
    """Returns (blob_path, mime_type, error). error is None on success -
    on failure (e.g. Gemini image generation isn't available yet because
    billing isn't enabled on the project), returns (None, None, message)
    rather than raising, so a missing reference image never blocks the
    text description from being established."""
    prompt = (
        f"A children's picture book character reference portrait of "
        f"{name}. {visual_description} Simple, warm, flat-illustration "
        "style suitable for an early-reader picture book. Plain light "
        "background, character centered, friendly and expressive pose. "
        "This is a character model sheet - clear, unambiguous depiction "
        "of exactly this one character."
    )
    try:
        data, mime_type = _generate_image_bytes(prompt)
    except Exception as e:
        return None, None, str(e)
    ext = ".png" if "png" in mime_type else ".jpg"
    blob_path = f"{IMAGE_DIR}/characters/{_slugify(name)}{ext}"
    storage.write_bytes(blob_path, data, content_type=mime_type)
    return blob_path, mime_type, None


def ensure_character_bible(theme):
    """Returns the persistent character bible, drafting each character's
    text description from `theme` the first time this is ever called -
    subsequent calls never re-draft or overwrite an established
    description, regardless of what `theme` is passed, which is what makes
    "future books maintain the same look".

    Self-healing for reference *images* specifically: if a character's
    portrait failed earlier (e.g. Gemini image generation wasn't available
    yet), every call retries just that image - once it succeeds, it's
    saved and never regenerated again either. The text description is
    never blocked on the image, so the story/pipeline can proceed with a
    character that has no locked-in visual reference yet."""
    with _lock:
        bible = _read_characters_raw()

    if not bible:
        drafted = _draft_character_bible(theme)
        bible = {}
        for char in drafted:
            name = (char.get("name") or "").strip()
            description = (char.get("visual_description") or "").strip()
            if not name or not description:
                continue
            bible[name.lower()] = {
                "name": name,
                "visual_description": description,
                "reference_image_blob": None,
                "reference_image_mime_type": None,
                "reference_image_error": None,
                "created_at": _now_iso(),
            }

    changed = False
    for entry in bible.values():
        if entry.get("reference_image_blob"):
            continue
        blob_path, mime_type, error = _generate_character_reference_image(
            entry["name"], entry["visual_description"]
        )
        entry["reference_image_blob"] = blob_path
        entry["reference_image_mime_type"] = mime_type
        entry["reference_image_error"] = error
        changed = True

    if changed:
        with _lock:
            # Re-check under the lock in case of a race between two
            # concurrent calls - merge rather than blindly overwrite, since
            # the other caller may have filled in an image this one didn't.
            current = _read_characters_raw()
            for key, entry in bible.items():
                current_entry = current.get(key)
                if current_entry and current_entry.get("reference_image_blob"):
                    continue  # someone else already filled this one in
                current[key] = entry
            storage.write_text(CHARACTERS_BLOB, json.dumps(current, indent=2))
            bible = current
    return bible


def _slugify(name):
    return "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_")


# --- Gemini image generation ---

def _generate_image_bytes(prompt, reference_images=None):
    """reference_images: optional list of (bytes, mime_type) tuples passed
    as actual image input alongside the text prompt, for character
    consistency. Returns (image_bytes, mime_type)."""
    contents = []
    if reference_images:
        for data, mime_type in reference_images:
            contents.append(types.Part.from_bytes(data=data, mime_type=mime_type))
    contents.append(prompt)

    response = _get_gemini_client().models.generate_content(
        model=IMAGE_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(aspect_ratio="1:1"),
        ),
    )
    candidates = response.candidates or []
    if not candidates or not candidates[0].content or not candidates[0].content.parts:
        raise RuntimeError("Gemini returned no content for this illustration.")
    for part in candidates[0].content.parts:
        if part.inline_data and part.inline_data.data:
            return part.inline_data.data, part.inline_data.mime_type
    raise RuntimeError("Gemini didn't return an image for this prompt (text-only response).")


def _reference_images_for_bible(bible):
    """Loads every character's reference image bytes once per book, so all
    pages generated for that book share the exact same conditioning input.
    Characters with no reference image yet (e.g. generated before billing
    was enabled) are silently skipped - page generation still proceeds
    text-only for them rather than blocking on it."""
    refs = []
    for entry in bible.values():
        blob_path = entry.get("reference_image_blob")
        if not blob_path:
            continue
        data = storage.read_bytes(blob_path)
        if data is not None:
            refs.append((data, entry["reference_image_mime_type"]))
    return refs


# --- story + per-page illustration description generation ---

STORY_PROMPT = (
    "You are writing book {book_number} of \"The Shy Fly\", a children's "
    "picture book series for early readers (ages 5-7). Simple vocabulary, "
    "short sentences, warm and funny tone, a clear beginning/middle/end. "
    "Write 10-15 pages, each with exactly one short beat of story (1-3 "
    "sentences of page text - this is read aloud to a 5-7 year old).\n\n"
    "This book's theme/plot:\n{theme}\n\n"
    "Established recurring characters (keep their personalities and visual "
    "appearance consistent with these descriptions in every "
    "illustration_description you write):\n{character_notes}\n\n"
    "For each page, also write an illustration_description: a specific, "
    "concrete scene description for an illustrator, naming which "
    "established characters appear and what they're doing/feeling in that "
    "moment - reference their established visual description rather than "
    "re-inventing their look. "
    'Respond as JSON only: {{"title": "...", "pages": [{{"page_number": 1, '
    '"text": "...", "illustration_description": "..."}}, ...]}}'
)


def _character_notes(bible):
    if not bible:
        return "(none established yet - this is the first book in the series)"
    return "\n".join(f"- {c['name']}: {c['visual_description']}" for c in bible.values())


def _draft_story(book_number, theme, bible):
    prompt = STORY_PROMPT.format(
        book_number=book_number, theme=theme, character_notes=_character_notes(bible)
    )
    response = _get_anthropic_client().messages.create(
        model=STORY_MODEL,
        max_tokens=MAX_STORY_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    parsed = json.loads(strip_json_fence(text))
    return parsed.get("title", f"The Shy Fly, Book {book_number}"), parsed.get("pages", [])


# --- book storage ---

def _book_blob(book_number):
    return f"{BOOKS_DIR}/{book_number}.json"


def read_book(book_number):
    text = storage.read_text(_book_blob(book_number))
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _write_book(book):
    storage.write_text(_book_blob(book["book_number"]), json.dumps(book, indent=2))


def list_books():
    """Lists all books - no index blob, so this just lists everything under
    BOOKS_DIR via storage.py; book counts are small (one per week) so
    reading each one is cheap."""
    books = []
    for relative_path in storage.list_prefix(f"{BOOKS_DIR}/"):
        if not relative_path.endswith(".json"):
            continue
        text = storage.read_text(relative_path)
        if text is None:
            continue
        try:
            book = json.loads(text)
        except json.JSONDecodeError:
            continue
        books.append(_book_summary(book))
    return sorted(books, key=lambda b: b["book_number"])


def _book_summary(book):
    return {
        "book_number": book["book_number"],
        "title": book["title"],
        "status": book["status"],
        "created_at": book["created_at"],
        "approved_at": book.get("approved_at"),
        "page_count": len(book.get("pages", [])),
    }


def approve_book(book_number):
    book = read_book(book_number)
    if book is None:
        return False, "No such book."
    if book["status"] == "approved":
        return False, "Already approved."
    book["status"] = "approved"
    book["approved_at"] = _now_iso()
    _write_book(book)
    return True, "Approved."


# --- orchestration ---

def generate_book(book_number, theme):
    """Full pipeline: ensure the character bible exists, draft the story +
    per-page illustration descriptions, generate an illustration for every
    page (conditioned on the established character reference images), and
    save the result as a draft. Runs synchronously - this is a handful of
    sequential API calls, not an agentic/untrusted process, so it doesn't
    need code mode's job isolation."""
    bible = ensure_character_bible(theme)
    reference_images = _reference_images_for_bible(bible)

    title, pages_in = _draft_story(book_number, theme, bible)

    pages_out = []
    for page in pages_in:
        page_number = page.get("page_number", len(pages_out) + 1)
        text = (page.get("text") or "").strip()
        illustration_description = (page.get("illustration_description") or "").strip()
        entry = {
            "page_number": page_number,
            "text": text,
            "illustration_description": illustration_description,
            "image_blob": None,
            "image_mime_type": None,
            "image_error": None,
        }
        if illustration_description:
            try:
                data, mime_type = _generate_image_bytes(illustration_description, reference_images)
                blob_path = f"{IMAGE_DIR}/books/{book_number}/page_{page_number}.png"
                storage.write_bytes(blob_path, data, content_type=mime_type)
                entry["image_blob"] = blob_path
                entry["image_mime_type"] = mime_type
            except Exception as e:
                entry["image_error"] = str(e)
        pages_out.append(entry)

    book = {
        "book_number": book_number,
        "title": title,
        "theme": theme,
        "status": "draft",
        "created_at": _now_iso(),
        "approved_at": None,
        "pages": pages_out,
    }
    _write_book(book)
    return book
