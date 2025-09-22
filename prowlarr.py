# prowlarr-qbittorent-search
# VERSION: 1.0
# AUTHORS: anon0mis (https://github.com/anon0mis)
# CREDITS: 
#   Based on https://github.com/qbittorrent/search-plugins/blob/master/nova3/engines/jackett.py 
#   By Diego de las Heras (ngosang@hotmail.es) 
#   AND CONTRIBUTORS:   ukharley
#                   hannsen (github.com/hannsen)
#                   Alexander Georgievskiy <galeksandrp@gmail.com>

import json
import os
import urllib.request
from datetime import datetime
from http.cookiejar import CookieJar
from multiprocessing.dummy import Pool
from threading import Lock
from typing import Any, Dict, List, Union
from urllib.parse import unquote, urlencode

import helpers
from novaprinter import prettyPrinter

###############################################################################

class ProxyManager:
    HTTP_PROXY_KEY = "http_proxy"
    HTTPS_PROXY_KEY = "https_proxy"

    def __init__(self) -> None:
        self.http_proxy = os.environ.get(self.HTTP_PROXY_KEY, "")
        self.https_proxy = os.environ.get(self.HTTPS_PROXY_KEY, "")

    def enable_proxy(self, enable: bool) -> None:
        # http proxy
        if enable:
            os.environ[self.HTTP_PROXY_KEY] = self.http_proxy
            os.environ[self.HTTPS_PROXY_KEY] = self.https_proxy
        else:
            os.environ.pop(self.HTTP_PROXY_KEY, None)
            os.environ.pop(self.HTTPS_PROXY_KEY, None)

        # SOCKS proxy
        # best effort and avoid breaking older qbt versions
        try:
            helpers.enable_socks_proxy(enable)
        except AttributeError:
            pass


# initialize it early to ensure env vars were not tampered
proxy_manager = ProxyManager()
proxy_manager.enable_proxy(False)  # off by default

###############################################################################

# load configuration from file
CONFIG_FILE = 'prowlarr.json'
CONFIG_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), CONFIG_FILE)
CONFIG_DATA: Dict[str, Any] = {
    'api_key': 'YOUR_API_KEY_HERE', # Prowlarr API Key
    'url': 'http://127.0.0.1:9696', # Prowlarr URL
    'tracker_first': False,         # Add tracker name to beginning or end of search result
    'thread_count': 20,             # Number of threads to use for HTTP requests
    'result_limit': 500,            # Max number of results to request from each indexer
    'show_disabled_indexers': True  # Show error messages for disabled indexers
}
PRINTER_THREAD_LOCK = Lock()


def load_configuration() -> None:
    global CONFIG_DATA
    try:
        # try to load user data from file
        with open(CONFIG_PATH, encoding='utf-8') as f:
            CONFIG_DATA = json.load(f)
    except ValueError:
        # if file exists, but it's malformed we load add a flag
        CONFIG_DATA['malformed'] = True
    except Exception:  # pylint: disable=broad-exception-caught
        # if file doesn't exist, we create it
        save_configuration()

    # do some checks
    if any(item not in CONFIG_DATA for item in ['api_key', 'tracker_first', 'url']):
        CONFIG_DATA['malformed'] = True

    # add missing keys
    if 'thread_count' not in CONFIG_DATA:
        CONFIG_DATA['thread_count'] = 20
        save_configuration()

    if 'result_limit' not in CONFIG_DATA:
        CONFIG_DATA['result_limit'] = 500
        save_configuration()

    if 'show_disabled_indexers' not in CONFIG_DATA:
        CONFIG_DATA['show_disabled_indexers'] = True
        save_configuration()


def save_configuration() -> None:
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        f.write(json.dumps(CONFIG_DATA, indent=4, sort_keys=True))


load_configuration()

###############################################################################

class prowlarr:
    name = 'Prowlarr'
    url = CONFIG_DATA['url'] if CONFIG_DATA['url'][-1] != '/' else CONFIG_DATA['url'][:-1]
    api_key = CONFIG_DATA['api_key']
    thread_count = CONFIG_DATA['thread_count']
    result_limit = CONFIG_DATA['result_limit']
    tracker_first = CONFIG_DATA['tracker_first']
    show_disabled_indexers = CONFIG_DATA['show_disabled_indexers']

    supported_categories = {
        'all': None,
        'anime': ['5070'],
        'books': ['7000'],
        'games': ['1000', '4000'],
        'movies': ['2000'],
        'music': ['3000'],
        'software': ['4000'],
        'tv': ['5000'],
    }

    disabled_indexers: List[str] = []

    def download_torrent(self, download_url: str) -> None:
        # fix for some indexers with magnet link inside .torrent file
        if download_url.startswith('magnet:?'):
            print(download_url + " " + download_url)
        proxy_manager.enable_proxy(True)
        response = self.get_response(download_url)
        proxy_manager.enable_proxy(False)
        if response is not None and response.startswith('magnet:?'):
            print(response + " " + download_url)
        else:
            print(helpers.download_file(download_url))

    def search(self, what: str, cat: str = 'all') -> None:
        what = unquote(what)
        category = self.supported_categories[cat.lower()]

        # check for malformed configuration
        if 'malformed' in CONFIG_DATA:
            self.handle_error("Malformed configuration file", what)
            return

        # check api_key
        if self.api_key == "YOUR_API_KEY_HERE":
            self.handle_error("API key error", what)
            return

        # search in Prowlarr API
        if self.thread_count > 1:
            args = []
            indexers = self.get_prowlarr_indexers(what)
            for indexer in indexers:
                args.append((what, category, indexer))
            with Pool(min(len(indexers), self.thread_count)) as pool:
                pool.starmap(self.search_prowlarr_indexer, args)
        else:
            self.search_prowlarr_indexer(what, category, 'all')

    def get_prowlarr_indexers(self, what: str) -> List[Dict[str, Any]]:
        params = urlencode([
            ('apikey', self.api_key),
        ])

        indexer_status_url = f"{self.url}/api/v1/indexerstatus?{params}"
        status_response = self.get_response(indexer_status_url)
        if status_response is None:
            self.handle_error("Connection error getting indexer statuses", what)
            return []
        
        status_results = json.loads(status_response)
        self.disabled_indexers = [status.get('indexerId') for status in status_results]

        indexer_url = f"{self.url}/api/v1/indexer?{params}"
        response = self.get_response(indexer_url)
        if response is None:
            self.handle_error("Connection error getting indexer list", what)
            return []
        # process results
        indexer_results = json.loads(response)
        indexers = []
        for indexer in indexer_results:
            if indexer.get('enable'):
                indexers.append(indexer)
        return indexers

    def search_prowlarr_indexer(self, what: str, category: Union[List[str], None], indexer: Dict[str, Any] = None) -> None:
        def toStr(s: Union[str, None]) -> str:
            return s if s is not None else ''
        
        if indexer.get('id') in self.disabled_indexers:
            if self.show_disabled_indexers:
                self.handle_error(f"Indexer '{indexer.get('name')}' is disabled or has errors", what)
            return

        # prepare Prowlarr url
        params_tmp = [
            ('apikey', self.api_key),
            ('query', what),
            ('limit', self.result_limit)
        ]

        if indexer is not None:
            params_tmp.append(('indexerIds', indexer.get('id')))
        
        if category is not None:
            for cat in category:
                params_tmp.append(('categories', cat))

        params = urlencode(params_tmp)
        prowlarr_url = f"{self.url}/api/v1/search?{params}"
        response = self.get_response(prowlarr_url)
        if response is None:
            self.handle_error("Connection error for indexer: " + indexer.get('name'), what)
            return

        # process search results
        response_json = json.loads(response)
        for result in response_json:
            res: Dict[str, Any] = {}

            title_tmp = result.get('title')
            if title_tmp is not None:
                title = title_tmp
            else:
                continue

            tracker = result.get('indexer')
            if self.tracker_first:
                res['name'] = f"[{tracker}] {title}"
            else:
                res['name'] = f"{title} [{tracker}]"

            if 'downloadUrl' in result:
                res['link'] = str(result.get('downloadUrl'))
            elif 'magnetUrl' in result:
                res['link'] = str(result.get('magnetUrl'))
            else:
                continue

            if res['link'].startswith('http'):
                res['link'] = self.resolve_url(res['link'])

            res['size'] = str(result.get('size'))
            res['size'] = -1 if res['size'] is None else (toStr(res['size']) + ' B')

            res['seeds'] = result.get('seeders')
            res['seeds'] = -1 if res['seeds'] is None else res['seeds']

            res['leech'] = result.get('leechers')
            res['leech'] = -1 if res['leech'] is None else res['leech']

            res['desc_link'] = result.get('infoUrl')
            if res['desc_link'] is None:
                if 'guid' in result:
                    res['desc_link'] = result.get('guid')
                else:
                    res['desc_link'] = ''

            # note: engine_url can't be changed, torrent download stops working
            res['engine_url'] = self.url

            try:
                date = datetime.strptime(result.get('publishDate'), "%Y-%m-%dT%H:%M:%SZ")
                res['pub_date'] = int(date.timestamp())
            except Exception:  # pylint: disable=broad-exception-caught
                res['pub_date'] = -1

            self.pretty_printer_thread_safe(res)

    def get_response(self, query: str) -> Union[str, None]:
        response = None
        try:
            # we can't use helpers.retrieve_url because of redirects
            # we need the cookie processor to handle redirects
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            response = opener.open(query).read().decode('utf-8')
        except urllib.request.HTTPError as e:
            # if the page returns a magnet redirect, used in download_torrent
            if e.code == 302:
                response = e.url
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        return response

    def handle_error(self, error_msg: str, what: str) -> None:
        # we need to print the search text to be displayed in qBittorrent when
        # 'Torrent names only' is enabled
        self.pretty_printer_thread_safe({
            'link': self.url,
            'name': f"Prowlarr: {error_msg}! Right-click this row and select 'Open description page' to open help. Configuration file: '{CONFIG_PATH}' Search: '{what}'",
            'size': -1,
            'seeds': -1,
            'leech': -1,
            'engine_url': self.url,
            'desc_link': 'https://github.com/anon0mis/prowlarr-qbittorent-search',
            'pub_date': -1
        })

    def pretty_printer_thread_safe(self, dictionary: Dict[str, Any]) -> None:
        escaped_dict = self.escape_pipe(dictionary)
        with PRINTER_THREAD_LOCK:
            prettyPrinter(escaped_dict)  # type: ignore[arg-type] # refactor later

    def escape_pipe(self, dictionary: Dict[str, Any]) -> Dict[str, Any]:
        # Safety measure until it's fixed in prettyPrinter
        for key in dictionary.keys():
            if isinstance(dictionary[key], str):
                dictionary[key] = dictionary[key].replace('|', '%7C')
        return dictionary
    
    # Dirty hack to resolve URL after redirects, but it works
    def resolve_url(self, query: str) -> str:
        try:
            res = urllib.request.urlopen(query)
            return res.url
        except urllib.error.HTTPError as e:
           return e.url
    

if __name__ == "__main__":
    prowlarr_se = prowlarr()
    prowlarr_se.search("Linux ISO")
