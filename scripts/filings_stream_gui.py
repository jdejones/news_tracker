"""
GUI for `scripts/filings_stream.py`.

Left panel:
  - live feed (like the console output)
  - links are clickable

Right panel:
  - embedded browser that loads clicked filing links

Run:
  python scripts/filings_stream_gui.py

Dependencies (install via pip):
  - PyQt6
  - PyQt6-WebEngine
  - websockets
  - colorama
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
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
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

# Allow running as: `python scripts/filings_stream_gui.py`
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts.filings_stream import load_symbol_metadata, stream_filings
from api_keys import open_ai as oai_key
from x import Post_Constructor, post_scheduler, scheduled_post


@dataclass(frozen=True)
class FilingEvent:
    ticker: str
    form_type: str
    filed_at: str
    link: str
    color: str = "default"  # "green" | "yellow" | "default"


def _now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _html_escape(s: Any) -> str:
    s = "" if s is None else str(s)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _color_to_css(color: str) -> str:
    if color == "green":
        return "#137333"  # readable green
    if color == "yellow":
        return "#b06000"  # amber/brown for contrast on white
    return "#222222"

def _color_to_bg_css(color: str) -> str:
    # Subtle backgrounds to keep the attention cues without ANSI escape codes.
    if color == "green":
        return "#e6f4ea"  # light green
    if color == "yellow":
        return "#fff4e5"  # light amber
    return "#ffffff"


class StreamWorker(QThread):
    log_line = pyqtSignal(str)  # plain text
    filing_event = pyqtSignal(dict)  # JSON-serializable dict
    status = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_evt: asyncio.Event | None = None

    def request_stop(self) -> None:
        loop = self._loop
        stop_evt = self._stop_evt
        if loop is None or stop_evt is None:
            return
        try:
            loop.call_soon_threadsafe(stop_evt.set)
        except Exception:
            pass

    def run(self) -> None:
        # Separate event loop per thread (safe on Windows)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._stop_evt = asyncio.Event()

        symbols, sectors_industries, banking_industries = load_symbol_metadata()

        def on_log(line: str) -> None:
            self.log_line.emit(line)

        def on_filing(payload: dict) -> None:
            self.filing_event.emit(payload)

        async def runner() -> None:
            try:
                self.status.emit("Connecting…")
                await stream_filings(
                    symbols=symbols,
                    sectors_industries=sectors_industries,
                    banking_industries=banking_industries,
                    on_log=on_log,
                    on_filing=on_filing,
                    stop=self._stop_evt,
                )
            finally:
                self.status.emit("Stopped")

        try:
            loop.run_until_complete(runner())
        except Exception as e:
            self.log_line.emit(f"[GUI] Stream stopped due to error: {e}")
        finally:
            try:
                loop.stop()
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            self._stop_evt = None
            self.stopped.emit()


class SummarizeWorker(QThread):
    summary_ready = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, copied_text: str) -> None:
        super().__init__()
        self._copied_text = copied_text

    def run(self) -> None:
        try:
            copied_text = self._copied_text

            # Provide API key via env var so we can keep the exact snippet below.
            if oai_key and not os.environ.get("OPENAI_API_KEY"):
                os.environ["OPENAI_API_KEY"] = oai_key

            # IMPORTANT: Keep this block exactly as specified by the user.
            from openai import OpenAI
            client = OpenAI()

            response = client.responses.create(
                model="gpt-5.2",
                input=f"In less than 300 characters summarize the follow text from an SEC filing: \n {copied_text}",
                reasoning={
                    "effort": "none"
                },
                text={
                    "verbosity": "low"
                }
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

            self.summary_ready.emit(summary)
        except AttributeError as e:
            # Most common cause: old openai package (< 1.55.0) without `client.responses`.
            msg = str(e)
            try:
                import openai  # type: ignore

                ver = getattr(openai, "__version__", "unknown")
            except Exception:
                ver = "unknown"

            if "responses" in msg:
                self.error.emit(
                    f"{msg}\n\nYour installed `openai` package is {ver} and does not support "
                    "`client.responses.create()`. Upgrade with:\n\n"
                    "  pip install -U \"openai>=1.55.0\""
                )
            else:
                self.error.emit(msg)
        except Exception as e:
            self.error.emit(str(e))


class XPostWorker(QThread):
    done = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, *, tweet_text: str, link: str | None) -> None:
        super().__init__()
        self._tweet_text = tweet_text
        self._link = link

    def run(self) -> None:
        try:
            pc = Post_Constructor()
            post = pc.x_post(text=self._tweet_text)
            if self._link:
                pc.x_post(text=self._link, reply_to_tweet_id=post["tweet_id"])
            self.done.emit()
        except Exception as e:
            self.error.emit(str(e))


class XScheduleWorker(QThread):
    done = pyqtSignal(datetime)
    error = pyqtSignal(str)

    def __init__(self, *, tweet_text: str, link: str | None, scheduled_time: datetime) -> None:
        super().__init__()
        self._tweet_text = tweet_text
        self._link = link
        self._scheduled_time = scheduled_time

    def run(self) -> None:
        try:
            prev_cwd = os.getcwd()
            try:
                os.chdir(_PROJECT_ROOT)
                post = scheduled_post(
                    headline=self._tweet_text,
                    scheduled_time=self._scheduled_time,
                    link=self._link,
                )
                ps = post_scheduler()
                ps.enqueue_post(post)
                ps.task_scheduler()
            finally:
                os.chdir(prev_cwd)
            self.done.emit(self._scheduled_time)
        except Exception as e:
            self.error.emit(str(e))


class FilingsStreamWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SEC Filings Stream — News Tracker")
        self.resize(1280, 760)

        self._worker: StreamWorker | None = None
        self._x_post_worker: XPostWorker | None = None
        self._x_schedule_worker: XScheduleWorker | None = None
        self._ticker_filter: set[str] | None = None
        self._form_filter: set[str] | None = None
        self._feed_history: list[tuple[str, Any]] = []  # ("log", str) | ("filing", dict)
        self._max_feed_history = 2000

        self._build_ui()
        self._set_running(False)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)

        # Top controls
        controls = QHBoxLayout()
        outer.addLayout(controls)

        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.clear_btn = QPushButton("Clear feed")
        self.ticker_filter_input = QLineEdit()
        self.ticker_filter_input.setPlaceholderText("ticker: AAPL MSFT, form: 4 8-K")
        self.status_label = QLabel("Ready")

        controls.addWidget(self.start_btn)
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.clear_btn)
        controls.addWidget(QLabel("Filter"))
        controls.addWidget(self.ticker_filter_input, 1)
        controls.addWidget(self.status_label)

        self.start_btn.clicked.connect(self.start_stream)
        self.stop_btn.clicked.connect(self.stop_stream)
        self.clear_btn.clicked.connect(self.clear_feed)
        self.ticker_filter_input.textChanged.connect(self._on_filter_changed)

        # Split panes
        splitter = QSplitter()
        outer.addWidget(splitter, 1)

        # Left: feed (rich text with clickable links)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.feed = QTextBrowser()
        self.feed.setOpenLinks(False)  # we handle link clicks
        self.feed.anchorClicked.connect(self._on_feed_link_clicked)
        self.feed.setStyleSheet("QTextBrowser { font-family: Segoe UI, Arial; font-size: 12px; }")

        left_layout.addWidget(self.feed, 1)

        splitter.addWidget(left)

        # Right: embedded browser with URL bar
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        url_row = QHBoxLayout()
        right_layout.addLayout(url_row)

        self.url_bar = QLineEdit()
        self.url_bar.setPlaceholderText("Enter a URL and press Enter…")
        self.go_btn = QPushButton("Go")
        self.open_external_btn = QPushButton("Open external")

        url_row.addWidget(self.url_bar, 1)
        url_row.addWidget(self.go_btn)
        url_row.addWidget(self.open_external_btn)

        # Vertical split: browser (top) + selection box (bottom)
        v_split = QSplitter(Qt.Orientation.Vertical)
        right_layout.addWidget(v_split, 1)

        self.web = QWebEngineView()
        v_split.addWidget(self.web)

        selection_panel = QWidget()
        selection_layout = QVBoxLayout(selection_panel)
        selection_layout.setContentsMargins(0, 0, 0, 0)

        selection_controls = QHBoxLayout()
        selection_layout.addLayout(selection_controls)

        selection_controls.addWidget(QLabel("Selected text:"))
        selection_controls.addStretch(1)
        self.copy_text_btn = QPushButton("Copy text")
        selection_controls.addWidget(self.copy_text_btn)
        self.summarize_btn = QPushButton("Summarize")
        selection_controls.addWidget(self.summarize_btn)

        self.selected_text_box = QPlainTextEdit()
        self.selected_text_box.setPlaceholderText(
            "Highlight text in the browser above, then click “Copy text”…"
        )
        selection_layout.addWidget(self.selected_text_box, 1)

        self.summary_text_box = QPlainTextEdit()
        self.summary_text_box.setPlaceholderText("Summary will appear here…")
        selection_layout.addWidget(QLabel("Summary:"))
        selection_layout.addWidget(self.summary_text_box, 1)

        summary_controls = QHBoxLayout()
        selection_layout.addLayout(summary_controls)

        self.copy_summary_btn = QPushButton("Copy text")
        summary_controls.addWidget(self.copy_summary_btn)

        # Post-stage controls (to the right of the summary copy button)
        self.post_x_btn = QPushButton("Post to X")
        summary_controls.addWidget(self.post_x_btn)
        self.schedule_x_btn = QPushButton("Schedule Post")
        summary_controls.addWidget(self.schedule_x_btn)
        self.view_scheduled_btn = QPushButton("View Scheduled")
        summary_controls.addWidget(self.view_scheduled_btn)

        summary_controls.addStretch(1)

        v_split.addWidget(selection_panel)
        v_split.setSizes([1, 1])  # ~50/50 height split

        self.go_btn.clicked.connect(self._go_clicked)
        self.url_bar.returnPressed.connect(self._go_clicked)
        self.open_external_btn.clicked.connect(self._open_external_clicked)
        self.copy_text_btn.clicked.connect(self._copy_selected_text_clicked)
        self.post_x_btn.clicked.connect(self._post_to_x_clicked)
        self.schedule_x_btn.clicked.connect(self._schedule_post_clicked)
        self.view_scheduled_btn.clicked.connect(self._view_scheduled_clicked)
        self.summarize_btn.clicked.connect(self._summarize_clicked)
        self.copy_summary_btn.clicked.connect(self._copy_summary_clicked)
        self.web.urlChanged.connect(lambda url: self.url_bar.setText(url.toString()))

        splitter.addWidget(right)
        splitter.setSizes([520, 760])

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.status_label.setText("Running" if running else "Ready")

    def append_log(self, line: str) -> None:
        self._push_history(("log", line))
        sb = self.feed.verticalScrollBar()
        was_at_bottom = sb.value() >= (sb.maximum() - 4)
        old_value = sb.value()

        ts = _now_str()
        safe = _html_escape(line)
        self.feed.append(f"<span style='color:#666'>[{ts}]</span> {safe}")
        # Only auto-scroll if the user was already at the bottom.
        if was_at_bottom:
            self.feed.moveCursor(QTextCursor.MoveOperation.End)
        else:
            sb.setValue(old_value)

    def append_filing(self, payload: dict) -> None:
        self._push_history(("filing", dict(payload or {})))
        try:
            ev = FilingEvent(
                ticker=str(payload.get("ticker") or ""),
                form_type=str(payload.get("form_type") or ""),
                filed_at=str(payload.get("filed_at") or ""),
                link=str(payload.get("link") or ""),
                color=str(payload.get("color") or "default"),
            )
        except Exception:
            return

        if not ev.ticker and not ev.link:
            return

        # Apply ticker filter (case-insensitive). If set, only show matching tickers.
        if self._ticker_filter is not None:
            t = (ev.ticker or "").strip().upper()
            if not t or t not in self._ticker_filter:
                return

        # Apply form filter (case-insensitive). If set, only show matching form types.
        if self._form_filter is not None:
            ft = (ev.form_type or "").strip().upper()
            if not ft or ft not in self._form_filter:
                return

        ts = _now_str()
        header = f"{_html_escape(ev.ticker)}: {_html_escape(ev.form_type)}, {_html_escape(ev.filed_at)}"
        css = _color_to_css(ev.color)
        bg = _color_to_bg_css(ev.color)
        link = _html_escape(ev.link)

        block = (
            f"<div style='margin: 6px 0 10px 0; padding: 8px 10px; background:{bg}; border-left: 5px solid {css}; border-radius: 6px;'>"
            f"<div><span style='color:#666'>[{ts}]</span> <b style='color:{css}'>{header}</b></div>"
            f"<div style='margin-left: 16px;'><a href='{link}'>{link}</a></div>"
            f"</div>"
        )
        sb = self.feed.verticalScrollBar()
        was_at_bottom = sb.value() >= (sb.maximum() - 4)
        old_value = sb.value()

        self.feed.append(block)
        # Only auto-scroll if the user was already at the bottom.
        if was_at_bottom:
            self.feed.moveCursor(QTextCursor.MoveOperation.End)
        else:
            sb.setValue(old_value)

    def clear_feed(self) -> None:
        self._feed_history.clear()
        self.feed.clear()

    def _push_history(self, item: tuple[str, Any]) -> None:
        self._feed_history.append(item)
        if len(self._feed_history) > self._max_feed_history:
            # Drop oldest in a simple FIFO manner.
            self._feed_history = self._feed_history[-self._max_feed_history :]

    def _parse_filters(self, raw: str) -> tuple[set[str] | None, set[str] | None]:
        """
        Parse a filter string like:
            ticker: AAPL MSFT, form: 4 8-K

        - Sections are comma-separated
        - Each section is "key: <whitespace-separated values>"
        - Case-insensitive matching
        - If no key is provided, the values are treated as tickers (backward compatible)
        """
        s = (raw or "").strip()
        if not s:
            return None, None

        tickers: list[str] = []
        forms: list[str] = []

        segments = [seg.strip() for seg in s.split(",") if seg.strip()]
        for seg in segments:
            if ":" in seg:
                key, val = seg.split(":", 1)
                key = key.strip().lower()
                val = val.strip()
                tokens = [p.strip().upper() for p in val.split() if p.strip()]
                if not tokens:
                    continue
                if key in ("ticker", "tickers"):
                    tickers.extend(tokens)
                elif key in ("form", "forms", "form_type", "formtype"):
                    forms.extend(tokens)
                else:
                    # Unknown key: ignore
                    continue
            else:
                # Backward compatible: treat as ticker list.
                tokens = [p.strip().upper() for p in seg.split() if p.strip()]
                tickers.extend(tokens)

        ticker_set = set(tickers) if tickers else None
        form_set = set(forms) if forms else None
        return ticker_set, form_set

    def _on_filter_changed(self, _text: str) -> None:
        self._ticker_filter, self._form_filter = self._parse_filters(self.ticker_filter_input.text())
        self._rebuild_feed_from_history()

    def _rebuild_feed_from_history(self) -> None:
        sb = self.feed.verticalScrollBar()
        was_at_bottom = sb.value() >= (sb.maximum() - 4)
        # Preserve relative scroll position when user isn't at bottom.
        old_max = max(sb.maximum(), 1)
        old_ratio = sb.value() / old_max

        self.feed.clear()

        for kind, data in self._feed_history:
            if kind == "log":
                # Re-render logs as plain log lines (timestamped at render time).
                ts = _now_str()
                safe = _html_escape(str(data))
                self.feed.append(f"<span style='color:#666'>[{ts}]</span> {safe}")
                continue

            if kind == "filing":
                payload = data if isinstance(data, dict) else {}
                try:
                    ev = FilingEvent(
                        ticker=str(payload.get("ticker") or ""),
                        form_type=str(payload.get("form_type") or ""),
                        filed_at=str(payload.get("filed_at") or ""),
                        link=str(payload.get("link") or ""),
                        color=str(payload.get("color") or "default"),
                    )
                except Exception:
                    continue

                if not ev.ticker and not ev.link:
                    continue

                if self._ticker_filter is not None:
                    t = (ev.ticker or "").strip().upper()
                    if not t or t not in self._ticker_filter:
                        continue
                if self._form_filter is not None:
                    ft = (ev.form_type or "").strip().upper()
                    if not ft or ft not in self._form_filter:
                        continue

                ts = _now_str()
                header = f"{_html_escape(ev.ticker)}: {_html_escape(ev.form_type)}, {_html_escape(ev.filed_at)}"
                css = _color_to_css(ev.color)
                bg = _color_to_bg_css(ev.color)
                link = _html_escape(ev.link)
                block = (
                    f"<div style='margin: 6px 0 10px 0; padding: 8px 10px; background:{bg}; border-left: 5px solid {css}; border-radius: 6px;'>"
                    f"<div><span style='color:#666'>[{ts}]</span> <b style='color:{css}'>{header}</b></div>"
                    f"<div style='margin-left: 16px;'><a href='{link}'>{link}</a></div>"
                    f"</div>"
                )
                self.feed.append(block)

        if was_at_bottom:
            self.feed.moveCursor(QTextCursor.MoveOperation.End)
        else:
            sb.setValue(int(old_ratio * max(sb.maximum(), 1)))

    def start_stream(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return

        self._worker = StreamWorker()
        self._worker.log_line.connect(self.append_log)
        self._worker.filing_event.connect(self.append_filing)
        self._worker.status.connect(self.status_label.setText)
        self._worker.stopped.connect(lambda: self._set_running(False))

        self._set_running(True)
        self._worker.start()

    def stop_stream(self) -> None:
        if self._worker is None:
            return
        self._worker.request_stop()

    def _on_feed_link_clicked(self, url: QUrl) -> None:
        if not url.isValid():
            return
        self.web.setUrl(url)

    def _go_clicked(self) -> None:
        raw = (self.url_bar.text() or "").strip()
        if not raw:
            return
        url = QUrl.fromUserInput(raw)
        if not url.isValid():
            QMessageBox.warning(self, "Invalid URL", f"Could not parse URL:\n\n{raw}")
            return
        self.web.setUrl(url)

    def _open_external_clicked(self) -> None:
        url = self.web.url()
        if not url.isValid() or not url.toString().strip():
            return
        QDesktopServices.openUrl(url)

    def _copy_selected_text_clicked(self) -> None:
        # QWebEngine selection retrieval is easiest via JS; fall back to selectedText().
        def _set_text(text: Any) -> None:
            txt = "" if text is None else str(text)
            if txt.strip():
                self.selected_text_box.setPlainText(txt)

        try:
            self.web.page().runJavaScript("window.getSelection().toString()", _set_text)
            return
        except Exception:
            pass

        try:
            _set_text(self.web.page().selectedText())
        except Exception:
            _set_text("")

    def _tweet_text_for_x(self) -> str:
        # Use the editable summary box as the tweet body (user can modify).
        return (self.summary_text_box.toPlainText() or "").rstrip()

    def _link_for_x(self) -> str | None:
        try:
            url = self.web.url()
            if url.isValid():
                s = (url.toString() or "").strip()
                return s or None
        except Exception:
            pass
        raw = (self.url_bar.text() or "").strip()
        return raw or None

    def _post_to_x_clicked(self) -> None:
        tweet_text = self._tweet_text_for_x()
        if not tweet_text.strip():
            QMessageBox.information(self, "No text", "Enter a summary first (or edit the Summary box).")
            return
        if len(tweet_text) > 280:
            QMessageBox.critical(
                self,
                "Tweet too long",
                f"This text is too long to post.\n\nLength: {len(tweet_text)}/280",
            )
            return

        link = self._link_for_x()
        msg = "This will post the Summary text to X"
        if link:
            msg += " and then reply with the current browser URL.\n\nContinue?"
        else:
            msg += ".\n\nNo URL detected to reply with.\n\nContinue?"
        if QMessageBox.question(self, "Confirm post", msg) != QMessageBox.StandardButton.Yes:
            return

        self.post_x_btn.setEnabled(False)
        self.status_label.setText("Posting to X…")

        worker = XPostWorker(tweet_text=tweet_text, link=link)

        def _done() -> None:
            self.post_x_btn.setEnabled(True)
            if self._worker is not None and self._worker.isRunning():
                self.status_label.setText("Running")
            else:
                self.status_label.setText("Ready")

        worker.done.connect(lambda: QMessageBox.information(self, "Done", "Post sent to X."))
        worker.done.connect(lambda: _done())
        worker.error.connect(lambda e: QMessageBox.critical(self, "Post failed", e))
        worker.error.connect(lambda _e: _done())

        self._x_post_worker = worker
        worker.start()

    def _schedule_post_clicked(self) -> None:
        tweet_text = self._tweet_text_for_x()
        if not tweet_text.strip():
            QMessageBox.information(self, "No text", "Enter a summary first (or edit the Summary box).")
            return
        if len(tweet_text) > 280:
            QMessageBox.critical(
                self,
                "Tweet too long",
                f"This text is too long to schedule.\n\nLength: {len(tweet_text)}/280",
            )
            return

        link = self._link_for_x()

        dlg = QDialog(self)
        dlg.setWindowTitle("Schedule Post")
        dlg.setModal(True)

        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("This will enqueue the post and create a Windows scheduled task."))

        row = QHBoxLayout()
        layout.addLayout(row)

        row.addWidget(QLabel("Date"))
        date_edit = QDateEdit()
        date_edit.setCalendarPopup(True)
        row.addWidget(date_edit)

        row.addWidget(QLabel("Time"))
        time_edit = QTimeEdit()
        row.addWidget(time_edit)

        default_dt = (datetime.now() + timedelta(minutes=10)).replace(second=0, microsecond=0)
        date_edit.setDate(default_dt.date())
        time_edit.setTime(default_dt.time())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        layout.addWidget(buttons)
        buttons.rejected.connect(dlg.reject)

        def _on_ok() -> None:
            d = date_edit.date().toPyDate()
            t = time_edit.time().toPyTime()
            scheduled_time = datetime(d.year, d.month, d.day, t.hour, t.minute, 0)
            if scheduled_time < datetime.now():
                QMessageBox.warning(dlg, "Invalid time", "Scheduled time must be in the future.")
                return
            if QMessageBox.question(
                dlg,
                "Confirm schedule",
                f"Schedule this post for {scheduled_time.strftime('%Y-%m-%d %H:%M')}?",
            ) != QMessageBox.StandardButton.Yes:
                return
            dlg.accept()

            self.schedule_x_btn.setEnabled(False)
            self.status_label.setText("Scheduling post…")

            worker = XScheduleWorker(tweet_text=tweet_text, link=link, scheduled_time=scheduled_time)

            def _done() -> None:
                self.schedule_x_btn.setEnabled(True)
                if self._worker is not None and self._worker.isRunning():
                    self.status_label.setText("Running")
                else:
                    self.status_label.setText("Ready")

            worker.done.connect(
                lambda dt: QMessageBox.information(
                    self, "Scheduled", f"Post scheduled for {dt.strftime('%Y-%m-%d %H:%M')}."
                )
            )
            worker.done.connect(lambda _dt: _done())
            worker.error.connect(lambda e: QMessageBox.critical(self, "Scheduling failed", e))
            worker.error.connect(lambda _e: _done())

            self._x_schedule_worker = worker
            worker.start()

        buttons.accepted.connect(_on_ok)

        dlg.exec()

    def _delete_windows_task_for_post_id(self, post_id: Any) -> None:
        if post_id is None or str(post_id).strip() == "":
            return
        task_name = f"NewsTracker_Post_{post_id}"
        try:
            subprocess.run(
                ["schtasks", "/Delete", "/TN", task_name, "/F"],
                capture_output=True,
                text=True,
                shell=True,
            )
        except Exception:
            pass

    def _view_scheduled_clicked(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Scheduled posts")
        dlg.resize(980, 420)

        layout = QVBoxLayout(dlg)

        header = QHBoxLayout()
        layout.addLayout(header)

        count_label = QLabel("")
        count_label.setStyleSheet("color:#444;")
        header.addWidget(count_label, 1)

        refresh_btn = QPushButton("Refresh")
        header.addWidget(refresh_btn)

        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["Post ID", "Scheduled Time", "Headline", "Link"])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table, 1)

        actions = QHBoxLayout()
        layout.addLayout(actions)
        actions.addStretch(1)
        delete_btn = QPushButton("Delete selected")
        actions.addWidget(delete_btn)

        posts_for_rows: list[dict[str, Any]] = []

        def display_one_line(v: Any) -> str:
            s = "" if v is None else str(v)
            return s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")

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

        def load_posts() -> None:
            nonlocal posts_for_rows
            try:
                prev_cwd = os.getcwd()
                try:
                    os.chdir(_PROJECT_ROOT)
                    ps = post_scheduler()
                    posts = list(ps.scheduled_posts)
                finally:
                    os.chdir(prev_cwd)
            except Exception as e:
                QMessageBox.critical(dlg, "Could not load scheduled posts", str(e))
                posts = []

            def sort_key(p: Any) -> tuple[int, datetime]:
                dt = parse_time(p.get("scheduled_time")) if isinstance(p, dict) else None
                return (0 if dt else 1, dt or datetime.max)

            posts_sorted = sorted([p for p in posts if isinstance(p, dict)], key=sort_key)
            posts_for_rows = posts_sorted

            table.setRowCount(len(posts_sorted))
            for r, p in enumerate(posts_sorted):
                dt = parse_time(p.get("scheduled_time"))
                dt_str = dt.strftime("%Y-%m-%d %H:%M:%S") if dt else display_one_line(p.get("scheduled_time") or "")
                vals = [
                    display_one_line(p.get("post_id", "")),
                    dt_str,
                    display_one_line(p.get("headline") or ""),
                    display_one_line(p.get("link") or ""),
                ]
                for c, v in enumerate(vals):
                    item = QTableWidgetItem(v)
                    table.setItem(r, c, item)

            count_label.setText(f"{len(posts_sorted)} scheduled post(s) in queue")

        def delete_selected() -> None:
            sel = table.selectionModel().selectedRows()
            if not sel:
                QMessageBox.information(dlg, "No selection", "Select a scheduled post to delete.")
                return
            row = sel[0].row()
            if row < 0 or row >= len(posts_for_rows):
                return
            post = posts_for_rows[row]
            post_id = post.get("post_id")
            when = str(post.get("scheduled_time") or "").strip()
            headline = str(post.get("headline") or "").strip()

            if QMessageBox.question(
                dlg,
                "Confirm delete",
                f"Delete this scheduled post?\n\nPost ID: {post_id}\nScheduled: {when}\n\n{headline[:220]}",
            ) != QMessageBox.StandardButton.Yes:
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
                        matches_id = (post_id is not None) and (p.get("post_id") == post_id)
                        matches_fallback = (str(p.get("scheduled_time") or "") == when) and (
                            str(p.get("headline") or "") == headline
                        )
                        if not removed and (matches_id or matches_fallback):
                            removed = True
                            continue
                        remaining.append(p)

                    if not removed:
                        QMessageBox.information(
                            dlg,
                            "Not found",
                            "That scheduled post was not found in `scheduled_posts.txt`.\nIt may have already been removed.",
                        )
                        load_posts()
                        return

                    ps.scheduled_posts = deque(remaining)
                    ps.save_queue()

                    self._delete_windows_task_for_post_id(post_id)
                    try:
                        ps.task_scheduler()
                    except Exception:
                        pass
                finally:
                    os.chdir(prev_cwd)
            except Exception as e:
                QMessageBox.critical(dlg, "Delete failed", str(e))
                return

            load_posts()

        refresh_btn.clicked.connect(load_posts)
        delete_btn.clicked.connect(delete_selected)
        load_posts()

        dlg.exec()

    def _summarize_clicked(self) -> None:
        copied_text = (self.selected_text_box.toPlainText() or "").strip()
        if not copied_text:
            QMessageBox.information(self, "Nothing to summarize", "Copy some text first.")
            return

        self.summarize_btn.setEnabled(False)
        self.status_label.setText("Summarizing…")

        worker = SummarizeWorker(copied_text=copied_text)

        def _done() -> None:
            self.summarize_btn.setEnabled(True)
            # Don’t overwrite stream status if running; just clear the transient message.
            if self._worker is not None and self._worker.isRunning():
                self.status_label.setText("Running")
            else:
                self.status_label.setText("Ready")

        worker.summary_ready.connect(lambda s: self.summary_text_box.setPlainText(s))
        worker.summary_ready.connect(lambda _s: _done())
        worker.error.connect(lambda msg: QMessageBox.critical(self, "Summarization error", msg))
        worker.error.connect(lambda _msg: _done())

        # Keep a reference so it isn't GC'd mid-run.
        self._summarize_worker = worker  # type: ignore[attr-defined]
        worker.start()

    def _copy_summary_clicked(self) -> None:
        txt = self.summary_text_box.toPlainText() or ""
        QApplication.clipboard().setText(txt)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        # Best-effort stop the stream thread cleanly on window close.
        try:
            self.stop_stream()
        except Exception:
            pass
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    win = FilingsStreamWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

