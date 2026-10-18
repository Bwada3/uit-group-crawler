"""Shared request pacing and durable pauses after Facebook rejects requests."""
from __future__ import annotations

from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
import re
from threading import Lock
import time
from urllib.parse import urlsplit,parse_qs

import local_store as store


class FacebookRateLimit(RuntimeError):
    pass


class RequestCancelled(RuntimeError):
    pass


def setting(name,default,maximum):
    try:
        value=float(os.environ.get(name,str(default)))
        return min(maximum,max(0,value)) if math.isfinite(value) else default
    except ValueError:return default


def retry_delay(value,now):
    if not value:return 0
    try:
        text=str(value).strip()
        if text.isdecimal():return float(text)
        date=parsedate_to_datetime(text)
        if date.tzinfo is None:date=date.replace(tzinfo=timezone.utc)
        return max(0,date.timestamp()-now)
    except (ValueError,TypeError,OverflowError):return 0


def rate_error(raw):
    # Only top-level error envelopes count. A post/comment containing these
    # words must never pause the scraper.
    if not re.search(r'"(?:error|errors|errorSummary|errorDescription)"\s*:',raw):return False
    remaining=raw.strip().removeprefix('for (;;);').lstrip();decoder=json.JSONDecoder()
    while remaining:
        try:packet,end=decoder.raw_decode(remaining)
        except ValueError:return False
        if isinstance(packet,dict):
            errors=packet.get('errors') or []
            messages=[str(packet.get(key) or '') for key in ('errorSummary','errorDescription')]
            if isinstance(errors,list):messages.extend(str(e.get('message','')) for e in errors if isinstance(e,dict))
            if (errors or packet.get('error')) and any(re.search(
                r'rate.?limit|too many requests|request limit|throttl|going too fast|temporarily blocked|quá nhanh|quá nhiều yêu cầu',
                message,re.I) for message in messages):return True
        remaining=remaining[end:].lstrip()
    return False


def _ensure(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS facebook_cooldown ('
        'id INTEGER PRIMARY KEY CHECK(id=1), until REAL NOT NULL, strikes INTEGER NOT NULL, last_limited REAL NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS facebook_limit_events ('
        'event_id INTEGER PRIMARY KEY, detected_at REAL NOT NULL, status INTEGER NOT NULL, '
        'operation TEXT NOT NULL, path TEXT NOT NULL, applies_to_scraper INTEGER NOT NULL, kind TEXT NOT NULL)')


def request_operation(request):
    name=parse_qs(getattr(request,'post_data',None) or '').get('fb_api_req_friendly_name',[''])[0]
    return name if re.fullmatch(r'[A-Za-z0-9_]{1,160}',name) else ''


def data_request(request):
    url=urlsplit(request.url);host=url.hostname or ''
    if not (host=='facebook.com' or host.endswith('.facebook.com')):return False
    if '/api/graphql' in url.path:
        name=request_operation(request)
        return not name or bool(re.search(r'comment|repl|ufi|groupscometfeed',name,re.I))
    try:return request.is_navigation_request() and request.frame.parent_frame is None
    except Exception:return False


def cooldown_status(now=None):
    now=time.time() if now is None else now
    with store._connect() as connection:
        _ensure(connection)
        row=connection.execute('SELECT until,strikes,last_limited FROM facebook_cooldown WHERE id=1').fetchone()
        event=connection.execute('SELECT detected_at,status,operation,path,applies_to_scraper,kind '
                                 'FROM facebook_limit_events ORDER BY event_id DESC LIMIT 1').fetchone()
    until=row[0] if row else 0
    return {'until':until,'strikes':row[1] if row else 0,'lastLimited':row[2] if row else 0,
            'remaining':max(0,math.ceil(until-now)),
            'lastSignal':dict(event) if event else None}


def pause_message(until):
    deadline=datetime.fromtimestamp(until).astimezone().strftime('%H:%M:%S %d/%m/%Y')
    return ('Facebook đang giới hạn yêu cầu. Ứng dụng tạm nghỉ đến '+deadline+
            '; đã giữ dữ liệu và hàng đợi. Hết thời gian chờ không có nghĩa Facebook đã gỡ giới hạn.')


class RequestGate:
    def __init__(self,*,clock=time.time,monotonic=time.monotonic,sleep=time.sleep):
        self.clock=clock;self.monotonic=monotonic;self.sleep=sleep;self.lock=Lock()
        self.interval=setting('FACEBOOK_REQUEST_INTERVAL_SECONDS',0,60)
        self.base_pause=max(60,setting('FACEBOOK_COOLDOWN_SECONDS',900,86400))
        self.until=cooldown_status(self.clock())['until'];self.next_request=0
        self.failure=None

    @property
    def blocked(self):return self.until>self.clock() or self.failure is not None

    def check(self):
        if self.failure is not None:raise RuntimeError('Không lưu được trạng thái tạm nghỉ; đã dừng yêu cầu Facebook.') from self.failure
        if self.until>self.clock():raise FacebookRateLimit(pause_message(self.until))

    def before(self,cancelled=lambda:False,progress=None):
        announced=False
        while True:
            self.check()
            if cancelled():raise RequestCancelled('Đã dừng trước khi gửi yêu cầu Facebook.')
            with self.lock:
                self.check()
                delay=self.next_request-self.monotonic()
                if delay<=0:
                    self.next_request=self.monotonic()+self.interval
                    return
            if progress and not announced and delay>=1:
                progress('running',15,'Đang giãn cách yêu cầu Facebook; dữ liệu đã đọc vẫn được lưu.')
                announced=True
            self.sleep(min(.05,delay))

    def limited(self,retry_after=None):
        with self.lock:
            now=self.clock();hint=retry_delay(retry_after,now)
            # Stop other readers even if committing the pause fails.
            self.until=max(self.until,now+self.base_pause,now+hint)
            try:
                with store._connect() as connection:
                    _ensure(connection)
                    connection.execute('BEGIN IMMEDIATE')
                    row=connection.execute('SELECT until,strikes,last_limited FROM facebook_cooldown WHERE id=1').fetchone()
                    if row and row[0]>now:
                        # Concurrent responses belong to the same incident.
                        until=max(row[0],now+hint);strikes=row[1];last=row[2]
                    else:
                        strikes=(row[1] if row and now-row[2]<86400 else 0)+1
                        until=now+max(hint,min(21600,self.base_pause*2**min(strikes-1,10)))
                        last=now
                    connection.execute('INSERT INTO facebook_cooldown VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                        'until=excluded.until,strikes=excluded.strikes,last_limited=excluded.last_limited',(until,strikes,last))
                self.until=until
            except Exception as error:
                self.failure=error
                raise
        return self.until

    def inspect_response(self,response,request=None):
        headers=response.headers
        kind='http_429' if response.status==429 else ''
        if not kind and response.status==200 and '/api/graphql' in response.url:
            try:raw=response.text()
            except Exception:return  # A transport error is not proof of a limit.
            if len(raw)<=16*1024*1024 and rate_error(raw):kind='graphql_rate_error'
        if not kind:return
        relevant=True if request is None else data_request(request)
        # Store only diagnostic metadata: no query strings, request form,
        # cookies, authentication headers, response bodies or comment text.
        operation=request_operation(request) if request is not None else 'direct_data_request'
        path=urlsplit(getattr(response,'url',getattr(request,'url',''))).path[:300]
        with store._connect() as connection:
            _ensure(connection)
            connection.execute('INSERT INTO facebook_limit_events(detected_at,status,operation,path,applies_to_scraper,kind) '
                               'VALUES(?,?,?,?,?,?)',(self.clock(),response.status,operation,path,int(relevant),kind))
            connection.execute('DELETE FROM facebook_limit_events WHERE event_id NOT IN '
                               '(SELECT event_id FROM facebook_limit_events ORDER BY event_id DESC LIMIT 100)')
        if relevant:self.limited(headers.get('retry-after'))

    def attach(self,context,cancelled=lambda:False,progress=None):
        def facebook(request):
            host=urlsplit(request.url).hostname or ''
            return host=='facebook.com' or host.endswith('.facebook.com')

        def controlled(request):
            url=urlsplit(request.url)
            if not facebook(request):return False
            if self.blocked and '/api/graphql' in url.path:return True
            return data_request(request)

        def route_request(route):
            if not controlled(route.request):route.fallback();return
            try:self.before(cancelled,progress)
            except (FacebookRateLimit,RequestCancelled):route.abort();return
            except Exception as error:self.failure=error;route.abort();return
            route.fallback()

        def response_received(response):
            if not facebook(response.request):return
            try:self.inspect_response(response,response.request)
            except Exception as error:self.failure=error

        context.route('**/*',route_request)
        context.on('response',response_received)
