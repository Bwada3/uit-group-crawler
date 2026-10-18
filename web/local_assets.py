"""Restore bounded caching for public static assets while routing guards stay on."""
from __future__ import annotations

from collections import OrderedDict
import os
import re
import time
from urllib.parse import urlsplit


def static_asset(url: str) -> bool:
    parsed=urlsplit(url)
    host=(parsed.hostname or '').lower()
    return parsed.scheme=='https' and (host=='fbcdn.net' or host.endswith('.fbcdn.net')) and '/rsrc.php/' in parsed.path


class AssetCache:
    def __init__(self,context,*,eligible_url=static_asset,max_bytes=32*1024*1024,max_entry_bytes=4*1024*1024,clock=time.monotonic):
        self.context=context;self.eligible_url=eligible_url;self.clock=clock
        self.max_bytes=max_bytes;self.max_entry_bytes=min(max_entry_bytes,max_bytes)
        self.entries=OrderedDict();self.bytes=0;self.served=set()
        self.closed=False
        self.stats={'hits':0,'misses':0,'stored':0,'savedBytes':0,'evictions':0}
        context.route('**/*',self._route)
        context.on('requestfinished',self._finished)
        context.on('requestfailed',self._failed)
        context.on('close',self._close)

    def _key(self,request):
        return (request.url,request.headers.get('origin',''))

    def _eligible(self,request):
        return request.method=='GET' and not request.headers.get('range') and request.resource_type in {'script','stylesheet','font'} and self.eligible_url(request.url)

    def _route(self,route):
        request=route.request
        if self.closed:route.fallback();return
        if not self._eligible(request):route.fallback();return
        key=self._key(request);entry=self.entries.get(key)
        if entry and entry['expires']<=self.clock():
            self._drop(key);entry=None
        if entry is None:
            self.stats['misses']+=1
            route.fallback();return
        self.entries.move_to_end(key)
        self.served.add(request)
        self.stats['hits']+=1;self.stats['savedBytes']+=len(entry['body'])
        route.fulfill(status=200,headers=entry['headers'],body=entry['body'])

    def _drop(self,key):
        entry=self.entries.pop(key,None)
        if entry:self.bytes-=len(entry['body'])

    def _finished(self,request):
        if request in self.served:
            self.served.discard(request);return
        if self.closed or not self._eligible(request):return
        try:
            response=request.response()
            if response is None or response.status!=200:return
            # response.headers omits security-related fields such as Set-Cookie.
            headers={key.lower():value for key,value in response.all_headers().items()}
            control=headers.get('cache-control','').lower()
            directives={part.strip().split('=',1)[0] for part in control.split(',')}
            if 'public' not in directives or directives & {'no-store','no-cache','private'} or 'set-cookie' in headers:return
            age=re.search(r'(?:^|,)\s*max-age\s*=\s*"?(\d+)',control)
            if not age or int(age.group(1))<=0:return
            ttl=min(3600,int(age.group(1))-max(0,int(headers.get('age','0'))))
            if ttl<=0:return
            vary={value.strip().lower() for value in headers.get('vary','').split(',') if value.strip()}
            if vary-{'accept-encoding'}:return
            kind=headers.get('content-type','').lower()
            if not any(value in kind for value in ('javascript','text/css','font','woff')):return
            if int(headers.get('content-length','0'))>self.max_entry_bytes:return
            body=response.body()
            if self.closed or not body or len(body)>self.max_entry_bytes:return
            # Playwright exposes decoded response bytes. Do not replay their
            # original compressed or hop-by-hop transport headers.
            cached_headers={key:value for key,value in headers.items() if key not in
                {'content-encoding','content-length','transfer-encoding','connection','keep-alive','age'}}
            key=self._key(request)
            self._drop(key)
            while self.entries and (self.bytes+len(body)>self.max_bytes or len(self.entries)>=256):
                oldest=next(iter(self.entries));self._drop(oldest);self.stats['evictions']+=1
            self.entries[key]={'body':body,'headers':cached_headers,'expires':self.clock()+ttl}
            self.bytes+=len(body);self.stats['stored']+=1
        except Exception:
            # Caching is optional. Network/extraction behavior stays unchanged
            # when a resource is unavailable or cannot be cached safely.
            return

    def _failed(self,request):
        self.served.discard(request)

    def _close(self,*args):
        self.closed=True;self.entries.clear();self.served.clear();self.bytes=0


def install_asset_cache(context):
    # Real Facebook replay exposed intermittent stalled page bootstrapping.
    # Keep this experimental cache opt-in until that is resolved.
    if os.environ.get('FACEBOOK_ASSET_CACHE','0')!='1':return
    if getattr(context,'_grouppulse_asset_cache',None) is None:
        context._grouppulse_asset_cache=AssetCache(context)
