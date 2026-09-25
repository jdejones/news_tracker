"""
Search and filter the consolidated ``news.stock_news`` table.

Run from the project root:
    python scripts/news_filter.py
"""
from __future__ import annotations

import csv
import os
import re
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Callable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from PyQt6.QtCore import QDate, QDateTime, QThread, QTime, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from api_keys import news_database


NEWS_DB_URL = f"mysql+pymysql://root:{news_database}@127.0.0.1:3306/news"
DISPLAY_COLUMNS = ("id", "Ticker", "Title", "Source", "Date", "Url", "Category")
SEARCH_COLUMNS = {
    "Title": ("Title",),
    "Source": ("Source",),
    "Category": ("Category",),
    "URL": ("Url",),
    "All text fields": ("Title", "Source", "Category", "Url"),
}
SORT_COLUMNS = {
    "Date": "Date",
    "Ticker": "Ticker",
    "Title": "Title",
    "Source": "Source",
    "Category": "Category",
    "ID": "id",
}


@dataclass(frozen=True)
class SearchRequest:
    query: str
    mode: str
    fields: tuple[str, ...]
    tickers: tuple[str, ...]
    source: str
    category: str
    start: datetime | None
    end: datetime | None
    case_sensitive: bool
    sort_column: str
    descending: bool
    limit: int
    similarity: int


@dataclass(frozen=True)
class NewsRow:
    news_id: int
    ticker: str
    title: str
    source: str
    published_at: datetime | None
    url: str
    category: str
    similarity: float | None = None


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _like_value(value: str) -> str:
    """Escape MySQL LIKE metacharacters while retaining substring semantics."""
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _normalized_tickers(value: str) -> tuple[str, ...]:
    tokens = re.split(r"[\s,;]+", value.strip())
    return tuple(dict.fromkeys(token.upper() for token in tokens if token and token != "*"))


def _row_search_text(row: NewsRow, fields: tuple[str, ...]) -> str:
    values = {
        "Title": row.title,
        "Source": row.source,
        "Category": row.category,
        "Url": row.url,
    }
    return " ".join(values[field] for field in fields if values.get(field))


def create_news_engine() -> Engine:
    return create_engine(
        NEWS_DB_URL,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )


def load_filter_options() -> dict[str, list[str]]:
    engine = create_news_engine()
    try:
        with engine.connect() as connection:
            tickers = [
                str(row[0])
                for row in connection.execute(
                    text("SELECT DISTINCT Ticker FROM stock_news WHERE Ticker IS NOT NULL ORDER BY Ticker")
                )
                if row[0]
            ]
            sources = [
                str(row[0])
                for row in connection.execute(
                    text("SELECT DISTINCT Source FROM stock_news WHERE Source IS NOT NULL ORDER BY Source")
                )
                if row[0]
            ]
            categories = [
                str(row[0])
                for row in connection.execute(
                    text("SELECT DISTINCT Category FROM stock_news WHERE Category IS NOT NULL ORDER BY Category")
                )
                if row[0]
            ]
        return {"tickers": tickers, "sources": sources, "categories": categories}
    finally:
        engine.dispose()


def search_news(request: SearchRequest) -> tuple[list[NewsRow], bool]:
    """Execute a parameterized, read-only search and return rows plus truncation state."""
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if request.tickers:
        names = []
        for index, ticker in enumerate(request.tickers):
            name = f"ticker_{index}"
            names.append(f":{name}")
            params[name] = ticker
        clauses.append(f"Ticker IN ({', '.join(names)})")

    if request.source:
        clauses.append("Source LIKE :source_filter ESCAPE '\\\\'")
        params["source_filter"] = _like_value(request.source)
    if request.category:
        clauses.append("Category LIKE :category_filter ESCAPE '\\\\'")
        params["category_filter"] = _like_value(request.category)
    if request.start is not None:
        clauses.append("Date >= :start_date")
        params["start_date"] = request.start
    if request.end is not None:
        clauses.append("Date <= :end_date")
        params["end_date"] = request.end

    # Similarity is calculated in Python. Fetch a bounded candidate set after
    # applying all inexpensive database filters.
    fuzzy = request.mode == "Similar"
    if request.query and not fuzzy:
        expressions = []
        for index, column in enumerate(request.fields):
            param = f"query_{index}"
            column_expr = f"BINARY `{column}`" if request.case_sensitive else f"`{column}`"
            if request.mode == "Contains":
                expressions.append(f"COALESCE({column_expr}, '') LIKE :{param} ESCAPE '\\\\'")
                params[param] = _like_value(request.query)
            elif request.mode == "Exact field":
                expressions.append(f"COALESCE({column_expr}, '') = :{param}")
                params[param] = request.query
            elif request.mode == "Regular expression":
                if request.case_sensitive:
                    expressions.append(f"CAST(COALESCE(`{column}`, '') AS BINARY) REGEXP BINARY :{param}")
                else:
                    expressions.append(f"COALESCE(`{column}`, '') REGEXP :{param}")
                params[param] = request.query
        clauses.append("(" + " OR ".join(expressions) + ")")

    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    direction = "DESC" if request.descending else "ASC"
    candidate_limit = min(max(request.limit * 20, 1000), 10000) if fuzzy else request.limit + 1
    params["row_limit"] = candidate_limit + (0 if fuzzy else 0)
    statement = text(
        "SELECT id, Ticker, Title, Source, Date, Url, Category "
        f"FROM stock_news{where_sql} "
        f"ORDER BY `{request.sort_column}` {direction}, id {direction} "
        "LIMIT :row_limit"
    )

    engine = create_news_engine()
    try:
        with engine.connect() as connection:
            records = connection.execute(statement, params).mappings().all()
    finally:
        engine.dispose()

    rows = [
        NewsRow(
            news_id=int(record["id"]),
            ticker=str(record["Ticker"] or ""),
            title=str(record["Title"] or ""),
            source=str(record["Source"] or ""),
            published_at=_to_datetime(record["Date"]),
            url=str(record["Url"] or ""),
            category=str(record["Category"] or ""),
        )
        for record in records
    ]

    if fuzzy and request.query:
        needle = request.query if request.case_sensitive else request.query.casefold()
        scored: list[NewsRow] = []
        for row in rows:
            candidate = _row_search_text(row, request.fields)
            if not request.case_sensitive:
                candidate = candidate.casefold()
            score = SequenceMatcher(None, needle, candidate).ratio() * 100
            # A matching phrase within a long headline should still rank highly.
            if needle and needle in candidate:
                score = max(score, 100.0)
            if score >= request.similarity:
                scored.append(
                    NewsRow(
                        news_id=row.news_id,
                        ticker=row.ticker,
                        title=row.title,
                        source=row.source,
                        published_at=row.published_at,
                        url=row.url,
                        category=row.category,
                        similarity=score,
                    )
                )
        scored.sort(key=lambda item: item.similarity or 0, reverse=True)
        truncated = len(scored) > request.limit or len(records) >= candidate_limit
        return scored[: request.limit], truncated

    truncated = len(rows) > request.limit
    return rows[: request.limit], truncated


class Worker(QThread):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, function: Callable[[], Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.function = function

    def run(self) -> None:
        try:
            result = self.function()
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)


class NumericItem(QTableWidgetItem):
    def __init__(self, display: str, value: int | float) -> None:
        super().__init__(display)
        self.value = value

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, NumericItem):
            return self.value < other.value
        return super().__lt__(other)


class DateItem(QTableWidgetItem):
    def __init__(self, value: datetime | None) -> None:
        super().__init__(value.strftime("%Y-%m-%d %H:%M:%S") if value else "")
        self.value = value or datetime.min

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, DateItem):
            return self.value < other.value
        return super().__lt__(other)


class NewsFilterWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("News Tracker — Search news.stock_news")
        self.resize(1450, 820)
        self._worker: Worker | None = None
        self._rows: list[NewsRow] = []
        self._build_ui()
        self._apply_style()
        self.search_button.setEnabled(False)
        self._load_options()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        filters = QGroupBox("Search filters")
        grid = QGridLayout(filters)

        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("Search title, source, category, or URL")
        self.query_edit.setClearButtonEnabled(True)
        self.query_edit.returnPressed.connect(self.run_search)
        grid.addWidget(QLabel("Keywords / pattern"), 0, 0)
        grid.addWidget(self.query_edit, 0, 1, 1, 3)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Contains", "Exact field", "Similar", "Regular expression"])
        self.mode_combo.currentTextChanged.connect(self._mode_changed)
        grid.addWidget(QLabel("Match"), 0, 4)
        grid.addWidget(self.mode_combo, 0, 5)

        self.field_combo = QComboBox()
        self.field_combo.addItems(SEARCH_COLUMNS)
        grid.addWidget(QLabel("Search in"), 0, 6)
        grid.addWidget(self.field_combo, 0, 7)

        self.case_check = QCheckBox("Case sensitive")
        grid.addWidget(self.case_check, 0, 8)

        self.ticker_edit = QLineEdit()
        self.ticker_edit.setPlaceholderText("All, or e.g. AAPL, MSFT")
        self.ticker_edit.setClearButtonEnabled(True)
        grid.addWidget(QLabel("Stocks"), 1, 0)
        grid.addWidget(self.ticker_edit, 1, 1)

        self.ticker_combo = QComboBox()
        self.ticker_combo.addItem("Add stock…")
        self.ticker_combo.currentTextChanged.connect(self._add_ticker)
        grid.addWidget(self.ticker_combo, 1, 2)

        self.source_combo = QComboBox()
        self.source_combo.setEditable(True)
        self.source_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.source_combo.addItem("")
        self.source_combo.lineEdit().setPlaceholderText("Any source")
        grid.addWidget(QLabel("Source contains"), 1, 3)
        grid.addWidget(self.source_combo, 1, 4)

        self.category_combo = QComboBox()
        self.category_combo.setEditable(True)
        self.category_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.category_combo.addItem("")
        self.category_combo.lineEdit().setPlaceholderText("Any category")
        grid.addWidget(QLabel("Category contains"), 1, 5)
        grid.addWidget(self.category_combo, 1, 6, 1, 2)

        self.start_check = QCheckBox("From")
        self.start_edit = QDateTimeEdit()
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.start_edit.setDateTime(QDateTime(QDate.currentDate().addMonths(-1), QTime(0, 0)))
        self.start_edit.setEnabled(False)
        self.start_check.toggled.connect(self.start_edit.setEnabled)
        grid.addWidget(self.start_check, 2, 0)
        grid.addWidget(self.start_edit, 2, 1, 1, 2)

        self.end_check = QCheckBox("To")
        self.end_edit = QDateTimeEdit()
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.end_edit.setDateTime(QDateTime.currentDateTime())
        self.end_edit.setEnabled(False)
        self.end_check.toggled.connect(self.end_edit.setEnabled)
        grid.addWidget(self.end_check, 2, 3)
        grid.addWidget(self.end_edit, 2, 4)

        self.similarity_label = QLabel("Similarity ≥")
        self.similarity_spin = QSpinBox()
        self.similarity_spin.setRange(1, 100)
        self.similarity_spin.setValue(55)
        self.similarity_spin.setSuffix("%")
        self.similarity_label.setVisible(False)
        self.similarity_spin.setVisible(False)
        grid.addWidget(self.similarity_label, 2, 5)
        grid.addWidget(self.similarity_spin, 2, 6)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(SORT_COLUMNS)
        grid.addWidget(QLabel("Sort by"), 3, 0)
        grid.addWidget(self.sort_combo, 3, 1)

        self.direction_combo = QComboBox()
        self.direction_combo.addItems(["Newest / Z–A", "Oldest / A–Z"])
        grid.addWidget(self.direction_combo, 3, 2)

        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 5000)
        self.limit_spin.setValue(500)
        self.limit_spin.setSingleStep(100)
        grid.addWidget(QLabel("Maximum rows"), 3, 3)
        grid.addWidget(self.limit_spin, 3, 4)

        self.search_button = QPushButton("Search")
        self.search_button.setDefault(True)
        self.search_button.clicked.connect(self.run_search)
        self.clear_button = QPushButton("Clear filters")
        self.clear_button.clicked.connect(self.clear_filters)
        self.export_button = QPushButton("Export CSV…")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_csv)
        grid.addWidget(self.search_button, 3, 6)
        grid.addWidget(self.clear_button, 3, 7)
        grid.addWidget(self.export_button, 3, 8)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(7, 2)
        outer.addWidget(filters)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Date", "Ticker", "Title", "Source", "Category", "URL", "ID", "Match"]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 75)
        self.table.setColumnWidth(2, 510)
        self.table.setColumnWidth(3, 140)
        self.table.setColumnWidth(4, 120)
        self.table.setColumnWidth(5, 280)
        self.table.setColumnWidth(6, 80)
        self.table.setColumnWidth(7, 75)
        self.table.cellDoubleClicked.connect(self.open_selected_url)
        outer.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.open_button = QPushButton("Open selected URL")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_selected_url)
        self.copy_button = QPushButton("Copy selected rows")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_selected)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Loading filter options…")

        search_action = QAction(self)
        search_action.setShortcut(QKeySequence("Ctrl+Return"))
        search_action.triggered.connect(self.run_search)
        self.addAction(search_action)
        copy_action = QAction(self)
        copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        copy_action.triggered.connect(self.copy_selected)
        self.addAction(copy_action)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget { font-family: "Segoe UI"; font-size: 10pt; }
            QMainWindow { background: #f5f7fa; }
            QGroupBox { font-weight: 600; margin-top: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
            QLineEdit, QComboBox, QDateTimeEdit, QSpinBox, QTableWidget {
                background: white; color: #202124;
            }
            QPushButton { padding: 5px 10px; }
            """
        )

    def _run_worker(
        self,
        function: Callable[[], Any],
        success: Callable[[Any], None],
        failure: Callable[[str], None],
    ) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self._worker = Worker(function, self)
        self._worker.succeeded.connect(success)
        self._worker.failed.connect(failure)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _worker_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    def _load_options(self) -> None:
        self._run_worker(load_filter_options, self._options_loaded, self._options_failed)

    def _options_loaded(self, options: dict[str, list[str]]) -> None:
        self.ticker_combo.blockSignals(True)
        self.ticker_combo.addItems(options["tickers"])
        self.ticker_combo.setCurrentIndex(0)
        self.ticker_combo.blockSignals(False)
        self.source_combo.addItems(options["sources"])
        self.category_combo.addItems(options["categories"])
        self.statusBar().showMessage(
            f"Ready — {len(options['tickers']):,} stocks, "
            f"{len(options['sources']):,} sources, {len(options['categories']):,} categories"
        )
        self.search_button.setEnabled(True)

    def _options_failed(self, error: str) -> None:
        self.search_button.setEnabled(True)
        self.statusBar().showMessage("Could not load filter options. Manual searches are still available.")
        QMessageBox.warning(self, "Database connection", f"Could not load filter options:\n\n{error}")

    def _mode_changed(self, mode: str) -> None:
        fuzzy = mode == "Similar"
        self.similarity_label.setVisible(fuzzy)
        self.similarity_spin.setVisible(fuzzy)
        placeholders = {
            "Contains": "Text contained anywhere in the selected field(s)",
            "Exact field": "Entire selected field must equal this text",
            "Similar": "Find approximately matching text",
            "Regular expression": r"Database regex, e.g. \b(FDA|approval)\b",
        }
        self.query_edit.setPlaceholderText(placeholders[mode])

    def _add_ticker(self, ticker: str) -> None:
        if not ticker or ticker == "Add stock…":
            return
        current = list(_normalized_tickers(self.ticker_edit.text()))
        if ticker.upper() not in current:
            current.append(ticker.upper())
            self.ticker_edit.setText(", ".join(current))
        self.ticker_combo.blockSignals(True)
        self.ticker_combo.setCurrentIndex(0)
        self.ticker_combo.blockSignals(False)

    def _request(self) -> SearchRequest:
        start = self.start_edit.dateTime().toPyDateTime() if self.start_check.isChecked() else None
        end = self.end_edit.dateTime().toPyDateTime() if self.end_check.isChecked() else None
        if start is not None and end is not None and start > end:
            raise ValueError("'From' must be earlier than 'To'.")
        query = self.query_edit.text().strip()
        mode = self.mode_combo.currentText()
        return SearchRequest(
            query=query,
            mode=mode,
            fields=SEARCH_COLUMNS[self.field_combo.currentText()],
            tickers=_normalized_tickers(self.ticker_edit.text()),
            source=self.source_combo.currentText().strip(),
            category=self.category_combo.currentText().strip(),
            start=start,
            end=end,
            case_sensitive=self.case_check.isChecked(),
            sort_column=SORT_COLUMNS[self.sort_combo.currentText()],
            descending=self.direction_combo.currentIndex() == 0,
            limit=self.limit_spin.value(),
            similarity=self.similarity_spin.value(),
        )

    def run_search(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        try:
            request = self._request()
        except ValueError as exc:
            QMessageBox.information(self, "Invalid filter", str(exc))
            return
        self.search_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        self.statusBar().showMessage("Searching news.stock_news…")
        self._run_worker(lambda: search_news(request), self._search_loaded, self._search_failed)

    def _search_loaded(self, result: tuple[list[NewsRow], bool]) -> None:
        rows, truncated = result
        self._rows = rows
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values: list[QTableWidgetItem] = [
                DateItem(row.published_at),
                QTableWidgetItem(row.ticker),
                QTableWidgetItem(row.title),
                QTableWidgetItem(row.source),
                QTableWidgetItem(row.category),
                QTableWidgetItem(row.url),
                NumericItem(str(row.news_id), row.news_id),
                NumericItem(f"{row.similarity:.1f}%" if row.similarity is not None else "", row.similarity or -1),
            ]
            for column, item in enumerate(values):
                item.setData(Qt.ItemDataRole.UserRole, row)
                item.setToolTip(item.text())
                self.table.setItem(row_index, column, item)
        self.table.setSortingEnabled(True)
        self.table.resizeRowsToContents()
        self.search_button.setEnabled(True)
        self.clear_button.setEnabled(True)
        self.export_button.setEnabled(bool(rows))
        suffix = " (result or fuzzy candidate limit reached)" if truncated else ""
        self.statusBar().showMessage(f"Found {len(rows):,} row(s){suffix}.")

    def _search_failed(self, error: str) -> None:
        self.search_button.setEnabled(True)
        self.clear_button.setEnabled(True)
        self.statusBar().showMessage("Search failed.")
        message = error
        if self.mode_combo.currentText() == "Regular expression":
            message += "\n\nCheck that the pattern uses MySQL regular-expression syntax."
        QMessageBox.critical(self, "Search failed", message)

    def clear_filters(self) -> None:
        self.query_edit.clear()
        self.mode_combo.setCurrentIndex(0)
        self.field_combo.setCurrentIndex(0)
        self.case_check.setChecked(False)
        self.ticker_edit.clear()
        self.source_combo.setCurrentIndex(0)
        self.source_combo.setEditText("")
        self.category_combo.setCurrentIndex(0)
        self.category_combo.setEditText("")
        self.start_check.setChecked(False)
        self.end_check.setChecked(False)
        self.sort_combo.setCurrentIndex(0)
        self.direction_combo.setCurrentIndex(0)
        self.limit_spin.setValue(500)
        self.similarity_spin.setValue(55)
        self.query_edit.setFocus()
        self.statusBar().showMessage("Filters cleared.")

    def _selected_row_objects(self) -> list[NewsRow]:
        selected_indexes = self.table.selectionModel().selectedRows()
        rows: list[NewsRow] = []
        for index in selected_indexes:
            item = self.table.item(index.row(), 0)
            value = item.data(Qt.ItemDataRole.UserRole) if item else None
            if isinstance(value, NewsRow):
                rows.append(value)
        return rows

    def _selection_changed(self) -> None:
        selected = self._selected_row_objects()
        self.copy_button.setEnabled(bool(selected))
        self.open_button.setEnabled(any(row.url for row in selected))

    def open_selected_url(self, *_args: Any) -> None:
        selected = self._selected_row_objects()
        if not selected:
            current = self.table.currentRow()
            if current >= 0:
                item = self.table.item(current, 0)
                row = item.data(Qt.ItemDataRole.UserRole) if item else None
                selected = [row] if isinstance(row, NewsRow) else []
        urls = [row.url for row in selected if row.url]
        if not urls:
            QMessageBox.information(self, "No URL", "The selected row does not contain a URL.")
            return
        for url in urls[:10]:
            webbrowser.open(url)
        if len(urls) > 10:
            self.statusBar().showMessage("Opened the first 10 selected URLs.")

    def copy_selected(self) -> None:
        selected_rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not selected_rows:
            return
        lines = ["\t".join(self.table.horizontalHeaderItem(column).text() for column in range(8))]
        for row in selected_rows:
            lines.append("\t".join((self.table.item(row, column) or QTableWidgetItem()).text() for column in range(8)))
        QApplication.clipboard().setText("\n".join(lines))
        self.statusBar().showMessage(f"Copied {len(selected_rows):,} row(s).")

    def export_csv(self) -> None:
        if not self._rows:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export search results",
            "news_search_results.csv",
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file)
                writer.writerow(["Date", "Ticker", "Title", "Source", "Category", "URL", "ID", "Match"])
                for row in self._rows:
                    writer.writerow(
                        [
                            row.published_at.isoformat(sep=" ") if row.published_at else "",
                            row.ticker,
                            row.title,
                            row.source,
                            row.category,
                            row.url,
                            row.news_id,
                            f"{row.similarity:.1f}%" if row.similarity is not None else "",
                        ]
                    )
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.statusBar().showMessage(f"Exported {len(self._rows):,} row(s) to {path}")


def main() -> None:
    app = QApplication(sys.argv)
    window = NewsFilterWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
