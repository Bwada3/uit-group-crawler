"""Local Playwright adapter for Facebook group, page, and profile timelines."""

from __future__ import annotations

import re
import time
import io
import logging
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from local_store import normalize_post_url


POST_SELECTOR = 'div[role="article"]'
POST_MESSAGE_SELECTORS = (
    '[data-ad-rendering-role="story_message"]',
    '[data-ad-comet-preview="message"]',
    '[data-ad-preview="message"]',
)
COMMENT_LABEL = re.compile(
    r"^(?:comment|reply)\s+by\b|^(?:bình luận|trả lời)\s+(?:của|bởi)\b",
    re.IGNORECASE,
)
PROFILE_DIR = Path(__file__).resolve().parent / ".playwright-profile"
COMMENT_WORDS = re.compile(r"comment|bình luận|commentaire", re.IGNORECASE)
REACTION_WORDS = re.compile(r"reaction|like|thích|réaction|j'aime", re.IGNORECASE)
REACTION_TOTAL_WORDS = re.compile(r"reactions?|people reacted|cảm xúc|réactions?|likes?|thích", re.IGNORECASE)
SHARE_WORDS = re.compile(r"share|partage|chia sẻ", re.IGNORECASE)
NO_NEW_POST_TIMEOUT_SECONDS = 60 * 60

REACTION_LABELS = {
    "reactionLikeCount": re.compile(r"\blike\b|(?<!yêu )thích", re.IGNORECASE),
    "reactionLoveCount": re.compile(r"love|yêu thích", re.IGNORECASE),
    "reactionCareCount": re.compile(r"care|thương thương", re.IGNORECASE),
    "reactionHahaCount": re.compile(r"haha|cười", re.IGNORECASE),
    "reactionWowCount": re.compile(r"wow|ngạc nhiên", re.IGNORECASE),
    "reactionSadCount": re.compile(r"sad|buồn", re.IGNORECASE),
    "reactionAngryCount": re.compile(r"angry|phẫn nộ", re.IGNORECASE),
}

REACTION_NAME_PATTERN = (
    r"(?:like|love|care|haha|wow|sad|angry|thích|yêu\s+thích|"
    r"thương\s+thương|cười|ngạc nhiên|buồn|phẫn nộ)"
)

REACTION_COUNT_PATTERNS = {
    key: re.compile(
        rf"(?<!\w)(\d[\d,.]*\s*[KMB]?)\s*(?:{label.pattern})\b|"
        rf"(?:{label.pattern})\s*[:·-]?\s*(\d[\d,.]*\s*[KMB]?)(?!\w)",
        re.IGNORECASE,
    )
    for key, label in REACTION_LABELS.items()
}

_OCR_ENGINE = None
_OCR_CACHE: dict[str, str] = {}
_OCR_LOGGER = logging.getLogger(__name__)


def _count(text: str) -> int:
    match = re.search(r"(?<!\w)(\d[\d,.]*\s*[KMB]?)(?!\w)", text, re.IGNORECASE)
    if not match:
        return 0
    return _count_from_match(match.group(1)) or 0


def _count_from_match(value: str) -> int | None:
    normalized = value.replace(" ", "")
    multiplier = 1
    if normalized[-1:].upper() in {"K", "M", "B"}:
        multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[normalized[-1].upper()]
        normalized = normalized[:-1]
    # Facebook may render thousands with either a comma or a period.
    if re.fullmatch(r"\d{1,3}(?:[,.]\d{3})+", normalized):
        normalized = normalized.replace(",", "").replace(".", "")
    else:
        normalized = normalized.replace(",", ".")
    try:
        return int(float(normalized) * multiplier)
    except ValueError:
        return None


def _owned_values(article) -> list[str]:
    try:
        return article.evaluate("""root => {
            const selectors = '[aria-label], [title], [data-tooltip-content], [role="button"], [role="tab"], button, a, span';
            const countLabel = /(?:\\d[\\d,.]*\\s*[KMB]?\\s*(?:likes?|love|care|haha|wow|sad|angry|reactions?|comments?|shares?|thích|yêu thích|thương thương|cười|ngạc nhiên|buồn|phẫn nộ|bình luận|chia sẻ|lượt cảm xúc))|(?:(?:likes?|love|care|haha|wow|sad|angry|reactions?|comments?|shares?|thích|yêu thích|thương thương|cười|ngạc nhiên|buồn|phẫn nộ|bình luận|chia sẻ|lượt cảm xúc)\\s*[:·-]?\\s*\\d)/i;
            const isCommentContent = element => {
                for (let current = element; current && current !== root; current = current.parentElement) {
                    if (current.getAttribute('role') === 'article') return true;
                    const label = current.getAttribute('aria-label') || '';
                    if (/^(?:comment|reply)\\s+by\\b|^(?:bình luận|trả lời)\\s+(?:của|bởi)\\b/i.test(label)) return true;
                }
                return false;
            };
            const isOwned = element => {
                if (root.matches('[role="main"]')) return element.closest('[role="main"]') === root && !isCommentContent(element);
                if (root.matches('[role="article"]')) return element.closest('[role="article"]') === root;
                return root.contains(element) && !isCommentContent(element);
            };
            const values = [];
            for (const element of root.querySelectorAll(selectors)) {
                if (!isOwned(element)) continue;
                for (const name of ['aria-label', 'title', 'data-tooltip-content']) {
                    const value = element.getAttribute(name);
                    if (value) values.push(value.trim());
                }
                const text = (element.innerText || '').replace(/\\s+/g, ' ').trim();
                const reactionSummary = [element.getAttribute('aria-label'), element.getAttribute('title')]
                    .filter(Boolean).join(' ');
                if (element.matches('[role="button"], button') && /^[0-9][0-9,. ]*$/.test(text) &&
                    /who reacted|người đã bày tỏ cảm xúc/i.test(reactionSummary)) values.push(text + ' reactions');
                if (text && text.length <= 160 &&
                    !element.closest('div[dir="auto"]') &&
                    (element.matches('[role="button"], [role="tab"], button, a') || countLabel.test(text))) values.push(text);
            }
            return [...new Set(values)];
        }""")
    except Exception:
        return []


def _article_text(article) -> str:
    """Read only text blocks owned by this post/comment, excluding nested articles."""
    try:
        parts = article.evaluate("""root => Array.from(root.querySelectorAll('div[dir="auto"]'))
            .filter(element => {
                if (root.matches('[role="article"]')) return element.closest('[role="article"]') === root;
                const commentRoot = element.closest('[aria-label^="Comment by" i], [aria-label^="Reply by" i], [aria-label^="Bình luận của" i], [aria-label^="Bình luận bởi" i], [aria-label^="Trả lời của" i], [aria-label^="Trả lời bởi" i]');
                return commentRoot === root && element.closest('[role="article"]') === root.closest('[role="article"]');
            })
            .filter(element => !element.querySelector('div[dir="auto"]'))
            .map(element => (element.innerText || '').trim())
            .filter(Boolean)""")
    except Exception:
        return ""
    unique: list[str] = []
    for part in parts:
        value = str(part).strip()
        if value and value not in unique:
            unique.append(value)
    return "\n".join(unique)


def _post_text(article) -> str:
    """Extract the story message itself, never every text block in its feed card."""
    try:
        value = article.evaluate("""(root, selectors) => {
            const isCommentContent = element => {
                for (let current = element; current && current !== root; current = current.parentElement) {
                    if (current.getAttribute('role') === 'article') return true;
                    const label = current.getAttribute('aria-label') || '';
                    const pagelet = current.getAttribute('data-pagelet') || '';
                    if (/^(?:comment|reply)\\s+by\\b|^(?:bình luận|trả lời)\\s+(?:của|bởi)\\b/i.test(label) ||
                        /comment/i.test(pagelet)) return true;
                }
                return false;
            };
            const belongsToPost = element => {
                const owned = root.matches('[role="main"]')
                    ? element.closest('[role="main"]') === root
                    : root.matches('[role="article"]')
                        ? element.closest('[role="article"]') === root
                        : root.contains(element);
                return owned && !isCommentContent(element);
            };

            for (const selector of selectors) {
                const message = Array.from(root.querySelectorAll(selector))
                    .filter(belongsToPost)
                    .find(element => (element.innerText || '').trim());
                if (message) {
                    const text = (message.innerText || '').trim();
                    if (text) return text;
                }
            }

            if (root.matches('[role="main"]')) return '';

            // Older Facebook layouts may omit the story-message marker. Use only
            // the first post-owned text block as a conservative fallback.
            const firstBlock = Array.from(root.querySelectorAll('div[dir="auto"]'))
                .filter(element => belongsToPost(element))
                .filter(element => !element.querySelector('div[dir="auto"]'))[0];
            return firstBlock ? (firstBlock.innerText || '').trim() : '';
        }""", list(POST_MESSAGE_SELECTORS))
    except Exception:
        return ""
    return str(value or "").strip()


def _comment_author(item) -> str:
    author = _owned_first_text(item, "strong, h3, h4")
    if author:
        return author
    try:
        label = item.get_attribute("aria-label") or ""
    except Exception:
        label = ""
    match = re.match(r"^(?:comment|reply)\s+by\s+(.+)$", label, re.IGNORECASE)
    if not match:
        match = re.match(r"^(?:bình luận|trả lời)\s+(?:của|bởi)\s+(.+)$", label, re.IGNORECASE)
    if not match:
        match = re.match(
            r"^(?:b\u00ecnh lu\u1eadn|tr\u1ea3 l\u1eddi)\s+d\u01b0\u1edbi t\u00ean\s+(.+?)(?:\s+(?:v\u00e0o|l\u00fac)\s+.+)?$",
            label,
            re.IGNORECASE,
        )
    return match.group(1).strip() if match else ""


def _comment_text(item, author: str) -> str:
    text = _article_text(item)
    if not text:
        return ""
    author_key = " ".join(author.split()).casefold()
    parts = [part.strip() for part in text.splitlines() if part.strip()]
    return "\n".join(part for part in parts if " ".join(part.split()).casefold() != author_key)


def _valid_media_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and (
            hostname == "fbcdn.net"
            or hostname.endswith(".fbcdn.net")
            or hostname == "fbsbx.com"
            or hostname.endswith(".fbsbx.com")
        )
    except ValueError:
        return False


def _post_media(article) -> list[dict]:
    try:
        candidates = article.evaluate("""root => {
            const isCommentContent = element => {
                for (let current = element; current && current !== root; current = current.parentElement) {
                    if (current.getAttribute('role') === 'article') return true;
                    const label = current.getAttribute('aria-label') || '';
                    const pagelet = current.getAttribute('data-pagelet') || '';
                    if (/^(?:comment|reply)\\s+by\\b|^(?:bình luận|trả lời)\\s+(?:của|bởi)\\b/i.test(label) ||
                        /comment/i.test(pagelet)) return true;
                }
                return false;
            };
            const owned = element => {
                const inRoot = root.matches('[role="main"]')
                    ? element.closest('[role="main"]') === root
                    : root.matches('[role="article"]')
                        ? element.closest('[role="article"]') === root
                        : root.contains(element);
                return inRoot && !isCommentContent(element);
            };
            const media = [];
            for (const image of root.querySelectorAll('img')) {
                if (!owned(image)) continue;
                const rect = image.getBoundingClientRect();
                const width = Math.max(image.naturalWidth || 0, rect.width || 0);
                const height = Math.max(image.naturalHeight || 0, rect.height || 0);
                if (width < 160 || height < 100) continue;
                const url = image.currentSrc || image.src || '';
                if (url.startsWith('https://')) media.push({ type: 'image', url, alt: image.alt || '' });
            }
            for (const element of root.querySelectorAll('[style*="background"]')) {
                if (!owned(element)) continue;
                const rect = element.getBoundingClientRect();
                if (rect.width < 160 || rect.height < 100) continue;
                const background = getComputedStyle(element).backgroundImage || '';
                for (const match of background.matchAll(/url\\((['"]?)(.*?)\\1\\)/g)) {
                    const url = match[2] || '';
                    if (url.startsWith('https://')) {
                        const visualText = (element.innerText || '').trim();
                        media.push({
                            type: 'image',
                            url,
                            alt: element.getAttribute('aria-label') || '',
                            visualText: visualText.length <= 1_500 ? visualText : '',
                        });
                    }
                }
            }
            for (const video of root.querySelectorAll('video')) {
                if (!owned(video)) continue;
                const rect = video.getBoundingClientRect();
                if (Math.max(rect.width || 0, video.videoWidth || 0) < 160 ||
                    Math.max(rect.height || 0, video.videoHeight || 0) < 100) continue;
                const url = video.currentSrc || video.src || video.poster || '';
                if (url.startsWith('https://')) media.push({ type: 'video', url, alt: '' });
            }
            const unique = new Map();
            for (const item of media) if (!unique.has(item.url)) unique.set(item.url, item);
            return Array.from(unique.values());
        }""")
    except Exception:
        return []
    return [item for item in candidates if _valid_media_url(str(item.get("url") or ""))]


def _ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr import LangDet, LangRec, ModelType, OCRVersion, RapidOCR

        _OCR_ENGINE = RapidOCR(params={
            "Det.ocr_version": OCRVersion.PPOCRV6,
            # PP-OCRv6 uses one shared multilingual detector; selecting its
            # English route keeps the enum valid and resolves that same model.
            "Det.lang_type": LangDet.EN,
            "Det.model_type": ModelType.SMALL,
            "Rec.ocr_version": OCRVersion.PPOCRV6,
            # PP-OCRv6 routes English through the same multilingual model,
            # whose supported language list includes Vietnamese. RapidOCR's
            # LangRec enum does not yet expose a Vietnamese enum member.
            "Rec.lang_type": LangRec.EN,
            "Rec.model_type": ModelType.SMALL,
            "EngineConfig.onnxruntime.use_cuda": False,
            "EngineConfig.onnxruntime.use_dml": False,
        })
        _OCR_LOGGER.info("RapidOCR selected CPUExecutionProvider")
    return _OCR_ENGINE


def _ocr_image(image_bytes: bytes) -> str:
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
        image.thumbnail((3_000, 3_000))
        image_array = np.asarray(image)
        engine = _ocr_engine()
        result = engine(image_array)
    lines = getattr(result, "txts", None) or []
    return "\n".join(str(line).strip() for line in lines if str(line).strip())


def _ocr_post_media(media: list[dict], page) -> tuple[list[dict], str, str]:
    image_media = [item for item in media if item["type"] == "image"]
    if not image_media:
        for item in media:
            item["ocrText"] = ""
            item["ocrStatus"] = "not_applicable"
        return media, "", "no_image"

    for item in image_media:
        item["ocrText"] = ""
        visual_text = str(item.pop("visualText", "") or "").strip()
        raw_ocr_text = _OCR_CACHE.get(item["url"])
        if raw_ocr_text is None:
            try:
                response = page.context.request.get(
                    item["url"], timeout=8_000, headers={"Referer": page.url}
                )
                content_type = response.headers.get("content-type", "").lower()
                image_bytes = response.body() if response.ok and content_type.startswith("image/") else b""
                if not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
                    item["ocrStatus"] = "skipped"
                    item["ocrSource"] = "none"
                    continue
                raw_ocr_text = _ocr_image(image_bytes)
                _OCR_CACHE[item["url"]] = raw_ocr_text
            except Exception:
                item["ocrStatus"] = "unavailable"
                item["ocrSource"] = "none"
                continue

        if raw_ocr_text:
            item["ocrText"] = raw_ocr_text
            item["ocrSource"] = "ocr"
        elif visual_text:
            # Facebook renders text-background posts as HTML over a plain
            # image. Preserve that visible text when the downloaded background
            # itself contains no glyphs for OCR to detect.
            item["ocrText"] = visual_text
            item["ocrSource"] = "facebook_text_layer"
        else:
            item["ocrSource"] = "ocr"
        item["ocrStatus"] = "complete"

    for item in media:
        if item["type"] != "image":
            item["ocrText"] = ""
            item["ocrStatus"] = "not_applicable"
            item["ocrSource"] = "none"
    distinct_texts: list[str] = []
    seen_texts: set[str] = set()
    for item in image_media:
        value = str(item.get("ocrText") or "").strip()
        key = " ".join(value.split()).casefold()
        if value and key not in seen_texts:
            seen_texts.add(key)
            distinct_texts.append(value)
    text = "\n\n".join(distinct_texts)
    statuses = {item["ocrStatus"] for item in image_media}
    status = "complete" if statuses == {"complete"} else "partial"
    return media, text, status


def _reaction_counts_from_values(values: list[str]) -> dict[str, int | None]:
    counts: dict[str, int | None] = {key: None for key in REACTION_LABELS}
    for value in values:
        for key, pattern in REACTION_COUNT_PATTERNS.items():
            match = pattern.search(value)
            if not match:
                label = REACTION_LABELS[key]
                match = re.search(
                    rf"(\d[\d,.]*\s*[KMB]?)\s*(?:people|người)\s*"
                    rf"(?:who\s+)?(?:reacted|đã\s+bày\s+tỏ\s+cảm\s+xúc)\s*"
                    rf"(?:to\s+|for\s+)?(?:{label.pattern})(?!\w)",
                    value,
                    re.IGNORECASE,
                )
                if not match:
                    continue
                token = match.group(1)
            else:
                token = match.group(1) or match.group(2)
            count = _count_from_match(token)
            if count is not None:
                counts[key] = max(counts[key] or 0, count)
    return counts


def _reaction_all_total(values: list[str]) -> int | None:
    all_tab = re.compile(
        r"(\d[\d,.]*\s*[KMB]?)\s*(?:people|người)\s*"
        r"(?:who\s+)?(?:reacted|đã\s+bày\s+tỏ\s+cảm\s+xúc)\s*"
        r"(?:to\s+)?(?:all|tất cả)\b",
        re.IGNORECASE,
    )
    for value in values:
        match = all_tab.search(value)
        if not match:
            match = re.fullmatch(r"(?:all|tất cả)\s*[:·-]?\s*(\d[\d,.]*\s*[KMB]?)", value.strip(), re.I)
        if match:
            return _count_from_match(match.group(1))
    return None


def _complete_reaction_counts(values: list[str], *, summary: bool = False) -> tuple[dict[str, int | None], bool]:
    """Infer absent types only when exact counts reconcile with an explicit total."""
    counts = _reaction_counts_from_values(values)
    total = _reaction_all_total(values)
    if summary and total is None:
        for value in values:
            match = re.fullmatch(
                r"(?:reactions?|cảm xúc)\s*[:·-]?\s*(\d[\d,.]*\s*[KMB]?)|"
                r"(\d[\d,.]*\s*[KMB]?)\s*(?:reactions?|people reacted|cảm xúc)",
                value.strip(), re.I,
            )
            if match:
                total = _count_from_match(match.group(1) or match.group(2))
                break
    # Rounded totals such as 1K cannot prove that omitted types are zero.
    relevant = [value for value in values if any(pattern.search(value) for pattern in REACTION_LABELS.values())
                or re.search(r"all|tất cả|reactions?|cảm xúc", value, re.I)]
    rounded = any(re.search(r"\d[\d,.]*\s*[KMB]\b", value, re.I) for value in relevant)
    if total is not None and not rounded:
        if sum(value or 0 for value in counts.values()) == total:
            return {key: value or 0 for key, value in counts.items()}, True
        return counts, False
    return counts, all(value is not None for value in counts.values())


def _visible_label_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(character for character in normalized if unicodedata.category(character) not in {"Cc", "Cf", "Cs"})


def _reaction_dialog_values(dialog) -> list[str]:
    try:
        if not dialog.count() or not dialog.is_visible():
            return []
        return dialog.evaluate("""root => {
            const values = [];
            for (const element of root.querySelectorAll('[role="tab"], [role="tab"] [aria-label], [role="tab"] [title]')) {
                for (const name of ['aria-label', 'title']) {
                    const value = element.getAttribute(name);
                    if (value) values.push(value.trim());
                }
                const text = (element.innerText || '').replace(/\\s+/g, ' ').trim();
                if (text && text.length <= 120) values.push(text);
            }
            return [...new Set(values)];
        }""")
    except Exception:
        return []


class _PostNavigationError(RuntimeError):
    pass


def _assert_post_location(page, expected_url: str) -> None:
    expected = _canonical_group_permalink(expected_url)
    if expected and _canonical_group_permalink(page.url) != expected:
        raise _PostNavigationError(
            "Tab permalink đã rời bài đăng đang đọc. Đã dừng để tránh lấy nhầm dữ liệu; "
            "các bài đã lưu trong kho vẫn còn."
        )


def _post_actions(root, selector: str, *, owned_only: bool = False) -> list[dict]:
    """Snapshot node identity, not a position that React can change before a click."""
    return root.evaluate(r"""(root, args) => {
        const actions = [];
        let serial = 0;
        for (const element of root.querySelectorAll(args.selector)) {
            if (!element.isConnected || !element.getClientRects().length) continue;
            const anchor = element.closest('a[href]');
            if (anchor) {
                const raw = (anchor.getAttribute('href') || '').trim();
                if (raw && raw !== '#' && !/^javascript:void\(0\);?$/i.test(raw)) {
                    try {
                        const url = new URL(raw, location.href);
                        if (url.origin !== location.origin || url.pathname !== location.pathname ||
                            url.search !== location.search) continue;
                    } catch (_) { continue; }
                }
            }
            if (args.ownedOnly) {
                let nestedComment = false;
                for (let parent = element; parent && parent !== root; parent = parent.parentElement) {
                    const label = parent.getAttribute('aria-label') || '';
                    if (parent.getAttribute('role') === 'article' ||
                        /^(?:comment|reply)\s+by\b|^(?:b\u00ecnh lu\u1eadn|tr\u1ea3 l\u1eddi)\s+(?:c\u1ee7a|b\u1edfi)\b/i.test(label)) {
                        nestedComment = true;
                        break;
                    }
                }
                if (nestedComment) continue;
            }
            const labels = [element.getAttribute('aria-label') || '',
                element.getAttribute('title') || '', element.innerText || '']
                .map(value => value.replace(/\s+/g, ' ').trim());
            // Large wrapper buttons are not comment-expansion controls.
            if (!labels.some(label => label && label.length <= 240)) continue;
            let token = element.getAttribute('data-grouppulse-post-action');
            if (!token) {
                token = args.prefix + '-' + serial++;
                element.setAttribute('data-grouppulse-post-action', token);
            }
            actions.push({token, labels,
                firstLine: (element.innerText || '').trim().split(/\r?\n/)[0].trim()});
        }
        return actions;
    }""", {"selector": selector, "ownedOnly": owned_only, "prefix": str(time.time_ns())})


def _click_post_action(root, action: dict, page, wait_ms: int = 500) -> bool:
    expected_url = page.url
    clicked = root.evaluate(r"""(root, action) => {
        const element = Array.from(root.querySelectorAll('[data-grouppulse-post-action]'))
            .find(node => node.getAttribute('data-grouppulse-post-action') === action.token);
        if (!element || !element.isConnected || !element.getClientRects().length) return false;
        const labels = [element.getAttribute('aria-label') || '',
            element.getAttribute('title') || '', element.innerText || '']
            .map(value => value.replace(/\s+/g, ' ').trim());
        if (JSON.stringify(labels) !== JSON.stringify(action.labels)) return false;
        const anchor = element.closest('a[href]');
        if (anchor) {
            const raw = (anchor.getAttribute('href') || '').trim();
            if (raw && raw !== '#' && !/^javascript:void\(0\);?$/i.test(raw)) {
                try {
                    const url = new URL(raw, location.href);
                    if (url.origin !== location.origin || url.pathname !== location.pathname ||
                        url.search !== location.search) return false;
                } catch (_) { return false; }
            }
        }
        // Atomic identity/label/href check and activation; no stale pixel coordinates.
        element.click();
        return true;
    }""", action)
    if clicked:
        page.wait_for_timeout(wait_ms)
    _assert_post_location(page, expected_url)
    return bool(clicked)


def _close_post_child_dialog(dialog, page) -> bool:
    """Close only this child dialog; Escape could dismiss the underlying post."""
    if not dialog.count() or not dialog.is_visible():
        return False
    expected_url = page.url
    reaction_token = dialog.get_attribute("data-grouppulse-reaction-dialog")
    clicked = dialog.evaluate(r"""dialog => {
        if (!dialog.isConnected || !dialog.getClientRects().length) return false;
        const fold = value => (value || '').normalize('NFD')
            .replace(/[\u0300-\u036f]/g, '').replace(/[\u0110\u0111]/g, 'd').toLowerCase().trim();
        const closeLabels = new Set(['close', 'dismiss', 'close dialog', 'dong', 'dong hop thoai', 'dong cua so']);
        // Facebook sometimes places the close header beside role=dialog rather
        // than inside it. Climb only within the child's own portal, stopping
        // before any ancestor that contains the original post dialog.
        for (let root = dialog, depth = 0; root && depth < 7; root = root.parentElement, depth++) {
            if (root.matches('body, html, [data-grouppulse-existing-dialog]') ||
                root.querySelector('[data-grouppulse-existing-dialog]')) break;
            for (const element of root.querySelectorAll('button, [role="button"]')) {
                if (!element.isConnected || !element.getClientRects().length || element.closest('a[href]')) continue;
                const labels = [element.getAttribute('aria-label'), element.getAttribute('title'), element.innerText];
                if (!labels.some(label => closeLabels.has(fold(label)))) continue;
                element.click();
                return true;
            }
        }
        return false;
    }""")
    if clicked:
        page.wait_for_function(
            "token => { const dialog = document.querySelector('[data-grouppulse-reaction-dialog=\"' + token + '\"]'); "
            "return !dialog || !dialog.getClientRects().length; }",
            arg=reaction_token, timeout=1_500, polling=50,
        )
    _assert_post_location(page, expected_url)
    return bool(clicked)


def _open_reaction_dialog(article, page) -> list[str]:
    safe_summary = re.compile(
        r"(?:see|view|show|hiển thị).{0,40}(?:who reacted|reactions?|người đã bày tỏ cảm xúc)|"
        r"^\s*\d[\d,.]*\s*[KMB]?\s+(?:reactions?|people reacted)\s*$|"
        rf"^\s*{REACTION_NAME_PATTERN}\s*[:·-]?\s*\d[\d,.]*\s*[KMB]?\s*(?:people|người)\s*$",
        re.IGNORECASE,
    )
    for action in _post_actions(article, '[role="button"], button, a', owned_only=True):
        if not any(safe_summary.search(_visible_label_text(label).strip())
                   for label in action["labels"] if label.strip() and len(label) <= 240):
            continue
        token = str(time.time_ns())
        expected_url = page.url
        dialog = None
        try:
            page.locator('[role="dialog"]:visible').evaluate_all(
                "(nodes, token) => nodes.forEach(node => node.setAttribute('data-grouppulse-existing-dialog', token))",
                token,
            )
            if not _click_post_action(article, action, page, wait_ms=0):
                continue
            page.wait_for_function(
                r"""token => {
                    const fold = value => (value || '').normalize('NFD')
                        .replace(/[\u0300-\u036f]/g, '').replace(/\u0111/g, 'd').toLowerCase();
                    const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'))
                        .filter(node => node.getClientRects().length &&
                            node.getAttribute('data-grouppulse-existing-dialog') !== token &&
                            !node.querySelector('[data-grouppulse-existing-dialog="' + token + '"]') &&
                            !node.querySelector('[data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"], [data-ad-preview="message"]'));
                    const matches = dialogs.filter(node => {
                        const labels = Array.from(node.querySelectorAll('[role="tab"]')).map(tab =>
                            fold((tab.getAttribute('aria-label') || '') + ' ' + (tab.innerText || '')));
                        return labels.some(label => /all|tat ca/.test(label)) &&
                            labels.some(label => /like|love|care|haha|wow|sad|angry|thich|thuong thuong|buon|phan no/.test(label));
                    });
                    const dialog = matches[matches.length - 1];
                    if (!dialog) return false;
                    dialog.setAttribute('data-grouppulse-reaction-dialog', token);
                    return true;
                }""", arg=token, timeout=6_000, polling=50,
            )
            # Pin the loaded reaction content, not its temporary spinner dialog:
            # Facebook replaces that spinner node while fetching reactions.
            dialog = page.locator(f'[data-grouppulse-reaction-dialog="{token}"]')
            return _reaction_dialog_values(dialog)
        except _PostNavigationError:
            raise
        except Exception:
            logging.getLogger(__name__).debug("Could not read the reactions overlay", exc_info=True)
        finally:
            if dialog is not None:
                _close_post_child_dialog(dialog, page)
            for attribute in ('data-grouppulse-existing-dialog', 'data-grouppulse-reaction-dialog'):
                page.locator(f'[{attribute}="{token}"]').evaluate_all(
                    "(nodes, attribute) => nodes.forEach(node => node.removeAttribute(attribute))", attribute,
                )
            _assert_post_location(page, expected_url)
    return []


def _reaction_breakdown(article, page, values=None) -> tuple[dict[str, int | None], bool]:
    # Read only the post's existing summary. Never open the reaction overlay.
    return _complete_reaction_counts(_owned_values(article) if values is None else values, summary=True)


def _first_text(locator) -> str:
    try:
        return " ".join((locator.first.inner_text(timeout=1_500) or "").split())
    except Exception:
        return ""


def _owned_first_text(article, selector: str) -> str:
    try:
        values = article.evaluate("""(root, selector) => Array.from(root.querySelectorAll(selector))
            .filter(element => {
                if (root.matches('[role="main"]') || !root.matches('[role="article"]')) {
                    if (root.matches('[role="main"]') && element.closest('[role="main"]') !== root) return false;
                    if (!root.matches('[role="main"]') && !root.contains(element)) return false;
                    for (let current = element; current && current !== root; current = current.parentElement) {
                        if (current.getAttribute('role') === 'article') return false;
                        const label = current.getAttribute('aria-label') || '';
                        if (/^(?:comment|reply)\\s+by\\b|^(?:bình luận|trả lời)\\s+(?:của|bởi)\\b/i.test(label)) return false;
                    }
                    return true;
                }
                return element.closest('[role="article"]') === root;
            })
            .map(element => (element.innerText || '').trim())
            .filter(Boolean)""", selector)
        return values[0] if values else ""
    except Exception:
        return ""


def _group_key_from_url(value: str) -> str:
    try:
        match = re.match(r"^/groups/([^/]+)(?:/|$)", urlsplit(value).path, re.IGNORECASE)
        return unquote(match.group(1)).casefold() if match else ""
    except Exception:
        return ""


def _source_group_keys(page, source_url: str) -> set[str]:
    keys = {key for key in (_group_key_from_url(source_url), _group_key_from_url(page.url)) if key}
    try:
        discovered = page.evaluate("""() => {
            const norm = value => (value || '').replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
            const headings = Array.from(document.querySelectorAll('[role="main"] h1, h1'))
                .map(item => norm(item.innerText)).filter(Boolean);
            const pageTitle = norm(document.title.split('|')[0]);
            if (pageTitle && pageTitle !== 'facebook') headings.push(pageTitle);
            const canonicalKeys = Array.from(document.querySelectorAll(
                'link[rel="canonical"], meta[property="og:url"]'
            )).flatMap(element => {
                try {
                    const url = new URL(element.getAttribute('href') || element.getAttribute('content'), location.href);
                    const match = url.pathname.match(/^\\/groups\\/([^/]+)\\/?$/i);
                    return match ? [decodeURIComponent(match[1]).toLocaleLowerCase()] : [];
                } catch { return []; }
            });
            const anchorKeys = Array.from(document.querySelectorAll('a[href]')).flatMap(anchor => {
                let url;
                try { url = new URL(anchor.href, location.href); } catch { return []; }
                const match = url.pathname.match(/^\\/groups\\/([^/]+)\\/?$/i);
                if (!match) return [];
                const label = norm(anchor.innerText || anchor.getAttribute('aria-label'));
                const matchesHeading = headings.some(heading =>
                    anchor.querySelector('h1') || label === heading || label.startsWith(heading + ' ·'));
                if (anchor.closest('[role="article"]') && !matchesHeading) return [];
                const containsHeading = matchesHeading;
                if (!containsHeading && anchor.getAttribute('aria-current') !== 'page') return [];
                return [decodeURIComponent(match[1]).toLocaleLowerCase()];
            });
            return [...canonicalKeys, ...anchorKeys];
        }""")
        keys.update(str(key) for key in discovered if key)
    except Exception:
        pass
    return keys


def _canonical_group_permalink(value: str, expected_group_keys: set[str] | None = None) -> str:
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not (
            hostname == "facebook.com" or hostname.endswith(".facebook.com")
        ):
            return ""
        group_path = re.match(r"(/groups/[^/]+)(?:/|$)", parsed.path, re.IGNORECASE)
        if not group_path:
            return ""
        group_key = unquote(group_path.group(1).rsplit("/", 1)[-1]).casefold()
        if expected_group_keys is not None and group_key not in expected_group_keys:
            return ""
        path_post = re.search(r"/(?:posts|permalink)/(\d+)(?:/|$)", parsed.path, re.IGNORECASE)
        query = parse_qs(parsed.query)
        query_post = next((
            match
            for key in ("multi_permalinks", "story_fbid")
            if (values := query.get(key))
            if (match := re.match(r"(\d+)", values[0]))
        ), None)
        post_id = path_post.group(1) if path_post else (query_post.group(1) if query_post else "")
        return f"https://www.facebook.com{group_path.group(1)}/permalink/{post_id}/" if post_id else ""
    except (TypeError, ValueError):
        return ""


def _post_url_from_hrefs(links: list[str], expected_group_keys: set[str] | None = None) -> str:
    for href in links:
        permalink = _canonical_group_permalink(href, expected_group_keys)
        if permalink:
            parsed = urlsplit(href)
            if re.search(r"/(?:posts|permalink)/\d+", parsed.path, re.IGNORECASE):
                # Keep Facebook's comment_id query for navigation, then save
                # the canonical permalink after the post page loads.
                return href
            return permalink

        if expected_group_keys is not None:
            # Links in a post do not prove that another group is this source.
            continue
        return href
    return ""


def _post_url_from_comment_hrefs(links: list[str], expected_group_keys: set[str] | None) -> str:
    """Read the parent post ID from comment URLs; never interpret comments as posts."""
    if expected_group_keys is None:
        return ""
    parents: dict[str, str] = {}
    for href in links:
        if not parse_qs(urlsplit(href).query).get("comment_id"):
            continue
        parent_url = _canonical_group_permalink(href, expected_group_keys)
        if parent_url:
            post_id = parent_url.rstrip("/").rsplit("/", 1)[-1]
            parents[post_id] = parent_url
    # Two different parent IDs in one card are ambiguous; do not guess.
    return next(iter(parents.values())) if len(parents) == 1 else ""


def _post_url(article, expected_group_keys: set[str] | None = None) -> str:
    try:
        links = article.evaluate("""root => Array.from(root.querySelectorAll('a[href]'))
            .filter(link => {
                const owned = root.matches('[role="main"]')
                    ? link.closest('[role="main"]') === root
                    : root.matches('[role="article"]')
                        ? link.closest('[role="article"]') === root
                        : root.contains(link);
                if (!owned) return false;
                for (let current = link; current && current !== root; current = current.parentElement) {
                    if (current.getAttribute('role') === 'article') return false;
                    const label = current.getAttribute('aria-label') || '';
                    if (/^(?:comment|reply)\\s+by\\b|^(?:bình luận|trả lời)\\s+(?:của|bởi)\\b/i.test(label)) return false;
                }
                return true;
            })
            .map(link => {
                const url = new URL(link.href, location.href);
                if (!['facebook.com', 'www.facebook.com', 'm.facebook.com'].includes(url.hostname)) return null;
                const storyLink = /\/(?:posts|permalink)\/\d+/i.test(url.pathname) ||
                    /[?&](?:story_fbid|multi_permalinks)=\d+/i.test(url.search);
                if (!storyLink) return null;
                const label = [link.getAttribute('aria-label'), link.getAttribute('title'), link.innerText]
                    .filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                let score = /\/permalink\/\d+/i.test(url.pathname) ? 60 :
                    /\/posts\/\d+/i.test(url.pathname) ? 50 : 30;
                if (/comment|bình luận/i.test(label)) score += 100;
                if (/[?&]comment_id=/i.test(url.search)) score += 20;
                return {href: url.href, score};
            })
            .filter(Boolean)
            .sort((left, right) => right.score - left.score)
            .map(link => link.href)""", timeout=1_500)
        direct_url = _post_url_from_hrefs(links, expected_group_keys)
        if direct_url:
            return direct_url
        comment_links = article.evaluate("""root => Array.from(root.querySelectorAll('a[href]'))
            .map(anchor => anchor.href)
            .filter(href => {
                try { return new URL(href, location.href).searchParams.has('comment_id'); }
                catch { return false; }
            })""", timeout=1_500)
        return _post_url_from_comment_hrefs(comment_links, expected_group_keys)
    except Exception:
        pass
    return ""


def _wait_permalink_render(page,timeout_ms: int = 500) -> None:
    """Wake on relevant rendering changes; the caller still verifies readiness."""
    page.evaluate("""timeout => {
        const signature=()=>{
            const selector='[data-ad-rendering-role="story_message"],[data-ad-comet-preview="message"],[data-ad-preview="message"]';
            const stories=Array.from(document.querySelectorAll(selector));
            const shell=document.querySelector('[role="dialog"]')||document.querySelector('[role="main"]')||document.body;
            return JSON.stringify([stories.length,stories.map(node=>node.textContent.length),
                shell?.querySelectorAll('button,[role="button"],img,video,textarea,[role="textbox"]').length,
                shell ? Array.from(shell.querySelectorAll('[role="progressbar"],[data-visualcompletion="loading-state"],[aria-busy="true"]')).filter(node=>node.getClientRects().length).length : 0]);
        };
        const initial=signature();
        return new Promise(resolve=>{
            let done=false;
            const finish=()=>{if(done)return;done=true;observer.disconnect();clearTimeout(timer);resolve();};
            const observer=new MutationObserver(()=>{if(signature()!==initial)finish();});
            const timer=setTimeout(finish,Math.max(1,timeout));
            observer.observe(document,{childList:true,subtree:true,characterData:true,attributes:true,
                attributeFilter:['aria-label','aria-busy','style','class']});
        });
    }""",timeout_ms)


def _open_post_permalink(detail_page, post_url: str, *, _readiness_seconds: float = 12, _render_events: bool = False):
    """Open a feed card's comments permalink and return its top-level story container."""
    parsed_url = urlsplit(post_url)
    hostname = (parsed_url.hostname or "").casefold()
    if (
        parsed_url.scheme != "https"
        or not (hostname == "facebook.com" or hostname.endswith(".facebook.com"))
        or not re.search(r"/(?:permalink|posts)/\d+/?$", parsed_url.path, re.IGNORECASE)
    ):
        raise RuntimeError("Không mở permalink vì URL bài đăng không hợp lệ.")

    target_path = parsed_url.path[:180]
    last_article = None
    try:
        # `commit` avoids waiting for Facebook's long-running scripts before we
        # can inspect the navigation and report useful failures.
        detail_page.goto(post_url, wait_until="commit", timeout=20_000)
        readiness_started=time.monotonic()
        deadline=readiness_started+_readiness_seconds
        while time.monotonic()<deadline:
            article = _permalink_container(detail_page)
            if article is not None and (_post_text(article) or _post_media(article)):
                last_article = article
                expected_comments = _visible_count(article, COMMENT_WORDS)
                loaded_comments = _visible_comment_count(article, include_replies=True)
                readiness = article.evaluate(r"""root => {
                    const visible = node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
                    const loading=Array.from(root.querySelectorAll('[role="progressbar"],[data-visualcompletion="loading-state"],[aria-busy="true"]')).some(visible);
                    const fold = text => (text || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
                        .toLowerCase().replace(/\s+/g, ' ').trim();
                    const composer = Array.from(root.querySelectorAll('[role="textbox"], textarea')).filter(visible).some(node=>
                        /comment|binh luan/.test(fold(node.getAttribute('aria-label'))));
                    const ready=composer || Array.from(root.querySelectorAll('button, [role="button"]')).filter(visible).some(node => {
                        const labels = [node.getAttribute('aria-label'), node.innerText].map(fold);
                        // A rendered post comment action is enough to START
                        // expansion; completeness is checked after expansion.
                        return labels.some(label => /^(?:most relevant|phu hop nhat|newest|moi nhat|all comments|tat ca binh luan|comment|comments|binh luan|write a comment|leave a comment|viet binh luan)$/.test(label) ||
                            /^(?:xem|view|see|show) .*(?:binh luan|comments?|phan hoi|replies)/.test(label));
                    });
                    return {ready,loading};
                }""")
                if readiness['ready'] or (expected_comments > 0 and loaded_comments >= expected_comments):
                    return article
                if not readiness['loading'] and expected_comments <= 0 and time.monotonic()-readiness_started>=2.5:
                    return article
            remaining=deadline-time.monotonic()
            if remaining>0:
                wait_ms=max(1,min(500,int(remaining*1000)))
                # Event polling is retained for diagnostic comparisons only.
                # The live sample with fixed polling finished sooner and did
                # not need the extra retries observed in the event sample.
                if _render_events:_wait_permalink_render(detail_page,wait_ms)
                else:detail_page.wait_for_timeout(wait_ms)
        if last_article is not None:
            # Keep a late comment shell retryable instead of recording zero
            # comments as complete while Facebook is still loading it.
            if readiness['loading'] and not readiness['ready']:
                raise RuntimeError('Facebook vẫn đang tải bình luận; giữ bài trong hàng đợi để thử lại.')
            return last_article
    except Exception as error:
        current_url = detail_page.url or "about:blank"
        current_path = urlsplit(current_url).path[:120] if current_url != "about:blank" else current_url
        raise RuntimeError(
            f"Không mở được permalink Facebook {target_path}; Chromium đang ở {current_path}. "
            f"Lỗi điều hướng: {type(error).__name__}. Không lưu nội dung từ timeline thay cho bài này."
        ) from error

    current_url = detail_page.url or "about:blank"
    current_path = urlsplit(current_url).path[:120] if current_url != "about:blank" else current_url
    raise RuntimeError(
        f"Đã thử mở permalink Facebook {target_path}, nhưng Chromium chuyển đến {current_path} "
        "và không thấy khung bài đăng sau 12 giây. Kiểm tra phiên đăng nhập/quyền xem group; "
        "không lưu nội dung từ timeline thay cho bài này."
    )


def _post_card_from_story_message(marker):
    """Find the feed card owning a story-message node and its action row."""
    candidate = marker.locator("xpath=..")
    for _ in range(12):
        try:
            if not candidate.count():
                return None
            is_card = candidate.evaluate("""root => {
                const storySelector = '[data-ad-rendering-role="story_message"], ' +
                    '[data-ad-comet-preview="message"], [data-ad-preview="message"]';
                const fold = value => (value || '').toLocaleLowerCase().normalize('NFD')
                    .replace(/[\\u0300-\\u036f]/g, '').replace(/\\s+/g, ' ').trim();
                const storyNodes = Array.from(root.querySelectorAll(storySelector));
                if (root.matches(storySelector)) storyNodes.unshift(root);
                const ownedStories = storyNodes.filter(node => {
                    const owner = node.closest('[role="article"]');
                    return !owner || (!/^(?:comment|reply)\\s+by\\b|^(?:binh luan|tra loi)\\s+(?:cua|boi)\\b/.test(fold(owner.getAttribute('aria-label'))) &&
                        !owner.parentElement?.closest('[role="article"]'));
                });
                const uniqueStories = new Set(ownedStories.map(node => fold(node.innerText)).filter(Boolean));
                if (uniqueStories.size !== 1) return false;
                if (/^(?:comment|reply)\\s+by\\b|^(?:binh luan|tra loi)\\s+(?:cua|boi)\\b/.test(fold(root.getAttribute('aria-label')))) return false;
                const labels = Array.from(root.querySelectorAll('button, [role="button"]'))
                    .filter(element => element.getClientRects().length)
                    .flatMap(element => [fold(element.getAttribute('aria-label')), fold(element.innerText)]);
                const hasCommentAction = labels.some(label =>
                    /^(?:viet binh luan|binh luan|write a comment|leave a comment|comment|comments|(?:\\d+\\s+)?comments?)(?:\\s+\\d+)?$/.test(label));
                const hasLikeAction = labels.some(label => /^(?:thich|like)$/.test(label));
                const hasShareAction = labels.some(label => /^(?:chia se|share)(?:[,.:].*)?$/.test(label));
                return hasCommentAction && hasLikeAction && hasShareAction;
            }""")
            if is_card:
                return candidate
        except Exception:
            return None
        try:
            candidate = candidate.locator("xpath=..")
        except Exception:
            return None
    return None


def _resolve_feed_permalink(page, article, group_keys: set[str], cancelled) -> str:
    """Read or hydrate a card's story link without clicking or navigating the feed."""
    if cancelled():
        return ""
    direct_url = _post_url(article, group_keys)
    if direct_url:
        return direct_url
    group_keys.update(_source_group_keys(page, page.url))
    try:
        discovered = article.evaluate(r"""root => {
            const fold = value => (value || '').normalize('NFD')
                .replace(/[\u0300-\u036f]/g, '').replace(/\u0111/gi, 'd').toLocaleLowerCase();
            const owned = element => {
                for (let current = element; current && current !== root; current = current.parentElement) {
                    if (current.getAttribute('role') === 'article') return false;
                    const label = fold(current.getAttribute('aria-label'));
                    if (/^(?:comment|reply)\s+by\b|^(?:binh luan|tra loi)\s+(?:cua|boi)\b/i.test(label)) return false;
                }
                return true;
            };
            const candidates = Array.from(root.querySelectorAll('a[href], button, [role="button"]'))
                .filter(owned).map(element => {
                    const label = fold([element.getAttribute('aria-label'), element.getAttribute('title'), element.innerText]
                        .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim());
                    const timestamp = element.querySelector('abbr, time') ||
                        /^(?:\d+\s*(?:giay|phut|gio|ngay|tuan|thang|nam|seconds?|minutes?|hours?|days?|weeks?|months?|years?|[smhdwy])\b)/i.test(label) ||
                        /(?:\bluc\s+\d|\bat\s+\d|\d{1,2}[:/]\d{1,2})/i.test(label);
                    const comment = /^(?:(?:\d+\s+)?comments?|(?:\d+\s+)?binh luan|viet binh luan)(?:\s+\d+)?$/i.test(label);
                    const storyLink = /\/groups\/[^/]+\/(?:posts|permalink)\/\d+/i.test(element.getAttribute('href') || '');
                    return {element, score: timestamp ? 100 : comment ? 80 : storyLink ? 60 : 0};
                }).filter(item => item.score).sort((a, b) => b.score - a.score).slice(0, 5);
            const hrefs = [];
            const addHref = value => {
                if (typeof value !== 'string' || !value) return;
                try { hrefs.push(new URL(value, location.href).href); } catch {}
            };
            const readProps = props => {
                if (!props) return;
                addHref(props.href);
                addHref(props.url);
                if (props.linkProps) {
                    addHref(props.linkProps.href);
                    addHref(props.linkProps.url);
                }
            };
            for (const {element} of candidates) {
                addHref(element.getAttribute('href'));
                addHref(element.getAttribute('data-href'));
                const propKey = Object.keys(element).find(key => key.startsWith('__reactProps$'));
                if (propKey) readProps(element[propKey]);
                const fiberKey = Object.keys(element).find(key => key.startsWith('__reactFiber$'));
                let fiber = fiberKey ? element[fiberKey] : null;
                for (let depth = 0; fiber && depth < 6; depth++, fiber = fiber.return) {
                    readProps(fiber.memoizedProps);
                    if (fiber.stateNode === root) break;
                }
                // Hover can populate Facebook's timestamp href. These events
                // never click the comment button or scroll a distant card into view.
                if (typeof PointerEvent !== 'undefined') {
                    element.dispatchEvent(new PointerEvent('pointerover', {bubbles: true}));
                }
                element.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                element.dispatchEvent(new MouseEvent('mouseenter', {bubbles: false}));
            }
            return {hrefs};
        }""", timeout=1_500)
        direct_url = _post_url_from_hrefs(discovered['hrefs'], group_keys)
        if direct_url:
            return direct_url
        page.wait_for_timeout(120)
        direct_url = _post_url(article, group_keys)
        if direct_url:
            return direct_url
    except Exception:
        logging.getLogger(__name__).debug("Could not hydrate a Facebook card's permalink", exc_info=True)
    return ""


def _click_comment_for_permalink(page, article, group_keys: set[str], cancelled) -> str:
    """Resolve a missing link through the post dialog, then close that dialog in place."""
    if cancelled():
        return ""
    original_url = page.url
    original_path = urlsplit(original_url).path.rstrip("/")
    token = str(time.monotonic_ns())
    original_scroll = page.evaluate("""token => {
        window.__grouppulseFeedDocumentToken = token;
        document.querySelectorAll('[role="dialog"]').forEach(dialog => {
            dialog.setAttribute('data-grouppulse-existing-dialog', token);
        });
        return window.scrollY;
    }""", token)
    context = page.context
    pages_before = set(context.pages)
    captured_navigations: list[str] = []
    # Facebook may use window.open for this control. Capture its destination
    # without creating an ephemeral tab that steals focus and is then closed.
    page.evaluate(r"""({token, groupKeys}) => {
        const original = window.open;
        const capture = {token, urls: [], original, sourceUrl: location.href, history: []};
        const postDestination = value => {
            try {
                const target = new URL(value, location.href);
                const match = target.pathname.match(/^\/groups\/([^/]+)\/(?:posts|permalink)\/(\d+)\/?$/i);
                return target.protocol === 'https:' && /(^|\.)facebook\.com$/i.test(target.hostname) && match &&
                    groupKeys.includes(decodeURIComponent(match[1]).toLocaleLowerCase()) ? target.href : '';
            } catch (_) { return ''; }
        };
        for (const name of ['pushState', 'replaceState']) {
            const originalMethod = history[name];
            const wrapper = function(state, title, url) {
                const destination = postDestination(url);
                if (destination) capture.urls.push(destination);
                // Retain the history entry so closing Facebook's modal can go
                // back normally, while keeping the source address unchanged.
                return originalMethod.call(this, state, title, destination ? capture.sourceUrl : url);
            };
            capture.history.push({name, original: originalMethod, wrapper});
            history[name] = wrapper;
        }
        capture.wrapper = function(url, ...args) {
            const destination = postDestination(url);
            if (destination) capture.urls.push(destination);
            // This scope only resolves the comment control. No popup is
            // needed, including profile links or destinations outside the group.
            return null;
        };
        window.__grouppulsePopupCapture = capture;
        window.open = capture.wrapper;
    }""", {"token": token, "groupKeys": sorted(group_keys)})

    def capture_post_navigation(route) -> None:
        request = route.request
        permalink = _canonical_group_permalink(request.url, group_keys)
        if request.is_navigation_request() and request.frame == page.main_frame and permalink:
            captured_navigations.append(permalink)
            # A 204 response cancels a document navigation while retaining the
            # current group document. Resolve its URL without loading the post
            # in the scrolling timeline or opening another group tab.
            route.fulfill(status=204, body="")
        else:
            route.fallback()

    page.route("**/*", capture_post_navigation)

    def close_dialog_in_place() -> None:
        if not page.evaluate("token => window.__grouppulseFeedDocumentToken === token", token):
            raise RuntimeError(
                "Nút bình luận đã điều hướng toàn trang thay vì mở hộp bài đăng. "
                "Chưa lưu bài này; dữ liệu đã lưu trong kho vẫn còn."
            )
        for _ in range(3):
            dialog_state = page.evaluate(r"""token => {
                const dialogs = Array.from(document.querySelectorAll('[role="dialog"]')).filter(dialog =>
                    dialog.getClientRects().length &&
                    dialog.getAttribute('data-grouppulse-existing-dialog') !== token);
                const dialog = dialogs[dialogs.length - 1];
                if (!dialog) return {exists: false, clicked: false};
                const fold = value => (value || '').normalize('NFD')
                    .replace(/[\u0300-\u036f]/g, '').replace(/\u0111/gi, 'd').toLocaleLowerCase().trim();
                let close=null;
                // Some Facebook layouts render the close header as a sibling
                // of role=dialog. Stay inside that overlay's ownership scope.
                for(let root=dialog,depth=0;root && depth<5;root=root.parentElement,depth++){
                    if(root.matches('body,html') || root.querySelector('[data-grouppulse-existing-dialog="'+token+'"]'))break;
                    close=Array.from(root.querySelectorAll('button,[role="button"]')).find(button=>
                        button.getClientRects().length && /^(?:close|dismiss|close dialog|dong|dong hop thoai|dong cua so)$/.test(
                            fold(button.getAttribute('aria-label') || button.getAttribute('title') || button.innerText)));
                    if(close)break;
                }
                if (close) close.click();
                return {exists: true, clicked: Boolean(close)};
            }""", token)
            if not dialog_state['exists'] and urlsplit(page.url).path.rstrip("/") == original_path:
                break
            if not dialog_state['clicked']:
                page.keyboard.press("Escape")
            try:
                page.wait_for_function("""token=>!Array.from(document.querySelectorAll('[role="dialog"]')).some(dialog=>
                    dialog.getClientRects().length && dialog.getAttribute('data-grouppulse-existing-dialog')!==token)""",arg=token,timeout=1000)
            except PlaywrightTimeoutError:
                pass
        remaining_dialog = page.evaluate("""token => Array.from(document.querySelectorAll('[role="dialog"]'))
            .some(dialog => dialog.getClientRects().length &&
                dialog.getAttribute('data-grouppulse-existing-dialog') !== token)""", token)
        if remaining_dialog or urlsplit(page.url).path.rstrip("/") != original_path:
            raise RuntimeError(
                "Chưa đóng được hộp bài đăng để trở về timeline group. "
                "Đã dừng trước khi cuộn hoặc lưu nhầm bài; dữ liệu trong kho vẫn còn."
            )
        page.evaluate("""({scrollY}) => {
            if (Math.abs(window.scrollY - scrollY) > 1) window.scrollTo(0, scrollY);
        }""", {"scrollY": original_scroll})

    try:
        targets = article.evaluate(r"""(root, token) => {
            const fold = value => (value || '').normalize('NFD')
                .replace(/[\u0300-\u036f]/g, '').replace(/\u0111/gi, 'd').toLocaleLowerCase();
            return Array.from(root.querySelectorAll('button, [role="button"]')).map((element, index) => {
                for (let current = element; current && current !== root; current = current.parentElement) {
                    if (current.getAttribute('role') === 'article') return null;
                    if (/^(?:comment|reply)\s+by\b|^(?:binh luan|tra loi)\s+(?:cua|boi)\b/i.test(fold(current.getAttribute('aria-label')))) return null;
                }
                const label = fold(element.getAttribute('aria-label') || element.getAttribute('title') || element.innerText)
                    .replace(/\s+/g, ' ').trim();
                if (!/^(?:(?:[\d.,]+\s*[kmb]?\s+)?(?:comments?|binh luan)|write a comment|leave a comment|viet binh luan)(?:\s+[\d.,]+\s*[kmb]?)?$/.test(label)) return null;
                const target = `${token}-${index}`;
                element.setAttribute('data-grouppulse-comment-action', target);
                return {target, score: /\d/.test(label) ? 100 : /^(?:comments?|binh luan)$/.test(label) ? 60 : 10};
            }).filter(Boolean).sort((a, b) => b.score - a.score).slice(0, 2).map(item => item.target);
        }""", token, timeout=1_500)
        for target in targets:
            if cancelled():
                return ""
            # A DOM click avoids Playwright scrolling backwards to an older
            # loaded card. The source document is verified before resuming.
            control = article.locator(f'[data-grouppulse-comment-action="{target}"]')
            if not control.count():
                continue
            clicked = control.evaluate(r"""(element, groupKeys) => {
                if (!element.isConnected) return false;
                const fold = value => (value || '').normalize('NFD')
                    .replace(/[\u0300-\u036f]/g, '').replace(/\u0111/gi, 'd').toLocaleLowerCase();
                const label = fold(element.getAttribute('aria-label') || element.getAttribute('title') || element.innerText)
                    .replace(/\s+/g, ' ').trim();
                if (!/^(?:(?:[\d.,]+\s*[kmb]?\s+)?(?:comments?|binh luan)|write a comment|leave a comment|viet binh luan)(?:\s+[\d.,]+\s*[kmb]?)?$/.test(label)) return false;
                const anchor = element.closest('a[href]');
                const rawHref = anchor ? anchor.getAttribute('href') : '';
                if (rawHref && !rawHref.startsWith('#')) {
                    let url;
                    try { url = new URL(rawHref, location.href); } catch { return false; }
                    const match = url.pathname.match(/^\/groups\/([^/]+)\/(?:posts|permalink)\/\d+\/?$/i);
                    if (!match || !groupKeys.includes(decodeURIComponent(match[1]).toLocaleLowerCase())) return false;
                }
                element.click();
                return true;
            }""", sorted(group_keys), timeout=1_500)
            if not clicked:
                continue
            resolved_url = ""
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not cancelled():
                popup_urls = page.evaluate("token => window.__grouppulsePopupCapture?.token === token ? window.__grouppulsePopupCapture.urls : []", token)
                resolved_url = (
                    captured_navigations[-1]
                    if captured_navigations
                    else _canonical_group_permalink(popup_urls[-1], group_keys) if popup_urls
                    else _canonical_group_permalink(page.url, group_keys)
                )
                if not resolved_url:
                    for popup in context.pages:
                        if popup not in pages_before:
                            resolved_url = _canonical_group_permalink(popup.url, group_keys)
                            if resolved_url:
                                break
                if not resolved_url:
                    dialogs = page.locator('[role="dialog"]')
                    for dialog_index in range(dialogs.count() - 1, -1, -1):
                        dialog = dialogs.nth(dialog_index)
                        if dialog.get_attribute('data-grouppulse-existing-dialog') == token:
                            continue
                        resolved_url = _canonical_group_permalink(_post_url(dialog, group_keys), group_keys)
                        if resolved_url:
                            break
                if not resolved_url:
                    resolved_url = _canonical_group_permalink(_post_url(article, group_keys), group_keys)
                if resolved_url:
                    # Give an asynchronously mounted dialog a chance to show
                    # its close control before restoring the source route.
                    page.wait_for_timeout(150)
                    break
                page.wait_for_timeout(150)
            close_dialog_in_place()
            if resolved_url:
                return resolved_url
        return ""
    finally:
        page.evaluate("""token => {
            const capture = window.__grouppulsePopupCapture;
            if (capture?.token !== token) return;
            if (window.open === capture.wrapper) window.open = capture.original;
            for (const method of capture.history) {
                if (history[method.name] === method.wrapper) history[method.name] = method.original;
            }
            delete window.__grouppulsePopupCapture;
        }""", token)
        page.unroute("**/*", capture_post_navigation)
        for popup in context.pages:
            if popup not in pages_before:
                try:
                    popup.close()
                except Exception:
                    pass
        close_dialog_in_place()
        page.evaluate("""token => {
            document.querySelectorAll('[data-grouppulse-existing-dialog]').forEach(dialog => {
                if (dialog.getAttribute('data-grouppulse-existing-dialog') === token) {
                    dialog.removeAttribute('data-grouppulse-existing-dialog');
                }
            });
            document.querySelectorAll('[data-grouppulse-comment-action]').forEach(element => {
                if ((element.getAttribute('data-grouppulse-comment-action') || '').startsWith(token + '-')) {
                    element.removeAttribute('data-grouppulse-comment-action');
                }
            });
        }""", token)


def _install_group_navigation_guard(context, timeline_page, group_keys: set[str], progress) -> None:
    """Keep scraper documents inside the source group; leave iframe/data requests alone."""
    from local_assets import install_asset_cache
    install_asset_cache(context)
    if not getattr(context,'_grouppulse_network_tracked',False):
        context._grouppulse_network_tracked=True
        requests={}
        def comment_request(request):
            if '/api/graphql' not in request.url:
                return
            try:
                operation=parse_qs(request.post_data or '').get('fb_api_req_friendly_name',[''])[0].casefold()
                if not any(word in operation for word in ('comment','repl','ufi')):
                    return
                target=request.frame.page
                state=getattr(target,'_grouppulse_comment_network',None)
                if state is None:
                    state={'pending':set(),'last':time.monotonic()}
                    target._grouppulse_comment_network=state
                state['pending'].add(request)
                requests[request]=state
            except Exception:
                pass
        def comment_finished(request):
            state=requests.pop(request,None)
            if state is not None:
                state['pending'].discard(request)
                state['last']=time.monotonic()
        context.on('request',comment_request)
        context.on('requestfinished',comment_finished)
        context.on('requestfailed',comment_finished)
    def guard(route) -> None:
        request = route.request
        if not request.is_navigation_request():
            route.fallback()
            return
        try:
            frame = request.frame
            if frame.parent_frame is not None:
                route.fallback()
                return
            destination_page = frame.page
        except Exception:
            destination_page = None
        parsed = urlsplit(request.url)
        hostname = (parsed.hostname or "").casefold()
        group_key = _group_key_from_url(request.url)
        official_host = parsed.scheme == "https" and (
            hostname == "facebook.com" or hostname.endswith(".facebook.com")
        )
        root_group = bool(re.fullmatch(r"/groups/[^/]+/?", parsed.path, re.IGNORECASE))
        post_url = _canonical_group_permalink(request.url, group_keys)
        allowed = official_host and group_key in group_keys and (
            root_group or (bool(post_url) and destination_page is not timeline_page)
        )
        if allowed:
            route.fallback()
        else:
            route.fulfill(status=204, body="")
            progress("running", 15, "Đã chặn link ra ngoài group hoặc trang cá nhân; tiếp tục đọc bài…")

    context.route("**/*", guard)


def _assert_group_timeline(page, group_keys: set[str]) -> None:
    parsed = urlsplit(page.url)
    if (
        _group_key_from_url(page.url) not in group_keys
        or not re.fullmatch(r"/groups/[^/]+/?", parsed.path, re.IGNORECASE)
    ):
        raise RuntimeError(
            "Tab timeline đã rời group nguồn. Scraper dừng trước khi đọc nhầm trang cá nhân; "
            "dữ liệu đã lưu trong kho vẫn còn."
        )


def _permalink_container(page):
    path = urlsplit(page.url).path
    if not re.search(r"/(?:permalink|posts)/\d+", path, re.IGNORECASE) and not _canonical_group_permalink(page.url):
        return None

    # Inspect one DOM snapshot instead of dozens of synchronous driver calls.
    # Return a locator token so the extraction API and its retry behavior stay
    # unchanged. Never bind a background story to the requested permalink.
    token = page.evaluate(r"""() => {
      const expected=location.pathname.match(/\/(?:permalink|posts)\/(\d+)/)?.[1];
      const selector='[data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"], [data-ad-preview="message"]';
      const visible=node=>node.getClientRects().length && getComputedStyle(node).visibility!=='hidden' && !node.closest('[aria-hidden="true"],[inert]');
      const identity=root=>{
        const ids=new Set();
        for(const link of root.querySelectorAll('a[href]')){
          const url=new URL(link.href,location.href);
          if(!/(^|\.)facebook\.com$/.test(url.hostname))continue;
          const match=url.pathname.match(/^\/groups\/[^/]+\/(?:permalink|posts)\/(\d+)/);
          if(!match)continue;
          // Links in comments and embedded shared stories identify a different
          // object; only the owning story's own links establish its identity.
          let nested=false;
          for(let node=link.parentElement;node && node!==root;node=node.parentElement){
            if(node.matches('[role="article"]') && !node.querySelector(selector)){nested=true;break;}
          }
          if(!nested)ids.add(match[1]);
        }
        return !expected || !ids.size || ids.has(expected);
      };
      const unique_story = root => {
        const nodes = Array.from(root.querySelectorAll(
            '[data-ad-rendering-role="story_message"], ' +
            '[data-ad-comet-preview="message"], [data-ad-preview="message"]'
        ));
        if (root.matches('[data-ad-rendering-role="story_message"], ' +
            '[data-ad-comet-preview="message"], [data-ad-preview="message"]')) nodes.unshift(root);
        const stories = nodes.filter(node => !root.contains(node.parentElement?.closest(selector)));
        if (stories.length) return stories.length === 1;
        if (!root.matches('[role="dialog"], [role="article"], [role="main"]')) return false;
        return Array.from(root.querySelectorAll('img,video')).some(node => {
            for (let parent=node; parent && parent!==root; parent=parent.parentElement)
                if (parent.getAttribute('role')==='article' || /^(?:comment|reply) /i.test(parent.getAttribute('aria-label') || '')) return false;
            const box=node.getBoundingClientRect();return box.width>=160 && box.height>=100;
        });
      };
      const bind=root=>{
        for(const previous of document.querySelectorAll('[data-grouppulse-permalink-owner]'))previous.removeAttribute('data-grouppulse-permalink-owner');
        root.setAttribute('data-grouppulse-permalink-owner','current');return 'current';
      };
      const dialogs=Array.from(document.querySelectorAll('[role="dialog"]')).filter(visible);
      for(const dialog of dialogs.reverse())if(unique_story(dialog) && identity(dialog))return bind(dialog);
      // A loading modal must not fall through to a story on the backing feed.
      if(dialogs.length)return null;
      let best=null,bestCount=-1;
      const checked=new Set();
      for(const marker of document.querySelectorAll(selector)){
        if(!visible(marker))continue;
        let container=marker;
        for(let step=0;step<12 && (container=container.parentElement);step++){
          if(checked.has(container))continue;checked.add(container);
          if(!visible(container) || !unique_story(container) || !identity(container))continue;
          // Feed cards require a matching URL; the permalink modal above can
          // render its header before Facebook hydrates any links.
          if(container.closest('[role="feed"]') && !Array.from(container.querySelectorAll('a[href]')).some(a=>a.href.match(/\/(?:posts|permalink)\/(\d+)/)?.[1]===expected))continue;
          const count=container.querySelectorAll('div[role="article"]').length;
          if(count>bestCount){best=container;bestCount=count;}
        }
      }
      if(best)return bind(best);
      const main=document.querySelector('[role="main"]');
      return main && visible(main) && unique_story(main) && identity(main) ? bind(main) : null;
    }""")
    return page.locator('[data-grouppulse-permalink-owner="current"]') if token else None


def _post_date(article) -> str:
    try:
        times = article.evaluate("""root => Array.from(root.querySelectorAll('abbr, time'))
            .filter(element => {
                const owned = root.matches('[role="main"]')
                    ? element.closest('[role="main"]') === root
                    : root.matches('[role="article"]')
                        ? element.closest('[role="article"]') === root
                        : root.contains(element);
                if (!owned) return false;
                for (let current = element; current && current !== root; current = current.parentElement) {
                    if (current.getAttribute('role') === 'article') return false;
                    const label = current.getAttribute('aria-label') || '';
                    if (/^(?:comment|reply)\\s+by\\b|^(?:bình luận|trả lời)\\s+(?:của|bởi)\\b/i.test(label)) return false;
                }
                return true;
            })
            .slice(0, 4)
            .map(element => element.getAttribute('datetime') || element.getAttribute('data-utime') || element.getAttribute('title') || '')""")
        for value in times:
            if value:
                if value.isdigit():
                    return datetime.fromtimestamp(int(value)).isoformat()
                return value
    except Exception:
        pass
    return ""


def _date_sort_value(value: str) -> float | None:
    if not value:
        return None
    try:
        if value.isdigit():
            return float(value)
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _owned_numeric_counts(article) -> list[tuple[str, str]]:
    try:
        values = article.evaluate("""root => {
            const storySelector = '[data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"], [data-ad-preview="message"]';
            const isCommentContent = element => {
                for (let current = element; current && current !== root; current = current.parentElement) {
                    const isPostArticle = current.matches(storySelector) || current.querySelector(storySelector);
                    if (current.getAttribute('role') === 'article' && !isPostArticle) return true;
                    const label = current.getAttribute('aria-label') || '';
                    if (/^(?:comment|reply)\\s+by\\b|^(?:b\\u00ecnh lu\\u1eadn|tr\\u1ea3 l\\u1eddi)\\s+(?:c\\u1ee7a|b\\u1edfi)\\b/i.test(label)) return true;
                }
                return false;
            };
            const isOwned = element => {
                if (root.matches('[role="main"]')) return element.closest('[role="main"]') === root && !isCommentContent(element);
                if (root.matches('[role="article"]')) return element.closest('[role="article"]') === root && !isCommentContent(element);
                return root.contains(element) && !isCommentContent(element);
            };
            return Array.from(root.querySelectorAll('button, [role="button"]'))
                .filter(element => isOwned(element))
                .map(element => ({
                    label: [element.getAttribute('aria-label'), element.getAttribute('title')].filter(Boolean).join(' '),
                    text: (element.innerText || '').replace(/\\s+/g, ' ').trim(),
                }))
                .filter(value => value.label && /^\\d[\\d,.]*\\s*[KMB]?$/i.test(value.text));
        }""")
    except Exception:
        return []
    return [
        (str(value.get("label") or ""), str(value.get("text") or ""))
        for value in values
        if isinstance(value, dict)
    ]


def _visible_count(article, words: re.Pattern[str]) -> int:
    try:
        for label, text in _owned_numeric_counts(article):
            if words.search(label):
                count = _count(text)
                if count is not None:
                    return count
        for value in _owned_values(article):
            if words.search(value):
                count = _count(value)
                if count:
                    return count
    except Exception:
        pass
    return 0


def _visible_comments(article, post_text: str, include_replies: bool) -> list[dict]:
    """Read comments in one browser round trip, keeping each reply's own fields."""
    try:
        rows = article.evaluate(r"""root => {
            const story = '[data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"], [data-ad-preview="message"]';
            const commentLabel = /^(?:comment|reply)\s+by\b|^(?:b\u00ecnh lu\u1eadn|tr\u1ea3 l\u1eddi)\s+(?:c\u1ee7a|b\u1edfi|d\u01b0\u1edbi t\u00ean)\b/i;
            const commentSelector = '[aria-label^="Comment by" i], [aria-label^="Reply by" i], ' +
                '[aria-label^="B\u00ecnh lu\u1eadn c\u1ee7a" i], [aria-label^="B\u00ecnh lu\u1eadn b\u1edfi" i], ' +
                '[aria-label^="Tr\u1ea3 l\u1eddi c\u1ee7a" i], [aria-label^="Tr\u1ea3 l\u1eddi b\u1edfi" i]';
            const candidates = Array.from(root.querySelectorAll('div[role="article"], ' + commentSelector))
                .filter(node => commentLabel.test(node.getAttribute('aria-label') || '') || !node.querySelector(story));
            const roots = new Set(candidates);
            return candidates.map(item => {
                const owns = node => {
                    if (item.matches('[role="article"]')) return node.closest('[role="article"]') === item;
                    for (let current = node; current && current !== item; current = current.parentElement) {
                        if (current.matches('[role="article"]') || commentLabel.test(current.getAttribute('aria-label') || '')) return false;
                    }
                    return item.contains(node);
                };
                let depth = 0;
                for (let parent = item.parentElement; parent && parent !== root; parent = parent.parentElement)
                    if (roots.has(parent)) depth++;
                const heading = Array.from(item.querySelectorAll('strong,h3,h4')).find(owns);
                const label = item.getAttribute('aria-label') || '';
                let author = heading ? (heading.innerText || '').trim() : '';
                if (!author) {
                    const match = label.match(/^(?:comment|reply)\s+by\s+(.+)$/i) ||
                        label.match(/^(?:b\u00ecnh lu\u1eadn|tr\u1ea3 l\u1eddi)\s+(?:c\u1ee7a|b\u1edfi)\s+(.+)$/i) ||
                        label.match(/^(?:b\u00ecnh lu\u1eadn|tr\u1ea3 l\u1eddi)\s+d\u01b0\u1edbi t\u00ean\s+(.+?)(?:\s+(?:v\u00e0o|l\u00fac)\s+.+)?$/i);
                    author = match ? match[1].trim() : '';
                }
                const textOwned = node => item.matches('[role="article"]')
                    ? node.closest('[role="article"]') === item
                    : node.closest(commentSelector) === item && node.closest('[role="article"]') === item.closest('[role="article"]');
                const parts = Array.from(item.querySelectorAll('div[dir="auto"]')).filter(textOwned)
                    .filter(node => !node.querySelector('div[dir="auto"]'))
                    .map(node => (node.innerText || '').trim()).filter(Boolean);
                const values = [];
                const numbers = [];
                for (const node of item.querySelectorAll('[aria-label],[title],[data-tooltip-content],[role="button"],button,a,span')) {
                    if (!owns(node)) continue;
                    for (const attr of ['aria-label','title','data-tooltip-content']) {
                        const value = node.getAttribute(attr); if (value) values.push(value.trim());
                    }
                    const text = (node.innerText || '').replace(/\s+/g,' ').trim();
                    if (text && text.length <= 160 && !node.closest('div[dir="auto"]')) values.push(text);
                    if (node.matches('button,[role="button"]') && /^\d[\d,.]*\s*[KMB]?$/i.test(text))
                        numbers.push({label:[node.getAttribute('aria-label'),node.getAttribute('title')].filter(Boolean).join(' '),text});
                }
                const dates = Array.from(item.querySelectorAll('abbr,time')).filter(owns).slice(0,4)
                    .map(node => node.getAttribute('datetime') || node.getAttribute('data-utime') || node.getAttribute('title') || '');
                const links = Array.from(item.querySelectorAll('a[href]')).filter(owns).map(node=>node.href);
                const images = Array.from(item.querySelectorAll('img')).filter(owns).filter(image => {
                    const box = image.getBoundingClientRect();
                    const link = image.closest('a[href]');
                    if (link && /\/profile\.php|\/people\/|\/user\//i.test(link.href)) return false;
                    return (box.width >= 64 && box.height >= 48) || !!image.closest('div[dir="auto"]');
                }).map(image => ({url:image.currentSrc || image.src, alt:image.getAttribute('alt') || ''}));
                return {author,parts:[...new Set(parts)],depth,values:[...new Set(values)],numbers,dates,links,images};
            });
        }""")
    except Exception:
        return []
    comments = []
    for row in rows:
        if row["depth"] and not include_replies:
            continue
        author = row["author"]
        author_key = " ".join(author.split()).casefold()
        text = "\n".join(value for value in row["parts"] if " ".join(value.split()).casefold() != author_key)
        media = [{"type": "image", "url": image["url"], "alt": image["alt"]}
                 for image in row["images"] if _valid_media_url(image["url"])]
        if not text and media:
            text = "\n".join(dict.fromkeys(image["alt"] for image in media if image["alt"]))
        if (not text and not media) or (text and text == post_text):
            continue
        likes = None
        for value in row["numbers"]:
            if REACTION_WORDS.search(value["label"]):
                likes = _count(value["text"])
                break
        if likes is None:
            likes = next((_count(value) for value in row["values"] if REACTION_WORDS.search(value) and _count(value)), 0)
        date = next((value for value in row["dates"] if value), "")
        if date.isdigit():
            try:
                date = datetime.fromtimestamp(int(date)).isoformat()
            except (ValueError, OSError, OverflowError):
                date = ""
        # Reply permalinks carry the parent comment_id as well. Prefer the
        # reply's own ID so collecting batches cannot overwrite its parent.
        comment_id = next((values[0] for key in ("reply_comment_id", "comment_id")
                           for link in reversed(row["links"])
                           if (values := parse_qs(urlsplit(link).query).get(key))), "")
        comment = {"commentId": comment_id or f"local-{len(comments) + 1}",
                         "profileName": author or "Kh\u00f4ng r\u00f5", "text": text,
                         "date": date, "likesCount": likes, "threadingDepth": row["depth"]}
        if media:
            comment["media"] = media
        comments.append(comment)
    return comments


def _fold_ui_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _visible_comment_count(article, include_replies: bool) -> int:
    try:
        return int(article.evaluate("""(root, includeReplies) => {
            const storySelector = '[data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"], [data-ad-preview="message"]';
            const isComment = element => {
                const label = element.getAttribute('aria-label') || '';
                if (/^(?:comment|reply)\\s+by\\b|^(?:b\\u00ecnh lu\\u1eadn|tr\\u1ea3 l\\u1eddi)\\s+(?:c\\u1ee7a|b\\u1edfi|d\\u01b0\\u1edbi t\\u00ean)\\b/i.test(label)) return true;
                return element.getAttribute('role') === 'article' &&
                    !element.matches(storySelector) && !element.querySelector(storySelector);
            };
            const candidates = Array.from(root.querySelectorAll(
                'div[role="article"], [aria-label^="Comment by" i], [aria-label^="Reply by" i], ' +
                '[aria-label^="Bình luận" i], [aria-label^="Trả lời" i]'
            )).filter(isComment);
            const unique = Array.from(new Set(candidates));
            if (includeReplies) return unique.length;
            return unique.filter(element => {
                for (let parent = element.parentElement; parent && parent !== root; parent = parent.parentElement) {
                    if (parent.getAttribute('role') === 'article' ||
                        /^(?:reply|tr\\u1ea3 l\\u1eddi)\\b/i.test(parent.getAttribute('aria-label') || '')) return false;
                }
                return true;
            }).length;
        }""", include_replies))
    except Exception:
        return 0


def _select_all_comments(article, page) -> None:
    sort_labels = {"most relevant", "phu hop nhat", "newest", "moi nhat"}
    for action in _post_actions(article, 'button, [role="button"]'):
        if not any(_fold_ui_text(label).strip() in sort_labels for label in action["labels"]):
            continue
        token = str(time.time_ns())
        expected_url = page.url
        menu_root = page.locator('body')
        try:
            page.locator('[role="menu"]:visible, [role="listbox"]:visible').evaluate_all(
                "(nodes, token) => nodes.forEach(node => node.setAttribute('data-grouppulse-old-comment-menu', token))",
                token,
            )
            if not _click_post_action(article, action, page, wait_ms=0):
                continue
            page.wait_for_function(
                """token => {
                    const menus = Array.from(document.querySelectorAll('[role="menu"], [role="listbox"]'))
                        .filter(node => node.getClientRects().length &&
                            node.getAttribute('data-grouppulse-old-comment-menu') !== token);
                    for (const menu of menus) menu.setAttribute('data-grouppulse-comment-menu', token);
                    return menus.length > 0;
                }""", arg=token, timeout=2_000,
            )
            selector = ', '.join(
                f'[data-grouppulse-comment-menu="{token}"] {role}'
                for role in ('[role="menuitem"]', '[role="menuitemradio"]', '[role="option"]', '[role="button"]', 'button')
            )
            page.wait_for_function(
                r"""selector => Array.from(document.querySelectorAll(selector)).some(node => {
                    const label = ((node.getAttribute('aria-label') || '') + ' ' + (node.innerText || ''))
                        .normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
                    return /all comments|tat ca binh luan/.test(label);
                })""", arg=selector, timeout=2_000,
            )
            for option in _post_actions(menu_root, selector):
                if not any(re.fullmatch(r"(?:all comments|tat ca binh luan)(?:[ .].*)?",
                                       _fold_ui_text(label).strip()) for label in option["labels"]):
                    continue
                if _click_post_action(menu_root, option, page, wait_ms=100):
                    return
        except _PostNavigationError:
            raise
        except PlaywrightTimeoutError:
            logging.getLogger(__name__).debug("Comment sort menu did not expose All comments")
        finally:
            # The menu may be slow or already dismissed. Never send a global
            # Escape, which could close the post's own permalink dialog.
            if page.locator(f'[data-grouppulse-comment-menu="{token}"]:visible').count():
                _click_post_action(article, action, page, wait_ms=100)
            for attribute in ('data-grouppulse-old-comment-menu', 'data-grouppulse-comment-menu'):
                page.locator(f'[{attribute}="{token}"]').evaluate_all(
                    "(nodes, attribute) => nodes.forEach(node => node.removeAttribute(attribute))", attribute,
                )
            _assert_post_location(page, expected_url)
        return


def _is_post_expansion(label: str, include_replies: bool) -> bool:
    label = _fold_ui_text(label).replace("đ", "d").strip().rstrip(" .…")
    if label in {"xem them", "see more"}:
        return True
    # Only the vocabulary used by expand controls, never arbitrary profile/content text.
    match = re.fullmatch(
        r"(?:xem|see|view|show) "
        r"(?:(?:all|more|previous|older|earlier|tat ca|cac|them|truoc|cu|\d+)\s+)*"
        r"(?:binh luan|comments?|phan hoi|cau tra loi|tra loi|repl(?:y|ies))"
        r"(?:\s+(?:more|previous|older|khac|truoc|do|nua|cu|\d+))*[ .]*", label
    )
    if not match:
        return False
    has_reply = any(word in label for word in ("phan hoi", "tra loi", "repl"))
    return include_replies or not has_reply


def _scroll_comment_thread(article) -> dict:
    """Scroll the post's own comment viewport, never the group timeline."""
    return article.evaluate(r"""root => {
        const comments = Array.from(root.querySelectorAll('div[role="article"], [aria-label^="Comment by" i], [aria-label^="Reply by" i]'))
            .filter(node=>!node.querySelector('[contenteditable="true"]') && node.querySelector('div[dir="auto"], img'));
        const last = comments[comments.length - 1];
        if (!last) return {available: false};
        for (let node = last.parentElement; node && !node.matches('body,html'); node = node.parentElement) {
            const style = getComputedStyle(node);
            if (!/(auto|scroll)/.test(style.overflowY) || node.clientHeight < 100 || node.scrollHeight <= node.clientHeight + 4) continue;
            const before = node.scrollTop;
            const lastBottom=last.getBoundingClientRect().bottom-node.getBoundingClientRect().top+before;
            // Every currently rendered record was snapshotted before scrolling.
            // Jump to the loaded batch's end only when there is no virtualized
            // tail after its last actual comment; otherwise cover each viewport.
            const atLoadedEnd=lastBottom>=node.scrollHeight-node.clientHeight*.3;
            node.scrollTop = atLoadedEnd ? node.scrollHeight-node.clientHeight :
                Math.min(node.scrollHeight - node.clientHeight, before + Math.max(200, node.clientHeight * .85));
            node.dispatchEvent(new Event('scroll'));
            return {available: true, before, after: node.scrollTop,
                bottom: node.scrollTop + node.clientHeight >= node.scrollHeight - 4};
        }
        return {available: false};
    }""")


def _comment_activity(article) -> dict:
    return article.evaluate(r"""root => {
        let state=root.__grouppulseCommentWatch;
        if(!state){
            const bump=()=>{state.version=window.__grouppulseCommentRevision=(window.__grouppulseCommentRevision||0)+1;};
            state={version:0,bump};bump();
            state.observer=new MutationObserver(bump);
            state.observer.observe(root,{childList:true,subtree:true,characterData:true,attributes:true,
                attributeFilter:['aria-label','aria-expanded','aria-busy']});
            root.__grouppulseCommentWatch=state;
        }
        if(state.observer.takeRecords().length)state.bump();
        const loading=Array.from(root.querySelectorAll('[role="progressbar"],[data-visualcompletion="loading-state"],[aria-busy="true"]'))
            .some(node=>node.getClientRects().length && getComputedStyle(node).visibility!=='hidden');
        return {version:state.version,loading};
    }""")


def _wait_comment_change(article, version: int, timeout: int = 150) -> None:
    article.evaluate("""(root,args) => {
        const state=root.__grouppulseCommentWatch;
        if(!root.isConnected || !state || state.version!==args.version)return;
        return new Promise(resolve=>{
            const observer=new MutationObserver(()=>{observer.disconnect();clearTimeout(timer);resolve();});
            const timer=setTimeout(()=>{observer.disconnect();resolve();},args.timeout);
            observer.observe(root,{childList:true,subtree:true,characterData:true,attributes:true,
                attributeFilter:['aria-label','aria-expanded','aria-busy']});
        });
    }""", {'version':version,'timeout':timeout})


def _expand_post_content(article, include_replies: bool, page, cancelled=None, *, _stall_seconds: float = 30, on_batch=None) -> list[dict]:
    """Collect batches while expanding replies and scrolling a permalink thread."""
    attempted: set[tuple[str, int, tuple[str, ...]]] = set()
    collected: dict[tuple, dict] = {}
    expected = _visible_count(article, COMMENT_WORDS)
    # Facebook may replace the count row when switching comment sort mode or
    # expanding a thread. Preserve the initial visible lower bound as well.
    article._grouppulse_expected_comment_count = expected
    can_scroll = bool(_canonical_group_permalink(page.url))
    idle_since = time.monotonic()
    last_capture_progress=idle_since
    last_version = None
    batch = []
    while not (cancelled and cancelled()):
        activity = _comment_activity(article)
        network=getattr(page,'_grouppulse_comment_network',{'pending':set(),'last':0})
        idle_since=max(idle_since,network['last'])
        loading=activity['loading'] or bool(network['pending'])
        quiet_limit=15 if loading else 2
        if activity['version'] != last_version:
            batch = _visible_comments(article, "", include_replies)
            last_version = activity['version']
        before = len(collected)
        changed=[]
        for comment in batch:
            comment_id = comment["commentId"]
            key = ("id", comment_id) if not comment_id.startswith('local-') else (
                "content", comment["profileName"], comment["text"], comment["date"], comment["threadingDepth"],
                tuple(image["url"] for image in comment.get("media", [])))
            if collected.get(key)!=comment:
                collected[key] = comment
                changed.append(comment)
        if changed and on_batch:
            on_batch(list(collected.values()),changed)
        if len(collected) > before:
            idle_since = time.monotonic()
            last_capture_progress=idle_since
        if time.monotonic()-last_capture_progress>=_stall_seconds:
            # Polling/mutations can continue forever without returning another
            # comment. Persist a partial record and let the durable queue resume
            # it later; never convert this watchdog into a complete zero count.
            article._grouppulse_comments_incomplete=True
            return list(collected.values())
        current_count = len(batch)
        clicked = 0
        for action in _post_actions(article, 'button, [role="button"], a'):
            key = (action["token"], current_count, tuple(action["labels"]))
            if key in attempted or not any(_is_post_expansion(label, include_replies) for label in action["labels"]):
                continue
            is_reply = any(re.search(r'phan hoi|tra loi|repl', _fold_ui_text(label)) for label in action['labels'])
            see_more = any(_fold_ui_text(label).strip().rstrip(' .…') in ('see more','xem them') for label in action['labels'])
            if clicked and not (is_reply or see_more):
                continue
            attempted.add(key)
            if _click_post_action(article, action, page, wait_ms=0):
                clicked += 1
            if clicked >= 4 or (clicked and not (is_reply or see_more)):
                break
        if clicked:
            idle_since = time.monotonic()
            _wait_comment_change(article, activity['version'])
        else:
            if not can_scroll or (expected > 0 and len(collected) >= expected):
                return list(collected.values())
            if not batch and expected <= 0 and not loading and time.monotonic()-idle_since>=.15:
                return list(collected.values())
            state = _scroll_comment_thread(article)
            if not state["available"]:
                # Changing comment sort mode can briefly remove the scroll
                # viewport while its request is still loading.
                if (expected > len(collected) or loading or not batch) and time.monotonic() - idle_since < (quiet_limit if expected or loading else .15):
                    _wait_comment_change(article, activity['version'])
                    continue
                return list(collected.values())
            if state["after"] != state["before"]:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= quiet_limit:
                return list(collected.values())
            _wait_comment_change(article, activity['version'])
    return list(collected.values())


def _extract_post(
    article,
    include_replies: bool,
    page,
    expected_group_keys: set[str] | None = None,
    on_stage=None,
    cancelled=None,
    media_pipeline=None,
    on_capture=None,
) -> tuple[dict, list[dict]]:
    def stage(message: str) -> None:
        if on_stage:
            on_stage(message)

    expected_url = page.url
    preview=None
    initial_media=None
    if on_capture is not None:
        from local_capture import emit_capture
        _assert_post_location(page,expected_url)
        url=_canonical_group_permalink(page.url,expected_group_keys) or _post_url(article,expected_group_keys)
        initial_media=_post_media(article)
        preview={
            'id':url,'url':url,'postUrl':url,
            'author':_owned_first_text(article,'strong, h3, h4') or 'Không rõ tác giả',
            'text':_post_text(article),'time':_post_date(article),'media':initial_media,
            'ocrStatus':'pending','ocrText':'','comments':[],'commentsComplete':False,
            'commentsCount':_visible_count(article,COMMENT_WORDS),'commentsCapturedCount':0,
            'likesCount':None,'sharesCount':None,'reactionBreakdownAvailable':False,
            'reactionBreakdownComplete':False,**{key:None for key in REACTION_LABELS},
        }
        if preview['text'] or initial_media:
            emit_capture(on_capture,preview,[])

    def capture_comments(rows,changed):
        if preview is not None:
            preview.update(text=_post_text(article),comments=rows,commentsCapturedCount=len(rows))
            if preview['text'] or preview['media']:
                emit_capture(on_capture,preview,changed)

    prepared_media = None
    if media_pipeline is not None:
        prepared_media = media_pipeline.prepare(initial_media if initial_media is not None else _post_media(article), page)
        if preview is not None and prepared_media:
            preview['media']=prepared_media
            emit_capture(on_capture,preview,[])
        if any(item.get('ocrStatus') == 'pending' for item in prepared_media):
            # Start CPU recognition while Facebook is loading comments. The
            # eventual post job reuses the durable content-hash OCR cache.
            media_pipeline.submit({'media': prepared_media})
    stage("Đang mở tất cả bình luận trong permalink…")
    _select_all_comments(article, page)
    _assert_post_location(page, expected_url)
    stage("Đang tải nội dung, bình luận và các phản hồi của bài…")
    expanded_comments = _expand_post_content(article, include_replies, page, cancelled=cancelled,
                                            on_batch=capture_comments if preview is not None else None)
    _assert_post_location(page, expected_url)
    stage("Đang đọc nội dung bài đăng và liên kết bình luận…")
    post_text = _post_text(article)
    is_permalink = bool(_canonical_group_permalink(page.url, expected_group_keys)) or _permalink_container(page) is not None
    author = _owned_first_text(article, "strong" if is_permalink else "strong, h3, h4")
    url = (_canonical_group_permalink(page.url, expected_group_keys) or page.url) if is_permalink else _post_url(article, expected_group_keys)
    comments = expanded_comments
    stage("Đang đọc ảnh và nhận dạng chữ bằng CPU…")
    if media_pipeline is None:
        media, ocr_text, ocr_status = _ocr_post_media(_post_media(article), page)
    else:
        media, ocr_text, ocr_status = prepared_media, '', 'pending'
    stage("Đang đọc cảm xúc hiển thị trên bài…")
    numeric_counts = _owned_numeric_counts(article)
    values = _owned_values(article)
    def snapshot_count(words):
        for label, text in numeric_counts:
            if words.search(label):
                return _count(text)
        return next((_count(value) for value in values if words.search(value) and _count(value)), 0)
    reaction_counts, reaction_breakdown_complete = _reaction_breakdown(article, page, values)
    _assert_post_location(page, expected_url)
    if reaction_breakdown_complete:
        aggregate_reactions = sum(value or 0 for value in reaction_counts.values())
    else:
        reported_total = snapshot_count(REACTION_TOTAL_WORDS)
        aggregate_reactions = reported_total if reported_total else None
    reported_comments = max(snapshot_count(COMMENT_WORDS),getattr(article,'_grouppulse_expected_comment_count',0))
    post = {
        "id": url or f"local-post-{int(time.time() * 1000)}",
        "url": url,
        "postUrl": url,
        "author": author or "Không rõ tác giả",
        "text": post_text,
        "media": media,
        "ocrText": ocr_text,
        "ocrStatus": ocr_status,
        "time": _post_date(article),
        "likesCount": aggregate_reactions,
        **reaction_counts,
        "reactionBreakdownAvailable": any(value is not None for value in reaction_counts.values()),
        "reactionBreakdownComplete": reaction_breakdown_complete,
        "commentsCount": reported_comments,
        "commentsCapturedCount": len(comments),
        "commentsComplete": len(comments) >= reported_comments and not getattr(article,'_grouppulse_comments_incomplete',False),
        "sharesCount": snapshot_count(SHARE_WORDS),
        "comments": comments,
    }
    return post, comments


def _login_timeout_message(page) -> str:
    try:
        path = page.evaluate("location.pathname").casefold()
    except Exception:
        path = ""
    try:
        body = page.locator("body").inner_text(timeout=1_500).casefold()
    except Exception:
        body = ""

    if any(marker in path for marker in ("checkpoint", "two_step", "two-factor", "auth_platform")):
        return "Facebook yêu cầu xác minh tài khoản hoặc mã xác thực. Hoàn thành bước này trong cửa sổ Chromium rồi chạy lại."
    if any(marker in body for marker in ("two-factor authentication", "authentication code", "security code", "mã xác thực", "mã bảo mật")):
        return "Facebook đang chờ mã xác thực hoặc xác nhận đăng nhập. Hoàn thành bước này trong cửa sổ Chromium rồi chạy lại."
    if any(marker in body for marker in ("incorrect password", "password you entered is incorrect", "mật khẩu bạn nhập không đúng", "mật khẩu không chính xác")):
        return "Facebook từ chối thông tin đăng nhập. Kiểm tra thông báo ngay trên trang Chromium; trạng thái .env chỉ xác nhận hai giá trị không trống, không xác thực mật khẩu."
    if "login" in path or "log in" in body or "đăng nhập" in body:
        return "Facebook vẫn ở màn hình đăng nhập sau 3 phút. Kiểm tra lỗi hiện trên trang Chromium; có thể cần xác minh, hoặc Facebook không chấp nhận biểu mẫu đăng nhập tự động."
    return "Facebook chưa xác nhận đăng nhập sau 3 phút. Xem thông báo trên cửa sổ Chromium; trạng thái .env chỉ xác nhận tài khoản và mật khẩu đã được nạp."


def _select_newest_feed(page) -> bool:
    newest={'new posts','newest posts','newest','most recent','latest','bai viet moi','bai dang moi','moi nhat'}
    menus={'most relevant','recent activity','top posts','sort','sap xep','phu hop nhat','hoat dong gan day','bai viet hang dau'}
    root=page.locator('body')
    for action in _post_actions(root,'button,[role="button"]'):
        labels={_fold_ui_text(value).replace('đ','d').strip() for value in action['labels']}
        if not labels.intersection(newest | menus):
            continue
        control=page.locator(f'[data-grouppulse-post-action="{action["token"]}"]')
        if control.evaluate("node => !!node.closest('[role=article],[role=dialog]')"):
            continue
        if labels.intersection(newest):
            return True
        if not _click_post_action(root,action,page,wait_ms=0):
            continue
        try:
            page.wait_for_function(r"""() => Array.from(document.querySelectorAll('[role="menu"] [role="menuitem"], [role="menu"] [role="menuitemradio"], [role="listbox"] [role="option"], [role="menu"] [role="button"]'))
                .some(node=>node.getClientRects().length && /^(?:new posts|newest(?: posts)?|most recent|latest|bai viet moi|bai dang moi|moi nhat)(?:\s|$)/.test(
                    ((node.getAttribute('aria-label')||'')+' '+(node.innerText||'')).normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/\u0111/g,'d').toLowerCase().trim()))""",timeout=1500)
            for option in _post_actions(root,'[role="menu"] [role="menuitem"], [role="menu"] [role="menuitemradio"], [role="listbox"] [role="option"], [role="menu"] [role="button"]'):
                labels=[_fold_ui_text(value).replace('đ','d').strip() for value in option['labels']]
                if any(value==name or value.startswith(name+' ') for value in labels for name in newest):
                    return _click_post_action(root,option,page,wait_ms=150)
        except PlaywrightTimeoutError:
            return False
    return False


def _open_source_page(page, email: str, password: str, source_url: str) -> None:
    page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
    email_input = page.locator('input[name="email"]')
    password_input = page.locator('input[name="pass"]')
    try:
        try:
            page.wait_for_function("""() => {
                const login=document.querySelector('input[name="email"]');
                return (login && login.getClientRects().length) || document.querySelector('[role="navigation"], [role="main"]');
            }""",timeout=5_000)
            login_required = email_input.is_visible()
        except PlaywrightTimeoutError:
            login_required = False
        if login_required:
            email_input.fill(email)
            password_input.fill(password)
            page.get_by_role("button", name=re.compile(r"log in|login|đăng nhập|connexion|se connecter", re.I)).first.click()
            try:
                page.wait_for_function(
                    """() => {
                        if (/(checkpoint|two_step_verification|two-factor|login)/i.test(location.pathname)) return false;
                        return !Array.from(document.querySelectorAll('input[name="email"], input[name="pass"]'))
                            .some(element => element.getClientRects().length > 0);
                    }""",
                    timeout=180_000,
                )
            except PlaywrightTimeoutError:
                raise RuntimeError(_login_timeout_message(page)) from None
    except RuntimeError:
        raise
    except PlaywrightTimeoutError:
        raise RuntimeError("Playwright hết thời gian khi thao tác biểu mẫu đăng nhập. Kiểm tra màn hình Chromium.") from None
    except Exception as error:
        raise RuntimeError(f"Không thể thao tác biểu mẫu đăng nhập Facebook ({type(error).__name__}). Kiểm tra màn hình Chromium.") from None

    page.goto(source_url, wait_until="domcontentloaded")
    page.wait_for_selector(f'{POST_SELECTOR}, [data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"]', timeout=25_000)

    _select_newest_feed(page)

    try:
        page.wait_for_selector(
            f'{POST_SELECTOR}, [data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"]',
            timeout=25_000,
        )
    except PlaywrightTimeoutError:
        raise RuntimeError("Không tìm thấy bài đăng ở URL này. Kiểm tra đăng nhập và quyền xem group, page hoặc profile.") from None


def scrape_facebook_posts(
    *,
    email: str,
    password: str,
    source_url: str,
    include_replies: bool,
    progress,
    cancelled,
    known_post_urls: set[str] | None = None,
    known_group_keys: set[str] | None = None,
    on_source=None,
    on_post=None,
    max_posts: int | None = None,
    replay_urls: list[str] | None = None,
    collect_results: bool = True,
    resume_history: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Return visible timeline posts in newest-to-oldest order."""
    if _group_key_from_url(source_url):
        import sys
        from local_runner import run_group
        return run_group(sys.modules[__name__], email=email, password=password, source_url=source_url,
                         include_replies=include_replies, progress=progress, cancelled=cancelled,
                         known_post_urls=known_post_urls, known_group_keys=known_group_keys,
                         on_source=on_source, on_post=on_post, max_posts=max_posts, replay_urls=replay_urls, collect_results=collect_results,resume_history=resume_history)
    posts: list[dict] = []
    comments: list[dict] = []
    known_post_urls = known_post_urls if known_post_urls is not None else set()
    known_group_post_ids: set[str] = set()

    def group_post_id(value: str) -> str:
        normalized = normalize_post_url(value)
        match = re.search(
            r"/groups/[^/]+/(?:permalink|posts)/(\d+)(?:/|$)",
            urlsplit(normalized).path,
            re.IGNORECASE,
        )
        return match.group(1) if match else ""

    def refresh_known_group_post_ids() -> None:
        known_group_post_ids.update(
            post_id for post_id in (group_post_id(value) for value in known_post_urls) if post_id
        )

    def is_known_post(value: str) -> bool:
        normalized = normalize_post_url(value)
        if normalized and normalized in known_post_urls:
            return True
        post_id = group_post_id(normalized)
        return bool(post_id and post_id in known_group_post_ids)

    refresh_known_group_post_ids()
    group_source = bool(_group_key_from_url(source_url))

    def top_level_articles(page) -> list:
        permalink = _permalink_container(page)
        if permalink is not None:
            return [permalink]
        if group_source:
            scan_prefix = str(time.monotonic_ns())
            try:
                cards = page.evaluate("""({selector, scanPrefix}) => {
                    const attribute = 'data-codex-feed-card-id';
                    document.querySelectorAll(`[${attribute}]`).forEach(element => element.removeAttribute(attribute));
                    const storySelector = selector;
                    const fold = value => (value || '').toLocaleLowerCase().normalize('NFD')
                        .replace(/[\\u0300-\\u036f]/g, '').replace(/\\s+/g, ' ').trim();
                    const messageNodes = Array.from(document.querySelectorAll(storySelector));
                    const cards = [];
                    const seenCards = new Set();
                    for (const marker of messageNodes) {
                        if (!marker.getClientRects().length) continue;
                        const owner = marker.closest('[role="article"]');
                        if (owner && (/^(?:comment|reply)\\s+by\\b|^(?:binh luan|tra loi)\\s+(?:cua|boi)\\b/.test(fold(owner.getAttribute('aria-label'))) ||
                            owner.parentElement?.closest('[role="article"]'))) continue;
                        let card = marker.parentElement;
                        for (let depth = 0; card && depth < 12; depth++, card = card.parentElement) {
                            const stories = Array.from(card.querySelectorAll(storySelector));
                            if (card.matches(storySelector)) stories.unshift(card);
                            const ownedStories = stories.filter(node => {
                                const owner = node.closest('[role="article"]');
                                return !owner || (!/^(?:comment|reply)\\s+by\\b|^(?:binh luan|tra loi)\\s+(?:cua|boi)\\b/.test(fold(owner.getAttribute('aria-label'))) &&
                                    !owner.parentElement?.closest('[role="article"]'));
                            });
                            const uniqueStories = new Set(ownedStories.map(node => fold(node.innerText)).filter(Boolean));
                            if (uniqueStories.size !== 1) continue;
                            const labels = Array.from(card.querySelectorAll('button, [role="button"]'))
                                .filter(element => element.getClientRects().length)
                                .flatMap(element => [fold(element.getAttribute('aria-label')), fold(element.innerText)]);
                            const hasComment = labels.some(label =>
                                /^(?:viet binh luan|binh luan|write a comment|leave a comment|comment|comments|(?:\\d+\\s+)?comments?)(?:\\s+\\d+)?$/.test(label));
                            const hasLike = labels.some(label => /^(?:thich|like)$/.test(label));
                            const hasShare = labels.some(label => /^(?:chia se|share)(?:[,.:].*)?$/.test(label));
                            if (!hasComment || !hasLike || !hasShare) continue;
                            if (seenCards.has(card)) break;
                            seenCards.add(card);
                            const id = `${scanPrefix}-${cards.length}`;
                            card.setAttribute(attribute, id);
                            const hrefs = Array.from(card.querySelectorAll('a[href]'))
                                .filter(link => {
                                    for (let current = link; current && current !== card; current = current.parentElement) {
                                        if (current.getAttribute('role') === 'article') return false;
                                        const label = current.getAttribute('aria-label') || '';
                                        if (/^(?:comment|reply)\\s+by\\b|^(?:bình luận|trả lời)\\s+(?:của|bởi)\\b/i.test(label)) return false;
                                    }
                                    return true;
                                })
                                .map(link => {
                                    try {
                                        const url = new URL(link.href, location.href);
                                        if (!['facebook.com', 'www.facebook.com', 'm.facebook.com'].includes(url.hostname)) return null;
                                        if (!(/\\/(?:posts|permalink)\\/\\d+/i.test(url.pathname) || /[?&](?:story_fbid|multi_permalinks)=\\d+/i.test(url.search))) return null;
                                        const label = [link.getAttribute('aria-label'), link.getAttribute('title'), link.innerText]
                                            .filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                                        let score = /\\/permalink\\/\\d+/i.test(url.pathname) ? 60 :
                                            /\\/posts\\/\\d+/i.test(url.pathname) ? 50 : 30;
                                        if (/comment|bình luận/i.test(label)) score += 100;
                                        if (/[?&]comment_id=/i.test(url.search)) score += 20;
                                        return {href: url.href, score};
                                    } catch { return null; }
                                })
                                .filter(Boolean)
                                .sort((left, right) => right.score - left.score)
                                .map(link => link.href);
                            const commentHrefs = Array.from(card.querySelectorAll('a[href]'))
                                .map(anchor => anchor.href).filter(href => {
                                    try { return new URL(href, location.href).searchParams.has('comment_id'); }
                                    catch { return false; }
                                });
                            cards.push({id, hrefs, commentHrefs});
                            break;
                        }
                    }
                    return cards;
                }""", {
                    "selector": ", ".join(POST_MESSAGE_SELECTORS),
                    "scanPrefix": scan_prefix,
                })
                return [
                    (
                        page.locator(f'[data-codex-feed-card-id="{card["id"]}"]'),
                        _post_url_from_hrefs(card["hrefs"], group_keys)
                        or _post_url_from_comment_hrefs(card["commentHrefs"], group_keys),
                    )
                    for card in cards
                ]
            except Exception:
                # Keep the old locator-based scan as a fallback if Facebook changes
                # the DOM shape or the batched DOM query cannot run.
                for attempt in range(4):
                    markers = page.locator(", ".join(POST_MESSAGE_SELECTORS))
                    top_level = []
                    seen_cards: set[str] = set()
                    try:
                        for index in range(markers.count()):
                            marker = markers.nth(index)
                            if not marker.is_visible():
                                continue
                            if marker.evaluate(r"""element => {
                                const owner = element.closest('[role="article"]');
                                if (!owner) return false;
                                const label = (owner.getAttribute('aria-label') || '').normalize('NFD')
                                    .replace(/[\u0300-\u036f]/g, '').toLowerCase();
                                return /^(?:comment|reply)\s+by\b|^(?:binh luan|tra loi)\s+(?:cua|boi)\b/.test(label) ||
                                    Boolean(owner.parentElement && owner.parentElement.closest('[role="article"]'));
                            }"""):
                                continue
                            card = _post_card_from_story_message(marker)
                            if card is None:
                                continue
                            permalink_url = _post_url(card, group_keys)
                            card_key = card.evaluate("""(root, fallbackId) => {
                                const attribute = 'data-grouppulse-node-id';
                                if (!root.hasAttribute(attribute)) root.setAttribute(attribute, fallbackId);
                                return root.getAttribute(attribute);
                            }""", f"{scan_prefix}-fallback-{index}")
                            if card_key in seen_cards:
                                continue
                            seen_cards.add(card_key)
                            top_level.append(card)
                        if top_level:
                            return top_level
                    except Exception:
                        if attempt < 3:
                            page.wait_for_timeout(300)
                return []
        for attempt in range(4):
            articles = page.locator(POST_SELECTOR)
            top_level = []
            try:
                for index in range(articles.count()):
                    article = articles.nth(index)
                    if not article.evaluate("element => !element.parentElement?.closest('div[role=article]')"):
                        continue
                    if COMMENT_LABEL.search(article.get_attribute("aria-label") or ""):
                        continue
                    top_level.append(article)
                return top_level
            except Exception:
                # Facebook replaces virtualized feed cards while lazy content loads.
                if attempt < 3:
                    page.wait_for_timeout(300)
        return []

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            slow_mo=0,
            viewport={"width": 1440, "height": 1000},
            args=["--start-maximized"],
        )
        context.set_default_timeout(3_000)
        context.set_default_navigation_timeout(45_000)
        page = context.pages[0] if context.pages else context.new_page()
        page.bring_to_front()
        detail_page = None
        try:
            progress("running", 5, "Đang đăng nhập Facebook trong cửa sổ Chromium cục bộ…")
            _open_source_page(page, email, password, source_url)
            group_keys = _source_group_keys(page, source_url) if group_source else set()
            if group_source:
                group_keys.update(known_group_keys or set())
            source_aliases = [source_url, page.url]
            source_aliases.extend(f"https://www.facebook.com/groups/{key}/" for key in group_keys)
            if on_source:
                on_source(page.url, source_aliases)
                refresh_known_group_post_ids()
            direct_post_page = _permalink_container(page) is not None
            if group_source:
                _install_group_navigation_guard(
                    context, page if not direct_post_page else None, group_keys,
                    lambda status, _, message: progress(status, min(94, 15 + len(posts)), message),
                )
            # Keep the timeline loaded while opening each post's comments permalink.
            # Create this tab lazily so it is not left on about:blank before a usable
            # post URL has been found.
            progress("running", 15, "Đã mở timeline; đang đọc các bài đăng hiển thị…")
            no_new_post_since = time.monotonic()
            last_wait_message_at = 0.0
            while not cancelled():
                if group_source and not direct_post_page:
                    _assert_group_timeline(page, group_keys)
                articles = top_level_articles(page)
                for article_item in articles:
                    if cancelled():
                        break
                    if group_source and not direct_post_page:
                        _assert_group_timeline(page, group_keys)
                    if isinstance(article_item, tuple):
                        article, feed_permalink = article_item
                    else:
                        article, feed_permalink = article_item, ""
                    if not feed_permalink:
                        feed_permalink = _post_url(article, group_keys if group_source else None)
                    if direct_post_page and not feed_permalink:
                        feed_permalink = _canonical_group_permalink(page.url) or page.url
                        group_key = _group_key_from_url(feed_permalink)
                        if group_key:
                            group_keys.add(group_key)
                    if group_source and feed_permalink:
                        feed_group_key = _group_key_from_url(feed_permalink)
                        group_alias = f"https://www.facebook.com/groups/{feed_group_key}/" if feed_group_key else ""
                        if group_alias and group_alias not in source_aliases:
                            source_aliases.append(group_alias)
                            if on_source:
                                on_source(source_url, source_aliases)
                                refresh_known_group_post_ids()
                    post_article = article
                    post_page = page
                    if is_known_post(feed_permalink):
                        continue

                    # Try link hydration first, then resolve the comment dialog
                    # in place. Never load another copy of the group timeline.
                    if (
                        group_source
                        and not direct_post_page
                        and not _canonical_group_permalink(feed_permalink, group_keys)
                    ):
                        resolved_url = ""
                        for link_attempt in range(3):
                            if cancelled():
                                break
                            progress(
                                "running", min(92, 15 + len(posts)),
                                f"Đang xác định permalink và kiểm tra kho (lần {link_attempt + 1}/3)…",
                            )
                            resolved_url = _resolve_feed_permalink(page, article, group_keys, cancelled)
                            if not resolved_url:
                                resolved_url = _click_comment_for_permalink(page, article, group_keys, cancelled)
                            if resolved_url:
                                break
                            if link_attempt < 2:
                                page.wait_for_timeout(350)
                        if cancelled():
                            break
                        if not resolved_url:
                            raise RuntimeError(
                                "Không lấy được permalink thuộc group sau 3 lần thử nút bình luận. "
                                "Bài này chưa được lưu; scraper dừng để không bỏ sót bài. "
                                "Các bài đã lưu trong kho vẫn còn."
                            )
                        if resolved_url:
                            feed_permalink = resolved_url
                            resolved_group_key = _group_key_from_url(feed_permalink)
                            group_alias = f"https://www.facebook.com/groups/{resolved_group_key}/" if resolved_group_key else ""
                            if group_alias and group_alias not in source_aliases:
                                source_aliases.append(group_alias)
                                if on_source:
                                    on_source(source_url, source_aliases)
                                    refresh_known_group_post_ids()

                    if is_known_post(feed_permalink):
                        continue

                    for read_attempt in range(3):
                        if cancelled():
                            break
                        try:
                            if not direct_post_page and feed_permalink:
                                if detail_page is None or detail_page.is_closed():
                                    detail_page = context.new_page()
                                post_page = detail_page
                                if group_source:
                                    progress(
                                        "running",
                                        min(92, 15 + len(posts)),
                                        f"Đang đọc bài trong tab permalink (lần {read_attempt + 1}/3): {urlsplit(feed_permalink).path}",
                                    )
                                post_article = _open_post_permalink(detail_page, feed_permalink)
                            post, post_comments = _extract_post(
                                post_article,
                                include_replies,
                                post_page,
                                group_keys if group_source else None,
                                cancelled=cancelled,
                                on_stage=lambda message: progress(
                                    "running", min(92, 15 + len(posts)), message,
                                ),
                            )
                            break
                        except (RuntimeError, PlaywrightTimeoutError) as error:
                            if direct_post_page or read_attempt >= 2:
                                raise RuntimeError(
                                    f"Chưa đọc được bài {urlsplit(feed_permalink).path}. "
                                    "Bài chưa được lưu; scraper dừng để không bỏ sót. "
                                    f"Chi tiết: {error}"
                                ) from error
                            progress(
                                "running", min(92, 15 + len(posts)),
                                f"Bài tải lỗi; đang thử lại bài này (lần {read_attempt + 2}/3)…",
                            )
                            page.wait_for_timeout(500)
                    if cancelled():
                        break
                    if group_source and (
                        _group_key_from_url(str(post.get("url") or "")) not in group_keys
                        or group_post_id(str(post.get("url") or "")) != group_post_id(feed_permalink)
                        or not _canonical_group_permalink(post_page.url, group_keys)
                        or group_post_id(post_page.url) != group_post_id(feed_permalink)
                    ):
                        raise RuntimeError(
                            "Trang permalink trả về bài khác với bài đang lấy trong group. "
                            "Chưa lưu bài này; scraper dừng để tránh lưu nhầm hoặc bỏ sót."
                        )
                    if not post["text"] and not post["media"] and not post["comments"]:
                        raise RuntimeError(
                            "Bài đang đọc chưa có nội dung, media hoặc bình luận. "
                            "Chưa lưu bài này; scraper dừng để không đánh dấu nhầm là đã scrape."
                        )
                    post_key = normalize_post_url(str(post.get("url") or feed_permalink or ""))
                    if is_known_post(post_key):
                        continue
                    posts.append(post)
                    for comment in post_comments:
                        comment["postId"] = post["id"]
                        comment["postAuthor"] = post["author"]
                        comment["postUrl"] = post["url"] or feed_permalink
                        comment["parentPostUrl"] = comment["postUrl"]
                        comment["commentUrl"] = ""
                        comments.append(comment)
                    if on_post:
                        on_post(post, post_comments)
                    # Only a successful capture restarts the one-hour idle timer.
                    no_new_post_since = time.monotonic()
                    if post_key:
                        known_post_urls.add(post_key)
                        post_id = group_post_id(post_key)
                        if post_id:
                            known_group_post_ids.add(post_id)
                    progress(
                        "running",
                        min(94, 15 + len(posts)),
                        f"\u0110\u00e3 \u0111\u1ecdc {len(posts)} b\u00e0i v\u00e0 {len(comments)} b\u00ecnh lu\u1eadn; \u0111ang t\u00ecm b\u00e0i c\u0169 h\u01a1n...",
                    )
                if cancelled() or direct_post_page:
                    break
                # Keep scrolling through Facebook's delayed, virtualized timeline;
                # stop only after an hour without discovering another feed post.
                if time.monotonic() - no_new_post_since >= NO_NEW_POST_TIMEOUT_SECONDS:
                    break
                scroll_before = page.evaluate("""() => {
                    const before = window.scrollY;
                    window.scrollBy(0, Math.min(window.innerHeight * 0.8, 800));
                    return before;
                }""")
                page.wait_for_timeout(900)
                if (
                    abs(page.evaluate("window.scrollY") - scroll_before) < 1
                    and time.monotonic() - last_wait_message_at >= 5
                ):
                    last_wait_message_at = time.monotonic()
                    progress(
                        "running", min(94, 15 + len(posts)),
                        f"Đang chờ Facebook tải thêm bài ở cuối timeline; đã lấy {len(posts)} bài mới…",
                    )
            if not include_replies:
                for comment in comments:
                    comment["threadingDepth"] = 0
            dated_posts = [(_date_sort_value(str(post.get("time") or "")), index, post) for index, post in enumerate(posts)]
            dated_posts.sort(key=lambda item: (item[0] is None, -(item[0] or 0), item[1]))
            posts = [item[2] for item in dated_posts]
            return posts, comments
        finally:
            context.close()
