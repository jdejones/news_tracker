import tweepy
from api_keys import bearer_token, consumer_key, consumer_secret, access_token, access_token_secret
from inputs import NewsImporter



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
                       link: str, 
                       snippet: [str|None]=None,
                       symbol: str|None=None) -> str:
        if symbol:
            headline = f"${symbol} {headline}"
        if snippet:
            post = self.x_post(text=f"{headline} {snippet}")
            self.x_post(text=f"{link}", reply_to_tweet_id=post['tweet_id'])
        else:
            post = self.x_post(text=f"{headline}")
            self.x_post(text=f"{link}", reply_to_tweet_id=post['tweet_id'])
        return post['success']