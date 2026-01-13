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
            return f"https://elite.finviz.com/news_export.ashx?v=3&t=symbol&auth={finviz_api_key}".replace("symbol", self.symbol)
        elif self.url == "crypto_news":
            return f"https://elite.finviz.com/news_export.ashx?v=5&t=symbol&auth={finviz_api_key}".replace("symbol", self.symbol)
        else:
            raise ValueError(f"Unknown URL type: {self.url}")
        
    def __init__(self, url:str, symbol:str=None):
        self.api_key = finviz_api_key
        self.url = url
        self.symbol = symbol
        self.finviz_api_key = finviz_api_key
        if self.symbol:
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
        self.most_recent_link_all_df = None
        self.q = NewsQueue()

    def _ensure_symbol_news_table(self, symbol: str) -> None:
        """
        Ensure a per-ticker MySQL table exists with canonical column names.

        Columns:
            Title, Source, Date, Url, Category, Ticker
        """
        insp = inspect(self.engine)
        if symbol.lower() in insp.get_table_names():
            return

        md = MetaData()
        Table(
            symbol,
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
        return self.cache_most_recent_link.loc[self.cache_most_recent_link['Ticker'] == symbol.upper(), 'News URL'].values[0]
    
    def _most_recent_link_all(self) -> pd.DataFrame:
        response = requests.get(finviz_api_urls['screener'])
        response_text = response.content.decode('utf-8')
        df = pd.read_csv(StringIO(response_text))
        self.most_recent_link_all_df = df[['Ticker', 'News URL']]
        
    def _compare_most_recent_link(self, symbol:str) -> bool:
        #TODO Would this be more to make the comparison during initialization and store the symbol in a list?
        if self.most_recent_link_all_df is None:
            self._most_recent_link_all()
        return self._most_recent_link_symbol_cached(symbol) == self.most_recent_link_all_df.loc[self.most_recent_link_all_df['Ticker'] == symbol.upper(), 'News URL'].values[0]
    
    def _update_most_recent_link_cached(self, symbol: str, link: str):
        self.cache_most_recent_link.loc[self.cache_most_recent_link['Ticker'] == symbol.upper(), 'News URL'] = link
        self.cache_most_recent_link.to_sql("cache_most_recent_link", con=self.engine, if_exists='replace', index=False)
    
    def _update_most_recent_link_cached_all(self):
        self._most_recent_link_all()
        self.most_recent_link_all_df.to_sql("cache_most_recent_link", con=self.engine, if_exists='replace', index=False)

    def _load_queue(self):
        self.q = self.q.load_pickle()
    
    def _save_queue(self):
        self.q.save_pickle()
    
    def _assign_skip_status(self):
        if self.most_recent_link_all_df is None:
            self._most_recent_link_all()
        for node in self.q.queue:
                if (node.symbol.upper() in self.most_recent_link_all_df['Ticker'].values) and (node.symbol.upper() in self.cache_most_recent_link['Ticker'].values):
                    most_recent_link = self.most_recent_link_all_df.loc[self.most_recent_link_all_df['Ticker'] == node.symbol.upper(), 'News URL'].values[0]
                    cached_link = self.cache_most_recent_link.loc[self.cache_most_recent_link['Ticker'] == node.symbol.upper(), 'News URL'].values[0]
                    if self._compare_most_recent_link(node.symbol):
                        node.skip = True
                    else:
                        node.skip = False
                else:
                    node.skip = False #*Don't skip if symbol doesn't have a most recent link.
                    
    def _get_tables(self) -> list[str]:
        return inspect(self.engine).get_table_names()

    def _get_table_exists(self, symbol: str) -> bool:
        return symbol.lower() in self._get_tables()
    
    def store_symbol_news(self, symbols: list[str]):
        block = False
        if len(symbols) > 1:
            block = True
        for symbol in tqdm.tqdm(symbols):
            #TODO Make a more effecient approach. This is a quick fix.
            skip = False
            with self.q.mutex:
                for node in self.q.queue:
                    if (node.symbol.upper() == symbol.upper()) and (node.skip == True):
                        skip = True
                        break
            if skip:
                continue
            if self._get_table_exists(symbol):
                stored_df = pd.read_sql(f"SELECT * FROM `{symbol}` limit 100", con=self.engine)
            else:
                self._ensure_symbol_news_table(symbol)
                stored_df = pd.read_sql(f"SELECT * FROM `{symbol}` limit 100", con=self.engine)
            importer = FinvizNewsImporter(url="stock_news", symbol=symbol)
            results = importer.import_finviz_news()
            results['Date'] = pd.to_datetime(results['Date'])
            daily_results = len(results.loc[results['Date'] > datetime.now() - timedelta(days=1)])
            with self.q.mutex:
                for node in self.q.queue:
                    if node.symbol.upper() == symbol.upper():
                        node.headline_count = daily_results
                        break
            self.q.save_pickle()
            results_todb = (
                results.loc[(results['Ticker'] == symbol.upper()) &
                            (~results['Url'].isin(stored_df['Url'].values))]
            )
            if len(results_todb) > 0:
                results_todb.to_sql(f"{symbol.lower()}", con=self.engine, if_exists='append', index=False)
                
            # Update internal cache ("most recent link stored internally") AFTER storing
            if self.most_recent_link_all_df is None:
                self._most_recent_link_all()

            if symbol.upper() in self.most_recent_link_all_df["Ticker"].values:
                latest_external_link = self.most_recent_link_all_df.loc[
                    self.most_recent_link_all_df["Ticker"] == symbol.upper(), "News URL"
                ].values[0]
                self._update_most_recent_link_cached(symbol, latest_external_link)                

            if block:
                time.sleep(5)



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

    def __init__(self, maxsize: int = 0, threshold: int = 95) -> None:
        """
        Args:
            maxsize: passed through to queue.Queue
            threshold: headline budget reset threshold, default is 95 (between 90 and 100)
        """
        if not (90 <= threshold <= 100):
            raise ValueError("threshold must be between 90 and 100 (inclusive).")
        super().__init__(maxsize=maxsize)

        # Sum of headline_count for nodes "removed" during the most recent traversal.
        self.iteration_headline_sum: int = 0
        # Internal container used by traverse() to stage removed symbols.
        self._staged_symbols: List[str] = []
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

        if self.is_empty():
            return ValueError("Queue is empty")
        
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
