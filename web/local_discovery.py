"""Atomic feed snapshots; only immutable post URLs survive discovery."""
from __future__ import annotations
import time
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


def discover(adapter, page, group_keys: set[str], known, cancelled) -> tuple[list[str], bool]:
    cards = page.evaluate(r"""prefix => {
        const story = '[data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"], [data-ad-preview="message"]';
        const fold = text => (text || '').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/\u0111/g,'d').toLowerCase().replace(/\s+/g,' ').trim();
        const comment = node => /^(?:comment|reply)\s+by\b|^(?:binh luan|tra loi)\s+(?:cua|boi|duoi ten)\b/.test(fold(node.getAttribute('aria-label')));
        const owned = (root,node) => {
            for (let current=node;current && current!==root;current=current.parentElement)
                if (current.getAttribute('role')==='article' || comment(current)) return false;
            return root.contains(node);
        };
        const markers = Array.from(document.querySelectorAll(story));
        const roots = Array.from(document.querySelectorAll('div[role="article"]')).filter(node =>
            !comment(node) && !node.parentElement?.closest('[role="article"]'));
        const mediaStarts=Array.from(document.querySelectorAll('img,video,[style*="background"]')).filter(node=>{
            const box=node.getBoundingClientRect();return box.width>=160 && box.height>=100 &&
                (node.matches('img,video') || getComputedStyle(node).backgroundImage.includes('url('));
        });
        const starts = [...markers,...roots,...mediaStarts];
        const seen = new Set(), result = [];
        for (const marker of starts) {
            if (!marker.getClientRects().length) continue;
            if(Array.from(seen).some(root=>root.contains(marker)))continue;
            let inComment=false;
            for(let parent=marker;parent && !parent.matches('body,html');parent=parent.parentElement){
                if(comment(parent)){inComment=true;break;}
            }
            if(inComment)continue;
            const owner=marker.closest('[role="article"]');
            if (owner && (comment(owner) || owner.parentElement?.closest('[role="article"]'))) continue;
            let root = marker.matches(story) ? marker.parentElement : marker;
            for(let depth=0;root && depth<12 && !root.matches('body,html');depth++,root=root.parentElement) {
                const stories=Array.from(root.querySelectorAll(story)).filter(node=>owned(root,node) && !root.contains(node.parentElement?.closest(story)));
                if(stories.length>1) break;
                const actions=Array.from(root.querySelectorAll('button,[role="button"]')).filter(node=>owned(root,node) && node.getClientRects().length);
                const labels=actions.flatMap(node=>[fold(node.getAttribute('aria-label')),fold(node.innerText)]);
                const controls=labels.some(label=>/^(?:thich|da thich|like|liked|comment|comments|binh luan|viet binh luan|write a comment|leave a comment)(?:[ .,:].*)?$/.test(label) || /^(?:\d+\s+)?(?:comments?|binh luan)(?:\s+\d+)?$/.test(label));
                const hrefs=Array.from(root.querySelectorAll('a[href]')).filter(node=>owned(root,node) && !node.closest(story))
                    .map(node=>node.href).filter(href=>/\/(?:posts|permalink)\/\d+|[?&](?:story_fbid|multi_permalinks)=\d+/i.test(href));
                const media=Array.from(root.querySelectorAll('img,video,[style*="background"]')).some(node=>{
                    if(!owned(root,node))return false;
                    const box=node.getBoundingClientRect();return box.width>=160 && box.height>=100 &&
                        (node.matches('img,video') || getComputedStyle(node).backgroundImage.includes('url('));
                });
                if(!(stories.length || media) || !(controls || hrefs.length)) continue;
                if(seen.has(root)) break;
                seen.add(root);
                const token=prefix+'-'+result.length;
                root.setAttribute('data-grouppulse-discovery',token);
                const commentHrefs=Array.from(root.querySelectorAll('a[href]')).filter(node=>!node.closest('div[dir="auto"]'))
                    .map(node=>node.href).filter(href=>/[?&]comment_id=/.test(href));
                result.push({token,hrefs,commentHrefs,root});
                break;
            }
        }
        const minimal=result.filter(item=>!result.some(other=>item.root!==other.root && item.root.contains(other.root)));
        const intervals=minimal.map(item=>{const box=item.root.getBoundingClientRect();return {top:box.top+scrollY,bottom:box.bottom+scrollY};});
        intervals.sort((a,b)=>a.top-b.top);
        const end=Math.max(0,...intervals.map(box=>box.bottom));
        const gap=intervals.some((box,index)=>index && box.top-intervals[index-1].bottom>innerHeight*.8);
        window.__grouppulseLoadedFeedEnd={url:location.href,end,
            safe:!!intervals.length && !gap && document.scrollingElement.scrollHeight-end<=innerHeight*.5};
        const cache=window.__grouppulseResolvedCards ||= new WeakMap();
        return minimal.map(({root,...item})=>{
            const saved=cache.get(root);
            if(saved){
                if(saved.observer.takeRecords().length)saved.valid=false;
                if(saved.valid)item.hrefs.unshift(saved.url);
            }
            return item;
        });
    }""", str(time.monotonic_ns()))
    urls, unresolved = [], []
    if any(card['hrefs'] for card in cards):
        group_keys.update(adapter._source_group_keys(page,page.url))
    for card in cards:
        url = adapter._post_url_from_hrefs(card['hrefs'], group_keys) or adapter._post_url_from_comment_hrefs(card['commentHrefs'], group_keys)
        if url:
            url = adapter._canonical_group_permalink(url, group_keys)
            if not known(url) and url not in urls:
                urls.append(url)
        elif not any(adapter._canonical_group_permalink(href) for href in card['hrefs']):
            unresolved.append(card)
    # Resolve missing links before spending time on any detail page. A locator
    # is scoped to this short discovery step, never to a queued post.
    changed = False
    for card in unresolved:
        if cancelled():
            break
        article = page.locator(f'[data-grouppulse-discovery="{card["token"]}"]')
        if not article.count():
            changed = True
            continue
        try:
            url = adapter._resolve_feed_permalink(page, article, group_keys, cancelled)
            if not url:
                url = adapter._click_comment_for_permalink(page, article, group_keys, cancelled)
            url = adapter._canonical_group_permalink(url, group_keys)
            if url:
                page.evaluate("""({token,url})=>{
                    const root=document.querySelector('[data-grouppulse-discovery="'+token+'"]');
                    if(!root)return;
                    const cache=window.__grouppulseResolvedCards ||= new WeakMap();
                    const previous=cache.get(root);previous?.observer.disconnect();
                    const state={url,valid:true};
                    state.observer=new MutationObserver(()=>{state.valid=false;});
                    state.observer.observe(root,{childList:true,subtree:true,characterData:true,
                        attributes:true,attributeFilter:['href','aria-label','data-ft']});
                    cache.set(root,state);
                }""",{'token':card['token'],'url':url})
            if url and not known(url) and url not in urls:
                urls.append(url)
            elif not url:
                changed = True
        except PlaywrightTimeoutError:
            changed = True
    return urls, changed


def advance_feed(page) -> None:
    page.evaluate("""() => {
        const state=window.__grouppulseLoadedFeedEnd;
        const step=Math.min(innerHeight*.8,800);
        const destination=state?.safe && state.url===location.href ? state.end-innerHeight*.5 : scrollY+step;
        if(destination>scrollY+step)window.scrollTo(0,destination);
        else window.scrollBy(0,step);
    }""")
