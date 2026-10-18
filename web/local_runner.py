"""Durable discovery, fixed readers and a separate CPU OCR pipeline."""
from __future__ import annotations

import json
import os
import time
from local_tasks import TaskQueue, post_id
from local_media import MediaPipeline


def run_group(adapter, **options):
    from local_profile import ProfileLease
    with ProfileLease(adapter.PROFILE_DIR):
        return _run_group(adapter, **options)


def _run_group(adapter, *, email, password, source_url, include_replies, progress, cancelled,
              known_post_urls=None, known_group_keys=None, on_source=None, on_post=None, max_posts=None, replay_urls=None, collect_results=True,resume_history=True):
    from local_discovery import discover, advance_feed
    from local_workers import DetailWorkers
    from local_store import normalize_post_url
    from playwright.sync_api import TimeoutError as BrowserTimeout
    from local_throttle import RequestGate
    gate=RequestGate()
    gate.check()
    known_urls = known_post_urls if known_post_urls is not None else set()
    completed_ids = {post_id(url) for url in known_urls}
    discovered_ids = set()
    posts, pending_reads, pending_ocr = [], [], []
    pipeline = MediaPipeline(adapter._ocr_image)
    queue = workers = None
    detail_page = None
    feed = None
    errors = []
    order = {}
    saved_count = saved_comments = 0
    try:
        worker_count = min(4,max(0,int(os.environ.get('FACEBOOK_BROWSER_WORKERS','2'))))
    except ValueError:
        worker_count = 2

    def known(url):
        return normalize_post_url(url) in known_urls or post_id(url) in completed_ids or post_id(url) in discovered_ids

    def retry(task, error):
        from local_response_reader import ResponseRateLimit
        if isinstance(error,ResponseRateLimit):
            gate.limited()
            queue.update(task,'queued',error='Facebook rate limit')
            gate.check()
        attempts = task['attempts']+1
        queue.update(task,'queued' if attempts<3 else 'failed',error=f'{type(error).__name__}: {error}')
        if attempts>=3:
            errors.append(task['post_id'])
        progress('running',min(92,15+saved_count),f'Bài {task["post_id"]} tải lỗi; đã giữ tác vụ để thử lại.')

    def validate_capture(task, post, comments):
        if adapter._group_key_from_url(post['url']) not in keys or post_id(post['url'])!=task['post_id']:
            raise RuntimeError('Permalink trả về bài khác; chưa lưu dữ liệu.')
        if not post['text'] and not post['media'] and not post['comments']:
            raise RuntimeError('Bài chưa có nội dung để lưu.')
        for row in comments:
            row.update(postId=post['id'],postAuthor=post['author'],postUrl=post['url'],parentPostUrl=post['url'],commentUrl='')

    def checkpoint_capture(task, post, comments):
        import local_store
        # The callback runs on the browser's own thread; only plain data crosses
        # this boundary. SQLite commits before the next Facebook request.
        captured=json.loads(json.dumps(post))
        rows=json.loads(json.dumps(comments))
        validate_capture(task,captured,captured.get('comments',[]))
        validate_capture(task,captured,rows)
        captured['commentsComplete']=False
        captured['ocrStatus']='pending'
        local_store.save_post(source_url,captured,rows,partial=True)
        queue.update(task,'reading',{'post':captured})
        if not task.get('_content_saved'):
            task['_content_saved']=True
            progress('running',15,f'Đã lưu nội dung bài {task["post_id"]}; đang bổ sung bình luận và OCR…')

    def accept_capture(task, post):
        validate_capture(task,post,post['comments'])
        import local_store
        captured=json.loads(json.dumps(post))
        captured['ocrStatus']='pending'
        local_store.save_post(source_url,captured,post['comments'])
        queue.update(task,'ocr_running',{'post':post})
        pending_ocr.append((task,pipeline.submit(post)))

    def collect(block=False):
        nonlocal saved_count,saved_comments
        failures=[]

        def failed_read(task,error):
            try:
                retry(task,error)
            except Exception as fatal:
                # A fatal read must not interrupt saving other completed reads
                # or OCR results. Raise only after draining this batch.
                failures.append(fatal)

        for item in list(pending_reads):
            task,future=item
            if not future.done() and not block:
                continue
            pending_reads.remove(item)
            try:
                post=future.result()
            except Exception as error:
                failed_read(task,error)
                continue
            try:
                accept_capture(task,post)
            except RuntimeError as error:
                failed_read(task,error)
            except Exception as error:
                failures.append(error)
        for item in list(pending_ocr):
            task,future=item
            if not future.done() and not block:
                continue
            try:
                post=future.result()
                if on_post:
                    on_post(post,post['comments'])
                else:
                    import local_store
                    local_store.save_post(source_url,post,post['comments'])
                complete=post.get('commentsComplete') is not False and post.get('ocrStatus') in ('complete','no_image')
                queue.update(task,'done' if complete else 'partial',{'post':post})
                completed_ids.add(task['post_id'])
                known_urls.add(normalize_post_url(post['url']))
                if collect_results:
                    order[post['id']]=task['queue_order']
                    posts.append(post)
                saved_count+=1
                saved_comments+=len(post['comments'])
                pending_ocr.remove(item)
                progress('running',min(94,15+saved_count),f'Đã lưu/cập nhật {saved_count} bài và {saved_comments} bình luận…')
            except Exception as error:
                # Keep the durable OCR checkpoint and continue saving the rest.
                failures.append(error)
        if failures:
            raise failures[0]

    try:
        with adapter.sync_playwright() as playwright:
            context=playwright.chromium.launch_persistent_context(
                user_data_dir=str(adapter.PROFILE_DIR),channel='chrome',headless=False,
                slow_mo=0,viewport={'width':1440,'height':1000},args=['--start-maximized'],service_workers='block')
            context.set_default_timeout(3000)
            context.set_default_navigation_timeout(45000)
            gate.attach(context,cancelled,progress)
            page=context.pages[0] if context.pages else context.new_page()
            page.bring_to_front()
            try:
                progress('running',5,'Đang mở phiên Facebook và nguồn dữ liệu…')
                adapter._open_source_page(page,email,password,source_url)
                keys=adapter._source_group_keys(page,source_url) | (known_group_keys or set())
                aliases=[source_url,page.url]+[f'https://www.facebook.com/groups/{key}/' for key in keys]
                if on_source:
                    on_source(page.url,aliases)
                    completed_ids.update(post_id(url) for url in known_urls)
                queue=TaskQueue(source_url)
                discovered_ids.update(queue.identities())
                direct=bool(adapter._canonical_group_permalink(page.url,keys))
                recovering=bool(queue.queued_count())
                feed_attempted=False
                if not direct and replay_urls is None and os.environ.get('FACEBOOK_NATIVE_FEED','1')!='0':
                    from local_feed import NativeFeed
                    feed=NativeFeed(adapter,page,queue,keys,known,progress,resume=resume_history)
                    feed.request_gate=gate
                adapter._install_group_navigation_guard(context,None if direct else page,keys,progress)
                if direct:
                    if not (normalize_post_url(page.url) in known_urls or post_id(page.url) in completed_ids):
                        queue.add([adapter._canonical_group_permalink(page.url,keys)])
                elif worker_count:
                    workers=DetailWorkers(adapter,context.storage_state(),keys,pipeline,include_replies,progress,cancelled,worker_count)
                    workers.on_capture=checkpoint_capture
                    workers.request_gate=gate
                    if feed is not None and getattr(workers,'response_queries',None) is not None:
                        feed.on_story=workers.response_queries.put_story
                if replay_urls is not None:
                    validated=[adapter._canonical_group_permalink(url,keys) for url in replay_urls]
                    if not all(validated):
                        raise RuntimeError('Danh sách đo thử chứa permalink ngoài group nguồn.')
                    queue.add(validated)
                    discovered_ids.update(post_id(url) for url in validated)
                capacity=worker_count if workers else 1
                last_new=last_scan=time.monotonic()
                discovery_changes=0
                progress('running',15,'Đang bổ sung dữ liệu cho các bài chưa hoàn tất trong hàng đợi…' if recovering else 'Đang đưa permalink vào hàng đợi và đọc bài song song…')
                while not cancelled():
                    collect()
                    gate.check()
                    if max_posts and saved_count>=max_posts:
                        break
                    if cancelled():
                        break
                    while len(pending_reads)<capacity and len(pending_ocr)<3 and not cancelled():
                        if max_posts and saved_count+len(pending_reads)+len(pending_ocr)>=max_posts:
                            break
                        task=queue.next()
                        if task is None:
                            break
                        if normalize_post_url(task['post_url']) in known_urls or task['post_id'] in completed_ids:
                            queue.update(task,'done')
                            continue
                        checkpoint=json.loads(task['payload_json'])['post'] if task['payload_json'] else None
                        if checkpoint and checkpoint.get('commentsComplete') is not False:
                            post=checkpoint
                            pipeline.prepare(post['media'],page)
                            accept_capture(task,post)
                            continue
                        if workers:
                            pending_reads.append((task,workers.submit(task)))
                        else:
                            try:
                                post_page=page
                                if not direct:
                                    if detail_page is None or detail_page.is_closed():
                                        detail_page=context.new_page()
                                    post_page=detail_page
                                    article=adapter._open_post_permalink(detail_page,task['post_url'])
                                else:
                                    article=adapter._permalink_container(page)
                                if article is None:
                                    raise RuntimeError('Bài chưa tải xong.')
                                post,_=adapter._extract_post(article,include_replies,post_page,keys,
                                    media_pipeline=pipeline,cancelled=cancelled,
                                    on_capture=lambda post,comments,task=task:checkpoint_capture(task,post,comments),
                                    on_stage=lambda text:progress('running',min(92,15+saved_count),text))
                                accept_capture(task,post)
                            except (RuntimeError,BrowserTimeout) as error:
                                retry(task,error)
                            collect()
                        if workers:
                            collect()
                    if direct or replay_urls is not None:
                        if not pending_reads and not pending_ocr and not queue.queued_count():
                            break
                        page.wait_for_timeout(50)
                        continue
                    # Finish durable URLs left by an earlier run before loading
                    # further feed pages. New work still pipelines normally.
                    if recovering:
                        if pending_reads or pending_ocr or queue.queued_count():
                            page.wait_for_timeout(50)
                            continue
                        recovering=False
                    if queue.queued_count()<30 and time.monotonic()-last_scan>=.9:
                        adapter._assert_group_timeline(page,keys)
                        urls=[];changed=False;added=0
                        native_added=feed.added if feed else 0
                        if feed and feed.ready:
                            feed.fetch_next()
                        elif feed and not feed_attempted:
                            feed_attempted=True
                            feed.bootstrap(cancelled)
                        else:
                            urls,changed=discover(adapter,page,keys,known,cancelled)
                            added=queue.add(urls)
                            discovered_ids.update(post_id(url) for url in urls)
                        if feed:
                            added+=feed.added-native_added
                            discovered_ids.update(feed.ids)
                        if workers is not None:
                            workers.keys.update(keys)
                        if added:
                            last_new=time.monotonic()
                        if changed:
                            discovery_changes+=1
                            if discovery_changes>3:
                                raise RuntimeError('Card chưa có permalink sau khi quét lại; các URL đã phát hiện vẫn được giữ trong hàng đợi để tiếp tục.')
                        else:
                            discovery_changes=0
                            if not added and not cancelled() and not (feed and feed.ready):
                                advance_feed(page)
                        last_scan=time.monotonic()
                    if not pending_reads and not pending_ocr and not queue.queued_count() and time.monotonic()-last_new>=adapter.NO_NEW_POST_TIMEOUT_SECONDS:
                        break
                    page.wait_for_timeout(50)
                collect(block=True)
                if errors and not cancelled():
                    raise RuntimeError(f'{len(errors)} bài tải lỗi được giữ trong hàng đợi để tiếp tục ở lượt sau.')
                posts.sort(key=lambda post:(adapter._date_sort_value(str(post.get('time') or '')) is None,
                    -(adapter._date_sort_value(str(post.get('time') or '')) or 0),order.get(post['id'],0)))
                return posts,[row for post in posts for row in post['comments']]
            finally:
                try:
                    if workers is not None:
                        workers.close()
                    if queue is not None:
                        collect(block=True)
                finally:
                    if feed is not None:
                        feed.close()
                    context.close()
    finally:
        pipeline.close()
