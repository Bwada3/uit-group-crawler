"""Request policy checks use fake clocks, temporary SQLite and local fixtures."""
from pathlib import Path
import json
import os
import sys
import tempfile
from threading import Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import local_store as store
from local_throttle import RequestGate,FacebookRateLimit,RequestCancelled,cooldown_status,retry_delay,rate_error


class Clock:
    def __init__(self):self.now=1700000000.;self.sleeps=[]
    def read(self):return self.now
    def sleep(self,seconds):self.sleeps.append(seconds);self.now+=seconds


class ThrottleRegression(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();root=Path(self.directory.name)
        self.patches=[patch.object(store,'DATA_DIR',root),patch.object(store,'DATABASE_PATH',root/'test.sqlite3'),
            patch.dict(os.environ,{'FACEBOOK_REQUEST_INTERVAL_SECONDS':'2.5','FACEBOOK_COOLDOWN_SECONDS':'900'})]
        for p in self.patches:p.start()
        self.clock=Clock()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.directory.cleanup()

    def gate(self):return RequestGate(clock=self.clock.read,monotonic=self.clock.read,sleep=self.clock.sleep)

    def test_request_spacing_is_shared(self):
        gate=self.gate();times=[]
        for _ in range(4):gate.before();times.append(self.clock.read())
        self.assertTrue(all(b-a>=2.49 for a,b in zip(times,times[1:])))

    def test_zero_interval_never_sleeps_between_requests(self):
        with patch.dict(os.environ,{'FACEBOOK_REQUEST_INTERVAL_SECONDS':'0'}):gate=self.gate()
        started=self.clock.read()
        for _ in range(100):gate.before()
        self.assertEqual(self.clock.read(),started)
        self.assertEqual(self.clock.sleeps,[])

    def test_missing_interval_defaults_to_no_spacing(self):
        with patch.dict(os.environ):
            os.environ.pop('FACEBOOK_REQUEST_INTERVAL_SECONDS',None)
            gate=self.gate()
        self.assertEqual(gate.interval,0)
        gate.before();gate.before()
        self.assertEqual(self.clock.sleeps,[])

    def test_zero_interval_still_honors_cancel_and_facebook_limit(self):
        with patch.dict(os.environ,{'FACEBOOK_REQUEST_INTERVAL_SECONDS':'0'}):gate=self.gate()
        with self.assertRaises(RequestCancelled):gate.before(lambda:True)
        gate.before()
        gate.limited('1200')
        with self.assertRaises(FacebookRateLimit):gate.before()
        self.assertEqual(cooldown_status(self.clock.read())['remaining'],1200)
        self.assertEqual(self.clock.sleeps,[])

    def test_independent_threads_share_one_schedule(self):
        with patch.dict(os.environ,{'FACEBOOK_REQUEST_INTERVAL_SECONDS':'.04'}):gate=RequestGate()
        observed=[]
        def run():gate.before();observed.append(time.monotonic())
        threads=[Thread(target=run) for _ in range(5)]
        for t in threads:t.start()
        for t in threads:t.join(timeout=3)
        self.assertEqual(len(observed),5)
        times=sorted(observed);self.assertTrue(all(b-a>=.035 for a,b in zip(times,times[1:])))

    def test_wait_is_cancellable(self):
        gate=self.gate();gate.before();start=self.clock.read()
        with self.assertRaises(RequestCancelled):gate.before(lambda:self.clock.read()>start)
        self.assertLess(self.clock.read()-start,.1)

    def test_cooldown_survives_new_gate(self):
        gate=self.gate();deadline=gate.limited()
        self.assertEqual(cooldown_status(self.clock.read())['remaining'],900)
        with self.assertRaises(FacebookRateLimit):self.gate().before()
        self.clock.now=deadline+1;self.gate().before()

    def test_repeated_incident_backs_off_without_counting_siblings_twice(self):
        gate=self.gate();first=gate.limited();gate.limited()
        self.assertEqual(cooldown_status(self.clock.read())['strikes'],1)
        self.assertEqual(gate.until,first)
        self.clock.now=first+1;second=self.gate().limited()
        self.assertEqual(second-self.clock.read(),1800)
        self.assertEqual(cooldown_status(self.clock.read())['strikes'],2)

    def test_retry_after_is_respected_even_above_local_backoff_cap(self):
        gate=self.gate();gate.limited('86400')
        self.assertEqual(gate.until-self.clock.read(),86400)

    def test_sibling_retry_after_can_extend_current_pause(self):
        gate=self.gate();gate.limited();self.clock.now+=1;gate.limited('3600')
        state=cooldown_status(self.clock.read())
        self.assertEqual(state['remaining'],3600);self.assertEqual(state['strikes'],1)

    def test_retry_after_accepts_http_date(self):
        self.assertEqual(retry_delay('Tue, 14 Nov 2023 22:15:20 GMT',1700000000),120)
        self.assertEqual(retry_delay('invalid',1700000000),0)

    def test_error_envelope_only(self):
        self.assertTrue(rate_error('{"errors":[{"message":"Rate limit exceeded"}]}'))
        self.assertTrue(rate_error('{"error":1,"errorSummary":"Going too fast"}'))
        self.assertFalse(rate_error('{"data":{"body":{"text":"Rate limit exceeded"}}}'))
        self.assertFalse(rate_error('{"data":{"errors":[{"message":"Rate limit exceeded"}]}}'))

    def test_http_429_records_retry_after(self):
        gate=self.gate();gate.inspect_response(SimpleNamespace(status=429,headers={'retry-after':'1200'}))
        self.assertEqual(gate.until-self.clock.read(),1200)

    def test_http_200_error_envelope_records_pause(self):
        gate=self.gate();gate.inspect_response(SimpleNamespace(status=200,headers={},url='https://www.facebook.com/api/graphql/',
            text=lambda:'for (;;);{"errors":[{"message":"Rate limit exceeded"}]}'))
        self.assertTrue(gate.blocked)

    def test_persistence_error_still_stops_requests(self):
        gate=self.gate()
        with patch.object(store,'_connect',side_effect=OSError('Disk full')):
            with self.assertRaises(OSError):gate.limited()
        self.assertTrue(gate.blocked)
        with self.assertRaisesRegex(RuntimeError,'trạng thái'):gate.before()

    def test_route_respects_pacing_and_blocks_during_pause(self):
        gate=self.gate();handlers={};events=[]
        context=SimpleNamespace(route=lambda pattern,fn:handlers.update(route=fn),on=lambda event,fn:handlers.update(response=fn))
        gate.attach(context)
        request=SimpleNamespace(url='https://www.facebook.com/api/graphql/',is_navigation_request=lambda:False)
        route=SimpleNamespace(request=request,fallback=lambda:events.append('sent'),abort=lambda:events.append('blocked'))
        handlers['route'](route);handlers['route'](route)
        self.assertEqual(events,['sent','sent']);self.assertGreaterEqual(sum(self.clock.sleeps),2.49)
        gate.limited();handlers['route'](route);self.assertEqual(events[-1],'blocked')

    def test_static_media_are_not_delayed(self):
        gate=self.gate();handlers={};events=[]
        context=SimpleNamespace(route=lambda pattern,fn:handlers.update(route=fn),on=lambda event,fn:None)
        gate.attach(context)
        request=SimpleNamespace(url='https://cdn.fbcdn.net/image.jpg')
        handlers['route'](SimpleNamespace(request=request,fallback=lambda:events.append('sent')))
        self.assertEqual(events,['sent']);self.assertEqual(self.clock.sleeps,[])

    def test_unrelated_ui_query_is_not_queued_but_cannot_run_during_pause(self):
        gate=self.gate();handlers={};events=[]
        context=SimpleNamespace(route=lambda pattern,fn:handlers.update(route=fn),on=lambda event,fn:None)
        gate.attach(context)
        request=SimpleNamespace(url='https://www.facebook.com/api/graphql/',post_data='fb_api_req_friendly_name=CometNotificationsQuery')
        route=SimpleNamespace(request=request,fallback=lambda:events.append('sent'),abort=lambda:events.append('blocked'))
        handlers['route'](route);handlers['route'](route)
        self.assertEqual(self.clock.sleeps,[])
        gate.limited();handlers['route'](route);self.assertEqual(events,['sent','sent','blocked'])

    def test_response_body_transport_failure_is_not_a_rate_limit(self):
        gate=self.gate()
        def fail():raise RuntimeError('Response body unavailable')
        gate.inspect_response(SimpleNamespace(status=200,headers={},url='https://www.facebook.com/api/graphql/',text=fail))
        self.assertFalse(gate.blocked)

    def test_background_429_does_not_pause_scraper(self):
        gate=self.gate()
        request=SimpleNamespace(url='https://www.facebook.com/ajax/bz/?token=private',is_navigation_request=lambda:False)
        gate.inspect_response(SimpleNamespace(status=429,headers={},url=request.url),request)
        self.assertFalse(gate.blocked)
        state=cooldown_status(self.clock.read())
        self.assertEqual(state['strikes'],0)
        self.assertEqual(state['lastSignal']['applies_to_scraper'],0)
        self.assertEqual(state['lastSignal']['path'],'/ajax/bz/')

    def test_notification_rate_error_does_not_pause_scraper(self):
        gate=self.gate()
        request=SimpleNamespace(url='https://www.facebook.com/api/graphql/',post_data='fb_api_req_friendly_name=CometNotificationsQuery')
        gate.inspect_response(SimpleNamespace(status=200,headers={},url=request.url,
            text=lambda:'{"errors":[{"message":"Rate limit exceeded"}]}'),request)
        self.assertFalse(gate.blocked)
        self.assertEqual(cooldown_status(self.clock.read())['lastSignal']['operation'],'CometNotificationsQuery')

    def test_comment_limit_logs_safe_metadata_and_pauses(self):
        gate=self.gate()
        request=SimpleNamespace(url='https://www.facebook.com/api/graphql/?token=private',
            post_data='fb_api_req_friendly_name=CommentListComponentsRootQuery&fb_dtsg=private&variables=private')
        gate.inspect_response(SimpleNamespace(status=429,headers={'retry-after':'1200'},url=request.url),request)
        state=cooldown_status(self.clock.read())
        self.assertTrue(gate.blocked);self.assertEqual(state['remaining'],1200)
        self.assertEqual(state['lastSignal']['status'],429)
        self.assertEqual(state['lastSignal']['applies_to_scraper'],1)
        self.assertNotIn('private',json.dumps(state))

    def test_signal_history_is_bounded(self):
        gate=self.gate()
        request=SimpleNamespace(url='https://www.facebook.com/ajax/bz/',is_navigation_request=lambda:False)
        response=SimpleNamespace(status=429,headers={},url=request.url)
        for _ in range(110):gate.inspect_response(response,request)
        with store._connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM facebook_limit_events').fetchone()[0],100)

    def test_classification_does_not_clear_existing_pause(self):
        gate=self.gate();deadline=gate.limited()
        request=SimpleNamespace(url='https://www.facebook.com/ajax/bz/',is_navigation_request=lambda:False)
        gate.inspect_response(SimpleNamespace(status=429,headers={},url=request.url),request)
        self.assertTrue(gate.blocked)
        self.assertEqual(cooldown_status(self.clock.read())['until'],deadline)

    def test_pause_contains_no_authentication_data(self):
        self.gate().limited()
        with store._connect() as connection:
            row=dict(connection.execute('SELECT * FROM facebook_cooldown').fetchone())
        self.assertEqual(set(row),{'id','until','strikes','last_limited'})


class BrowserThrottleRegression(unittest.TestCase):
    setUp=ThrottleRegression.setUp
    tearDown=ThrottleRegression.tearDown
    def test_browser_requests_share_spacing_and_http_error_stops_next_request(self):
        from playwright.sync_api import sync_playwright
        with patch.dict(os.environ,{'FACEBOOK_REQUEST_INTERVAL_SECONDS':'.06'}):gate=RequestGate()
        sent=[];status=[200]
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=True)
            context=browser.new_context()
            def fixture(route):
                sent.append((route.request.url,time.monotonic()))
                route.fulfill(status=status[0],content_type='application/json' if '/api/graphql' in route.request.url else 'text/html',
                              body='{"data":{}}' if status[0]==200 else '{"errors":[{"message":"Rate limit exceeded"}]}')
            context.route('**/*',fixture);gate.attach(context)
            page=context.new_page();page.goto('https://www.facebook.com/groups/fixture/')
            page.evaluate("async()=>Promise.all([1,2,3].map(n=>fetch('/api/graphql/?n='+n)))")
            times=[stamp for _,stamp in sent]
            self.assertEqual(len(times),4)
            self.assertTrue(all(b-a>=.045 for a,b in zip(times,times[1:])))
            status[0]=429
            page.evaluate("async()=>{await fetch('/api/graphql/?limited=1')}")
            page.wait_for_timeout(100)
            self.assertTrue(gate.blocked)
            before=len(sent)
            page.evaluate("async()=>{try{await fetch('/api/graphql/?blocked=1')}catch(e){}}")
            self.assertEqual(len(sent),before)
            browser.close()


if __name__=='__main__':unittest.main()
