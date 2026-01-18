"""
Point-and-click GUI to:
1) list symbols stored in the MySQL `news` database (per-symbol tables),
2) retrieve headlines for a selected symbol,
3) select a row and post headline/link to X.

Run:
    python scripts/news_headline_poster_gui.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, inspect

import tkinter as tk
from tkinter import messagebox
from tkinter import ttk

# Allow running as: `python scripts/news_headline_poster_gui.py`
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from api_keys import news_database
from x import Post_Constructor, post_scheduler, scheduled_post


NEWS_DB_URL = f"mysql+pymysql://root:{news_database}@127.0.0.1:3306/news"


@dataclass(frozen=True)
class HeadlineRow:
    title: str
    url: str
    date: datetime | None
    source: str | None = None
    category: str | None = None


def _safe_dt(v: Any) -> datetime | None:
    try:
        dt = pd.to_datetime(v, errors="coerce")
        if pd.isna(dt):
            return None
        # pandas Timestamp -> python datetime
        return dt.to_pydatetime()
    except Exception:
        return None


def list_news_symbols() -> list[str]:
    """
    Returns table names from the `news` DB that look like ticker tables.
    (We exclude known non-ticker tables.)
    """
    engine = create_engine(NEWS_DB_URL, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    tables = inspect(engine).get_table_names()

    excluded = {"cache_most_recent_link"}
    symbols = []
    for t in tables:
        tl = str(t).lower().strip()
        if tl in excluded:
            continue
        if tl.startswith("cache_") or tl.startswith("tmp_"):
            continue
        symbols.append(tl)

    return sorted(set(symbols))


def retrieve_symbol_headlines(symbol: str) -> pd.DataFrame:
    """
    Read a per-symbol table from the MySQL `news` database.

    Expected columns (from your `Controller._ensure_symbol_news_table`):
        Title, Source, Date, Url, Category, Ticker
    """
    sym = str(symbol).strip().lower()
    if not sym:
        raise ValueError("symbol must be non-empty")
    engine = create_engine(NEWS_DB_URL, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    # Use backticks to avoid issues with table names that collide with keywords.
    return pd.read_sql(f"SELECT * FROM `{sym}`", con=engine)


class NewsHeadlinePosterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("News Tracker — Post a Headline")
        self.root.geometry("1100x650")

        self.pc = Post_Constructor()

        self._updates_file_path = os.path.join(_PROJECT_ROOT, "most_recent_updates.txt")
        self._updates_file_last_mtime: float | None = None

        self._rows_by_iid: dict[str, HeadlineRow] = {}
        self._selected_symbol: str | None = None
        self._selected_row: HeadlineRow | None = None

        self._build_ui()
        self._load_symbols_async()
        self._schedule_updates_refresh()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        top = ttk.Frame(self.root, padding=10)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(6, weight=1)

        ttk.Label(top, text="Symbol").grid(row=0, column=0, sticky="w")
        self.symbol_var = tk.StringVar(value="")
        self.symbol_combo = ttk.Combobox(top, textvariable=self.symbol_var, width=18)
        self.symbol_combo.grid(row=0, column=1, sticky="w", padx=(6, 12))
        self.symbol_combo.bind("<<ComboboxSelected>>", lambda _e: self.load_headlines_clicked())
        self.symbol_combo.bind("<Return>", lambda _e: self.load_headlines_clicked())

        ttk.Label(top, text="Since (YYYY-MM-DD)").grid(row=0, column=2, sticky="w")
        self.since_var = tk.StringVar(value=(datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d"))
        self.since_entry = ttk.Entry(top, textvariable=self.since_var, width=14)
        self.since_entry.grid(row=0, column=3, sticky="w", padx=(6, 12))
        self.since_entry.bind("<Return>", lambda _e: self.load_headlines_clicked())

        self.refresh_symbols_btn = ttk.Button(top, text="Refresh symbols", command=self._load_symbols_async)
        self.refresh_symbols_btn.grid(row=0, column=4, sticky="w", padx=(0, 8))

        self.load_btn = ttk.Button(top, text="Load headlines", command=self.load_headlines_clicked)
        self.load_btn.grid(row=0, column=5, sticky="w")

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(top, textvariable=self.status_var).grid(row=0, column=6, sticky="e")

        main = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        main.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        left = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=3)
        main.add(right, weight=2)

        # Left: table
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)

        columns = ("Date", "Title", "Source", "Category", "Url")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        self.tree.grid(row=0, column=0, sticky="nsew")

        self.tree.heading("Date", text="Date")
        self.tree.heading("Title", text="Title")
        self.tree.heading("Source", text="Source")
        self.tree.heading("Category", text="Category")
        self.tree.heading("Url", text="Url")

        self.tree.column("Date", width=150, anchor="w", stretch=False)
        self.tree.column("Title", width=520, anchor="w", stretch=True)
        self.tree.column("Source", width=140, anchor="w", stretch=False)
        self.tree.column("Category", width=120, anchor="w", stretch=False)
        self.tree.column("Url", width=220, anchor="w", stretch=True)

        yscroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=yscroll.set)

        self.tree.bind("<<TreeviewSelect>>", self._on_row_selected)
        self.tree.bind("<Double-1>", lambda _e: self.open_link_clicked())

        # Right: preview + actions
        right.columnconfigure(0, weight=1)
        # Keep preview/buttons tight; put extra vertical space above the updates box.
        right.rowconfigure(0, weight=0)
        right.rowconfigure(1, weight=0)
        right.rowconfigure(2, weight=1)  # spacer
        right.rowconfigure(3, weight=0)  # updates

        preview = ttk.LabelFrame(right, text="Selection / Post Preview", padding=10)
        preview.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        preview.columnconfigure(0, weight=1)

        # Editable tweet text (user can modify before posting)
        self.title_text = tk.Text(preview, height=8, wrap="word")
        self.title_text.grid(row=0, column=0, sticky="nsew")
        self.title_text.bind("<KeyRelease>", lambda _e: self._update_length_indicator())

        self.link_var = tk.StringVar(value="")
        ttk.Entry(preview, textvariable=self.link_var).grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.preview_var = tk.StringVar(value="")
        ttk.Label(preview, textvariable=self.preview_var, foreground="#444").grid(row=2, column=0, sticky="w", pady=(8, 0))

        btns = ttk.Frame(right)
        btns.grid(row=1, column=0, sticky="ew")
        btns.columnconfigure(4, weight=1)

        self.open_link_btn = ttk.Button(btns, text="Open link", command=self.open_link_clicked)
        self.open_link_btn.grid(row=0, column=0, sticky="w")

        self.post_btn = ttk.Button(btns, text="Post to X", command=self.post_clicked)
        self.post_btn.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.schedule_btn = ttk.Button(btns, text="Schedule Post", command=self.schedule_clicked)
        self.schedule_btn.grid(row=0, column=2, sticky="w", padx=(8, 0))

        self.view_scheduled_btn = ttk.Button(btns, text="View scheduled", command=self.view_scheduled_clicked)
        self.view_scheduled_btn.grid(row=0, column=3, sticky="w", padx=(8, 0))

        self.copy_btn = ttk.Button(btns, text="Copy tweet text", command=self.copy_tweet_text_clicked)
        self.copy_btn.grid(row=0, column=5, sticky="e")

        ttk.Frame(right).grid(row=2, column=0, sticky="nsew")

        updates = ttk.LabelFrame(right, text="Most recent updates", padding=10)
        updates.grid(row=3, column=0, sticky="sew", pady=(10, 0))
        updates.columnconfigure(0, weight=1)

        self.updates_text = tk.Text(
            updates,
            height=8,
            wrap="none",
            cursor="arrow",  # keep pointer cursor (not I-beam)
            takefocus=0,  # avoid tab-focus into a read-only log
        )
        self.updates_text.grid(row=0, column=0, sticky="nsew")
        self.updates_text.configure(state="disabled")
        self.updates_text.bind("<Button-1>", self._on_updates_text_click)

        updates_scroll = ttk.Scrollbar(updates, orient=tk.VERTICAL, command=self.updates_text.yview)
        updates_scroll.grid(row=0, column=1, sticky="ns")
        self.updates_text.configure(yscrollcommand=updates_scroll.set)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)
        self.root.update_idletasks()

    def _read_updates_file_reversed(self) -> str:
        try:
            if not os.path.exists(self._updates_file_path):
                return f"(File not found)\n{self._updates_file_path}"

            # Only refresh if the file changed (best-effort).
            try:
                mtime = os.path.getmtime(self._updates_file_path)
            except OSError:
                mtime = None
            if mtime is not None and self._updates_file_last_mtime == mtime:
                return ""
            self._updates_file_last_mtime = mtime

            with open(self._updates_file_path, "r", encoding="utf-8") as f:
                lines = [ln.rstrip("\n") for ln in f.readlines()]
            # Reverse order (newest at top).
            lines = list(reversed([ln for ln in lines if ln.strip() != ""]))
            return "\n".join(lines) if lines else "(No updates yet)"
        except Exception as e:
            return f"(Could not read updates)\n{e}"

    def _refresh_updates_box(self) -> None:
        text = self._read_updates_file_reversed()
        # Empty string means "no change" (mtime unchanged).
        if text == "":
            return
        # Preserve user scroll position while updating.
        y0, _y1 = self.updates_text.yview()
        x0, _x1 = self.updates_text.xview()
        self.updates_text.configure(state="normal")
        self.updates_text.delete("1.0", "end")
        self.updates_text.insert("1.0", text)
        self.updates_text.yview_moveto(y0)
        self.updates_text.xview_moveto(x0)
        self.updates_text.configure(state="disabled")

    def _schedule_updates_refresh(self) -> None:
        self._refresh_updates_box()
        # Keep it current without user interaction.
        self.root.after(3000, self._schedule_updates_refresh)

    def _symbol_from_updates_click(self, event: tk.Event) -> str | None:
        """
        Attempt to extract a ticker symbol from the updates box where the user clicked.
        Supports common formats like:
          - AAPL ...
          - $AAPL ...
          - BRK.B ...
          - RDS-A ...
        """
        try:
            idx = self.updates_text.index(f"@{event.x},{event.y}")
            line = self.updates_text.get(f"{idx} linestart", f"{idx} lineend")
            # idx looks like "line.column"
            col = int(str(idx).split(".", 1)[1])
        except Exception:
            return None

        if not line or col < 0:
            return None

        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-^$")
        col = min(col, max(len(line) - 1, 0))

        # Expand left/right around the click to capture a full token.
        left = col
        while left > 0 and line[left - 1] in allowed:
            left -= 1
        right = col
        while right < len(line) and line[right] in allowed:
            right += 1

        token = line[left:right].strip()
        token = token.lstrip("^$").strip()
        if not token:
            return None

        sym = token.upper()

        # Keep validation permissive but avoid accidental words.
        # - 1-6 leading letters
        # - optional suffix like .B / -A / .WS / -P etc.
        if not re.fullmatch(r"[A-Z]{1,6}([.-][A-Z0-9]{1,5})?", sym):
            return None
        return sym

    def _on_updates_text_click(self, event: tk.Event) -> None:
        sym = self._symbol_from_updates_click(event)
        if not sym:
            return

        # Load as-if the user typed it into the Symbol box and pressed Enter.
        self.symbol_var.set(sym)
        self.load_headlines_clicked()

    def _load_symbols_async(self) -> None:
        self.refresh_symbols_btn.configure(state="disabled")
        self._set_status("Loading symbols from DB…")

        def worker() -> None:
            try:
                symbols = list_news_symbols()
            except Exception as e:
                self.root.after(0, lambda: self._on_symbols_loaded_error(e))
                return
            self.root.after(0, lambda: self._on_symbols_loaded(symbols))

        threading.Thread(target=worker, daemon=True).start()

    def _on_symbols_loaded(self, symbols: list[str]) -> None:
        self.symbol_combo["values"] = [s.upper() for s in symbols]
        self.refresh_symbols_btn.configure(state="normal")
        self._set_status(f"Loaded {len(symbols)} symbols.")

        # Auto-select first if empty
        if not self.symbol_var.get() and symbols:
            self.symbol_var.set(symbols[0].upper())

    def _on_symbols_loaded_error(self, e: Exception) -> None:
        self.refresh_symbols_btn.configure(state="normal")
        self._set_status("Failed to load symbols.")
        messagebox.showerror("DB error", f"Could not list symbols from MySQL.\n\n{e}")

    def _parse_since_date(self) -> datetime | None:
        raw = (self.since_var.get() or "").strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Since date must be formatted as YYYY-MM-DD")

    def load_headlines_clicked(self) -> None:
        symbol = (self.symbol_var.get() or "").strip().lower()
        if not symbol:
            messagebox.showwarning("Missing symbol", "Please choose a symbol.")
            return

        try:
            since_dt = self._parse_since_date()
        except Exception as e:
            messagebox.showwarning("Invalid date", str(e))
            return

        self._selected_symbol = symbol
        self._selected_row = None
        self._rows_by_iid.clear()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._clear_preview()

        self.load_btn.configure(state="disabled")
        self._set_status(f"Loading headlines for {symbol.upper()}…")

        def worker() -> None:
            try:
                df = retrieve_symbol_headlines(symbol)
                if "Date" in df.columns:
                    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
                if since_dt is not None and "Date" in df.columns:
                    df = df.loc[df["Date"] >= since_dt]
                if "Date" in df.columns:
                    df = df.sort_values("Date", ascending=False)
                df = df.head(500)  # keep UI responsive
            except Exception as e:
                self.root.after(0, lambda: self._on_headlines_loaded_error(e))
                return
            self.root.after(0, lambda: self._on_headlines_loaded(df))

        threading.Thread(target=worker, daemon=True).start()

    def _on_headlines_loaded(self, df: pd.DataFrame) -> None:
        self.load_btn.configure(state="normal")

        # Normalize expected columns
        for col in ("Title", "Url"):
            if col not in df.columns:
                self._set_status("Unexpected DB schema for this table.")
                messagebox.showerror(
                    "Schema error",
                    f"Expected column '{col}' in the table for {self._selected_symbol or ''}, but it was not found.",
                )
                return

        count = 0
        for _, r in df.iterrows():
            row = HeadlineRow(
                title=str(r.get("Title", "") or "").strip(),
                url=str(r.get("Url", "") or "").strip(),
                date=_safe_dt(r.get("Date")),
                source=(str(r.get("Source")) if "Source" in df.columns and pd.notna(r.get("Source")) else None),
                category=(str(r.get("Category")) if "Category" in df.columns and pd.notna(r.get("Category")) else None),
            )
            if not row.title or not row.url:
                continue
            date_str = row.date.strftime("%Y-%m-%d %H:%M") if row.date else ""
            iid = self.tree.insert(
                "",
                "end",
                values=(
                    date_str,
                    row.title,
                    row.source or "",
                    row.category or "",
                    row.url,
                ),
            )
            self._rows_by_iid[str(iid)] = row
            count += 1

        self._set_status(f"Loaded {count} headlines for {(self._selected_symbol or '').upper()}.")

    def _on_headlines_loaded_error(self, e: Exception) -> None:
        self.load_btn.configure(state="normal")
        self._set_status("Failed to load headlines.")
        messagebox.showerror("DB error", f"Could not load headlines.\n\n{e}")

    def _clear_preview(self) -> None:
        self.title_text.delete("1.0", "end")
        self.link_var.set("")
        self._update_length_indicator()

    def _on_row_selected(self, _event: Any) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        iid = str(sel[0])
        row = self._rows_by_iid.get(iid)
        if not row:
            return
        self._selected_row = row
        self._update_preview()

    def _tweet_text_for_selection(self) -> str:
        if not self._selected_row or not self._selected_symbol:
            return ""
        symbol = self._selected_symbol.upper()
        # Match your existing behavior in Post_Constructor.construct_post()
        return f"${symbol}\n {self._selected_row.title}"

    def _current_tweet_text(self) -> str:
        # Preserve user edits; strip only trailing whitespace/newlines added by Text widget.
        return self.title_text.get("1.0", "end").rstrip()

    def _update_length_indicator(self) -> None:
        tweet_text = self._current_tweet_text()
        n = len(tweet_text)
        if n == 0:
            self.preview_var.set("")
        elif n > 280:
            self.preview_var.set(f"Tweet length: {n}/280 (too long — will fail)")
        else:
            self.preview_var.set(f"Tweet length: {n}/280 (link will be posted as a reply)")

    def _update_preview(self) -> None:
        if not self._selected_row:
            self._clear_preview()
            return

        tweet_text = self._tweet_text_for_selection()
        self.title_text.delete("1.0", "end")
        self.title_text.insert("1.0", tweet_text)
        self.link_var.set(self._selected_row.url)
        self._update_length_indicator()

    def open_link_clicked(self) -> None:
        url = (self.link_var.get() or "").strip()
        if not url:
            messagebox.showinfo("No link", "No link to open. Select a row or paste a link.")
            return
        webbrowser.open(url)

    def copy_tweet_text_clicked(self) -> None:
        text = self._current_tweet_text()
        if not text:
            messagebox.showinfo("No text", "Enter or select a headline first.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status("Copied tweet text to clipboard.")

    def post_clicked(self) -> None:
        if not self._selected_symbol:
            messagebox.showinfo("No symbol", "Select a symbol first.")
            return

        tweet_text = self._current_tweet_text()
        if not tweet_text.strip():
            messagebox.showinfo("No text", "Enter or select a headline first.")
            return
        if len(tweet_text) > 280:
            messagebox.showerror(
                "Tweet too long",
                f"This headline is too long to post with the symbol prefix.\n\nLength: {len(tweet_text)}/280",
            )
            return
        link = (self.link_var.get() or "").strip() or None

        if not messagebox.askyesno(
            "Confirm post",
            "This will post the selected headline to X and then reply with the link.\n\nContinue?",
        ):
            return

        self.post_btn.configure(state="disabled")
        self._set_status("Posting to X…")

        symbol = self._selected_symbol.upper()

        def worker() -> None:
            try:
                # If user edited the text, we should post exactly what they see.
                # `construct_post` will prepend `$symbol` again, so we call `x_post` directly.
                post = self.pc.x_post(text=tweet_text)
                if link:
                    self.pc.x_post(text=link, reply_to_tweet_id=post["tweet_id"])
                ok = True
            except Exception as e:
                self.root.after(0, lambda: self._on_post_error(e))
                return
            self.root.after(0, lambda: self._on_post_done(ok))

        threading.Thread(target=worker, daemon=True).start()

    def _on_post_done(self, ok: Any) -> None:
        self.post_btn.configure(state="normal")
        self._set_status("Posted successfully." if ok else "Post completed.")
        messagebox.showinfo("Done", "Post sent to X.")

    def _on_post_error(self, e: Exception) -> None:
        self.post_btn.configure(state="normal")
        self._set_status("Post failed.")
        messagebox.showerror("Post failed", str(e))

    def schedule_clicked(self) -> None:
        if not self._selected_symbol:
            messagebox.showinfo("No symbol", "Select a symbol first.")
            return

        tweet_text = self._current_tweet_text()
        if not tweet_text.strip():
            messagebox.showinfo("No text", "Enter or select a headline first.")
            return
        if len(tweet_text) > 280:
            messagebox.showerror(
                "Tweet too long",
                f"This headline is too long to schedule.\n\nLength: {len(tweet_text)}/280",
            )
            return

        self._open_schedule_popup(tweet_text=tweet_text, link=(self.link_var.get() or "").strip() or None)

    def _open_schedule_popup(self, tweet_text: str, link: str | None) -> None:
        win = tk.Toplevel(self.root)
        win.title("Schedule Post")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        container = ttk.Frame(win, padding=12)
        container.grid(row=0, column=0, sticky="nsew")

        now = datetime.now()
        default_dt = (now + timedelta(minutes=10)).replace(second=0, microsecond=0)

        ttk.Label(container, text="Date (YYYY-MM-DD)").grid(row=0, column=0, sticky="w")
        date_var = tk.StringVar(value=default_dt.strftime("%Y-%m-%d"))
        date_entry = ttk.Entry(container, textvariable=date_var, width=16)
        date_entry.grid(row=0, column=1, sticky="w", padx=(8, 0))

        ttk.Label(container, text="Time (HH:MM, 24h)").grid(row=1, column=0, sticky="w", pady=(8, 0))
        time_var = tk.StringVar(value=default_dt.strftime("%H:%M"))
        time_entry = ttk.Entry(container, textvariable=time_var, width=16)
        time_entry.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        hint = ttk.Label(container, text="This will enqueue the post and create a Windows scheduled task.", foreground="#444")
        hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        buttons = ttk.Frame(container)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))

        def parse_dt() -> datetime | None:
            raw_date = (date_var.get() or "").strip()
            raw_time = (time_var.get() or "").strip()
            try:
                d = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                messagebox.showwarning("Invalid date", "Date must be formatted as YYYY-MM-DD.", parent=win)
                return None
            try:
                t = datetime.strptime(raw_time, "%H:%M").time()
            except ValueError:
                messagebox.showwarning("Invalid time", "Time must be formatted as HH:MM (24-hour).", parent=win)
                return None
            dt = datetime(d.year, d.month, d.day, t.hour, t.minute, 0)
            if dt < datetime.now():
                messagebox.showwarning("Invalid time", "Scheduled time must be in the future.", parent=win)
                return None
            return dt

        def on_ok() -> None:
            scheduled_time = parse_dt()
            if scheduled_time is None:
                return

            if not messagebox.askyesno(
                "Confirm schedule",
                f"Schedule this post for {scheduled_time.strftime('%Y-%m-%d %H:%M')}?",
                parent=win,
            ):
                return

            self.schedule_btn.configure(state="disabled")
            self._set_status("Scheduling post…")

            def worker() -> None:
                try:
                    # Ensure we write/read the queue from the project root location.
                    prev_cwd = os.getcwd()
                    try:
                        os.chdir(_PROJECT_ROOT)
                        post = scheduled_post(
                            headline=tweet_text,
                            scheduled_time=scheduled_time,
                            link=link,
                        )
                        ps = post_scheduler()
                        ps.enqueue_post(post)
                        ps.task_scheduler()
                    finally:
                        os.chdir(prev_cwd)
                except Exception as e:
                    self.root.after(0, lambda: self._on_schedule_error(e))
                    return
                self.root.after(0, lambda: self._on_schedule_done(scheduled_time))

            threading.Thread(target=worker, daemon=True).start()
            win.destroy()

        def on_cancel() -> None:
            win.destroy()

        ttk.Button(buttons, text="Cancel", command=on_cancel).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="OK", command=on_ok).grid(row=0, column=1)

        date_entry.focus_set()
        win.bind("<Return>", lambda _e: on_ok())
        win.bind("<Escape>", lambda _e: on_cancel())

    def _on_schedule_done(self, scheduled_time: datetime) -> None:
        self.schedule_btn.configure(state="normal")
        self._set_status("Scheduled successfully.")
        messagebox.showinfo("Scheduled", f"Post scheduled for {scheduled_time.strftime('%Y-%m-%d %H:%M')}.")

    def _on_schedule_error(self, e: Exception) -> None:
        self.schedule_btn.configure(state="normal")
        self._set_status("Scheduling failed.")
        messagebox.showerror("Scheduling failed", str(e))

    def view_scheduled_clicked(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Scheduled posts")
        win.geometry("980x420")
        win.transient(self.root)

        container = ttk.Frame(win, padding=10)
        container.grid(row=0, column=0, sticky="nsew")
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(1, weight=1)
        container.rowconfigure(2, weight=0)

        header = ttk.Frame(container)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)

        count_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=count_var, foreground="#444").grid(row=0, column=0, sticky="w")

        # Map visible rows back to the underlying scheduled post dict.
        posts_by_iid: dict[str, dict[str, Any]] = {}

        def load_posts() -> None:
            try:
                prev_cwd = os.getcwd()
                try:
                    os.chdir(_PROJECT_ROOT)
                    ps = post_scheduler()
                    posts = list(ps.scheduled_posts)
                finally:
                    os.chdir(prev_cwd)
            except Exception as e:
                messagebox.showerror("Could not load scheduled posts", str(e), parent=win)
                posts = []

            def display_one_line(v: Any) -> str:
                # IMPORTANT: display-only cleanup; do not modify stored post content.
                s = "" if v is None else str(v)
                # Normalize Windows newlines and remove embedded newlines that break table layout.
                s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
                return s

            def parse_time(v: Any) -> datetime | None:
                if isinstance(v, datetime):
                    return v
                if not v:
                    return None
                s = str(v).strip()
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        return datetime.strptime(s, fmt)
                    except ValueError:
                        continue
                try:
                    return datetime.fromisoformat(s)
                except Exception:
                    return None

            def sort_key(p: Any) -> tuple[int, datetime]:
                if isinstance(p, dict):
                    dt = parse_time(p.get("scheduled_time"))
                else:
                    dt = None
                return (0 if dt else 1, dt or datetime.max)

            posts_sorted = sorted(posts, key=sort_key)

            for iid in tree.get_children():
                tree.delete(iid)
            posts_by_iid.clear()

            for p in posts_sorted:
                if not isinstance(p, dict):
                    continue
                dt = parse_time(p.get("scheduled_time"))
                dt_str = dt.strftime("%Y-%m-%d %H:%M:%S") if dt else display_one_line(p.get("scheduled_time") or "")
                iid = tree.insert(
                    "",
                    "end",
                    values=(
                        display_one_line(p.get("post_id", "")),
                        dt_str,
                        display_one_line(p.get("headline") or ""),
                        display_one_line(p.get("link") or ""),
                    ),
                )
                posts_by_iid[str(iid)] = dict(p)

            count_var.set(f"{len(posts_sorted)} scheduled post(s) in queue")

        ttk.Button(header, text="Refresh", command=load_posts).grid(row=0, column=1, sticky="e")

        columns = ("Post ID", "Scheduled Time", "Headline", "Link")
        tree = ttk.Treeview(container, columns=columns, show="headings", selectmode="browse")
        tree.grid(row=1, column=0, sticky="nsew")

        tree.heading("Post ID", text="Post ID")
        tree.heading("Scheduled Time", text="Scheduled Time")
        tree.heading("Headline", text="Headline")
        tree.heading("Link", text="Link")

        tree.column("Post ID", width=70, anchor="w", stretch=False)
        tree.column("Scheduled Time", width=170, anchor="w", stretch=False)
        tree.column("Headline", width=520, anchor="w", stretch=True)
        tree.column("Link", width=220, anchor="w", stretch=True)

        yscroll = ttk.Scrollbar(container, orient=tk.VERTICAL, command=tree.yview)
        yscroll.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=yscroll.set)

        actions = ttk.Frame(container)
        actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        actions.columnconfigure(0, weight=1)

        def _delete_windows_task_for_post_id(post_id: Any) -> None:
            """
            Best-effort: remove the Windows scheduled task for this post.
            Tasks are created by x.PostScheduler.task_scheduler() with name:
                NewsTracker_Post_<post_id>
            """
            if post_id is None or str(post_id).strip() == "":
                return
            task_name = f"NewsTracker_Post_{post_id}"
            try:
                # schtasks is a Windows utility; ignore errors if task is missing.
                subprocess.run(
                    ["schtasks", "/Delete", "/TN", task_name, "/F"],
                    capture_output=True,
                    text=True,
                    shell=True,
                )
            except Exception:
                # Best-effort cleanup only.
                pass

        def delete_selected() -> None:
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("No selection", "Select a scheduled post to delete.", parent=win)
                return

            iid = str(sel[0])
            post = posts_by_iid.get(iid)
            if not post:
                messagebox.showwarning("Missing item", "Could not find the selected post in memory. Try Refresh.", parent=win)
                return

            post_id = post.get("post_id")
            when = str(post.get("scheduled_time") or "").strip()
            headline = str(post.get("headline") or "").strip()

            if not messagebox.askyesno(
                "Confirm delete",
                f"Delete this scheduled post?\n\nPost ID: {post_id}\nScheduled: {when}\n\n{headline[:220]}",
                parent=win,
            ):
                return

            try:
                prev_cwd = os.getcwd()
                try:
                    os.chdir(_PROJECT_ROOT)
                    ps = post_scheduler()

                    removed = False
                    remaining: list[dict[str, Any]] = []
                    for p in ps.scheduled_posts:
                        if not isinstance(p, dict):
                            continue

                        # Prefer removing by post_id (should be unique); fall back to time+headline.
                        matches_id = (post_id is not None) and (p.get("post_id") == post_id)
                        matches_fallback = (str(p.get("scheduled_time") or "") == when) and (str(p.get("headline") or "") == headline)

                        if not removed and (matches_id or matches_fallback):
                            removed = True
                            continue
                        remaining.append(p)

                    if not removed:
                        messagebox.showinfo(
                            "Not found",
                            "That scheduled post was not found in `scheduled_posts.txt`.\nIt may have already been removed.",
                            parent=win,
                        )
                        load_posts()
                        return

                    ps.scheduled_posts = deque(remaining)
                    ps.save_queue()

                    # Keep Windows Task Scheduler in sync to avoid posts firing early.
                    _delete_windows_task_for_post_id(post_id)
                    try:
                        ps.task_scheduler()
                    except Exception:
                        # Task scheduling is helpful but not required for deletion.
                        pass
                finally:
                    os.chdir(prev_cwd)
            except Exception as e:
                messagebox.showerror("Delete failed", str(e), parent=win)
                return

            load_posts()

        ttk.Button(actions, text="Delete selected", command=delete_selected).grid(row=0, column=1, sticky="e")

        # Convenience: allow hitting Delete key to remove selection.
        tree.bind("<Delete>", lambda _e: delete_selected())

        load_posts()


def main() -> None:
    root = tk.Tk()
    # ttk theme (best-effort)
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    NewsHeadlinePosterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

