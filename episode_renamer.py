#!/usr/bin/env python3
"""
TV Episode Renamer (macOS / Tkinter)

Pick a folder of episode files, point the app at an epguides.com show page,
choose a season, and it renames the files (in natural alphabetical order) to:

    <season>x<episode> - <episode title>.<original extension>

e.g.  3x01 - Smells Like Mean Spirit.mkv      (season padding off)
      03x01 - Smells Like Mean Spirit.mkv     (season padding on)

The episode number is always two digits (01, 02, ... 12).

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


def guess_season(folder):
    """Guess a season number from a folder's name; 1 if none is found."""
    name = os.path.basename(os.path.normpath(folder))
    # "Season 3", "Series 3", "S03" ...
    m = re.search(r"(?i)(?:season|series|(?<![a-z])s)[\s._-]*(\d{1,2})(?![a-z\d])", name)
    if not m:  # otherwise the first standalone 1-2 digit number, e.g. "04 - Ted Lasso"
        m = re.search(r"(?<![A-Za-z\d])(\d{1,2})(?![A-Za-z\d])", name)
    if m and int(m.group(1)) >= 1:
        return int(m.group(1))
    return 1


def list_files(folder):
    names = [
        n
        for n in os.listdir(folder)
        if not n.startswith(".") and os.path.isfile(os.path.join(folder, n))
    ]
    return sorted(names, key=natural_key)


def build_plan(files, titles, season, pad_season):
    """Return a list of (old_name, new_name_or_None), one per file."""
    season_str = f"{season:02d}" if pad_season else str(season)
    plan = []
    for i, old in enumerate(files):
        if i < len(titles):
            ep, title = titles[i]
            ext = os.path.splitext(old)[1]
            new = f"{season_str}x{ep:02d} - {sanitize_title(title)}{ext}"
            plan.append((old, new))
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


CHECKED = "☑"
UNCHECKED = "☐"


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

        self.folder_var = tk.StringVar()
        self.url_var = tk.StringVar()
        self.season_var = tk.StringVar(value="1")
        self.pad_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Choose a folder and enter a URL to begin.")

        self._build()

        for var in (self.folder_var, self.season_var, self.pad_var):
            var.trace_add("write", lambda *_: self._invalidate_preview())
        self.folder_var.trace_add("write", self._on_folder_change)

    # ---- layout ----------------------------------------------------------

    def _build(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(4, weight=1)
        self.rowconfigure(5, weight=2)

        ttk.Label(self, text="Folder:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.folder_var).grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=6, pady=3
        )
        ttk.Button(self, text="Browse…", command=self.on_browse).grid(
            row=0, column=3, pady=3
        )

        ttk.Label(self, text="Episodes URL:").grid(row=1, column=0, sticky="w", pady=3)
        url_entry = ttk.Entry(self, textvariable=self.url_var)
        url_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=6, pady=3)
        url_entry.insert(0, "https://epguides.com/")

        ttk.Label(self, text="Season number:").grid(row=2, column=0, sticky="w", pady=3)
        opts = ttk.Frame(self)
        opts.grid(row=2, column=1, columnspan=3, sticky="ew", padx=6, pady=3)
        ttk.Spinbox(opts, from_=1, to=99, width=5, textvariable=self.season_var).pack(
            side="left"
        )
        ttk.Checkbutton(
            opts,
            text="Leading zero in season number (03x01 instead of 3x01)",
            variable=self.pad_var,
        ).pack(side="left", padx=14)

        self.fetch_btn = ttk.Button(self, text="Fetch titles", command=self.on_fetch)
        self.fetch_btn.grid(row=3, column=0, sticky="w", pady=(8, 4))

        # Titles list
        titles_box = ttk.LabelFrame(self, text="Episode titles", padding=6)
        titles_box.grid(row=4, column=0, columnspan=4, sticky="nsew", pady=4)
        self.titles_tree = self._make_tree(
            titles_box,
            [
                ("chk", "Use", 50, "center"),
                ("ep", "Episode", 80, "center"),
                ("title", "Title", 460, "w"),
            ],
        )
        self.titles_tree.bind("<Button-1>", self._on_titles_click)
        self.titles_tree.bind("<space>", self._on_titles_space)
        bar = ttk.Frame(titles_box)
        bar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(bar, text="Select all", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(bar, text="Select none", command=lambda: self._set_all(False)).pack(
            side="left", padx=6
        )
        ttk.Label(
            bar,
            text="Click a box (or press Space) to include or skip an episode.",
            foreground="gray40",
        ).pack(side="left", padx=8)

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
        self.preview_btn = ttk.Button(btns, text="Preview", command=self.on_preview)
        self.preview_btn.pack(side="left")
        self.rename_btn = ttk.Button(
            btns, text="Rename files", command=self.on_rename, state="disabled"
        )
        self.rename_btn.pack(side="left", padx=8)

        ttk.Label(self, textvariable=self.status_var, foreground="gray40").grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(4, 0)
        )

    @staticmethod
    def _make_tree(parent, columns):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            parent, columns=[c[0] for c in columns], show="headings", height=6
        )
        for cid, heading, width, anchor in columns:
            tree.heading(cid, text=heading)
            tree.column(cid, width=width, anchor=anchor, stretch=(cid not in ("ep", "chk")))
        sb = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        return tree

    # ---- helpers ---------------------------------------------------------

    def _get_season(self):
        try:
            season = int(self.season_var.get().strip())
            if season < 1:
                raise ValueError
            return season
        except ValueError:
            messagebox.showerror("Invalid season", "Please enter a season number (1 or higher).")
            return None

    def _invalidate_preview(self):
        self.plan = None
        self.rename_btn.config(state="disabled")
        self.preview_tree.delete(*self.preview_tree.get_children())

    def _on_folder_change(self, *_):
        """Pre-set the season from the folder name once it is a real folder."""
        folder = self.folder_var.get().strip()
        if folder and os.path.isdir(folder):
            self.season_var.set(str(guess_season(folder)))

    def _selected_titles(self):
        return [(ep, t) for ep, t in self.titles if self.checked.get(ep, True)]

    def _toggle_row(self, iid):
        ep = int(iid)
        self.checked[ep] = not self.checked.get(ep, True)
        self.titles_tree.set(iid, "chk", CHECKED if self.checked[ep] else UNCHECKED)
        self._invalidate_preview()
        self._update_titles_status()

    def _set_all(self, value):
        for ep, _ in self.titles:
            self.checked[ep] = value
            self.titles_tree.set(str(ep), "chk", CHECKED if value else UNCHECKED)
        self._invalidate_preview()
        self._update_titles_status()

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
        threading.Thread(
            target=self._fetch_worker, args=(url, season), daemon=True
        ).start()
        self.after(100, self._poll_fetch)

    def _fetch_worker(self, url, season):
        try:
            html = fetch_html(url)
            self.queue.put(("ok", season, parse_season_titles(html, season)))
        except HTTPError as exc:
            self.queue.put(("err", f"The server returned an error: HTTP {exc.code}."))
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
            self.queue.put(("err", msg))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("err", f"Unexpected error: {exc}"))

    def _poll_fetch(self):
        try:
            msg = self.queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_fetch)
            return

        self.fetch_btn.config(state="normal")
        if msg[0] == "err":
            self.status_var.set("Fetch failed.")
            messagebox.showerror("Fetch failed", msg[1])
            return

        _, season, titles = msg
        self.titles = titles
        self.checked = {ep: True for ep, _ in titles}
        self.fetched_season = season if titles else None
        self.titles_tree.delete(*self.titles_tree.get_children())
        for ep, title in titles:
            self.titles_tree.insert("", "end", iid=str(ep), values=(CHECKED, ep, title))
        self._invalidate_preview()

        if titles:
            self.status_var.set(f"Loaded {len(titles)} titles for season {season}.")
        else:
            self.status_var.set("No episodes found.")
            messagebox.showwarning(
                "No episodes found",
                f"Couldn't find any episodes for season {season} at that URL.",
            )

    def on_preview(self):
        if not self.titles:
            messagebox.showinfo("No titles", "Fetch the titles first.")
            return
        season = self._get_season()
        if season is None:
            return
        if season != self.fetched_season:
            messagebox.showinfo(
                "Season changed",
                f"The titles shown are for season {self.fetched_season}. "
                "Click \"Fetch titles\" again after changing the season.",
            )
            return

        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Invalid folder", "Please choose a valid folder.")
            return
        try:
            files = list_files(folder)
        except OSError as exc:
            messagebox.showerror("Cannot read folder", str(exc))
            return
        if not files:
            messagebox.showinfo("Empty folder", "No files were found in that folder.")
            return

        titles = self._selected_titles()
        if not titles:
            messagebox.showinfo("No titles selected", "Tick at least one episode title.")
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
        if len(files) > len(titles):
            notes.append(
                f"{len(files) - len(titles)} file(s) have no matching title and will be skipped"
            )
        elif len(titles) > len(files):
            notes.append(f"{len(titles) - len(files)} title(s) have no file")
        self.status_var.set(
            f"{len(files)} file(s), {len(changes)} to rename."
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
    root.title("TV Episode Renamer")
    root.geometry("820x720")
    root.minsize(640, 560)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
