"""Server checks use a temporary database and an ephemeral loopback port."""
from pathlib import Path
import sys
import tempfile
import json
import sqlite3
import subprocess
from types import SimpleNamespace
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.request import urlopen
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import local_server as server
import local_store as store


class ServerRegression(unittest.TestCase):
    def test_removed_json_import_endpoint_returns_not_found(self):
        replies=[]
        handler=SimpleNamespace(path='/api/data/import',request_origin_allowed=lambda:True,
                                send_json=lambda status,payload:replies.append((status,payload)))
        server.Handler.do_POST(handler)
        self.assertEqual(replies[0][0],404)
        self.assertFalse(hasattr(server.Handler,'import_saved_data'))

    def test_cooldown_rejects_start_without_browser_or_new_job(self):
        from local_throttle import RequestGate
        source='https://www.facebook.com/groups/230048322891238'
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(store,'DATA_DIR',Path(directory)), \
             patch.object(store,'DATABASE_PATH',Path(directory)/'test.sqlite3'), \
             patch.object(server,'credentials_configured',return_value=True), \
             patch.object(server.threading,'Thread') as start:
            RequestGate().limited('1200')
            replies=[]
            handler=SimpleNamespace(read_json=lambda:{'sourceUrl':source},send_json=lambda status,data:replies.append((status,data)))
            server.Handler.start_job(handler)
            self.assertEqual(replies[0][0],429)
            self.assertGreaterEqual(replies[0][1]['retryAfterSeconds'],1199)
            start.assert_not_called()

    def test_active_job_and_summary_http(self):
        source='https://www.facebook.com/groups/230048322891238'
        job=server.Job(job_id='12345678-1234-1234-1234-123456789012',source_url=source,status='running',posts=[{'text':'fixture'}])
        with patch.object(server,'JOBS',{job.job_id:job}),patch.object(server.Handler,'log_message'):
            http=server.LocalHTTPServer(('127.0.0.1',0),server.Handler)
            worker=Thread(target=lambda:http.serve_forever(poll_interval=.05));worker.start()
            try:
                base='http://127.0.0.1:'+str(http.server_port)
                def get(path):
                    with urlopen(base+path,timeout=5) as response:return json.load(response)
                active=get('/api/jobs/active')['job']
                self.assertEqual(active['id'],job.job_id)
                self.assertNotIn('posts',active)
                self.assertNotIn('posts',get('/api/jobs/'+job.job_id+'?summary=1'))
                self.assertEqual(get('/api/jobs/'+job.job_id)['posts'],job.posts)
                self.assertIsNone(get('/api/jobs/active?sourceUrl=https%3A%2F%2Fwww.facebook.com%2Fgroups%2Fother')['job'])
            finally:http.shutdown();http.server_close();worker.join()

    def test_compact_status_omits_warehouse_but_keeps_identity(self):
        job=server.Job(job_id='12345678-1234-1234-1234-123456789012',source_url='https://www.facebook.com/groups/230048322891238')
        job.posts=[{'text':'Large warehouse'}]
        summary=server.job_payload(job,include_data=False)
        self.assertNotIn('posts',summary)
        self.assertNotIn('comments',summary)
        self.assertEqual(summary['sourceUrl'],job.source_url)
        self.assertEqual(server.job_payload(job)['posts'],job.posts)

    def test_second_server_cannot_bind_same_port(self):
        http=server.LocalHTTPServer(('127.0.0.1',0),server.Handler)
        try:
            with self.assertRaises(OSError):
                duplicate=server.LocalHTTPServer(('127.0.0.1',http.server_port),server.Handler)
                duplicate.server_close()
        finally:http.server_close()

    def test_database_failure_does_not_leave_job_queued(self):
        job=server.Job(job_id='00000000-0000-0000-0000-000000000001')
        with patch.object(server,'known_post_keys',side_effect=RuntimeError('DB unavailable')), \
             patch.object(server,'load_saved_data',side_effect=RuntimeError('DB unavailable')):
            server.run_scrape(job,{'sourceUrl':'https://www.facebook.com/groups/230048322891238','includeReplies':True})
        self.assertEqual(job.status,'failed')
        self.assertEqual(job.error,'DB unavailable')

    def test_interrupted_job_keeps_each_committed_post(self):
        import local_scraper
        source='https://www.facebook.com/groups/230048322891238'
        post={'id':'111111','url':source+'/permalink/111111/','text':'Saved before failure','author':'Author'}
        def scrape(**options):
            options['on_post'](post,[])
            raise RuntimeError('Facebook rate limit')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(store,'DATA_DIR',Path(directory)), \
             patch.object(store,'DATABASE_PATH',Path(directory)/'test.sqlite3'), \
             patch.object(store,'_SCHEMA_READY',False), \
             patch.object(server,'credentials_configured',return_value=True), \
             patch.dict(server.os.environ,{'FACEBOOK_EMAIL':'fixture','FACEBOOK_PASSWORD':'fixture'}), \
             patch.object(local_scraper,'scrape_facebook_posts',scrape):
            job=server.Job(job_id='00000000-0000-0000-0000-000000000002',source_url=source)
            server.run_scrape(job,{'sourceUrl':source,'includeReplies':True})
            self.assertEqual(job.status,'failed')
            self.assertEqual(job.error,'Facebook rate limit')
            self.assertEqual(job.posts,[post])
            self.assertEqual(store.load_saved_data(source)['posts'],[post])

    def test_saved_attachment_is_served_and_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(store,'DATA_DIR',Path(directory)),patch.object(server.Handler,'log_message'):
            media=Path(directory)/'media';media.mkdir()
            name='a'*64+'.png';(media/name).write_bytes(b'fixture-image')
            http=server.LocalHTTPServer(('127.0.0.1',0),server.Handler)
            worker=Thread(target=lambda:http.serve_forever(poll_interval=.05));worker.start()
            try:
                base='http://127.0.0.1:'+str(http.server_port)
                with urlopen(base+'/api/media/'+name,timeout=5) as response:
                    self.assertEqual(response.read(),b'fixture-image')
                    self.assertEqual(response.headers['Content-Type'],'image/png')
                with self.assertRaises(HTTPError) as failure:
                    urlopen(base+'/api/media/../../.env',timeout=5)
                self.assertEqual(failure.exception.code,404)
            finally:
                http.shutdown();http.server_close();worker.join()


class SavedChangesRegression(unittest.TestCase):
    source='https://www.facebook.com/groups/230048322891238'

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        root=Path(self.directory.name)
        self.patches=[patch.object(store,'DATA_DIR',root),patch.object(store,'DATABASE_PATH',root/'test.sqlite3'),
                      patch.object(store,'_SCHEMA_READY',False)]
        for item in self.patches:item.start()

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.directory.cleanup()

    def post(self,identifier='111111',**values):
        return {'id':identifier,'url':self.source+'/permalink/'+identifier+'/',
                'text':'Committed body','media':[],'commentsComplete':False,'ocrStatus':'pending',**values}

    def test_partial_is_visible_from_separate_connection_before_completion(self):
        post=self.post()
        store.save_post(self.source,post,[],partial=True)
        with sqlite3.connect(store.DATABASE_PATH) as separate:
            self.assertEqual(json.loads(separate.execute('SELECT post_json FROM posts').fetchone()[0]),post)
        data=store.load_saved_changes(self.source,0)
        self.assertEqual(data['posts'],[post])
        self.assertEqual(data['comments'],[])
        self.assertNotIn(post['url'].rstrip('/'),store.known_post_keys(self.source))
        self.assertEqual(store.load_saved_data(self.source)['revision'],data['revision'])

    def test_each_comment_page_commits_and_survives_reopening(self):
        post=self.post();store.save_post(self.source,post,[],partial=True)
        first=store.load_saved_changes(self.source,0)
        rows=[{'commentId':'333333','postUrl':post['url'],'text':'First page'}]
        store.save_post(self.source,{**post,'commentsCapturedCount':1},rows,partial=True)
        second=store.load_saved_changes(self.source,first['revision'])
        self.assertEqual(second['comments'],rows)
        rows.append({'commentId':'444444','postUrl':post['url'],'text':'Second page'})
        store.save_post(self.source,{**post,'commentsCapturedCount':2},rows[-1:],partial=True)
        store._SCHEMA_READY=False
        resumed=store.load_saved_changes(self.source,second['revision'])
        self.assertEqual(resumed['comments'],rows)
        self.assertEqual(store.load_saved_changes(self.source,resumed['revision'])['posts'],[])
        with store._connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM post_updates').fetchone()[0],1)

    def test_paged_updates_do_not_skip_rewritten_posts(self):
        for identifier in ('111111','222222','333333'):store.save_post(self.source,self.post(identifier),[])
        first=store.load_saved_changes(self.source,0,limit=1)
        self.assertTrue(first['hasMore'])
        store.save_post(self.source,self.post('222222',text='Updated after first page'),[])
        second=store.load_saved_changes(self.source,first['revision'],limit=1)
        third=store.load_saved_changes(self.source,second['revision'],limit=1)
        self.assertEqual(second['posts'][0]['id'],'333333')
        self.assertEqual(third['posts'][0]['text'],'Updated after first page')
        self.assertFalse(third['hasMore'])

    def test_old_database_migrates_without_rewriting_existing_content(self):
        store.save_post(self.source,self.post(),[])
        with store._connect() as c:
            c.execute('DROP TRIGGER posts_capture_insert');c.execute('DROP TRIGGER posts_capture_update')
            c.execute('DROP TABLE post_updates')
            old=c.execute('SELECT post_json,captured_at FROM posts').fetchall()
        store._SCHEMA_READY=False
        self.assertEqual(store.load_saved_data(self.source)['revision'],0)
        with store._connect() as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT post_json,captured_at FROM posts')],[tuple(r) for r in old])
        store.save_post(self.source,self.post(text='New capture'),[],partial=True)
        self.assertEqual(store.load_saved_changes(self.source,0)['posts'][0]['text'],'New capture')

    def test_aliases_resolve_and_other_sources_do_not_leak(self):
        alias='https://www.facebook.com/groups/testgroup'
        store.register_source_aliases(self.source,[alias])
        post=self.post();store.save_post(self.source,post,[])
        store.save_post('https://www.facebook.com/groups/other',self.post('222222'),[])
        self.assertEqual(store.load_saved_changes(alias,0)['posts'],[post])

    def test_rolled_back_write_does_not_publish_revision(self):
        store.save_post(self.source,self.post(),[])
        revision=store.load_saved_data(self.source)['revision']
        with self.assertRaisesRegex(RuntimeError,'rollback'):
            with store._connect() as c:
                c.execute('UPDATE posts SET captured_at=?',('uncommitted',))
                raise RuntimeError('rollback')
        self.assertEqual(store.load_saved_changes(self.source,revision)['posts'],[])
        self.assertEqual(store.load_saved_data(self.source)['revision'],revision)

    def test_capture_survives_process_exit_before_comments_and_ocr_complete(self):
        script='''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import local_store as store
store.DATA_DIR=Path(sys.argv[2]);store.DATABASE_PATH=store.DATA_DIR/'test.sqlite3'
source='https://www.facebook.com/groups/230048322891238'
post={'id':'111111','url':source+'/permalink/111111/','text':'Saved before process exit',
      'media':[],'commentsComplete':False,'ocrStatus':'pending'}
store.save_post(source,post,[{'commentId':'333333','postUrl':post['url'],'text':'Saved first page'}],partial=True)
os._exit(0)
'''
        subprocess.run([sys.executable,'-B','-c',script,str(ROOT),str(store.DATA_DIR)],check=True,timeout=15)
        saved=store.load_saved_data(self.source)
        self.assertEqual(saved['posts'][0]['text'],'Saved before process exit')
        self.assertFalse(saved['posts'][0]['commentsComplete'])
        self.assertEqual(saved['comments'][0]['text'],'Saved first page')
        self.assertEqual(store.load_saved_changes(self.source,0)['posts'],saved['posts'])
        self.assertEqual(store.known_post_keys(self.source),set())

    def test_changes_http_requires_valid_cursor_and_source(self):
        from urllib.parse import urlencode
        store.save_post(self.source,self.post(),[])
        with patch.object(server.Handler,'log_message'):
            http=server.LocalHTTPServer(('127.0.0.1',0),server.Handler)
            worker=Thread(target=lambda:http.serve_forever(poll_interval=.05));worker.start()
            try:
                base='http://127.0.0.1:'+str(http.server_port)+'/api/data/changes?'
                with urlopen(base+urlencode({'sourceUrl':self.source,'afterRevision':0}),timeout=5) as response:
                    self.assertEqual(json.load(response)['posts'],[self.post()])
                for value in ('','-1','abc','9223372036854775808'):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(base+urlencode({'sourceUrl':self.source,'afterRevision':value}),timeout=5)
                    self.assertEqual(error.exception.code,400)
                with self.assertRaises(HTTPError) as error:
                    urlopen(base+urlencode({'sourceUrl':'https://example.com/group','afterRevision':0}),timeout=5)
                self.assertEqual(error.exception.code,400)
            finally:http.shutdown();http.server_close();worker.join()
