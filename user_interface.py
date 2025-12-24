from inputs import NewsImporter
import pandas as pd
import datetime
import os
import re
from pprint import pprint
import time
import hashlib

try:
    import mysql.connector  # mysql-connector-python
except Exception:
    mysql = None

import api_keys

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
    
    def store_headlines(self, topic, headlines, dataframe=False):
        """
        Store `headlines` into a MySQL database named `headlines`.

        - Database: `headlines`
        - Table: derived from `topic` (sanitized)
        - Columns: saved_date, headline, headline_hash (used for de-duping)

        Notes:
        - DB password is sourced from `api_keys.py` (which is gitignored) and/or env vars.
        - No secrets are printed/logged.
        """
        if mysql is None:
            raise ImportError(
                "Missing dependency for MySQL. Install `mysql-connector-python` "
                "(e.g. `pip install mysql-connector-python`)."
            )

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

        # Pull MySQL config from api_keys (or env vars via api_keys defaults).
        host = getattr(api_keys, "MYSQL_HOST", "localhost")
        user = getattr(api_keys, "MYSQL_USER", "root")
        port = int(getattr(api_keys, "MYSQL_PORT", 3306))
        password = getattr(api_keys, "MYSQL_PASSWORD", "") or ""
        if not password:
            raise ValueError(
                "MySQL password is missing. Set `MYSQL_PASSWORD` in `api_keys.py` "
                "or set environment variable MYSQL_PASSWORD."
            )

        # Normalize headlines into (saved_date, headline) rows.
        rows = []
        if headlines is None:
            headlines = self.headlines

        if isinstance(headlines, pd.DataFrame):
            # Prefer a 'headlines' column, else use the first column.
            if "headlines" in headlines.columns:
                series = headlines["headlines"]
            else:
                series = headlines.iloc[:, 0]

            for idx, val in series.items():
                text = "" if val is None else str(val).strip()
                if not text:
                    continue
                try:
                    d = pd.to_datetime(idx).date()
                except Exception:
                    d = datetime.date.today()
                rows.append((d, text))
        else:
            for item in headlines:
                if item is None:
                    continue
                if isinstance(item, (tuple, list)) and len(item) >= 2:
                    text = "" if item[1] is None else str(item[1]).strip()
                else:
                    text = str(item).strip()
                if not text:
                    continue
                rows.append((datetime.date.today(), text))

        if not rows:
            return pd.DataFrame(columns=["saved_date", "headline"]) if dataframe else 0

        # Connect, create DB + table, insert with de-dupe on headline_hash.
        cnx = mysql.connector.connect(
            host=host,
            user=user,
            password=password,
            port=port,
        )
        try:
            cur = cnx.cursor()
            cur.execute("CREATE DATABASE IF NOT EXISTS `headlines` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
            cur.execute("USE `headlines`;")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{table}` (
                    id INT NOT NULL AUTO_INCREMENT,
                    saved_date DATE NOT NULL,
                    headline TEXT NOT NULL,
                    headline_hash CHAR(32) NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uniq_headline_hash (headline_hash)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )

            insert_sql = f"INSERT IGNORE INTO `{table}` (saved_date, headline, headline_hash) VALUES (%s, %s, %s);"
            payload = []
            for saved_date, text in rows:
                h = hashlib.md5(text.encode("utf-8")).hexdigest()
                payload.append((saved_date, text, h))

            cur.executemany(insert_sql, payload)
            cnx.commit()

            inserted = cur.rowcount
        finally:
            try:
                cnx.close()
            except Exception:
                pass

        if dataframe:
            return pd.DataFrame({"saved_date": [r[0] for r in rows], "headline": [r[1] for r in rows]})
        return inserted

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

    def google_search(self, search_text):
        from selenium import webdriver
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_experimental_option("detach", True)
        driver = webdriver.Chrome(r"C:\Users\jdejo\OneDrive\Desktop\chromedriver_win32 (1)\chromedriver.exe", options=chrome_options)
        driver.get(r"https://www.google.com/search?q=" + search_text)

    def google_searches(self, search_items):
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
    