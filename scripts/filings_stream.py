import asyncio
import json
import signal
import sys
import os
import websockets
import json
from colorama import init, Fore, Style
init(autoreset=True)


symbols_location = r"E:\Market Research\Studies\Sector Studies\Watchlists\High_AvgDV.txt"
symbols = [line.strip() for line in open(symbols_location, 'r').readlines()]
sectors_industries = json.load(open(r"E:\Market Research\Dataset\Fundamental Data\symbol_sector_industry.txt"))

banking_industries = set(
    [
        sectors_industries[sym]['industry'] for sym in symbols if 
        (sym in sectors_industries) and        
        (sectors_industries[sym]['sector'] == 'Financial Services')
    ]
)

# Ensure project root is on sys.path when running "python scripts\filings_stream.py"
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from api_keys import sec_api_key

# Fill this in (you said you'll handle security after)
API_KEY = sec_api_key
WS_URL = f"wss://stream.sec-api.io?apiKey={API_KEY}"

stop_event = asyncio.Event()


def _request_stop(*_args):
    stop_event.set()


async def stream_filings():
    backoff_s = 1

    while not stop_event.is_set():
        try:
            print(f"Connecting to Stream API: {WS_URL}")
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                print("Connected. Awaiting new filings... (Ctrl+C to stop)")
                backoff_s = 1  # reset after successful connect

                while not stop_event.is_set():
                    msg = await ws.recv()

                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        print(f"[WARN] Non-JSON message: {msg!r}")
                        continue

                    # The browser example expects a list of filings
                    if isinstance(data, list):
                        for filing in data:
                            if filing.get("ticker", "") not in symbols:
                                continue
                            ticker = filing.get("ticker", "")
                            form_type = filing.get("formType", "")
                            filed_at = filing.get("filedAt", "").replace("T", " ").replace("-05:00", "")
                            link = filing.get("linkToFilingDetails", "")
                            if (ticker in sectors_industries) and (sectors_industries[ticker]['industry'] not in banking_industries):
                                print(Fore.GREEN + f"{ticker}: {form_type}, {filed_at},\n {Fore.BLUE}{link}")
                            elif ticker not in sectors_industries:
                                print(Fore.YELLOW + f"{ticker}: {form_type}, {filed_at},\n {Fore.BLUE}{link}")
                            else:
                                print(f"{ticker}: {form_type}, {filed_at},\n {Fore.BLUE}{link}")
                    else:
                        # Just print whatever came back
                        print(json.dumps(data, indent=2))

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[ERROR] Connection/stream error: {e}", file=sys.stderr)
            if stop_event.is_set():
                break
            print(f"Reconnecting in {backoff_s}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff_s)
            except asyncio.TimeoutError:
                pass
            backoff_s = min(backoff_s * 2, 60)


async def main():
    # Graceful stop on Ctrl+C / SIGTERM (SIGTERM may not fire in all Windows scenarios)
    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_stop)

    task = asyncio.create_task(stream_filings())
    await stop_event.wait()
    task.cancel()
    with contextlib.suppress(Exception):
        await task


if __name__ == "__main__":
    import contextlib

    asyncio.run(main())