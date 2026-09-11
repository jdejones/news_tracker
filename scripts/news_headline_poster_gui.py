"""
Pure PyQt6 GUI for browsing news headlines and posting them to X.

Run:
    python scripts/news_headline_poster_gui.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import webbrowser
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

import pandas as pd
from sqlalchemy import create_engine, text

from PyQt6.QtCore import QDate, QThread, QTime, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QMouseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView

    _WEBENGINE_IMPORT_ERROR: Exception | None = None
except Exception as exc:
    QWebEngineView = None  # type: ignore[assignment]
    _WEBENGINE_IMPORT_ERROR = exc


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


@dataclass(frozen=True)
class FeedRow:
    news_id: int
    ticker: str
    title: str
    url: str
    date: datetime | None
    source: str | None = None
    category: str | None = None


def _safe_dt(value: Any) -> datetime | None:
    try:
        dt = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(dt) else dt.to_pydatetime()
    except Exception:
        return None


def list_news_symbols() -> list[str]:
    """Return symbols represented in the consolidated news table."""
    engine = create_engine(NEWS_DB_URL, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    try:
        symbols = pd.read_sql(
            text("SELECT DISTINCT Ticker FROM stock_news ORDER BY Ticker"),
            con=engine,
        )
        return symbols["Ticker"].dropna().astype(str).str.lower().tolist()
    finally:
        engine.dispose()


def retrieve_symbol_headlines(
    symbol: str,
    *,
    since: datetime | None = None,
    limit: int = 500,
) -> pd.DataFrame:
    """Read one symbol's headlines from the consolidated news table."""
    sym = str(symbol).strip().upper()
    if not sym:
        raise ValueError("symbol must be non-empty")
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    engine = create_engine(NEWS_DB_URL, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    columns = "Title, Source, Date, Url, Category, Ticker"
    if since is None:
        statement = text(
            f"SELECT {columns} FROM stock_news "
            "WHERE Ticker = :ticker "
            "ORDER BY Date DESC LIMIT :limit"
        )
        params = {"ticker": sym, "limit": limit}
    else:
        statement = text(
            f"SELECT {columns} FROM stock_news "
            "WHERE Ticker = :ticker AND Date >= :since "
            "ORDER BY Date DESC LIMIT :limit"
        )
        params = {"ticker": sym, "since": since, "limit": limit}

    try:
        return pd.read_sql(statement, con=engine, params=params)
    finally:
        engine.dispose()


def retrieve_news_feed(
    symbols: list[str],
    *,
    after_id: int | None = None,
    limit: int = 500,
) -> pd.DataFrame:
    """Read the latest rows, or rows added after an id, for several symbols."""
    normalized = list(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()))
    if not normalized:
        raise ValueError("at least one symbol is required")
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    symbol_params = {f"symbol_{index}": symbol for index, symbol in enumerate(normalized)}
    placeholders = ", ".join(f":{name}" for name in symbol_params)
    columns = "id, Ticker, Title, Source, Date, Url, Category"
    params: dict[str, Any] = {**symbol_params, "limit": limit}
    if after_id is None:
        statement = text(
            f"SELECT {columns} FROM stock_news "
            f"WHERE Ticker IN ({placeholders}) "
            "ORDER BY id DESC LIMIT :limit"
        )
    else:
        statement = text(
            f"SELECT {columns} FROM stock_news "
            f"WHERE Ticker IN ({placeholders}) AND id > :after_id "
            "ORDER BY id ASC LIMIT :limit"
        )
        params["after_id"] = after_id

    engine = create_engine(NEWS_DB_URL, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    try:
        return pd.read_sql(statement, con=engine, params=params)
    finally:
        engine.dispose()


class FunctionWorker(QThread):
    """Run one backend callable and marshal its result back to Qt's main thread."""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, function: Callable[[], Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._function = function

    def run(self) -> None:
        try:
            result = self._function()
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)


class UpdatesText(QPlainTextEdit):
    symbolClicked = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMaximumHeight(47)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        cursor = self.cursorForPosition(event.position().toPoint())
        symbol = self._symbol_at_position(self.toPlainText(), cursor.position())
        if symbol:
            self.symbolClicked.emit(symbol)
        super().mousePressEvent(event)

    @staticmethod
    def _symbol_at_position(text: str, position: int) -> str | None:
        if not text:
            return None
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-^$")
        position = min(max(position, 0), len(text) - 1)
        left = position
        while left > 0 and text[left - 1] in allowed:
            left -= 1
        right = position
        while right < len(text) and text[right] in allowed:
            right += 1
        token = text[left:right].strip().lstrip("^$").upper()
        return token if re.fullmatch(r"[A-Z]{1,6}([.-][A-Z0-9]{1,5})?", token) else None


class NewsHeadlinePosterWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        if QWebEngineView is None:
            raise RuntimeError(f"PyQt6-WebEngine is required: {_WEBENGINE_IMPORT_ERROR}")

        self.setWindowTitle("News Tracker — Post a Headline")
        self.resize(1100, 650)

        self.pc = Post_Constructor()
        self._selected_symbol: str | None = None
        self._selected_row: HeadlineRow | None = None
        self._rows: list[HeadlineRow] = []
        self._workers: set[FunctionWorker] = set()
        self._closing = False
        self._updates_file_path = os.path.join(_PROJECT_ROOT, "most_recent_updates.txt")
        self._updates_file_last_mtime: float | None = None
        self._feed_symbols: list[str] = []
        self._feed_seen_ids: set[int] = set()
        self._feed_last_id = 0
        self._feed_request_generation = 0
        self._feed_refresh_in_progress = False

        self._build_ui()
        self._apply_style()
        self._load_symbols_async()

        self._updates_timer = QTimer(self)
        self._updates_timer.timeout.connect(self._refresh_updates_box)
        self._updates_timer.start(3000)
        self._refresh_updates_box()

        self._feed_timer = QTimer(self)
        self._feed_timer.timeout.connect(self._poll_news_feed)
        self._feed_timer.start(3000)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)

        controls = QHBoxLayout()
        outer.addLayout(controls)
        controls.addWidget(QLabel("Symbol"))
        self.symbol_combo = QComboBox()
        self.symbol_combo.setEditable(True)
        self.symbol_combo.setMinimumWidth(115)
        controls.addWidget(self.symbol_combo)
        controls.addWidget(QLabel("Since (YYYY-MM-DD)"))
        self.since_entry = QLineEdit((datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d"))
        self.since_entry.setMaximumWidth(110)
        controls.addWidget(self.since_entry)
        self.refresh_symbols_btn = QPushButton("Refresh symbols")
        self.load_btn = QPushButton("Load headlines")
        controls.addWidget(self.refresh_symbols_btn)
        controls.addWidget(self.load_btn)
        self.updates_text = UpdatesText()
        controls.addWidget(self.updates_text, 1)
        self.status_label = QLabel("Ready")
        controls.addWidget(self.status_label)

        self.refresh_symbols_btn.clicked.connect(self._load_symbols_async)
        self.load_btn.clicked.connect(self.load_headlines_clicked)
        self.symbol_combo.activated.connect(lambda _index: self.load_headlines_clicked())
        self.symbol_combo.lineEdit().returnPressed.connect(self.load_headlines_clicked)
        self.since_entry.returnPressed.connect(self.load_headlines_clicked)
        self.updates_text.symbolClicked.connect(self._load_updates_symbol)

        main_split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(main_split, 1)

        left_split = QSplitter(Qt.Orientation.Vertical)
        main_split.addWidget(left_split)

        self.headline_table = QTableWidget(0, 5)
        self.headline_table.setHorizontalHeaderLabels(["Date", "Title", "Source", "Category", "Url"])
        self.headline_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.headline_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.headline_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.headline_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(50)
        self.headline_table.setColumnWidth(0, 135)
        self.headline_table.setColumnWidth(1, 400)
        self.headline_table.setColumnWidth(2, 120)
        self.headline_table.setColumnWidth(3, 120)
        self.headline_table.setColumnWidth(4, 350)
        self.headline_table.setSortingEnabled(True)
        self.headline_table.sortItems(0, Qt.SortOrder.DescendingOrder)
        self.headline_table.itemSelectionChanged.connect(self._on_row_selected)
        self.headline_table.cellDoubleClicked.connect(lambda _row, _col: self.open_link_clicked())
        left_split.addWidget(self.headline_table)

        browser_group = QGroupBox("Browser")
        browser_layout = QVBoxLayout(browser_group)
        browser_controls = QHBoxLayout()
        browser_layout.addLayout(browser_controls)
        self.browser_addr_entry = QLineEdit()
        self.browser_go_btn = QPushButton("Go")
        self.browser_open_external_btn = QPushButton("Open external")
        browser_controls.addWidget(self.browser_addr_entry, 1)
        browser_controls.addWidget(self.browser_go_btn)
        browser_controls.addWidget(self.browser_open_external_btn)
        self.browser_status_label = QLabel("")
        browser_layout.addWidget(self.browser_status_label)
        self.web = QWebEngineView()
        self.web.setUrl(QUrl("about:blank"))
        self.web.page().setBackgroundColor(QColor("#ffffff"))
        browser_layout.addWidget(self.web, 1)
        self.browser_addr_entry.returnPressed.connect(self.browser_go_clicked)
        self.browser_go_btn.clicked.connect(self.browser_go_clicked)
        self.browser_open_external_btn.clicked.connect(self.browser_open_external_clicked)
        self.web.urlChanged.connect(self._on_browser_url_changed)
        self.web.loadStarted.connect(lambda: self.browser_status_label.setText("Loading…"))
        self.web.loadFinished.connect(self._on_browser_load_finished)
        left_split.addWidget(browser_group)

        right_split = QSplitter(Qt.Orientation.Vertical)
        main_split.addWidget(right_split)

        post_panel = QWidget()
        post_layout = QVBoxLayout(post_panel)
        post_layout.setContentsMargins(0, 0, 0, 0)
        preview = QGroupBox("Selection / Post Preview")
        preview_layout = QVBoxLayout(preview)
        self.title_text = QPlainTextEdit()
        self.title_text.setMinimumHeight(100)
        self.link_entry = QLineEdit()
        self.preview_label = QLabel("")
        preview_layout.addWidget(self.title_text)
        preview_layout.addWidget(self.link_entry)
        preview_layout.addWidget(self.preview_label)
        post_layout.addWidget(preview, 1)
        self.title_text.textChanged.connect(self._update_length_indicator)

        post_buttons = QHBoxLayout()
        post_layout.addLayout(post_buttons)
        self.open_link_check = QCheckBox("Open link")
        self.open_external_btn = QPushButton("Open external")
        self.post_btn = QPushButton("Post to X")
        self.schedule_btn = QPushButton("Schedule Post")
        self.view_scheduled_btn = QPushButton("View scheduled")
        self.copy_btn = QPushButton("Copy text")
        for widget in (
            self.open_link_check,
            self.open_external_btn,
            self.post_btn,
            self.schedule_btn,
            self.view_scheduled_btn,
        ):
            post_buttons.addWidget(widget)
        post_buttons.addStretch(1)
        post_buttons.addWidget(self.copy_btn)
        self.open_link_check.toggled.connect(self._on_open_link_toggle)
        self.open_external_btn.clicked.connect(self.open_external_clicked)
        self.post_btn.clicked.connect(self.post_clicked)
        self.schedule_btn.clicked.connect(self.schedule_clicked)
        self.view_scheduled_btn.clicked.connect(self.view_scheduled_clicked)
        self.copy_btn.clicked.connect(self.copy_tweet_text_clicked)
        right_split.addWidget(post_panel)

        feed_group = QGroupBox("Live News Feed")
        feed_layout = QVBoxLayout(feed_group)
        feed_controls = QHBoxLayout()
        feed_controls.addWidget(QLabel("Symbols"))
        self.feed_symbols_entry = QLineEdit()
        self.feed_symbols_entry.setPlaceholderText("AAPL, MSFT, NVDA")
        self.feed_symbols_entry.setMinimumWidth(80)
        self.feed_symbols_entry.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.feed_apply_btn = QPushButton("Apply")
        self.feed_refresh_btn = QPushButton("Refresh")
        feed_controls.addWidget(self.feed_symbols_entry, 1)
        feed_controls.addWidget(self.feed_apply_btn)
        feed_controls.addWidget(self.feed_refresh_btn)
        feed_layout.addLayout(feed_controls)
        self.feed_status_label = QLabel("Enter comma- or space-separated symbols to start the feed.")
        self.feed_status_label.setWordWrap(True)
        self.feed_status_label.setMinimumWidth(0)
        self.feed_status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        feed_layout.addWidget(self.feed_status_label)
        self.feed_table = QTableWidget(0, 6)
        self.feed_table.setHorizontalHeaderLabels(["Date", "Symbol", "Title", "Source", "Category", "Url"])
        self.feed_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.feed_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.feed_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        feed_header = self.feed_table.horizontalHeader()
        feed_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        feed_header.setStretchLastSection(False)
        feed_header.setMinimumSectionSize(45)
        self.feed_table.setColumnWidth(0, 135)
        self.feed_table.setColumnWidth(1, 75)
        self.feed_table.setColumnWidth(2, 360)
        self.feed_table.setColumnWidth(3, 105)
        self.feed_table.setColumnWidth(4, 90)
        self.feed_table.setColumnWidth(5, 220)
        self.feed_table.setSortingEnabled(True)
        self.feed_table.sortItems(0, Qt.SortOrder.DescendingOrder)
        feed_layout.addWidget(self.feed_table, 1)
        self.feed_apply_btn.clicked.connect(self.apply_feed_symbols)
        self.feed_refresh_btn.clicked.connect(self.refresh_news_feed)
        self.feed_symbols_entry.returnPressed.connect(self.apply_feed_symbols)
        self.feed_table.itemSelectionChanged.connect(self._on_feed_row_selected)
        self.feed_table.cellDoubleClicked.connect(lambda _row, _column: self.open_link_clicked())
        right_split.addWidget(feed_group)

        main_split.setSizes([660, 440])
        left_split.setSizes([370, 230])
        right_split.setSizes([220, 380])

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget { font-family: "Segoe UI"; font-size: 10pt; }
            QMainWindow { background: #f6f7f9; }
            QGroupBox { font-weight: bold; margin-top: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 3px; }
            QPlainTextEdit, QLineEdit, QComboBox, QTableWidget { background: white; color: #222; }
            QPushButton { padding: 5px 9px; }
            """
        )

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _start_worker(
        self,
        function: Callable[[], Any],
        success: Callable[[Any], None],
        failure: Callable[[str], None],
    ) -> None:
        worker = FunctionWorker(function, self)
        self._workers.add(worker)
        worker.succeeded.connect(success)
        worker.failed.connect(failure)
        worker.finished.connect(lambda w=worker: self._worker_finished(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _worker_finished(self, worker: FunctionWorker) -> None:
        self._workers.discard(worker)
        if self._closing and not self._workers:
            QTimer.singleShot(0, self.close)

    def _load_symbols_async(self) -> None:
        self.refresh_symbols_btn.setEnabled(False)
        self._set_status("Loading symbols from DB…")
        self._start_worker(list_news_symbols, self._on_symbols_loaded, self._on_symbols_loaded_error)

    def _on_symbols_loaded(self, value: Any) -> None:
        symbols = [str(symbol) for symbol in value]
        current = self.symbol_combo.currentText()
        self.symbol_combo.clear()
        self.symbol_combo.addItems([symbol.upper() for symbol in symbols])
        if current:
            self.symbol_combo.setCurrentText(current)
        self.refresh_symbols_btn.setEnabled(True)
        self._set_status(f"Loaded {len(symbols)} symbols.")

    def _on_symbols_loaded_error(self, error: str) -> None:
        self.refresh_symbols_btn.setEnabled(True)
        self._set_status("Failed to load symbols.")
        QMessageBox.critical(self, "DB error", f"Could not list symbols from MySQL.\n\n{error}")

    def _parse_since_date(self) -> datetime | None:
        raw = self.since_entry.text().strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Since date must be formatted as YYYY-MM-DD") from exc

    def load_headlines_clicked(self) -> None:
        symbol = self.symbol_combo.currentText().strip().lower()
        if not symbol:
            QMessageBox.warning(self, "Missing symbol", "Please choose a symbol.")
            return
        try:
            since_dt = self._parse_since_date()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid date", str(exc))
            return

        self._selected_symbol = symbol
        self._selected_row = None
        self._rows.clear()
        self.headline_table.setRowCount(0)
        self._clear_preview()
        self._clear_browser()
        self.load_btn.setEnabled(False)
        self._set_status(f"Loading headlines for {symbol.upper()}…")

        def load() -> pd.DataFrame:
            df = retrieve_symbol_headlines(symbol, since=since_dt, limit=500)
            if "Date" in df.columns:
                df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
            return df

        self._start_worker(load, self._on_headlines_loaded, self._on_headlines_loaded_error)

    def _on_headlines_loaded(self, value: Any) -> None:
        df = value
        self.load_btn.setEnabled(True)
        for column in ("Title", "Url"):
            if column not in df.columns:
                self._set_status("Unexpected DB schema for this table.")
                QMessageBox.critical(
                    self,
                    "Schema error",
                    f"Expected column '{column}' in the table for {self._selected_symbol or ''}, but it was not found.",
                )
                return

        rows: list[HeadlineRow] = []
        for _, record in df.iterrows():
            row = HeadlineRow(
                title=str(record.get("Title", "") or "").strip(),
                url=str(record.get("Url", "") or "").strip(),
                date=_safe_dt(record.get("Date")),
                source=str(record.get("Source")) if "Source" in df.columns and pd.notna(record.get("Source")) else None,
                category=(
                    str(record.get("Category"))
                    if "Category" in df.columns and pd.notna(record.get("Category"))
                    else None
                ),
            )
            if row.title and row.url:
                rows.append(row)

        self._rows = rows
        sorting_enabled = self.headline_table.isSortingEnabled()
        self.headline_table.setSortingEnabled(False)
        self.headline_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                row.date.strftime("%Y-%m-%d %H:%M") if row.date else "",
                row.title,
                row.source or "",
                row.category or "",
                row.url,
            ]
            for column_index, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column_index == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                self.headline_table.setItem(row_index, column_index, item)
        self.headline_table.setSortingEnabled(sorting_enabled)
        self._set_status(f"Loaded {len(rows)} headlines for {(self._selected_symbol or '').upper()}.")

    def _on_headlines_loaded_error(self, error: str) -> None:
        self.load_btn.setEnabled(True)
        self._set_status("Failed to load headlines.")
        QMessageBox.critical(self, "DB error", f"Could not load headlines.\n\n{error}")

    @staticmethod
    def _parse_feed_symbols(raw: str) -> list[str]:
        candidates = re.split(r"[\s,;]+", raw.strip())
        symbols: list[str] = []
        for candidate in candidates:
            symbol = candidate.strip().lstrip("$").upper()
            if not symbol:
                continue
            if not re.fullmatch(r"[A-Z0-9^]{1,10}(?:[.-][A-Z0-9]{1,5})?", symbol):
                raise ValueError(f"Invalid symbol: {candidate}")
            if symbol not in symbols:
                symbols.append(symbol)
        return symbols

    def apply_feed_symbols(self) -> None:
        try:
            symbols = self._parse_feed_symbols(self.feed_symbols_entry.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid symbols", str(exc))
            return
        if not symbols:
            QMessageBox.information(self, "No symbols", "Enter one or more symbols for the news feed.")
            return

        self._feed_symbols = symbols
        self.feed_symbols_entry.setText(", ".join(symbols))
        self.refresh_news_feed()

    def refresh_news_feed(self) -> None:
        if not self._feed_symbols:
            self.apply_feed_symbols()
            return
        self._feed_request_generation += 1
        generation = self._feed_request_generation
        self._feed_seen_ids.clear()
        self._feed_last_id = 0
        self.feed_table.setRowCount(0)
        self._start_feed_request(generation, initial=True)

    def _poll_news_feed(self) -> None:
        if not self._feed_symbols or self._feed_refresh_in_progress:
            return
        self._start_feed_request(self._feed_request_generation, initial=False)

    def _set_feed_status(self, message: str, symbols: list[str] | None = None) -> None:
        self.feed_status_label.setText(message)
        watched_symbols = self._feed_symbols if symbols is None else symbols
        self.feed_status_label.setToolTip(
            f"Watching: {', '.join(watched_symbols)}" if watched_symbols else ""
        )

    def _start_feed_request(self, generation: int, *, initial: bool) -> None:
        self._feed_refresh_in_progress = True
        self.feed_apply_btn.setEnabled(False)
        self.feed_refresh_btn.setEnabled(False)
        symbols = list(self._feed_symbols)
        after_id = None if initial else self._feed_last_id
        symbol_word = "symbol" if len(symbols) == 1 else "symbols"
        self._set_feed_status(
            f"Loading latest news for {len(symbols)} {symbol_word}…"
            if initial
            else f"Checking {len(symbols)} {symbol_word} for new rows…",
            symbols,
        )

        self._start_worker(
            lambda: retrieve_news_feed(symbols, after_id=after_id, limit=500),
            lambda value: self._on_feed_rows_loaded(value, generation, initial),
            lambda error: self._on_feed_rows_error(error, generation),
        )

    def _on_feed_rows_loaded(self, value: Any, generation: int, initial: bool) -> None:
        if generation != self._feed_request_generation:
            return
        self._feed_refresh_in_progress = False
        self.feed_apply_btn.setEnabled(True)
        self.feed_refresh_btn.setEnabled(True)
        df = value
        required = {"id", "Ticker", "Title", "Url"}
        if not required.issubset(df.columns):
            self._set_feed_status("The news table returned an unexpected schema.")
            return

        rows: list[FeedRow] = []
        for _, record in df.iterrows():
            try:
                news_id = int(record.get("id"))
            except (TypeError, ValueError):
                continue
            row = FeedRow(
                news_id=news_id,
                ticker=str(record.get("Ticker", "") or "").strip().upper(),
                title=str(record.get("Title", "") or "").strip(),
                url=str(record.get("Url", "") or "").strip(),
                date=_safe_dt(record.get("Date")),
                source=str(record.get("Source")) if pd.notna(record.get("Source")) else None,
                category=str(record.get("Category")) if pd.notna(record.get("Category")) else None,
            )
            if row.news_id not in self._feed_seen_ids and row.title:
                rows.append(row)

        sorting_enabled = self.feed_table.isSortingEnabled()
        self.feed_table.setSortingEnabled(False)
        for row in rows:
            table_row = self.feed_table.rowCount() if initial else 0
            self.feed_table.insertRow(table_row)
            values = [
                row.date.strftime("%Y-%m-%d %H:%M") if row.date else "",
                row.ticker,
                row.title,
                row.source or "",
                row.category or "",
                row.url,
            ]
            for column_index, value_text in enumerate(values):
                item = QTableWidgetItem(value_text)
                if column_index == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                self.feed_table.setItem(table_row, column_index, item)
            self._feed_seen_ids.add(row.news_id)
            self._feed_last_id = max(self._feed_last_id, row.news_id)

        while self.feed_table.rowCount() > 500:
            bottom_row = self.feed_table.rowCount() - 1
            item = self.feed_table.item(bottom_row, 0)
            old_row = item.data(Qt.ItemDataRole.UserRole) if item else None
            if isinstance(old_row, FeedRow):
                self._feed_seen_ids.discard(old_row.news_id)
            self.feed_table.removeRow(bottom_row)
        self.feed_table.setSortingEnabled(sorting_enabled)
        if sorting_enabled:
            self.feed_table.sortItems(0, Qt.SortOrder.DescendingOrder)

        symbol_count = len(self._feed_symbols)
        symbol_word = "symbol" if symbol_count == 1 else "symbols"
        if initial:
            message = f"Showing {len(rows)} current row(s) for {symbol_count} {symbol_word}."
        elif rows:
            message = f"Added {len(rows)} new row(s). Watching {symbol_count} {symbol_word}."
        else:
            message = f"Live — watching {symbol_count} {symbol_word}."
        self._set_feed_status(message)

    def _on_feed_rows_error(self, error: str, generation: int) -> None:
        if generation != self._feed_request_generation:
            return
        self._feed_refresh_in_progress = False
        self.feed_apply_btn.setEnabled(True)
        self.feed_refresh_btn.setEnabled(True)
        self._set_feed_status(f"Feed refresh failed: {error}")

    def _on_feed_row_selected(self) -> None:
        selected = self.feed_table.selectionModel().selectedRows()
        if not selected:
            return
        date_item = self.feed_table.item(selected[0].row(), 0)
        row = date_item.data(Qt.ItemDataRole.UserRole) if date_item else None
        if not isinstance(row, FeedRow):
            return
        self._selected_symbol = row.ticker.lower()
        self._selected_row = HeadlineRow(
            title=row.title,
            url=row.url,
            date=row.date,
            source=row.source,
            category=row.category,
        )
        self.title_text.setPlainText(self._tweet_text_for_selection())
        self.link_entry.setText(row.url)
        self._maybe_open_link_in_browser()

    def _on_row_selected(self) -> None:
        selected = self.headline_table.selectionModel().selectedRows()
        if not selected:
            return
        date_item = self.headline_table.item(selected[0].row(), 0)
        if date_item is None:
            return
        row = date_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(row, HeadlineRow):
            return
        self._selected_row = row
        self.title_text.setPlainText(self._tweet_text_for_selection())
        self.link_entry.setText(self._selected_row.url)
        self._maybe_open_link_in_browser()

    def _tweet_text_for_selection(self) -> str:
        if not self._selected_row or not self._selected_symbol:
            return ""
        return f"${self._selected_symbol.upper()}\n {self._selected_row.title}"

    def _current_tweet_text(self) -> str:
        return self.title_text.toPlainText().rstrip()

    def _clear_preview(self) -> None:
        self.title_text.clear()
        self.link_entry.clear()
        self._update_length_indicator()

    def _update_length_indicator(self) -> None:
        count = len(self._current_tweet_text())
        if count == 0:
            text = ""
        elif count > 280:
            text = f"Tweet length: {count}/280 (too long — will fail)"
        else:
            text = f"Tweet length: {count}/280 (link will be posted as a reply)"
        self.preview_label.setText(text)

    def _normalize_browser_url(self, raw: str) -> str:
        value = (raw or "").strip()
        if not value:
            return ""
        if value.lower().startswith(
            ("http://", "https://", "file://", "about:", "chrome:", "edge:", "view-source:")
        ):
            return value
        return "https:" + value if value.startswith("//") else "https://" + value

    def _load_browser_url(self, raw: str) -> None:
        value = self._normalize_browser_url(raw)
        if not value:
            return
        url = QUrl.fromUserInput(value)
        if not url.isValid():
            QMessageBox.warning(self, "Invalid URL", f"Could not parse URL:\n\n{raw}")
            return
        self.browser_addr_entry.setText(value)
        self.web.setUrl(url)

    def _clear_browser(self) -> None:
        self.browser_addr_entry.clear()
        self.browser_status_label.clear()
        self.web.setUrl(QUrl("about:blank"))

    def _on_browser_url_changed(self, url: QUrl) -> None:
        if not self.browser_addr_entry.hasFocus():
            self.browser_addr_entry.setText(url.toString())

    def _on_browser_load_finished(self, ok: bool) -> None:
        current = self.web.url().toString()
        self.browser_status_label.setText(f"Loaded: {current}" if ok else f"Failed to load: {current}")
        if ok:
            self.web.page().runJavaScript(
                """
                (() => {
                  const id = "newsTrackerScrollbarStyle";
                  let el = document.getElementById(id);
                  if (!el) {
                    el = document.createElement("style"); el.id = id;
                    (document.head || document.documentElement).appendChild(el);
                  }
                  el.textContent = "html,body{background:#fff!important}"
                    + "::-webkit-scrollbar{width:12px;height:12px}"
                    + "::-webkit-scrollbar-track,::-webkit-scrollbar-corner{background:#f1f3f4}"
                    + "::-webkit-scrollbar-thumb{background:#c1c1c1;border-radius:8px;border:3px solid #f1f3f4}";
                })();
                """
            )

    def browser_go_clicked(self) -> None:
        self._load_browser_url(self.browser_addr_entry.text())

    def browser_open_external_clicked(self) -> None:
        raw = self.browser_addr_entry.text().strip() or self.link_entry.text().strip()
        if not raw:
            QMessageBox.information(self, "No link", "No URL to open.")
            return
        QDesktopServices.openUrl(QUrl.fromUserInput(self._normalize_browser_url(raw)))

    def open_link_clicked(self) -> None:
        if not self.open_link_check.isChecked():
            return
        url = self.link_entry.text().strip()
        if not url:
            QMessageBox.information(self, "No link", "No link to open. Select a row or paste a link.")
            return
        self._load_browser_url(url)

    def open_external_clicked(self) -> None:
        url = self.link_entry.text().strip()
        if not url:
            QMessageBox.information(self, "No link", "No link to open. Select a row or paste a link.")
            return
        webbrowser.open(url)

    def _on_open_link_toggle(self, enabled: bool) -> None:
        if enabled:
            self._maybe_open_link_in_browser()

    def _maybe_open_link_in_browser(self) -> None:
        if self.open_link_check.isChecked() and self.link_entry.text().strip():
            self._load_browser_url(self.link_entry.text())

    def copy_tweet_text_clicked(self) -> None:
        text = self._current_tweet_text()
        if not text:
            QMessageBox.information(self, "No text", "Enter or select a headline first.")
            return
        QApplication.clipboard().setText(text)
        self._set_status("Copied tweet text to clipboard.")

    def _validate_post_text(self, text: str, empty_message: str, action: str) -> bool:
        if not text.strip():
            QMessageBox.information(self, "No text", empty_message)
            return False
        if len(text) > 280:
            QMessageBox.critical(self, "Tweet too long", f"This text is too long to {action}.\n\nLength: {len(text)}/280")
            return False
        return True

    def post_clicked(self) -> None:
        if not self._selected_symbol:
            QMessageBox.information(self, "No symbol", "Select a symbol first.")
            return
        self._post_text(self._current_tweet_text(), self.link_entry.text().strip() or None, self.post_btn)

    def _post_text(self, text: str, link: str | None, button: QPushButton) -> None:
        if not self._validate_post_text(text, "Enter or generate text first.", "post"):
            return
        message = "This will post the text to X"
        message += " and then reply with the current URL.\n\nContinue?" if link else ".\n\nNo URL detected to reply with.\n\nContinue?"
        if QMessageBox.question(self, "Confirm post", message) != QMessageBox.StandardButton.Yes:
            return
        button.setEnabled(False)
        self._set_status("Posting to X…")

        def post() -> None:
            result = self.pc.x_post(text=text)
            if link:
                self.pc.x_post(text=link, reply_to_tweet_id=result["tweet_id"])

        def done(_value: Any) -> None:
            button.setEnabled(True)
            self._set_status("Posted successfully.")
            QMessageBox.information(self, "Done", "Post sent to X.")

        def failed(error: str) -> None:
            button.setEnabled(True)
            self._set_status("Post failed.")
            QMessageBox.critical(self, "Post failed", error)

        self._start_worker(post, done, failed)

    def schedule_clicked(self) -> None:
        if not self._selected_symbol:
            QMessageBox.information(self, "No symbol", "Select a symbol first.")
            return
        text = self._current_tweet_text()
        if self._validate_post_text(text, "Enter or select a headline first.", "schedule"):
            self._open_schedule_dialog(text, self.link_entry.text().strip() or None)

    def _open_schedule_dialog(self, tweet_text: str, link: str | None) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Schedule Post")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("This will enqueue the post and create a Windows scheduled task."))
        row = QHBoxLayout()
        layout.addLayout(row)
        row.addWidget(QLabel("Date (YYYY-MM-DD)"))
        date_edit = QDateEdit()
        date_edit.setDisplayFormat("yyyy-MM-dd")
        date_edit.setCalendarPopup(True)
        row.addWidget(date_edit)
        row.addWidget(QLabel("Time (HH:mm, 24h)"))
        time_edit = QTimeEdit()
        time_edit.setDisplayFormat("HH:mm")
        row.addWidget(time_edit)
        default = (datetime.now() + timedelta(minutes=10)).replace(second=0, microsecond=0)
        date_edit.setDate(QDate(default.year, default.month, default.day))
        time_edit.setTime(QTime(default.hour, default.minute))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        layout.addWidget(buttons)
        buttons.rejected.connect(dialog.reject)

        def accept() -> None:
            date = date_edit.date().toPyDate()
            time = time_edit.time().toPyTime()
            scheduled_time = datetime(date.year, date.month, date.day, time.hour, time.minute)
            if scheduled_time < datetime.now():
                QMessageBox.warning(dialog, "Invalid time", "Scheduled time must be in the future.")
                return
            if (
                QMessageBox.question(
                    dialog,
                    "Confirm schedule",
                    f"Schedule this post for {scheduled_time.strftime('%Y-%m-%d %H:%M')}?",
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            dialog.accept()
            self._schedule_post(tweet_text, link, scheduled_time)

        buttons.accepted.connect(accept)
        dialog.exec()

    def _schedule_post(self, tweet_text: str, link: str | None, scheduled_time: datetime) -> None:
        self.schedule_btn.setEnabled(False)
        self._set_status("Scheduling post…")

        def schedule() -> datetime:
            previous = os.getcwd()
            try:
                os.chdir(_PROJECT_ROOT)
                post = scheduled_post(headline=tweet_text, scheduled_time=scheduled_time, link=link)
                scheduler = post_scheduler()
                scheduler.enqueue_post(post)
                scheduler.task_scheduler()
            finally:
                os.chdir(previous)
            return scheduled_time

        def finish_buttons() -> None:
            self.schedule_btn.setEnabled(True)

        def done(value: Any) -> None:
            finish_buttons()
            self._set_status("Scheduled successfully.")
            QMessageBox.information(self, "Scheduled", f"Post scheduled for {value.strftime('%Y-%m-%d %H:%M')}.")

        def failed(error: str) -> None:
            finish_buttons()
            self._set_status("Scheduling failed.")
            QMessageBox.critical(self, "Scheduling failed", error)

        self._start_worker(schedule, done, failed)

    def _delete_windows_task_for_post_id(self, post_id: Any) -> None:
        if post_id is None or not str(post_id).strip():
            return
        try:
            subprocess.run(
                ["schtasks", "/Delete", "/TN", f"NewsTracker_Post_{post_id}", "/F"],
                capture_output=True,
                text=True,
                shell=True,
            )
        except Exception:
            pass

    def view_scheduled_clicked(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Scheduled posts")
        dialog.resize(980, 420)
        layout = QVBoxLayout(dialog)
        header = QHBoxLayout()
        layout.addLayout(header)
        count_label = QLabel("")
        refresh_btn = QPushButton("Refresh")
        header.addWidget(count_label, 1)
        header.addWidget(refresh_btn)
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["Post ID", "Scheduled Time", "Headline", "Link"])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table, 1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        delete_btn = QPushButton("Delete selected")
        actions.addWidget(delete_btn)
        layout.addLayout(actions)
        posts_for_rows: list[dict[str, Any]] = []

        def one_line(value: Any) -> str:
            return ("" if value is None else str(value)).replace("\r", " ").replace("\n", " ")

        def parse_time(value: Any) -> datetime | None:
            if isinstance(value, datetime):
                return value
            if not value:
                return None
            text = str(value).strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    pass
            try:
                return datetime.fromisoformat(text)
            except ValueError:
                return None

        def load_posts() -> None:
            nonlocal posts_for_rows
            try:
                previous = os.getcwd()
                try:
                    os.chdir(_PROJECT_ROOT)
                    posts = list(post_scheduler().scheduled_posts)
                finally:
                    os.chdir(previous)
            except Exception as exc:
                QMessageBox.critical(dialog, "Could not load scheduled posts", str(exc))
                posts = []
            posts_for_rows = sorted(
                [post for post in posts if isinstance(post, dict)],
                key=lambda post: (
                    0 if parse_time(post.get("scheduled_time")) else 1,
                    parse_time(post.get("scheduled_time")) or datetime.max,
                ),
            )
            table.setRowCount(len(posts_for_rows))
            for row_index, post in enumerate(posts_for_rows):
                dt = parse_time(post.get("scheduled_time"))
                values = [
                    one_line(post.get("post_id", "")),
                    dt.strftime("%Y-%m-%d %H:%M:%S") if dt else one_line(post.get("scheduled_time", "")),
                    one_line(post.get("headline", "")),
                    one_line(post.get("link", "")),
                ]
                for column, value in enumerate(values):
                    table.setItem(row_index, column, QTableWidgetItem(value))
            count_label.setText(f"{len(posts_for_rows)} scheduled post(s) in queue")

        def delete_selected() -> None:
            selected = table.selectionModel().selectedRows()
            if not selected:
                QMessageBox.information(dialog, "No selection", "Select a scheduled post to delete.")
                return
            row = selected[0].row()
            if not 0 <= row < len(posts_for_rows):
                return
            selected_post = posts_for_rows[row]
            post_id = selected_post.get("post_id")
            when = str(selected_post.get("scheduled_time") or "").strip()
            headline = str(selected_post.get("headline") or "").strip()
            if (
                QMessageBox.question(
                    dialog,
                    "Confirm delete",
                    f"Delete this scheduled post?\n\nPost ID: {post_id}\nScheduled: {when}\n\n{headline[:220]}",
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            try:
                previous = os.getcwd()
                try:
                    os.chdir(_PROJECT_ROOT)
                    scheduler = post_scheduler()
                    removed = False
                    remaining: list[dict[str, Any]] = []
                    for post in scheduler.scheduled_posts:
                        if not isinstance(post, dict):
                            continue
                        matches_id = post_id is not None and post.get("post_id") == post_id
                        matches_fallback = (
                            str(post.get("scheduled_time") or "") == when
                            and str(post.get("headline") or "") == headline
                        )
                        if not removed and (matches_id or matches_fallback):
                            removed = True
                        else:
                            remaining.append(post)
                    if not removed:
                        QMessageBox.information(
                            dialog,
                            "Not found",
                            "That scheduled post was not found in `scheduled_posts.txt`.\nIt may have already been removed.",
                        )
                        load_posts()
                        return
                    scheduler.scheduled_posts = deque(remaining)
                    scheduler.save_queue()
                    self._delete_windows_task_for_post_id(post_id)
                    try:
                        scheduler.task_scheduler()
                    except Exception:
                        pass
                finally:
                    os.chdir(previous)
            except Exception as exc:
                QMessageBox.critical(dialog, "Delete failed", str(exc))
                return
            load_posts()

        refresh_btn.clicked.connect(load_posts)
        delete_btn.clicked.connect(delete_selected)
        table.keyPressEvent = self._delete_key_handler(table.keyPressEvent, delete_selected)  # type: ignore[method-assign]
        load_posts()
        dialog.exec()

    @staticmethod
    def _delete_key_handler(original: Callable[[Any], None], delete: Callable[[], None]) -> Callable[[Any], None]:
        def handler(event: Any) -> None:
            if event.key() == Qt.Key.Key_Delete:
                delete()
            else:
                original(event)

        return handler

    def _read_updates_file_reversed(self) -> str:
        try:
            if not os.path.exists(self._updates_file_path):
                return f"(File not found)  {self._updates_file_path}"
            mtime = os.path.getmtime(self._updates_file_path)
            if self._updates_file_last_mtime == mtime:
                return ""
            self._updates_file_last_mtime = mtime
            with open(self._updates_file_path, "r", encoding="utf-8") as file:
                lines = [line.rstrip("\n") for line in file if line.strip()]
            return "  ".join(reversed(lines)) if lines else "(No updates yet)"
        except Exception as exc:
            return f"(Could not read updates)  {exc}"

    def _refresh_updates_box(self) -> None:
        text = self._read_updates_file_reversed()
        if not text:
            return
        scrollbar = self.updates_text.horizontalScrollBar()
        old_value = scrollbar.value()
        self.updates_text.setPlainText(text)
        scrollbar.setValue(old_value)

    def _load_updates_symbol(self, symbol: str) -> None:
        self.symbol_combo.setCurrentText(symbol)
        self.load_headlines_clicked()

    def closeEvent(self, event: Any) -> None:
        self._updates_timer.stop()
        self._feed_timer.stop()
        if self._workers:
            self._closing = True
            self.setEnabled(False)
            self._set_status("Waiting for background operation to finish…")
            event.ignore()
            return
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    if QWebEngineView is None:
        detail = str(_WEBENGINE_IMPORT_ERROR or "unknown import error")
        message = (
            "News Headline Poster requires PyQt6-WebEngine.\n\n"
            'Install it with:\n  pip install "PyQt6-WebEngine"\n\n'
            f"Details: {detail}"
        )
        QMessageBox.critical(None, "Missing PyQt6-WebEngine", message)
        print(message, file=sys.stderr)
        raise SystemExit(1)
    window = NewsHeadlinePosterWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
