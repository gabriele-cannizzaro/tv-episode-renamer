#!/usr/bin/env python3
"""
TV Episode Renamer (macOS / Tkinter)

Pick a folder of episode files, point the app at an epguides.com show page,
choose a season, and it renames the files (in natural alphabetical order) to:

    <season>x<episode> - <episode title>.<original extension>

e.g.  3x01 - Smells Like Mean Spirit.mkv      (season padding off)
      03x01 - Smells Like Mean Spirit.mkv     (season padding on)

The episode number is always two digits (01, 02, ... 12).

When a folder is pasted or chosen, the series name is taken from the folder
above it (e.g. ".../Ted Lasso/04 - Ted Lasso"), turned into an epguides URL
(https://epguides.com/TedLasso/) and the titles are fetched automatically;
if that fails nothing happens.

The season number is pre-filled from the chosen folder's name (default 1),
and each fetched title has a checkbox: unchecked titles are left out, so the
files are matched to the remaining titles in order.

Requires only the Python standard library (Tkinter ships with Python on macOS).
"""

import os
import queue
import re
import ssl
import threading
import traceback
import unicodedata
import uuid
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# --------------------------------------------------------------------------
# Fetching & parsing
# --------------------------------------------------------------------------

CODE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")  # e.g. "3-1"
# Fallback for older plain-text (<pre>) epguides layouts:
#   " 23.   3-1   15 Mar 23   Smells Like Mean Spirit"
TEXT_LINE_RE = re.compile(
    r"(?<!\d)(\d+)-(\d+)\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4}\s+(.+?)\s*$"
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


class _TableParser(HTMLParser):
    """Collects table rows (list of cell texts) and the page's plain text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.text = []
        self._row = None
        self._cell = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)


def fetch_html(url):
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    req = Request(url, headers={"User-Agent": USER_AGENT})
    ctx = None
    try:  # use certifi's CA bundle if it happens to be installed
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    with urlopen(req, timeout=20, context=ctx) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def parse_season_titles(html, season):
    """Return a sorted list of (episode_number, title) for the given season."""
    parser = _TableParser()
    parser.feed(html)

    found = {}
    for row in parser.rows:
        for i, cell in enumerate(row):
            m = CODE_RE.match(cell)
            if not m:
                continue
            s, e = int(m.group(1)), int(m.group(2))
            # Columns are: episode #, season-episode code, air date, title
            title = row[i + 2] if len(row) > i + 2 and row[i + 2] else ""
            if not title:
                title = next((c for c in reversed(row[i + 1:]) if c), "")
            if s == season and title:
                found.setdefault(e, title)
            break

    if not found:  # fallback: plain-text layout
        for line in "".join(parser.text).splitlines():
            m = TEXT_LINE_RE.search(line)
            if m and int(m.group(1)) == season:
                found.setdefault(int(m.group(2)), m.group(3).strip())

    return sorted(found.items())


# --------------------------------------------------------------------------
# Renaming logic
# --------------------------------------------------------------------------


def sanitize_title(title):
    """Make a title safe to use in a macOS file name."""
    title = re.sub(r"\s*:\s*", " - ", title)  # ':' shows up as '/' in Finder
    title = title.replace("/", "-")
    title = re.sub(r"[\x00-\x1f\x7f]", "", title)
    title = " ".join(title.split())
    return title.strip(" .")


def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


# "Season 3", "Series 3", "S03" ...
_SEASON_KEYWORD_RE = re.compile(r"(?i)(?:season|series|(?<![a-z])s)[\s._-]*(\d{1,2})(?![a-z\d])")
# otherwise the first standalone 1-2 digit number, e.g. "04 - Ted Lasso"
_SEASON_NUMBER_RE = re.compile(r"(?<![A-Za-z\d])(\d{1,2})(?![A-Za-z\d])")


def _season_match(name):
    """The match whose group 1 is the season number inside a folder name."""
    return _SEASON_KEYWORD_RE.search(name) or _SEASON_NUMBER_RE.search(name)


def guess_season(folder):
    """Guess a season number from a folder's name; 1 if none is found."""
    name = os.path.basename(os.path.normpath(folder))
    m = _season_match(name)
    if m and int(m.group(1)) >= 1:
        return int(m.group(1))
    return 1


def next_season_folder(folder):
    """Add 1 to the season number in the last part of the path, keeping any
    zero padding: '.../04 - Ted Lasso' -> '.../05 - Ted Lasso'. None if the
    folder name has no season number."""
    head, sep, name = folder.strip().rstrip("/").rpartition("/")
    m = _season_match(name)
    if not m:
        return None
    digits = m.group(1)
    new_digits = str(int(digits) + 1).zfill(len(digits))
    return head + sep + name[: m.start(1)] + new_digits + name[m.end(1):]


def pascal_case(text):
    """'Ted Lasso' -> 'TedLasso'. Only each word's first letter is changed, so
    'NCIS Los Angeles' -> 'NCISLosAngeles'. Accents, apostrophes and other
    punctuation are dropped, since the result goes into a URL."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"['’`]", "", text)  # "Grey's" -> "Greys"
    words = re.split(r"[^A-Za-z0-9]+", text)
    return "".join(w[:1].upper() + w[1:] for w in words if w)


def series_slug(folder):
    """Series name for the epguides URL: the text between the last and the
    last-but-one "/" of the folder path, in Pascal case, without a leading
    "The". None if there is none.

        /Volumes/Hermes/TV Series/Ted Lasso/04 - Ted Lasso  ->  TedLasso
        /Volumes/Hermes/TV Series/The Office/Season 2        ->  Office
    """
    parts = folder.strip().rstrip("/").split("/")
    if len(parts) < 2:
        return None
    name = re.sub(r"(?i)^\s*the[\s._-]+(?=[A-Za-z0-9])", "", parts[-2])  # "The Office" -> "Office"
    return pascal_case(name) or None


def list_files(folder):
    names = [
        n
        for n in os.listdir(folder)
        if not n.startswith(".") and os.path.isfile(os.path.join(folder, n))
    ]
    return sorted(names, key=natural_key)


def group_files(files):
    """Group files that share a name apart from the extension (episode.mkv and
    episode.srt) so they count as one episode. Order follows the sorted list."""
    groups = {}
    for f in files:
        groups.setdefault(os.path.splitext(f)[0].lower(), []).append(f)
    return list(groups.values())


def build_plan(files, titles, season, pad_season):
    """Return a list of (old_name, new_name_or_None), one per file. Files with
    the same name but different extensions get the same new name."""
    season_str = f"{season:02d}" if pad_season else str(season)
    plan = []
    for i, group in enumerate(group_files(files)):
        for old in group:
            if i < len(titles):
                ep, title = titles[i]
                ext = os.path.splitext(old)[1]
                plan.append((old, f"{season_str}x{ep:02d} - {sanitize_title(title)}{ext}"))
            else:
                plan.append((old, None))
    return plan


def apply_renames(folder, renames):
    """
    Rename files safely. `renames` is a list of (old, new) with old != new.
    Returns None on success, or an error message (changes are rolled back).
    """
    existing = {n.lower() for n in os.listdir(folder)}
    sources = {o.lower() for o, _ in renames}
    targets = [n.lower() for _, n in renames]

    if len(set(targets)) != len(targets):
        return "Two files would end up with the same new name."
    for _, n in renames:
        if n.lower() in existing and n.lower() not in sources:
            return f'A file named "{n}" already exists in the folder.'

    # Phase 1: move everything to unique temp names (avoids swap collisions).
    items = []  # [tmp, old, new, state]
    try:
        for i, (old, new) in enumerate(renames):
            tmp = f".rename_tmp_{uuid.uuid4().hex}_{i}"
            os.rename(os.path.join(folder, old), os.path.join(folder, tmp))
            items.append([tmp, old, new, "tmp"])
        # Phase 2: temp names -> final names.
        for it in items:
            os.rename(os.path.join(folder, it[0]), os.path.join(folder, it[2]))
            it[3] = "final"
    except Exception as exc:  # roll back whatever was done
        for tmp, old, new, state in items:
            try:
                if state == "tmp":
                    os.rename(os.path.join(folder, tmp), os.path.join(folder, old))
                elif state == "final":
                    os.rename(os.path.join(folder, new), os.path.join(folder, old))
            except OSError:
                pass
        return f"{exc}\n\nAny changes made so far were rolled back."
    return None


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------


VERSION = "1.24"

AUTO_FETCH_DELAY_MS = 600  # wait for a pasted/typed folder path to settle
EPGUIDES_BASE = "https://epguides.com/"

CHECKED = "☑"
UNCHECKED = "☐"
MIXED = "▣"  # header box when only some titles are ticked


class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=12)
        self.master = master
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)

        self.titles = []  # [(ep, title)]
        self.checked = {}  # episode number -> included?
        self.fetched_season = None
        self.plan = None
        self.queue = queue.Queue()
        self._fetch_token = 0  # identifies the most recent fetch
        self._auto_after = None  # pending automatic fetch (after-callback id)
        self._pending_fetches = 0  # fetches started but not yet finished

        self.folder_var = tk.StringVar()
        self.url_var = tk.StringVar()
        self.season_var = tk.StringVar(value="1")
        self.pad_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Choose a folder and enter a URL to begin.")

        self._build()

        for var in (self.folder_var, self.season_var):
            var.trace_add("write", lambda *_: self._invalidate_preview())
        self.folder_var.trace_add("write", self._on_folder_change)
        self.pad_var.trace_add("write", self._on_pad_change)

    # ---- layout ----------------------------------------------------------

    def _build(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(3, weight=1)
        self.rowconfigure(5, weight=2)

        ttk.Label(self, text="Folder:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.folder_var).grid(
            row=0, column=1, sticky="ew", padx=6, pady=3
        )
        self.next_btn = ttk.Button(self, text="Next", command=self.on_next)
        self.next_btn.grid(row=0, column=2, sticky="ew", padx=(0, 4), pady=3)
        self._update_next_state()
        ttk.Button(self, text="Browse…", command=self.on_browse).grid(
            row=0, column=3, sticky="ew", pady=3
        )

        ttk.Label(self, text="Episodes URL:").grid(row=1, column=0, sticky="w", pady=3)
        # The URL and the season input share one cell, so the season input's right
        # edge lines up with the right edge of the folder input above.
        url_row = ttk.Frame(self)
        url_row.grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        url_row.columnconfigure(0, weight=1)
        url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        url_entry.grid(row=0, column=0, sticky="ew")
        url_entry.insert(0, "https://epguides.com/")
        ttk.Label(url_row, text="Season:").grid(row=0, column=1, padx=(12, 6))
        ttk.Spinbox(url_row, from_=1, to=99, width=4, textvariable=self.season_var).grid(
            row=0, column=2
        )

        # Fetch button: right of the season input, as wide as Next and Browse
        # together (it spans the two columns they sit in). While titles are being
        # fetched, a text takes its place. Both widgets always occupy this same
        # cell and only their stacking order changes, so nothing ever moves.
        self.fetching_label = ttk.Label(
            self, text="Attempting to fetch titles", foreground="gray40", anchor="center"
        )
        self.fetching_label.grid(row=1, column=2, columnspan=2, sticky="nsew", pady=3)
        self.fetch_btn = ttk.Button(self, text="Fetch titles", command=self.on_fetch)
        self.fetch_btn.grid(row=1, column=2, columnspan=2, sticky="ew", pady=3)
        # (the button is created last, so it starts on top of the text)

        # Titles list
        titles_box = ttk.LabelFrame(self, text="Episode titles", padding=6)
        titles_box.grid(row=3, column=0, columnspan=4, sticky="nsew", pady=(12, 4))
        self.titles_tree = self._make_tree(
            titles_box,
            [
                ("chk", UNCHECKED, 50, "center"),  # header is a master checkbox
                ("ep", "Episode", 80, "center"),
                ("title", "Title", 460, "w"),
            ],
        )
        self.titles_tree.bind("<Button-1>", self._on_titles_click)
        self.titles_tree.bind("<space>", self._on_titles_space)
        # The header box of the first column ticks or unticks every title.
        self.titles_tree.heading("chk", anchor="center", command=self._toggle_all)

        # Leading-zero option: between the titles list and the preview
        ttk.Checkbutton(
            self,
            text="Leading zero in season number (03x01 instead of 3x01)",
            variable=self.pad_var,
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=8)

        # Preview table
        preview_box = ttk.LabelFrame(self, text="Preview", padding=6)
        preview_box.grid(row=5, column=0, columnspan=4, sticky="nsew", pady=4)
        self.preview_tree = self._make_tree(
            preview_box,
            [("old", "Current name", 330, "w"), ("new", "New name", 330, "w")],
        )
        self.preview_tree.tag_configure("skip", foreground="gray50")

        btns = ttk.Frame(self)
        btns.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(6, 2))
        self.rename_btn = ttk.Button(
            btns, text="Rename files", command=self.on_rename, state="disabled"
        )
        self.rename_btn.pack(side="right")
        # Status text, where the Preview button used to be. width=1 + expand lets
        # it fill the free space and be clipped (rather than push the window) if long.
        ttk.Label(
            btns, textvariable=self.status_var, foreground="gray40", anchor="w", width=1
        ).pack(side="left", fill="x", expand=True)

    @staticmethod
    def _make_tree(parent, columns):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            parent, columns=[c[0] for c in columns], show="headings", height=6
        )
        for cid, heading, width, anchor in columns:
            tree.heading(cid, text=heading, anchor="w")
            tree.column(cid, width=width, anchor=anchor, stretch=(cid not in ("ep", "chk")))
        sb = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        return tree

    # ---- helpers ---------------------------------------------------------

    def _get_season(self, quiet=False):
        try:
            season = int(self.season_var.get().strip())
            if season < 1:
                raise ValueError
            return season
        except ValueError:
            if not quiet:
                messagebox.showerror(
                    "Invalid season", "Please enter a season number (1 or higher)."
                )
            return None

    def _show_fetching(self, fetching):
        """Show the "Attempting to fetch titles" text in place of the Fetch titles
        button, and back. Both sit in the same cell; only the stacking changes."""
        (self.fetching_label if fetching else self.fetch_btn).tkraise()

    def _invalidate_preview(self):
        self.plan = None
        self.rename_btn.config(state="disabled")
        self.preview_tree.delete(*self.preview_tree.get_children())

    def _on_folder_change(self, *_):
        """Once the folder is a real one: pre-set the season from its name, then
        (after a short pause) try to fetch the titles automatically."""
        if self._auto_after is not None:
            self.after_cancel(self._auto_after)
            self._auto_after = None
        folder = self.folder_var.get().strip()
        self._update_next_state()
        if folder and os.path.isdir(folder):
            self.season_var.set(str(guess_season(folder)))
            if self.titles:  # titles already loaded: preview them for this folder
                self._build_preview(quiet=True)
            self._auto_after = self.after(
                AUTO_FETCH_DELAY_MS, lambda: self._auto_fetch(folder)
            )

    def _auto_fetch(self, folder):
        """Guess the epguides URL from the folder path and fetch silently.

        Nothing is shown and nothing changes unless the fetch finds titles."""
        self._auto_after = None
        slug = series_slug(folder)
        season = self._get_season(quiet=True)
        if not slug or season is None:
            return
        self._start_fetch(f"{EPGUIDES_BASE}{slug}/", season, auto_folder=folder)

    def _selected_titles(self):
        return [(ep, t) for ep, t in self.titles if self.checked.get(ep, True)]

    def _toggle_row(self, iid):
        ep = int(iid)
        self.checked[ep] = not self.checked.get(ep, True)
        self.titles_tree.set(iid, "chk", CHECKED if self.checked[ep] else UNCHECKED)
        self._selection_changed()

    def _toggle_all(self):
        """Header box: untick everything if everything is ticked, else tick all."""
        if not self.titles:
            return
        self._set_all(not all(self.checked.get(ep, True) for ep, _ in self.titles))

    def _update_master_checkbox(self):
        """Header box of the first column: ☑ all ticked, ☐ none ticked, ▣ some."""
        states = [self.checked.get(ep, True) for ep, _ in self.titles]
        if states and all(states):
            symbol = CHECKED
        elif any(states):
            symbol = MIXED
        else:
            symbol = UNCHECKED
        self.titles_tree.heading("chk", text=symbol)

    def _set_all(self, value):
        for ep, _ in self.titles:
            self.checked[ep] = value
            self.titles_tree.set(str(ep), "chk", CHECKED if value else UNCHECKED)
        self._selection_changed()

    def _selection_changed(self):
        """Ticking or unticking titles refreshes the preview if one is showing."""
        had_preview = self.plan is not None
        self._update_master_checkbox()
        self._invalidate_preview()
        self._update_titles_status()
        if had_preview or (self.titles and os.path.isdir(self.folder_var.get().strip())):
            self._build_preview(quiet=True)

    def _on_pad_change(self, *_):
        """Changing the leading-zero option refreshes the preview if one is showing."""
        had_preview = self.plan is not None
        self._invalidate_preview()
        if had_preview:
            self._build_preview(quiet=True)

    def _update_titles_status(self):
        if self.titles:
            self.status_var.set(
                f"{len(self._selected_titles())} of {len(self.titles)} titles selected "
                f"for season {self.fetched_season}."
            )

    def _on_titles_click(self, event):
        tree = self.titles_tree
        if tree.identify_region(event.x, event.y) != "cell":
            return None
        if tree.identify_column(event.x) != "#1":
            return None
        iid = tree.identify_row(event.y)
        if iid:
            self._toggle_row(iid)
            return "break"
        return None

    def _on_titles_space(self, _event):
        for iid in self.titles_tree.selection():
            self._toggle_row(iid)
        return "break"

    # ---- actions ---------------------------------------------------------

    def on_browse(self):
        folder = filedialog.askdirectory(title="Choose the folder containing the episodes")
        if folder:
            self.folder_var.set(folder)

    def _update_next_state(self):
        """The Next button is only usable when the folder input has a value."""
        btn = getattr(self, "next_btn", None)
        if btn is not None:
            has_folder = bool(self.folder_var.get().strip())
            btn.state(["!disabled"] if has_folder else ["disabled"])

    def on_next(self):
        """Move to the next season's folder (season number in the path + 1)."""
        folder = self.folder_var.get().strip()
        if not folder:
            self.status_var.set("Choose a folder first.")
            return
        new = next_season_folder(folder)
        if new is None:
            self.status_var.set("Couldn't find a season number in the folder name.")
        elif not os.path.isdir(new):
            self.status_var.set(f"Next season's folder not found: {new}")
        else:
            self.folder_var.set(new)  # also pre-sets the season and fetches the titles

    def on_fetch(self):
        url = self.url_var.get().strip()
        if not url or url.rstrip("/") in ("https://epguides.com", "http://epguides.com"):
            messagebox.showerror(
                "Missing URL", "Please enter the epguides.com page for the show\n"
                "(e.g. https://epguides.com/TedLasso/)."
            )
            return
        season = self._get_season()
        if season is None:
            return

        self.fetch_btn.config(state="disabled")
        self.status_var.set("Fetching titles…")
        self._start_fetch(url, season)

    def _start_fetch(self, url, season, auto_folder=None):
        """Fetch in a background thread; the result is handled by _poll_fetch.
        `auto_folder` is set for automatic fetches (triggered by a folder)."""
        self._pending_fetches += 1
        self._show_fetching(True)
        self._fetch_token += 1
        meta = {"url": url, "token": self._fetch_token, "auto_folder": auto_folder}
        threading.Thread(
            target=self._fetch_worker, args=(url, season, meta), daemon=True
        ).start()
        self.after(100, self._poll_fetch)

    def _fetch_worker(self, url, season, meta):
        try:
            html = fetch_html(url)
            self.queue.put(("ok", meta, season, parse_season_titles(html, season)))
        except HTTPError as exc:
            self.queue.put(
                ("err", meta, f"The server returned an error: HTTP {exc.code}.")
            )
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                msg = (
                    "SSL certificate verification failed. If you installed Python from "
                    "python.org, run \"Install Certificates.command\" in "
                    "/Applications/Python 3.x/ and try again."
                )
            else:
                msg = f"Could not reach the site: {reason}"
            self.queue.put(("err", meta, msg))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("err", meta, f"Unexpected error: {exc}"))

    def _poll_fetch(self):
        try:
            msg = self.queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_fetch)
            return

        kind, meta = msg[0], msg[1]

        # This fetch is done (whatever the result): bring the button back once
        # no other fetch is still running.
        self._pending_fetches = max(0, self._pending_fetches - 1)
        if self._pending_fetches == 0:
            self._show_fetching(False)

        if meta["auto_folder"] is not None:
            # Automatic fetch: do nothing unless it found titles, and the
            # folder hasn't changed (and no newer fetch started) meanwhile.
            if (
                kind != "ok"
                or not msg[3]
                or meta["token"] != self._fetch_token
                or self.folder_var.get().strip() != meta["auto_folder"]
            ):
                return
            self.url_var.set(meta["url"])
            self._apply_titles(msg[2], msg[3])
            return

        self.fetch_btn.config(state="normal")
        if kind == "err":
            self.status_var.set("Fetch failed.")
            messagebox.showerror("Fetch failed", msg[2])
            return

        season, titles = msg[2], msg[3]
        self._apply_titles(season, titles)
        if not titles:
            self.status_var.set("No episodes found.")
            messagebox.showwarning(
                "No episodes found",
                f"Couldn't find any episodes for season {season} at that URL.",
            )

    def _apply_titles(self, season, titles):
        """Show freshly fetched titles (all ticked), then refresh the preview.

        Used after every successful fetch, automatic or via the button."""
        self.titles = titles
        self.checked = {ep: True for ep, _ in titles}
        self.fetched_season = season if titles else None
        self.titles_tree.delete(*self.titles_tree.get_children())
        for ep, title in titles:
            self.titles_tree.insert("", "end", iid=str(ep), values=(CHECKED, ep, title))
        self._update_master_checkbox()
        self._invalidate_preview()
        if titles:
            self.status_var.set(f"Loaded {len(titles)} titles for season {season}.")
            self._build_preview(quiet=True)

    def on_preview(self):
        self._build_preview()

    def _build_preview(self, quiet=False):
        """Fill the preview table.

        With quiet=True (automatic refresh after a fetch or a checkbox change)
        a problem clears the preview and is explained in the status line
        instead of in a dialog.
        """

        def problem(kind, title, message):
            self._invalidate_preview()
            if quiet:
                self.status_var.set(message)
            else:
                getattr(messagebox, kind)(title, message)

        if not self.titles:
            problem("showinfo", "No titles", "Fetch the titles first.")
            return
        season = self._get_season(quiet=quiet)
        if season is None:
            self._invalidate_preview()
            if quiet:
                self.status_var.set("Please enter a season number (1 or higher).")
            return
        if season != self.fetched_season:
            problem(
                "showinfo",
                "Season changed",
                f"The titles shown are for season {self.fetched_season}. "
                "Click \"Fetch titles\" again after changing the season.",
            )
            return

        folder = self.folder_var.get().strip()
        if quiet and not folder:  # no folder chosen yet: nothing to preview
            self._invalidate_preview()
            return
        if not folder or not os.path.isdir(folder):
            problem("showerror", "Invalid folder", "Please choose a valid folder.")
            return
        try:
            files = list_files(folder)
        except OSError as exc:
            problem("showerror", "Cannot read folder", str(exc))
            return
        if not files:
            problem("showinfo", "Empty folder", "No files were found in that folder.")
            return

        titles = self._selected_titles()
        if not titles:
            problem("showinfo", "No titles selected", "Tick at least one episode title.")
            return

        self.plan = build_plan(files, titles, season, self.pad_var.get())
        self.preview_tree.delete(*self.preview_tree.get_children())
        for old, new in self.plan:
            if new is None:
                self.preview_tree.insert(
                    "", "end", values=(old, "(no matching title – skipped)"), tags=("skip",)
                )
            elif new == old:  # already correctly named: nothing to do, grey it out
                self.preview_tree.insert("", "end", values=(old, new), tags=("skip",))
            else:
                self.preview_tree.insert("", "end", values=(old, new))

        changes = [(o, n) for o, n in self.plan if n and n != o]
        unchanged = sum(1 for o, n in self.plan if n == o)
        notes = []
        if unchanged:
            notes.append(f"{unchanged} already correctly named")
        n_eps = len(group_files(files))
        if n_eps > len(titles):
            notes.append(
                f"{n_eps - len(titles)} episode(s) have no matching title and will be skipped"
            )
        elif len(titles) > n_eps:
            notes.append(f"{len(titles) - n_eps} title(s) have no file")
        extra = f" in {n_eps} episode(s)" if n_eps != len(files) else ""
        self.status_var.set(
            f"{len(files)} file(s){extra}, {len(changes)} to rename."
            + (" " + "; ".join(notes) + "." if notes else "")
        )
        self.rename_btn.config(state="normal" if changes else "disabled")

    def on_rename(self):
        if not self.plan:
            return
        folder = self.folder_var.get().strip()
        changes = [(o, n) for o, n in self.plan if n and n != o]
        if not changes:
            return
        if not messagebox.askyesno(
            "Rename files",
            f"Rename {len(changes)} file(s) in:\n{folder}\n\nThis cannot be undone. Continue?",
        ):
            return

        error = apply_renames(folder, changes)
        if error:
            self.status_var.set("Rename failed.")
            messagebox.showerror("Rename failed", error)
            return

        self._invalidate_preview()
        self.status_var.set(f"Renamed {len(changes)} file(s).")
        messagebox.showinfo("Done", f"Renamed {len(changes)} file(s).")


def main():
    root = tk.Tk()
    root.title(f"TV Episode Renamer v{VERSION}")

    def report_error(exc_type, exc, tb):
        # Errors inside Tk callbacks are otherwise swallowed silently when the
        # app is launched by double-clicking, which just looks like "nothing
        # happened". Show them instead.
        traceback.print_exception(exc_type, exc, tb)
        messagebox.showerror(
            "Unexpected error", "".join(traceback.format_exception(exc_type, exc, tb))[-1500:]
        )

    root.report_callback_exception = report_error
    root.geometry("820x720")
    root.minsize(640, 560)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
