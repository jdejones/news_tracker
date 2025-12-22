from inputs import NewsImporter
import pandas as pd
import datetime
import os
import re
from pprint import pprint
import time

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
    