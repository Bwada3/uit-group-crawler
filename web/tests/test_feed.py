"""Native cursor parsing and crash-safe queue checks use synthetic data only."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode,parse_qs

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import local_scraper as scraper
import local_store as store
from local_tasks import TaskQueue
from local_feed import NativeFeed,FeedResumeError,page_urls,stream_data,QUERY

GROUP='230048322891238'
SOURCE='https://www.facebook.com/groups/'+GROUP


def story(identifier='111111',group=GROUP):
    return {'node':{'__typename':'Story','post_id':identifier,
        'permalink_url':'https://www.facebook.com/groups/alias/posts/'+identifier+'/',
        'to':{'__typename':'Group','id':group,'url':'https://www.facebook.com/groups/alias/'}}}


def payload(cursor='next',identifier='111111'):
    packets=[{'data':{'node':{'__typename':'Group','id':GROUP,'group_feed':{'edges':[story(identifier)]}}}},
             {'path':['node','group_feed','edges',1],'data':story(str(int(identifier)+1))},
             {'path':['node','group_feed'],'data':{'page_info':{'end_cursor':cursor,'has_next_page':True}}}]
    return '\n'.join(json.dumps(item) for item in packets)


class FeedRegression(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        root=Path(self.directory.name)
        self.patches=[patch.object(store,'DATA_DIR',root),patch.object(store,'DATABASE_PATH',root/'test.sqlite3'),patch.object(store,'_SCHEMA_READY',False),
            patch.dict(os.environ,{'FACEBOOK_FEED_TRANSPORT':'route','FACEBOOK_FEED_BATCH_SIZE':'20'})]
        for item in self.patches:item.start()
        self.queue=TaskQueue(SOURCE)
        self.sent=[]
        self.response=SimpleNamespace(status=200,text=lambda:payload('third','222222'),dispose=lambda:None)
        self.listeners={};self.handler=None
        self.page=SimpleNamespace(on=self.on,remove_listener=self.remove_listener,route=self.route,unroute=self.unroute,
            evaluate=self.dispatch,wait_for_timeout=lambda *a:None)

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.directory.cleanup()

    def on(self,event,callback):self.listeners.setdefault(event,[]).append(callback)
    def remove_listener(self,event,callback):self.listeners[event].remove(callback)
    def route(self,pattern,callback):self.handler=callback
    def unroute(self,pattern,callback):self.handler=None
    def dispatch(self,*args):
        if not self.handler:return
        form={'fb_api_req_friendly_name':QUERY,'variables':json.dumps({'id':GROUP,'count':3,'cursor':'ui-current','sortingSetting':'TOP_POSTS'})}
        request=SimpleNamespace(url='https://www.facebook.com/api/graphql/',post_data=urlencode(form),response=lambda:self.response)
        def fallback(**kwargs):
            self.sent.append(kwargs)
            for callback in self.listeners.get('requestfinished',[]):callback(request)
        self.handler(SimpleNamespace(request=request,fallback=fallback,abort=lambda:None))

    def feed(self,resume=True,actor='fixture-account'):
        feed=NativeFeed(scraper,self.page,self.queue,{GROUP},lambda url:False,lambda *args:None,resume=resume)
        form={'fb_api_req_friendly_name':QUERY,'variables':json.dumps({'id':GROUP,'count':3,'cursor':'first','sortingSetting':'TOP_POSTS'}),
              '__user':actor,'fb_dtsg':'fake-secret-token'}
        request=SimpleNamespace(url='https://www.facebook.com/api/graphql/',post_data=urlencode(form),
            headers={'content-type':'application/x-www-form-urlencoded'},response=lambda:SimpleNamespace(status=200,text=lambda:payload()))
        feed._observe(request)
        return feed

    def test_streamed_edges_and_page_info_assemble(self):
        keys={GROUP}
        urls,state=page_urls(scraper,'for (;;);'+payload(),GROUP,keys)
        self.assertEqual(len(urls),2)
        self.assertEqual(state,{'cursor':'next','hasNext':True})
        self.assertIn('alias',keys)

    def test_existing_partial_without_task_payload_does_not_count_as_unread(self):
        old=SOURCE+'/permalink/111111/';new=SOURCE+'/permalink/222222/'
        store.save_post(SOURCE,{'url':old,'text':'Stored','commentsComplete':False},[])
        queue=TaskQueue(SOURCE);queue.add([old,new])
        self.assertEqual(queue.unread_count(),1)
        selected=queue.next(unread_only=True)
        self.assertEqual(selected['post_url'],new)
        self.assertIsNone(queue.next(unread_only=True))
        self.assertEqual(queue.next()['post_url'],old)

    def test_checkpoint_retries_follow_unseen_urls_and_keep_payload(self):
        old=SOURCE+'/permalink/111111/';new=SOURCE+'/permalink/222222/'
        self.queue.add([old]);task=self.queue.next()
        payload={'post':{'url':old,'text':'Stored','commentsComplete':False}}
        self.queue.update(task,'partial',payload)
        queue=TaskQueue(SOURCE);queue.add([new])
        self.assertEqual(queue.unread_count(),1)
        self.assertEqual(queue.next()['post_url'],new)
        recovered=queue.next()
        self.assertEqual(recovered['post_url'],old)
        self.assertEqual(json.loads(recovered['payload_json']),payload)

    def test_old_failed_url_without_capture_does_not_block_unread_work(self):
        old=SOURCE+'/permalink/111111/';new=SOURCE+'/permalink/222222/'
        self.queue.add([old]);task=self.queue.next()
        self.queue.update(task,'failed',error='Permalink post not visible')
        restarted=TaskQueue(SOURCE);restarted.add([new])
        self.assertEqual(restarted.unread_count(),1)
        self.assertEqual(restarted.next(unread_only=True)['post_url'],new)
        self.assertIsNone(restarted.next(unread_only=True))
        retry=restarted.next()
        self.assertEqual(retry['post_url'],old)
        self.assertEqual(retry['error'],'Permalink post not visible')

    def test_failed_new_read_is_deferred_instead_of_remaining_unread(self):
        url=SOURCE+'/permalink/111111/'
        self.queue.add([url]);task=self.queue.next(unread_only=True)
        self.queue.update(task,'queued',error='Permalink post not visible')
        self.assertEqual(self.queue.unread_count(),0)
        self.assertIsNone(self.queue.next(unread_only=True))
        self.assertEqual(self.queue.next()['post_url'],url)

    def test_media_recovery_precedes_unseen_posts_without_reopening_partial(self):
        old=SOURCE+'/permalink/111111/';new=SOURCE+'/permalink/222222/'
        self.queue.add([old]);task=self.queue.next()
        self.queue.update(task,'ocr_running',{'post':{'url':old,'media':[],'commentsComplete':True}})
        queue=TaskQueue(SOURCE);queue.add([new])
        self.assertEqual(queue.unread_count(),1)
        self.assertEqual(queue.next(unread_only=True)['state'],'media_pending')
        self.assertEqual(queue.next(unread_only=True)['post_url'],new)

    def test_story_observer_receives_only_verified_top_level_posts(self):
        raw=stream_data(payload());raw['node']['group_feed']['edges'][0]=story('111111','foreign')
        seen=[];urls,_=page_urls(scraper,json.dumps({'data':raw}),GROUP,{GROUP},seen.append)
        self.assertEqual([n['post_id'] for n in seen],['111112']);self.assertEqual(len(urls),1)

    def test_invalid_feed_does_not_publish_partial_story_metadata(self):
        raw=stream_data(payload());raw['node']['group_feed']['edges'][1]['node']['post_id']='invalid'
        seen=[]
        with self.assertRaises(FeedResumeError):page_urls(scraper,json.dumps({'data':raw}),GROUP,{GROUP},seen.append)
        self.assertFalse(seen)

    def test_foreign_group_cannot_pollute_queue(self):
        data=stream_data(payload())
        data['node']['group_feed']['edges'][0]=story('111111','foreign')
        urls,_=page_urls(scraper,json.dumps({'data':data}),GROUP,{GROUP})
        self.assertEqual(len(urls),1)

    def test_comment_and_shared_story_ids_are_not_used(self):
        data=stream_data(payload())
        node=data['node']['group_feed']['edges'][0]['node']
        node['feedback']={'comments':[{'post_id':'999999'}]}
        node['attached_story']=story('888888')['node']
        urls,_=page_urls(scraper,json.dumps({'data':data}),GROUP,{GROUP})
        self.assertEqual(len(urls),2)
        self.assertFalse(any('999999' in url or '888888' in url for url in urls))

    def test_section_header_is_not_a_post_but_unknown_units_are_rejected(self):
        data=stream_data(payload())
        edge=data['node']['group_feed']['edges'][0]
        edge['node']={'__typename':'GroupsSectionHeaderUnit','title':'Header'}
        urls,_=page_urls(scraper,json.dumps({'data':data}),GROUP,{GROUP})
        self.assertEqual(len(urls),1)
        edge['node']['__typename']='UnknownUnit'
        with self.assertRaises(FeedResumeError):page_urls(scraper,json.dumps({'data':data}),GROUP,{GROUP})

    def test_incomplete_or_mismatched_page_is_rejected(self):
        data=stream_data(payload())
        data['node']['group_feed'].pop('page_info')
        with self.assertRaises(FeedResumeError):page_urls(scraper,json.dumps({'data':data}),GROUP,{GROUP})
        data=stream_data(payload());data['node']['id']='other'
        with self.assertRaises(FeedResumeError):page_urls(scraper,json.dumps({'data':data}),GROUP,{GROUP})
        with self.assertRaises(FeedResumeError):stream_data('{"errors":[{"message":"failure"}]}')

    def test_cursor_and_urls_commit_atomically(self):
        url=SOURCE+'/permalink/111111/'
        self.queue.commit_feed_page([url],{'cursor':'first'})
        with store._connect() as connection:
            connection.execute("CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON feed_checkpoints BEGIN SELECT RAISE(ABORT,'fixture failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            self.queue.commit_feed_page([SOURCE+'/permalink/222222/'],{'cursor':'second'})
        self.assertEqual(self.queue.identities(),{'111111'})
        self.assertEqual(self.queue.feed_checkpoint()['cursor'],'first')

    def test_restart_reuses_exact_cursor_and_keeps_auth_only_in_ram(self):
        first=self.feed()
        self.assertTrue(first.bootstrap(lambda:False))
        self.response.text=lambda:payload('next')
        first.fetch_next()
        self.assertIsNone(json.loads(parse_qs(self.sent[0]['post_data'])['variables'][0])['cursor'])
        saved=self.queue.feed_checkpoint()
        first.close()
        self.response.text=lambda:payload('third','222222')
        restarted=self.feed()
        self.assertTrue(restarted.bootstrap(lambda:False))
        self.assertTrue(restarted.resumed)
        restarted.fetch_next()
        variables=json.loads(parse_qs(self.sent[-1]['post_data'])['variables'][0])
        self.assertEqual(variables['cursor'],saved['cursor'])
        self.assertEqual(variables['count'],20)
        self.assertEqual(self.queue.feed_checkpoint()['cursor'],'third')
        serialized=json.dumps(self.queue.feed_checkpoint())
        self.assertNotIn('fake-secret-token',serialized)
        self.assertNotIn('fixture-account',serialized)
        self.assertEqual(self.queue.identities(),{'111111','111112','222222','222223'})

    def test_account_change_preserves_checkpoint_and_requires_explicit_fresh_scan(self):
        first=self.feed();first.bootstrap(lambda:False);first.fetch_next()
        before=self.queue.feed_checkpoint()
        changed=self.feed(actor='different-account')
        with self.assertRaises(FeedResumeError):changed.bootstrap(lambda:False)
        self.assertEqual(self.queue.feed_checkpoint(),before)
        fresh=self.feed(resume=False,actor='different-account')
        self.assertTrue(fresh.bootstrap(lambda:False))
        self.assertFalse(fresh.resumed)

    def test_browser_fetch_queues_page_without_scrolling_or_replaying_stored_auth(self):
        feed=self.feed();feed.bootstrap(lambda:False)
        calls=[]
        def evaluate(expression,args):
            calls.append(args)
            self.assertIn('fetch(',expression)
            return {'status':200,'text':payload('next')}
        with patch.dict(os.environ,{'FACEBOOK_FEED_TRANSPORT':'fetch'}),patch.object(self.page,'evaluate',evaluate):
            feed.fetch_next()
        self.assertEqual(self.queue.identities(),{'111111','111112'})
        variables=json.loads(parse_qs(calls[0]['body'])['variables'][0])
        self.assertIsNone(variables['cursor'])
        self.assertIn('fake-secret-token',parse_qs(calls[0]['body'])['fb_dtsg'])
        self.assertNotIn('fake-secret-token',json.dumps(self.queue.feed_checkpoint()))
        self.assertIsNone(self.handler)

    def test_browser_fetch_failure_preserves_prior_cursor(self):
        feed=self.feed();feed.bootstrap(lambda:False)
        before=self.queue.feed_checkpoint()
        with patch.dict(os.environ,{'FACEBOOK_FEED_TRANSPORT':'fetch'}),patch.object(self.page,'evaluate',side_effect=RuntimeError('disconnected')):
            with self.assertRaises(FeedResumeError):feed.fetch_next()
        self.assertEqual(self.queue.feed_checkpoint(),before)

    def test_rate_limit_or_invalid_page_never_advances_checkpoint(self):
        feed=self.feed();feed.bootstrap(lambda:False);feed.fetch_next()
        before=self.queue.feed_checkpoint()
        self.response.status=429
        with self.assertRaises(FeedResumeError):feed.fetch_next()
        self.assertEqual(self.queue.feed_checkpoint(),before)
        self.response.status=200;self.response.text=lambda:'{"data":{"node":null}}'
        with self.assertRaises(FeedResumeError):feed.fetch_next()
        self.assertEqual(self.queue.feed_checkpoint(),before)


if __name__=='__main__':unittest.main()
