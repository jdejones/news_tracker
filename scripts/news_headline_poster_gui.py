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
import ctypes
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, inspect

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox
from tkinter import ttk

# Optional: real in-app browser (Chromium) using PyQt6 WebEngine.
# We keep the GUI in Tkinter but embed a Qt widget for robust, JS-capable page rendering.
try:
    from PyQt6.QtCore import QUrl, Qt
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout
    from PyQt6.QtWebEngineWidgets import QWebEngineView

    _HAVE_QT_WEBENGINE = True
except Exception:
    QUrl = None  # type: ignore[assignment]
    Qt = None  # type: ignore[assignment]
    QColor = None  # type: ignore[assignment]
    QApplication = None  # type: ignore[assignment]
    QWidget = None  # type: ignore[assignment]
    QVBoxLayout = None  # type: ignore[assignment]
    QWebEngineView = None  # type: ignore[assignment]
    _HAVE_QT_WEBENGINE = False

_QT_APP: "QApplication | None" = None
_QT_ENV_CONFIGURED = False

# Allow running as: `python scripts/news_headline_poster_gui.py`
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from api_keys import news_database
from x import Post_Constructor, post_scheduler, scheduled_post


NEWS_DB_URL = f"mysql+pymysql://root:{news_database}@127.0.0.1:3306/news"


def _apply_filings_stream_like_style(root: tk.Tk) -> None:
    """
    Visual-only styling to make this Tkinter GUI feel closer to `filings_stream_gui.py`:
    - clean typography (Segoe UI)
    - consistent padding
    - subtle, modern Treeview look
    """
    # Set sensible default fonts (best-effort; falls back silently if unavailable).
    try:
        default_family = "Segoe UI"
        default_size = 10
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                f = tkfont.nametofont(name)
                f.configure(family=default_family, size=default_size)
            except Exception:
                pass
        try:
            fixed = tkfont.nametofont("TkFixedFont")
            fixed.configure(family="Consolas", size=10)
        except Exception:
            pass
    except Exception:
        pass

    # ttk theme + widget styling
    try:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            # Keep the current theme if clam isn't available.
            pass

        bg = "#f6f7f9"
        panel_bg = "#ffffff"
        subtle = "#444444"

        try:
            root.configure(background=bg)
        except Exception:
            pass

        # Base
        style.configure(".", background=bg)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=subtle)
        style.configure("TButton", padding=(10, 6))
        style.configure("TCheckbutton", background=bg, foreground=subtle)
        style.configure("TCombobox", padding=(6, 4))
        style.configure("TEntry", padding=(6, 4))

        # Labelframe "cards"
        style.configure("TLabelframe", background=bg)
        style.configure("TLabelframe.Label", background=bg, foreground="#222222")

        # Treeview (table)
        style.configure(
            "Treeview",
            background=panel_bg,
            fieldbackground=panel_bg,
            foreground="#222222",
            rowheight=24,
            borderwidth=1,
            relief="solid",
        )
        style.configure("Treeview.Heading", font=(default_family, default_size, "bold"), foreground="#222222")
        style.map(
            "Treeview",
            background=[("selected", "#e8f0fe")],
            foreground=[("selected", "#222222")],
        )
    except Exception:
        pass


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
        self._qt_overlay: "QWidget | None" = None
        self._qt_overlay_hwnd: int | None = None
        self._qt_web: "QWebEngineView | None" = None
        self._qt_web_hwnd: int | None = None

        self._updates_file_path = os.path.join(_PROJECT_ROOT, "most_recent_updates.txt")
        self._updates_file_last_mtime: float | None = None

        self._rows_by_iid: dict[str, HeadlineRow] = {}
        self._selected_symbol: str | None = None
        self._selected_row: HeadlineRow | None = None

        self._build_ui()
        self._init_internal_browser()
        self._load_symbols_async()
        self._schedule_updates_refresh()

        # Ensure cleanup on close.
        try:
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        except Exception:
            pass

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        top = ttk.Frame(self.root, padding=10)
        top.grid(row=0, column=0, sticky="ew")
        # Let the updates strip grow; keep status anchored right.
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

        # Most recent updates (compact, horizontal)
        updates_strip = ttk.Frame(top)
        updates_strip.grid(row=0, column=6, sticky="ew", padx=(10, 10))
        updates_strip.columnconfigure(0, weight=1)

        self.updates_text = tk.Text(
            updates_strip,
            height=1,
            wrap="none",
            cursor="arrow",  # keep pointer cursor (not I-beam)
            takefocus=0,  # avoid tab-focus into a read-only log
        )
        self.updates_text.grid(row=0, column=0, sticky="ew")
        self.updates_text.configure(state="disabled")
        self.updates_text.bind("<Button-1>", self._on_updates_text_click)

        updates_xscroll = ttk.Scrollbar(updates_strip, orient=tk.HORIZONTAL, command=self.updates_text.xview)
        updates_xscroll.grid(row=1, column=0, sticky="ew")
        self.updates_text.configure(xscrollcommand=updates_xscroll.set)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(top, textvariable=self.status_var).grid(row=0, column=7, sticky="e")

        main = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        main.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        left = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=3)
        main.add(right, weight=2)

        # Left: resizable split (table + browser) like QSplitter in `filings_stream_gui.py`
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)

        left_split = ttk.Panedwindow(left, orient=tk.VERTICAL)
        left_split.grid(row=0, column=0, sticky="nsew")

        table_host = ttk.Frame(left_split)
        table_host.columnconfigure(0, weight=1)
        table_host.rowconfigure(0, weight=1)

        columns = ("Date", "Title", "Source", "Category", "Url")
        self.tree = ttk.Treeview(table_host, columns=columns, show="headings", selectmode="browse")
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

        yscroll = ttk.Scrollbar(table_host, orient=tk.VERTICAL, command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=yscroll.set)

        self.tree.bind("<<TreeviewSelect>>", self._on_row_selected)
        self.tree.bind("<Double-1>", lambda _e: self.open_link_clicked())

        # Browser (minimal article preview) — beneath the table
        browser = ttk.LabelFrame(left_split, text="Browser", padding=8)
        browser.columnconfigure(0, weight=1)
        browser.rowconfigure(2, weight=1)

        # Address bar (like `filings_stream_gui.py`)
        self.browser_addr_var = tk.StringVar(value="")
        self.browser_url_var = tk.StringVar(value="")

        browser_controls = ttk.Frame(browser)
        browser_controls.grid(row=0, column=0, columnspan=2, sticky="ew")
        browser_controls.columnconfigure(0, weight=1)

        self.browser_addr_entry = ttk.Entry(browser_controls, textvariable=self.browser_addr_var)
        self.browser_addr_entry.grid(row=0, column=0, sticky="ew")
        self.browser_addr_entry.bind("<Return>", lambda _e: self.browser_go_clicked())

        self.browser_go_btn = ttk.Button(browser_controls, text="Go", command=self.browser_go_clicked)
        self.browser_go_btn.grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.browser_open_external_btn = ttk.Button(
            browser_controls, text="Open external", command=self.browser_open_external_clicked
        )
        self.browser_open_external_btn.grid(row=0, column=2, sticky="e", padx=(8, 0))

        # Subtle status line (loading/loaded/errors).
        ttk.Label(browser, textvariable=self.browser_url_var, foreground="#444").grid(row=1, column=0, sticky="w", pady=(6, 0))

        # If PyQt6 WebEngine is available, embed a real browser here.
        # Otherwise, fall back to a simple text preview (scrape) so the app still runs.
        if _HAVE_QT_WEBENGINE:
            # Use a real Tk frame as the native host so we can control its background
            # (and avoid black "gutter" artifacts around the embedded child HWND).
            self.browser_host = tk.Frame(browser, background="#ffffff", highlightthickness=0, bd=0)
            self.browser_host.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
            self.browser_host.bind("<Configure>", self._on_browser_host_configure)
        else:
            self.browser_text = tk.Text(browser, height=10, wrap="word")
            self.browser_text.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
            browser_yscroll = ttk.Scrollbar(browser, orient=tk.VERTICAL, command=self.browser_text.yview)
            browser_yscroll.grid(row=2, column=1, sticky="ns", pady=(8, 0))
            self.browser_text.configure(yscrollcommand=browser_yscroll.set)

        left_split.add(table_host, weight=3)
        left_split.add(browser, weight=2)

        # Right: resizable split (top controls + AI) like QSplitter in `filings_stream_gui.py`
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        right_split = ttk.Panedwindow(right, orient=tk.VERTICAL)
        right_split.grid(row=0, column=0, sticky="nsew")

        top_right = ttk.Frame(right_split)
        top_right.columnconfigure(0, weight=1)

        preview = ttk.LabelFrame(top_right, text="Selection / Post Preview", padding=10)
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

        btns = ttk.Frame(top_right)
        btns.grid(row=1, column=0, sticky="ew")
        btns.columnconfigure(5, weight=1)

        self.open_link_enabled_var = tk.BooleanVar(value=False)
        self.open_link_btn = ttk.Checkbutton(
            btns,
            text="Open link",
            variable=self.open_link_enabled_var,
            command=self._on_open_link_toggle,
        )
        self.open_link_btn.grid(row=0, column=0, sticky="w")

        self.open_external_btn = ttk.Button(btns, text="Open external", command=self.open_external_clicked)
        self.open_external_btn.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.post_btn = ttk.Button(btns, text="Post to X", command=self.post_clicked)
        self.post_btn.grid(row=0, column=2, sticky="w", padx=(8, 0))

        self.schedule_btn = ttk.Button(btns, text="Schedule Post", command=self.schedule_clicked)
        self.schedule_btn.grid(row=0, column=3, sticky="w", padx=(8, 0))

        self.view_scheduled_btn = ttk.Button(btns, text="View scheduled", command=self.view_scheduled_clicked)
        self.view_scheduled_btn.grid(row=0, column=4, sticky="w", padx=(8, 0))

        self.copy_btn = ttk.Button(btns, text="Copy text", command=self.copy_tweet_text_clicked)
        self.copy_btn.grid(row=0, column=6, sticky="e")

        ai_split = ttk.Panedwindow(right_split, orient=tk.VERTICAL)

        # AI input
        ai_in = ttk.LabelFrame(ai_split, text="A.I. input", padding=10)
        ai_in.columnconfigure(0, weight=1)
        ai_in.rowconfigure(0, weight=1)

        ai_in_controls = ttk.Frame(ai_in)
        ai_in_controls.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ai_in_controls.columnconfigure(0, weight=1)

        self.ai_copy_in_btn = ttk.Button(ai_in_controls, text="Copy text", command=self.ai_copy_input_clicked)
        self.ai_copy_in_btn.grid(row=0, column=0, sticky="w")
        self.ai_summarize_btn = ttk.Button(ai_in_controls, text="Summarize", command=self.ai_summarize_clicked)
        self.ai_summarize_btn.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.ai_input_text = tk.Text(ai_in, height=6, wrap="word")
        self.ai_input_text.grid(row=0, column=0, sticky="nsew")

        # AI output
        ai_out = ttk.LabelFrame(ai_split, text="A.I. output", padding=10)
        ai_out.columnconfigure(0, weight=1)
        ai_out.rowconfigure(0, weight=1)

        self.ai_output_text = tk.Text(ai_out, height=6, wrap="word")
        self.ai_output_text.grid(row=0, column=0, sticky="nsew")

        ai_out_controls = ttk.Frame(ai_out)
        ai_out_controls.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ai_out_controls.columnconfigure(5, weight=1)

        self.ai_copy_out_btn = ttk.Button(ai_out_controls, text="Copy text", command=self.ai_copy_output_clicked)
        self.ai_copy_out_btn.grid(row=0, column=0, sticky="w")

        self.ai_post_btn = ttk.Button(ai_out_controls, text="Post to X", command=self.ai_post_clicked)
        self.ai_post_btn.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.ai_schedule_btn = ttk.Button(ai_out_controls, text="Schedule Post", command=self.ai_schedule_clicked)
        self.ai_schedule_btn.grid(row=0, column=2, sticky="w", padx=(8, 0))

        self.ai_view_scheduled_btn = ttk.Button(
            ai_out_controls,
            text="View Scheduled",
            command=self.view_scheduled_clicked,
        )
        self.ai_view_scheduled_btn.grid(row=0, column=3, sticky="w", padx=(8, 0))

        right_split.add(top_right, weight=1)
        right_split.add(ai_split, weight=2)
        ai_split.add(ai_in, weight=1)
        ai_split.add(ai_out, weight=1)

        # Apply initial splitter proportions (visual-only; user can drag-adjust).
        self._main_split = main
        self._left_split = left_split
        self._right_split = right_split
        self._ai_split = ai_split
        self.root.after_idle(self._apply_default_split_sizes)

        # Make text widgets match the cleaner Qt look (visual-only).
        try:
            text_font = ("Segoe UI", 10)
            for w in (self.updates_text, self.title_text, self.ai_input_text, self.ai_output_text):
                try:
                    w.configure(
                        font=text_font,
                        background="#ffffff",
                        foreground="#222222",
                        insertbackground="#222222",
                        selectbackground="#e8f0fe",
                        selectforeground="#222222",
                    )
                except Exception:
                    pass
            if not _HAVE_QT_WEBENGINE:
                try:
                    self.browser_text.configure(
                        font=text_font,
                        background="#ffffff",
                        foreground="#222222",
                        insertbackground="#222222",
                        selectbackground="#e8f0fe",
                        selectforeground="#222222",
                    )
                except Exception:
                    pass
        except Exception:
            pass

    def _apply_default_split_sizes(self) -> None:
        """
        Visual-only: set initial sash positions for the Panedwindows to feel like
        `filings_stream_gui.py`'s splitters (and remain user-adjustable).
        """
        try:
            # Main split: ~60/40.
            w = int(self._main_split.winfo_width())
            if w > 200:
                self._main_split.sashpos(0, int(w * 0.60))
        except Exception:
            pass

        try:
            # Left split: table gets a bit more height than browser.
            h = int(self._left_split.winfo_height())
            if h > 200:
                self._left_split.sashpos(0, int(h * 0.62))
        except Exception:
            pass

        try:
            # Right split: keep preview/buttons smaller than AI section.
            h = int(self._right_split.winfo_height())
            if h > 200:
                self._right_split.sashpos(0, int(h * 0.35))
        except Exception:
            pass

        try:
            # AI split: 50/50 input/output.
            h = int(self._ai_split.winfo_height())
            if h > 200:
                self._ai_split.sashpos(0, int(h * 0.50))
        except Exception:
            pass

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)
        self.root.update_idletasks()

    def _on_close(self) -> None:
        # Best-effort cleanup for the embedded Qt widget.
        try:
            self._qt_overlay = None
            self._qt_overlay_hwnd = None
            self._qt_web = None
            self._qt_web_hwnd = None
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _ensure_qt_app(self) -> "QApplication | None":
        global _QT_APP
        global _QT_ENV_CONFIGURED
        if not _HAVE_QT_WEBENGINE:
            return None
        # QtWebEngine + native embedding can produce black "unpainted" regions on some Windows
        # setups (especially with DPI scaling). Prefer safer software paths unless the user
        # explicitly overrides via environment variables.
        if not _QT_ENV_CONFIGURED:
            _QT_ENV_CONFIGURED = True
            try:
                os.environ.setdefault("QT_OPENGL", "software")
            except Exception:
                pass
            try:
                existing = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
                flags = existing.split() if existing else []
                # Add only if not already present.
                for f in (
                    "--disable-gpu",
                    "--disable-gpu-compositing",
                    "--disable-features=VizDisplayCompositor",
                ):
                    if f not in flags:
                        flags.append(f)
                os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags).strip()
            except Exception:
                pass
        try:
            existing = QApplication.instance()  # type: ignore[misc]
        except Exception:
            existing = None
        if existing is not None:
            _QT_APP = existing
            return _QT_APP
        if _QT_APP is None:
            # Create a QApplication without taking over the main loop;
            # we pump events via Tk's `after()`.
            # IMPORTANT: Qt expects argv to contain a program name. An empty list can
            # trigger: "Argument list is empty, the program name is not passed..."
            _QT_APP = QApplication(sys.argv or ["news_headline_poster_gui"])  # type: ignore[call-arg]
        return _QT_APP

    def _pump_qt_events(self) -> None:
        app = self._ensure_qt_app()
        if app is not None:
            try:
                app.processEvents()  # type: ignore[misc]
            except Exception:
                pass
        self.root.after(10, self._pump_qt_events)

    def _init_internal_browser(self) -> None:
        if not _HAVE_QT_WEBENGINE:
            self.browser_url_var.set("Browser fallback mode (no PyQt6-WebEngine): will scrape page text.")
            return

        app = self._ensure_qt_app()
        if app is None:
            return

        try:
            # Create a dedicated frameless overlay window and host the webview inside it.
            overlay = QWidget()  # type: ignore[call-arg]
            try:
                if Qt is not None:
                    overlay.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)  # type: ignore[union-attr]
            except Exception:
                pass
            try:
                overlay.setWindowTitle("")  # avoid a visible title if the OS draws one
            except Exception:
                pass
            try:
                overlay.setStyleSheet("background: #ffffff;")
            except Exception:
                pass

            layout = QVBoxLayout(overlay)  # type: ignore[call-arg]
            try:
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(0)
            except Exception:
                pass

            web = QWebEngineView(overlay)  # type: ignore[call-arg]
            web.setUrl(QUrl("about:blank"))  # type: ignore[arg-type]
            # Help avoid unpainted "black gutters" when embedded in a non-Qt host:
            # force an opaque, white background.
            try:
                if Qt is not None:
                    web.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)  # type: ignore[union-attr]
            except Exception:
                pass
            try:
                web.setStyleSheet("background: #ffffff;")
            except Exception:
                pass
            try:
                if QColor is not None:
                    web.page().setBackgroundColor(QColor("#ffffff"))  # type: ignore[union-attr]
            except Exception:
                pass
            try:
                layout.addWidget(web)
            except Exception:
                pass

            # Force native handle creation.
            overlay.show()
            web.show()

            self._qt_overlay = overlay
            try:
                self._qt_overlay_hwnd = int(overlay.winId())
            except Exception:
                self._qt_overlay_hwnd = None

            self._qt_web = web
            try:
                self._qt_web_hwnd = int(web.winId())
            except Exception:
                self._qt_web_hwnd = None

            # Make the overlay owned by the Tk root so it stays above the app and minimizes with it.
            try:
                GWLP_HWNDPARENT = -8
                root_hwnd = int(self.root.winfo_id())
                try:
                    ctypes.windll.user32.SetWindowLongPtrW(self._qt_overlay_hwnd, GWLP_HWNDPARENT, root_hwnd)
                except Exception:
                    ctypes.windll.user32.SetWindowLongW(self._qt_overlay_hwnd, GWLP_HWNDPARENT, root_hwnd)
            except Exception:
                pass

            # Size it correctly immediately.
            self._resize_embedded_browser()

            # Start pumping Qt events.
            self._pump_qt_events()
            self._schedule_browser_addr_sync()
            self._schedule_apply_browser_chrome_fix()

            # Track Tk window moves/minimize to keep the overlay aligned.
            try:
                self.root.bind("<Configure>", lambda _e: self._resize_embedded_browser())
                self.root.bind("<Unmap>", lambda _e: self._hide_qt_overlay())
                self.root.bind("<Map>", lambda _e: self._show_qt_overlay())
            except Exception:
                pass
        except Exception as e:
            self.browser_url_var.set(f"Browser init failed (fallback mode): {e}")
            self._qt_overlay = None
            self._qt_overlay_hwnd = None
            self._qt_web = None
            self._qt_web_hwnd = None

    def _hide_qt_overlay(self) -> None:
        try:
            if self._qt_overlay is not None:
                try:
                    self._qt_overlay.hide()  # type: ignore[union-attr]
                    return
                except Exception:
                    pass
            if self._qt_overlay_hwnd:
                ctypes.windll.user32.ShowWindow(self._qt_overlay_hwnd, 0)  # SW_HIDE
        except Exception:
            pass

    def _show_qt_overlay(self) -> None:
        try:
            # Defer to the resize logic (it applies min-size hide logic too).
            self._resize_embedded_browser()
        except Exception:
            pass

    def _schedule_browser_addr_sync(self) -> None:
        """
        Keep the address bar synced without Qt→Python callbacks.
        This avoids rare hard-crashes from QtWebEngine emitting signals on non-Python threads.
        """
        try:
            if self._qt_web is not None and _HAVE_QT_WEBENGINE:
                try:
                    s = str(self._qt_web.url().toString() or "").strip()  # type: ignore[union-attr]
                except Exception:
                    s = ""
                if s:
                    try:
                        # Don't clobber user typing/pasting in the address bar.
                        editing = False
                        try:
                            if hasattr(self, "browser_addr_entry"):
                                editing = (self.root.focus_get() == self.browser_addr_entry)
                        except Exception:
                            editing = False

                        if (not editing) and hasattr(self, "browser_addr_var") and (self.browser_addr_var.get() != s):
                            self.browser_addr_var.set(s)
                    except Exception:
                        pass
        except Exception:
            pass
        # 4 Hz is plenty for a status/address bar.
        try:
            self.root.after(250, self._schedule_browser_addr_sync)
        except Exception:
            pass

    def _schedule_apply_browser_chrome_fix(self) -> None:
        """
        Visual-only: apply CSS that fixes scrollbar/corner "gutter" artifacts that can
        appear as black rectangles when QWebEngineView is embedded as a native child window.
        """
        try:
            self._apply_browser_css()
        except Exception:
            pass
        try:
            # Re-apply periodically; some navigations replace the document quickly.
            self.root.after(500, self._schedule_apply_browser_chrome_fix)
        except Exception:
            pass

    def _apply_browser_css(self) -> None:
        if self._qt_web is None or not _HAVE_QT_WEBENGINE:
            return
        js = r"""
        (function () {
          try {
            const id = "newsTrackerScrollbarStyle";
            let el = document.getElementById(id);
            if (!el) {
              el = document.createElement("style");
              el.id = id;
              (document.head || document.documentElement).appendChild(el);
            }
            el.textContent = `
              html, body { background: #ffffff !important; }
              /* Force scrollbar track/corner to a light neutral (prevents black gutter blocks). */
              ::-webkit-scrollbar { width: 12px; height: 12px; }
              ::-webkit-scrollbar-track { background: #f1f3f4; }
              ::-webkit-scrollbar-thumb {
                background: #c1c1c1;
                border-radius: 8px;
                border: 3px solid #f1f3f4;
              }
              ::-webkit-scrollbar-thumb:hover { background: #a8a8a8; }
              ::-webkit-scrollbar-corner { background: #f1f3f4; }
            `;
          } catch (e) { /* ignore */ }
        })();
        """
        try:
            self._qt_web.page().runJavaScript(js)  # type: ignore[union-attr]
        except Exception:
            pass

    def _on_browser_host_configure(self, _event: tk.Event) -> None:
        self._resize_embedded_browser()

    def _resize_embedded_browser(self) -> None:
        if self._qt_overlay is None or self._qt_overlay_hwnd is None or self._qt_web is None:
            return
        try:
            # Overlay mode: position the borderless Qt window over the Tk host frame using
            # the host HWND's *screen* rectangle (physical pixels), then translate to Qt's
            # logical coordinates using the screen devicePixelRatio (important at 125% DPI).
            host_hwnd = int(self.browser_host.winfo_id())

            class _RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            rect = _RECT()
            ok = ctypes.windll.user32.GetWindowRect(host_hwnd, ctypes.byref(rect))
            if not ok:
                # Fall back to Tk coords if needed.
                x = int(self.browser_host.winfo_rootx())
                y = int(self.browser_host.winfo_rooty())
                w = int(self.browser_host.winfo_width())
                h = int(self.browser_host.winfo_height())
            else:
                x = int(rect.left)
                y = int(rect.top)
                w = int(rect.right - rect.left)
                h = int(rect.bottom - rect.top)
            if w <= 1 or h <= 1:
                return
            # If the pane is made extremely small, QtWebEngine can become unstable when embedded.
            # Hide the child window below a minimum size to avoid crashes.
            try:
                SW_HIDE = 0
                SW_SHOWNOACTIVATE = 4
                if w < 120 or h < 90:
                    try:
                        self._qt_overlay.hide()  # type: ignore[union-attr]
                    except Exception:
                        ctypes.windll.user32.ShowWindow(self._qt_overlay_hwnd, SW_HIDE)
                    return
                try:
                    self._qt_overlay.show()  # type: ignore[union-attr]
                except Exception:
                    ctypes.windll.user32.ShowWindow(self._qt_overlay_hwnd, SW_SHOWNOACTIVATE)
            except Exception:
                pass
            # Prefer Qt geometry changes (keeps QtWebEngine painting stable).
            dpr = 1.0
            try:
                app = self._ensure_qt_app()
                if app is not None:
                    try:
                        scr = app.primaryScreen()  # type: ignore[union-attr]
                        if scr is not None:
                            dpr = float(getattr(scr, "devicePixelRatio", lambda: 1.0)() or 1.0)
                    except Exception:
                        dpr = 1.0
            except Exception:
                dpr = 1.0

            x_q = int(x / dpr)
            y_q = int(y / dpr)
            w_q = max(int(w / dpr), 1)
            h_q = max(int(h / dpr), 1)
            try:
                self._qt_overlay.setGeometry(x_q, y_q, w_q, h_q)  # type: ignore[union-attr]
                try:
                    self._qt_overlay.raise_()  # type: ignore[union-attr]
                except Exception:
                    pass
            except Exception:
                # Last-resort: direct Win32 move.
                ctypes.windll.user32.MoveWindow(self._qt_overlay_hwnd, x, y, w, h, True)
            # Force a redraw of the native child window to prevent leftover artifacts.
            try:
                RDW_INVALIDATE = 0x0001
                RDW_UPDATENOW = 0x0100
                RDW_ALLCHILDREN = 0x0080
                ctypes.windll.user32.RedrawWindow(
                    self._qt_overlay_hwnd,
                    None,
                    None,
                    RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN,
                )
            except Exception:
                pass
        except Exception:
            pass

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
            # Compact single-line display with horizontal scroll.
            return "  ".join(lines) if lines else "(No updates yet)"
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
        self._clear_browser()
        try:
            self.ai_input_text.delete("1.0", "end")
            self.ai_output_text.delete("1.0", "end")
        except Exception:
            pass

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
        self._maybe_open_link_in_browser()

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
        # Only load into the embedded browser if Open link is enabled.
        if not bool(self.open_link_enabled_var.get()):
            return
        url = (self.link_var.get() or "").strip()
        if not url:
            messagebox.showinfo("No link", "No link to open. Select a row or paste a link.")
            return
        self._load_browser_url(url)

    def open_external_clicked(self) -> None:
        url = (self.link_var.get() or "").strip()
        if not url:
            messagebox.showinfo("No link", "No link to open. Select a row or paste a link.")
            return
        webbrowser.open(url)

    def _on_open_link_toggle(self) -> None:
        # If the user just enabled it, immediately load whatever URL is present.
        if bool(self.open_link_enabled_var.get()):
            self._maybe_open_link_in_browser()

    def _maybe_open_link_in_browser(self) -> None:
        if not bool(self.open_link_enabled_var.get()):
            return
        url = (self.link_var.get() or "").strip()
        if not url:
            return
        self._load_browser_url(url)

    def _clear_browser(self) -> None:
        self.browser_url_var.set("")
        try:
            if hasattr(self, "browser_addr_var"):
                self.browser_addr_var.set("")
        except Exception:
            pass
        try:
            if self._qt_web is not None and _HAVE_QT_WEBENGINE:
                self._qt_web.setUrl(QUrl("about:blank"))  # type: ignore[arg-type]
            else:
                self.browser_text.delete("1.0", "end")
        except Exception:
            pass

    def browser_go_clicked(self) -> None:
        raw = ""
        try:
            raw = (self.browser_addr_var.get() or "").strip()
        except Exception:
            raw = ""
        if not raw:
            return
        self._load_browser_url(raw)

    def browser_open_external_clicked(self) -> None:
        raw = ""
        try:
            raw = (self.browser_addr_var.get() or "").strip()
        except Exception:
            raw = ""
        if not raw:
            # Fall back to the selection URL.
            raw = (self.link_var.get() or "").strip()
        if not raw:
            messagebox.showinfo("No link", "No URL to open.")
            return
        webbrowser.open(self._normalize_browser_url(raw))

    def _load_browser_url(self, url: str) -> None:
        raw_in = (url or "").strip()
        if not raw_in:
            return
        raw = self._normalize_browser_url(raw_in)
        try:
            if hasattr(self, "browser_addr_var"):
                self.browser_addr_var.set(raw)
        except Exception:
            pass

        # Preferred: real embedded browser (handles JS-heavy sites).
        if self._qt_web is not None and _HAVE_QT_WEBENGINE:
            try:
                self.browser_url_var.set(f"Loading: {raw}")
                try:
                    # More robust than QUrl(raw): adds scheme, handles spaces, etc.
                    qurl = QUrl.fromUserInput(raw)  # type: ignore[union-attr]
                except Exception:
                    qurl = QUrl(raw)  # type: ignore[arg-type]
                self._qt_web.setUrl(qurl)  # type: ignore[arg-type]
                # Apply visual CSS once the page is likely present.
                try:
                    self.root.after(750, self._apply_browser_css)
                except Exception:
                    pass
                return
            except Exception as e:
                # Fall back to scrape if Qt fails unexpectedly.
                self.browser_url_var.set(f"Browser error (fallback): {e}")

        # Fallback: scrape page text into a Tk Text widget (best-effort).
        self.browser_url_var.set(f"Loading (fallback): {raw}")

        def worker() -> None:
            try:
                text = self._fetch_article_text_fallback(raw)
            except Exception as e:
                self.root.after(0, lambda: self._on_browser_fallback_error(raw, e))
                return
            self.root.after(0, lambda: self._on_browser_fallback_done(raw, text))

        threading.Thread(target=worker, daemon=True).start()

    def _normalize_browser_url(self, raw: str) -> str:
        """
        Normalize user-entered URLs so both QtWebEngine and requests() can load them.
        Examples:
        - "finviz.com" -> "https://finviz.com"
        - "www.cnn.com" -> "https://www.cnn.com"
        - "//example.com" -> "https://example.com"
        """
        s = (raw or "").strip()
        if not s:
            return ""
        lower = s.lower()
        # Leave special/internal schemes untouched.
        if lower.startswith(("http://", "https://", "file://", "about:", "chrome:", "edge:", "view-source:")):
            return s
        if s.startswith("//"):
            return "https:" + s
        # If the user typed a bare host/path, assume https.
        return "https://" + s

    def _fetch_article_text_fallback(self, url: str) -> str:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NewsTracker/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()

        html = resp.text or ""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "header", "footer", "svg"]):
            try:
                tag.decompose()
            except Exception:
                pass
        root = soup.find("article") or soup.find("main") or soup.body or soup
        text = root.get_text("\n", strip=True) if root else ""
        lines = [ln.strip() for ln in (text or "").splitlines()]
        lines = [ln for ln in lines if ln]
        return "\n".join(lines)

    def _on_browser_fallback_done(self, url: str, text: str) -> None:
        if not hasattr(self, "browser_text"):
            return
        self.browser_url_var.set(f"Loaded (fallback): {url}")
        self.browser_text.delete("1.0", "end")
        self.browser_text.insert("1.0", text or "")
        self.browser_text.yview_moveto(0.0)

    def _on_browser_fallback_error(self, url: str, e: Exception) -> None:
        if not hasattr(self, "browser_text"):
            return
        self.browser_url_var.set(f"Failed (fallback): {url}")
        self.browser_text.delete("1.0", "end")
        self.browser_text.insert("1.0", f"Could not load article text.\n\nURL: {url}\n\nError:\n{e}")
        self.browser_text.yview_moveto(0.0)

    def ai_copy_input_clicked(self) -> None:
        """
        Copy highlighted text from the Browser panel into the A.I. input box.
        Falls back to clipboard if nothing is selected in the Browser box.
        """
        # Preferred: pull selection directly from the embedded QWebEngineView (real browser).
        if self._qt_web is not None and _HAVE_QT_WEBENGINE:
            self.ai_copy_in_btn.configure(state="disabled")

            def _finish(selected: Any) -> None:
                self.ai_copy_in_btn.configure(state="normal")
                copied = ("" if selected is None else str(selected)).strip()
                if not copied:
                    # Fall back to clipboard (e.g., if user used Ctrl+C in the browser).
                    try:
                        copied = str(self.root.clipboard_get() or "").strip()
                    except Exception:
                        copied = ""

                if not copied:
                    messagebox.showinfo("No text", "Highlight text in the Browser panel first.")
                    return

                self.ai_input_text.delete("1.0", "end")
                self.ai_input_text.insert("1.0", copied)
                self._set_status("Copied text into A.I. input.")

            try:
                # Using JS avoids API differences across PyQt6 versions.
                self._qt_web.page().runJavaScript(  # type: ignore[union-attr]
                    "window.getSelection().toString()",
                    lambda s: self.root.after(0, lambda: _finish(s)),
                )
                return
            except Exception:
                self.ai_copy_in_btn.configure(state="normal")
                # Fall through to clipboard/text fallback.

        copied = ""
        try:
            if hasattr(self, "browser_text"):
                copied = self.browser_text.get("sel.first", "sel.last")
        except Exception:
            copied = ""

        if not copied.strip():
            try:
                copied = str(self.root.clipboard_get() or "")
            except Exception:
                copied = ""

        copied = (copied or "").strip()
        if not copied:
            messagebox.showinfo("No text", "Highlight text in the Browser panel (or copy it) first.")
            return

        self.ai_input_text.delete("1.0", "end")
        self.ai_input_text.insert("1.0", copied)
        self._set_status("Copied text into A.I. input.")

    def ai_summarize_clicked(self) -> None:
        copied_text = (self.ai_input_text.get("1.0", "end") or "").rstrip()
        if not copied_text.strip():
            messagebox.showinfo("Nothing to summarize", "Copy some text into the A.I. input box first.")
            return

        self.ai_summarize_btn.configure(state="disabled")
        self._set_status("Summarizing…")

        def worker() -> None:
            try:
                summary = self._summarize_with_openai(copied_text=copied_text)
            except Exception as e:
                self.root.after(0, lambda: self._on_summarize_error(e))
                return
            self.root.after(0, lambda: self._on_summarize_done(summary))

        threading.Thread(target=worker, daemon=True).start()

    def _summarize_with_openai(self, *, copied_text: str) -> str:
        # Keep the API key in env var like the filings GUI does.
        from api_keys import open_ai as oai_key

        if oai_key and not os.environ.get("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = oai_key

        from openai import OpenAI

        client = OpenAI()
        prompt = f"In less than 300 characters summarize the following article:\n{copied_text}"
        response = client.responses.create(
            model="gpt-5.2",
            input=prompt,
            reasoning={"effort": "none"},
            text={"verbosity": "low"},
        )

        summary = ""
        try:
            summary = str(getattr(response, "output_text", "") or "")
        except Exception:
            summary = ""
        if not summary.strip():
            try:
                summary = str(response)
            except Exception:
                summary = ""
        return summary

    def _on_summarize_done(self, summary: str) -> None:
        self.ai_summarize_btn.configure(state="normal")
        self.ai_output_text.delete("1.0", "end")
        self.ai_output_text.insert("1.0", (summary or "").strip())
        self._set_status("Summary ready.")

    def _on_summarize_error(self, e: Exception) -> None:
        self.ai_summarize_btn.configure(state="normal")
        self._set_status("Summarize failed.")
        messagebox.showerror("Summarize failed", str(e))

    def ai_copy_output_clicked(self) -> None:
        text = (self.ai_output_text.get("1.0", "end") or "").rstrip()
        if not text.strip():
            messagebox.showinfo("No text", "No A.I. output to copy yet.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status("Copied A.I. output to clipboard.")

    def ai_post_clicked(self) -> None:
        tweet_text = (self.ai_output_text.get("1.0", "end") or "").rstrip()
        if not tweet_text.strip():
            messagebox.showinfo("No text", "Enter or generate A.I. output first.")
            return
        if len(tweet_text) > 280:
            messagebox.showerror("Tweet too long", f"This text is too long to post.\n\nLength: {len(tweet_text)}/280")
            return

        link = (self.link_var.get() or "").strip() or None
        msg = "This will post the A.I. output text to X"
        if link:
            msg += " and then reply with the current URL.\n\nContinue?"
        else:
            msg += ".\n\nNo URL detected to reply with.\n\nContinue?"
        if not messagebox.askyesno("Confirm post", msg):
            return

        self.ai_post_btn.configure(state="disabled")
        self._set_status("Posting A.I. output to X…")

        def worker() -> None:
            try:
                post = self.pc.x_post(text=tweet_text)
                if link:
                    self.pc.x_post(text=link, reply_to_tweet_id=post["tweet_id"])
            except Exception as e:
                self.root.after(0, lambda: self._on_ai_post_error(e))
                return
            self.root.after(0, lambda: self._on_ai_post_done())

        threading.Thread(target=worker, daemon=True).start()

    def _on_ai_post_done(self) -> None:
        self.ai_post_btn.configure(state="normal")
        self._set_status("Posted successfully.")
        messagebox.showinfo("Done", "Post sent to X.")

    def _on_ai_post_error(self, e: Exception) -> None:
        self.ai_post_btn.configure(state="normal")
        self._set_status("Post failed.")
        messagebox.showerror("Post failed", str(e))

    def ai_schedule_clicked(self) -> None:
        tweet_text = (self.ai_output_text.get("1.0", "end") or "").rstrip()
        if not tweet_text.strip():
            messagebox.showinfo("No text", "Enter or generate A.I. output first.")
            return
        if len(tweet_text) > 280:
            messagebox.showerror("Tweet too long", f"This text is too long to schedule.\n\nLength: {len(tweet_text)}/280")
            return

        self._open_schedule_popup(tweet_text=tweet_text, link=(self.link_var.get() or "").strip() or None)

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
    _apply_filings_stream_like_style(root)
    NewsHeadlinePosterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

