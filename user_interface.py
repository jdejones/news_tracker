from inputs import NewsImporter
import pandas as pd
pd.set_option("display.max_colwidth", None)
import datetime
import os
import re
from pprint import pprint
import time
import hashlib
from api_keys import news_database

from sqlalchemy import (
    create_engine,
    inspect,
    MetaData,
    Table,
    Integer,
    Column,
    Date,
    Text,
    String,
    DateTime,
    UniqueConstraint,
    text as sql_text,
    func,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert


class UserInterface(NewsImporter):
    def __init__(self):
        super().__init__()
        
    def find_headline(self, word, regex=False, check_dataframe=False, save_dataframe=False):
        self.headlines = []
        for item in self.link_titles_all_set:
            if regex==False:
                if word in item.lower():
                    self.headlines.append((len(self.headlines), item.strip()))
            elif regex==True:
                if re.findall(word, item.lower()):
                    self.headlines.append((len(self.headlines), item.strip()))
        if save_dataframe:
            tagpath = fr"E:\Market Research\Dataset\News\Market News\tags\{word}" 
            if not os.path.exists(tagpath):
                os.makedirs(tagpath)
            framepath = fr"E:\Market Research\Dataset\News\Market News\tags\{word}\dataframe.txt"
            if not os.path.exists(framepath):
                df = pd.DataFrame(data=[item[1] for item in self.find_headline(word)])
                df.index = [pd.to_datetime(datetime.date.today()).date()] * len(df)
                df.columns = ['headlines']
                df.to_csv(framepath)
            else:
                df = pd.read_csv(framepath, index_col='Unnamed: 0')
                df2 = pd.DataFrame(data=[item[1] for item in self.headlines], index = [pd.to_datetime(datetime.date.today()).date()] * len(self.headlines))
                df2.columns = ['headlines']
                df3 = pd.concat([df, df2], axis=0)
                df3.drop_duplicates(inplace=True)
                df3.to_csv(framepath)
                return df3
            return df
        if check_dataframe:
            tagpath = fr"E:\Market Research\Dataset\News\Market News\tags\{word}" 
            if not os.path.exists(tagpath):
                print('No Tag Folder\n')
                return self.headlines
            framepath = fr"E:\Market Research\Dataset\News\Market News\tags\{word}\dataframe.txt"
            if not os.path.exists(framepath):
                print('No Tag Dataframe')
                return self.headlines
            df = pd.read_csv(framepath, index_col='Unnamed: 0')
            df2 = pd.DataFrame(data=[item[1] for item in self.headlines], index = [pd.to_datetime(datetime.date.today()).date()] * len(self.headlines))
            df2.columns = ['headlines']
            df3 = pd.concat([df, df2], axis=0)
            df3.drop_duplicates(inplace=True)
            return df3
        return self.headlines
    
    def get_tables(self, url: str = f"mysql+pymysql://root:{news_database}@127.0.0.1:3306/news_topics") -> list[str]:
        """Get list of all table names in the 'news' database."""
        engine = create_engine(url, pool_pre_ping=True, connect_args={'connect_timeout': 5})
        
        inspector = inspect(engine)
        table_names = inspector.get_table_names()
        
        return table_names    
    
    def store_headlines(self, 
                        topic: str, 
                        headlines: list[int|str, str] | pd.DataFrame, 
                        dataframe: bool = False,
                        url: str = f"mysql+pymysql://root:{news_database}@127.0.0.1:3306/news_topics"):
        """
        Store `headlines` into a MySQL database named `headlines`.

        - Database: `headlines`
        - Table: derived from `topic` (sanitized)
        - Columns: saved_date, headline, headline_hash (used for de-duping)
        """

        if topic is None:
            raise ValueError("topic must be a non-empty string")

        topic_str = str(topic).strip()
        if not topic_str:
            raise ValueError("topic must be a non-empty string")

        # Safe table name: only letters/numbers/underscore, max 64 chars for MySQL identifiers.
        table = re.sub(r"[^0-9a-zA-Z_]+", "_", topic_str).strip("_").lower()
        if not table:
            raise ValueError(f"topic '{topic_str}' results in an empty table name after sanitization")
        if table[0].isdigit():
            table = f"topic_{table}"
        table = table[:64]
        if not re.fullmatch(r"[a-zA-Z_][0-9a-zA-Z_]*", table):
            raise ValueError(f"Unsafe table name derived from topic: {table!r}")
        
        #Remove headlines if previously stored
        if topic in self.get_tables():
            engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})

            # query database -> DataFrame
            table = pd.read_sql(f"SELECT * FROM {topic}", con=engine)
            headlines = [headline for headline in headlines if headline not in table['headline'].values]
            

        # Normalize headlines into (saved_date, headline) rows.
        rows = []
        if headlines is None:
            raise ValueError("headlines must be a non-empty list or dataframe")

        if isinstance(headlines, pd.DataFrame):
            # Prefer a 'headlines' column, else use the first column.
            if ("headline" in headlines.columns) and ('link' in headlines.columns):
                rows = [(datetime.date.today(), row['headline'], row['link']) for _, row in headlines.iterrows()]
            else:
                raise ValueError("headlines dataframe must have a 'headline' and 'link' column")
        else:
            if not isinstance(headlines, list):
                headlines = [headlines]
            headlines = self._headline_to_link(headlines)
            if ("headline" in headlines.columns) and ('link' in headlines.columns):
                rows = [(datetime.date.today(), row['headline'], row['link']) for _, row in headlines.iterrows()]
            else:
                raise ValueError("headlines dataframe must have a 'headline' and 'link' column")


        if not rows:
            return pd.DataFrame(columns=["saved_date", "headline"]) if dataframe else 0

        # Create database if needed (connect at server level).
        engine = create_engine(url, pool_pre_ping=True, connect_args={'connect_timeout': 5})


        # Create/reflect table in `headlines` database.
        metadata = MetaData()
        headlines_table = Table(
            table,
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("saved_date", Date, nullable=False),
            Column("headline", Text, nullable=False),
            Column("link", Text, nullable=True),
            Column("headline_hash", String(32), nullable=False),
            Column("created_at", DateTime, nullable=False, server_default=func.current_timestamp()),
            UniqueConstraint("headline_hash", name="uniq_headline_hash"),
            mysql_charset="utf8mb4",
        )
        metadata.create_all(engine)

        payload = []
        for saved_date, text, link in rows:
            h = hashlib.md5(text.encode("utf-8")).hexdigest()
            payload.append({"saved_date": saved_date, "headline": text, "link": link, "headline_hash": h})

        # INSERT IGNORE (de-dupe on uniq_headline_hash).
        stmt = mysql_insert(headlines_table).values(payload).prefix_with("IGNORE")
        with engine.begin() as conn:
            result = conn.execute(stmt)
            inserted = int(result.rowcount or 0)

        if dataframe:
            return pd.DataFrame({"saved_date": [r[0] for r in rows], "headline": [r[1] for r in rows], "link": [r[2] for r in rows]})
        return inserted

    def retrieve_headlines(self, topic: str,
                           url: str = f"mysql+pymysql://root:{news_database}@127.0.0.1:3306/news"):
        engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
        table = pd.read_sql(f"SELECT * FROM {topic}", con=engine)
        return table

    def get_tags(folder=r"E:\Market Research\Dataset\News\Market News\tags"):
        return os.listdir(folder)

    def print_tagged_headlines(self, **kwargs):
        for todo, tags in kwargs.items():
            for tag in tags:
                if todo == 'save':
                    pprint(tag)
                    pprint(self.find_headline(tag, save_dataframe=True))            
                elif todo == 'check':
                    pprint(tag)
                    pprint(self.find_headline(tag, check_dataframe=True))

    def ngram_headline_polarity_scores(self, ngram):
        headlines = []
        for item in self.link_titles_all_set:
            if ngram in item.lower():
                headlines.append(item.strip())
        for headline in headlines:
            polarity_scores[headline] = sia.polarity_scores(headline)

    def browser_search(self, search_text):
        from selenium import webdriver
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_experimental_option("detach", True)
        driver = webdriver.Chrome(r"C:\Users\jdejo\OneDrive\Desktop\chromedriver_win32 (1)\chromedriver.exe", options=chrome_options)
        driver.get(r"https://www.google.com/search?q=" + search_text)

    def browser_searches(self, search_items):
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_experimental_option("detach", True)
        driver = webdriver.Chrome(r"C:\Users\jdejo\OneDrive\Desktop\chromedriver_win32 (1)\chromedriver.exe", options=chrome_options)
        n=0
        for item in search_items:
            driver.get(r"https://www.google.com/search?q=" + item)
            if n<len(search_items):
                driver.execute_script("window.open('');")
                driver.switch_to.window(driver.window_handles[-1])
            n+=1
        time.sleep(1)
        driver.switch_to.window(driver.window_handles[-1])
        driver.close()
    
    def ngrams_frame(self):
        mono_ngrams = [item[0] for item in sorted(self.fdist_stem_dict.items(), key=lambda x: x[1], reverse=True)]
        mono_ngrams_series = pd.Series(mono_ngrams, name='ngrams')
        mono_n = [item[1] for item in sorted(self.fdist_stem_dict.items(), key=lambda x: x[1], reverse=True)]
        mono_n_series = pd.Series(mono_n, name='n')
        bi_ngrams = [item[0] for item in sorted(self.fdist_bigrams_stemmed_dict.items(), key=lambda x: x[1], reverse=True)]
        bi_ngrams_series = pd.Series(bi_ngrams, name='ngrams')
        bi_n = [item[1] for item in sorted(self.fdist_bigrams_stemmed_dict.items(), key=lambda x: x[1], reverse=True)]
        bi_n_series = pd.Series(bi_n, name='n')
        tri_ngrams = [item[0] for item in sorted(self.fdist_trigrams_stemmed_dict.items(), key=lambda x: x[1], reverse=True)]
        tri_ngrams_series = pd.Series(tri_ngrams, name='ngrams')
        tri_n = [item[1] for item in sorted(self.fdist_trigrams_stemmed_dict.items(), key=lambda x: x[1], reverse=True)]
        tri_n_series = pd.Series(tri_n, name='n')
        df = pd.concat([mono_ngrams_series, mono_n_series, bi_ngrams_series, bi_n_series, tri_ngrams_series, tri_n_series],  axis=1)
        df.columns = pd.MultiIndex.from_tuples((('mono', 'ngrams'), ('mono', 'n'),('bi', 'ngrams'), ('bi', 'n'), ('tri', 'ngrams'), ('tri', 'n')))
        return df
    
    def _headline_to_link(self, headlines: list[int, str]):
        if isinstance(headlines, tuple):
            return self.links_df.loc[self.links_df.headline == headlines[1]][['headline', 'link']]
        return self.links_df.loc[self.links_df.headline.isin([_[1] for _ in headlines])][['headline', 'link']]

