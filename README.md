```text
███████╗ ██████╗ ██╗  ██╗██╗   ██╗██████╗  ██████╗ ██╗  ██╗██╗   ██╗
██╔════╝██╔═══██╗██║ ██╔╝██║   ██║██╔══██╗██╔═══██╗██║ ██╔╝██║   ██║
███████╗██║   ██║█████╔╝ ██║   ██║██║  ██║██║   ██║█████╔╝ ██║   ██║
╚════██║██║   ██║██╔═██╗ ██║   ██║██║  ██║██║   ██║██╔═██╗ ██║   ██║
███████║╚██████╔╝██║  ██╗╚██████╔╝██████╔╝╚██████╔╝██║  ██╗╚██████╔╝
╚══════╝ ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝ ╚═════╝
  速読 · flashing-word speed reading · one file, stdlib only
```

![python](https://img.shields.io/badge/python-3-3776AB?logo=python&logoColor=white)
![deps](https://img.shields.io/badge/dependencies-none-44CC11)
![format](https://img.shields.io/badge/reads-EPUB%20%2B%20plain%20text-1BB91F)
![tests](https://img.shields.io/badge/tests-offline-89E051)

**sokudoku** (速読) is Japanese for "speed reading". Instead of showing a
page, it flashes the text a few words at a time at one fixed point on the
screen — rapid serial visual presentation — so the text moves and your
eyes do not. It reads EPUBs and plain text files (prose wrapped at any
width; blank lines separate paragraphs), and the whole program is one
auditable, standard-library-only Python file.

The design follows the reading research rather than the app marketing
(see `rsvp-reading.txt` for the full essay): the starting speed is a
gentle 100 wpm, meant to be ramped up with `↑`/`+` as your focus
settles — comprehension holds into the ~400–500 wpm band and decays
steeply past it; flashes never cross a sentence boundary and the stream
hesitates at sentence and paragraph ends; and regression — RSVP's known
weakness — is one keypress away, with rewind-to-sentence-start on every
resume.

---

## ⚡ 30-second demo

```bash
git clone https://github.com/v0idravl/sokudoku.git
cd sokudoku
./sokudoku.py book.epub        # EPUB
./sokudoku.py notes.txt        # plain prose
```

It starts paused; press `Space` to begin.

---

## ⌨️ Keys

| Key | Action |
|---|---|
| `Space` | pause / resume (resume rewinds to the sentence start) |
| `↑` / `↓`, `+` / `-` | faster / slower, 25 wpm per step |
| `w` | cycle flash width: 8 → 12 → 16 → 20 characters |
| `←` / `→` (`h` / `l`) | back / forward one sentence |
| `n` / `p` | next / previous chapter |
| `t` | table of contents overlay (EPUBs) |
| `c` | toggle the dimmed context words around the flash (off by default) |
| `g` | start of chapter |
| `q` | quit |

---

## 📖 How it reads

Each flash is sized by **characters, not word count**: whole words are
packed up to the width budget (spaces included), so short words group
naturally and long words stand alone. A single word may exceed the
budget — one BIG word is fine — but a *super*-long word (more than 4
characters over) dash-breaks across flashes: `antidis-`, `establishment`.
Dash-broken words count fractionally against the clock, so the wpm rate
stays honest.

The pivot letter of the middle word (the "optimal recognition point",
about a third of the way in) is pinned to the screen's center column and
shown in red. A dimmed speed indicator (`100 wpm 16ch`) — plain dim
text, no bar — sits at the right end of the bottom row at all times.
The status bar itself only appears while paused, showing book-level
progress — percentage of the whole book read and approximate time to
finish at the current speed; while the stream runs there is no bar at
all, so nothing pulls at your attention.

Position, speed, and flash width persist per book in
`~/.local/state/sokudoku/<hash>.json` (respecting `XDG_STATE_HOME`),
keyed by the book's absolute path and mtime, so a replaced file starts
fresh. The state root is sokudoku's own, so reading the same book here
and in [yomu](https://github.com/v0idravl/yomu) never clobbers either
position.

---

## 🔗 Relation to yomu

sokudoku is the flashing-word counterpart of
[yomu](https://github.com/v0idravl/yomu), the terminal EPUB pager. Its
EPUB parser (container walk, TOC detection, XHTML-to-plaintext) is
vendored verbatim from yomu so this repo stands alone: clone one file
and read. If you want a conventional page-at-a-time reader with
bookmarks, that's yomu.

---

## 🛠 Requirements

Python 3 and a terminal. Nothing else — no pip, no network, no
dependencies.

---

## 🧪 Tests

```bash
python3 test_sokudoku.py -v
```

The suite builds a tiny synthetic EPUB in a tempdir and exercises the
vendored parser (NCX and EPUB 3 nav TOCs, entity decoding, script/style
dropping), text reflow, flash chunking, sentence navigation, timing
pauses, and per-book state. The curses player itself is not driven by
the tests (no tty); everything beneath it is.
