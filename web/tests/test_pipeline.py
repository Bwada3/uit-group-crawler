"""Regression checks for durable discovery, fixed workers and CPU overlap."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import local_scraper as scraper
import local_store as store
from local_tasks import TaskQueue
from local_media import MediaPipeline
from playwright.sync_api import sync_playwright

GROUP='https://www.facebook.com/groups/230048322891238'
KEYS={'230048322891238'}
URLS=[GROUP+f'/permalink/{identifier}/' for identifier in (111111,222222)]


class PipelineRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager=sync_playwright()
        cls.playwright=cls.manager.__enter__()
        cls.browser=cls.playwright.chromium.launch(channel='chrome',headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.manager.__exit__(None,None,None)

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        root=Path(self.directory.name)
        self.patches=[patch.object(store,'DATA_DIR',root),patch.object(store,'DATABASE_PATH',root/'test.sqlite3'),
            patch.object(store,'_SCHEMA_READY',False),patch.object(scraper,'PROFILE_DIR',root/'profile'),
            patch.dict(os.environ,{'FACEBOOK_BROWSER_WORKERS':'0','FACEBOOK_NATIVE_FEED':'0','FACEBOOK_RESPONSE_READER':'0',
                                  'FACEBOOK_REQUEST_INTERVAL_SECONDS':'0'})]
        for item in self.patches:item.start()
        self.context=self.browser.new_context()
        self.context.set_default_timeout(1000)
        self.page=self.context.new_page()
        self.context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='<body></body>'))
        self.page.goto(GROUP)

    def tearDown(self):
        if not self.page.is_closed():self.context.close()
        for item in reversed(self.patches):item.stop()
        self.directory.cleanup()

    def existing(self):
        context=self.context
        class Existing:
            def __enter__(self):return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda **kwargs:context))
            def __exit__(self,*args):return False
        return Existing

    def feed(self, second_missing=False):
        first='<div id="first" role="article"><a href="'+URLS[0]+'">time</a><div data-ad-rendering-role="story_message">First post</div><button>Like</button><button>Comment</button></div>'
        link='' if second_missing else '<a href="'+URLS[1]+'">time</a>'
        return first+'<div id="second" role="article">'+link+'<div data-ad-rendering-role="story_message">Second post</div><button>Like</button><button aria-label="5 comments" onclick="window.open(\''+URLS[1]+'\')">5</button></div>'

    def route_feed(self, second_missing=False):
        self.context.unroute('**/*')
        feed=self.feed(second_missing)
        def route(request):
            detail='<div id="detail" role="dialog"><strong>Author</strong><div data-ad-rendering-role="story_message">Detail</div><button>All comments</button></div>'
            request.fulfill(status=200,content_type='text/html',body=detail if '/permalink/' in request.request.url else feed)
        self.context.route('**/*',route)
        self.page.goto(GROUP)

    def run_group(self, **options):
        with patch.object(scraper,'sync_playwright',self.existing()),patch.object(scraper,'_open_source_page',lambda *args:None):
            return scraper.scrape_facebook_posts(email='',password='',source_url=GROUP,include_replies=True,
                progress=lambda *args:None,cancelled=lambda:False,known_group_keys=KEYS,max_posts=options.pop('max_posts',2),**options)

    def test_foreign_group_cannot_become_alias(self):
        keys=set(KEYS)
        value=scraper._post_url_from_hrefs(['https://www.facebook.com/groups/foreign/permalink/999/?comment_id=1'],keys)
        self.assertEqual(value,'')
        self.assertEqual(keys,KEYS)

    def test_recovered_queue_finishes_before_resuming_feed_without_dom_discovery(self):
        self.route_feed()
        queue=TaskQueue(GROUP);queue.add([URLS[0]])
        events=[]
        class FakeFeed:
            def __init__(self,adapter,page,queue,keys,known,progress,resume=True):
                self.queue=queue;self.ready=False;self.saved={'cursor':'saved'};self.added=0;self.ids=set()
            def bootstrap(self,cancelled):events.append('resume-feed');self.ready=True;return True
            def fetch_next(self):
                self.added+=self.queue.add([URLS[1]])
                self.ids.add('222222')
            def close(self):pass
        def extract(article,include_replies,page,*args,**kwargs):
            events.append(page.url)
            return {'id':page.url,'url':page.url,'text':'Post','author':'Author','media':[],'comments':[],'commentsComplete':True},[]
        with patch.dict(os.environ,{'FACEBOOK_NATIVE_FEED':'1'}),patch('local_feed.NativeFeed',FakeFeed), \
             patch('local_discovery.discover',side_effect=AssertionError('Native feed must not revisit DOM cards')), \
             patch.object(scraper,'_extract_post',extract):
            posts,_=self.run_group()
        self.assertEqual(len(posts),2)
        self.assertLess(events.index(URLS[0]),events.index('resume-feed'))
        self.assertLess(events.index('resume-feed'),events.index(URLS[1]))

    def test_resolved_card_cached_until_dom_changes(self):
        from local_discovery import discover
        self.page.set_content(self.feed(second_missing=True))
        with patch.object(scraper,'_resolve_feed_permalink',return_value=''), \
             patch.object(scraper,'_click_comment_for_permalink',return_value=URLS[1]) as click:
            first,_=discover(scraper,self.page,set(KEYS),lambda url:False,lambda:False)
            second,_=discover(scraper,self.page,set(KEYS),lambda url:True,lambda:False)
            self.assertEqual(set(first),set(URLS))
            self.assertEqual(second,[])
            self.assertEqual(click.call_count,1)
            # A recycled element must be resolved again, even if story text
            # has not changed. No text-based duplicate detection is allowed.
            self.page.locator('#second button').last.evaluate("node=>node.setAttribute('aria-label','6 comments')")
            discover(scraper,self.page,set(KEYS),lambda url:True,lambda:False)
            self.assertEqual(click.call_count,2)

    def test_newest_sort_waits_for_delayed_menu(self):
        self.page.set_content('''<button id="sort" onclick="setTimeout(()=>{const menu=document.createElement('div');menu.setAttribute('role','menu');menu.innerHTML='<button role=menuitem onclick=selectNewest()>New posts</button>';document.body.append(menu)},300)">Recent activity</button>
            <script>window.selected=false;function selectNewest(){window.selected=true;document.querySelector('[role=menu]').remove();sort.textContent='New posts';}</script>''')
        self.assertTrue(scraper._select_newest_feed(self.page))
        self.assertTrue(self.page.evaluate('selected'))

    def test_existing_newest_sort_does_not_open_menu(self):
        self.page.set_content('<button onclick="window.badClick=true">New posts</button>')
        self.assertTrue(scraper._select_newest_feed(self.page))
        self.assertFalse(self.page.evaluate('!!window.badClick'))

    def test_virtual_comment_tail_is_not_skipped(self):
        self.page.set_content('''<div id="post"><div id="thread" style="height:200px;overflow-y:auto">
            <div role="article" aria-label="Comment by Person" style="height:100px"><div dir="auto">First visible comment</div></div>
            <div style="height:10000px"></div></div></div>''')
        state=scraper._scroll_comment_thread(self.page.locator('#post'))
        self.assertTrue(state['available'])
        self.assertLessEqual(state['after'],200)

    def test_pending_comment_response_is_not_treated_as_end(self):
        self.page.goto(URLS[0])
        self.page.set_content('''<div id="post" role="dialog"><div data-ad-rendering-role="story_message">Post</div><button aria-label="1 comment">1</button></div>
            <script>setTimeout(()=>{const row=document.createElement('div');row.setAttribute('role','article');row.setAttribute('aria-label','Comment by Person');
            row.innerHTML='<a href="?comment_id=1">time</a><div dir="auto">Delayed comment</div>';post.append(row);},2700);</script>''')
        state={'pending':{'fixture'},'last':0}
        self.page._grouppulse_comment_network=state
        def finish():
            state['pending'].clear();state['last']=time.monotonic()
        timer=threading.Timer(2.5,finish);timer.start()
        try:
            rows=scraper._expand_post_content(self.page.locator('#post'),True,self.page)
            self.assertEqual(len(rows),1)
        finally:timer.join()

    def test_independent_reply_controls_start_as_a_batch(self):
        self.page.goto(URLS[0])
        self.page.set_content('''<div id="post" role="dialog"><div data-ad-rendering-role="story_message">Post</div><button aria-label="4 comments">4</button>
            <button onclick="expand(this,1)">View 1 reply</button><button onclick="expand(this,2)">View 1 reply</button>
            <button onclick="expand(this,3)">View 1 reply</button><button onclick="expand(this,4)">View 1 reply</button></div>
            <script>window.clickTimes=[];function expand(button,id){clickTimes.push(performance.now());button.remove();setTimeout(()=>{
              const row=document.createElement('div');row.setAttribute('role','article');row.setAttribute('aria-label','Comment by Person');
              row.innerHTML='<a href="?comment_id='+id+'">time</a><div dir="auto">Reply '+id+'</div>';post.append(row);},300);}</script>''')
        rows=scraper._expand_post_content(self.page.locator('#post'),True,self.page)
        self.assertEqual(len(rows),4)
        times=self.page.evaluate('clickTimes')
        self.assertLess(max(times)-min(times),300)

    def test_equal_text_does_not_merge_post_containers(self):
        self.page.goto(URLS[0])
        self.page.set_content('<main role="main">'+''.join('<section><div data-ad-rendering-role="story_message">Same</div><div role="article" aria-label="Comment by Person"><div dir="auto">Comment '+str(i)+'</div></div></section>' for i in range(2))+'</main>')
        root=scraper._permalink_container(self.page)
        self.assertIsNotNone(root)
        self.assertEqual(root.locator('[data-ad-rendering-role="story_message"]').count(),1)

    def test_image_only_post_without_share_is_discovered(self):
        from local_discovery import discover
        self.page.set_content('<div role="article"><a href="'+URLS[0]+'">time</a><img width="300" height="200" src="https://test.fbcdn.net/img.png"><button>Like</button><button>Comment</button></div>')
        urls,changed=discover(scraper,self.page,set(KEYS),lambda value:False,lambda:False)
        self.assertEqual(urls,[URLS[0]])
        self.assertFalse(changed)
        self.page.goto(URLS[0])
        self.page.set_content('<div role="dialog"><strong>Author</strong><img width="300" height="200" src="https://test.fbcdn.net/img.png"><button>Comment</button></div>')
        self.assertIsNotNone(scraper._permalink_container(self.page))

    def test_image_only_post_without_article_role_is_discovered(self):
        from local_discovery import discover
        self.page.set_content('<section><a href="'+URLS[0]+'">time</a><img width="300" height="200" src="https://test.fbcdn.net/img.png"><button>Like</button><button>Comment</button></section>')
        urls,changed=discover(scraper,self.page,set(KEYS),lambda value:False,lambda:False)
        self.assertEqual(urls,[URLS[0]])
        self.assertFalse(changed)

    def test_loaded_known_feed_batch_advances_without_skipping_virtual_gap(self):
        from local_discovery import discover,advance_feed
        feed=''.join('<section style="height:1500px"><a href="'+url+'">time</a><div data-ad-rendering-role="story_message">Post</div><button>Like</button><button>Comment</button></section>' for url in URLS)
        self.page.set_content(feed)
        discover(scraper,self.page,set(KEYS),lambda value:True,lambda:False)
        advance_feed(self.page)
        self.assertGreater(self.page.evaluate('scrollY'),800)
        self.page.set_content('<main><img width="500" height="300" src="https://test.fbcdn.net/cover.png">'+feed.replace('</section>','</section><div style="height:10000px"></div>',1)+'</main>')
        self.page.evaluate('window.scrollTo(0,0)')
        discover(scraper,self.page,set(KEYS),lambda value:True,lambda:False)
        advance_feed(self.page)
        self.assertLessEqual(self.page.evaluate('scrollY'),800)

    def test_permalink_resolved_before_another_post_replaces_feed(self):
        self.route_feed(second_missing=True)
        reads=[]
        def extract(article,include_replies,page,*args,**kwargs):
            reads.append(page.url)
            if len(reads)==1:
                self.page.evaluate("document.getElementById('second').remove()")
            return {'id':page.url,'url':page.url,'text':'Post','author':'Author','media':[],'comments':[],'commentsComplete':True},[]
        with patch.object(scraper,'_extract_post',extract):
            posts,_=self.run_group()
        self.assertEqual(set(reads),set(URLS))
        self.assertEqual(len(posts),2)
        self.assertEqual(len(store.known_post_keys(GROUP)),2)

    def test_two_fixed_readers_overlap_and_inherit_auth(self):
        self.route_feed()
        self.context.add_cookies([{'name':'fixture-session','value':'fixture','domain':'.facebook.com','path':'/'}])
        observed=[]
        def observer(context):
            observed.append(any(item['name']=='fixture-session' for item in context.cookies()))
            context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='<div role="dialog"><strong>Author</strong><div data-ad-rendering-role="story_message">Detail</div><button>All comments</button></div>'))
        active=maximum=0
        lock=threading.Lock()
        original=scraper._open_post_permalink
        def opened(page,url):
            nonlocal active,maximum
            with lock:
                active+=1
                maximum=max(maximum,active)
            try:return original(page,url)
            finally:
                with lock:active-=1
        with patch.dict(os.environ,{'FACEBOOK_BROWSER_WORKERS':'2'}),patch.object(scraper,'_worker_context_observer',observer,create=True), \
             patch.object(scraper,'_open_post_permalink',opened),patch('playwright.sync_api.Page.bring_to_front') as focus:
            posts,_=self.run_group()
        self.assertEqual(maximum,2)
        self.assertEqual(observed,[True,True])
        self.assertEqual(focus.call_count,1)
        self.assertEqual(len(posts),2)
        self.assertTrue(all(post['ocrStatus']=='no_image' for post in posts))

    def test_ocr_overlaps_next_browser_read(self):
        self.route_feed()
        release=threading.Event()
        started=threading.Event()
        media_dir=store.DATA_DIR/'media';media_dir.mkdir()
        name='a'*64+'.png';(media_dir/name).write_bytes(b'fixture')
        order=[]
        def recognize(raw):
            started.set()
            self.assertTrue(release.wait(5),'Browser waited for OCR before reading the next post')
            order.append('ocr-done')
            return 'Text'
        def extract(article,include_replies,page,*args,**kwargs):
            order.append(page.url)
            if page.url==URLS[1]:
                self.assertTrue(started.wait(1))
                release.set()
            media=[{'type':'image','url':'https://test.fbcdn.net/a.png','localPath':'data/media/'+name,'ocrStatus':'pending'}]
            return {'id':page.url,'url':page.url,'text':'Post','author':'Author','media':media,'comments':[],'commentsComplete':True},[]
        with patch.object(scraper,'_extract_post',extract),patch.object(scraper,'_ocr_image',recognize):
            posts,_=self.run_group()
        self.assertLess(order.index(URLS[1]),order.index('ocr-done'))
        self.assertTrue(all(post['ocrText']=='Text' for post in posts))

    def test_response_workers_keep_durable_save_and_skip_dom_when_complete(self):
        self.route_feed();observed=[]
        def observer(context):
            context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='<body></body>'))
        def read(reader,url,*args):
            observed.append(url)
            return {'id':url,'url':url,'text':'Response post','author':'Author','media':[],'comments':[],'commentsComplete':True}
        with patch.dict(os.environ,{'FACEBOOK_BROWSER_WORKERS':'2','FACEBOOK_RESPONSE_READER':'1'}), \
             patch.object(scraper,'_worker_context_observer',observer,create=True), \
             patch('local_response_reader.QueryRegistry.get',return_value={'fixture':True}), \
             patch('local_response_reader.ResponseReader.try_read',read), \
             patch.object(scraper,'_open_post_permalink',side_effect=AssertionError('Should not open DOM')):
            posts,_=self.run_group()
        self.assertEqual(set(observed),set(URLS));self.assertEqual(len(posts),2)
        self.assertEqual(len(store.known_post_keys(GROUP)),2)

    def test_response_rate_limit_stops_and_keeps_tasks_queued(self):
        from local_response_reader import ResponseRateLimit
        self.route_feed()
        def observer(context):context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='<body></body>'))
        with patch.dict(os.environ,{'FACEBOOK_BROWSER_WORKERS':'2','FACEBOOK_RESPONSE_READER':'1'}), \
             patch.object(scraper,'_worker_context_observer',observer,create=True), \
             patch('local_response_reader.QueryRegistry.get',return_value={'fixture':True}), \
             patch('local_response_reader.ResponseReader.try_read',side_effect=ResponseRateLimit('Rate limit')), \
             patch.object(scraper,'_open_post_permalink',side_effect=AssertionError('Do not fall back after rate limit')):
            with self.assertRaisesRegex(RuntimeError,'Facebook'):self.run_group()
        with store._connect() as c:
            states=[row[0] for row in c.execute('SELECT state FROM scrape_tasks')]
        self.assertEqual(states,['queued','queued'])

    def test_rate_limit_drain_saves_successful_reads_and_ocr(self):
        self.rate_limit_drain(0)

    def test_rate_limit_drain_saves_read_after_multiple_failures(self):
        self.rate_limit_drain(2)

    def rate_limit_drain(self,successful_index):
        from concurrent.futures import Future
        from local_response_reader import ResponseRateLimit
        self.route_feed()
        urls=[GROUP+f'/permalink/{identifier}/' for identifier in (111111,222222,333333,444444)]
        queue=TaskQueue(GROUP);queue.add(urls)
        submitted=[]
        ocr=Future()
        captured=[]

        class FakeWorkers:
            def __init__(self,*args):self.keys=set(KEYS)
            def submit(self,task):
                future=Future();submitted.append((task,future))
                # Delay all browser completions until all three readers run.
                # One succeeds while two independently report a rate limit.
                if len(submitted)==3:
                    for index,(_,result) in enumerate(submitted):
                        if index==successful_index:result.set_result(post(urls[index]))
                        else:result.set_exception(ResponseRateLimit('Rate limit'))
                return future
            def close(self):ocr.set_result(captured[0])

        def post(url):
            return {'id':url,'url':url,'text':'Captured before rate limit','author':'Author',
                    'media':[],'comments':[],'commentsComplete':True,'ocrStatus':'no_image'}

        def submit_ocr(pipeline,value):
            captured.append(value)
            return ocr

        with patch.dict(os.environ,{'FACEBOOK_BROWSER_WORKERS':'3'}), \
             patch('local_workers.DetailWorkers',FakeWorkers), \
             patch.object(MediaPipeline,'submit',submit_ocr):
            with self.assertRaisesRegex(RuntimeError,'Facebook'):self.run_group(max_posts=3)
        saved=store.load_saved_data(GROUP)
        self.assertEqual([item['url'] for item in saved['posts']],[urls[successful_index]])
        with store._connect() as connection:
            states=dict(connection.execute('SELECT post_id,state FROM scrape_tasks'))
        self.assertEqual(states,{str(identifier):'done' if index==successful_index else 'queued'
                                for index,identifier in enumerate((111111,222222,333333,444444))})

    def test_task_checkpoint_survives_restart_without_reopening_done(self):
        queue=TaskQueue(GROUP);queue.add(URLS)
        first=queue.next()
        queue.update(first,'ocr_running',{'post':{'text':'Checkpoint','media':[]}})
        restarted=TaskQueue(GROUP)
        recovered=restarted.next()
        self.assertEqual(json.loads(recovered['payload_json'])['post']['text'],'Checkpoint')
        restarted.update(recovered,'done')
        self.assertEqual(restarted.next()['post_url'],URLS[1])

    def test_retry_preserves_checkpoint_written_after_task_was_dequeued(self):
        queue=TaskQueue(GROUP);queue.add([URLS[0]])
        stale=queue.next()
        post={'id':URLS[0],'url':URLS[0],'text':'Already saved','commentsComplete':False}
        queue.update(stale,'reading',{'post':post})
        queue.update(stale,'queued',error='Facebook rate limit')
        recovered=TaskQueue(GROUP).next()
        self.assertEqual(json.loads(recovered['payload_json'])['post'],post)

    def test_worker_partial_content_and_comments_survive_rate_limit(self):
        from local_response_reader import ResponseRateLimit
        self.route_feed()
        def observer(context):context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='<body></body>'))
        def read(reader,url,*args):
            post={'id':url,'url':url,'text':'Saved immediately','author':'Author','media':[],
                  'comments':[],'commentsCount':2,'commentsCapturedCount':0,'commentsComplete':False}
            reader.on_capture(post,[])
            row={'commentId':str(int(url.rstrip('/').rsplit('/',1)[1])+7),'text':'Saved first page'}
            post.update(comments=[row],commentsCapturedCount=1)
            reader.on_capture(post,[row])
            raise ResponseRateLimit('Rate limit')
        with patch.dict(os.environ,{'FACEBOOK_BROWSER_WORKERS':'2','FACEBOOK_RESPONSE_READER':'1'}), \
             patch.object(scraper,'_worker_context_observer',observer,create=True), \
             patch('local_response_reader.QueryRegistry.get',return_value={'fixture':True}), \
             patch('local_response_reader.ResponseReader.try_read',read):
            with self.assertRaisesRegex(RuntimeError,'Facebook'):self.run_group()
        saved=store.load_saved_data(GROUP)
        self.assertEqual(len(saved['posts']),2)
        self.assertEqual(len(saved['comments']),2)
        self.assertTrue(all(not row['commentsComplete'] for row in saved['posts']))
        self.assertTrue(all(row['postId']==row['postUrl']==row['parentPostUrl'] for row in saved['comments']))
        self.assertEqual(store.known_post_keys(GROUP),set())
        with store._connect() as connection:
            tasks=connection.execute('SELECT state,payload_json FROM scrape_tasks').fetchall()
        self.assertTrue(all(row[0]=='queued' and json.loads(row[1])['post']['commentsCapturedCount']==1 for row in tasks))

    def test_dom_content_survives_failure_before_comments_open(self):
        self.route_feed()
        with patch.object(scraper,'_select_all_comments',side_effect=RuntimeError('Comment request failed')):
            with self.assertRaises(RuntimeError):self.run_group(replay_urls=[URLS[0]])
        saved=store.load_saved_data(GROUP)
        self.assertEqual(len(saved['posts']),1)
        self.assertEqual(saved['posts'][0]['text'],'Detail')
        self.assertFalse(saved['posts'][0]['commentsComplete'])
        self.assertNotIn(URLS[0].rstrip('/'),store.known_post_keys(GROUP))
        resumed=TaskQueue(GROUP).next()
        self.assertEqual(json.loads(resumed['payload_json'])['post']['text'],'Detail')

    def test_captured_post_is_committed_before_ocr_starts(self):
        self.route_feed()
        submitted=[]
        def recognize(pipeline,post):
            saved=store.load_saved_data(GROUP)
            self.assertIn(post['url'],[row['url'] for row in saved['posts']])
            self.assertNotIn(post['url'].rstrip('/'),store.known_post_keys(GROUP))
            submitted.append(post['url'])
            from concurrent.futures import Future
            result=Future();result.set_result({**post,'ocrStatus':'no_image'});return result
        with patch.object(MediaPipeline,'submit',recognize):self.run_group()
        self.assertEqual(set(submitted),set(URLS))
        self.assertEqual(len(store.known_post_keys(GROUP)),2)

    def test_partial_comment_checkpoint_is_read_again(self):
        self.route_feed()
        queue=TaskQueue(GROUP);queue.add([URLS[0]])
        task=queue.next()
        queue.update(task,'partial',{'post':{'id':URLS[0],'url':URLS[0],'text':'Partial','media':[],'comments':[],'commentsComplete':False}})
        reads=[]
        def extract(article,include_replies,page,*args,**kwargs):
            reads.append(page.url)
            return {'id':page.url,'url':page.url,'text':'Read again','author':'Author','media':[],'comments':[],'commentsComplete':True},[]
        with patch.object(scraper,'_extract_post',extract):
            posts,_=self.run_group()
        self.assertIn(URLS[0],reads)
        self.assertEqual(len(posts),2)

    def test_profile_lease_rejects_second_owner_and_recovers(self):
        from local_profile import ProfileLease
        with ProfileLease(scraper.PROFILE_DIR):
            with self.assertRaises(RuntimeError):
                with ProfileLease(scraper.PROFILE_DIR):pass
        with ProfileLease(scraper.PROFILE_DIR):pass

    def test_no_result_accumulation_keeps_persisted_data(self):
        self.route_feed()
        result=self.run_group(collect_results=False)
        self.assertEqual(result,([],[]))
        self.assertEqual(len(store.load_saved_data(GROUP)['posts']),2)

    def test_partial_posts_resume_but_legacy_posts_still_skip(self):
        store.save_post(GROUP,{'url':URLS[0],'commentsComplete':False},[])
        store.save_post(GROUP,{'url':URLS[1]},[])
        self.assertNotIn(URLS[0].rstrip('/'),store.known_post_keys(GROUP))
        self.assertIn(URLS[1].rstrip('/'),store.known_post_keys(GROUP))

    def test_media_files_and_ocr_cache_use_content_hash(self):
        parent=threading.get_ident();downloads=[];recognitions=[]
        def get(*args,**kwargs):
            downloads.append(threading.get_ident())
            return SimpleNamespace(ok=True,headers={'content-type':'image/png'},body=lambda:b'fixture-image',dispose=lambda:None)
        page=SimpleNamespace(url=URLS[0],context=SimpleNamespace(request=SimpleNamespace(get=get)))
        def recognize(raw):
            recognitions.append(threading.get_ident());return 'Cached text'
        pipeline=MediaPipeline(recognize)
        try:
            for url in ('https://test.fbcdn.net/a.png?one','https://test.fbcdn.net/a.png?two'):
                media=pipeline.prepare([{'type':'image','url':url}],page)
                post=pipeline.submit({'media':media}).result(timeout=5)
                self.assertEqual(post['ocrStatus'],'complete')
                self.assertTrue((store.DATA_DIR/'media'/Path(media[0]['localPath']).name).is_file())
            self.assertEqual(downloads,[parent,parent])
            self.assertEqual(len(recognitions),1)
            self.assertNotEqual(recognitions[0],parent)
        finally:pipeline.close()


if __name__=='__main__':unittest.main()
