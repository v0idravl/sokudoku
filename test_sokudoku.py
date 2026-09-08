#!/usr/bin/env python3
"""Tests for sokudoku: EPUB loading (the vendored parser), document
tokenizing, chunking, sentence navigation, timing, and state. The curses
player itself is not tested (no tty); everything it depends on is. The
synthetic EPUB builder is the same one yomu's suite uses."""

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

import sokudoku

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>The Test Book</dc:title>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch3" href="text/ch%203.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
    <itemref idref="ch3"/>
  </spine>
</package>
"""

NCX = """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>
    <navPoint id="n1" playOrder="1">
      <navLabel><text>One: The Start</text></navLabel>
      <content src="text/ch1.xhtml"/>
    </navPoint>
    <navPoint id="n2" playOrder="2">
      <navLabel><text>Two: The Middle</text></navLabel>
      <content src="text/ch2.xhtml#frag"/>
    </navPoint>
    <navPoint id="n3" playOrder="3">
      <navLabel><text>Three: The End</text></navLabel>
      <content src="text/ch%203.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
"""

CH1 = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>One</title>
<style>body { color: red; }</style>
<script>alert("evil");</script></head>
<body>
<h1>Chapter One</h1>
<p>Tom &amp; Jerry &mdash; a known paragraph with entities.</p>
<ul><li>first item</li><li>second item</li></ul>
</body>
</html>
"""

CH2 = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<h2>Second Heading</h2>
<p>Plain middle paragraph.</p>
<ol><li>step one</li><li>step two</li></ol>
</body>
</html>
"""

CH3 = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body><p>Final chapter lives in a file with a space in its name.</p></body>
</html>
"""

OPF_NAV = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Nav Book</dc:title>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>
"""

NAV_XHTML = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops">
<body>
<nav epub:type="toc"><ol><li><a href="ch1.xhtml">Nav Chapter</a></li></ol></nav>
</body>
</html>
"""

PROSE = """\
First paragraph, hard wrapped
across two lines like prose.

Second paragraph ends here.
A new sentence follows it.

==========
"""


def build_epub(path, nav=False):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        if nav:
            z.writestr("META-INF/container.xml", CONTAINER.replace(
                "OEBPS/content.opf", "OEBPS/nav.opf"))
            z.writestr("OEBPS/nav.opf", OPF_NAV)
            z.writestr("OEBPS/nav.xhtml", NAV_XHTML)
            z.writestr("OEBPS/ch1.xhtml", CH1)
        else:
            z.writestr("META-INF/container.xml", CONTAINER)
            z.writestr("OEBPS/content.opf", OPF)
            z.writestr("OEBPS/toc.ncx", NCX)
            z.writestr("OEBPS/text/ch1.xhtml", CH1)
            z.writestr("OEBPS/text/ch2.xhtml", CH2)
            z.writestr("OEBPS/text/ch 3.xhtml", CH3)


class SokudokuTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.epub = self.dir / "book.epub"
        build_epub(self.epub)
        self.txt = self.dir / "notes.txt"
        self.txt.write_text(PROSE, encoding="utf-8")

    # --- the vendored EPUB parser ---

    def test_toc_parsing_ncx(self):
        book = sokudoku.load_book(str(self.epub))
        self.assertEqual(book.title, "The Test Book")
        self.assertEqual(len(book.chapters), 3)
        self.assertEqual(book.toc, [
            ("One: The Start", 0),
            ("Two: The Middle", 1),   # fragment id ignored
            ("Three: The End", 2),    # percent-escaped href resolved
        ])

    def test_toc_parsing_nav_epub3(self):
        nav_epub = self.dir / "nav.epub"
        build_epub(nav_epub, nav=True)
        book = sokudoku.load_book(str(nav_epub))
        self.assertEqual(book.toc, [("Nav Chapter", 0)])

    def test_chapter_rendering(self):
        book = sokudoku.load_book(str(self.epub))
        text = "\n".join(book.render(0, 78))
        self.assertIn("Chapter One", text)          # h1 kept...
        self.assertIn("===========", text)          # ...and underlined
        self.assertIn("Tom & Jerry — a known paragraph", text)  # entities
        self.assertIn("- first item", text)
        self.assertNotIn('alert("evil")', text)     # script dropped
        self.assertNotIn("color: red", text)        # style dropped
        text2 = "\n".join(book.render(1, 78))
        self.assertIn("Second Heading", text2)
        self.assertIn("1. step one", text2)
        self.assertIn("2. step two", text2)

    def test_corrupt_epub(self):
        bad = self.dir / "bad.epub"
        bad.write_bytes(b"this is not a zip at all")
        with self.assertRaises(sokudoku.SokudokuError):
            sokudoku.load_book(str(bad))

    def test_missing_container(self):
        broken = self.dir / "broken.epub"
        with zipfile.ZipFile(broken, "w") as z:
            z.writestr("random.txt", "no container.xml here")
        with self.assertRaises(sokudoku.SokudokuError):
            sokudoku.load_doc(str(broken))

    # --- document loading ---

    def test_load_text_reflows_wrapped_prose(self):
        doc = sokudoku.load_doc(str(self.txt))
        self.assertEqual(doc.title, "notes")
        self.assertEqual(len(doc.chapters), 1)
        flat = [w for w, _, _ in doc.chapters[0]]
        self.assertIn("First", flat)
        self.assertIn("across", flat)  # hard wrap joined into one paragraph
        self.assertNotIn("==========", flat)  # rule-only paragraph dropped

    def test_load_epub(self):
        doc = sokudoku.load_doc(str(self.epub))
        self.assertEqual(doc.title, "The Test Book")
        self.assertEqual(len(doc.chapters), 3)
        flat = [w for w, _, _ in doc.chapters[0]]
        self.assertIn("Tom", flat)
        self.assertIn("Jerry", flat)            # entities decoded
        self.assertNotIn('alert("evil");', flat)  # script never surfaces
        # the h1 underline ("===========") must not flash as a "word"
        self.assertFalse(any(set(w) <= sokudoku._RULE_CHARS for w in flat))

    def test_extension_does_not_decide_format(self):
        renamed = self.dir / "actually.txt"
        renamed.write_bytes(self.epub.read_bytes())
        doc = sokudoku.load_doc(str(renamed))  # a zip named .txt still parses
        self.assertEqual(doc.title, "The Test Book")

    def test_empty_text_file_is_a_clean_error(self):
        empty = self.dir / "empty.txt"
        empty.write_text("", encoding="utf-8")
        with self.assertRaises(sokudoku.SokudokuError):
            sokudoku.load_doc(str(empty))
        with self.assertRaises(sokudoku.SokudokuError):
            sokudoku.load_doc(str(self.dir / "missing.txt"))

    # --- the stream ---

    def test_sentence_end_detection(self):
        self.assertTrue(sokudoku._is_sent_end("stop."))
        self.assertTrue(sokudoku._is_sent_end('stop."'))  # closing quote after
        self.assertTrue(sokudoku._is_sent_end("really?"))
        # abbreviations like "Mr." are accepted false positives: they add
        # a harmless extra pause, cheaper than a full abbreviation list
        self.assertTrue(sokudoku._is_sent_end("Mr."))
        self.assertFalse(sokudoku._is_sent_end("word"))

    def test_chunk_never_crosses_sentence_boundary(self):
        words = sokudoku._tokenize(["one two. three four five."])
        pieces, j, o, _ = sokudoku._chunk(words, 0, 0, 8)
        self.assertEqual(pieces, ["one", "two."])  # 8 chars, then stop.
        self.assertEqual((j, o), (2, 0))
        pieces, j, o, _ = sokudoku._chunk(words, j, o, 20)
        self.assertEqual(pieces, ["three", "four", "five."])
        self.assertEqual((j, o), (5, 0))

    def test_chunk_never_crosses_paragraph_boundary(self):
        words = sokudoku._tokenize(["para one", "para two"])
        pieces, j, o, _ = sokudoku._chunk(words, 0, 0, 20)
        self.assertEqual(pieces, ["para", "one"])
        self.assertEqual((j, o), (2, 0))

    def test_chunk_packs_by_characters(self):
        words = sokudoku._tokenize(["a bb ccc dddd eeeee"])
        pieces, j, o, eff = sokudoku._chunk(words, 0, 0, 8)
        self.assertEqual(pieces, ["a", "bb", "ccc"])  # 8 chars with spaces
        self.assertEqual((j, o), (3, 0))
        self.assertEqual(eff, 3.0)
        pieces, j, o, _ = sokudoku._chunk(words, 3, 0, 7)
        self.assertEqual(pieces, ["dddd"])  # " eeeee" would make 10 > 7
        self.assertEqual((j, o), (4, 0))

    def test_one_big_word_shows_whole(self):
        words = sokudoku._tokenize(["pneumonia ok"])
        pieces, j, o, eff = sokudoku._chunk(words, 0, 0, 8)
        self.assertEqual(pieces, ["pneumonia"])  # 9 <= 8 + SPLIT_OVER
        self.assertEqual((j, o), (1, 0))
        self.assertEqual(eff, 1.0)

    def test_super_long_word_dash_breaks(self):
        words = sokudoku._tokenize(["antidisestablishment rules"])
        pieces, j, o, eff = sokudoku._chunk(words, 0, 0, 8)
        self.assertEqual(pieces, ["antidis-"])
        self.assertEqual((j, o), (0, 7))       # same word, mid-way
        self.assertAlmostEqual(eff, 7 / 20)
        pieces, j, o, _ = sokudoku._chunk(words, j, o, 8)
        self.assertEqual(pieces, ["establi-"])
        self.assertEqual((j, o), (0, 14))
        pieces, j, o, _ = sokudoku._chunk(words, j, o, 8)
        self.assertEqual(pieces, ["shment"])   # last piece, room left...
        self.assertEqual((j, o), (1, 0))       # ...but "rules" won't fit
        pieces, j, o, _ = sokudoku._chunk(words, j, o, 8)
        self.assertEqual(pieces, ["rules"])
        self.assertEqual((j, o), (2, 0))

    def test_sentence_navigation(self):
        words = sokudoku._tokenize(["a b. c d. e f."])  # sentences at 0, 2, 4
        self.assertEqual(sokudoku._sentence_start(words, 3), 2)  # mid "c d."
        self.assertEqual(sokudoku._sentence_start(words, 2), 2)  # at boundary
        self.assertEqual(sokudoku._next_sentence(words, 2), 4)   # past "d."
        self.assertEqual(sokudoku._next_sentence(words, 4), 5)   # clamps

    def test_orp_table(self):
        self.assertEqual(sokudoku._orp("a"), 0)
        self.assertEqual(sokudoku._orp("word"), 1)
        self.assertEqual(sokudoku._orp("reading"), 2)
        self.assertEqual(sokudoku._orp("comprehension"), 3)
        self.assertEqual(sokudoku._orp("antidisestablishment"), 4)

    def test_delay_pauses_at_boundaries(self):
        words = sokudoku._tokenize(["mid sentence here. second one."])
        per_word = 60.0 / 300
        # mid-sentence flash ("mid sentence"): no extra pause
        self.assertAlmostEqual(sokudoku._delay(words, 2, 0, 2.0, 300),
                               2 * per_word)
        # sentence-final flash ("here."): one extra beat
        self.assertAlmostEqual(sokudoku._delay(words, 3, 0, 1.0, 300),
                               per_word * (1 + sokudoku.SENT_PAUSE))
        # "one." is sentence-final AND paragraph-final: both pauses
        self.assertAlmostEqual(sokudoku._delay(words, 5, 0, 2.0, 300),
                               per_word * (2 + sokudoku.SENT_PAUSE
                                           + sokudoku.PARA_PAUSE))
        # a dash-broken continuation (o > 0) earns no boundary pause
        self.assertAlmostEqual(sokudoku._delay(words, 3, 4, 0.5, 300),
                               0.5 * per_word)

    # --- state ---

    def test_state_round_trip_under_sokudoku_root(self):
        os.environ["XDG_STATE_HOME"] = str(self.dir / "state")
        self.addCleanup(os.environ.pop, "XDG_STATE_HOME")
        sokudoku._save_state(str(self.txt),
                             {"chapter": 0, "word": 12, "wpm": 350,
                              "width": 16})
        self.assertEqual(sokudoku._load_state(str(self.txt))["word"], 12)
        self.assertEqual(sokudoku._load_state(str(self.dir / "none.txt")), {})
        # state lives under the sokudoku dir, nowhere else
        p = sokudoku._state_path(str(self.txt))
        self.assertEqual(p.parent.name, "sokudoku")

    def test_state_key_changes_with_mtime(self):
        os.environ["XDG_STATE_HOME"] = str(self.dir / "state")
        self.addCleanup(os.environ.pop, "XDG_STATE_HOME", None)
        p1 = sokudoku._state_path(str(self.txt))
        os.utime(self.txt, (1_700_000_000, 1_800_000_000))
        p2 = sokudoku._state_path(str(self.txt))
        self.assertNotEqual(p1, p2)

    def test_fmt_duration(self):
        self.assertEqual(sokudoku._fmt_duration(0), "0 min")
        self.assertEqual(sokudoku._fmt_duration(42.4), "42 min")
        self.assertEqual(sokudoku._fmt_duration(89.6), "1 h 30 min")
        self.assertEqual(sokudoku._fmt_duration(150), "2 h 30 min")

    def test_book_progress_spans_chapters(self):
        doc = sokudoku.load_doc(str(self.epub))
        sizes = [len(c) for c in doc.chapters]
        done, total = sokudoku._book_progress(doc, 0, 0)
        self.assertEqual((done, total), (0, sum(sizes)))
        # mid second chapter: first chapter's words plus the offset
        done, _ = sokudoku._book_progress(doc, 1, 5)
        self.assertEqual(done, sizes[0] + 5)


if __name__ == "__main__":
    unittest.main()
