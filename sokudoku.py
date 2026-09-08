#!/usr/bin/env python3
"""sokudoku -- flashing-word speed reading (RSVP) for the terminal.

sokudoku (速読) is Japanese for "speed reading". It flashes a book at you
a few words at a time at one fixed point on the screen: the text moves,
your eyes do not. It reads EPUBs and plain text files -- prose wrapped at
any width, paragraphs separated by blank lines.

The design follows the reading research rather than the app marketing:

  * Speed starts at a gentle 100 wpm and ramps up in 25 wpm steps as
    your focus settles; comprehension holds into the ~400-500 wpm band
    and decays steeply past it (Rayner et al., 2016). Speed is not the
    point of the method; continuity is.
  * Regression is one keypress away -- h/l jump by sentence -- because
    losing the ability to re-read is RSVP's known comprehension cost
    (Schotter, Tran & Rayner, 2014). Resuming from a pause rewinds to
    the start of the current sentence for the same reason.
  * The stream hesitates at sentence ends and a little longer at
    paragraph ends, which measurably helps comprehension (Masson, 1983).
  * Flashes are sized by characters, not word count: whole words are
    packed up to the width budget (one BIG word may exceed it), never
    crossing a sentence boundary -- preserving the phrase structure that
    one-word-at-a-time presentation strips away. Only a super-long word
    dash-breaks across two flashes.

Python's standard library only. No pip, no network. Layers 1-2 (the EPUB
container walk and the XHTML-to-plaintext renderer) are vendored verbatim
from yomu (github.com/v0idravl/yomu) so this file stands alone -- if
yomu's parser changes, port the change here.
"""

# Everything below is Python's standard library. Nothing else exists.
import argparse
import curses
import hashlib
import html.parser
import json
import os
import posixpath
import re
import sys
import textwrap
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import unquote

MAX_WIDTH = 80      # prose measure when flattening EPUB chapters
DEFAULT_WPM = 100   # start slow; ramp up with Up/+ as focus builds
MIN_WPM, MAX_WPM = 60, 1000
WPM_STEP = 25
WIDTHS = (8, 12, 16, 20)  # flash width presets in characters (key: w)
DEFAULT_WIDTH = 16
MIN_WIDTH, MAX_WIDTH_FLASH = 4, 60
SPLIT_OVER = 4      # a word longer than width+this dash-breaks in two
CTX_WORDS = 6       # dimmed context words shown on each side (toggle: c)

# Pauses are multiples of one word's display time: a sentence end adds a
# beat, a paragraph end adds two. The stream breathes instead of
# suffocating the reader (Masson, 1983).
SENT_PAUSE = 1.0
PARA_PAUSE = 2.0

_SENT_END = (".", "!", "?", "…")
_CLOSERS = "\"'”’)]}»"  # quotes/brackets that may follow a real full stop
_RULE_CHARS = frozenset("=-–—_*~")  # heading rules and scene breaks

class SokudokuError(Exception):
    """Every expected failure lands here, so main() prints a clean message."""

# --- Layer 1: the EPUB container (a zip with a fixed map inside) -----------
# Vendored from yomu -- keep in sync.

def _read_container(zf):
    # Every EPUB must contain META-INF/container.xml at that exact path. It
    # is the format's map of itself: it points at the OPF "package document"
    # holding the real metadata. We take the first rootfile, like every
    # reading system in practice (multi-rendition EPUBs are a rarity).
    try:
        data = zf.read("META-INF/container.xml")
    except KeyError:
        raise SokudokuError("not an EPUB: META-INF/container.xml is missing")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise SokudokuError(f"corrupt container.xml: {e}")
    # "{*}name" is ElementTree's namespace wildcard: EPUB XML is always
    # namespaced ("{http://...}rootfile") and we only want the local name.
    for el in root.iterfind(".//{*}rootfile"):
        if el.get("full-path"):
            return el.get("full-path")
    raise SokudokuError("container.xml declares no rootfile (no OPF package)")

def _resolve(opf_dir, href):
    # Hrefs in the OPF are relative to the OPF's own directory, not the zip
    # root. Percent-escapes ("ch%201.xhtml") are literal bytes of the filename
    # inside the zip; a fragment ("#sec2") is an anchor, never part of a path.
    return posixpath.normpath(posixpath.join(opf_dir, unquote(href).split("#")[0]))

def _parse_ncx(data, opf_dir):
    # EPUB 2 TOC: the NCX (a DAISY leftover). Nested <navPoint>s, each with
    # a <navLabel><text> and a <content src="...">; we flatten the nesting
    # into document order -- right for a simple linear reader.
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    toc = []
    for np_ in root.iterfind(".//{*}navPoint"):
        t = np_.find("{*}navLabel/{*}text")
        c = np_.find("{*}content")
        if t is not None and c is not None and c.get("src"):
            label = " ".join((t.text or "").split())
            if label:
                toc.append((label, _resolve(opf_dir, c.get("src"))))
    return toc

class _NavParser(html.parser.HTMLParser):
    # EPUB 3 TOC: a regular XHTML document whose manifest item carries
    # properties="nav". The TOC is a <nav> element of ordinary <a> links, so
    # we reuse the HTML machinery and take the first <nav> only (later navs
    # are landmarks/page-lists, not the reading order).
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self._in_nav, self._seen_nav = [], False, False
        self._href, self._text = None, []

    def handle_starttag(self, tag, attrs):
        if tag == "nav":
            self._in_nav = not self._seen_nav
            self._seen_nav = True
        elif tag == "a" and self._in_nav:
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "nav":
            self._in_nav = False
        elif tag == "a" and self._href is not None:
            label = " ".join("".join(self._text).split())
            if label and self._href:
                self.links.append((label, self._href))
            self._href = None

class Book:
    """An open EPUB: title, spine (reading order), and a TOC."""

    def __init__(self, zf, title, chapters, toc):
        self._zf = zf
        self.title = title
        self.chapters = chapters  # zip paths, in spine (reading) order
        self.toc = toc            # [(label, chapter_index)]
        self._cache = {}          # (index, width) -> [wrapped lines]

    def render(self, index, width):
        key = (index, width)
        if key not in self._cache:
            try:
                raw = self._zf.read(self.chapters[index])
            except KeyError:
                raise SokudokuError(
                    f"spine item missing: {self.chapters[index]}")
            # EPUB mandates UTF-8/16; 'replace' keeps one bad byte from
            # killing the whole chapter.
            self._cache[key] = _render_text(raw.decode("utf-8", "replace"),
                                            width)
        return self._cache[key]

def load_book(path):
    """Open the zip, walk container.xml -> OPF -> manifest/spine."""
    if not os.path.isfile(path):
        raise SokudokuError(f"{path}: no such file")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise SokudokuError(f"{path}: not a valid EPUB (not a zip archive)")
    opf_path = _read_container(zf)
    try:
        opf = ET.fromstring(zf.read(opf_path))
    except KeyError:
        raise SokudokuError(
            f"container points at {opf_path}, not in the archive")
    except ET.ParseError as e:
        raise SokudokuError(f"corrupt OPF package document: {e}")
    opf_dir = posixpath.dirname(opf_path)

    # The MANIFEST is the inventory: every file in the book, keyed by id with
    # its href and media-type. The SPINE is the reading order: idrefs into
    # the manifest. A file can sit in the manifest but not the spine (an
    # appendix reachable only by link); we read spine order only.
    manifest = {}
    for el in opf.iterfind(".//{*}item"):
        if el.get("id") and el.get("href"):
            manifest[el.get("id")] = {
                "path": _resolve(opf_dir, el.get("href")),
                "media": el.get("media-type", ""),
                "props": el.get("properties", "")}
    t = opf.find(".//{*}title")
    title = (t.text or "").strip() if t is not None else None
    chapters = [manifest[el.get("idref")]["path"]
                for el in opf.iterfind(".//{*}itemref")
                if el.get("idref") in manifest]
    if not chapters:
        raise SokudokuError("the OPF spine is empty: nothing to read")

    # TOC detection: EPUB 3 marks its nav document with properties="nav" in
    # the manifest; EPUB 2 uses a separate NCX file (media-type below). Try
    # nav first -- a v3 book may ship a vestigial NCX for old readers -- then
    # NCX, then fall back to the spine itself as the flattest honest TOC.
    index_of = {p: i for i, p in enumerate(chapters)}
    raw_toc = []
    nav = next((i for i in manifest.values() if "nav" in i["props"].split()),
               None)
    if nav:
        try:
            p = _NavParser()
            p.feed(zf.read(nav["path"]).decode("utf-8", "replace"))
            raw_toc = [(t, _resolve(opf_dir, h)) for t, h in p.links]
        except KeyError:
            pass
    if not raw_toc:
        ncx = next((i for i in manifest.values()
                    if i["media"] == "application/x-dtbncx+xml"), None)
        if ncx:
            try:
                raw_toc = _parse_ncx(zf.read(ncx["path"]), opf_dir)
            except KeyError:
                pass
    toc = [(label, index_of[p]) for label, p in raw_toc if p in index_of]
    if not toc:
        toc = [(f"Chapter {i + 1}", i) for i in range(len(chapters))]
    return Book(zf, title or Path(path).stem, chapters, toc)

# --- Layer 2: XHTML -> plaintext -------------------------------------------
# Vendored from yomu -- keep in sync.

_DROP = {"script", "style", "head"}
# Why drop these: <script>/<style> hold code, not prose -- with no JS/CSS
# engine their *source* would render as garbage text. <head> is metadata
# (title, links) that a reader shows in its own UI instead.
_BLOCK = {"p", "div", "section", "article", "header", "footer", "aside",
          "blockquote", "pre", "tr", "dt", "dd"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

class _TextRenderer(html.parser.HTMLParser):
    # Streaming XHTML-to-text. html.parser is forgiving, which suits us:
    # EPUB XHTML is often sloppy, and we want the text, not validity. We
    # collect paragraphs ("blocks") of words -- every block-level tag closes
    # the current paragraph, and textwrap re-wraps later at whatever width
    # we ask for. Raw newlines in HTML source mean nothing, so we never
    # trust them. convert_charrefs=True makes entities (&amp; &mdash;
    # &#8212;) arrive already unescaped in handle_data: named and numeric
    # character references handled uniformly, for free.
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self._words, self._kind = [], [], "p"
        self._drop, self._lists = 0, []

    def _close(self):
        if self._words:
            self.blocks.append((self._kind, " ".join(self._words)))
        self._words, self._kind = [], "p"

    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            self._drop += 1
            return
        if self._drop:
            return
        if tag in ("ul", "ol"):
            self._close()
            self._lists.append([tag, 0])
        elif tag == "li":
            self._close()
            top = self._lists[-1] if self._lists else ["ul", 0]
            top[1] += 1  # an orphaned <li> degrades to a "-" bullet
            self._words.append(f"{top[1]}." if top[0] == "ol" else "-")
        elif tag in _HEADINGS:
            self._close()
            self._kind = tag
        elif tag in _BLOCK or tag == "br":
            self._close()
        elif tag == "hr":
            self._close()
            self.blocks.append(("hr", ""))
        elif tag == "img":
            # No images in a terminal; the alt text is the author's own
            # description of what is missing -- the honest substitute.
            if alt := dict(attrs).get("alt", "").strip():
                self._words.append(f"[img: {alt}]")
        # <a> is deliberately unhandled: its link text flows through as
        # ordinary words and the (useless-in-print) href is dropped.

    def handle_endtag(self, tag):
        if tag in _DROP:
            self._drop = max(0, self._drop - 1)
            return
        if self._drop:
            return
        if tag in ("ul", "ol"):
            self._close()
            if self._lists:
                self._lists.pop()
        elif tag in _BLOCK or tag in _HEADINGS or tag == "li":
            self._close()

    def handle_data(self, data):
        if not self._drop:
            self._words.extend(data.split())

    def close(self):
        super().close()
        self._close()

def _render_text(xhtml, width):
    r = _TextRenderer()
    r.feed(xhtml)
    r.close()
    lines = []
    for kind, text in r.blocks:
        if lines and lines[-1] != "":
            lines.append("")  # one blank line between paragraphs
        if kind == "hr":
            lines.append("-" * min(40, width))
            continue
        if kind in ("h3", "h4", "h5", "h6"):
            text = text.upper()
        wrapped = textwrap.wrap(text, width) or [""]
        lines.extend(wrapped)
        if kind in ("h1", "h2"):
            rule = "=" if kind == "h1" else "-"
            lines.append(rule * min(width, max(len(l) for l in wrapped)))
    # Collapse runs of blank lines: source markup often nests empty divs,
    # which would otherwise show up as pages of nothing.
    out = []
    for line in lines:
        if line or not (out and out[-1] == ""):
            out.append(line)
    return out

# --- Layer 3: documents -- a book reduced to chapters of words -------------

def _is_sent_end(word):
    # "done." ends a sentence; so does 'done."' -- strip closing quotes
    # and brackets before checking for terminal punctuation.
    return word.rstrip(_CLOSERS).endswith(_SENT_END)

def _tokenize(paras):
    """Paragraph strings -> [(word, sent_end, para_end), ...]."""
    words = []
    for para in paras:
        # Words made only of rule characters are heading underlines or
        # scene breaks leaking through as text: nothing to flash.
        toks = [t for t in para.split() if not set(t) <= _RULE_CHARS]
        words.extend((t, _is_sent_end(t), i == len(toks) - 1)
                     for i, t in enumerate(toks))
    return words

def _lines_to_paras(lines):
    """Hard-wrapped lines -> reflowed paragraphs, split on blank lines."""
    paras, cur = [], []
    for ln in lines:
        if ln.strip():
            cur.append(ln.strip())
        elif cur:
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    # A paragraph that is all rule characters (a bare "----" scene break)
    # carries no words; drop it rather than flashing punctuation.
    return [p for p in paras if not set(p) <= _RULE_CHARS | {" "}]

class Doc:
    """What the player needs: a title, chapters of tokenized words, a TOC."""
    def __init__(self, title, chapters, toc):
        self.title = title
        self.chapters = chapters  # [ [(word, sent_end, para_end), ...] ]
        self.toc = toc            # [(label, chapter_index)]

    def label_for(self, index):
        return next((l for l, i in self.toc if i == index),
                    f"Chapter {index + 1}")

def _load_text(path):
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise SokudokuError(f"{path}: {e.strerror or e}")
    blocks = re.split(r"\n[ \t]*\n", raw)  # blank line = paragraph break
    words = _tokenize(_lines_to_paras(blocks))
    if not words:
        raise SokudokuError(f"{path}: no prose to read")
    title = Path(path).stem
    return Doc(title, [words], [(title, 0)])

def load_doc(path):
    """EPUBs go through the vendored parser; non-zips are plain prose."""
    if not os.path.isfile(path):
        raise SokudokuError(f"{path}: no such file")
    # zipfile.is_zipfile is the truth, the extension only a hint: a .txt
    # that is really a zip still parses, a renamed .epub still reads.
    if not zipfile.is_zipfile(path):
        return _load_text(path)
    book = load_book(path)
    chapters = [_tokenize(_lines_to_paras(book.render(i, MAX_WIDTH)))
                for i in range(len(book.chapters))]
    if not any(chapters):
        raise SokudokuError(f"{path}: the book contains no prose")
    return Doc(book.title, chapters, book.toc)

# --- Layer 4: the stream -- chunking, navigation, timing -------------------

def _orp(word):
    # The "optimal recognition point": the letter the eye aims for, about
    # a third of the way in. The table is the one Spritz published.
    n = len(word)
    return 0 if n <= 1 else 1 if n <= 5 else 2 if n <= 9 else \
        3 if n <= 13 else 4

def _chunk(words, i, off, width):
    """Build one flash starting at word i, char offset off within it.

    Words are packed whole up to the character budget (a first word may
    exceed it by SPLIT_OVER -- one BIG word is fine); a word longer than
    that dash-breaks across flashes ("antidis-", "establishment"). Never
    crosses a sentence/paragraph boundary. Returns (pieces, j, o, eff):
    the display strings, the position of the next flash, and the
    effective word count shown (dash-broken words count fractionally, so
    the wpm timing stays honest)."""
    pieces, eff, j, o = [], 0.0, i, off
    while j < len(words):
        w = words[j][0]
        avail = len(w) - o
        used = sum(map(len, pieces)) + max(0, len(pieces) - 1)
        room = width - used - (1 if pieces else 0)  # the separator space
        if pieces and (words[j - 1][1] or words[j - 1][2]):
            break  # a flash that straddles "stop. Start" hides the pause
        if avail <= room or (not pieces and o == 0
                             and avail <= width + SPLIT_OVER):
            pieces.append(w[o:])
            eff += avail / len(w)
            j, o = j + 1, 0
            continue
        if not pieces:
            # Super-long word, empty flash: take width-1 chars + a dash.
            # avail > width here, so the word always continues; o > 0
            # means this flash is itself a continuation piece.
            take = width - 1
            pieces.append(w[o:o + take] + "-")
            eff += take / len(w)
            return pieces, j, o + take, eff
        break  # the next whole word does not fit: end the flash here
    return pieces, j, o, eff

def _sentence_start(words, i):
    k = max(0, min(i, len(words) - 1))
    while k > 0 and not words[k - 1][1]:
        k -= 1
    return k

def _next_sentence(words, i):
    k = i
    while k < len(words) and not words[k][1]:
        k += 1
    return min(k + 1, len(words) - 1)

def _delay(words, j, o, eff, wpm):
    # Boundary pauses only apply when the flash ended on a completed
    # word: a dash-broken continuation (o > 0) must not earn the pause
    # of a sentence it has not finished yet.
    per_word = 60.0 / wpm
    extra = 0.0
    if o == 0 and j > 0:
        extra = (SENT_PAUSE if words[j - 1][1] else 0.0) + \
                (PARA_PAUSE if words[j - 1][2] else 0.0)
    return per_word * (eff + extra)

# --- Layer 5: per-book state ------------------------------------------------

def _state_path(book_path):
    # Keyed by absolute path AND mtime: editing or replacing the file starts
    # fresh instead of landing mid-stream in a different book.
    # XDG_STATE_HOME is the spec-mandated home for per-run state like this.
    # sokudoku keeps its own state root, so reading a book here and in yomu
    # never clobbers either position.
    st = os.stat(book_path)
    key = hashlib.sha256(
        f"{os.path.abspath(book_path)}:{st.st_mtime_ns}".encode()
    ).hexdigest()[:32]
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "sokudoku" / f"{key}.json"

def _load_state(path):
    try:
        with open(_state_path(path)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}  # missing/corrupt state just means "start at the top"

def _save_state(path, state):
    p = _state_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:  # write-then-rename: never a half-written file
        json.dump(state, f)
    os.replace(tmp, p)

# --- Layer 6: the player (curses) -------------------------------------------

def _clamp(v, hi):
    # Both bounds matter: below the top of a short chapter a backwards jump
    # would drive the index negative, and words[-5:...] is an EMPTY slice --
    # a blank screen that then gets saved as the reading position.
    return max(0, min(v, hi))

def _put(win, y, x, s, attr):
    """addnstr with clipping, so a wide chunk on a narrow tty can't crash."""
    rows, cols = win.getmaxyx()
    if not 0 <= y < rows:
        return
    if x < 0:
        s, x = s[-x:], 0
    if not s or x >= cols - 1:
        return
    try:
        win.addnstr(y, x, s, cols - 1 - x, attr)
    except curses.error:
        pass

def _fmt_duration(minutes):
    m = int(round(minutes))
    return f"{m} min" if m < 90 else f"{m // 60} h {m % 60:02d} min"

def _book_progress(doc, ch, i):
    """Words read and words total, across the whole book (not the chapter)."""
    done = sum(len(c) for c in doc.chapters[:ch]) + i
    return done, sum(len(c) for c in doc.chapters)

def _overlay(stdscr, title, items):
    """A navigable list (the TOC). Returns (chapter, word) or None."""
    if not items:
        return None
    sel, off = 0, 0
    while True:
        rows, cols = stdscr.getmaxyx()
        sel = max(0, min(sel, len(items) - 1))
        off = min(max(off, sel - rows + 2), sel)  # keep selection visible
        stdscr.erase()
        stdscr.addnstr(0, 0, f" {title} ".ljust(cols), cols - 1,
                       curses.A_REVERSE)
        for row, (label, _c, _l) in enumerate(items[off:off + rows - 1], 1):
            attr = curses.A_REVERSE if off + row - 1 == sel else 0
            stdscr.addnstr(row, 0, f" {label}".ljust(cols), cols - 1, attr)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("q"), 27): return None
        elif key in (ord("j"), curses.KEY_DOWN): sel += 1
        elif key in (ord("k"), curses.KEY_UP): sel -= 1
        elif key in (curses.KEY_ENTER, 10, 13): return items[sel][1], items[sel][2]

def _draw(stdscr, doc, ch, words, i, off, pieces, j, o, wpm, width,
          paused, show_ctx, red):
    rows, cols = stdscr.getmaxyx()
    stdscr.erase()
    text = " ".join(pieces)
    # The pivot letter of the middle piece sits on the screen's center
    # column, Spritz-style: the eye's anchor never moves. For a
    # dash-broken piece the ORP is taken from the piece itself.
    focus = (len(pieces) - 1) // 2
    fp = pieces[focus].rstrip("-")
    pivot = sum(len(p) + 1 for p in pieces[:focus]) + \
        min(_orp(fp), len(fp) - 1)
    row, pivot_col = rows // 2, cols // 2
    x = pivot_col - pivot
    if show_ctx:
        left = " ".join(w for w, _, _ in words[max(0, i - CTX_WORDS):i])
        if off:  # mid dash-broken word: its earlier letters are context
            left = f"{left} {words[i][0][:off]}".strip()
        right = " ".join(w for w, _, _ in words[j:j + CTX_WORDS])
        if o:  # the current word continues past this flash
            right = f"{words[j][0][o:]} {right}".strip()
        _put(stdscr, row, x - len(left) - 1, left, curses.A_DIM)
        _put(stdscr, row, x + len(text) + 1, right, curses.A_DIM)
    _put(stdscr, row, x, text, curses.A_BOLD)
    _put(stdscr, row, x + pivot, text[pivot], red)
    _put(stdscr, row - 2, pivot_col, "v", curses.A_DIM)
    _put(stdscr, row + 2, pivot_col, "^", curses.A_DIM)
    if paused:
        _put(stdscr, 0, 2, "space: play   arrows: speed/sentences   "
                           "w: flash width   c: context   t: contents   "
                           "q: quit", curses.A_DIM)
        # Book-level progress appears on pause only: while the stream
        # runs it would be one more thing pulling at your attention.
        done, total = _book_progress(doc, ch, i)
        book_pct = int(100 * done / max(1, total))
        left = _fmt_duration((total - done) / wpm)
        status = (f" PAUSED | {doc.title} | {book_pct}% of book | "
                  f"~{left} left")
        # The bar itself is pause-only: a bright band across the bottom
        # is exactly the distraction the stream is meant to remove.
        _put(stdscr, rows - 1, 0, status.ljust(cols), curses.A_REVERSE)
    # The speed indicator is plain dim text -- no bar -- so it can stay
    # on screen while playing without shouting.
    speed = f"{wpm} wpm {width}ch "
    _put(stdscr, rows - 1, cols - len(speed), speed, curses.A_DIM)
    stdscr.refresh()

def _tui(stdscr, doc, path, wpm, width):
    curses.curs_set(0)
    red = curses.A_BOLD  # no colors? the pivot letter is at least bold
    if curses.has_colors():
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_RED, -1)
            red = curses.color_pair(1) | curses.A_BOLD
        except curses.error:
            pass
    state = _load_state(path)
    ch = _clamp(state.get("chapter", 0), len(doc.chapters) - 1)
    i, off = state.get("word", 0), 0  # off: char offset, mid dash-break
    paused, show_ctx = True, False  # start paused: aim before the stream
    # Context off by default: the dimmed side text distracts more than
    # it orients (toggle with c if you ever want it).

    while True:
        words = doc.chapters[ch]
        if not words:  # an empty spine item: flow past it
            if ch < len(doc.chapters) - 1:
                ch, i, off = ch + 1, 0, 0
                continue
            break
        i = _clamp(i, len(words) - 1)
        pieces, j, o, eff = _chunk(words, i, off, width)
        _draw(stdscr, doc, ch, words, i, off, pieces, j, o, wpm, width,
              paused, show_ctx, red)
        _save_state(path, {"chapter": ch, "word": i,
                           "wpm": wpm, "width": width})

        stdscr.timeout(-1 if paused else
                       max(20, int(_delay(words, j, o, eff, wpm) * 1000)))
        key = stdscr.getch()
        if key == -1:  # the timeout fired: show the next flash
            if j >= len(words):
                if ch < len(doc.chapters) - 1:
                    ch, i, off = ch + 1, 0, 0
                else:
                    paused = True  # end of the book: hold the last words
            else:
                i, off = j, o
            continue
        off = 0  # any manual move lands on a whole word
        if key in (ord("q"), 27):
            break
        elif key == ord(" "):
            paused = not paused
            if not paused:
                # Re-enter on a sentence boundary, not mid-thought: the
                # regression RSVP is famous for denying you, restored.
                i = _sentence_start(words, i)
        elif key in (curses.KEY_UP, ord("+"), ord("=")):
            wpm = min(MAX_WPM, wpm + WPM_STEP)
        elif key in (curses.KEY_DOWN, ord("-")):
            wpm = max(MIN_WPM, wpm - WPM_STEP)
        elif key == ord("w"):
            width = next((w for w in WIDTHS if w > width), WIDTHS[0])
        elif key in (curses.KEY_LEFT, ord("h")):
            s = _sentence_start(words, i)
            i = _sentence_start(words, s - 1) if s == i else s
        elif key in (curses.KEY_RIGHT, ord("l")):
            i = _next_sentence(words, i)
        elif key == ord("n") and ch < len(doc.chapters) - 1:
            ch, i = ch + 1, 0
        elif key == ord("p") and ch > 0:
            ch, i = ch - 1, 0
        elif key == ord("g"):
            i = 0
        elif key == ord("c"):
            show_ctx = not show_ctx
        elif key == ord("t"):
            jump = _overlay(stdscr, "Contents",
                            [(label, k, 0) for label, k in doc.toc])
            if jump:
                ch, i = jump[0], jump[1]
        elif key == curses.KEY_RESIZE:
            pass  # the loop redraws at the new size on its own
    _save_state(path, {"chapter": ch, "word": i, "wpm": wpm,
                       "width": width})

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="sokudoku",
        description="flash a book a few words at a time (EPUB or plain "
                    "text), so the text moves and your eyes do not")
    ap.add_argument("book", help="an .epub file or a plain text file")
    ap.add_argument("--wpm", type=int,
                    help=f"words per minute (default {DEFAULT_WPM}; "
                         "remembered per book)")
    ap.add_argument("--width", type=int,
                    help=f"flash width in characters (default "
                         f"{DEFAULT_WIDTH}; remembered per book)")
    args = ap.parse_args(argv)
    try:
        doc = load_doc(args.book)
        state = _load_state(args.book)
        wpm = args.wpm or state.get("wpm") or DEFAULT_WPM
        wpm = max(MIN_WPM, min(wpm, MAX_WPM))
        width = args.width or state.get("width") or DEFAULT_WIDTH
        width = max(MIN_WIDTH, min(width, MAX_WIDTH_FLASH))
        curses.wrapper(_tui, doc, args.book, wpm, width)
    except SokudokuError as e:
        print(f"sokudoku: {e}", file=sys.stderr)
        return 1
    except curses.error:
        print("sokudoku: could not start the terminal UI (is this a real "
              "tty?)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
