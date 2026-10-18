"""Save attachments and overlap CPU OCR with browser work; no Playwright in threads."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import Future
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Lock
import local_store as store


class MediaPipeline:
    def __init__(self, recognize):
        self.recognize = recognize
        self.directory = store.DATA_DIR / 'media'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='cpu-ocr')
        self.lock=Lock()
        self.downloads={}

    def prepare(self, media: list[dict], page) -> list[dict]:
        for item in media:
            item.setdefault('ocrText', '')
            item['ocrStatus'] = 'not_applicable' if item['type'] != 'image' else 'pending'
            if item['type'] != 'image':
                continue
            if item.get('localPath') and (store.DATA_DIR / 'media' / Path(item['localPath']).name).is_file():
                continue
            with self.lock:
                cached=self.downloads.get(item['url'])
            if cached and (self.directory/Path(cached['localPath']).name).is_file():
                item.update(cached)
                continue
            response = None
            try:
                response = page.context.request.get(item['url'], timeout=8000, headers={'Referer': page.url})
                kind = response.headers.get('content-type', '').split(';')[0].lower()
                if not response.ok or not kind.startswith('image/'):
                    raise ValueError('Image is unavailable')
                raw = response.body()
                if not raw or len(raw) > 20 * 1024 * 1024:
                    raise ValueError('Image exceeds the supported size')
                digest = hashlib.sha256(raw).hexdigest()
                suffix = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif'}.get(kind, '.img')
                filename = digest + suffix
                path = self.directory / filename
                with self.lock:
                    if not path.exists():
                        with tempfile.NamedTemporaryFile(dir=self.directory, suffix='.tmp', delete=False) as output:
                            output.write(raw)
                            temporary = Path(output.name)
                        temporary.replace(path)
                item.update(localPath='data/media/'+filename, localUrl='/api/media/'+filename, contentHash=digest)
                with self.lock:
                    if len(self.downloads)>=512:
                        self.downloads.pop(next(iter(self.downloads)))
                    self.downloads[item['url']]={key:item[key] for key in ('localPath','localUrl','contentHash')}
            except Exception:
                item['ocrStatus'] = 'unavailable'
            finally:
                if response is not None:
                    try:response.dispose()
                    except Exception:pass
        return media

    def submit(self, post: dict):
        # The worker owns a copy; the browser's checkpoint cannot race with OCR.
        copied = json.loads(json.dumps(post))
        if not any(item['type'] == 'image' for item in copied['media']):
            copied.update(ocrStatus='no_image',ocrText='')
            future = Future()
            future.set_result(copied)
            return future
        return self.executor.submit(self._finish, copied)

    def _finish(self, post: dict) -> dict:
        for item in post['media']:
            if item['type'] != 'image' or item.get('ocrStatus') == 'unavailable':
                continue
            try:
                path = self.directory / Path(item['localPath']).name
                cache = self.directory / (path.stem+'.ocr-v6.json')
                if cache.is_file():
                    text = json.loads(cache.read_text(encoding='utf-8'))['text']
                else:
                    text = self.recognize(path.read_bytes())
                    temporary = cache.with_suffix('.tmp')
                    temporary.write_text(json.dumps({'text': text}, ensure_ascii=False), encoding='utf-8')
                    temporary.replace(cache)
                visual = item.pop('visualText', '')
                item.update(ocrText=text or visual, ocrStatus='complete', ocrSource='ocr' if text or not visual else 'facebook_text_layer')
            except Exception:
                item['ocrStatus'] = 'unavailable'
        images = [item for item in post['media'] if item['type'] == 'image']
        post['ocrStatus'] = ('complete' if all(item['ocrStatus'] == 'complete' for item in images) else 'partial') if images else 'no_image'
        post['ocrText'] = '\n\n'.join(dict.fromkeys(item.get('ocrText', '') for item in images if item.get('ocrText')))
        return post

    def close(self):
        self.executor.shutdown(wait=True)
