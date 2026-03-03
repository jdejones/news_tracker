"""
Logic controller for finviz news import, processing, and storage.
"""

from api_keys import finviz_api_key, news_database
from utils import finviz_api_urls
import requests
import pandas as pd
from io import StringIO
from dataclasses import dataclass
from typing import List, Iterator, Optional
import queue
import pickle
from pathlib import Path
from datetime import datetime, timedelta
from sqlalchemy import (
    create_engine,
    inspect,
    MetaData,
    Table,
    Column,
    String,
    DateTime,
    Text,
)
import time
import warnings
import tqdm


class FinvizNewsImporter:
    # Helper function for URLs that need symbol
    def get_finviz_url_with_symbol(self) -> str:
        """Get finviz URL that requires a symbol parameter"""
        if self.url == "stock_news":
            return f"https://elite.finviz.com/news_export.ashx?v=3&t={self.symbol}&auth={finviz_api_key}"
        elif self.url == "crypto_news":
            return f"https://elite.finviz.com/news_export.ashx?v=5&t={self.symbol}&auth={finviz_api_key}"
        else:
            raise ValueError(f"Unknown URL type: {self.url}")
        
    def __init__(self, url:str, symbol:str=None):
        self.api_key = finviz_api_key
        self.url = url
        self.symbol = symbol
        self.finviz_api_key = finviz_api_key
        if self.symbol:
            # Allow batching: symbol can be a list like ["AAPL", "MSFT"].
            # Always join lists (including length 1) to avoid URLs like "t=['AAPL']".
            if isinstance(self.symbol, list):
                self.symbol = ",".join([str(s) for s in self.symbol])
            self.url = self.get_finviz_url_with_symbol()
    

    def import_finviz_news(self):
        if self.symbol != None:
            self.symbol = self.symbol
        if not self.symbol:
            self.url = finviz_api_urls[self.url]
        self.finviz_api_key = self.finviz_api_key
        response = requests.get(self.url)
        response_text = response.content.decode('utf-8')
        df = pd.read_csv(StringIO(response_text))
        return df
        
    def __call__(self):
        return self.import_finviz_news()



class Controller:
    def __init__(self):
        self.database_url = f"mysql+pymysql://root:{news_database}@127.0.0.1:3306/news"
        self.engine = create_engine(self.database_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
        self.cache_most_recent_link = pd.read_sql("SELECT * FROM cache_most_recent_link", con=self.engine)


        # If True, symbols that aren't present in the finviz screener export (most_recent_link_all_df)
        # will be marked skippable, so the update loop can naturally drain.
        # Set to False if you prefer to always poll those symbols (may keep the loop running forever).
        self.skip_if_missing_from_screener: bool = True

        # Track when the in-memory cache dataframe needs to be persisted back to SQL.
        self._cache_dirty: bool = False
        self.most_recent_link_all_df = None
        self.q = NewsQueue()
        self.most_recent_updates = [line.strip() for line in open(Path(__file__).resolve().with_name('most_recent_updates.txt'), 'r')]

        # If the cache table is empty, seed it from the current screener export so skip logic can work.
        if len(self.cache_most_recent_link) == 0:
            try:
                self._update_most_recent_link_cached_all()
            except Exception:
                # Best-effort: script can still run without cache, but won't be able to "drain" naturally.
                pass

    def _ensure_symbol_news_table(self, symbol: str) -> None:
        """
        Ensure a per-ticker MySQL table exists with canonical column names.

        Columns:
            Title, Source, Date, Url, Category, Ticker
        """
        insp = inspect(self.engine)
        table_name = symbol.lower()
        if table_name in insp.get_table_names():
            return

        md = MetaData()
        Table(
            table_name,
            md,
            Column("Title", Text, nullable=True),
            Column("Source", String(255), nullable=True),
            Column("Date", DateTime, nullable=True),
            Column("Url", Text, nullable=True),
            Column("Category", String(255), nullable=True),
            Column("Ticker", String(32), nullable=True, index=True),
            mysql_charset="utf8mb4",
        )
        md.create_all(self.engine)

    def _most_recent_link_symbol_cached(self, symbol: str) -> str:
        sym = str(symbol).upper()
        if len(self.cache_most_recent_link) == 0:
            return ""
        mask = self.cache_most_recent_link["Ticker"].astype(str).str.upper() == sym
        if not mask.any():
            return ""
        vals = self.cache_most_recent_link.loc[mask, "News_URL"].values
        return "" if len(vals) == 0 else (vals[0] or "")
    
    def _most_recent_link_all(self) -> pd.DataFrame:
        response = requests.get(finviz_api_urls['screener'])
        response_text = response.content.decode('utf-8')
        df = pd.read_csv(StringIO(response_text))
        df = df.loc[~df['Ticker'].isna()]
        df.columns = [col.replace(' ', '_') for col in df.columns]
        self.most_recent_link_all_df = df[['Ticker', 'News_URL']]
        
    def _compare_most_recent_link(self, symbol:str) -> bool:
        #TODO Would this be more to make the comparison during initialization and store the symbol in a list?
        if self.most_recent_link_all_df is None:
            self._most_recent_link_all()
        sym = str(symbol).upper()
        cached = self._most_recent_link_symbol_cached(sym)
        # If either side is missing, treat as "not comparable" -> False (meaning: don't skip).
        if not cached:
            return False
        if self.most_recent_link_all_df is None or len(self.most_recent_link_all_df) == 0:
            return False
        mask = self.most_recent_link_all_df["Ticker"].astype(str).str.upper() == sym
        if not mask.any():
            return False
        ext_vals = self.most_recent_link_all_df.loc[mask, "News_URL"].values
        if len(ext_vals) == 0:
            return False
        external = ext_vals[0] or ""
        return cached == external
    
    def _update_most_recent_link_cached(self, symbol: str, link: str):
        """
        Upsert a single symbol's cached link into the in-memory dataframe.
        Persist with _flush_most_recent_link_cache() (batched).
        """
        sym = str(symbol).upper()
        ln = "" if link is None else str(link)

        if len(self.cache_most_recent_link) == 0:
            self.cache_most_recent_link = pd.DataFrame([{"Ticker": sym, "News_URL": ln}])
            self._cache_dirty = True
            return

        mask = self.cache_most_recent_link["Ticker"].astype(str).str.upper() == sym
        if mask.any():
            self.cache_most_recent_link.loc[mask, "News_URL"] = ln
        else:
            self.cache_most_recent_link = pd.concat(
                [self.cache_most_recent_link, pd.DataFrame([{"Ticker": sym, "News_URL": ln}])],
                ignore_index=True,
            )
        self._cache_dirty = True

    def _flush_most_recent_link_cache(self) -> None:
        """Persist in-memory cache to SQL (best-effort)."""
        if not getattr(self, "_cache_dirty", False):
            return
        try:
            self.cache_most_recent_link.to_sql(
                "cache_most_recent_link", con=self.engine, if_exists="replace", index=False
            )
            self._cache_dirty = False
        except Exception:
            # Best-effort: leave dirty so a later flush might succeed.
            pass
    
    def _update_most_recent_link_cached_all(self):
        self._most_recent_link_all()
        self.most_recent_link_all_df.to_sql("cache_most_recent_link", con=self.engine, if_exists='replace', index=False)
        # Keep in-memory cache in sync.
        self.cache_most_recent_link = self.most_recent_link_all_df.copy()
        self._cache_dirty = False

    def _load_queue(self):
        # Always load from repo root (same folder as this module).
        self.q = self.q.load_pickle(Path(__file__).resolve().with_name("news_queue.pkl"))
    
    def _save_queue(self):
        # Always save to repo root (same folder as this module).
        self.q.save_pickle(Path(__file__).resolve().with_name("news_queue.pkl"))
    
    def _assign_skip_status(self):
        if self.most_recent_link_all_df is None:
            self._most_recent_link_all()
        if self.most_recent_link_all_df is None:
            return

        screener_syms = set(self.most_recent_link_all_df["Ticker"].astype(str).str.upper().values)
        cached_syms = set(self.cache_most_recent_link["Ticker"].astype(str).str.upper().values) if len(self.cache_most_recent_link) else set()

        # Seed cache for any screener symbol missing from cache (so it can be skippable immediately).
        missing_cache_syms = screener_syms - cached_syms
        if len(missing_cache_syms) > 0:
            for sym in missing_cache_syms:
                try:
                    latest_external_link = self.most_recent_link_all_df.loc[
                        self.most_recent_link_all_df["Ticker"].astype(str).str.upper() == sym, "News_URL"
                    ].values[0]
                    self._update_most_recent_link_cached(sym, latest_external_link)
                except Exception:
                    continue
            # Best-effort flush once after seeding.
            self._flush_most_recent_link_cache()

        for node in self.q.queue:
            sym = str(node.symbol).upper()
            if self.most_recent_link_all_df.loc[self.most_recent_link_all_df.Ticker == sym].News_URL.isna().values:
                node.skip = True
                continue
            if sym not in screener_syms:
                # If the symbol isn't present in the screener export, link-based "freshness" comparison
                # can't work. Default to skipping it so the update loop can drain.
                node.skip = True if self.skip_if_missing_from_screener else False
                continue

            # Screener symbol: compare cached most-recent link vs current screener most-recent link.
            node.skip = True if self._compare_most_recent_link(sym) else False
                    
    def _get_tables(self) -> list[str]:
        return inspect(self.engine).get_table_names()

    def _get_table_exists(self, symbol: str) -> bool:
        return symbol.lower() in self._get_tables()

    def _batch_symbols_by_headline_budget(
        self,
        symbols: list[str],
        *,
        max_total_headlines: int = 90,
        max_symbols: int = 25,
    ) -> list[list[str]]:
        """
        Pack symbols into batches for the finviz API's "100 headlines total" behavior.

        We approximate the number of headlines a symbol will "consume" using the
        current `NewsNode.headline_count` in `self.q.queue`. Batches are created so
        that sum(headline_count) stays under `max_total_headlines` (best-effort).

        This is intentionally a simple, stable, single-pass packer (O(n)).
        """
        # De-dupe while preserving order.
        seen: set[str] = set()
        ordered: list[str] = []
        for s in (symbols or []):
            sym = str(s).upper()
            if sym in seen:
                continue
            seen.add(sym)
            ordered.append(sym)

        # Snapshot headline_count once under lock.
        with self.q.mutex:
            counts = {
                str(n.symbol).upper(): int(getattr(n, "headline_count", 1) or 1)
                for n in self.q.queue
            }

        def weight(sym_u: str) -> int:
            try:
                w = int(counts.get(sym_u, 1) or 1)
            except Exception:
                w = 1
            # Keep weights sane; finviz hard-caps at 100 rows returned.
            return max(1, min(100, w))

        batches: list[list[str]] = []
        cur: list[str] = []
        cur_sum = 0
        for sym_u in ordered:
            w = weight(sym_u)
            if cur and (cur_sum + w > max_total_headlines or len(cur) >= max_symbols):
                batches.append(cur)
                cur = []
                cur_sum = 0
            cur.append(sym_u)
            cur_sum += w

        if cur:
            batches.append(cur)
        return batches
    
    def store_symbol_news(self, symbols: list[str]):
        #*The logic for skipping symbols and counting headlines may cause some headlines to be missed.
        symbols = list(symbols or [])
        if len(symbols) == 0:
            return
        
        symbols = list(set(symbols))
        
        # Remove skipped symbols before calling the API.
        with self.q.mutex:
            skipped = {node.symbol.upper() for node in self.q.queue if node.skip is True}
        symbols_to_request = [s for s in symbols if str(s).upper() not in skipped]
        if len(symbols_to_request) == 0:
            return

        # One finviz API call for the whole batch (API returns max 100 rows total).
        importer = FinvizNewsImporter(url="stock_news", symbol=symbols_to_request)
        results = importer.import_finviz_news()
        results["Date"] = pd.to_datetime(results["Date"], errors="coerce")

        missing_symbols = []
        for symbol in tqdm.tqdm(symbols_to_request, desc="Processing symbols"):
            if len(results.loc[results["Ticker"] == symbol.upper()]) == 0:
                missing_symbols.append(symbol)
                continue
            
            symbol_u = symbol.upper()
            symbol_l = symbol.lower()

            if self._get_table_exists(symbol_l):
                stored_df = pd.read_sql(f"SELECT * FROM `{symbol_l}` limit 300", con=self.engine)
            else:
                self._ensure_symbol_news_table(symbol_l)
                stored_df = pd.read_sql(f"SELECT * FROM `{symbol_l}` limit 300", con=self.engine)

            symbol_results = results.loc[results["Ticker"] == symbol_u]

            daily_results = len(symbol_results.loc[symbol_results["Date"] > datetime.now() - timedelta(days=1)])
            with self.q.mutex:
                for node in self.q.queue:
                    if node.symbol.upper() == symbol_u:
                        node.headline_count = daily_results if daily_results < 100 else 100
                        if node.headline_count == 0:
                            node.headline_count = 1
                        break

            results_todb = (
                symbol_results.loc[
                    ((~symbol_results["Url"].isin(stored_df["Url"].values)) &
                     (~symbol_results["Title"].isin(stored_df["Title"].values)))
                ]
            )
            if len(results_todb) > 0:
                results_todb.to_sql(symbol_l, con=self.engine, if_exists="append", index=False)
                if len(self.most_recent_updates) >= 100:
                    self.most_recent_updates.remove(self.most_recent_updates[0])
                    self.most_recent_updates.append(symbol)
                else:
                    self.most_recent_updates.append(symbol)

            if self.most_recent_link_all_df is None:
                self._most_recent_link_all()

            if symbol_u in self.most_recent_link_all_df["Ticker"].values:
                latest_external_link = self.most_recent_link_all_df.loc[
                    self.most_recent_link_all_df["Ticker"] == symbol_u, "News_URL"
                ].values[0]
                self._update_most_recent_link_cached(symbol, latest_external_link)
            
        with open(Path(__file__).resolve().with_name('most_recent_updates.txt'), 'w') as f:
            for update in self.most_recent_updates:
                f.write(update + '\n')

        # Persist cache updates as a single write.
        self._flush_most_recent_link_cache()

        # Always persist queue to repo-root pickle.
        self._save_queue()

        # If some nodes were encountered but couldn't fit in the remaining budget during
        # traversal, fetch them individually after the batch completes.
        budget_skipped = list(getattr(self.q, "budget_skipped_symbols", set()))
        if hasattr(self.q, "budget_skipped_symbols"):
            self.q.budget_skipped_symbols.clear()

        budget_skipped = budget_skipped + missing_symbols
        
        # Respect API rate limit between requests.
        time.sleep(5)

        if len(budget_skipped) > 0:#* Was < 200. I changed it because each symbol is iterated over once & rate limits are respected. There shouldn't be that many budget skipped so didn't make sense. I haven't seen near that many budget skipped.
            # Micro-batch requests so we get more frequent DB progress while respecting
            # finviz's "100 headlines total per request" behavior.
            missing_set = {str(s).upper() for s in missing_symbols}
            budget_batches = self._batch_symbols_by_headline_budget(
                budget_skipped, max_total_headlines=90, max_symbols=25
            )
            total_budget_skipped = len(budget_skipped)
            total_budge_batches = len(budget_batches)

            for batch in tqdm.tqdm(budget_batches, desc=f"Processing {total_budget_skipped} budget skipped symbols in {total_budge_batches} batch(es)"):
                # One API call per batch, then fall back to per-symbol calls if a symbol is missing
                # from the batch response (e.g., due to truncation).
                importer = FinvizNewsImporter(url="stock_news", symbol=batch)
                batch_results = importer.import_finviz_news()
                time.sleep(5)

                if "Date" in batch_results.columns:
                    batch_results["Date"] = pd.to_datetime(batch_results["Date"], errors="coerce")

                for symbol_u in batch:
                    symbol = str(symbol_u)
                    symbol_l = symbol.lower()

                    # Skip if the node became skippable (best-effort).
                    with self.q.mutex:
                        if any((n.symbol.upper() == symbol_u and n.skip is True) for n in self.q.queue):
                            continue

                    # Prefer batch response when it contains rows for this symbol; otherwise retry individually.
                    use_batch = (
                        "Ticker" in batch_results.columns
                        and (batch_results["Ticker"].astype(str).str.upper() == symbol_u).any()
                    )
                    if use_batch:
                        single_results = batch_results
                    else:
                        importer = FinvizNewsImporter(url="stock_news", symbol=symbol_u)
                        single_results = importer.import_finviz_news()
                        time.sleep(5)

                    if "Date" not in single_results.columns:
                        for node in self.q.queue:
                            if node.symbol.upper() == symbol_u:
                                node.skip = True
                                break
                        continue

                    # Safe even if already converted (batch path).
                    single_results["Date"] = pd.to_datetime(single_results["Date"], errors="coerce")

                    if symbol_u in missing_set:
                        if len(single_results.loc[single_results["Ticker"].astype(str).str.upper() == symbol_u]) == 0:
                            for node in self.q.queue:
                                if node.symbol.upper() == symbol_u:
                                    node.skip = True
                                    break
                            continue

                    if self._get_table_exists(symbol_l):
                        stored_df = pd.read_sql(
                            f"SELECT * FROM `{symbol_l}` limit 300", con=self.engine
                        )
                    else:
                        self._ensure_symbol_news_table(symbol_l)
                        stored_df = pd.read_sql(
                            f"SELECT * FROM `{symbol_l}` limit 300", con=self.engine
                        )

                    symbol_results = single_results.loc[
                        single_results["Ticker"].astype(str).str.upper() == symbol_u
                    ]

                    daily_results = len(
                        symbol_results.loc[
                            symbol_results["Date"] > datetime.now() - timedelta(days=1)
                        ]
                    )
                    with self.q.mutex:
                        for node in self.q.queue:
                            if node.symbol.upper() == symbol_u:
                                node.headline_count = daily_results if daily_results < 100 else 100
                                if node.headline_count == 0:
                                    node.headline_count = 1
                                break

                    results_todb = (
                        symbol_results.loc[
                            ((~symbol_results["Url"].isin(stored_df["Url"].values)) |
                             (~symbol_results["Title"].isin(stored_df["Title"].values)))
                        ]
                    )
                    if len(results_todb) > 0:
                        results_todb.to_sql(symbol_l, con=self.engine, if_exists="append", index=False)
                        if len(self.most_recent_updates) >= 100:
                            self.most_recent_updates.remove(self.most_recent_updates[0])
                            self.most_recent_updates.append(symbol_u)
                        else:
                            self.most_recent_updates.append(symbol_u)

                    if self.most_recent_link_all_df is None:
                        self._most_recent_link_all()

                    if symbol_u in self.most_recent_link_all_df["Ticker"].values:
                        latest_external_link = self.most_recent_link_all_df.loc[
                            self.most_recent_link_all_df["Ticker"] == symbol_u, "News_URL"
                        ].values[0]
                        self._update_most_recent_link_cached(symbol_u, latest_external_link)

                # Persist progress once per batch (more frequent than end-of-run, cheaper than per-symbol).
                with open(Path(__file__).resolve().with_name("most_recent_updates.txt"), "w") as f:
                    for update in self.most_recent_updates:
                        f.write(update + "\n")

                # Persist cache updates as a single write (best-effort).
                self._flush_most_recent_link_cache()

                # Always persist queue to repo-root pickle.
                self._save_queue()



@dataclass
class NewsNode:
    """
    A single queue item.

    Attributes:
        symbol: unique string identifier for the node
        headline_count: integer used for traversal/budgeting
    """
    symbol: str
    headline_count: int
    skip: bool = False


class NewsQueue(queue.Queue):
    """
    FIFO queue with "circular" traversal semantics.

    - FIFO: normal enqueue uses Queue.put(), dequeue uses Queue.get().
    - Circular: during iteration/traversal, we "remove" a node by taking it from the
      front and immediately placing it back at the *beginning* (front) of the queue,
      while non-eligible nodes are rotated to the back to allow scanning.
    """

    def __init__(self, maxsize: int = 0, threshold: int = 90) -> None:
        """
        Args:
            maxsize: passed through to queue.Queue
            threshold: headline budget threshold, default is 90 (between 90 and 100)
        """
        if not (90 <= threshold <= 100):
            raise ValueError("threshold must be between 90 and 100 (inclusive).")
        super().__init__(maxsize=maxsize)

        # Sum of headline_count for nodes "removed" during the most recent traversal.
        self.iteration_headline_sum: int = 0
        # Internal container used by traverse() to stage removed symbols.
        self._staged_symbols: List[str] = []
        # Symbols encountered but not selected because they exceeded remaining budget.
        self.budget_skipped_symbols: set[str] = set()
        self.threshold: int = threshold

    def is_empty(self) -> bool:
        return self.empty()

    def enqueue(self, node: NewsNode, block: bool = True, timeout: Optional[float] = None) -> NewsNode:
        """Enqueue a NewsNode at the end (FIFO). Returns the created node."""
        self.put(node, block=block, timeout=timeout)
    
    def bulk_enqueue(self, nodes: List[NewsNode], block: bool = True, timeout: Optional[float] = None) -> List[NewsNode]:
        for node in nodes:
            self.enqueue(node, block=block, timeout=timeout)

    def dequeue(self, block: bool = True, timeout: Optional[float] = None) -> NewsNode:
        """Dequeue and return the NewsNode from the front (FIFO)."""
        return self.get(block=block, timeout=timeout)

    def __iter__(self) -> Iterator[NewsNode]:
        """
        Iterate over a snapshot of the queue contents in FIFO order.
        (Snapshot avoids mutating the live queue while iterating.)
        """
        with self.mutex:
            snapshot = list(self.queue)
        return iter(snapshot)

    def traverse(self, threshold: Optional[int] = None) -> List[str]:
        """
        Traverse by repeatedly "removing" eligible nodes using remove_node().

        Rules implemented:
        - Computes remaining budget = threshold - iteration_headline_sum
        - Only removes nodes whose headline_count is < remaining budget
        - Stores removed node symbols in an internal container
        - Returns a copy of that container, then empties it
        - Adds removed node headline_count to iteration_headline_sum
        - Resets iteration_headline_sum back to 0 when it meets/exceeds threshold

        Returns:
            List of symbol strings for nodes removed during this call.
        """
        budget_threshold = self.threshold if threshold is None else threshold
        if not (90 <= budget_threshold <= 100):
            raise ValueError("threshold must be between 90 and 100 (inclusive).")

        # This attribute is "during a traversal", so reset at the start of each call.
        self.iteration_headline_sum = 0
        self.budget_skipped_symbols.clear()

        if self.is_empty():
            return []
        
        remaining = budget_threshold - self.iteration_headline_sum
        while remaining > 5:
            remaining = budget_threshold - self.iteration_headline_sum
            if remaining <= 0:
                break

            symbol = self.remove_node(max_headline_count=remaining)
            if symbol is None:
                break
            self._staged_symbols.append(symbol)

        result = self._staged_symbols.copy()
        self._staged_symbols.clear()

        if self.iteration_headline_sum >= budget_threshold:
            self.iteration_headline_sum = 0

        return result

    def remove_node(self, max_headline_count: int) -> Optional[str]:
        """
        "Remove" a node and immediately put it back at the beginning of the queue.

        The function scans the queue (rotating non-eligible nodes to the back) and
        selects the first node whose headline_count is < max_headline_count.

        Also updates `iteration_headline_sum` by adding that node's headline_count.

        Args:
            max_headline_count: maximum allowed headline_count for the removed node.

        Returns:
            The removed node's symbol, or None if no eligible node exists.
        """
        if max_headline_count <= 0:
            return None

        with self.mutex:
            n = len(self.queue)
            if n == 0:
                return None

            for _ in range(n):
                node: NewsNode = self.queue.popleft()
                if node.skip == True:
                    self.queue.append(node)
                    continue
                # Track nodes rejected due to current remaining-budget constraints.
                if node.headline_count >= max_headline_count:
                    self.budget_skipped_symbols.add(node.symbol)
                    self.queue.append(node)
                    continue
                if node.headline_count < max_headline_count:
                    # Put the removed node back at the beginning (front).
                    self.queue.append(node)
                    if node.headline_count == 0:
                        self.iteration_headline_sum += 1
                    else:
                        self.iteration_headline_sum += node.headline_count
                    return node.symbol

                # Not eligible: rotate to back and continue scanning.
                self.queue.append(node)

            return None

    def _snapshot_items(self) -> List[NewsNode]:
        """Return a FIFO-ordered snapshot of the current queue contents."""
        with self.mutex:
            return list(self.queue)

    def save_pickle(self, file_path: str | Path = "news_queue.pkl") -> Path:
        """
        Persist this queue across sessions by pickling a serializable snapshot.

        Note: queue.Queue contains locks/condition variables and cannot be pickled
        directly; we instead pickle the queue's items + configuration.
        """
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "maxsize": self.maxsize,
            "threshold": self.threshold,
            "items": self._snapshot_items(),
        }

        with path.open("wb") as f:
            pickle.dump(payload, f)

        return path

    @classmethod
    def load_pickle(cls, file_path: str | Path = "news_queue.pkl") -> "NewsQueue":
        """Load a NewsQueue previously saved by save_pickle()."""
        path = Path(file_path)
        with path.open("rb") as f:
            payload = pickle.load(f)

        items: List[NewsNode] = payload.get("items", [])
        saved_maxsize: int = int(payload.get("maxsize", 0))
        threshold: int = int(payload.get("threshold", 95))

        # Avoid blocking on load if a saved maxsize is smaller than item count.
        maxsize = 0 if saved_maxsize == 0 else max(saved_maxsize, len(items))
        q = cls(maxsize=maxsize, threshold=threshold)
        q.bulk_enqueue(items)
        return q
