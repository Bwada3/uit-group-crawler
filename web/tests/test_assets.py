"""Static caching never touches GraphQL, media attachments or navigation policy."""
import gzip
from pathlib import Path
import sys
import unittest
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import local_scraper as scraper
from local_assets import AssetCache,static_asset,install_asset_cache

GROUP='https://www.facebook.com/groups/230048322891238'
ASSET='https://static.xx.fbcdn.net/rsrc.php/v4/a/r/fixture.js'


class AssetRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager=sync_playwright();cls.p=cls.manager.__enter__()
        cls.browser=cls.p.chromium.launch(channel='chrome',headless=True)

    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.manager.__exit__(None,None,None)

    def setUp(self):
        self.context=self.browser.new_context()
        self.page=self.context.new_page();self.hits=0;self.control='public, max-age=3600, immutable';self.extra={};self.compressed=False
        self.context.route('**/*',self.serve)
        self.cache=AssetCache(self.context)

    def tearDown(self):self.context.close()

    def serve(self,route):
        request=route.request
        if request.url.startswith(ASSET):
            self.hits+=1
            body=b'window.assetValue="preserved";'
            headers={'Cache-Control':self.control,'Content-Type':'application/javascript',**self.extra}
            if self.compressed:body=gzip.compress(body);headers['Content-Encoding']='gzip'
            route.fulfill(status=200,headers=headers,body=body)
        elif '/api/graphql' in request.url:
            self.hits+=1;route.fulfill(status=200,content_type='application/json',body='{"count":1}')
        else:
            route.fulfill(status=200,content_type='text/html',body='<div data-ad-rendering-role="story_message">Post</div><script src="'+ASSET+'"></script>')

    def visit(self,identifier):
        self.page.goto(GROUP+'/permalink/'+str(identifier)+'/',wait_until='load')
        self.page.wait_for_function('window.assetValue==="preserved"')

    def test_public_script_is_reused_and_data_preserved(self):
        self.visit(111111);self.visit(222222)
        self.assertEqual(self.hits,1)
        self.assertEqual(self.cache.stats['hits'],1)
        self.assertEqual(self.page.locator('[data-ad-rendering-role]').inner_text(),'Post')

    def test_experimental_cache_is_disabled_by_default(self):
        with patch.dict('os.environ',{},clear=True):install_asset_cache(self.context)
        self.assertIsNone(getattr(self.context,'_grouppulse_asset_cache',None))

    def test_private_or_cookie_varying_assets_are_never_cached(self):
        for headers in ({'Cache-Control':'private, max-age=3600'}, {'Cache-Control':'public, no-store, max-age=3600'},
                        {'Vary':'Cookie'}):
            self.control=headers.get('Cache-Control','public, max-age=3600')
            self.extra={key:value for key,value in headers.items() if key!='Cache-Control'}
            before=self.hits
            self.visit(111111);self.visit(222222)
            self.assertEqual(self.hits-before,2,str(headers)+' '+str(self.cache.stats))
        self.assertEqual(self.cache.stats['stored'],0)

    def test_compressed_script_decodes_and_cookie_responses_are_uncached(self):
        counts=[];cookie=[False]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path.startswith('/rsrc.php/'):
                    counts.append(1);body=gzip.compress(b'window.assetValue="preserved";')
                    self.send_response(200)
                    self.send_header('Content-Type','application/javascript')
                    self.send_header('Cache-Control','public, max-age=3600')
                    self.send_header('Content-Encoding','gzip')
                    if cookie[0]:self.send_header('Set-Cookie','fixture=x; Path=/')
                else:
                    body=b'<script src="/rsrc.php/v4/a/r/script.js"></script>'
                    self.send_response(200);self.send_header('Content-Type','text/html')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=Thread(target=lambda:http.serve_forever(poll_interval=.05));worker.start()
        context=self.browser.new_context()
        cache=AssetCache(context,eligible_url=lambda url:'/rsrc.php/' in url)
        page=context.new_page()
        try:
            for identifier in (1,2):
                page.goto('http://127.0.0.1:'+str(http.server_port)+'/'+str(identifier),wait_until='load')
                self.assertEqual(page.evaluate('assetValue'),'preserved')
            self.assertEqual(len(counts),1)
            self.assertEqual(cache.stats['hits'],1)
            context.close()
            cookie[0]=True
            context=self.browser.new_context()
            cache=AssetCache(context,eligible_url=lambda url:'/rsrc.php/' in url)
            page=context.new_page()
            for identifier in (3,4):
                page.goto('http://127.0.0.1:'+str(http.server_port)+'/'+str(identifier),wait_until='load')
                self.assertEqual(page.evaluate('assetValue'),'preserved')
            self.assertEqual(len(counts),3)
            self.assertEqual(cache.stats['stored'],0)
        finally:context.close();http.shutdown();http.server_close();worker.join()

    def test_expired_entries_and_aged_responses_are_not_reused(self):
        self.cache.clock=lambda:0
        self.visit(111111)
        self.cache.clock=lambda:3601
        self.visit(222222)
        self.assertEqual(self.hits,2)
        self.cache._close()
        self.extra={'Age':'3601'}
        self.visit(333333);self.visit(444444)
        self.assertEqual(self.hits,4)

    def test_budget_evicts_lru_entries(self):
        self.cache.max_bytes=50;self.cache.max_entry_bytes=50
        self.visit(111111)
        # Two scripts with different immutable URLs exceed the byte budget.
        self.page.evaluate('''url=>new Promise(resolve=>{const node=document.createElement('script');node.src=url;node.onload=resolve;document.body.append(node);})''',ASSET+'?second')
        deadline=time.monotonic()+2
        while not self.cache.stats['evictions'] and time.monotonic()<deadline:self.page.wait_for_timeout(10)
        self.assertLessEqual(self.cache.bytes,50)
        self.assertEqual(self.cache.stats['evictions'],1)

    def test_navigation_guard_remains_effective(self):
        with patch('os.environ',dict(__import__('os').environ,FACEBOOK_ASSET_CACHE='0')):
            scraper._install_group_navigation_guard(self.context,self.page,{'230048322891238'},lambda *args:None)
        self.page.goto(GROUP,wait_until='load')
        try:self.page.goto('https://www.facebook.com/profile.php?id=999',wait_until='commit')
        except Exception:pass
        self.assertEqual(self.page.url,GROUP)
        self.assertEqual(self.page.evaluate('assetValue'),'preserved')

    def test_graphql_and_non_static_urls_are_excluded(self):
        self.visit(111111)
        before=self.hits
        for _ in range(2):self.page.evaluate('async()=>await (await fetch("/api/graphql/",{method:"POST"})).json()')
        self.assertEqual(self.hits-before,2)
        self.assertFalse(static_asset('https://www.facebook.com/api/graphql/'))
        self.assertFalse(static_asset('https://evil.example/rsrc.php/v4/a/r/fixture.js'))
        self.assertFalse(static_asset('https://static.xx.fbcdn.net/photo.png'))


if __name__=='__main__':unittest.main()
