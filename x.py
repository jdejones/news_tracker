import tweepy
from api_keys import bearer_token, consumer_key, consumer_secret, access_token, access_token_secret
from inputs import NewsImporter
import json
import datetime
from dataclasses import dataclass, asdict, field
from collections import deque
import subprocess
import os
import sys



class Post_Constructor():
    def __init__(self):
        self.news_importer = NewsImporter()
        self.news_results = None
        
    def get_news(self, provider: list|None):
        results = provider
        if isinstance(results, list):
            self.news_results = [(index, result) for index, result in enumerate(results)]
            return
        return self.news_results
    
    def x_post(self, text: str, reply_to_tweet_id: str = None) -> dict:
        """
        Create a post on X (Twitter).
        
        Parameters:
        -----------
        text : str
            The text content of the post (max 280 characters)
        reply_to_tweet_id : str, optional
            If provided, this post will be a reply to the specified tweet ID
        
        Returns:
        --------
        dict
            Response dictionary containing tweet data including tweet ID
        
        Raises:
        -------
        ValueError
            If text exceeds 280 characters
        Exception
            If API call fails
        """
        # Validate text length
        if len(text) > 280:
            raise ValueError("Post text cannot exceed 280 characters")
        
        if not text.strip():
            raise ValueError("Post text cannot be empty")
        
        # Initialize Twitter API v2 client
        client = tweepy.Client(
            bearer_token=bearer_token,
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            access_token=access_token,
            access_token_secret=access_token_secret,
            wait_on_rate_limit=True
        )
        
        try:
            # Create the post
            if reply_to_tweet_id:
                response = client.create_tweet(
                    text=text,
                    in_reply_to_tweet_id=reply_to_tweet_id
                )
            else:
                response = client.create_tweet(text=text)
            
            return {
                'success': True,
                'tweet_id': response.data['id'],
                'text': response.data['text'],
                'created_at': response.data.get('created_at')
            }
        
        except tweepy.TooManyRequests:
            raise Exception("Rate limit exceeded. Please wait before posting again.")
        except tweepy.Unauthorized:
            raise Exception("Authentication failed. Please check your API credentials.")
        except tweepy.Forbidden:
            raise Exception("You don't have permission to post. Check your API permissions.")
        except tweepy.BadRequest as e:
            raise Exception(f"Invalid request: {str(e)}")
        except Exception as e:
            raise Exception(f"Failed to create post: {str(e)}")
        
    def construct_post(self, 
                       headline: str, 
                       link: [str|None]=None, 
                       snippet: [str|None]=None,
                       symbol: str|None=None) -> str:
        if symbol:
            headline = f"${symbol} {headline}"
        if snippet:
            post = self.x_post(text=f"{headline} {snippet}")
            if link:
                self.x_post(text=f"{link}", reply_to_tweet_id=post['tweet_id'])
        else:
            post = self.x_post(text=f"{headline}")
            if link:
                self.x_post(text=f"{link}", reply_to_tweet_id=post['tweet_id'])
        return post['success']
    

@dataclass
class scheduled_post:
    headline: str
    scheduled_time: datetime.datetime
    post_id: int = len(json.load(open(r"C:\Users\jdejo\News_Tracker\scheduled_posts.txt"))) + 1
    link: str = None
    snippet: str = None
    symbol: str = None
    priority: int = 0

class post_scheduler:
    def __init__(self):
        """Initialize scheduler and load posts from file"""
        try:
            with open(r'C:\Users\jdejo\News_Tracker\scheduled_posts.txt', 'r') as f:
                content = f.read().strip()
                # Handle corrupted file (string representation of deque)
                if content.startswith('"deque') or content.startswith("'deque"):
                    # Try to extract the list from the string representation
                    try:
                        # Find the list part: deque([...])
                        start_idx = content.find('[')
                        end_idx = content.rfind(']')
                        if start_idx != -1 and end_idx != -1:
                            list_str = content[start_idx:end_idx+1]
                            # Replace single quotes with double quotes for JSON
                            list_str = list_str.replace("'", '"')
                            # Replace None with null
                            list_str = list_str.replace('None', 'null')
                            data = json.loads(list_str)
                            if isinstance(data, list):
                                self.scheduled_posts = deque(data)
                            else:
                                self.scheduled_posts = deque()
                        else:
                            self.scheduled_posts = deque()
                    except Exception:
                        # If extraction fails, initialize empty
                        self.scheduled_posts = deque()
                else:
                    # Try to load as JSON
                    try:
                        data = json.loads(content)
                        if isinstance(data, list):
                            self.scheduled_posts = deque(data)
                        else:
                            self.scheduled_posts = deque()
                    except json.JSONDecodeError:
                        self.scheduled_posts = deque()
        except FileNotFoundError:
            self.scheduled_posts = deque()
        except Exception as e:
            print(f"Warning: Error loading scheduled_posts.txt: {e}")
            self.scheduled_posts = deque()
    
    def enqueue_post(self, post: scheduled_post, priority: int = None):
        """Add a post to the back of the queue"""
        # Convert scheduled_post dataclass to dict
        if isinstance(post, scheduled_post):
            post = asdict(post)
        if isinstance(post['scheduled_time'], datetime.datetime):
            post['scheduled_time'] = post['scheduled_time'].strftime('%Y-%m-%d %H:%M:%S')
        
        if priority:
            for idx, existing_post in enumerate(self.scheduled_posts):
                if existing_post.get('priority', 0) >= priority:
                    continue
                self.scheduled_posts.insert(idx, post)
                break
            else:
                # If no lower priority found, append to end
                self.scheduled_posts.append(post)
        else:
            self.scheduled_posts.append(post)
        self.save_queue()
    
    def enqueue_posts(self, posts: list[scheduled_post]):
        """Add multiple posts to the back of the queue"""
        # Convert scheduled_post dataclasses to dicts
        post_dicts = []
        for post in posts:
            post_dict = asdict(post)
            post_dict['scheduled_time'] = post.scheduled_time.strftime('%Y-%m-%d %H:%M:%S')
            post_dicts.append(post_dict)
        self.scheduled_posts.extend(post_dicts)
        self.save_queue()
        
    def dequeue_post(self, by_index: int = 0) -> scheduled_post:
        """Remove and return the post from the front of the queue"""
        if self.is_empty():
            raise IndexError("Queue is empty")
        if by_index == 0:
            post = self.scheduled_posts.popleft()
        else:
            # Convert to list, remove item, convert back
            posts_list = list(self.scheduled_posts)
            post = posts_list.pop(by_index)
            self.scheduled_posts = deque(posts_list)
        self.save_queue()  # Save after removing
        return post
    
    def _post_to_dict(self, post: scheduled_post) -> dict:
        """Convert post to dictionary with datetime serialization"""
        post_dict = asdict(post)
        post_dict['scheduled_time'] = post.scheduled_time.strftime('%Y-%m-%d %H:%M:%S')
        return post_dict    
    
    def save_queue(self):
        """Save the current queue state to JSON file"""
        # Convert deque to list for JSON serialization
        list_of_dicts = list(self.scheduled_posts)
        # Ensure scheduled_time is a string if it's a datetime object
        for post_dict in list_of_dicts:
            if 'scheduled_time' in post_dict and isinstance(post_dict['scheduled_time'], datetime.datetime):
                post_dict['scheduled_time'] = post_dict['scheduled_time'].strftime('%Y-%m-%d %H:%M:%S')
        with open('scheduled_posts.txt', 'w') as f:
            json.dump(list_of_dicts, f, indent=2)
            
    def is_empty(self) -> bool:
        """Check if queue is empty"""
        return len(self.scheduled_posts) == 0

    def peek_post(self) -> dict:
        """View the next post without removing it"""
        if self.is_empty():
            raise IndexError("Queue is empty")
        return self.scheduled_posts[0]
    
    def task_scheduler(self):
        """
        Schedule Windows tasks for all posts in the queue.
        Each task will run scheduled_post.py with the post_id as an argument
        at the scheduled_time specified in each post.
        """
        if self.is_empty():
            print("No posts to schedule.")
            return
        
        # Get the absolute path to the script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(script_dir, 'usage', 'scripts', 'scheduled_post.py')
        script_path = os.path.normpath(script_path)
        
        # Get Python executable path
        python_exe = sys.executable
        
        scheduled_count = 0
        failed_count = 0
        
        for post_dict in self.scheduled_posts:
            try:
                # Parse scheduled_time from string if needed
                if isinstance(post_dict['scheduled_time'], str):
                    scheduled_time = datetime.datetime.strptime(
                        post_dict['scheduled_time'], 
                        '%Y-%m-%d %H:%M:%S'
                    )
                else:
                    scheduled_time = post_dict['scheduled_time']
                
                # Skip if scheduled time is in the past
                if scheduled_time < datetime.datetime.now():
                    print(f"Skipping post_id {post_dict['post_id']}: scheduled time is in the past")
                    continue
                
                # Format datetime for Windows Task Scheduler
                # Format: MM/DD/YYYY for date, HH:MM for time
                date_str = scheduled_time.strftime('%m/%d/%Y')
                time_str = scheduled_time.strftime('%H:%M')
                
                # Create unique task name
                task_name = f"NewsTracker_Post_{post_dict['post_id']}"
                
                # Build the command to run
                # Use quotes around paths to handle spaces
                # Script no longer takes post_id argument - it dequeues the first post
                # The script already changes to project root internally, so just run it directly
                command = f'"{python_exe}" "{script_path}"'
                
                # Create the schtasks command
                # Note: /SC must come before /ST and /SD for one-time tasks
                schtasks_cmd = [
                    'schtasks', '/Create',
                    '/TN', task_name,
                    '/TR', command,
                    '/SC', 'ONCE',
                    '/ST', time_str,
                    '/SD', date_str,
                    '/F'  # Force creation (overwrite if exists)
                ]
                
                # Execute the command
                result = subprocess.run(
                    schtasks_cmd,
                    capture_output=True,
                    text=True,
                    shell=True
                )
                
                if result.returncode == 0:
                    print(f"Successfully scheduled task '{task_name}' for {date_str} at {time_str}")
                    scheduled_count += 1
                else:
                    print(f"Failed to schedule task '{task_name}': {result.stderr}")
                    failed_count += 1
                    
            except Exception as e:
                print(f"Error scheduling post_id {post_dict.get('post_id', 'unknown')}: {str(e)}")
                failed_count += 1
        
        print(f"\nScheduling complete: {scheduled_count} successful, {failed_count} failed")