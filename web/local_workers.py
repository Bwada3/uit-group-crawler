"""Fixed browser workers. Each thread owns its Playwright, browser and page."""
from concurrent.futures import Future
from queue import Queue
from threading import Thread,Event
import os


class DetailWorkers:
    def __init__(self, adapter, storage_state, group_keys, pipeline, include_replies, progress, cancelled, count=2):
        self.jobs = Queue(maxsize=count)
        self.threads = []
        self.adapter = adapter
        self.state = storage_state
        self.keys = set(group_keys)
        self.pipeline = pipeline
        self.include_replies = include_replies
        self.progress = progress
        self.stop=Event()
        self.request_gate=None
        self.cancelled = lambda:cancelled() or self.stop.is_set() or bool(self.request_gate and self.request_gate.blocked)
        self.response_queries=None
        if os.environ.get('FACEBOOK_RESPONSE_READER','0')=='1':
            from local_response_reader import QueryRegistry
            self.response_queries=QueryRegistry()
        for index in range(count):
            thread = Thread(target=self._work, name=f'post-reader-{index}', daemon=True)
            thread.start()
            self.threads.append(thread)

    def submit(self, task):
        result = Future()
        self.jobs.put((task, result))
        return result

    def _work(self):
        try:
            self._serve()
        except Exception as error:
            # A driver startup failure must complete every submitted Future,
            # rather than leaving the scheduler waiting on a dead thread.
            while True:
                packet = self.jobs.get()
                if packet is None:
                    return
                packet[1].set_exception(error)

    def _serve(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = context = page = response_reader = None
            try:
                while True:
                    packet = self.jobs.get()
                    if packet is None:
                        break
                    task, result = packet
                    try:
                        if browser is None:
                            browser = playwright.chromium.launch(channel='chrome',headless=True)
                            context = browser.new_context(storage_state=self.state,viewport={'width':1440,'height':1000},service_workers='block')
                            context.set_default_timeout(3000)
                            context.set_default_navigation_timeout(30000)
                            observer = getattr(self.adapter,'_worker_context_observer',None)
                            if observer:
                                observer(context)
                            self.adapter._install_group_navigation_guard(context,None,self.keys,self.progress)
                            if self.request_gate:self.request_gate.attach(context,self.cancelled,self.progress)
                            page = context.new_page()
                            if self.response_queries is not None:
                                from local_response_reader import ResponseReader
                                response_reader=ResponseReader(self.adapter,context,self.response_queries,self.keys,self.cancelled)
                                response_reader.request_gate=self.request_gate
                                observe=getattr(self.adapter,'_response_reader_observer',None)
                                if observe:observe(response_reader)
                        post=None
                        callback=getattr(self,'on_capture',None)
                        capture=(lambda post,comments,task=task:callback(task,post,comments)) if callback else None
                        if response_reader is not None:
                            response_reader.on_capture=capture
                            from local_response_reader import ROOT_QUERY
                            if self.response_queries.get(ROOT_QUERY) is not None:
                                self.progress('running',15,f'Bài {task["post_id"]}: Đang đọc phản hồi Facebook…')
                                post=response_reader.try_read(task['post_url'],self.include_replies,page,self.pipeline)
                            else:response_reader.stats['bootstrapReads']+=1
                        if post is None:
                            if self.request_gate:self.request_gate.check()
                            if self.cancelled():raise RuntimeError('Đã dừng trước khi mở trang dự phòng; tác vụ được giữ lại.')
                            article=self.adapter._open_post_permalink(page,task['post_url'])
                            post, _=self.adapter._extract_post(article,self.include_replies,page,self.keys,
                                media_pipeline=self.pipeline,cancelled=self.cancelled,
                                on_capture=capture,
                                on_stage=lambda text:self.progress('running',15,f'Bài {task["post_id"]}: {text}'))
                        result.set_result(post)
                    except Exception as error:
                        from local_response_reader import ResponseRateLimit
                        if self.request_gate and self.request_gate.blocked:
                            try:self.request_gate.check()
                            except Exception as limited:error=limited
                        if isinstance(error,ResponseRateLimit):self.stop.set()
                        result.set_exception(error)
                        if page is not None and page.is_closed():
                            browser.close()
                            browser=context=page=response_reader=None
            finally:
                if browser is not None:
                    browser.close()

    def close(self):
        for thread in self.threads:
            self.jobs.put(None)
        for thread in self.threads:
            thread.join()
