#!/usr/bin/env python3
"""FantLab backup: ratings and reviews for any FantLab user.

Downloads the public ratings list and the full review texts from
fantlab.ru and stores one dated snapshot per run under
~/workspace/fantlab-backup/snapshots/<YYYY-MM-DD>/.

The FantLab user is supplied on the command line (--user-id, and
optionally --username); nothing about a particular user is hard-coded.

Standard library only (urllib + html.parser). No sign-in is needed:
the ratings list, the reviews list and the profile page are public.

Usage:
    python3 fantlab_backup.py --user-id 54173 [--username webstranger]
                              [--out DIR] [--dry-run]
                              [--max-pages N] [--resolve-permalinks]

  --user-id N          FantLab numeric user id (required). Example: the
                       user at https://fantlab.ru/user54173 has id 54173.
  --username NAME      FantLab login name, used only for display and for
                       matching the author byline (optional).
  --out DIR            snapshot root (default ~/workspace/fantlab-backup/snapshots)
  --dry-run            fetch and parse everything, verify counts, write nothing
  --max-pages N        fetch at most N pages of each list (smoke test only)
  --resolve-permalinks also fetch each reviewed work page to resolve the
                       review permalink (slow: one extra request per review)

Exit codes: 0 ok, 1 fetch/parse failure, 2 verification mismatch
(the collected counts do not match the totals FantLab reports).
On a verification mismatch nothing is written to a snapshot directory;
diagnostics go to <out>/failed/<timestamp>/ instead.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html as htmlmod
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

# ---------------------------------------------------------------------------
# Site-specific parameters.
#
# VERIFIED LIVE 2026-11-22 by direct fetch (no sign-in). The FantLab
# user is supplied at run time (--user-id, --username); the URLs are
# built from the user id by the helpers below, so no particular user
# is hard-coded anywhere in this file.
# ---------------------------------------------------------------------------

BASE_URL = "https://fantlab.ru"


def profile_url(user_id: int) -> str:
    return f"{BASE_URL}/user{user_id}"


def marks_url(user_id: int) -> str:
    return f"{BASE_URL}/user{user_id}/marks"


def responses_url(user_id: int) -> str:
    return f"{BASE_URL}/user{user_id}/responses"

# Ratings list. Default sort is by date (sort=date is also the active
# link on the page). Pages: ?page=1..N&sort=date, 200 rows per page.
# There is also a ?page=all link, but paginated fetching is used: it
# is gentler on the server and a single huge page is more likely to be
# truncated.
MARKS_SORT = "date"          # SORT_PARAM value: date | mark | name | autor | year
MARKS_TYPE = "all"           # TYPE_PARAM value: all | novel | story | cycle | graph
PAGE_PARAM = "page"          # pagination query parameter name
SORT_PARAM = "sort"          # sort query parameter name
MARKS_PAGE_SIZE = 200        # rows per marks page (observed 2026-11-22)

# Reviews list. Linked from the profile page as "Читать все отзывы".
# 50 reviews per page. Each item carries work, date/time, full text
# and score, so no per-work fetch is needed for the backup itself.
RESPONSES_PAGE_SIZE = 50     # reviews per responses page (observed 2026-11-22)

# Review permalinks. On a work page each review has an anchor
# <a name="responseNNNNNN"> and a copy-link that builds the permalink
# /work<id>/toresponse<response_id> (observed on /work2437003; the
# server redirects it to /work<id>?page=1#response<response_id>).
# The plain anchor form /work<id>?sort=date#response<response_id>
# points at the same review. Used only by find_review_on_work_page()
# / --resolve-permalinks.
REVIEW_URL_TEMPLATE = BASE_URL + "/work{work_id}/toresponse{response_id}"
REVIEW_ANCHOR_RE = re.compile(r"response(\d+)")

# Polite fetching: at least this many seconds between requests.
REQUEST_DELAY = 1.2
MAX_ATTEMPTS = 3             # attempts per request (transient errors only)
REQUEST_TIMEOUT = 30
USER_AGENT = (
    "FantLabBackup/1.0 (personal backup script; "
    "https://github.com/spmuse/fantlab-backup)"
)

DEFAULT_OUT = Path("~/workspace/fantlab-backup/snapshots").expanduser()

RUSSIAN_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


class BackupError(Exception):
    """A fetch or parse failure: abort, do not write a partial snapshot."""


class VerificationError(BackupError):
    """Collected counts do not match the totals FantLab reports."""


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

_last_fetch = 0.0


def fetch(url: str) -> str:
    """GET a page as UTF-8 text, politely paced, with retries.

    Retries only transient failures (5xx, network errors). A 404/403
    fails immediately: it means the URL or layout changed, and retrying
    would not help.
    """
    global _last_fetch
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wait = REQUEST_DELAY - (time.monotonic() - _last_fetch)
        if wait > 0:
            time.sleep(wait)
        _last_fetch = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 and attempt < MAX_ATTEMPTS:
                log(f"HTTP {exc.code} for {url}, retry {attempt}/{MAX_ATTEMPTS}")
                time.sleep(REQUEST_DELAY * attempt)
                continue
            raise BackupError(f"HTTP {exc.code} fetching {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < MAX_ATTEMPTS:
                log(f"network error for {url} ({exc}), retry {attempt}/{MAX_ATTEMPTS}")
                time.sleep(REQUEST_DELAY * attempt)
                continue
            raise BackupError(f"network error fetching {url}: {exc}") from exc
        charset = "utf-8"
        return raw.decode(charset, errors="strict")
    raise BackupError(f"gave up fetching {url}")


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Collapse whitespace and unescape HTML entities."""
    return re.sub(r"\s+", " ", htmlmod.unescape(text)).strip()


class _TableParser(HTMLParser):
    """Collects rows (lists of cells) of the first <table> found.

    Each cell is a dict: {"text": plain text, "links": [(href, text)],
    "html": raw inner HTML}. Keeping the raw cell HTML lets the caller
    inspect span classes (classification) and other markup.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[dict]]] = []
        self._table: list[list[dict]] | None = None
        self._table_is_marks = False
        self._row: list[dict] | None = None
        self._cell: dict | None = None
        self._cell_html: list[str] = []
        self._in_a = False
        self._a_href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        if tag == "table" and self._table is None:
            # The marks table carries class "v9b"; other tables on the
            # page (footer etc.) are ignored.
            self._table_is_marks = "v9b" in attrs_d.get("class", "")
            self._table = [] if self._table_is_marks else None
            if not self._table_is_marks:
                return
        elif tag == "tr" and self._table is not None and self._row is None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None and self._cell is None:
            self._cell = {"text": "", "links": [], "html": ""}
            self._cell_html = []
        if self._cell is not None:
            self._cell_html.append(self.get_starttag_text() or "")
        if tag == "a" and self._cell is not None:
            self._in_a = True
            self._a_href = attrs_d.get("href", "")

    def handle_endtag(self, tag: str) -> None:
        if self._cell is not None:
            self._cell_html.append(f"</{tag}>")
        if tag == "a" and self._cell is not None:
            self._in_a = False
            self._cell["links"].append((self._a_href, ""))
            self._a_href = ""
        elif tag in ("td", "th") and self._cell is not None:
            self._cell["html"] = "".join(self._cell_html)
            self._cell["text"] = _clean(self._cell["text"])
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"] += data
            if self._in_a and self._cell["links"] is not None:
                pass  # link text is recovered from cell text when needed


def parse_marks_page(html: str, page_no: int) -> list[dict]:
    """Parse one marks page into rating dicts.

    Fails loudly (BackupError) if the expected table/columns are absent
    or a row is malformed, instead of silently returning partial data.
    """
    m = re.search(r"Всего оценок:\s*(\d+)", html)
    if not m:
        raise BackupError(f"marks page {page_no}: 'Всего оценок' total not found")
    parser = _TableParser()
    parser.feed(html)
    if not parser.tables:
        raise BackupError(f"marks page {page_no}: no table found")
    table = parser.tables[0]
    if not table:
        raise BackupError(f"marks page {page_no}: empty table")
    header = [c["text"] for c in table[0]]
    if header[:2] != ["Произведение", "Оценка"] or len(header) < 5:
        raise BackupError(f"marks page {page_no}: unexpected header {header!r}")

    ratings: list[dict] = []
    for row in table[1:]:
        if len(row) < 5:
            raise BackupError(f"marks page {page_no}: row with {len(row)} cells: {row!r}")
        work_cell, score_cell, classif_cell, review_cell, date_cell = row[:5]
        work_links = [h for h, _ in work_cell["links"] if h.startswith("/work")]
        if not work_links:
            raise BackupError(f"marks page {page_no}: row without work link: {work_cell['text']!r}")
        work_id = int(re.search(r"/work(\d+)", work_links[0]).group(1))
        # Cell text looks like: '1.  Михаил Ланцов «Наследник империи. Том 3» [роман], 2026 г.'
        text = work_cell["text"]
        title_m = re.search(r"«(.+?)»", text)
        if not title_m:
            raise BackupError(f"marks page {page_no}: no «title» in {text!r}")
        title = title_m.group(1)
        authors = _clean(re.sub(r"^\d+\.\s*", "", text[: title_m.start()])).rstrip(",")
        type_m = re.search(r"\[([^\]]+)\]", text[title_m.end():])
        year_m = re.search(r"(\d{4})\s*г\.", text[title_m.end():])
        try:
            score = int(score_cell["text"])
        except ValueError as exc:
            raise BackupError(f"marks page {page_no}: bad score {score_cell['text']!r}") from exc
        if not 1 <= score <= 10:
            raise BackupError(f"marks page {page_no}: score out of range: {score}")
        date_rated = date_cell["text"]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_rated):
            raise BackupError(f"marks page {page_no}: bad date {date_rated!r}")
        classified = "box-classif-full" in classif_cell["html"]
        has_review = review_cell["text"].strip() == "есть"
        ratings.append({
            "work_id": work_id,
            "work_url": BASE_URL + f"/work{work_id}",
            "authors": authors,
            "title": title,
            "type": type_m.group(1) if type_m else "",
            "year": int(year_m.group(1)) if year_m else None,
            "score": score,
            "date_rated": date_rated,
            "classified": classified,
            "has_review": has_review,
        })
    return ratings


def parse_marks_total(html: str) -> int:
    m = re.search(r"Всего оценок:\s*(\d+)", html)
    if not m:
        raise BackupError("marks page: 'Всего оценок' total not found")
    return int(m.group(1))


def parse_profile(html: str) -> dict:
    """Parse the profile page totals: ratings count and reviews count."""
    ratings_m = re.search(r">Оценок</a>\s*</td>\s*<td>\s*(\d+)", html)
    reviews_m = re.search(r">Отзывов</a>\s*</td>\s*<td>\s*(\d+)", html)
    if not ratings_m or not reviews_m:
        raise BackupError("profile page: ratings/reviews totals not found")
    return {"ratings_total": int(ratings_m.group(1)),
            "reviews_total": int(reviews_m.group(1))}


def parse_review_date(raw: str, fetch_date: dt.date) -> str:
    """Parse a review date from the responses list.

    Forms seen live: '23 ноября 2025 г. 01:39' (full date), '1 ноября 16:56'
    (day and month, no year: the current year), and, for today's
    reviews, 'в 17:54' (time only: the fetch date is the review date).
    Returns 'YYYY-MM-DD HH:MM'.
    """
    raw = _clean(raw)
    months = ("января|февраля|марта|апреля|мая|июня|июля|августа|"
              "сентября|октября|ноября|декабря")
    m = re.search(
        rf"(\d{{1,2}}) ({months}) (\d{{4}}) г\. (\d{{2}}):(\d{{2}})", raw)
    if m:
        day, month, year, hh, mm = m.groups()
        return f"{int(year):04d}-{RUSSIAN_MONTHS[month]:02d}-{int(day):02d} {hh}:{mm}"
    m = re.search(rf"(\d{{1,2}}) ({months}) (\d{{2}}):(\d{{2}})", raw)
    if m:
        day, month, hh, mm = m.groups()
        return (f"{fetch_date.year:04d}-{RUSSIAN_MONTHS[month]:02d}-"
                f"{int(day):02d} {hh}:{mm}")
    m = re.search(r"в (\d{2}):(\d{2})", raw)
    if m:
        return f"{fetch_date.isoformat()} {m.group(1)}:{m.group(2)}"
    raise BackupError(f"unparseable review date: {raw!r}")


class _ResponsesParser(HTMLParser):
    """Parses one /user<id>/responses page.

    Each review is a <div class="response-item"> containing:
      p.response-work-info > a[href=/workNNNN...] 'Author «Title»'
      p.response-autor-info '<username>, <date>'
      div.response-body-home (full review text, spoiler blocks included)
      div.response-autor-mark 'Оценка: N'
    """

    def __init__(self, fetch_date: dt.date) -> None:
        super().__init__(convert_charrefs=True)
        self.fetch_date = fetch_date
        self.reviews: list[dict] = []
        self._cur: dict | None = None
        self._depth = 0
        self._section: str | None = None  # work | autor | body | mark
        self._section_depth = 0
        self._buf: list[str] = []
        self._in_spoiler = False

    def _start_section(self, name: str) -> None:
        self._section = name
        self._section_depth = self._depth
        self._buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        cls = attrs_d.get("class", "")
        if tag == "div" and "response-item" in cls and self._cur is None:
            self._cur = {"work_id": None, "work_url": "", "authors": "",
                         "title": "", "date": "", "score": None,
                         "text": "", "has_spoiler": False}
            self._depth = 1
            return
        if self._cur is None:
            return
        if tag == "div":
            self._depth += 1
        if tag == "p" and "response-work-info" in cls:
            self._start_section("work")
        elif tag == "p" and "response-autor-info" in cls:
            self._start_section("autor")
        elif tag == "div" and "response-body-home" in cls:
            self._start_section("body")
        elif tag == "div" and "response-autor-mark" in cls:
            self._start_section("mark")
        if self._section == "body" and tag == "div" and "h" in cls.split():
            self._in_spoiler = True
            if self._cur is not None:
                self._cur["has_spoiler"] = True
        if self._section == "work" and tag == "a":
            href = attrs_d.get("href", "")
            wm = re.search(r"/work(\d+)", href)
            if wm and self._cur is not None:
                self._cur["work_id"] = int(wm.group(1))
                self._cur["work_url"] = BASE_URL + f"/work{wm.group(1)}"
        if self._section in ("work", "autor", "body", "mark"):
            self._buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._cur is None:
            return
        # Close a section only on its own closing tag at its own depth.
        # The body section contains nested <p> and spoiler <div> elements,
        # so any other closing tag must not end it early.
        closes_section = (
            (self._section in ("work", "autor") and tag == "p")
            or (self._section in ("body", "mark") and tag == "div"
                and self._depth == self._section_depth))
        if closes_section:
            if tag == "div" and self._section == "body" and self._in_spoiler:
                self._in_spoiler = False
            text = _clean("".join(self._buf))
            if self._section == "work":
                tm = re.search(r"«(.+?)»", text)
                if not tm:
                    raise BackupError(f"review without «title»: {text!r}")
                self._cur["title"] = tm.group(1)
                self._cur["authors"] = _clean(text[: tm.start()]).rstrip(",")
            elif self._section == "autor":
                dm = re.search(r",\s*(.+)$", text)
                if not dm:
                    raise BackupError(f"review without date: {text!r}")
                self._cur["date"] = parse_review_date(dm.group(1), self.fetch_date)
            elif self._section == "body":
                self._cur["text"] = text
            elif self._section == "mark":
                # Usually 'Оценка: N'; some reviews carry no score ('Оценка: нет').
                sm = re.search(r"(\d+)", text)
                self._cur["score"] = int(sm.group(1)) if sm else None
            self._section = None
            self._buf = []
        elif tag == "div" and self._section == "body" and self._in_spoiler:
            # A nested </div> inside a spoiler block: spoiler ends, body continues.
            self._in_spoiler = False
        if tag == "div":
            self._depth -= 1
            if self._depth == 0:
                cur = self._cur
                # Score may legitimately be None ('Оценка: нет').
                missing = [k for k in ("work_id", "title", "date")
                           if not cur.get(k)]
                if missing or not cur["text"]:
                    raise BackupError(f"incomplete review parsed: {cur!r}")
                self.reviews.append(cur)
                self._cur = None

    def handle_data(self, data: str) -> None:
        if self._cur is not None and self._section in ("work", "autor", "body", "mark"):
            self._buf.append(data)


def parse_responses_page(html: str, fetch_date: dt.date, page_no: int) -> list[dict]:
    parser = _ResponsesParser(fetch_date)
    parser.feed(html)
    if parser._cur is not None:
        raise BackupError(f"responses page {page_no}: unterminated review item")
    return parser.reviews


def find_review_on_work_page(work_id: int, user_id: int,
                            username: str = "") -> dict | None:
    """Locate one user's review on a work page (fallback / permalink tool).

    Reviews live on the work page as <a name="responseNNNNNN"> anchors
    with the author link /user<id> and a datePublished attribute.
    Returns {response_id, permalink, date_published, text} or None if
    the user has no review on that page. The permalink template is
    REVIEW_URL_TEMPLATE. This is NOT used by the default backup run:
    the /responses list already carries the full text.
    """
    url = BASE_URL + f"/work{work_id}"
    html = fetch(url)
    # Each review block starts at its anchor; take the chunk up to the
    # next anchor (or end of the responses section).
    anchors = list(REVIEW_ANCHOR_RE.finditer(html))
    for i, anchor in enumerate(anchors):
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(html)
        chunk = html[anchor.start():end]
        if f"href='/user{user_id}'" not in chunk and f'href="/user{user_id}"' not in chunk:
            continue
        if username and username not in chunk:
            continue
        date_m = re.search(r'datePublished"\s+content="([^"]+)"', chunk)
        body_m = re.search(
            r'class="response-body-home"[^>]*>(.*?)</div>\s*</div>',
            chunk, re.S)
        text = _clean(re.sub(r"<[^>]+>", " ", body_m.group(1))) if body_m else ""
        response_id = anchor.group(1)
        return {
            "response_id": response_id,
            "permalink": REVIEW_URL_TEMPLATE.format(
                work_id=work_id, response_id=response_id),
            "date_published": date_m.group(1) if date_m else "",
            "text": text,
        }
    return None


# ---------------------------------------------------------------------------
# Snapshot storage and diff
# ---------------------------------------------------------------------------

def snapshot_dir(out_root: Path, day: dt.date) -> Path:
    """A dated directory that never overwrites a previous snapshot."""
    candidate = out_root / day.isoformat()
    if not candidate.exists():
        return candidate
    n = 2
    while (out_root / f"{day.isoformat()}-{n}").exists():
        n += 1
    return out_root / f"{day.isoformat()}-{n}"


def previous_snapshot(out_root: Path, current: Path) -> Path | None:
    if not out_root.exists():
        return None
    siblings = sorted(
        (p for p in out_root.iterdir()
         if p.is_dir() and p.name[0].isdigit() and p != current),
        key=lambda p: p.name, reverse=True)
    for sib in siblings:
        if (sib / "ratings.json").exists() and (sib / "reviews.json").exists():
            return sib
    return None


def compute_diff(old: dict, new_ratings: list[dict], new_reviews: list[dict]) -> dict:
    old_ratings = {r["work_id"]: r for r in old.get("ratings", [])}
    new_r = {r["work_id"]: r for r in new_ratings}
    new_ids, old_ids = set(new_r), set(old_ratings)
    changed_ratings = []
    for wid in sorted(new_ids & old_ids):
        o, n = old_ratings[wid], new_r[wid]
        diffs = {k: {"old": o.get(k), "new": n.get(k)}
                 for k in ("score", "date_rated", "classified", "has_review")
                 if o.get(k) != n.get(k)}
        if diffs:
            changed_ratings.append({"work_id": wid, "title": n["title"],
                                    "authors": n["authors"], "changes": diffs})

    def review_key(rv: dict) -> str:
        return f"{rv['work_id']}@{rv['date']}"

    old_reviews = {review_key(rv): rv for rv in old.get("reviews", [])}
    new_revs = {review_key(rv): rv for rv in new_reviews}
    changed_reviews = []
    for key in sorted(set(new_revs) & set(old_reviews)):
        o, n = old_reviews[key], new_revs[key]
        if o.get("text") != n.get("text") or o.get("score") != n.get("score"):
            changed_reviews.append({"work_id": n["work_id"], "title": n["title"],
                                    "date": n["date"]})
    return {
        "new_ratings": [new_r[w] for w in sorted(new_ids - old_ids)],
        "changed_ratings": changed_ratings,
        "removed_ratings": [old_ratings[w] for w in sorted(old_ids - new_ids)],
        "new_reviews": [new_revs[k] for k in sorted(set(new_revs) - set(old_reviews))],
        "changed_reviews": changed_reviews,
        "removed_reviews": [old_reviews[k] for k in sorted(set(old_reviews) - set(new_revs))],
        "counts": {
            "new_ratings": len(new_ids - old_ids),
            "changed_ratings": len(changed_ratings),
            "removed_ratings": len(old_ids - new_ids),
            "new_reviews": len(set(new_revs) - set(old_reviews)),
            "changed_reviews": len(changed_reviews),
            "removed_reviews": len(set(old_reviews) - set(new_revs)),
        },
    }


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def collect(user_id: int, username: str, max_pages: int | None,
            resolve_permalinks: bool, dry_run: bool) -> dict:
    """Fetch and parse everything. Returns all data plus raw HTML pages."""
    today = dt.date.today()
    fetch_date = today
    log(f"fetching profile {profile_url(user_id)}")
    profile_html = fetch(profile_url(user_id))
    profile = parse_profile(profile_html)
    log(f"profile totals: {profile['ratings_total']} ratings, "
        f"{profile['reviews_total']} reviews")

    # --- ratings, paginated, sort by date ---
    ratings: list[dict] = []
    raw_pages: list[tuple[str, str]] = [("profile.html", profile_html)]
    total_expected: int | None = None
    page = 1
    while True:
        if max_pages is not None and page > max_pages:
            log(f"--max-pages {max_pages}: stopping marks after page {page - 1}")
            break
        url = (f"{marks_url(user_id)}?{PAGE_PARAM}={page}&{SORT_PARAM}={MARKS_SORT}"
               f"&type={MARKS_TYPE}")
        log(f"fetching marks page {page}: {url}")
        html = fetch(url)
        raw_pages.append((f"marks-page-{page}.html", html))
        if total_expected is None:
            total_expected = parse_marks_total(html)
            log(f"marks total reported: {total_expected}")
        page_ratings = parse_marks_page(html, page)
        if page > 1 and len(page_ratings) == 0:
            raise BackupError(f"marks page {page}: empty page before reaching total")
        if page_ratings and len(page_ratings) != MARKS_PAGE_SIZE and total_expected is not None:
            remaining = total_expected - len(ratings)
            if len(page_ratings) != remaining:
                raise BackupError(
                    f"marks page {page}: got {len(page_ratings)} rows, "
                    f"expected {MARKS_PAGE_SIZE} or the final {remaining}")
        ratings.extend(page_ratings)
        log(f"marks page {page}: {len(page_ratings)} ratings "
            f"(total so far {len(ratings)})")
        if total_expected is not None and len(ratings) >= total_expected:
            break
        if len(page_ratings) < MARKS_PAGE_SIZE:
            break
        page += 1

    # --- reviews, paginated ---
    reviews: list[dict] = []
    page = 1
    while True:
        if max_pages is not None and page > max_pages:
            log(f"--max-pages {max_pages}: stopping responses after page {page - 1}")
            break
        url = f"{responses_url(user_id)}?{PAGE_PARAM}={page}"
        log(f"fetching responses page {page}: {url}")
        html = fetch(url)
        raw_pages.append((f"responses-page-{page}.html", html))
        page_reviews = parse_responses_page(html, fetch_date, page)
        if page > 1 and not page_reviews:
            raise BackupError(f"responses page {page}: empty page")
        reviews.extend(page_reviews)
        log(f"responses page {page}: {len(page_reviews)} reviews "
            f"(total so far {len(reviews)})")
        if len(page_reviews) < RESPONSES_PAGE_SIZE:
            break
        page += 1

    if resolve_permalinks:
        log("resolving review permalinks from work pages "
            f"({len(reviews)} reviews)")
        for rv in reviews:
            found = find_review_on_work_page(rv["work_id"], user_id, username)
            rv["permalink"] = found["permalink"] if found else None
            if found and found["text"] and found["text"] != rv["text"]:
                log(f"warning: text mismatch for work {rv['work_id']}")

    # Deduplicate ratings by work (a work should appear once).
    seen: set[int] = set()
    unique_ratings: list[dict] = []
    for r in ratings:
        if r["work_id"] in seen:
            log(f"warning: duplicate rating row for work {r['work_id']}, keeping first")
            continue
        seen.add(r["work_id"])
        unique_ratings.append(r)

    return {
        "ratings": unique_ratings,
        "reviews": reviews,
        "profile": profile,
        "total_expected": total_expected,
        "raw_pages": raw_pages,
        "fetch_date": today,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Back up FantLab ratings and reviews.")
    ap.add_argument("--user-id", type=int, required=True,
                    help="FantLab numeric user id, e.g. 54173 for "
                    "https://fantlab.ru/user54173")
    ap.add_argument("--username", default="",
                    help="FantLab login name, used only for display and "
                    "for matching the review byline (optional)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="snapshot root directory")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch, parse and verify, but write nothing")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="fetch at most N pages of each list (smoke test)")
    ap.add_argument("--resolve-permalinks", action="store_true",
                    help="also fetch each work page to resolve review permalinks")
    args = ap.parse_args(argv)

    try:
        data = collect(args.user_id, args.username,
                       args.max_pages, args.resolve_permalinks, args.dry_run)
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    ratings, reviews = data["ratings"], data["reviews"]
    profile = data["profile"]
    limited = args.max_pages is not None

    # --- verification (skipped only when --max-pages truncated the fetch) ---
    if not limited:
        if len(ratings) != data["total_expected"]:
            msg = (f"ratings count mismatch: collected {len(ratings)}, "
                   f"FantLab reports {data['total_expected']}")
            return fail(args.out, msg, data)
        if len(ratings) != profile["ratings_total"]:
            msg = (f"ratings count mismatch: collected {len(ratings)}, "
                   f"profile reports {profile['ratings_total']}")
            return fail(args.out, msg, data)
        if len(reviews) != profile["reviews_total"]:
            msg = (f"reviews count mismatch: collected {len(reviews)}, "
                   f"profile reports {profile['reviews_total']}")
            return fail(args.out, msg, data)
    else:
        log(f"--max-pages: verification skipped "
            f"({len(ratings)} ratings, {len(reviews)} reviews collected)")

    if args.dry_run:
        print(f"DRY RUN OK: {len(ratings)} ratings, {len(reviews)} reviews parsed")
        print(f"  marks total reported: {data['total_expected']}, "
              f"profile: {profile['ratings_total']} ratings / "
              f"{profile['reviews_total']} reviews")
        if ratings:
            print(f"  newest rating: {ratings[0]['date_rated']} "
                  f"{ratings[0]['authors']} «{ratings[0]['title']}» "
                  f"score {ratings[0]['score']}")
        if reviews:
            print(f"  newest review: {reviews[0]['date']} "
                  f"{reviews[0]['authors']} «{reviews[0]['title']}» "
                  f"score {reviews[0]['score']}")
        print("  nothing written (--dry-run)")
        return 0

    # --- write the snapshot ---
    out_root: Path = args.out.expanduser()
    out_root.mkdir(parents=True, exist_ok=True)
    snap = snapshot_dir(out_root, data["fetch_date"])
    prev = previous_snapshot(out_root, snap)
    old_data: dict = {}
    if prev is not None:
        old_data = {
            "ratings": json.loads((prev / "ratings.json").read_text(encoding="utf-8")),
            "reviews": json.loads((prev / "reviews.json").read_text(encoding="utf-8")),
        }
        log(f"previous snapshot: {prev}")
    diff = compute_diff(old_data, ratings, reviews)

    snap.mkdir(parents=True)
    (snap / "raw").mkdir()
    for name, html in data["raw_pages"]:
        (snap / "raw" / name).write_text(html, encoding="utf-8")
    write_json(snap / "ratings.json", ratings)
    write_json(snap / "reviews.json", reviews)
    with open(snap / "ratings.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "work_id", "authors", "title", "type", "year", "score",
            "date_rated", "work_url", "classified", "has_review"])
        w.writeheader()
        w.writerows(ratings)
    write_json(snap / "profile.json", {
        "user": args.username or None, "user_id": args.user_id,
        "snapshot_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "ratings_total": len(ratings),
        "reviews_total": len(reviews),
        "ratings_total_reported": profile["ratings_total"],
        "reviews_total_reported": profile["reviews_total"],
        "sources": {"profile": profile_url(args.user_id),
                    "marks": marks_url(args.user_id),
                    "responses": responses_url(args.user_id)},
    })
    write_json(snap / "diff.json", diff)

    c = diff["counts"]
    print(f"Snapshot written: {snap}")
    print(f"  ratings: {len(ratings)}, reviews: {len(reviews)}")
    print(f"  since previous snapshot: +{c['new_ratings']} ratings, "
          f"{c['changed_ratings']} changed, {c['removed_ratings']} removed; "
          f"+{c['new_reviews']} reviews, {c['changed_reviews']} changed, "
          f"{c['removed_reviews']} removed")
    return 0


def fail(out_root: Path, msg: str, data: dict) -> int:
    """Verification failed: write diagnostics to failed/, never a snapshot."""
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    failed = out_root.expanduser() / "failed" / stamp
    try:
        failed.mkdir(parents=True, exist_ok=True)
        (failed / "error.json").write_text(
            json.dumps({"error": msg, "collected_ratings": len(data["ratings"]),
                        "collected_reviews": len(data["reviews"]),
                        "profile": data["profile"],
                        "total_expected": data["total_expected"]},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: {msg} (also could not write diagnostics: {exc})",
              file=sys.stderr)
        return 2
    print(f"ERROR: {msg}", file=sys.stderr)
    print(f"Diagnostics written to {failed} (no snapshot created)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
