from urllib.request import Request, urlopen
from bs4 import BeautifulSoup as bs


def basic_bsoup(url):
    """This is a basic web scraper and will gather the soup from the specified url.
    It will return the bsoup variable which can be used with .findAll() or a 
    related function to find the desired elements on the webpage.
    A variable will need to be created to store the elements that are found.
    The following packages will need to be imported:
        from bs4 import beautifulSoup
        from urllib.request import Request, urlopen
    """
    site_link = url
    req = Request(site_link, headers={'User-Agent': 'Brave/1.32.113'}) #Masks the bot as presents it to the server as a web browser
    webpage = urlopen(req).read() #This obtains/reads the webpage/html.
    basic_bsoup.bsoup = bs(webpage, 'html.parser')
    return basic_bsoup.bsoup

def flatten_list(list_to_flatten):
    """Another 'flattened' list will need to be made outsde of this function """
    flatten_list.flattened = []
    for sublist in list_to_flatten:
        for val in sublist:
            flatten_list.flattened.append(val)
    return flatten_list.flattened