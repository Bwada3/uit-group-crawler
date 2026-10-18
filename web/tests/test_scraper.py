"""Offline regression checks. Browser traffic is fulfilled locally; warehouse is temporary."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import local_scraper as scraper
import local_store as store
from playwright.sync_api import sync_playwright

GROUP = "https://www.facebook.com/groups/230048322891238"
POST = GROUP + "/permalink/111111/"
KEYS = {"230048322891238"}


class BrowserRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = sync_playwright()
        cls.playwright = cls.manager.__enter__()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.manager.__exit__(None, None, None)

    def setUp(self):
        self.warehouse = tempfile.TemporaryDirectory()
        self.storage_patches = [patch.object(store, "DATA_DIR", Path(self.warehouse.name)),
            patch.object(store, "DATABASE_PATH", Path(self.warehouse.name)/"test.sqlite3"), patch.object(store, "_SCHEMA_READY", False),
            patch.object(scraper, "PROFILE_DIR", Path(self.warehouse.name)/"profile"),patch.dict(os.environ,{'FACEBOOK_BROWSER_WORKERS':'0','FACEBOOK_NATIVE_FEED':'0','FACEBOOK_RESPONSE_READER':'0',
                                                                                                      'FACEBOOK_REQUEST_INTERVAL_SECONDS':'0'})]
        for item in self.storage_patches:
            item.start()
        self.context = self.browser.new_context()
        self.context.set_default_timeout(1000)
        self.page = self.context.new_page()
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body="<body></body>"))
        self.page.goto(POST)

    def tearDown(self):
        if not self.page.is_closed():
            self.context.close()
        for item in reversed(self.storage_patches):
            item.stop()
        self.warehouse.cleanup()

    def test_post_comment_and_reply_ownership(self):
        self.page.set_content('''<div id="post" role="dialog"><strong>Post author</strong>
            <div data-ad-rendering-role="story_message">Actual post body</div>
            <div role="article" aria-label="Comment by Alice"><strong>Alice</strong>
              <a href="?comment_id=1001">time</a><div dir="auto">Parent comment</div>
              <button aria-label="3 likes">3</button>
              <div role="article" aria-label="Reply by Bob"><strong>Bob</strong>
                <a href="?comment_id=1001&amp;reply_comment_id=1002">time</a><div dir="auto">Reply body</div></div>
            </div></div>''')
        root = self.page.locator("#post")
        self.assertEqual(scraper._post_text(root), "Actual post body")
        comments = scraper._visible_comments(root, "Actual post body", True)
        self.assertEqual([value["text"] for value in comments], ["Parent comment", "Reply body"])
        self.assertEqual([value["profileName"] for value in comments], ["Alice", "Bob"])
        self.assertEqual([value["threadingDepth"] for value in comments], [0, 1])
        self.assertEqual([value["commentId"] for value in comments], ["1001", "1002"])
        self.assertEqual(comments[0]["likesCount"], 3)
        self.assertEqual(len(scraper._visible_comments(root, "", False)), 1)

    def test_no_menu_does_not_escape_permalink(self):
        self.page.set_content('''<div id="post" role="dialog"><button>Most relevant</button></div>
            <script>window.escapes=0;document.onkeydown=e=>{if(e.key==='Escape'){window.escapes++;post.remove();}};</script>''')
        scraper._select_all_comments(self.page.locator("#post"), self.page)
        self.assertTrue(self.page.locator("#post").is_visible())
        self.assertEqual(self.page.evaluate("escapes"), 0)
        self.assertEqual(self.page.url, POST)

    def test_image_only_comment_is_not_dropped(self):
        self.page.set_content('''<div id="post" role="dialog"><div data-ad-rendering-role="story_message">Post</div>
            <div role="article" aria-label="Comment by Alice"><strong>Alice</strong>
              <a href="?comment_id=1003">time</a><img width="180" height="120" src="https://test.fbcdn.net/comment.png"></div></div>''')
        comments = scraper._visible_comments(self.page.locator("#post"), "Post", True)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["commentId"], "1003")
        self.assertEqual(comments[0]["text"], "")
        self.assertEqual(comments[0]["media"][0]["url"], "https://test.fbcdn.net/comment.png")

    def test_stable_control_survives_profile_link_insertion(self):
        self.page.set_content('''<div id="post"><button onclick="window.expanded=true">View more comments</button></div>''')
        root = self.page.locator("#post")
        action = scraper._post_actions(root, "button,a")[0]
        self.page.evaluate("""() => {const a=document.createElement('a');a.href='/profile.php?id=1';a.textContent='Person';post.prepend(a);}""")
        self.assertTrue(scraper._click_post_action(root, action, self.page, wait_ms=0))
        self.assertTrue(self.page.evaluate("expanded"))
        self.assertEqual(self.page.url, POST)

    def test_reaction_spinner_replaced_and_only_child_closed(self):
        self.page.set_content('''<div id="post" role="dialog"><div data-ad-rendering-role="story_message">Post</div>
            <button onclick="openReaction()">View who reacted</button>
            <button aria-label="Close" onclick="window.badClose=true">x</button></div><script>
            function openReaction(){const s=document.createElement('div');s.setAttribute('role','dialog');s.textContent='Loading';document.body.append(s);
              setTimeout(()=>{const d=document.createElement('div');d.id='reactions';d.setAttribute('role','dialog');
                d.innerHTML='<span role="tab">All 5</span><span role="tab">Like 5</span><button aria-label="&#272;&#243;ng" onclick="this.parentElement.remove()">x</button>';s.replaceWith(d);},350);}
            </script>''')
        values = scraper._open_reaction_dialog(self.page.locator("#post"), self.page)
        self.assertIn("Like 5", values)
        self.assertEqual(self.page.locator("#reactions").count(), 0)
        self.assertTrue(self.page.locator("#post").is_visible())
        self.assertFalse(self.page.evaluate("!!window.badClose"))

    def test_exact_inline_reactions_skip_dialog(self):
        self.page.set_content('''<div id="post" role="dialog"><div data-ad-rendering-role="story_message">Post</div>
            <button aria-label="View who reacted">5</button><span aria-label="Like 4"></span><span aria-label="Love 1"></span>
            <div role="article" aria-label="Comment by Person"><button aria-label="Like 90">90</button></div></div>''')
        with patch.object(scraper, "_open_reaction_dialog") as opened:
            values, complete = scraper._reaction_breakdown(self.page.locator("#post"), self.page)
        opened.assert_not_called()
        self.assertTrue(complete)
        self.assertEqual(values["reactionLikeCount"], 4)
        self.assertEqual(values["reactionLoveCount"], 1)
        self.assertEqual(values["reactionSadCount"], 0)

    def test_missing_inline_types_never_open_reactions(self):
        with patch.object(scraper, "_owned_values", return_value=["5 reactions", "Like 4"]), \
             patch.object(scraper, "_open_reaction_dialog", return_value=["All 5", "Like 4", "Love 1"]) as opened:
            values, complete = scraper._reaction_breakdown(None, self.page)
        opened.assert_not_called()
        self.assertFalse(complete)
        self.assertEqual(values["reactionLikeCount"], 4)
        self.assertIsNone(values["reactionLoveCount"])

    def test_reaction_participant_names_are_not_counts(self):
        self.page.set_content('''<div id="reactions" role="dialog"><span role="tab">All 5</span>
            <span role="tab" aria-label="Like 4">4</span><span role="tab" aria-label="Love 1">1</span>
            <button aria-label="Like 900">Participant</button></div>''')
        values = scraper._reaction_dialog_values(self.page.locator("#reactions"))
        self.assertNotIn("Like 900", values)
        counts, complete = scraper._complete_reaction_counts(values)
        self.assertTrue(complete)
        self.assertEqual(counts["reactionLikeCount"], 4)

    def test_rounded_inline_totals_never_infer_zero_types(self):
        with patch.object(scraper, "_owned_values", return_value=["1K reactions", "Like 1K"]), \
             patch.object(scraper, "_open_reaction_dialog", return_value=[]) as opened:
            values, complete = scraper._reaction_breakdown(None, self.page)
        opened.assert_not_called()
        self.assertFalse(complete)
        self.assertIsNone(values["reactionLoveCount"])

    def test_no_reaction_summary_is_unknown(self):
        with patch.object(scraper, "_owned_values", return_value=["Like", "Comment"]), \
             patch.object(scraper, "_open_reaction_dialog", return_value=[]):
            values, complete = scraper._reaction_breakdown(None, self.page)
        self.assertFalse(complete)
        self.assertTrue(all(value is None for value in values.values()))

    def test_reaction_dialog_mismatched_total_is_incomplete(self):
        with patch.object(scraper, "_owned_values", return_value=["All 6", "Like 4", "Love 1"]), \
             patch.object(scraper, "_open_reaction_dialog", side_effect=AssertionError("Reaction overlay must never open")):
            values, complete = scraper._reaction_breakdown(None, self.page)
        self.assertFalse(complete)
        self.assertIsNone(values["reactionSadCount"])

    def test_comment_popup_resolves_without_switching_tabs(self):
        self.page.goto(GROUP)
        self.page.set_content('<div id="post" role="article"><div data-ad-rendering-role="story_message">Post</div>'
            '<button aria-label="5 comments" onclick="window.open(\''+POST+'\',\'_blank\')">5</button></div>'
            '<script>window.originalOpen=window.open;</script>')
        pages_before = list(self.context.pages)
        resolved = scraper._click_comment_for_permalink(self.page, self.page.locator("#post"), KEYS, lambda: False)
        self.assertEqual(resolved, POST)
        self.assertEqual(self.page.url, GROUP)
        self.assertEqual(self.context.pages, pages_before)
        self.assertTrue(self.page.evaluate("window.open === window.originalOpen"))

    def test_comment_spa_permalink_keeps_source_url(self):
        self.page.goto(GROUP)
        navigations = []
        self.page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == self.page.main_frame else None)
        self.page.set_content('<div id="post" role="article"><div data-ad-rendering-role="story_message">Post</div>'
            '<button aria-label="5 comments" onclick="openPost()">5</button></div><script>'
            'window.originalPush=history.pushState;window.originalReplace=history.replaceState;'
            'function openPost(){history.pushState({post:true},\'\',\''+POST+'\');'
            'const dialog=document.createElement(\'div\');dialog.setAttribute(\'role\',\'dialog\');'
            'dialog.innerHTML=\'<button aria-label="Close" onclick="this.parentElement.remove();history.back()">x</button>\';'
            'document.body.append(dialog);}</script>')
        resolved = scraper._click_comment_for_permalink(self.page, self.page.locator("#post"), KEYS, lambda: False)
        self.assertEqual(resolved, POST)
        self.assertEqual(self.page.url, GROUP)
        self.assertFalse(any("permalink" in url for url in navigations))
        self.assertEqual(self.page.locator('[role="dialog"]').count(), 0)
        self.assertTrue(self.page.evaluate("history.pushState === window.originalPush && history.replaceState === window.originalReplace"))

    def test_lazy_comment_batches_are_collected(self):
        self.page.set_content('''<div id="post" role="dialog"><div data-ad-rendering-role="story_message">Actual post</div>
            <button aria-label="30 comments">30</button>
            <div id="thread" style="overflow-y:auto;height:200px"><div id="list"></div></div></div><script>
            window.loaded=0;function batch(limit=10){for(let i=0;i<limit&&loaded<30;i++){loaded++;const d=document.createElement('div');
              d.setAttribute('role','article');d.setAttribute('aria-label','Comment by Person');d.style.height='70px';
              d.innerHTML='<strong>Person</strong><a href="?comment_id='+loaded+'">time</a><div dir="auto">Comment '+loaded+'</div>';list.append(d);}}
            batch(1);setTimeout(()=>batch(9),500);thread.onscroll=()=>{if(thread.scrollTop+thread.clientHeight>=thread.scrollHeight-5&&loaded<30)batch();};
            </script>''')
        started = time.monotonic()
        values = scraper._expand_post_content(self.page.locator("#post"), True, self.page, cancelled=lambda: time.monotonic()-started > 8)
        self.assertEqual(len(values), 30)
        self.assertEqual(len({value["commentId"] for value in values}), 30)
        self.assertEqual(self.page.evaluate("window.scrollY"), 0)

    def test_permalink_dialog_sibling_close_waits_for_transition(self):
        self.page.goto(GROUP)
        self.page.set_content('<div id="post" role="article"><div data-ad-rendering-role="story_message">Post</div>'
            '<button aria-label="5 comments" onclick="openPost()">5</button></div><script>'
            'function openPost(){history.pushState({},\'\',\''+POST+'\');'
            'const overlay=document.createElement(\'section\');'
            'overlay.innerHTML=\'<button aria-label="Close dialog" onclick="const root=this.parentElement;setTimeout(()=>root.remove(),1100)">x</button><div role="dialog">Post</div>\';'
            'document.body.append(overlay);}</script>')
        resolved=scraper._click_comment_for_permalink(self.page,self.page.locator('#post'),KEYS,lambda:False)
        self.assertEqual(resolved,POST)
        self.assertEqual(self.page.url,GROUP)
        self.assertEqual(self.page.locator('[role="dialog"]').count(),0)

    def test_ready_comment_action_starts_read_without_fixed_delay(self):
        self.context.unroute('**/*')
        self.context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',
            body='<div role="dialog"><div data-ad-rendering-role="story_message">Ready body</div><button>Comment</button></div>'))
        started=time.monotonic()
        self.assertIsNotNone(scraper._open_post_permalink(self.page,POST))
        self.assertLess(time.monotonic()-started,1.5)

    def test_permalink_wait_wakes_on_post_rendering(self):
        self.page.set_content('<div></div><script>setTimeout(()=>{document.body.innerHTML=\'<div role="dialog"><div data-ad-rendering-role="story_message">Ready post</div><button>Comment</button></div>\'},120)</script>')
        started=time.monotonic()
        scraper._wait_permalink_render(self.page,1000)
        self.assertLess(time.monotonic()-started,.8)
        self.assertEqual(self.page.locator('[data-ad-rendering-role]').inner_text(),'Ready post')

    def test_render_mutations_do_not_shorten_readiness_deadline(self):
        self.context.unroute('**/*')
        self.context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='''<div id="root"></div><script>
            let i=0;const timer=setInterval(()=>{root.innerHTML='<button>'+i+++'</button>'},20);
            setTimeout(()=>{clearInterval(timer);root.innerHTML='<div role="dialog"><div data-ad-rendering-role="story_message">Delayed post</div><button>Comment</button></div>'},900);
            </script>'''))
        article=scraper._open_post_permalink(self.page,POST,_readiness_seconds=2,_render_events=True)
        self.assertEqual(scraper._post_text(article),'Delayed post')

    def test_no_comment_controls_keep_original_fallback_wait(self):
        self.context.unroute('**/*')
        self.context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',
            body='<div role="dialog"><div data-ad-rendering-role="story_message">Post without controls</div></div>'))
        started=time.monotonic()
        article=scraper._open_post_permalink(self.page,POST)
        self.assertGreaterEqual(time.monotonic()-started,2.4)
        self.assertEqual(scraper._post_text(article),'Post without controls')

    def test_loading_status_does_not_record_zero_comments_after_fallback(self):
        self.context.unroute('**/*')
        self.context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='''
          <div role="dialog"><div data-ad-rendering-role="story_message">Post</div>
          <div id="loading" role="status" data-visualcompletion="loading-state">Loading...</div></div>
          <script>setTimeout(()=>loading.outerHTML='<button aria-label="7 comments">7</button><button>Comment</button>',2800)</script>'''))
        article=scraper._open_post_permalink(self.page,POST,_readiness_seconds=4)
        self.assertEqual(scraper._visible_count(article,scraper.COMMENT_WORDS),7)

    def test_loading_status_timeout_is_retryable(self):
        self.context.unroute('**/*')
        self.context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='''
          <div role="dialog"><div data-ad-rendering-role="story_message">Post</div>
          <div role="status" data-visualcompletion="loading-state">Loading...</div></div>'''))
        with self.assertRaises(RuntimeError):scraper._open_post_permalink(self.page,POST,_readiness_seconds=.35)

    def test_loading_modal_never_reads_backing_feed_card(self):
        self.context.unroute('**/*')
        self.context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='''
          <div role="main"><div role="article"><div data-ad-rendering-role="story_message">Wrong backing post</div><button>Comment</button></div></div>
          <div role="dialog" id="modal">Loading...</div>
          <script>setTimeout(()=>modal.innerHTML='<div data-ad-rendering-role="story_message">Target post</div><button>Comment</button>',250)</script>'''))
        article=scraper._open_post_permalink(self.page,POST,_readiness_seconds=2)
        self.assertEqual(scraper._post_text(article),'Target post')

    def test_mismatched_story_link_is_rejected(self):
        self.page.set_content('<div role="dialog"><a href="'+GROUP+'/permalink/222222/">Time</a>'
            '<div data-ad-rendering-role="story_message">Wrong post</div><button>Comment</button></div>')
        self.assertIsNone(scraper._permalink_container(self.page))

    def test_comment_count_disappearing_does_not_mark_partial_complete(self):
        self.page.set_content('<div id="post" role="dialog"><strong>Author</strong>'
            '<div data-ad-rendering-role="story_message">Post</div><button aria-label="6 comments">6</button></div>')
        article=self.page.locator('#post')
        def expand(*args,**kwargs):
            article._grouppulse_expected_comment_count=6
            article.locator('button').evaluate('node=>node.remove()')
            return []
        with patch.object(scraper,'_select_all_comments'),patch.object(scraper,'_expand_post_content',expand):
            post,_=scraper._extract_post(article,True,self.page,KEYS)
        self.assertEqual(post['commentsCount'],6)
        self.assertFalse(post['commentsComplete'])

    def test_comment_stall_keeps_partial_records_despite_pending_network(self):
        self.page.set_content('<div id="post" role="dialog"><div data-ad-rendering-role="story_message">Post</div>'
            '<button aria-label="5 comments">5</button><div role="article" aria-label="Comment by Reader">'
            '<strong>Reader</strong><a href="?comment_id=333333">time</a><div dir="auto">Saved comment</div></div></div>')
        self.page._grouppulse_comment_network={'pending':{object()},'last':time.monotonic()}
        article=self.page.locator('#post');start=time.monotonic()
        rows=scraper._expand_post_content(article,True,self.page,_stall_seconds=.3)
        self.assertLess(time.monotonic()-start,1.5);self.assertEqual(len(rows),1)
        self.assertTrue(article._grouppulse_comments_incomplete)

    def test_stalled_unknown_comment_total_is_never_marked_complete(self):
        self.page.set_content('<div id="post" role="dialog"><div data-ad-rendering-role="story_message">Post</div></div>')
        article=self.page.locator('#post')
        def stalled(*args,**kwargs):article._grouppulse_comments_incomplete=True;return []
        with patch.object(scraper,'_select_all_comments'),patch.object(scraper,'_expand_post_content',stalled):
            post,_=scraper._extract_post(article,True,self.page,KEYS)
        self.assertEqual(post['commentsCount'],0);self.assertFalse(post['commentsComplete'])

    def test_dom_comment_batch_is_emitted_before_next_expansion(self):
        self.page.set_content('<div id="post" role="dialog"><div data-ad-rendering-role="story_message">Post</div>'
            '<button aria-label="5 comments">5</button><div role="article" aria-label="Comment by Reader">'
            '<strong>Reader</strong><a href="?comment_id=333333">time</a><div dir="auto">Saved comment</div></div></div>')
        captured=[]
        def actions(*args):
            self.assertEqual(captured[0][0]['commentId'],'333333')
            raise RuntimeError('Next expansion failed')
        with patch.object(scraper,'_post_actions',actions):
            with self.assertRaisesRegex(RuntimeError,'Next expansion'):
                scraper._expand_post_content(self.page.locator('#post'),True,self.page,
                    on_batch=lambda rows,changed:captured.append(changed))

    def test_profile_document_navigation_is_blocked(self):
        self.page.goto(GROUP)
        self.page.set_content('<div id="marker">Source group</div>')
        scraper._install_group_navigation_guard(self.context, self.page, KEYS, lambda *args: None)
        self.page.evaluate("location.href='https://www.facebook.com/profile.php?id=1'")
        self.page.wait_for_timeout(150)
        self.assertEqual(self.page.url, GROUP)
        self.assertTrue(self.page.locator("#marker").is_visible())

    def test_full_loop_skips_saved_and_never_hides(self):
        requests, events, saved = [], [], []
        feed = ''.join('<div role="article"><a href="'+GROUP+'/posts/'+str(i)+'/">time</a><div data-ad-rendering-role="story_message">Post '+str(i)+'</div><button>Like</button><button>Comment</button><button>Share</button></div>' for i in (111111,222222))
        feed += '<script>window.scrollBy=()=>window.recordEvent("scroll");</script>'
        detail = '<div role="dialog"><strong>Author</strong><div data-ad-rendering-role="story_message">New body</div><button>All comments</button></div>'
        self.context.unroute("**/*")

        def route(request):
            requests.append(request.request.url)
            request.fulfill(status=200, content_type="text/html", body=detail if any(part in request.request.url for part in ("/posts/", "/permalink/")) else feed)

        self.context.route("**/*", route)
        self.page.expose_function("recordEvent", lambda value: events.append(value))
        self.page.goto(GROUP)
        context = self.context

        class Existing:
            def __enter__(self):
                return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda **kwargs: context))

            def __exit__(self, *args):
                return False

        started = time.monotonic()
        with patch.object(scraper, "sync_playwright", Existing), patch.object(scraper, "_open_source_page", lambda *args: None), \
             patch.object(scraper, "_open_reaction_dialog", side_effect=AssertionError("Reaction overlay must never open")), \
             patch("playwright.sync_api.Page.bring_to_front") as focus:
            posts, _ = scraper.scrape_facebook_posts(
                email="", password="", source_url=GROUP, include_replies=True,
                progress=lambda *args: None, cancelled=lambda: bool(events) or time.monotonic()-started > 10,
                known_post_urls={POST.rstrip('/')}, known_group_keys=KEYS, on_post=lambda post, comments: saved.append(post),
            )
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(saved), 1)
        self.assertEqual(focus.call_count, 1)
        self.assertEqual(events, ["scroll"])
        self.assertFalse(any("/111111/" in url or "/api/graphql" in url for url in requests))


class WarehouseRegression(unittest.TestCase):
    def test_partial_capture_preserves_prior_text_reactions_and_comments(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(store,'DATA_DIR',Path(directory)), \
             patch.object(store,'DATABASE_PATH',Path(directory)/'test.sqlite3'), \
             patch.object(store,'_SCHEMA_READY',False):
            original={'url':POST,'text':'Full body before interruption','commentsComplete':False,
                      'likesCount':3,'reactionLikeCount':3,'reactionLoveCount':0,
                      'reactionBreakdownComplete':True,'reactionBreakdownAvailable':True}
            store.save_post(GROUP,original,[{'commentId':'333333','text':'Previous comment'}])
            store.save_post(GROUP,{'url':POST,'text':'Full body','commentsComplete':False,
                'likesCount':None,'reactionLikeCount':None,'reactionBreakdownComplete':False},[],partial=True)
            saved=store.load_saved_data(GROUP)
            self.assertEqual(saved['posts'][0]['text'],original['text'])
            self.assertEqual(saved['posts'][0]['reactionLikeCount'],3)
            self.assertEqual(len(saved['comments']),1)

    def test_connection_is_committed_and_closed_after_context(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(store,'DATA_DIR',Path(directory)),patch.object(store,'DATABASE_PATH',Path(directory)/'test.sqlite3'):
                with store._connect() as connection:
                    connection.execute('CREATE TABLE probe(value INTEGER)')
                    connection.execute('INSERT INTO probe VALUES (1)')
                with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
                with store._connect() as restored:self.assertEqual(restored.execute('SELECT value FROM probe').fetchone()[0],1)

    def test_exception_rolls_back_and_closes_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(store,'DATA_DIR',Path(directory)),patch.object(store,'DATABASE_PATH',Path(directory)/'test.sqlite3'):
                with store._connect() as connection:connection.execute('CREATE TABLE probe(value INTEGER)')
                with self.assertRaises(RuntimeError):
                    with store._connect() as connection:
                        connection.execute('INSERT INTO probe VALUES (1)')
                        raise RuntimeError('fixture')
                with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
                with store._connect() as restored:self.assertEqual(restored.execute('SELECT count(*) FROM probe').fetchone()[0],0)

    def test_distinct_image_comments_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(store, "DATA_DIR", Path(directory)), patch.object(store, "DATABASE_PATH", Path(directory)/"warehouse.sqlite3"), patch.object(store, "_SCHEMA_READY", False):
                post = {"id": POST, "url": POST, "text": "Post"}
                comments = [{"profileName": "Same author", "date": "", "text": "", "threadingDepth": 0,
                             "media": [{"url": f"https://test.fbcdn.net/{name}.png"}]} for name in ("a", "b")]
                store.save_post(GROUP, post, comments)
                store.save_post(GROUP, post, comments)
                self.assertEqual(len(store.load_saved_data(GROUP)["comments"]), 2)

    def test_distinct_ids_keep_identical_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(store, "DATA_DIR", Path(directory)), patch.object(store, "DATABASE_PATH", Path(directory)/"warehouse.sqlite3"), patch.object(store, "_SCHEMA_READY", False):
                post = {"id": POST, "url": POST, "text": "Post"}
                comments = [{"commentId": str(identifier), "profileName": "Same author",
                             "date": "", "text": "Same text", "threadingDepth": 0}
                            for identifier in (1001, 1002)]
                store.save_post(GROUP, post, comments)
                store.save_post(GROUP, post, comments)
                restored = store.load_saved_data(GROUP)
                self.assertEqual(len(restored["comments"]), 2)
                self.assertEqual({row["commentId"] for row in restored["comments"]}, {"1001", "1002"})

    def test_saved_records_survive_reload_and_alias_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(store, "DATA_DIR", Path(directory)), patch.object(store, "DATABASE_PATH", Path(directory)/"warehouse.sqlite3"), patch.object(store, "_SCHEMA_READY", False):
                store.register_source_aliases(GROUP, [GROUP, "https://www.facebook.com/groups/testgroup"])
                post = {"id": POST, "url": POST, "text": "Saved post", "comments": []}
                comments = [{"profileName": "Person", "text": "Comment", "postUrl": POST, "threadingDepth": 0}]
                store.save_post(GROUP, post, comments)
                store.save_post(GROUP, post, comments)
                restored = store.load_saved_data("https://www.facebook.com/groups/testgroup")
                self.assertEqual(len(restored["posts"]), 1)
                self.assertEqual(len(restored["comments"]), 1)
                self.assertIn(POST.rstrip('/'), store.known_post_keys(GROUP))


if __name__ == "__main__":
    unittest.main()
