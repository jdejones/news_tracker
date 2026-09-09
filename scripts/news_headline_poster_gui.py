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
from sqlalchemy import create_engine, inspect

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


def _safe_dt(value: Any) -> datetime | None:
    try:
        dt = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(dt) else dt.to_pydatetime()
    except Exception:
        return None


def list_news_symbols() -> list[str]:
    """Return ticker-like table names from the news database."""
    engine = create_engine(NEWS_DB_URL, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    tables = inspect(engine).get_table_names()
    excluded = {"cache_most_recent_link"}
    return sorted(
        {
            str(table).lower().strip()
            for table in tables
            if str(table).lower().strip() not in excluded
            and not str(table).lower().strip().startswith(("cache_", "tmp_"))
        }
    )


def retrieve_symbol_headlines(symbol: str) -> pd.DataFrame:
    """Read one per-symbol table from the news database."""
    sym = str(symbol).strip().lower()
    if not sym:
        raise ValueError("symbol must be non-empty")
    engine = create_engine(NEWS_DB_URL, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    return pd.read_sql(f"SELECT * FROM `{sym}`", con=engine)


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

        self._build_ui()
        self._apply_style()
        self._load_symbols_async()

        self._updates_timer = QTimer(self)
        self._updates_timer.timeout.connect(self._refresh_updates_box)
        self._updates_timer.start(3000)
        self._refresh_updates_box()

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
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
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

        ai_split = QSplitter(Qt.Orientation.Vertical)
        right_split.addWidget(ai_split)
        ai_input_group = QGroupBox("A.I. input")
        ai_input_layout = QVBoxLayout(ai_input_group)
        self.ai_input_text = QPlainTextEdit()
        ai_input_layout.addWidget(self.ai_input_text, 1)
        ai_input_buttons = QHBoxLayout()
        self.ai_copy_in_btn = QPushButton("Copy text")
        self.ai_summarize_btn = QPushButton("Summarize")
        ai_input_buttons.addWidget(self.ai_copy_in_btn)
        ai_input_buttons.addWidget(self.ai_summarize_btn)
        ai_input_buttons.addStretch(1)
        ai_input_layout.addLayout(ai_input_buttons)
        self.ai_copy_in_btn.clicked.connect(self.ai_copy_input_clicked)
        self.ai_summarize_btn.clicked.connect(self.ai_summarize_clicked)
        ai_split.addWidget(ai_input_group)

        ai_output_group = QGroupBox("A.I. output")
        ai_output_layout = QVBoxLayout(ai_output_group)
        self.ai_output_text = QPlainTextEdit()
        ai_output_layout.addWidget(self.ai_output_text, 1)
        ai_output_buttons = QHBoxLayout()
        self.ai_copy_out_btn = QPushButton("Copy text")
        self.ai_post_btn = QPushButton("Post to X")
        self.ai_schedule_btn = QPushButton("Schedule Post")
        self.ai_view_scheduled_btn = QPushButton("View Scheduled")
        for widget in (
            self.ai_copy_out_btn,
            self.ai_post_btn,
            self.ai_schedule_btn,
            self.ai_view_scheduled_btn,
        ):
            ai_output_buttons.addWidget(widget)
        ai_output_buttons.addStretch(1)
        ai_output_layout.addLayout(ai_output_buttons)
        self.ai_copy_out_btn.clicked.connect(self.ai_copy_output_clicked)
        self.ai_post_btn.clicked.connect(self.ai_post_clicked)
        self.ai_schedule_btn.clicked.connect(self.ai_schedule_clicked)
        self.ai_view_scheduled_btn.clicked.connect(self.view_scheduled_clicked)
        ai_split.addWidget(ai_output_group)

        main_split.setSizes([660, 440])
        left_split.setSizes([370, 230])
        right_split.setSizes([220, 380])
        ai_split.setSizes([190, 190])

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
        self.ai_input_text.clear()
        self.ai_output_text.clear()
        self.load_btn.setEnabled(False)
        self._set_status(f"Loading headlines for {symbol.upper()}…")

        def load() -> pd.DataFrame:
            df = retrieve_symbol_headlines(symbol)
            if "Date" in df.columns:
                df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
                if since_dt is not None:
                    df = df.loc[df["Date"] >= since_dt]
                df = df.sort_values("Date", ascending=False)
            return df.head(500)

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
                self.headline_table.setItem(row_index, column_index, QTableWidgetItem(text))
        self._set_status(f"Loaded {len(rows)} headlines for {(self._selected_symbol or '').upper()}.")

    def _on_headlines_loaded_error(self, error: str) -> None:
        self.load_btn.setEnabled(True)
        self._set_status("Failed to load headlines.")
        QMessageBox.critical(self, "DB error", f"Could not load headlines.\n\n{error}")

    def _on_row_selected(self) -> None:
        selected = self.headline_table.selectionModel().selectedRows()
        if not selected:
            return
        index = selected[0].row()
        if not 0 <= index < len(self._rows):
            return
        self._selected_row = self._rows[index]
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

    def ai_copy_input_clicked(self) -> None:
        copied = self.web.page().selectedText().strip()
        if not copied:
            copied = QApplication.clipboard().text().strip()
        if not copied:
            QMessageBox.information(self, "No text", "Highlight text in the Browser panel first.")
            return
        self.ai_input_text.setPlainText(copied)
        self._set_status("Copied text into A.I. input.")

    def _summarize_with_openai(self, copied_text: str) -> str:
        from api_keys import open_ai as oai_key

        if oai_key and not os.environ.get("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = oai_key
        from openai import OpenAI

        response = OpenAI().responses.create(
            model="gpt-5.2",
            input=f"In less than 300 characters summarize the following article:\n{copied_text}",
            reasoning={"effort": "none"},
            text={"verbosity": "low"},
        )
        summary = str(getattr(response, "output_text", "") or "")
        return summary if summary.strip() else str(response)

    def ai_summarize_clicked(self) -> None:
        copied = self.ai_input_text.toPlainText().rstrip()
        if not copied.strip():
            QMessageBox.information(self, "Nothing to summarize", "Copy some text into the A.I. input box first.")
            return
        self.ai_summarize_btn.setEnabled(False)
        self._set_status("Summarizing…")
        self._start_worker(
            lambda: self._summarize_with_openai(copied),
            self._on_summarize_done,
            self._on_summarize_error,
        )

    def _on_summarize_done(self, value: Any) -> None:
        self.ai_summarize_btn.setEnabled(True)
        self.ai_output_text.setPlainText(str(value).strip())
        self._set_status("Summary ready.")

    def _on_summarize_error(self, error: str) -> None:
        self.ai_summarize_btn.setEnabled(True)
        self._set_status("Summarize failed.")
        QMessageBox.critical(self, "Summarize failed", error)

    def copy_tweet_text_clicked(self) -> None:
        text = self._current_tweet_text()
        if not text:
            QMessageBox.information(self, "No text", "Enter or select a headline first.")
            return
        QApplication.clipboard().setText(text)
        self._set_status("Copied tweet text to clipboard.")

    def ai_copy_output_clicked(self) -> None:
        text = self.ai_output_text.toPlainText().rstrip()
        if not text:
            QMessageBox.information(self, "No text", "No A.I. output to copy yet.")
            return
        QApplication.clipboard().setText(text)
        self._set_status("Copied A.I. output to clipboard.")

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

    def ai_post_clicked(self) -> None:
        self._post_text(self.ai_output_text.toPlainText().rstrip(), self.link_entry.text().strip() or None, self.ai_post_btn)

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

    def ai_schedule_clicked(self) -> None:
        text = self.ai_output_text.toPlainText().rstrip()
        if self._validate_post_text(text, "Enter or generate A.I. output first.", "schedule"):
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
        self.ai_schedule_btn.setEnabled(False)
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
            self.ai_schedule_btn.setEnabled(True)

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
