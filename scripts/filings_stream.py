import asyncio
import contextlib
import json
import signal
import sys
import os
import websockets
from colorama import init, Fore, Style
init(autoreset=True)

DEFAULT_SYMBOLS_LOCATION = r"E:\Market Research\Studies\Sector Studies\Watchlists\High_AvgDV.txt"
DEFAULT_SECTORS_INDUSTRIES_PATH = r"E:\Market Research\Dataset\Fundamental Data\symbol_sector_industry.txt"

# Ensure project root is on sys.path when running "python scripts\filings_stream.py"
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from api_keys import sec_api_key

# Fill this in (you said you'll handle security after)
API_KEY = sec_api_key
WS_URL = f"wss://stream.sec-api.io?apiKey={API_KEY}"

def load_symbol_metadata(
    symbols_location: str = DEFAULT_SYMBOLS_LOCATION,
    sectors_industries_path: str = DEFAULT_SECTORS_INDUSTRIES_PATH,
) -> tuple[list[str], dict, set]:
    """
    Load your watchlist and sector/industry metadata used for filtering + coloring.

    Returns:
      (symbols, sectors_industries, banking_industries)

    Notes:
    - This is best-effort: if files are missing/unreadable, returns empty structures.
    - Keeping this out of module import time makes the module safe to import from a GUI.
    """
    try:
        with open(symbols_location, "r", encoding="utf-8") as f:
            symbols = [ln.strip() for ln in f.readlines() if ln.strip()]
    except Exception:
        symbols = []

    try:
        with open(sectors_industries_path, "r", encoding="utf-8") as f:
            sectors_industries = json.load(f)
    except Exception:
        sectors_industries = {}

    banking_industries = set()
    try:
        for sym in symbols:
            meta = sectors_industries.get(sym)
            if not isinstance(meta, dict):
                continue
            if meta.get("sector") == "Financial Services":
                ind = meta.get("industry")
                if ind:
                    banking_industries.add(ind)
    except Exception:
        banking_industries = set()

    return symbols, sectors_industries, banking_industries


stop_event = asyncio.Event()


def _request_stop(*_args):
    stop_event.set()


async def stream_filings(
    *,
    symbols: list[str] | None = None,
    sectors_industries: dict | None = None,
    banking_industries: set | None = None,
    on_log=None,
    on_filing=None,
    stop: asyncio.Event | None = None,
):
    """
    Stream SEC filings from sec-api websocket.

    - If `symbols` is provided and non-empty, only those tickers are emitted.
    - `on_log(line: str)` receives human-readable status lines.
    - `on_filing(payload: dict)` receives structured filing events with keys:
        ticker, form_type, filed_at, link, color
    - `stop` is an asyncio.Event used to request shutdown (defaults to module `stop_event`).
    """
    stop_evt = stop or stop_event
    symbols_set = set(symbols or [])
    sectors_industries = sectors_industries or {}
    banking_industries = banking_industries or set()
    # If a structured filing callback is provided (GUI usage), avoid also emitting
    # the CLI-friendly colored log lines for each filing (prevents duplicates + ANSI codes).
    emit_cli_filing_logs = on_filing is None

    def _log(line: str) -> None:
        if on_log is not None:
            try:
                on_log(line)
                return
            except Exception:
                # Fall back to printing if callback fails.
                pass
        print(line)

    backoff_s = 1

    while not stop_evt.is_set():
        try:
            _log(f"Connecting to Stream API")
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                _log("Connected. Awaiting new filings... (Ctrl+C to stop)")
                backoff_s = 1  # reset after successful connect

                while not stop_evt.is_set():
                    msg = await ws.recv()

                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        _log(f"[WARN] Non-JSON message: {msg!r}")
                        continue

                    # The browser example expects a list of filings
                    if isinstance(data, list):
                        for filing in data:
                            ticker = filing.get("ticker", "")
                            if symbols_set and ticker not in symbols_set:
                                continue
                            form_type = filing.get("formType", "")
                            filed_at = filing.get("filedAt", "").replace("T", " ").replace("-05:00", "")
                            link = filing.get("linkToFilingDetails", "")

                            color = "default"
                            try:
                                if (
                                    (ticker in sectors_industries)
                                    and (sectors_industries[ticker].get("industry") not in banking_industries)
                                ):
                                    color = "green"
                                elif ticker not in sectors_industries:
                                    color = "yellow"
                            except Exception:
                                color = "default"

                            # Emit structured event (preferred for GUI).
                            payload = {
                                "ticker": ticker,
                                "form_type": form_type,
                                "filed_at": filed_at,
                                "link": link,
                                "color": color,
                            }
                            if on_filing is not None:
                                try:
                                    on_filing(payload)
                                except Exception:
                                    pass

                            # Also log in a CLI-friendly way (CLI usage only).
                            if emit_cli_filing_logs:
                                if color == "green":
                                    _log(Fore.GREEN + f"{ticker}: {form_type}, {filed_at},\n {Fore.BLUE}{link}")
                                elif color == "yellow":
                                    _log(Fore.YELLOW + f"{ticker}: {form_type}, {filed_at},\n {Fore.BLUE}{link}")
                                else:
                                    _log(f"{ticker}: {form_type}, {filed_at},\n {Fore.BLUE}{link}")
                    else:
                        # Just print whatever came back
                        _log(json.dumps(data, indent=2))

        except asyncio.CancelledError:
            return
        except Exception as e:
            # The stream sometimes disconnects without a proper websocket close handshake
            # (common transient condition). Treat it as a warning so the GUI doesn't look "broken".
            msg = str(e)
            if "no close frame received or sent" in msg.lower():
                _log(f"[WARN] Connection dropped (no close frame).")
            else:
                _log(f"[ERROR] Connection/stream error: {e}")
            if stop_evt.is_set():
                break
            _log(f"Reconnecting in {backoff_s}s...")
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=backoff_s)
            except asyncio.TimeoutError:
                pass
            backoff_s = min(backoff_s * 2, 60)


async def main():
    # Graceful stop on Ctrl+C / SIGTERM (SIGTERM may not fire in all Windows scenarios)
    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_stop)

    symbols, sectors_industries, banking_industries = load_symbol_metadata()
    task = asyncio.create_task(
        stream_filings(
            symbols=symbols,
            sectors_industries=sectors_industries,
            banking_industries=banking_industries,
            stop=stop_event,
        )
    )
    await stop_event.wait()
    task.cancel()
    with contextlib.suppress(Exception):
        await task


if __name__ == "__main__":
    asyncio.run(main())