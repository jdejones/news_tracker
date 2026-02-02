

import sys
import os
import time
import datetime

script_file = os.path.realpath(os.path.abspath(__file__))
script_dir = os.path.dirname(script_file)
project_root = os.path.dirname(script_dir)

sys.path.insert(0, project_root)
sys.path.insert(0, r"C:\Users\jdejo\Logger")



import logging
logging.basicConfig(level=logging.INFO, force=True)
# overview_logger = logging.getLogger("org_logging.overview")
from org_logging.timing import log_duration, log_timing
from org_logging import configure_logging, get_logger
run_id = configure_logging(app_name="news-tracker", log_dir=r"C:\Users\jdejo\logs")
overview_logger = get_logger("org_logging.overview", run_id=run_id)  # important: adds app/run_id fields

from Finviz_News_Processing import Controller

@log_duration(name="stock_news_update", logger=overview_logger)
def main():
    controller = Controller()
    controller._load_queue()
    start = time.monotonic()
    max_runtime_seconds = 4 * 60 * 60  # 4 hours
    print(f"Starting update at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        while True:
            if (time.monotonic() - start) >= max_runtime_seconds:
                controller._save_queue()
                print(f"\nTime limit reached (4 hours). Progress saved at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                return 0
            controller._assign_skip_status()
            symbols = controller.q.traverse()  # uses NewsQueue default threshold (90)
            if not symbols:
                break
            controller.store_symbol_news(symbols)
            controller._save_queue()
    except KeyboardInterrupt:
        # Ctrl+C: persist progress and exit cleanly.
        controller._save_queue()
        print("\nStopped by user (Ctrl+C). Progress saved.")
        return 0
    return 0

if __name__ == "__main__":
    symbols_to_update = main()
    print(f"Headlines updated at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

