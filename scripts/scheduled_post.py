"""
Script to post a scheduled tweet.
This script dequeues and posts the first item from scheduled_posts.txt.
"""
import sys
import os
import json
import datetime
from dataclasses import dataclass
from collections import deque

# Add parent directories to path to import x.py
# Use realpath to handle symlinks and ensure correct path resolution
# even when run from Windows Task Scheduler with different working directory
# Get the absolute path of this script file
# __file__ is always set when running as a script
# Use realpath to resolve any symlinks and get canonical path
# This ensures correct path resolution even when Task Scheduler
# runs the script with a different working directory
script_file = os.path.realpath(os.path.abspath(__file__))

# Get script directory and project root (two levels up)
script_dir = os.path.dirname(script_file)
project_root = os.path.dirname(os.path.dirname(script_dir))

# Validate that project_root exists and contains expected files
if not os.path.exists(project_root) or not os.path.exists(os.path.join(project_root, 'x.py')):
    raise ValueError(f"Invalid project_root: {project_root}")

sys.path.insert(0, project_root)

from x import Post_Constructor, post_scheduler, scheduled_post


def main():
    # Change to project root directory to ensure relative imports work
    # os.chdir(r"C:\Users\jdejo\News_Tracker")
    os.chdir(project_root)  
    try:
        scheduler = post_scheduler()
        
        # Check if queue is empty
        if scheduler.is_empty():
            print("Error: No posts in queue")
            sys.exit(1)
        
        # Dequeue the first post from the queue
        try:
            post_dict = scheduler.scheduled_posts.popleft()
            scheduler.save_queue()
        except IndexError:
            print("Error: Queue is empty")
            sys.exit(1)
        
        # Parse scheduled_time if it's a string
        scheduled_time = post_dict.get('scheduled_time')
        if isinstance(scheduled_time, str):
            scheduled_time = datetime.datetime.strptime(scheduled_time, '%Y-%m-%d %H:%M:%S')
        
        # Create Post_Constructor and post
        try:
            post_constructor = Post_Constructor()
            
            # Construct and post
            headline = post_dict.get('headline', '')
            link = post_dict.get('link') or ''
            snippet = post_dict.get('snippet')  # Keep None if not present
            symbol = post_dict.get('symbol')  # Keep None if not present
            
            
            # Use construct_post method
            success = post_constructor.construct_post(
                headline=headline,
                link=link,
                snippet=snippet,
                symbol=symbol
            )
            
            if not success:
                sys.exit(1)
                
        except Exception as e:
            import traceback
            traceback.print_exc()
            sys.exit(1)
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
