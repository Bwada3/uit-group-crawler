"""Opt-in reader of Facebook's own responses, with fail-closed DOM fallback.

Only browser-observed comment queries are replayed. Auth templates live in
memory; HTML and GraphQL responses must prove the source group and post IDs.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
import copy
from datetime import datetime
from html.parser import HTMLParser
import json
import re
from threading import Lock
from types import SimpleNamespace
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from local_feed import stream_data
from local_capture import CaptureWriteError,emit_capture
from local_throttle import FacebookRateLimit as ResponseRateLimit,rate_error

ROOT_QUERY='CommentListComponentsRootQuery'
PAGE_QUERY='CommentsListComponentsPaginationQuery'
REPLY_QUERIES={1:'Depth1CommentsListPaginationQuery',2:'Depth2CommentsListPaginationQuery'}
ALLOWED={ROOT_QUERY,PAGE_QUERY,*REPLY_QUERIES.values()}
UNFILTERED='CHRONOLOGICAL_UNFILTERED_INTENT_V1'


class ResponseFallback(Exception):
    """Incomplete/unsupported response: use the existing browser reader."""


def decoded(value):
    try:return base64.b64decode(value,validate=True).decode('utf-8')
    except Exception:raise ResponseFallback('Invalid encoded object ID') from None


def at(node,*path):
    for key in path:
        if not isinstance(node,dict):return None
        node=node.get(key)
    return node


def response_data(raw):
    # Inspect only error envelopes, never arbitrary comment text.
    if rate_error(raw):raise ResponseRateLimit('Facebook rate limit; stop response reads')
    remaining=raw.strip().removeprefix('for (;;);').lstrip();decoder=json.JSONDecoder()
    while remaining:
        try:packet,end=decoder.raw_decode(remaining)
        except ValueError:raise ResponseFallback('Query response is not JSON') from None
        if not isinstance(packet,dict):raise ResponseFallback('Invalid query envelope')
        errors=packet.get('errors') or []
        messages=[str(packet.get(key) or '') for key in ('errorSummary','errorDescription')]
        if isinstance(errors,list):messages.extend(str(error.get('message','')) for error in errors if isinstance(error,dict))
        if (errors or packet.get('error')) and any(re.search(r'rate.?limit|too many requests|request limit|throttl',message,re.I) for message in messages):
            raise ResponseRateLimit('Facebook rate limit; stop response reads')
        remaining=remaining[end:].lstrip()
    try:return stream_data(raw)
    except Exception as error:raise ResponseFallback('Query schema/error response') from error


def objects(value):
    if isinstance(value,dict):
        yield value
        for child in value.values():yield from objects(child)
    elif isinstance(value,list):
        for child in value:yield from objects(child)


class JsonScripts(HTMLParser):
    def __init__(self):super().__init__();self.collect=False;self.parts=[];self.values=[]
    def handle_starttag(self,tag,attrs):
        if tag=='script':self.collect=dict(attrs).get('type')=='application/json';self.parts=[]
    def handle_data(self,data):
        if self.collect:self.parts.append(data)
    def handle_endtag(self,tag):
        if tag=='script' and self.collect:
            try:self.values.append(json.loads(''.join(self.parts)))
            except (ValueError,RecursionError):pass
            self.collect=False;self.parts=[]


def document_story(html,post_id,group_keys):
    parser=JsonScripts();parser.feed(html)
    for node in objects(parser.values):
        if valid_story(node,post_id,group_keys):return node
    raise ResponseFallback('No verified source-group story in the response')


def valid_story(node,post_id,group_keys):
    if node.get('__typename')!='Story' or str(node.get('post_id',''))!=post_id:return False
    if at(node,'to','__typename')!='Group' or str(at(node,'to','id')) not in group_keys:return False
    if str(at(node,'feedback','associated_group','id'))!=str(at(node,'to','id')):return False
    try:return decoded(at(node,'feedback','id'))=='feedback:'+post_id
    except ResponseFallback:return False


class QueryRegistry:
    """Shared plain dicts only; no Playwright objects cross reader threads."""
    def __init__(self):
        self.lock=Lock();self.templates={};self.stories=OrderedDict();self.story_bytes=0
    def observe(self,request):
        parsed=urlsplit(request.url)
        if parsed.scheme!='https' or parsed.hostname!='www.facebook.com' or parsed.path.rstrip('/')!='/api/graphql':return
        form=parse_qs(request.post_data or '',keep_blank_values=True)
        name=form.get('fb_api_req_friendly_name',[''])[0]
        if name not in ALLOWED:return
        try:
            variables=json.loads(form.get('variables',[''])[0])
            if name==ROOT_QUERY and variables.get('commentsIntentToken')!=UNFILTERED:return
            if not isinstance(variables.get('id'),str):return
            headers={key:value for key,value in request.headers.items() if key.lower() in {'content-type','x-fb-lsd','x-asbd-id'}}
            headers.update({'x-fb-friendly-name':name,'Origin':'https://www.facebook.com'})
            template={'url':request.url,'form':form,'variables':variables,'headers':headers}
            with self.lock:self.templates[name]=template
        except Exception:return
    def get(self,name):
        with self.lock:return copy.deepcopy(self.templates.get(name))
    def put_story(self,story):
        raw=json.dumps(story,ensure_ascii=False).encode('utf-8')
        if len(raw)>2*1024*1024:return
        key=str(story.get('post_id',''))
        with self.lock:
            previous=self.stories.pop(key,None)
            if previous:self.story_bytes-=len(previous)
            while self.stories and (len(self.stories)>=64 or self.story_bytes+len(raw)>16*1024*1024):
                _,old=self.stories.popitem(last=False);self.story_bytes-=len(old)
            self.stories[key]=raw;self.story_bytes+=len(raw)
    def take_story(self,key):
        with self.lock:
            raw=self.stories.pop(key,None)
            if raw:self.story_bytes-=len(raw)
        return json.loads(raw) if raw else None


def image(adapter,url,alt='',**extra):
    if not isinstance(url,str) or not adapter._valid_media_url(url):raise ResponseFallback('Unsupported media URL')
    return {'type':'image','url':url,'alt':alt or '',**extra}


def attachment_media(adapter,attachments):
    if not isinstance(attachments,list):raise ResponseFallback('Missing attachment metadata')
    result=[]
    for attachment in attachments:
        album=at(attachment,'styles','attachment','all_subattachments')
        if album is not None:
            if not isinstance(album,dict) or type(album.get('count')) is not int or not isinstance(album.get('nodes'),list) or len(album['nodes'])!=album['count']:
                raise ResponseFallback('Incomplete album metadata')
            result.extend(attachment_media(adapter,album['nodes']))
            continue
        media=at(attachment,'styles','attachment','media') or at(attachment,'style_type_renderer','attachment','media') or attachment.get('media')
        if not isinstance(media,dict):raise ResponseFallback('Unsupported attachment')
        kind=media.get('__typename')
        if kind=='Photo':
            resource=media.get('photo_image') or media.get('image')
            if not resource:raise ResponseFallback('Photo URL missing')
            result.append(image(adapter,resource.get('uri'),media.get('accessibility_caption','')))
        elif kind=='Video':
            sources=at(media,'videoDeliveryResponseFragment','videoDeliveryResponseResult','progressive_urls') or []
            source=next((item.get('progressive_url') for item in sources if isinstance(item,dict) and adapter._valid_media_url(str(item.get('progressive_url','')))),None)
            if not source:raise ResponseFallback('Video source needs DOM')
            result.append({'type':'video','url':source,'alt':'','mediaId':media.get('id','')})
        elif kind=='GenericAttachmentMedia':
            resource=media.get('animated_image') or media.get('image') or media.get('sticker_image')
            if not isinstance(resource,dict) or not resource.get('uri'):raise ResponseFallback('External attachment needs DOM')
            result.append(image(adapter,resource['uri'],media.get('accessibility_caption') or media.get('animated_image_caption','')))
        else:raise ResponseFallback('Unsupported attachment kind')
    return result


def story_post(adapter,story,url):
    identifier=str(story['post_id'])
    if adapter._canonical_group_permalink(story.get('permalink_url',''))!=adapter._canonical_group_permalink(url):
        # Slug/numeric aliases are equivalent only after group validation above.
        if not re.search(r'/(?:posts|permalink)/'+re.escape(identifier)+r'/?$',urlsplit(story.get('permalink_url','')).path):
            raise ResponseFallback('Story URL mismatch')
    if story.get('attached_story') or story.get('work_reposted_story'):raise ResponseFallback('Shared story needs DOM')
    content=at(story,'comet_sections','content','story')
    if not isinstance(content,dict) or str(content.get('post_id'))!=identifier:raise ResponseFallback('Content owner mismatch')
    message=content.get('message')
    if message is not None and not isinstance(message.get('text'),str):raise ResponseFallback('Unsupported post text')
    text=(message or {}).get('text','').strip()
    media=attachment_media(adapter,story.get('attachments'))
    # Text-background posts carry their visible letters separately from the
    # plain background image. Preserve both for CPU OCR/text-layer fallback.
    section=at(content,'comet_sections','message','story')
    fmt=at(section,'text_format_metadata')
    background=at(fmt,'background_image','uri')
    if background:media.append(image(adapter,background,visualText=text))
    if not text and not media:raise ResponseFallback('Post has no supported content')
    context=at(story,'comet_sections','feedback','story','story_ufi_container','story','feedback_context','feedback_target_with_context')
    if not isinstance(context,dict) or context.get('id')!=story['feedback']['id']:raise ResponseFallback('Feedback owner mismatch')
    feedback=at(context,'comment_list_renderer','feedback') or (context if context.get('__typename')=='Feedback' else None)
    if not isinstance(feedback,dict) or feedback.get('id')!=context['id']:raise ResponseFallback('Comment owner mismatch')
    total=at(feedback,'comment_rendering_instance','comments','total_count')
    if type(total) is not int or total<0:raise ResponseFallback('Comment total missing')
    summary=at(context,'comet_ufi_summary_and_actions_renderer','feedback') or {}
    counts={key:None for key in adapter.REACTION_LABELS}
    reactions=None
    for entry in summary.get('adaptive_ufi_action_renderers',[]):
        value=at(entry,'feedback','reaction_count','count')
        if type(value) is int:reactions=value;break
    for edge in at(summary,'top_reactions','edges') or []:
        label=at(edge,'node','localized_name') or ''
        value=edge.get('reaction_count')
        if type(value) is not int or value<0:continue
        for key,pattern in adapter.REACTION_LABELS.items():
            if pattern.fullmatch(label.strip()):counts[key]=value;break
    complete=reactions is not None and sum(v for v in counts.values() if v is not None)==reactions
    if complete:counts={key:value or 0 for key,value in counts.items()}
    post={'id':url,'url':url,'postUrl':url,'author':(story.get('actors') or [{}])[0].get('name') or '\u004b\u0068\u00f4\u006e\u0067 \u0072\u00f5',
        'text':text,'media':list({m['url']:m for m in media}.values()),'ocrText':'','ocrStatus':'pending',
        'time':datetime.fromtimestamp(story['creation_time']).isoformat(),'likesCount':reactions,**counts,
        'reactionBreakdownAvailable':any(v is not None for v in counts.values()),'reactionBreakdownComplete':complete,
        'commentsCount':total,'commentsCapturedCount':0,'commentsComplete':False,'sharesCount':0,'comments':[],
        'captureMethod':'browser_response'}
    # Missing share counts must not be silently changed to zero.
    shares=[]
    for obj in objects(summary):
        if 'share_count' in obj:
            value=obj['share_count'];value=value.get('count') if isinstance(value,dict) else value
            if type(value) is int:shares.append(value)
    post['sharesCount']=max(shares) if shares else None
    return post,feedback


def comment_record(adapter,node,post_id):
    identifier=node.get('legacy_fbid')
    if node.get('__typename')!='Comment' or not isinstance(identifier,str) or not identifier.isdecimal():raise ResponseFallback('Missing comment identity')
    if decoded(node.get('id'))!='comment:'+post_id+'_'+identifier:raise ResponseFallback('Comment belongs to another post')
    parent=node.get('comment_direct_parent');parent_id=''
    if parent:
        parent_key=decoded(parent.get('id'));prefix='comment:'+post_id+'_'
        if not parent_key.startswith(prefix) or not parent_key[len(prefix):].isdecimal():raise ResponseFallback('Reply parent mismatch')
        parent_id=parent_key[len(prefix):]
    depth=node.get('depth')
    if type(depth) is not int or depth<0 or depth>2 or bool(parent_id)!=(depth>0):raise ResponseFallback('Unsupported comment depth')
    body=node.get('body') or node.get('preferred_body') or {}
    text=body.get('text','')
    if not isinstance(text,str):raise ResponseFallback('Unsupported comment body')
    attachments=node.get('attachments')
    media=attachment_media(adapter,attachments) if attachments else []
    if node.get('attached_story'):raise ResponseFallback('Attached comment story needs DOM')
    if not text and not media:raise ResponseFallback('Empty comment has unsupported content')
    if not text:text='\n'.join(dict.fromkeys(item.get('alt','') for item in media if item.get('alt')))
    feedback=node.get('feedback') or {}
    count=at(feedback,'reactors','count')
    if count is None:
        reduced=at(feedback,'reactors','count_reduced')
        count=int(reduced) if isinstance(reduced,str) and reduced.isdecimal() else None
    row={'commentId':identifier,'profileName':at(node,'author','name') or 'Unknown','text':text,
        'date':datetime.fromtimestamp(node['created_time']).isoformat(),'likesCount':count,'threadingDepth':depth,'parentCommentId':parent_id}
    if media:row['media']=media
    return row


class ResponseReader:
    def __init__(self,adapter,context,registry,keys,cancelled,*,browser_fetch=True):
        self.adapter=adapter;self.context=context;self.registry=registry;self.keys=keys;self.cancelled=cancelled
        self.browser_fetch=browser_fetch;self.page=None
        self.on_capture=None
        self.request_gate=None
        self.stats={'bootstrapReads':0,'attempts':0,'posts':0,'fallbacks':0,'feedStories':0,'documentRequests':0,'queryRequests':0,'rateLimits':0,'seconds':0.,'fallbackReasons':{}}
        context.on('request',registry.observe)

    def _check(self):
        if self.request_gate:self.request_gate.check()
        if self.cancelled():raise ResponseFallback('Cancelled')

    def _request(self,name,variables,url):
        template=self.registry.get(name)
        if template is None:raise ResponseFallback('Query not observed: '+name)
        form=template['form'];form['variables']=[json.dumps({**template['variables'],**variables},separators=(',',':'))]
        return {'url':template['url'],'headers':{k:v for k,v in template['headers'].items() if k.lower() not in {'origin','referer'}},
            'body':urlencode([(key,value) for key,values in form.items() for value in values]),'referrer':url}

    def _query_result(self,name,variables,result):
        status=result['status'];raw=result['text']
        if status==429:self.stats['rateLimits']+=1;raise ResponseRateLimit('Facebook rate limit; stop response reads')
        if status!=200:raise ResponseFallback('Query HTTP status '+str(status))
        if len(raw)>16*1024*1024:raise ResponseFallback('Query response too large')
        observer=getattr(self,'query_observer',None)
        if observer:observer(name,raw)
        try:data=response_data(raw)
        except ResponseRateLimit:self.stats['rateLimits']+=1;raise
        node=data.get('node')
        if not isinstance(node,dict) or node.get('id')!=variables['id']:raise ResponseFallback('Query response owner mismatch')
        return node

    def query_many(self,queries,url,on_result=None):
        nodes=[]
        size=1 if self.request_gate is not None else 3
        for start in range(0,len(queries),size):
            self._check();batch=queries[start:start+size]
            requests=[self._request(name,variables,url) for name,variables in batch]
            self.stats['queryRequests']+=len(batch)
            if self.browser_fetch:
                if urlsplit(self.page.url).scheme!='https' or urlsplit(self.page.url).hostname!='www.facebook.com':
                    self.page.goto('https://www.facebook.com/groups/'+sorted(self.keys)[0]+'/',wait_until='commit',timeout=15000)
                results=self.page.evaluate("""async items=>{
                    return await Promise.all(items.map(async args=>{
                        if(new URL(args.url,location.href).origin!==location.origin)throw Error('Wrong query origin');
                        const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
                        try{
                            const response=await fetch(args.url,{method:'POST',headers:args.headers,body:args.body,
                                referrer:args.referrer,credentials:'same-origin',mode:'same-origin',redirect:'error',signal:controller.signal});
                            return {status:response.status,text:await response.text()};
                        }finally{clearTimeout(timer);}
                    }));
                }""",requests)
            else:
                results=[]
                for args in requests:
                    if self.request_gate:self.request_gate.before(self.cancelled)
                    response=self.context.request.post(args['url'],data=args['body'],headers={**args['headers'],'Referer':url},timeout=15000,max_redirects=0)
                    try:
                        if self.request_gate:self.request_gate.inspect_response(response)
                        results.append({'status':response.status,'text':response.text()})
                    finally:response.dispose()
            if not isinstance(results,list) or len(results)!=len(batch):raise ResponseFallback('Incomplete query batch')
            failures=[]
            for index,((name,variables),result) in enumerate(zip(batch,results)):
                try:
                    node=self._query_result(name,variables,result)
                    if on_result:on_result(start+index,node)
                    nodes.append(node)
                except (ResponseRateLimit,ResponseFallback) as error:
                    failures.append(error)
            if failures:
                # Save successful sibling responses before stopping. A rate
                # limit must take precedence over an unsupported-response error.
                raise next((error for error in failures if isinstance(error,ResponseRateLimit)),failures[0])
        return nodes

    def query(self,name,variables,url):
        return self.query_many([(name,variables)],url)[0]

    def _comments(self,post,feedback,url,include_replies):
        identifier=re.search(r'/(?:permalink|posts)/(\d+)',url).group(1)
        root=self.query(ROOT_QUERY,{'id':feedback['id']},url)
        group=at(root,'associated_group','id')
        if group is not None and str(group) not in self.keys:raise ResponseFallback('Comment query group mismatch')
        connection=at(root,'comment_rendering_instance_for_feed_location','comments')
        records={};seen=set();pending=[];parents={};stalled={}
        published={}

        def publish():
            # Buffer replies until the observed connection's parent chain can
            # be proved. A later malformed page must never publish foreign rows.
            if self.on_capture is None:return
            valid=[]
            for row in records.values():
                parent=row['parentCommentId'];visited={row['commentId']}
                while parent and parent in records and parent not in visited:
                    visited.add(parent);parent=records[parent]['parentCommentId']
                if parent:continue
                owned=True
                for ancestor in parents.get(row['commentId'],()):
                    parent=row['parentCommentId'];visited=set()
                    while parent!=ancestor and parent in records and parent not in visited:
                        visited.add(parent);parent=records[parent]['parentCommentId']
                    if parent!=ancestor:owned=False;break
                if owned:valid.append(row)
            changed=[row for row in valid if published.get(row['commentId'])!=row]
            if changed:
                post.update(comments=valid,commentsCapturedCount=len(valid),commentsComplete=False)
                emit_capture(self.on_capture,post,changed)
                for row in changed:published[row['commentId']]=dict(row)

        def consume(current,owner,level,base_variables):
            if not isinstance(current,dict) or not isinstance(current.get('edges'),list):raise ResponseFallback('Missing comment connection')
            info=current.get('page_info')
            if not isinstance(info,dict) or type(info.get('has_next_page')) is not bool or type(info.get('has_previous_page')) is not bool:raise ResponseFallback('Incomplete comment pagination')
            before=len(records)
            for edge in current['edges']:
                node=edge.get('node') if isinstance(edge,dict) else None
                if not isinstance(node,dict):raise ResponseFallback('Missing comment node')
                row=comment_record(self.adapter,node,identifier)
                if level:parents.setdefault(row['commentId'],set()).add(base_variables['_parent'])
                previous=records.get(row['commentId']);records[row['commentId']]=row
                if previous is not None:continue
                child=node.get('feedback') or {};fields=child.get('replies_fields')
                if include_replies and row['threadingDepth']<2:
                    count=fields.get('total_count') if isinstance(fields,dict) else None
                    if type(count) is not int:raise ResponseFallback('Missing reply total')
                    replies=child.get('replies_connection')
                    if count>0 or (isinstance(replies,dict) and replies.get('edges')):
                        child_id=child.get('id')
                        if decoded(child_id)!='feedback:'+identifier+'_'+row['commentId']:raise ResponseFallback('Reply feedback mismatch')
                        token=at(child,'expansion_info','expansion_token')
                        if not isinstance(token,str):raise ResponseFallback('Reply expansion state missing')
                        next_level=row['threadingDepth']+1
                        variables={'id':child_id,'expansionToken':token,'_parent':row['commentId']}
                        pending.append((REPLY_QUERIES[next_level],variables,child_id,next_level))
            travelling=base_variables.get('_direction')
            if travelling:
                key=(owner,travelling)
                stalled[key]=stalled.get(key,0)+1 if len(records)==before else 0
                more=info['has_next_page'] if travelling=='After' else info['has_previous_page']
                if stalled[key]>=2 and more:raise ResponseFallback('Comment pages return no new IDs')
            for direction,flag,cursor_key in (('After','has_next_page','end_cursor'),('Before','has_previous_page','start_cursor')):
                # Reverse cursors point back to the page just consumed. Start
                # both directions only at the initial focused page, then keep
                # each traversal monotonic even when cursor nonces change.
                if travelling and travelling!=direction:continue
                if not info[flag]:continue
                cursor=info.get(cursor_key);key=(owner,direction,cursor)
                if not isinstance(cursor,str) or not cursor or key in seen:raise ResponseFallback('Repeated/missing comment cursor')
                seen.add(key)
                if level==0:name=PAGE_QUERY;prefix='comments'
                else:name=REPLY_QUERIES[level];prefix='replies' if level==1 else 'subReplies'
                variables=dict(base_variables)
                variables['_direction']=direction
                variables.update({prefix+'AfterCursor':None,prefix+'BeforeCursor':None,prefix+'AfterCount':None,prefix+'BeforeCount':None})
                variables[prefix+direction+'Cursor']=cursor;variables[prefix+direction+'Count']=-1
                pending.append((name,variables,owner,level))

        consume(connection,feedback['id'],0,{'id':feedback['id']})
        publish()
        while pending:
            self._check();batch=pending[:3];del pending[:3]
            queries=[(name,{k:v for k,v in variables.items() if not k.startswith('_')}) for name,variables,_,_ in batch]
            def received(index,result):
                _,variables,owner,level=batch[index]
                next_connection=at(result,'comment_rendering_instance_for_feed_location','comments') if level==0 else result.get('replies_connection')
                consume(next_connection,owner,level,variables)
                publish()
            self.query_many(queries,url,on_result=received)
        # A depth-1 connection can include flattened depth-2 descendants.
        # Validate the full parent chain after every page has been collected.
        for child,ancestors in parents.items():
            for ancestor in ancestors:
                parent=records[child]['parentCommentId'];visited=set()
                while parent!=ancestor:
                    if not parent or parent in visited or parent not in records:raise ResponseFallback('Reply connection parent mismatch')
                    visited.add(parent);parent=records[parent]['parentCommentId']
        post['comments']=list(records.values());post['commentsCapturedCount']=len(records)
        total=connection.get('total_count')
        if type(total) is int:post['commentsCount']=max(post['commentsCount'],total)
        post['commentsComplete']=len(records)>=post['commentsCount']

    def try_read(self,url,include_replies,page,media_pipeline):
        started=time.monotonic();self.stats['attempts']+=1
        self.page=page
        try:
            self._check()
            canonical=self.adapter._canonical_group_permalink(url,self.keys)
            if not canonical:raise ResponseFallback('Post URL not in source group')
            identifier=re.search(r'/permalink/(\d+)/',canonical).group(1)
            story=self.registry.take_story(identifier)
            if story is not None:
                if not valid_story(story,identifier,self.keys):raise ResponseFallback('Feed story owner mismatch')
                self.stats['feedStories']+=1
            else:
                self.stats['documentRequests']+=1
                if self.request_gate:self.request_gate.before(self.cancelled)
                response=self.context.request.get(canonical,headers={
                    'Referer':'https://www.facebook.com/groups/'+sorted(self.keys)[0]+'/',
                    'Accept':'text/html','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document','Upgrade-Insecure-Requests':'1'},timeout=15000,max_redirects=0)
                try:
                    if self.request_gate:self.request_gate.inspect_response(response)
                    if response.status==429:self.stats['rateLimits']+=1;raise ResponseRateLimit('Facebook rate limit; stop response reads')
                    if response.status!=200 or 'text/html' not in response.headers.get('content-type',''):raise ResponseFallback('Document HTTP status '+str(response.status))
                    if self.adapter._canonical_group_permalink(response.url,self.keys)!=canonical:raise ResponseFallback('Document redirect/owner mismatch')
                    html=response.text()
                    if len(html)>16*1024*1024:raise ResponseFallback('Document response too large')
                finally:response.dispose()
                story=document_story(html,identifier,self.keys)
            observer=getattr(self,'story_observer',None)
            if observer:observer(story)
            post,feedback=story_post(self.adapter,story,canonical)
            emit_capture(self.on_capture,post,[])
            if post['commentsCount']>0 and self.registry.get(ROOT_QUERY) is None:
                raise ResponseFallback('Query not observed: '+ROOT_QUERY)
            post['media']=media_pipeline.prepare(post['media'],SimpleNamespace(context=self.context,url=canonical))
            if post['media']:emit_capture(self.on_capture,post,[])
            if any(m.get('ocrStatus')=='pending' for m in post['media']):
                media_pipeline.submit({'media':post['media']})
            if post['commentsCount']>0:self._comments(post,feedback,canonical,include_replies)
            else:post['commentsComplete']=True
            self.stats['posts']+=1
            return post
        except ResponseRateLimit:
            if self.request_gate:self.request_gate.limited()
            raise
        except CaptureWriteError:raise
        except Exception as error:
            self.stats['fallbacks']+=1
            reason=str(error) if isinstance(error,ResponseFallback) else type(error).__name__
            self.stats['fallbackReasons'][reason]=self.stats['fallbackReasons'].get(reason,0)+1
            return None
        finally:
            self.stats['seconds']+=time.monotonic()-started
            observer=getattr(self,'stats_observer',None)
            if observer:observer(self.stats)
