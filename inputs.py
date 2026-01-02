import nltk
nltk.download('punkt_tab')
from nltk.tokenize import word_tokenize
from nltk.probability import FreqDist
from nltk.stem import PorterStemmer
from nltk.stem.wordnet import WordNetLemmatizer
from utils import *
from sources import *
from nltk.sentiment import SentimentIntensityAnalyzer
import pandas as pd
from api_keys import polygon_api_key, serpapi_api_key
from polygon.rest import RESTClient
from finvizfinance.quote import finvizfinance
import serpapi
pd.options.display.max_colwidth = 100


class NewsImporter:
    def __init__(self):
        self.link_titles_src1 = []
        self.link_titles_src1_for_df = []
        self.link_titles_src2 = []
        self.link_titles_src2_for_df = []
        self.link_titles_src3 = []
        self.link_titles_src3_for_df = []
        self.link_titles_src4 = []
        self.link_titles_src4_for_df = []
        self.link_titles_src5 = []
        self.link_titles_src5_for_df = []
        self.link_titles_src6 = []
        self.link_titles_src6_for_df = []
        self.link_titles_src7 = []
        self.link_titles_src7_for_df = []
        self.link_titles_src8 = []
        self.link_titles_src8_for_df = []
        self.link_titles_src9 = []
        self.link_titles_src9_for_df = []
        self.link_titles_src10 = []
        self.link_titles_src10_for_df = []
        self.link_titles_src11 = []
        self.link_titles_src11_for_df = []
        self.link_titles_src12 = []
        self.link_titles_src12_for_df = []
        self.link_titles_src13 = []
        self.link_titles_src13_for_df = []
        self.link_titles_all = []
        self.links_for_df = []
        self.links_df = []
        self.tokenized_word = []
        self.filtered_words = []
        self.stop_words_file = []
        self.stop_words_edited = []
        self.ps = []
        self.stemmed_words = []
        self.lem = []
        self.lemmed_words = []
        self.fdist_stem = []
        self.fdist_stem_dict = []
        self.fdist_lem = []
        self.fdist_lem_dict = []
        self.bigrams_lemmed = []
        self.bigrams_stemmed = []
        self.fdist_bigrams_lemmed = []
        self.fdist_bigrams_lemmed_dict = []
        self.fdist_bigrams_stemmed = []
        self.fdist_bigrams_stemmed_dict = []
        self.bigrams_lemmed_dict = []
        self.bi_stem = []
        self.bi_lem = []
        self.trigrams_lemmed = []
        self.trigrams_stemmed = []
        self.fdist_trigrams_lemmed = []
        self.fdist_trigrams_lemmed_dict = []
        self.fdist_trigrams_stemmed = []
        self.fdist_trigrams_stemmed_dict = []
        self.trigrams_lemmed_dict = []
        self.tri_lem = []
        self.tri_stem = []
        self.link_titles_all_set = []
        self.polarity_scores = []
        self.headlines = []
    
    def frontpage_headlines(self, clean_headlines: bool = True):
        #src1 news scraper
        source = src1
        soup = basic_bsoup(source)
        containers = soup.findAll("a", {"class" : "nn-tab-link"})
        self.link_titles_src1 = []
        self.link_titles_src1_for_df = []
        for container in containers:
            self.link_titles_src1.append(container.contents)
            self.link_titles_src1_for_df.append((str(container.string), container['href'], 'src1'))
        flatten_list(self.link_titles_src1)
        self.link_titles_src1 = []
        for element in flatten_list.flattened:
            self.link_titles_src1.append(element.strip())   
        ###############################################################################

        ###############################################################################
        #src2 News scraper 
        source = src2
        soup = basic_bsoup(source)
        containers = soup.findAll("a", {"class" : "Card-title"})
        self.link_titles_src2 = []
        self.link_titles_src2_for_df = []
        for container in containers:
            self.link_titles_src2.append(container.string)
            self.link_titles_src2_for_df.append((str(container.string), container['href'], 'src2 Business'))
        Not_none_values = filter(None.__ne__, self.link_titles_src2)
        self.link_titles_src2 = list(Not_none_values)
        ###############################################################################

        ###############################################################################
        #src3 News scraper
        source = src3
        soup = basic_bsoup(source)
        containers = soup.findAll("a", {"class" : "Card-title"})
        self.link_titles_src3 = []
        self.link_titles_src3_for_df = []
        for container in containers:
            self.link_titles_src3.append(container.string)
            self.link_titles_src3_for_df.append((str(container.string), container['href'], 'src3 Economy'))
        Not_none_values = filter(None.__ne__, self.link_titles_src3)
        self.link_titles_src3 = list(Not_none_values)
        ###############################################################################

        ###############################################################################
        #src4 News scraper
        source = src4
        soup = basic_bsoup(source)
        containers = soup.findAll("a", {"class" : "Card-title"})
        self.link_titles_src4 = []
        self.link_titles_src4_for_df = []
        for container in containers:
            self.link_titles_src4.append(container.string)
            self.link_titles_src4_for_df.append((str(container.string), container['href'], 'src4 Finance'))
        Not_none_values = filter(None.__ne__, self.link_titles_src4)
        self.link_titles_src4 = list(Not_none_values)
        ###############################################################################

        ###############################################################################
        #src5 News scraper
        source = src5
        soup = basic_bsoup(source)
        containers = soup.findAll("a", {"class" : "Card-title"})
        self.link_titles_src5 = []
        self.link_titles_src5_for_df = []
        for container in containers:
            self.link_titles_src5.append(container.string)
            self.link_titles_src5_for_df.append((str(container.string), container['href'], 'src5 Health and Science'))
        Not_none_values = filter(None.__ne__, self.link_titles_src5)
        self.link_titles_src5 = list(Not_none_values)
        ###############################################################################

        ###############################################################################
        #src6 News scraper
        source = src6
        soup = basic_bsoup(source)
        containers = soup.findAll("a", {"class" : "Card-title"})
        self.link_titles_src6 = []
        self.link_titles_src6_for_df = []
        for container in containers:
            self.link_titles_src6.append(container.string)
            self.link_titles_src6_for_df.append((str(container.string), container['href'], 'src6 Real Estate'))
        Not_none_values = filter(None.__ne__, self.link_titles_src6)
        self.link_titles_src6 = list(Not_none_values)
        ###############################################################################

        ###############################################################################
        #src7 News scraper
        source = src7
        soup = basic_bsoup(source)
        containers = soup.findAll("a", {"class" : "Card-title"})
        self.link_titles_src7 = []
        self.link_titles_src7_for_df = []
        for container in containers:
            self.link_titles_src7.append(container.string)
            self.link_titles_src7_for_df.append((str(container.string), container['href'], 'src7 Energy'))
        Not_none_values = filter(None.__ne__, self.link_titles_src7)
        self.link_titles_src7 = list(Not_none_values)
        ###############################################################################

        ###############################################################################
        #src8 News scraper
        source = src8
        soup = basic_bsoup(source)
        containers = soup.findAll("a", {"class" : "Card-title"})
        self.link_titles_src8 = []
        self.link_titles_src8_for_df = []
        for container in containers:
            self.link_titles_src8.append(container.string)
            self.link_titles_src8_for_df.append((str(container.string), container['href'], 'src8 Transportation'))
        Not_none_values = filter(None.__ne__, self.link_titles_src8)
        self.link_titles_src8 = list(Not_none_values)
        ###############################################################################

        ###############################################################################
        #src9 News scraper
        source = src9
        soup = basic_bsoup(source)
        containers = soup.findAll("a", {"class" : "Card-title"})
        self.link_titles_src9 = []
        self.link_titles_src9_for_df = []
        for container in containers:
            self.link_titles_src9.append(container.string)
            self.link_titles_src9_for_df.append((str(container.string), container['href'], 'src9 Industrials'))
        Not_none_values = filter(None.__ne__, self.link_titles_src9)
        self.link_titles_src9 = list(Not_none_values)
        ###############################################################################

        ###############################################################################
        #src10 News scraper
        source = src10
        soup = basic_bsoup(source)
        containers = soup.findAll("a", {"class" : "Card-title"})
        self.link_titles_src10 = []
        self.link_titles_src10_for_df = []
        for container in containers:
            self.link_titles_src10.append(container.string)
            self.link_titles_src10_for_df.append((str(container.string), container['href'], 'src10 Retail'))
        Not_none_values = filter(None.__ne__, self.link_titles_src10)
        self.link_titles_src10 = list(Not_none_values)
        ###############################################################################

        ###############################################################################
        #src11 News scraper
        #The beginning and end of this may be able to be trimmed-2/11/22
        source = src11
        soup = basic_bsoup(source)
        containers = soup.findAll("a")
        self.link_titles_src11 = []
        self.link_titles_src11_for_df = []
        for container in containers:
            try:
                self.link_titles_src11.append(container.string)
                if container.string == None:
                    continue
                self.link_titles_src11_for_df.append((str(container.string), container['href'], 'src11'))
            except KeyError as ke:
                continue
        Not_none_values = filter(None.__ne__, self.link_titles_src11)
        list_not_none_values = list(Not_none_values)
        self.link_titles_src11 = []
        for item in list_not_none_values:
            if len(item.split()) > 3:
                self.link_titles_src11.append(item)
        ###############################################################################

        ###############################################################################
        #src12 News scraper
        source = src12
        soup = basic_bsoup(source)
        containers = soup.findAll("span", {"class" : "card__title-text"})
        self.link_titles_src12 = []
        self.link_titles_src12_for_df = []
        for container in containers:
            try:
                self.link_titles_src12.append(container.string)
                self.link_titles_src12_for_df.append((str(container.string), container.parent.parent.parent['href'], 'src12'))
            except Exception as e:
                print('src12', e)
        Not_none_values = filter(None.__ne__, self.link_titles_src12)
        self.link_titles_src12 = list(Not_none_values)
        ###############################################################################

        ###############################################################################
        #src13 News scraper
        #There may be other pages at this site that could be scraped.
        source = src13
        soup = basic_bsoup(source)
        containers = soup.findAll("a")
        self.link_titles_src13 = []
        self.link_titles_src13_for_df = []
        for container in containers:
            try:#A picture was in a container and didn't have an 'href' tag. I added the try/except block and it looks like it's working normally now. I tried to handle the KeyError 'href' but it didn't work. I may be able to remove the try/except block in the future if the picture is removed from the site. 
                self.link_titles_src13.append(container.string)
                self.link_titles_src13_for_df.append((str(container.string), container['href'], 'src13'))
            except:
                continue
        Not_none_values = filter(None.__ne__, self.link_titles_src13)
        list_not_none_values = list(Not_none_values)
        self.link_titles_src13 = []
        for item in list_not_none_values:
            if len(item.split()) > 3:
                self.link_titles_src13.append(item)
        ###############################################################################

        ###############################################################################
        ###############################################################################
        #List of all link titles
        self.link_titles_all = self.link_titles_src2 + self.link_titles_src3 + \
                        self.link_titles_src7 + self.link_titles_src4 + \
                        self.link_titles_src5 + self.link_titles_src9 + \
                        self.link_titles_src6 + self.link_titles_src10 + \
                        self.link_titles_src8 + self.link_titles_src1 + \
                        self.link_titles_src11 + self.link_titles_src13 + \
                        self.link_titles_src12
        if clean_headlines:
            self.link_titles_all = [item for item in self.link_titles_all if len(str(item).strip().split(' ')) > 3]
        ###############################################################################
        ###############################################################################

        ###############################################################################
        ###############################################################################
        #DataFrame with headlines, link titles, and sources.
        self.links_for_df = self.link_titles_src1_for_df + self.link_titles_src3_for_df + \
                self.link_titles_src4_for_df + self.link_titles_src5_for_df + \
                self.link_titles_src5_for_df + self.link_titles_src6_for_df + \
                self.link_titles_src7_for_df + self.link_titles_src8_for_df + \
                self.link_titles_src9_for_df + self.link_titles_src10_for_df + \
                self.link_titles_src11_for_df + \
                self.link_titles_src12_for_df + self.link_titles_src13_for_df
        self.links_df = pd.DataFrame(self.links_for_df, columns=['headline', 'link', 'source'])
        if clean_headlines:
            self.links_df = self.links_df[self.links_df['headline'].apply(lambda x: len(str(x).strip().split(' ')) > 3)]
        ###############################################################################
        ###############################################################################
        
    def process_headlines(self):
        #Data Processing
        self.tokenized_word = word_tokenize(' '.join(self.link_titles_all).lower())
        self.filtered_words = []
        stop_words_file = open(r"C:\Users\jdejo\OneDrive\Documents\Python_Folders\News_Web_Scraper\stopwords.txt", "r")
        stop_words = stop_words_file.readlines()
        stop_words_file.close()
        stop_words_edited = []
        for j in stop_words:
            stop_words_edited.append(j.strip("\n"))
        for word in self.tokenized_word:
            if word not in stop_words_edited:
                self.filtered_words.append(word)
        self.ps = nltk.PorterStemmer()
        self.stemmed_words =[]
        for w in self.filtered_words:
            self.stemmed_words.append(self.ps.stem(w))
        self.lem = WordNetLemmatizer()
        self.lemmed_words = []
        for x in self.filtered_words:
            self.lemmed_words.append(self.lem.lemmatize(x))
        self.fdist_stem = FreqDist(self.stemmed_words)
        self.fdist_stem_dict = dict(self.fdist_stem)
        self.fdist_lem = FreqDist(self.lemmed_words)
        self.fdist_lem_dict = dict(self.fdist_lem)


        self.bigrams_lemmed = list(nltk.bigrams(self.lemmed_words))
        self.bigrams_stemmed = list(nltk.bigrams(self.stemmed_words))
        self.fdist_bigrams_lemmed = FreqDist(self.bigrams_lemmed)
        self.fdist_bigrams_lemmed_dict = dict(self.fdist_bigrams_lemmed)
        self.fdist_bigrams_stemmed = FreqDist(self.bigrams_stemmed)
        self.fdist_bigrams_stemmed_dict = dict(self.fdist_bigrams_stemmed)

        self.bigrams_lemmed_dict = {key:val for key, val in self.fdist_bigrams_lemmed_dict.items() if val > 3}
        bi_stem = pd.DataFrame.from_dict(self.bigrams_lemmed_dict, orient='index').describe()

        self.bigrams_stemmed_dict = {key:val for key, val in self.fdist_bigrams_stemmed_dict.items() if val > 3}
        bi_lem = pd.DataFrame.from_dict(self.bigrams_stemmed_dict, orient='index').describe()


        self.trigrams_lemmed = list(nltk.trigrams(self.lemmed_words))
        self.trigrams_stemmed = list(nltk.trigrams(self.stemmed_words))
        self.fdist_trigrams_lemmed = FreqDist(self.trigrams_lemmed)
        self.fdist_trigrams_lemmed_dict = dict(self.fdist_trigrams_lemmed)
        self.fdist_trigrams_stemmed = FreqDist(self.trigrams_stemmed)
        self.fdist_trigrams_stemmed_dict = dict(self.fdist_trigrams_stemmed)

        self.trigrams_lemmed_dict = {key:val for key, val in self.fdist_trigrams_lemmed_dict.items() if val > 3}
        self.tri_lem = pd.DataFrame.from_dict(self.trigrams_lemmed_dict, orient='index').describe()

        self.trigrams_stemmed_dict = {key:val for key, val in self.fdist_trigrams_stemmed_dict.items() if val > 3}
        self.tri_stem = pd.DataFrame.from_dict(self.trigrams_stemmed_dict, orient='index').describe()

        #The following function will find a word or sequence of words in link_titles_all.
        self.link_titles_all_set = set(self.link_titles_all)
        
        #Sentiment analysis with nltk's VADER
        self.sia = SentimentIntensityAnalyzer()
        #The following results will not be picklable for all lists that had the 
        #filter() function perfomed on them.
        self.polarity_scores = {}
        for item in self.link_titles_all:
            self.polarity_scores[item] = self.sia.polarity_scores(item)
        
    def symbol_news_polygon(self, symbol: str, from_date: str, limit: int = 10) -> list[str]:
        client = RESTClient(polygon_api_key)
        _news = []
        for n in client.list_ticker_news(
            ticker=symbol,
            published_utc_gte=from_date,
            order="asc",
            limit=limit,
            sort="published_utc",
            ):
            news.append(n)

        news = []
        # print date + title
        for index, item in enumerate(news):
            # verify this is an agg
            if isinstance(item, TickerNews):
                news.append(item.title)
        return news
    
    def symbol_news_finviz(self, symbol: str) -> list[str]:
        news = finvizfinance(symbol).news()
        return news

    def google_search(self, 
                      search_text: str, 
                      location: str = "Austin, Texas, United States",
                      engine: str = "google",
                      return_raw: bool = False) -> list[str]:
        params = {
        "engine": engine,
        "q": f"site:{search_text}",
        "location": location,
        "hl": "en",
        "gl": "us",
        "api_key": serpapi_api_key
        }

        search = serpapi.search(params)
        
        if return_raw:
            return search
        else:
            if engine == "google":
                return [
                    (item["title"], item["link"], item["snippet"])
                        for item in search["organic_results"]
                        ]
            return [
                (item["title"], item["link"])
                    for item in search["news_results"]
                    ]

