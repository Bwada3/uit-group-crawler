"""Response extraction uses synthetic objects and fake transport only."""
import base64,copy,json,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode,parse_qs
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import local_scraper as adapter
from local_capture import CaptureWriteError
from local_response_reader import (QueryRegistry,ResponseReader,ResponseFallback,ResponseRateLimit,
    document_story,story_post,comment_record,response_data,ROOT_QUERY,PAGE_QUERY,REPLY_QUERIES,UNFILTERED)

GROUP='230048322891238';POST='111111';URL='https://www.facebook.com/groups/'+GROUP+'/permalink/'+POST+'/'
def encoded(text):return base64.b64encode(text.encode()).decode()
def connection(nodes,more=False,cursor=None):return {'edges':[{'node':n} for n in nodes],'total_count':len(nodes),'page_info':{'has_next_page':more,'has_previous_page':False,'end_cursor':cursor,'start_cursor':None}}
def comment(identifier='222222',depth=0,parent=None,replies=0):
    f={'id':encoded('feedback:'+POST+'_'+identifier),'reactors':{'count_reduced':'2'}}
    if depth<2:f.update(replies_fields={'total_count':replies},replies_connection=connection([]),expansion_info={'expansion_token':'opaque-fixture'})
    return {'__typename':'Comment','id':encoded('comment:'+POST+'_'+identifier),'legacy_fbid':identifier,'depth':depth,
        'comment_direct_parent':{'id':encoded('comment:'+POST+'_'+parent)} if parent else None,
        'body':{'text':'Comment '+identifier},'author':{'name':'Reader'},'created_time':1700000000,'attachments':[],
        'feedback':f}
def story(total=0):
    fid=encoded('feedback:'+POST)
    f={'id':fid,'comment_rendering_instance':{'comments':{'total_count':total}}}
    summary={'id':fid,'adaptive_ufi_action_renderers':[{'feedback':{'id':fid,'reaction_count':{'count':3},'share_count':{'count':1}}}],
        'top_reactions':{'edges':[{'node':{'localized_name':'Like'},'reaction_count':3}]}}
    target={'id':fid,'comment_list_renderer':{'feedback':f},'comet_ufi_summary_and_actions_renderer':{'feedback':summary}}
    return {'__typename':'Story','post_id':POST,'permalink_url':URL,'to':{'__typename':'Group','id':GROUP},
        'feedback':{'id':fid,'associated_group':{'id':GROUP}},'creation_time':1700000000,'actors':[{'name':'Author'}],
        'attachments':[],'comet_sections':{'content':{'story':{'post_id':POST,'message':{'text':'Actual post'}}},
        'feedback':{'story':{'story_ufi_container':{'story':{'feedback_context':{'feedback_target_with_context':target}}}}}}}
def html(s):return '<script type="application/json">'+json.dumps({'require':[['handle',None,{'data':{'node_v2':s}}]]})+'</script>'
def response(text,status=200,url=URL,kind='text/html'):
    return SimpleNamespace(status=status,url=url,headers={'content-type':kind},text=lambda:text,dispose=lambda:None)

class ResponseRegression(unittest.TestCase):
    def setUp(self):
        self.story=story();self.calls=[];self.queries={};self.registry=QueryRegistry();self.events={}
        def get(url,**kwargs):self.calls.append(('GET',url));return response(html(self.story))
        def post(url,**kwargs):
            form=parse_qs(kwargs['data']);name=form['fb_api_req_friendly_name'][0];v=json.loads(form['variables'][0]);self.calls.append((name,v))
            value=self.queries[name](v)
            return value if not isinstance(value,dict) else response(json.dumps({'data':{'node':value}}),kind='application/json')
        self.context=SimpleNamespace(on=lambda e,cb:self.events.setdefault(e,[]).append(cb),request=SimpleNamespace(get=get,post=post))
        self.reader=ResponseReader(adapter,self.context,self.registry,{GROUP},lambda:False,browser_fetch=False)
        self.pipeline=SimpleNamespace(prepare=lambda media,page:media)
    def observe(self,name=ROOT_QUERY,**extra):
        v={'id':encoded('feedback:'+POST),'commentsIntentToken':UNFILTERED,**extra}
        request=SimpleNamespace(url='https://www.facebook.com/api/graphql/',headers={'content-type':'application/x-www-form-urlencoded'},
            post_data=urlencode({'fb_api_req_friendly_name':name,'variables':json.dumps(v),'doc_id':'fixture','fb_dtsg':'fixture-not-secret'}))
        self.registry.observe(request);return request
    def root(self,nodes,**extra):
        return {'__typename':'Feedback','id':encoded('feedback:'+POST),'associated_group':{'id':GROUP},'comment_rendering_instance_for_feed_location':{'comments':connection(nodes,**extra)}}
    def read(self):return self.reader.try_read(URL,True,SimpleNamespace(),self.pipeline)

    def test_post_text_is_separate_from_comment(self):
        self.story=story(1);self.observe();self.queries[ROOT_QUERY]=lambda v:self.root([comment()])
        p=self.read();self.assertEqual(p['text'],'Actual post');self.assertEqual(p['comments'][0]['text'],'Comment 222222');self.assertTrue(p['commentsComplete'])
    def test_foreign_group_is_rejected_before_comments_query(self):
        self.story['to']['id']='999999';self.assertIsNone(self.read());self.assertEqual(len(self.calls),1)
    def test_foreign_feedback_is_rejected(self):
        self.story['feedback']['id']=encoded('feedback:999999');self.assertIsNone(self.read())
    def test_invalid_url_is_rejected_before_request(self):
        self.assertIsNone(self.reader.try_read('https://www.facebook.com/profile.php?id=9',True,None,self.pipeline));self.assertFalse(self.calls)
    def test_loading_document_uses_dom_fallback(self):
        self.context.request.get=lambda *a,**kw:response('<div>Loading...</div>');self.assertIsNone(self.read())
    def test_missing_query_uses_dom_fallback(self):
        self.story=story(1);self.assertIsNone(self.read());self.assertIn('Query not observed: '+ROOT_QUERY,self.reader.stats['fallbackReasons'])
    def test_query_registry_rejects_mutations_and_filtered_root(self):
        self.observe('HideStoryMutation');self.assertFalse(self.registry.templates)
        self.observe(commentsIntentToken='RANKED_UNFILTERED');self.assertFalse(self.registry.templates)
    def test_two_comment_pages_have_correct_ids(self):
        self.story=story(2);self.observe();self.observe(PAGE_QUERY)
        self.queries[ROOT_QUERY]=lambda v:self.root([comment()],more=True,cursor='page2')
        self.queries[PAGE_QUERY]=lambda v:self.root([comment('333333')])
        p=self.read();self.assertEqual({c['commentId'] for c in p['comments']},{'222222','333333'});self.assertTrue(p['commentsComplete'])
        self.assertEqual(self.calls[-1][1]['commentsAfterCursor'],'page2')
    def test_repeated_cursor_falls_back(self):
        self.story=story(2);self.observe();self.observe(PAGE_QUERY)
        self.queries[ROOT_QUERY]=lambda v:self.root([comment()],more=True,cursor='same')
        self.queries[PAGE_QUERY]=self.queries[ROOT_QUERY]
        self.assertIsNone(self.read());self.assertLess(len(self.calls),5)
    def test_after_page_does_not_follow_reverse_cursor(self):
        self.story=story(2);self.observe();self.observe(PAGE_QUERY)
        self.queries[ROOT_QUERY]=lambda v:self.root([comment()],more=True,cursor='next')
        result=self.root([comment('333333')]);result['comment_rendering_instance_for_feed_location']['comments']['page_info'].update(has_previous_page=True,start_cursor='reverse')
        self.queries[PAGE_QUERY]=lambda v:result
        p=self.read();self.assertTrue(p['commentsComplete']);self.assertEqual(len(self.calls),3)
    def test_before_page_does_not_follow_forward_cursor(self):
        self.story=story(2);self.observe();self.observe(PAGE_QUERY)
        initial=self.root([comment('333333')]);initial['comment_rendering_instance_for_feed_location']['comments']['page_info'].update(has_previous_page=True,start_cursor='previous')
        self.queries[ROOT_QUERY]=lambda v:initial
        self.queries[PAGE_QUERY]=lambda v:self.root([comment()],more=True,cursor='reverse')
        p=self.read();self.assertTrue(p['commentsComplete']);self.assertEqual(len(self.calls),3)
        self.assertEqual(self.calls[-1][1]['commentsBeforeCursor'],'previous')
    def test_rotating_cursor_without_new_comment_ids_cannot_loop(self):
        self.story=story(5);self.observe();self.observe(PAGE_QUERY);sequence=[0]
        self.queries[ROOT_QUERY]=lambda v:self.root([comment()],more=True,cursor='first')
        def repeated(v):
            sequence[0]+=1;return self.root([comment()],more=True,cursor='nonce'+str(sequence[0]))
        self.queries[PAGE_QUERY]=repeated
        self.assertIsNone(self.read());self.assertLessEqual(sequence[0],2)
    def test_gif_comment_preserves_image_and_caption(self):
        n=comment();n['body']={'text':''};n['attachments']=[{'media':{'__typename':'GenericAttachmentMedia','animated_image':{'uri':'https://external.fbcdn.net/comment.gif'},'animated_image_caption':'GIF caption'}}]
        row=comment_record(adapter,n,POST);self.assertEqual(row['text'],'GIF caption');self.assertTrue(row['media'][0]['url'].endswith('.gif'))
    def test_unavailable_comment_stays_partial(self):
        self.story=story(2);self.observe();self.queries[ROOT_QUERY]=lambda v:self.root([comment()])
        p=self.read();self.assertEqual(p['commentsCount'],2);self.assertFalse(p['commentsComplete'])
    def test_reply_links_are_preserved(self):
        self.story=story(2);self.observe();self.observe(REPLY_QUERIES[1])
        self.queries[ROOT_QUERY]=lambda v:self.root([comment(replies=1)])
        self.queries[REPLY_QUERIES[1]]=lambda v:{'__typename':'Feedback','id':v['id'],'replies_connection':connection([comment('333333',1,'222222')])}
        p=self.read();self.assertTrue(p['commentsComplete']);self.assertEqual(p['comments'][1]['parentCommentId'],'222222')
    def test_reply_cannot_belong_to_another_post(self):
        n=comment();n['id']=encoded('comment:999999_222222')
        with self.assertRaises(ResponseFallback):comment_record(adapter,n,POST)
    def test_foreign_query_response_owner_falls_back(self):
        self.story=story(1);self.observe();self.queries[ROOT_QUERY]=lambda v:{'id':encoded('feedback:999999')}
        self.assertIsNone(self.read())
    def test_rate_limit_is_propagated(self):
        self.story=story(1);self.observe();self.queries[ROOT_QUERY]=lambda v:response('{}',status=429)
        with self.assertRaises(ResponseRateLimit):self.read()
        self.assertEqual(self.reader.stats['rateLimits'],1)

    def test_content_is_checkpointed_before_comment_rate_limit(self):
        self.story=story(1);self.observe();captured=[]
        self.reader.on_capture=lambda post,rows:captured.append(copy.deepcopy((post,rows)))
        def limited(v):
            self.assertEqual(captured[0][0]['text'],'Actual post')
            self.assertFalse(captured[0][0]['commentsComplete'])
            return response('{}',status=429)
        self.queries[ROOT_QUERY]=limited
        with self.assertRaises(ResponseRateLimit):self.read()
        self.assertEqual(captured[0][1],[])

    def test_each_comment_page_is_checkpointed_before_next_request(self):
        self.story=story(2);self.observe();self.observe(PAGE_QUERY);captured=[]
        self.reader.on_capture=lambda post,rows:captured.append(copy.deepcopy((post,rows)))
        self.queries[ROOT_QUERY]=lambda v:self.root([comment()],more=True,cursor='next')
        def limited(v):
            self.assertEqual([r['commentId'] for r in captured[-1][1]],['222222'])
            self.assertEqual(captured[-1][0]['commentsCapturedCount'],1)
            return response('{}',status=429)
        self.queries[PAGE_QUERY]=limited
        with self.assertRaises(ResponseRateLimit):self.read()
        self.assertFalse(captured[-1][0]['commentsComplete'])

    def test_successful_parallel_page_is_saved_even_if_sibling_is_rate_limited(self):
        self.story=story(4);self.observe();self.observe(REPLY_QUERIES[1]);captured=[]
        self.reader.on_capture=lambda post,rows:captured.extend(copy.deepcopy(rows))
        self.queries[ROOT_QUERY]=lambda v:self.root([comment(replies=1),comment('333333',replies=1)])
        def replies(v):
            if v['id']==encoded('feedback:'+POST+'_222222'):return response('{}',status=429)
            return {'id':v['id'],'replies_connection':connection([comment('444444',1,'333333')])}
        self.queries[REPLY_QUERIES[1]]=replies
        with self.assertRaises(ResponseRateLimit):self.read()
        self.assertEqual({row['commentId'] for row in captured},{'222222','333333','444444'})

    def test_parallel_rate_limit_takes_precedence_over_schema_fallback(self):
        self.story=story(4);self.observe();self.observe(REPLY_QUERIES[1])
        self.queries[ROOT_QUERY]=lambda v:self.root([comment(replies=1),comment('333333',replies=1)])
        def replies(v):
            if v['id']==encoded('feedback:'+POST+'_222222'):return {'id':'wrong'}
            return response('{}',status=429)
        self.queries[REPLY_QUERIES[1]]=replies
        with self.assertRaises(ResponseRateLimit):self.read()

    def test_paced_reader_sends_independent_queries_one_at_a_time(self):
        self.story=story(4);self.observe();self.observe(REPLY_QUERIES[1]);batches=[]
        self.queries[ROOT_QUERY]=lambda v:self.root([comment(replies=1),comment('333333',replies=1)])
        def replies(v):
            parent='222222' if v['id']==encoded('feedback:'+POST+'_222222') else '333333'
            child='444444' if parent=='222222' else '555555'
            return {'id':v['id'],'replies_connection':connection([comment(child,1,parent)])}
        self.queries[REPLY_QUERIES[1]]=replies
        self.reader.request_gate=SimpleNamespace(check=lambda:None,before=lambda *a:None,inspect_response=lambda r:None)
        self.reader.browser_fetch=True
        def fetch(expression,items):
            batches.append(len(items));results=[]
            for args in items:
                form=parse_qs(args['body']);name=form['fb_api_req_friendly_name'][0]
                node=self.queries[name](json.loads(form['variables'][0]))
                results.append({'status':200,'text':json.dumps({'data':{'node':node}})})
            return results
        page=SimpleNamespace(url=URL,evaluate=fetch)
        result=self.reader.try_read(URL,True,page,self.pipeline)
        self.assertTrue(result['commentsComplete']);self.assertEqual(batches,[1,1,1])

    def test_invalid_reply_is_not_checkpointed(self):
        self.story=story(2);self.observe();self.observe(REPLY_QUERIES[1]);captured=[]
        self.reader.on_capture=lambda post,rows:captured.extend(copy.deepcopy(rows))
        self.queries[ROOT_QUERY]=lambda v:self.root([comment(replies=1)])
        self.queries[REPLY_QUERIES[1]]=lambda v:{'id':v['id'],'replies_connection':connection([comment('333333',1,'999999')])}
        self.assertIsNone(self.read())
        self.assertEqual([row['commentId'] for row in captured],['222222'])

    def test_durable_write_error_is_not_swallowed_as_dom_fallback(self):
        def fail(post,rows):raise OSError('Disk full')
        self.reader.on_capture=fail
        with self.assertRaisesRegex(CaptureWriteError,'Disk full'):self.read()
        self.assertEqual(self.reader.stats['fallbacks'],0)
    def test_background_media_retains_visual_letters(self):
        c=self.story['comet_sections']['content']['story'];c['comet_sections']={'message':{'story':{'text_format_metadata':{'background_image':{'uri':'https://static.fbcdn.net/background.jpg'}}}}}
        p=self.read();self.assertEqual(p['media'][0]['visualText'],'Actual post')
    def test_unknown_attachment_falls_back(self):
        self.story['attachments']=[{'media':{'__typename':'UnknownMedia'}}];self.assertIsNone(self.read())
    def test_complete_album_preserves_every_photo(self):
        nodes=[{'media':{'__typename':'Photo','image':{'uri':'https://static.fbcdn.net/'+str(i)+'.jpg'}}} for i in range(6)]
        self.story['attachments']=[{'styles':{'attachment':{'all_subattachments':{'count':6,'nodes':nodes}}}}]
        self.assertEqual(len(self.read()['media']),6)
    def test_incomplete_album_needs_dom(self):
        self.story['attachments']=[{'styles':{'attachment':{'all_subattachments':{'count':6,'nodes':[]}}}}]
        self.assertIsNone(self.read())
    def test_video_retains_validated_playback_source(self):
        self.story['attachments']=[{'media':{'__typename':'Video','id':'9','videoDeliveryResponseFragment':{'videoDeliveryResponseResult':{'progressive_urls':[{'progressive_url':'https://video.fbcdn.net/a.mp4'}]}}}}]
        self.assertEqual(self.read()['media'][0]['type'],'video')
    def test_query_fetch_runs_in_authenticated_browser_origin(self):
        self.story=story(1);self.observe();args=[];self.reader.browser_fetch=True
        page=SimpleNamespace(url=URL,evaluate=lambda js,a:(args.append(a) or [{'status':200,'text':json.dumps({'data':{'node':self.root([comment()])}})}]))
        p=self.reader.try_read(URL,True,page,self.pipeline)
        self.assertTrue(p['commentsComplete']);self.assertEqual(len(args),1);self.assertNotIn('Origin',args[0][0]['headers']);self.assertEqual(len(self.calls),1)
    def test_cancelled_reader_does_not_issue_requests(self):
        self.reader.cancelled=lambda:True
        self.assertIsNone(self.read());self.assertFalse(self.calls)
    def test_feed_metadata_avoids_document_request(self):
        self.registry.put_story(self.story)
        p=self.read();self.assertEqual(p['text'],'Actual post');self.assertFalse(self.calls);self.assertEqual(self.reader.stats['feedStories'],1)
    def test_feed_metadata_is_revalidated_for_group(self):
        self.story['to']['id']='999999';self.registry.put_story(self.story)
        self.assertIsNone(self.read());self.assertFalse(self.calls)
    def test_story_cache_is_bounded_and_returns_independent_objects(self):
        for i in range(70):
            s=copy.deepcopy(self.story);s['post_id']=str(i);self.registry.put_story(s)
        self.assertEqual(len(self.registry.stories),64);self.assertLessEqual(self.registry.story_bytes,16*1024*1024)
        self.assertIsNone(self.registry.take_story('0'));self.assertIsNotNone(self.registry.take_story('69'))
    def test_feed_comment_metadata_uses_owning_feedback(self):
        t=self.story['comet_sections']['feedback']['story']['story_ufi_container']['story']['feedback_context']['feedback_target_with_context']
        f=t.pop('comment_list_renderer')['feedback'];t.update(f,__typename='Feedback')
        self.registry.put_story(self.story);self.assertTrue(self.read()['commentsComplete'])
    def test_comment_attachment_renderer_preserves_image(self):
        n=comment();n['body']={'text':''};n['attachments']=[{'style_type_renderer':{'attachment':{'media':{'__typename':'Photo','image':{'uri':'https://static.fbcdn.net/comment.jpg'}}}}}]
        row=comment_record(adapter,n,POST);self.assertEqual(len(row['media']),1)
    def test_nested_reply_keeps_immediate_parent(self):
        row=comment_record(adapter,comment('444444',2,'333333'),POST)
        self.assertEqual(row['threadingDepth'],2);self.assertEqual(row['parentCommentId'],'333333')
    def test_flattened_descendants_keep_valid_parent_chains(self):
        self.story=story(3);self.observe();self.observe(REPLY_QUERIES[1]);self.observe(REPLY_QUERIES[2])
        self.queries[ROOT_QUERY]=lambda v:self.root([comment(replies=2)])
        self.queries[REPLY_QUERIES[1]]=lambda v:{'id':v['id'],'replies_connection':connection([comment('333333',1,'222222',1),comment('444444',2,'333333')])}
        self.queries[REPLY_QUERIES[2]]=lambda v:{'id':v['id'],'replies_connection':connection([comment('444444',2,'333333')])}
        p=self.read();self.assertTrue(p['commentsComplete']);self.assertEqual(len(p['comments']),3)
    def test_unrelated_reply_chain_falls_back(self):
        self.story=story(2);self.observe();self.observe(REPLY_QUERIES[1])
        self.queries[ROOT_QUERY]=lambda v:self.root([comment(replies=1)])
        self.queries[REPLY_QUERIES[1]]=lambda v:{'id':v['id'],'replies_connection':connection([comment('333333',1,'999999')])}
        self.assertIsNone(self.read())
    def test_browser_query_waves_are_bounded_to_three(self):
        self.observe();self.reader.browser_fetch=True;batches=[]
        def evaluate(js,args):
            batches.append(len(args))
            return [{'status':200,'text':json.dumps({'data':{'node':self.root([])}})} for _ in args]
        self.reader.page=SimpleNamespace(url=URL,evaluate=evaluate)
        result=self.reader.query_many([(ROOT_QUERY,{'id':encoded('feedback:'+POST)})]*8,URL)
        self.assertEqual(len(result),8);self.assertEqual(batches,[3,3,2])
    def test_error_envelope_rate_limit_stops_reads(self):
        with self.assertRaises(ResponseRateLimit):response_data('{"errors":[{"message":"Rate limit exceeded"}]}')
    def test_comment_text_cannot_trigger_rate_limit_detection(self):
        d=response_data('{"data":{"node":{"body":{"text":"rate limit exceeded"}}}}')
        self.assertIn('node',d)
    def test_exact_reactions_can_reconcile_without_overlay(self):
        p=self.read();self.assertEqual(p['reactionLikeCount'],3);self.assertEqual(p['reactionLoveCount'],0);self.assertTrue(p['reactionBreakdownComplete'])
    def test_mismatched_reaction_total_does_not_infer_zero(self):
        t=self.story['comet_sections']['feedback']['story']['story_ufi_container']['story']['feedback_context']['feedback_target_with_context']
        t['comet_ufi_summary_and_actions_renderer']['feedback']['adaptive_ufi_action_renderers'][0]['feedback']['reaction_count']['count']=4
        p=self.read();self.assertFalse(p['reactionBreakdownComplete']);self.assertIsNone(p['reactionLoveCount'])
    def test_no_comments_needs_no_query(self):
        p=self.read();self.assertTrue(p['commentsComplete']);self.assertEqual(len(self.calls),1)
    def test_document_script_is_parsed_without_executing_js(self):
        s=document_story(html(self.story)+'<script>throw Error("never execute")</script>',POST,{GROUP})
        self.assertEqual(s['post_id'],POST)

if __name__=='__main__':unittest.main()
