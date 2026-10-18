"""Durable, per-source storage for locally scraped Facebook data."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlsplit, urlunsplit


HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
DATABASE_PATH = DATA_DIR / "group_pulse.sqlite3"
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


def normalize_source_key(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    if host in {"www.facebook.com", "m.facebook.com", "web.facebook.com"}:
        host = "facebook.com"
    path = "/" + "/".join(part for part in parsed.path.split("/") if part)
    return f"{host}{path}".casefold()


def normalize_post_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if not parsed.scheme or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if host in {"www.facebook.com", "m.facebook.com", "web.facebook.com"}:
        host = "www.facebook.com"
    path = "/" + "/".join(part for part in parsed.path.split("/") if part)
    if path != "/":
        path = path.rstrip("/")
    group_path = re.fullmatch(
        r"(/groups/[^/]+)(?:/(?:posts|permalink)/(\d+))?",
        path,
        re.IGNORECASE,
    )
    if group_path:
        post_id = group_path.group(2)
        if not post_id:
            query = parse_qs(parsed.query)
            post_id = next((
                match.group(1)
                for key in ("multi_permalinks", "story_fbid")
                if (values := query.get(key))
                if (match := re.match(r"(\d+)", values[0]))
            ), "")
        if post_id:
            path = f"{group_path.group(1)}/permalink/{post_id}"
    return urlunsplit((parsed.scheme.lower(), host, path, "", "")).casefold()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        with connection:
            yield connection
    finally:
        # sqlite3.Connection.__exit__ commits/rolls back but does not close.
        # Exception tracebacks can otherwise retain live DB handles on Windows.
        connection.close()


def init_store() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY and DATABASE_PATH.exists():
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY and DATABASE_PATH.exists():
            return
        with _connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
            CREATE TABLE IF NOT EXISTS sources (
                source_key TEXT PRIMARY KEY,
                source_url TEXT NOT NULL,
                canonical_url TEXT NOT NULL DEFAULT '',
                last_scraped_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS source_aliases (
                alias_key TEXT PRIMARY KEY,
                source_key TEXT NOT NULL REFERENCES sources(source_key) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS posts (
                source_key TEXT NOT NULL REFERENCES sources(source_key) ON DELETE CASCADE,
                post_key TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                post_json TEXT NOT NULL,
                PRIMARY KEY (source_key, post_key)
            );
            CREATE TABLE IF NOT EXISTS comments (
                source_key TEXT NOT NULL,
                post_key TEXT NOT NULL,
                comment_key TEXT NOT NULL,
                comment_json TEXT NOT NULL,
                PRIMARY KEY (source_key, post_key, comment_key),
                FOREIGN KEY (source_key, post_key)
                    REFERENCES posts(source_key, post_key) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_posts_source_captured
                ON posts(source_key, captured_at);
            CREATE INDEX IF NOT EXISTS idx_comments_source_post
                ON comments(source_key, post_key);
            CREATE TABLE IF NOT EXISTS scrape_tasks (
                source_key TEXT NOT NULL REFERENCES sources(source_key) ON DELETE CASCADE,
                post_id TEXT NOT NULL,
                post_url TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (source_key, post_id)
            );
            CREATE TABLE IF NOT EXISTS feed_checkpoints (
                source_key TEXT PRIMARY KEY REFERENCES sources(source_key) ON DELETE CASCADE,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_scrape_tasks_source_state
                ON scrape_tasks(source_key,state);
            CREATE TABLE IF NOT EXISTS post_updates (
                revision INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                post_key TEXT NOT NULL,
                UNIQUE(source_key, post_key),
                FOREIGN KEY (source_key, post_key)
                    REFERENCES posts(source_key, post_key) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_post_updates_source_revision
                ON post_updates(source_key, revision);
            CREATE TRIGGER IF NOT EXISTS posts_capture_insert AFTER INSERT ON posts
            BEGIN
                DELETE FROM post_updates WHERE source_key=NEW.source_key AND post_key=NEW.post_key;
                INSERT INTO post_updates(source_key, post_key)
                VALUES (NEW.source_key, NEW.post_key);
            END;
            CREATE TRIGGER IF NOT EXISTS posts_capture_update AFTER UPDATE ON posts
            BEGIN
                DELETE FROM post_updates WHERE source_key=NEW.source_key AND post_key=NEW.post_key;
                INSERT INTO post_updates(source_key, post_key)
                VALUES (NEW.source_key, NEW.post_key);
            END;
                """
            )
        _SCHEMA_READY = True


def _resolve_source_key(connection: sqlite3.Connection, source_url: str) -> str:
    alias_key = normalize_source_key(source_url)
    row = connection.execute(
        "SELECT source_key FROM source_aliases WHERE alias_key = ?", (alias_key,)
    ).fetchone()
    return str(row["source_key"]) if row else alias_key


def register_source_aliases(source_url: str, aliases: list[str], canonical_url: str = "") -> None:
    """Make alternate Facebook URLs for one source use the same stored dataset."""
    init_store()
    alias_keys = {normalize_source_key(source_url)}
    alias_keys.update(normalize_source_key(alias) for alias in aliases if alias)
    alias_keys.discard("")
    preferred_key = normalize_source_key(canonical_url or source_url)
    with _connect() as connection:
        placeholders = ",".join("?" for _ in alias_keys)
        existing = connection.execute(
            f"SELECT DISTINCT source_key FROM source_aliases WHERE alias_key IN ({placeholders})",
            tuple(alias_keys),
        ).fetchall() if alias_keys else []
        source_keys = [str(row["source_key"]) for row in existing]
        target_key = source_keys[0] if source_keys else preferred_key
        connection.execute(
            "INSERT OR IGNORE INTO sources(source_key, source_url, canonical_url) VALUES (?, ?, ?)",
            (target_key, source_url, canonical_url),
        )
        for other_key in source_keys[1:]:
            if other_key == target_key:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO posts(source_key, post_key, captured_at, post_json) "
                "SELECT ?, post_key, captured_at, post_json FROM posts WHERE source_key = ?",
                (target_key, other_key),
            )
            connection.execute(
                "INSERT OR IGNORE INTO comments(source_key, post_key, comment_key, comment_json) "
                "SELECT ?, post_key, comment_key, comment_json FROM comments WHERE source_key = ?",
                (target_key, other_key),
            )
            connection.execute(
                "INSERT OR IGNORE INTO scrape_tasks(source_key, post_id, post_url, state, attempts, payload_json, error) "
                "SELECT ?, post_id, post_url, state, attempts, payload_json, error FROM scrape_tasks WHERE source_key = ?",
                (target_key, other_key),
            )
            connection.execute(
                "UPDATE source_aliases SET source_key = ? WHERE source_key = ?",
                (target_key, other_key),
            )
            connection.execute(
                "INSERT OR IGNORE INTO feed_checkpoints(source_key,state_json,updated_at) "
                "SELECT ?,state_json,updated_at FROM feed_checkpoints WHERE source_key=?",
                (target_key,other_key),
            )
            connection.execute("DELETE FROM sources WHERE source_key = ?", (other_key,))
        for alias_key in alias_keys | {target_key}:
            connection.execute(
                "INSERT INTO source_aliases(alias_key, source_key) VALUES (?, ?) "
                "ON CONFLICT(alias_key) DO UPDATE SET source_key = excluded.source_key",
                (alias_key, target_key),
            )
        connection.execute(
            "UPDATE sources SET canonical_url = COALESCE(NULLIF(?, ''), canonical_url) "
            "WHERE source_key = ?",
            (canonical_url, target_key),
        )


def _stable_post_key(post: dict) -> str:
    url_key = normalize_post_url(str(post.get("url") or post.get("postUrl") or ""))
    if url_key:
        return url_key
    raw = json.dumps(
        [post.get("author"), post.get("time"), post.get("text"), post.get("media")],
        ensure_ascii=False,
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def known_post_keys(source_url: str) -> set[str]:
    """Return every post already saved for this source, so runs only add new posts."""
    init_store()
    with _connect() as connection:
        source_key = _resolve_source_key(connection, source_url)
        rows = connection.execute(
            "SELECT post_key FROM posts WHERE source_key = ? "
            "AND COALESCE(json_extract(post_json, '$.commentsComplete'), 1) != 0 "
            "AND COALESCE(json_extract(post_json, '$.ocrStatus'), 'complete') NOT IN ('pending', 'partial', 'unavailable')",
            (source_key,),
        ).fetchall()
    return {str(row["post_key"]) for row in rows}


def known_group_aliases(source_url: str) -> set[str]:
    """Read verified numeric/slug group aliases already associated with this source."""
    init_store()
    with _connect() as connection:
        source_key = _resolve_source_key(connection, source_url)
        rows = connection.execute(
            "SELECT alias_key FROM source_aliases WHERE source_key = ?", (source_key,)
        ).fetchall()
    return {
        match.group(1)
        for row in rows
        if (match := re.fullmatch(r"facebook\.com/groups/([^/]+)", str(row["alias_key"])))
    }


def save_post(source_url: str, post: dict, comments: list[dict], *, partial: bool = False) -> None:
    """Commit captured content and comment upserts, including unfinished reads."""
    init_store()
    post_key = _stable_post_key(post)
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        source_key = _resolve_source_key(connection, source_url)
        if partial:
            previous=connection.execute('SELECT post_json FROM posts WHERE source_key=? AND post_key=?',
                                        (source_key,post_key)).fetchone()
            if previous:
                old=json.loads(previous[0]);post=dict(post)
                for key in ('text','author','time','ocrText'):
                    value=post.get(key)
                    if not value or (key=='text' and str(old.get(key,'')).startswith(str(value))):
                        if key in old:post[key]=old[key]
                if old.get('reactionBreakdownComplete') and not post.get('reactionBreakdownComplete'):
                    for key in ('likesCount','reactionBreakdownAvailable','reactionBreakdownComplete',
                                'reactionLikeCount','reactionLoveCount','reactionCareCount','reactionHahaCount',
                                'reactionWowCount','reactionSadCount','reactionAngryCount'):
                        if key in old:post[key]=old[key]
                if post.get('sharesCount') is None and 'sharesCount' in old:post['sharesCount']=old['sharesCount']
                old_media={item.get('url'):item for item in (old.get('media') or []) if isinstance(item,dict)}
                if not post.get('media') and old_media:post['media']=list(old_media.values())
                for item in post.get('media') or []:
                    previous_media=old_media.get(item.get('url'),{})
                    for key in ('localPath','localUrl','contentHash','ocrText','ocrSource'):
                        if not item.get(key) and previous_media.get(key):item[key]=previous_media[key]
        post_json = json.dumps(post, ensure_ascii=False, separators=(",", ":"))
        connection.execute(
            "INSERT OR IGNORE INTO sources(source_key, source_url) VALUES (?, ?)",
            (source_key, source_url),
        )
        connection.execute(
            "INSERT INTO posts(source_key, post_key, captured_at, post_json) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source_key, post_key) DO UPDATE SET "
            "captured_at = excluded.captured_at, post_json = excluded.post_json",
            (source_key, post_key, now, post_json),
        )
        for comment in comments:
            identity = [comment.get("profileName"), comment.get("date"),
                        comment.get("text"), comment.get("threadingDepth")]
            comment_id = str(comment.get("commentId") or "")
            if comment_id.isdecimal():
                # Separate identical comments from the same author; Facebook's
                # own ID also stays stable when media URLs or text change.
                identity = ["facebook-comment", comment_id]
            elif comment.get("media"):
                identity.append([item.get("url") for item in comment["media"]])
            signature = json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
            )
            comment_key = hashlib.sha256(signature.encode("utf-8")).hexdigest()
            comment_json = json.dumps(comment, ensure_ascii=False, separators=(",", ":"))
            connection.execute(
                "INSERT OR IGNORE INTO comments(source_key, post_key, comment_key, comment_json) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(source_key, post_key, comment_key) "
                "DO UPDATE SET comment_json = excluded.comment_json",
                (source_key, post_key, comment_key, comment_json),
            )
        connection.execute(
            "UPDATE sources SET last_scraped_at = ? WHERE source_key = ?",
            (now, source_key),
        )


def _post_time_key(post: dict, captured_at: str, row_number: int) -> tuple[int, float]:
    value = str(post.get("time") or "").strip()
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        return 0, -timestamp
    except (OverflowError, OSError, ValueError):
        try:
            return 1, -datetime.fromisoformat(captured_at.replace("Z", "+00:00")).timestamp()
        except (OverflowError, OSError, ValueError):
            return 1, -float(row_number)


def load_saved_data(source_url: str) -> dict:
    init_store()
    with _connect() as connection:
        # Posts, comments and the update cursor must describe one committed
        # snapshot, even while two readers are saving in another connection.
        connection.execute('BEGIN')
        source_key = _resolve_source_key(connection, source_url)
        revision = connection.execute(
            'SELECT COALESCE(MAX(revision), 0) FROM post_updates WHERE source_key=?',
            (source_key,),
        ).fetchone()[0]
        post_rows = connection.execute(
            "SELECT rowid, post_key, captured_at, post_json FROM posts WHERE source_key = ?",
            (source_key,),
        ).fetchall()
        comment_rows = connection.execute(
            "SELECT post_key, comment_json FROM comments WHERE source_key = ? ORDER BY rowid",
            (source_key,),
        ).fetchall()
        source_row = connection.execute(
            "SELECT source_url, canonical_url, last_scraped_at FROM sources WHERE source_key = ?",
            (source_key,),
        ).fetchone()

    posts_with_order = []
    for row in post_rows:
        post = json.loads(row["post_json"])
        posts_with_order.append((post, str(row["captured_at"]), int(row["rowid"]), str(row["post_key"])))
    posts_with_order.sort(key=lambda item: _post_time_key(item[0], item[1], item[2]))
    ordered_posts = [item[0] for item in posts_with_order]
    comments_by_post: dict[str, list[dict]] = {}
    for row in comment_rows:
        comments_by_post.setdefault(str(row["post_key"]), []).append(json.loads(row["comment_json"]))
    ordered_comments = []
    ordered_post_keys = [item[3] for item in posts_with_order]
    for post_key in ordered_post_keys:
        ordered_comments.extend(comments_by_post.get(post_key, []))

    return {
        "revision": revision,
        "sourceKey": source_key,
        "sourceUrl": str(source_row["source_url"]) if source_row else source_url,
        "canonicalUrl": str(source_row["canonical_url"] or "") if source_row else "",
        "lastScrapedAt": str(source_row["last_scraped_at"] or "") if source_row else "",
        "posts": ordered_posts,
        "comments": ordered_comments,
    }


def load_saved_changes(source_url: str, after_revision: int, *, limit: int = 50) -> dict:
    """Read only committed posts changed since the UI's last snapshot.

    One update row per post bounds storage. Replacing its revision is safe:
    a post updated while a page is read will appear again on the next page.
    """
    if not isinstance(after_revision, int) or not 0 <= after_revision <= 9_223_372_036_854_775_807:
        raise ValueError('Invalid saved-data revision')
    limit = min(100, max(1, limit))
    init_store()
    with _connect() as connection:
        connection.execute('BEGIN')
        source_key = _resolve_source_key(connection, source_url)
        rows = connection.execute(
            'SELECT u.revision, p.post_key, p.post_json FROM post_updates u '
            'JOIN posts p ON p.source_key=u.source_key AND p.post_key=u.post_key '
            'WHERE u.source_key=? AND u.revision>? ORDER BY u.revision LIMIT ?',
            (source_key, after_revision, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        keys = [row['post_key'] for row in rows]
        comments = []
        if keys:
            placeholders = ','.join('?' for _ in keys)
            comments = [json.loads(row[0]) for row in connection.execute(
                f'SELECT comment_json FROM comments WHERE source_key=? AND post_key IN ({placeholders}) ORDER BY rowid',
                (source_key, *keys),
            )]
    return {
        'sourceKey': source_key,
        'revision': rows[-1]['revision'] if rows else after_revision,
        'hasMore': has_more,
        'posts': [json.loads(row['post_json']) for row in rows],
        'comments': comments,
    }
