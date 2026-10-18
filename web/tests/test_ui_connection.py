"""Browser recovery checks; every request is fulfilled locally, never Facebook."""
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
BASE='http://127.0.0.1:4173'
GROUP='https://www.facebook.com/groups/230048322891238'
JOB='12345678-1234-1234-1234-123456789012'


class ConnectionRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager=sync_playwright()
        cls.p=cls.manager.__enter__()
        cls.browser=cls.p.chromium.launch(channel='chrome',headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.manager.__exit__(None,None,None)

    def setUp(self):
        self.context=self.browser.new_context()
        self.page=self.context.new_page()
        self.posts=0;self.polls=0;self.active=False;self.complete=False
        self.start_inputs=[]
        self.saved_posts=[];self.saved_comments=[];self.job_error=''
        self.saved_revision=0
        self.change_data={'revision':0,'posts':[],'comments':[]}
        self.change_requests=[];self.fail_changes=False
        self.fail_poll=False;self.fail_start=False;self.fail_health=0;self.error_status=0
        self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.context.route('**/*',self.route)
        self.page.goto(BASE)
        self.page.wait_for_function('state.localBackend')

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors,[])

    def reply(self,route,value,status=200):
        route.fulfill(status=status,content_type='application/json',body=json.dumps(value))

    def route(self,route):
        request=route.request;parsed=urlsplit(request.url);path=parsed.path
        if parsed.netloc!='127.0.0.1:4173':route.abort();return
        if path=='/api/health':
            if self.fail_health:
                self.fail_health-=1;route.abort();return
            self.reply(route,{'localBackend':True,'credentialsConfigured':True});return
        if path=='/api/data':self.reply(route,{'posts':self.saved_posts,'comments':self.saved_comments,'revision':self.saved_revision});return
        if path=='/api/data/changes':
            from urllib.parse import parse_qs
            after=int(parse_qs(parsed.query)['afterRevision'][0]);self.change_requests.append(after)
            if self.fail_changes:route.abort();return
            payload=self.change_data if after<self.change_data['revision'] else {'revision':after,'posts':[],'comments':[]}
            self.reply(route,payload);return
        job={'id':JOB,'sourceUrl':GROUP,'createdAt':1,'progress':45,'message':'Reading',
             'status':'completed' if self.complete else 'running'}
        if self.job_error:job.update(status='failed',error=self.job_error)
        if path=='/api/jobs/active':self.reply(route,{'job':job if self.active else None});return
        if path=='/api/jobs' and request.method=='POST':
            self.posts+=1
            self.start_inputs.append(json.loads(request.post_data or '{}'))
            if self.error_status:self.reply(route,{'error':'Invalid request'},self.error_status);return
            self.active=True
            if self.fail_start:self.fail_start=False;route.abort();return
            self.reply(route,job,202);return
        if path=='/api/jobs/'+JOB:
            self.polls+=1
            if self.fail_poll:self.fail_poll=False;route.abort();return
            if not self.active:self.reply(route,{'error':'Missing job'},404);return
            if 'summary=1' not in parsed.query:job.update(posts=[],comments=[])
            self.reply(route,job);return
        files={'/':'index.html','/app.js':'app.js','/styles.css':'styles.css'}
        if path in files:
            route.fulfill(path=str(ROOT/'dist'/files[path]));return
        route.fulfill(status=404,body='Not found')

    def start(self):
        self.page.locator('#source-url').fill(GROUP)
        self.page.locator('#run-button').click()
        self.page.wait_for_function('state.activeLocalJob')

    def test_transient_poll_loss_recovers_same_job(self):
        self.fail_poll=True
        self.start()
        self.page.wait_for_function('document.querySelector("#status-detail").textContent.includes("Mất kết nối")')
        self.assertTrue(self.page.evaluate('state.busy'))
        self.assertEqual(self.posts,1)
        self.complete=True
        self.page.wait_for_function('!state.busy',timeout=15000)
        self.assertEqual(self.page.locator('#run-status').get_attribute('data-state'),'success')
        self.assertEqual(self.posts,1)
        self.assertIsNone(self.page.evaluate('savedLocalJob()'))

    def test_json_import_is_removed_but_json_export_works(self):
        self.assertEqual(self.page.locator('#import-json-file').count(),0)
        self.assertEqual(self.page.locator('input[type=file]').count(),0)
        self.page.locator('#source-url').fill(GROUP)
        post={'id':'111111','url':GROUP+'/permalink/111111/','text':'Stored post','author':'Author','media':[]}
        self.saved_posts=[post]
        self.page.evaluate('(source)=>loadSavedDataset(source)',GROUP)
        with self.page.expect_download() as pending:
            self.page.locator('#export-json').click()
        download=pending.value
        exported=json.loads(Path(download.path()).read_text(encoding='utf-8-sig'))
        self.assertEqual(exported['posts'],[post])
        self.assertEqual(exported['sourceUrl'],GROUP)

    def test_live_saved_content_shows_before_comments_and_ocr_finish(self):
        self.start()
        initial_raw=self.page.locator('#raw-json').text_content()
        post={'id':'111111','url':GROUP+'/permalink/111111/','text':'Committed body',
              'author':'Author','media':[],'ocrStatus':'pending','commentsComplete':False}
        self.change_data={'revision':1,'posts':[post],'comments':[]}
        self.page.wait_for_function('state.posts.length === 1')
        self.assertTrue(self.page.evaluate('state.busy'))
        self.assertEqual(self.page.evaluate('state.posts[0].text'),'Committed body')
        self.assertEqual(self.page.evaluate('state.savedRevision'),1)
        self.assertTrue(self.page.locator('#export-json').is_enabled())
        self.assertEqual(self.page.locator('#raw-json').text_content(),initial_raw)
        comment={'commentId':'333333','text':'First committed page','postUrl':post['url']}
        self.change_data={'revision':2,'posts':[{**post,'commentsCapturedCount':1}],'comments':[comment]}
        self.page.wait_for_function('state.comments.length === 1')
        self.assertEqual(self.page.evaluate('state.posts.length'),1)
        self.assertEqual(self.page.evaluate('state.posts[0].commentsCapturedCount'),1)
        self.assertTrue(self.page.evaluate('state.busy'))
        self.page.evaluate('(source)=>syncSavedChanges(source)',GROUP)
        self.assertEqual(self.page.evaluate('state.comments.length'),1)
        self.page.locator('[data-tab="raw"]').click()
        raw=json.loads(self.page.locator('#raw-json').text_content())
        self.assertEqual(raw['posts'][0]['text'],post['text'])
        self.assertEqual(raw['comments'],[comment])

    def test_live_update_failure_retries_without_losing_cursor_or_job(self):
        self.fail_changes=True;self.start()
        self.page.wait_for_function('state.savedSyncWarning.length > 0')
        self.assertTrue(self.page.evaluate('state.busy'))
        self.assertEqual(self.page.evaluate('state.savedRevision'),0)
        self.fail_changes=False
        post={'id':'111111','url':GROUP+'/permalink/111111/','text':'Persisted during display outage','media':[]}
        self.change_data={'revision':2,'posts':[post],'comments':[]}
        self.page.wait_for_function('state.savedRevision === 2')
        self.assertEqual(self.page.evaluate('state.posts[0].text'),post['text'])
        self.assertEqual(self.posts,1)
        self.assertEqual(self.page.evaluate('state.savedSyncWarning'),'')

    def test_live_update_replaces_changed_comments_and_keeps_other_posts(self):
        first={'id':'111111','url':GROUP+'/permalink/111111/','text':'Old','media':[]}
        other={'id':'222222','url':GROUP+'/permalink/222222/','text':'Other','media':[]}
        old_comment={'commentId':'333333','postUrl':first['url'],'text':'Old comment'}
        other_comment={'commentId':'444444','postUrl':other['url'],'text':'Other comment'}
        self.saved_posts=[first,other];self.saved_comments=[old_comment,other_comment]
        self.start()
        updated_comment={**old_comment,'text':'Expanded comment'}
        self.change_data={'revision':1,'posts':[{**first,'text':'Expanded'}],'comments':[updated_comment]}
        self.page.wait_for_function('state.savedRevision === 1')
        self.assertEqual(self.page.evaluate('state.posts.length'),2)
        self.assertEqual(self.page.evaluate('state.comments.length'),2)
        self.assertEqual(self.page.evaluate('state.posts[0].text'),'Expanded')
        self.assertEqual({r['text'] for r in self.page.evaluate('state.comments')},{'Expanded comment','Other comment'})

    def test_failed_job_loads_committed_warehouse_and_displays_counts(self):
        self.start()
        self.saved_posts=[{'id':'111111','url':GROUP+'/permalink/111111/',
                           'text':'Saved before interruption','author':'Author','media':[]}]
        self.job_error='Facebook rate limit'
        self.page.wait_for_function('!state.busy',timeout=15000)
        self.assertEqual(self.page.evaluate('state.posts.length'),1)
        detail=self.page.locator('#status-detail').text_content()
        self.assertIn('Facebook rate limit',detail)
        self.assertIn('1 bài',detail)
        self.assertIsNone(self.page.evaluate('savedLocalJob()'))

    def test_reload_resumes_without_posting_new_job(self):
        self.start()
        self.page.reload()
        self.page.wait_for_function('state.busy && state.activeLocalJob')
        self.assertEqual(self.posts,1)
        self.complete=True
        self.page.wait_for_function('!state.busy',timeout=15000)
        self.assertEqual(self.posts,1)

    def test_lost_start_response_finds_server_job_without_repeating_post(self):
        self.fail_start=True
        self.start()
        self.assertEqual(self.posts,1)
        self.complete=True
        self.page.wait_for_function('!state.busy',timeout=15000)
        self.assertEqual(self.posts,1)

    def test_get_retries_but_invalid_post_does_not(self):
        self.fail_health=1
        self.assertTrue(self.page.evaluate('async()=> (await localApiJson("/api/health")).localBackend'))
        self.error_status=400
        status=self.page.evaluate('async()=> {try {await localApiJson("/api/jobs",{method:"POST",body:{}})}catch(e){return e.status}}')
        self.assertEqual(status,400)
        self.assertEqual(self.posts,1)

    def test_fresh_history_option_is_sent_and_preserved_on_reload(self):
        self.page.locator('label:has(#resume-history)').click()
        self.assertFalse(self.page.locator('#resume-history').is_checked())
        self.start()
        self.assertFalse(self.start_inputs[0]['resumeHistory'])
        self.page.reload()
        self.page.wait_for_function('state.busy && state.activeLocalJob')
        self.assertFalse(self.page.locator('#resume-history').is_checked())
        self.assertTrue(self.page.locator('#resume-history').is_disabled())
        self.complete=True
        self.page.wait_for_function('!state.busy',timeout=15000)
        self.assertEqual(self.posts,1)

    def test_server_restart_clears_missing_job_with_actionable_message(self):
        self.page.evaluate('(job)=>{void runLocalScrape({sourceUrl:"'+GROUP+'"},job).catch(e=>window.recoveryError=e.message)}',{'id':JOB})
        self.page.wait_for_function('window.recoveryError')
        self.assertIn('Bắt đầu',self.page.evaluate('window.recoveryError'))
        self.assertIsNone(self.page.evaluate('savedLocalJob()'))
        self.assertEqual(self.posts,0)


if __name__=='__main__':unittest.main()
