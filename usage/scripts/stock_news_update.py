

import sys
import os

script_file = os.path.realpath(os.path.abspath(__file__))
script_dir = os.path.dirname(script_file)
project_root = os.path.dirname(os.path.dirname(script_dir))

sys.path.insert(0, project_root)

from Finviz_News_Processing import Controller

def main():
    controller = Controller()
    controller._update_most_recent_link_cached_all()
    controller._load_queue()
    controller._assign_skip_status()
    controller.store_symbol_news([_.symbol for _ in controller.q.queue])
    controller._save_queue()

if __name__ == "__main__":
    main()