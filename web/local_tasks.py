"""Durable URL work queue. Browser locators never cross the discovery boundary."""
from __future__ import annotations

import json
import re
from datetime import datetime,timezone
import local_store as store


def post_id(url: str) -> str:
    match = re.search(r"/(?:permalink|posts)/(\d+)(?:/|$)", url)
    return match.group(1) if match else store.normalize_post_url(url)


class TaskQueue:
    def __init__(self, source: str):
        self.source = source
        store.init_store()
        with store._connect() as connection:
            self.key = store._resolve_source_key(connection, source)
            connection.execute("INSERT OR IGNORE INTO sources(source_key,source_url) VALUES (?,?)", (self.key, source))
            connection.execute("UPDATE scrape_tasks SET state='queued', attempts=0 WHERE source_key=? AND state IN ('reading','partial','failed')", (self.key,))
            connection.execute("UPDATE scrape_tasks SET state='media_pending' WHERE source_key=? AND state='ocr_running'", (self.key,))

    def add(self, urls: list[str]) -> int:
        with store._connect() as connection:
            before = connection.total_changes
            connection.executemany("INSERT OR IGNORE INTO scrape_tasks(source_key,post_id,post_url) VALUES (?,?,?)",
                                   [(self.key, post_id(url), url) for url in urls])
            return connection.total_changes - before

    def feed_checkpoint(self) -> dict | None:
        with store._connect() as connection:
            row=connection.execute('SELECT state_json FROM feed_checkpoints WHERE source_key=?',(self.key,)).fetchone()
        return json.loads(row[0]) if row else None

    def commit_feed_page(self,urls: list[str],state: dict) -> int:
        """A cursor may advance only in the same transaction as its queued URLs."""
        encoded=json.dumps(state,ensure_ascii=False)
        with store._connect() as connection:
            before=connection.total_changes
            connection.executemany('INSERT OR IGNORE INTO scrape_tasks(source_key,post_id,post_url) VALUES (?,?,?)',
                [(self.key,post_id(url),url) for url in urls])
            added=connection.total_changes-before
            connection.execute('INSERT INTO feed_checkpoints(source_key,state_json,updated_at) VALUES (?,?,?) '
                'ON CONFLICT(source_key) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at',
                (self.key,encoded,datetime.now(timezone.utc).isoformat()))
            return added

    def next(self) -> dict | None:
        with store._connect() as connection:
            row = connection.execute("SELECT rowid AS queue_order,* FROM scrape_tasks WHERE source_key=? AND state IN ('queued','media_pending') ORDER BY rowid LIMIT 1", (self.key,)).fetchone()
            if not row:
                return None
            item = dict(row)
            connection.execute("UPDATE scrape_tasks SET state='reading', attempts=attempts+1 WHERE source_key=? AND post_id=?", (self.key, item['post_id']))
            return item

    def update(self, task: dict, state: str, payload: dict | None = None, error: str = "") -> None:
        # A browser may have saved a newer checkpoint since next() returned the
        # task. Retrying must preserve that database value, not the stale dict.
        encoded = '' if state=='done' else json.dumps(payload,ensure_ascii=False) if payload is not None else None
        with store._connect() as connection:
            connection.execute("UPDATE scrape_tasks SET state=?, payload_json=COALESCE(?,payload_json), error=? WHERE source_key=? AND post_id=?",
                               (state, encoded, error[:500], self.key, task['post_id']))

    def remaining(self) -> int:
        with store._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM scrape_tasks WHERE source_key=? AND state!='done'", (self.key,)).fetchone()[0]

    def identities(self) -> set[str]:
        with store._connect() as connection:
            return {row[0] for row in connection.execute('SELECT post_id FROM scrape_tasks WHERE source_key=?',(self.key,))}

    def queued_count(self) -> int:
        with store._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM scrape_tasks WHERE source_key=? AND state IN ('queued','media_pending')",(self.key,)).fetchone()[0]
