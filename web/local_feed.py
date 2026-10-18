"""Resume the group's native pagination, using a fresh browser-observed query.

Only pagination state is persisted. Authentication comes from fresh browser requests.
"""
from __future__ import annotations
import hashlib
import json
import os
import time
from types import SimpleNamespace
from urllib.parse import parse_qs,urlencode
from local_tasks import post_id

QUERY='GroupsCometFeedRegularStoriesPaginationQuery'


class FeedResumeError(RuntimeError):
    pass


def stream_data(raw: str) -> dict:
    """Assemble Relay's initial data and streamed/deferred path patches."""
    decoder=json.JSONDecoder();packets=[];raw=raw.strip()
    if raw.startswith('for (;;);'):raw=raw[9:].lstrip()
    while raw:
        packet,end=decoder.raw_decode(raw)
        if not isinstance(packet,dict) or packet.get('errors') or packet.get('error'):
            raise FeedResumeError('Facebook trả lỗi khi tải trang bài viết; điểm tiếp tục cũ được giữ nguyên.')
        packets.append(packet);raw=raw[end:].lstrip()
    root={}
    def merge(target,value):
        if isinstance(target,dict) and isinstance(value,dict):
            for key,item in value.items():
                if key in target:target[key]=merge(target[key],item)
                else:target[key]=item
            return target
        return value
    for packet in packets:
        data=packet.get('data')
        if not isinstance(data,dict):continue
        path=packet.get('path',[])
        if not path:root=merge(root,data);continue
        node=root
        for index,key in enumerate(path):
            last=index==len(path)-1
            if isinstance(node,list):
                if not isinstance(key,int) or key<0 or key>10000:raise FeedResumeError('Định dạng trang feed đã thay đổi.')
                while len(node)<=key:node.append(None)
                if last:node[key]=merge(node[key],data);break
                if node[key] is None:node[key]=[] if isinstance(path[index+1],int) else {}
                node=node[key]
            elif isinstance(node,dict):
                if last:node[key]=merge(node.get(key),data);break
                node=node.setdefault(key,[] if isinstance(path[index+1],int) else {})
            else:raise FeedResumeError('Định dạng trang feed đã thay đổi.')
    return root


def page_urls(adapter,raw: str,group_id: str,keys: set[str],on_story=None) -> tuple[list[str],dict]:
    data=stream_data(raw)
    group=data.get('node',{})
    if not isinstance(group,dict) or str(group.get('id',''))!=group_id or group.get('__typename')!='Group':
        raise FeedResumeError('Trang phân trang không thuộc group nguồn; chưa thay đổi điểm tiếp tục.')
    feed=group.get('group_feed',{});info=feed.get('page_info')
    if not isinstance(info,dict) or not isinstance(info.get('has_next_page'),bool) or not isinstance(feed.get('edges'),list):
        raise FeedResumeError('Không xác minh được phân trang feed; chưa thay đổi điểm tiếp tục.')
    if info['has_next_page'] and (not isinstance(info.get('end_cursor'),str) or not info['end_cursor']):
        raise FeedResumeError('Facebook chưa trả con trỏ trang tiếp theo.')
    urls=[];stories=[]
    for edge in feed['edges']:
        if not isinstance(edge,dict) or not isinstance(edge.get('node'),dict):
            raise FeedResumeError('Trang feed có phần bài viết chưa tải đủ.')
        story=edge['node']
        if story.get('__typename')=='GroupsSectionHeaderUnit':continue
        if story.get('__typename')!='Story':
            raise FeedResumeError('Có loại mục feed chưa được hỗ trợ; chưa tiến điểm tiếp tục.')
        target=story.get('to')
        if not isinstance(target,dict) or target.get('__typename')!='Group':
            raise FeedResumeError('Chưa xác minh được group của bài viết.')
        if str(target.get('id',''))!=group_id:continue
        # Trust the alias only when Facebook identifies this very Group ID.
        alias=adapter._group_key_from_url(str(target.get('url') or ''))
        if alias:keys.add(alias)
        url=adapter._canonical_group_permalink(str(story.get('permalink_url') or ''),keys)
        identifier=str(story.get('post_id') or '')
        if not identifier.isdecimal() or not url or post_id(url)!=identifier:
            raise FeedResumeError('ID bài viết không khớp permalink; chưa tiến điểm tiếp tục.')
        if url not in urls:urls.append(url);stories.append(story)
    if on_story:
        for story in stories:on_story(story)
    return urls,{'cursor':info.get('end_cursor'),'hasNext':info['has_next_page']}


class NativeFeed:
    def __init__(self,adapter,page,queue,keys,known,progress,resume=True):
        self.adapter=adapter;self.page=page;self.queue=queue;self.keys=keys;self.known=known;self.progress=progress
        self.saved=queue.feed_checkpoint() if resume else None
        self.template=None;self.bootstrap_page=None;self.state=None;self.error=None
        self.ready=False;self.resumed=False;self.request_count=0;self.resumed_cursor_requests=0
        self.on_story=None
        self.added=0;self.ids=set();self.seen_cursors=set();self.pending=None
        self.page.on('requestfinished',self._observe)
        self.page.on('requestfailed',self._failed)

    def _failed(self,request):
        if self.pending and self.pending.get('request')==request:
            self.pending['error']=FeedResumeError('Yêu cầu phân trang bị gián đoạn; điểm đã lưu vẫn được giữ nguyên.')

    def _parse(self,raw):
        observer=(lambda story:self.on_story(story) if not self.known(story['permalink_url']) else None) if self.on_story else None
        return page_urls(self.adapter,raw,self.template['group'],self.keys,observer)

    def _observe(self,request):
        if self.pending and self.pending.get('request')==request:
            try:
                response=request.response()
                if response is None or response.status!=200:
                    raise FeedResumeError('Facebook chưa chấp nhận yêu cầu phân trang; giữ điểm cũ và dừng lượt này.')
                observer=getattr(self.adapter,'_feed_response_observer',None)
                if observer:observer(response)
                self.pending['result']=self._parse(response.text())
            except Exception as error:self.pending['error']=error
            return
        if self.template is not None or '/api/graphql' not in request.url:return
        form=parse_qs(request.post_data or '',keep_blank_values=True)
        if form.get('fb_api_req_friendly_name')!=[QUERY]:return
        try:
            variables=json.loads(form.get('variables',['{}'])[0]);group_id=str(variables.get('id',''))
            if group_id not in self.keys:return
            response=request.response()
            if response is None or response.status!=200:return
            observer=(lambda story:self.on_story(story) if not self.known(story['permalink_url']) else None) if self.on_story else None
            urls,state=page_urls(self.adapter,response.text(),group_id,self.keys,observer)
            signature={key:variables.get(key) for key in ('id','sortingSetting','feedType','feedLocation','filterTopicId','useDefaultActor')}
            signature['actor']=hashlib.sha256((form.get('__user',[''])[0]+'|'+form.get('av',[''])[0]).encode()).hexdigest()
            headers={key:value for key,value in request.headers.items() if key.lower() in
                     {'content-type','x-fb-friendly-name','x-fb-lsd'}}
            self.template={'variables':variables,'signature':signature,'group':group_id,
                           'form':form,'url':request.url,'headers':headers}
            self.bootstrap_page=(urls,state)
        except Exception as error:
            self.error=error

    def bootstrap(self,cancelled) -> bool:
        deadline=time.monotonic()+5
        # Only a few startup cards load to obtain a fresh, browser-issued query.
        # Later pages use the saved cursor without rendering intervening cards.
        while self.template is None and time.monotonic()<deadline and not cancelled():
            self.page.evaluate('window.scrollTo(0,document.scrollingElement.scrollHeight)')
            self.page.wait_for_timeout(150)
        if cancelled():return False
        if self.template is None:
            if self.saved:
                raise FeedResumeError('Chưa lấy được truy vấn phân trang hiện tại. Điểm đã lưu vẫn còn; thử lại hoặc bỏ chọn tiếp tục để quét từ đầu.')
            return False
        if self.saved:
            if self.saved.get('signature')!=self.template['signature']:
                raise FeedResumeError('Điểm tiếp tục không khớp tài khoản hoặc chế độ sắp xếp. Bỏ chọn tiếp tục để quét lại từ đầu.')
            self.state=self.saved;self.resumed=True
            self.progress('running',15,'Đã khôi phục điểm tiếp tục lịch sử; không cần cuộn lại toàn bộ feed từ đầu.')
        else:
            # A fresh pass starts the same feed connection at its beginning.
            # The bootstrap request only obtains current query/authentication;
            # its cursor must not skip the initial server-rendered stories.
            self.state={'version':1,'signature':self.template['signature'],'cursor':None,'hasNext':True}
        self.ready=True
        return True

    def _commit(self,urls,state):
        state={'version':1,'signature':self.template['signature'],**state}
        filtered=[url for url in urls if not self.known(url)]
        self.added+=self.queue.commit_feed_page(filtered,state)
        self.ids.update(post_id(url) for url in urls)
        self.state=state

    def fetch_next(self):
        gate=getattr(self,'request_gate',None)
        if gate:gate.check()
        if os.environ.get('FACEBOOK_FEED_TRANSPORT','fetch')=='route':
            return self._fetch_via_route()
        if not self.ready or not self.state['hasNext']:return
        cursor=self.state['cursor']
        if cursor in self.seen_cursors:
            raise FeedResumeError('Facebook lặp con trỏ feed; đã giữ nguyên dữ liệu để tránh tải lặp vô hạn.')
        try:count=min(50,max(1,int(os.environ.get('FACEBOOK_FEED_BATCH_SIZE','20'))))
        except ValueError:count=20
        template=self.template
        variables={**template['variables'],'cursor':cursor,'count':count}
        form={**template['form'],'variables':[json.dumps(variables,separators=(',',':'))]}
        body=urlencode([(key,value) for key,values in form.items() for value in values])
        try:
            result=self.page.evaluate("""async args=>{
                if(new URL(args.url,location.href).origin!==location.origin)throw new Error('Wrong feed origin');
                const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),15000);
                try {
                    const response=await fetch(args.url,{method:'POST',headers:args.headers,body:args.body,
                        credentials:'same-origin',mode:'same-origin',redirect:'error',signal:controller.signal});
                    return {status:response.status,text:await response.text()};
                } finally {clearTimeout(timer);}
            }""",{'url':template['url'],'headers':template['headers'],'body':body})
        except Exception:
            if gate:gate.check()
            raise FeedResumeError('Mất kết nối khi tải trang feed tiếp theo; điểm đã lưu được giữ nguyên để tiếp tục.') from None
        if gate:gate.check()
        if result['status']==429:
            raise FeedResumeError('Facebook đang giới hạn yêu cầu; đã giữ điểm tiếp tục và dừng lượt này.')
        if result['status']!=200:
            raise FeedResumeError('Facebook chưa chấp nhận điểm tiếp tục; điểm cũ được giữ nguyên. Thử lại sau hoặc quét từ đầu.')
        observer=getattr(self.adapter,'_feed_response_observer',None)
        if observer:observer(SimpleNamespace(status=result['status'],text=lambda:result['text']))
        urls,state=self._parse(result['text'])
        if state['hasNext'] and state['cursor']==cursor:
            raise FeedResumeError('Facebook chưa trả trang tiếp theo hợp lệ; giữ nguyên điểm tiếp tục.')
        self._commit(urls,state)
        self.request_count+=1
        self.seen_cursors.add(cursor)
        if self.resumed:self.resumed_cursor_requests+=1

    def _fetch_via_route(self):
        gate=getattr(self,'request_gate',None)
        if gate:gate.check()
        if not self.ready or not self.state['hasNext']:return
        cursor=self.state['cursor']
        if cursor in self.seen_cursors:
            raise FeedResumeError('Facebook lặp con trỏ feed; đã giữ nguyên dữ liệu để tránh tải lặp vô hạn.')
        try:count=min(50,max(1,int(os.environ.get('FACEBOOK_FEED_BATCH_SIZE','20'))))
        except ValueError:count=20
        self.pending={}
        def route_page(route):
            request=route.request
            form=parse_qs(request.post_data or '',keep_blank_values=True)
            if form.get('fb_api_req_friendly_name')!=[QUERY] or self.pending.get('request'):
                route.fallback();return
            try:variables=json.loads(form.get('variables',['{}'])[0])
            except ValueError:route.fallback();return
            if str(variables.get('id',''))!=self.template['group']:
                route.fallback();return
            # Keep the browser's fresh authentication fields and real query.
            # Only pagination variables change, within this same group/feed.
            if any(variables.get(key)!=self.template['variables'].get(key) for key in
                   ('sortingSetting','feedType','feedLocation','filterTopicId','useDefaultActor')):
                self.pending['error']=FeedResumeError('Chế độ feed đã thay đổi; giữ điểm cũ để kiểm tra lại.')
                route.abort();return
            form['variables']=[json.dumps({**variables,'cursor':cursor,'count':count},separators=(',',':'))]
            self.pending['request']=request
            pairs=[(key,value) for key,values in form.items() for value in values]
            route.fallback(post_data=urlencode(pairs))
        pattern='**/api/graphql/**'
        self.page.route(pattern,route_page)
        try:
            deadline=time.monotonic()+15
            while not self.pending.get('result') and not self.pending.get('error') and time.monotonic()<deadline:
                if not self.pending.get('request'):
                    self.page.evaluate('window.scrollTo(0,document.scrollingElement.scrollHeight)')
                self.page.wait_for_timeout(150)
            if gate:gate.check()
            if self.pending.get('error'):raise self.pending['error']
            if not self.pending.get('result'):
                raise FeedResumeError('Facebook chưa tải được trang feed tiếp theo; điểm cũ được giữ nguyên. Thử tiếp tục lại sau.')
            self.request_count+=1
            urls,state=self.pending['result']
            if state['hasNext'] and state['cursor']==cursor:
                raise FeedResumeError('Facebook chưa trả trang tiếp theo hợp lệ; giữ nguyên điểm tiếp tục.')
            self._commit(urls,state)
            self.seen_cursors.add(cursor)
            if self.resumed:self.resumed_cursor_requests+=1
        finally:
            self.page.unroute(pattern,route_page)
            self.pending=None

    def close(self):
        self.page.remove_listener('requestfinished',self._observe)
        self.page.remove_listener('requestfailed',self._failed)
