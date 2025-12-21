# -*- coding: utf-8 -*-
"""
Created on Fri Feb 11 10:15:52 2022

@author: jdejo
"""
if __name__ == "__main__":
    from bs4 import BeautifulSoup as bs
    import bs4
    import nltk
    nltk.download('punkt_tab')
    from nltk.tokenize import word_tokenize
    from nltk.probability import FreqDist
    from nltk.corpus import stopwords
    from nltk.stem import PorterStemmer
    from nltk.stem.wordnet import WordNetLemmatizer
    from support_functions import *
    from sources import *
    from list_functions import *
    from nltk.sentiment import SentimentIntensityAnalyzer
    import pandas as pd
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    import time
    import io
    import os
    import stem.process
    import re
    from NordVPN import NordVPN
    import datetime
    from pprint import pprint
    pd.options.display.max_colwidth = 100
    
    ###############################################################################
    ###############################################################################
    #Connect to VPN
    # nvpn = NordVPN()
    # print(f'IP before connect {nvpn.check_ip()}')
    # nvpn.connect()
    # time.sleep(30)
    # print(f'IP after connect {nvpn.check_ip()}')
    ###############################################################################

    #Scrapers

    ###############################################################################

    

    #src1 news scraper
    source = src1
    soup = basic_bsoup(source)
    containers = soup.findAll("a", {"class" : "nn-tab-link"})
    link_titles_src1 = []
    link_titles_src1_for_df = []
    for container in containers:
        link_titles_src1.append(container.contents)
        link_titles_src1_for_df.append((str(container.string), container['href'], 'src1'))
    flatten_list(link_titles_src1)
    link_titles_src1 = []
    for element in flatten_list.flattened:
        link_titles_src1.append(element.strip())   
    ###############################################################################

    ###############################################################################
    #src2 News scraper 
    source = src2
    soup = basic_bsoup(source)
    containers = soup.findAll("a", {"class" : "Card-title"})
    link_titles_src2 = []
    link_titles_src2_for_df = []
    for container in containers:
        link_titles_src2.append(container.string)
        link_titles_src2_for_df.append((str(container.string), container['href'], 'src2 Business'))
    Not_none_values = filter(None.__ne__, link_titles_src2)
    link_titles_src2 = list(Not_none_values)
    ###############################################################################

    ###############################################################################
    #src3 News scraper
    source = src3
    soup = basic_bsoup(source)
    containers = soup.findAll("a", {"class" : "Card-title"})
    link_titles_src3 = []
    link_titles_src3_for_df = []
    for container in containers:
        link_titles_src3.append(container.string)
        link_titles_src3_for_df.append((str(container.string), container['href'], 'src3 Economy'))
    Not_none_values = filter(None.__ne__, link_titles_src3)
    link_titles_src3 = list(Not_none_values)
    ###############################################################################

    ###############################################################################
    #src4 News scraper
    source = src4
    soup = basic_bsoup(source)
    containers = soup.findAll("a", {"class" : "Card-title"})
    link_titles_src4 = []
    link_titles_src4_for_df = []
    for container in containers:
        link_titles_src4.append(container.string)
        link_titles_src4_for_df.append((str(container.string), container['href'], 'src4 Finance'))
    Not_none_values = filter(None.__ne__, link_titles_src4)
    link_titles_src4 = list(Not_none_values)
    ###############################################################################

    ###############################################################################
    #src5 News scraper
    source = src5
    soup = basic_bsoup(source)
    containers = soup.findAll("a", {"class" : "Card-title"})
    link_titles_src5 = []
    link_titles_src5_for_df = []
    for container in containers:
        link_titles_src5.append(container.string)
        link_titles_src5_for_df.append((str(container.string), container['href'], 'src5 Health and Science'))
    Not_none_values = filter(None.__ne__, link_titles_src5)
    link_titles_src5 = list(Not_none_values)
    ###############################################################################

    ###############################################################################
    #src6 News scraper
    source = src6
    soup = basic_bsoup(source)
    containers = soup.findAll("a", {"class" : "Card-title"})
    link_titles_src6 = []
    link_titles_src6_for_df = []
    for container in containers:
        link_titles_src6.append(container.string)
        link_titles_src6_for_df.append((str(container.string), container['href'], 'src6 Real Estate'))
    Not_none_values = filter(None.__ne__, link_titles_src6)
    link_titles_src6 = list(Not_none_values)
    ###############################################################################

    ###############################################################################
    #src7 News scraper
    source = src7
    soup = basic_bsoup(source)
    containers = soup.findAll("a", {"class" : "Card-title"})
    link_titles_src7 = []
    link_titles_src7_for_df = []
    for container in containers:
        link_titles_src7.append(container.string)
        link_titles_src7_for_df.append((str(container.string), container['href'], 'src7 Energy'))
    Not_none_values = filter(None.__ne__, link_titles_src7)
    link_titles_src7 = list(Not_none_values)
    ###############################################################################

    ###############################################################################
    #src8 News scraper
    source = src8
    soup = basic_bsoup(source)
    containers = soup.findAll("a", {"class" : "Card-title"})
    link_titles_src8 = []
    link_titles_src8_for_df = []
    for container in containers:
        link_titles_src8.append(container.string)
        link_titles_src8_for_df.append((str(container.string), container['href'], 'src8 Transportation'))
    Not_none_values = filter(None.__ne__, link_titles_src8)
    link_titles_src8 = list(Not_none_values)
    ###############################################################################

    ###############################################################################
    #src9 News scraper
    source = src9
    soup = basic_bsoup(source)
    containers = soup.findAll("a", {"class" : "Card-title"})
    link_titles_src9 = []
    link_titles_src9_for_df = []
    for container in containers:
        link_titles_src9.append(container.string)
        link_titles_src9_for_df.append((str(container.string), container['href'], 'src9 Industrials'))
    Not_none_values = filter(None.__ne__, link_titles_src9)
    link_titles_src9 = list(Not_none_values)
    ###############################################################################

    ###############################################################################
    #src10 News scraper
    source = src10
    soup = basic_bsoup(source)
    containers = soup.findAll("a", {"class" : "Card-title"})
    link_titles_src10 = []
    link_titles_src10_for_df = []
    for container in containers:
        link_titles_src10.append(container.string)
        link_titles_src10_for_df.append((str(container.string), container['href'], 'src10 Retail'))
    Not_none_values = filter(None.__ne__, link_titles_src10)
    link_titles_src10 = list(Not_none_values)
    ###############################################################################

    ###############################################################################
    #src11 News scraper
    #The beginning and end of this may be able to be trimmed-2/11/22
    source = src11
    soup = basic_bsoup(source)
    containers = soup.findAll("a")
    link_titles_src11 = []
    link_titles_src11_for_df = []
    for container in containers:
        try:
            link_titles_src11.append(container.string)
            if container.string == None:
                continue
            link_titles_src11_for_df.append((str(container.string), container['href'], 'src11'))
        except KeyError as ke:
            continue
    Not_none_values = filter(None.__ne__, link_titles_src11)
    list_not_none_values = list(Not_none_values)
    link_titles_src11 = []
    for item in list_not_none_values:
        if len(item.split()) > 3:
            link_titles_src11.append(item)
    ###############################################################################

    ###############################################################################
    #src12 News scraper
    source = src12
    soup = basic_bsoup(source)
    containers = soup.findAll("span", {"class" : "card__title-text"})
    link_titles_src12 = []
    link_titles_src12_for_df = []
    for container in containers:
        try:
            link_titles_src12.append(container.string)
            link_titles_src12_for_df.append((str(container.string), container.parent.parent.parent['href'], 'src12'))
        except Exception as e:
            print('src12', e)
    Not_none_values = filter(None.__ne__, link_titles_src12)
    link_titles_src12 = list(Not_none_values)
    ###############################################################################

    ###############################################################################
    #src13 News scraper
    #There may be other pages at this site that could be scraped.
    source = src13
    soup = basic_bsoup(source)
    containers = soup.findAll("a")
    link_titles_src13 = []
    link_titles_src13_for_df = []
    for container in containers:
        try:#A picture was in a container and didn't have an 'href' tag. I added the try/except block and it looks like it's working normally now. I tried to handle the KeyError 'href' but it didn't work. I may be able to remove the try/except block in the future if the picture is removed from the site. 
            link_titles_src13.append(container.string)
            link_titles_src13_for_df.append((str(container.string), container['href'], 'src13'))
        except:
            continue
    Not_none_values = filter(None.__ne__, link_titles_src13)
    list_not_none_values = list(Not_none_values)
    link_titles_src13 = []
    for item in list_not_none_values:
        if len(item.split()) > 3:
            link_titles_src13.append(item)
    ###############################################################################

    ###############################################################################
    ###############################################################################
    #List of all link titles
    link_titles_all = link_titles_src2 + link_titles_src3 + \
                    link_titles_src7 + link_titles_src4 + \
                    link_titles_src5 + link_titles_src9 + \
                    link_titles_src6 + link_titles_src10 + \
                    link_titles_src8 + link_titles_src1 + \
                    link_titles_src11 + link_titles_src13 + \
                    link_titles_src12
    ###############################################################################
    ###############################################################################

    ###############################################################################
    ###############################################################################
    #DataFrame with headlines, link titles, and sources.
    links_for_df = link_titles_src1_for_df + link_titles_src2_for_df + \
            link_titles_src3_for_df + link_titles_src4_for_df + \
            link_titles_src5_for_df + link_titles_src6_for_df + \
            link_titles_src7_for_df + link_titles_src8_for_df + \
            link_titles_src9_for_df + link_titles_src10_for_df + \
            link_titles_src11_for_df + \
            link_titles_src12_for_df + link_titles_src13_for_df
    links_df = pd.DataFrame(links_for_df, columns=['headline', 'link', 'source'])
    ###############################################################################
    ###############################################################################
    #Data Processing
    tokenized_word = word_tokenize(' '.join(link_titles_all).lower())
    filtered_words = []
    stop_words_file = open(r"C:\Users\jdejo\OneDrive\Documents\Python_Folders\News_Web_Scraper\stopwords.txt", "r")
    stop_words = stop_words_file.readlines()
    stop_words_file.close()
    stop_words_edited = []
    for j in stop_words:
        stop_words_edited.append(j.strip("\n"))
    for word in tokenized_word:
        if word not in stop_words_edited:
            filtered_words.append(word)
    ps = nltk.PorterStemmer()
    stemmed_words =[]
    for w in filtered_words:
        stemmed_words.append(ps.stem(w))
    lem = WordNetLemmatizer()
    lemmed_words = []
    for x in filtered_words:
        lemmed_words.append(lem.lemmatize(x))
    fdist_stem = FreqDist(stemmed_words)
    fdist_stem_dict = dict(fdist_stem)
    fdist_lem = FreqDist(lemmed_words)
    fdist_lem_dict = dict(fdist_lem)


    bigrams_lemmed = list(nltk.bigrams(lemmed_words))
    bigrams_stemmed = list(nltk.bigrams(stemmed_words))
    fdist_bigrams_lemmed = FreqDist(bigrams_lemmed)
    fdist_bigrams_lemmed_dict = dict(fdist_bigrams_lemmed)
    fdist_bigrams_stemmed = FreqDist(bigrams_stemmed)
    fdist_bigrams_stemmed_dict = dict(fdist_bigrams_stemmed)

    bigrams_lemmed_dict = {key:val for key, val in fdist_bigrams_lemmed_dict.items() if val > 3}
    bi_stem = pd.DataFrame.from_dict(bigrams_lemmed_dict, orient='index').describe()

    bigrams_stemmed_dict = {key:val for key, val in fdist_bigrams_stemmed_dict.items() if val > 3}
    bi_lem = pd.DataFrame.from_dict(bigrams_stemmed_dict, orient='index').describe()


    trigrams_lemmed = list(nltk.trigrams(lemmed_words))
    trigrams_stemmed = list(nltk.trigrams(stemmed_words))
    fdist_trigrams_lemmed = FreqDist(trigrams_lemmed)
    fdist_trigrams_lemmed_dict = dict(fdist_trigrams_lemmed)
    fdist_trigrams_stemmed = FreqDist(trigrams_stemmed)
    fdist_trigrams_stemmed_dict = dict(fdist_trigrams_stemmed)

    trigrams_lemmed_dict = {key:val for key, val in fdist_trigrams_lemmed_dict.items() if val > 3}
    tri_lem = pd.DataFrame.from_dict(trigrams_lemmed_dict, orient='index').describe()

    trigrams_stemmed_dict = {key:val for key, val in fdist_trigrams_stemmed_dict.items() if val > 3}
    tri_stem = pd.DataFrame.from_dict(trigrams_stemmed_dict, orient='index').describe()

    #The following function will find a word or sequence of words in link_titles_all.
    link_titles_all_set = set(link_titles_all)
    def find_headline(word, regex=False, check_dataframe=False, save_dataframe=False):
        headlines = []
        for item in link_titles_all_set:
            if regex==False:
                if word in item.lower():
                    headlines.append((len(headlines), item.strip()))
            elif regex==True:
                if re.findall(word, item.lower()):
                    headlines.append((len(headlines), item.strip()))
        if save_dataframe:
            tagpath = fr"E:\Market Research\Dataset\News\Market News\tags\{word}" 
            if not os.path.exists(tagpath):
                os.makedirs(tagpath)
            framepath = fr"E:\Market Research\Dataset\News\Market News\tags\{word}\dataframe.txt"
            if not os.path.exists(framepath):
                df = pd.DataFrame(data=[item[1] for item in find_headline(word)])
                df.index = [pd.to_datetime(datetime.date.today()).date()] * len(df)
                df.columns = ['headlines']
                df.to_csv(framepath)
            else:
                df = pd.read_csv(framepath, index_col='Unnamed: 0')
                df2 = pd.DataFrame(data=[item[1] for item in headlines], index = [pd.to_datetime(datetime.date.today()).date()] * len(headlines))
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
                return headlines
            framepath = fr"E:\Market Research\Dataset\News\Market News\tags\{word}\dataframe.txt"
            if not os.path.exists(framepath):
                print('No Tag Dataframe')
                return headlines
            df = pd.read_csv(framepath, index_col='Unnamed: 0')
            df2 = pd.DataFrame(data=[item[1] for item in headlines], index = [pd.to_datetime(datetime.date.today()).date()] * len(headlines))
            df2.columns = ['headlines']
            df3 = pd.concat([df, df2], axis=0)
            df3.drop_duplicates(inplace=True)
            return df3
        return headlines

    def get_tags(folder=r"E:\Market Research\Dataset\News\Market News\tags"):
        return os.listdir(folder)

    def print_tagged_headlines(**kwargs):
        for todo, tags in kwargs.items():
            for tag in tags:
                if todo == 'save':
                    pprint(tag)
                    pprint(find_headline(tag, save_dataframe=True))            
                elif todo == 'check':
                    pprint(tag)
                    pprint(find_headline(tag, check_dataframe=True))

    #Sentiment analysis with nltk's VADER
    sia = SentimentIntensityAnalyzer()
    #The following results will not be picklable for all lists that had the 
    #filter() function perfomed on them.
    polarity_scores = {}
    for item in link_titles_all:
        polarity_scores[item] = sia.polarity_scores(item)
