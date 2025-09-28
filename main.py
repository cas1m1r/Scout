from datetime import datetime
from email.utils import parsedate_to_datetime
from dotenv import load_dotenv
import feedparser
import time
import os
import json
from llama_utils import async_ask_model
from tracker import EventTracker  # NEW

load_dotenv()
URL = os.environ.get('URL')

GOOGLE_SEARCH_BASE = 'https://news.google.com/rss/search?'
def ensure_topic_dir(topic: str) -> str:
    out_dir = topic.lower().strip().replace(' ', '_').replace('/', '_')
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    return out_dir


def load_pre_prompt():
    with open(os.path.join('prompts','news_prepper.txt'),'r') as f:
        result = f.read()
    f.close()
    return result

def save_raw_stories(parsed_feeds, topic):
    out_dir = ensure_topic_dir(topic)
    t = datetime.fromtimestamp(time.time())
    timestamp = f'{t.month:02d}{t.day:02d}{t.year:04d}_{t.hour:02d}{t.minute:02d}{t.second:02d}'
    file_out = os.path.join(out_dir, f'data_{timestamp}.json')
    with open(file_out, 'w', encoding='utf-8') as f:
        f.write(json.dumps(parsed_feeds, indent=2, ensure_ascii=False))
    print(f'[+] Data saved to {file_out}')

def check_google_rss(query, time_span, depth: int = 100):
    """Pull Google News RSS for `query` within `time_span` (e.g., 24h).
    Returns list of dicts with fields expected by downstream code.
    """
    depth = max(1, int(depth or 100))
    result = []
    feed = feedparser.parse(f'{GOOGLE_SEARCH_BASE}q={query.replace(" ", "+")}+when:{time_span}')
    for entry in feed.get('entries', []):
        title = entry.get('title')
        link = entry.get('link')
        t = entry.get('published_parsed')  # time.struct_time or None
        tlabel = entry.get('published')
        # Robust TS; fallback to RFC822 parse if needed
        if t:
            ts = f'{t.tm_mon:02d}-{t.tm_mday:02d}-{t.tm_year:04d}_{t.tm_hour:02d}{t.tm_min:02d}{t.tm_sec:02d}'
        else:
            try:
                dt = parsedate_to_datetime(tlabel) if tlabel else datetime.utcnow()
                ts = f'{dt.month:02d}-{dt.day:02d}-{dt.year:04d}_{dt.hour:02d}{dt.minute:02d}{dt.second:02d}'
            except Exception:
                now = datetime.utcnow()
                ts = f'{now.month:02d}-{now.day:02d}-{now.year:04d}_{now.hour:02d}{now.minute:02d}{now.second:02d}'

        source = {'name': None, 'link': None}
        try:
            src = entry.get('source') or {}
            source = {'name': src.get('title'), 'link': src.get('href')}
        except Exception:
            pass

        result.append({
            'title': title,
            'link': link,
            'source': source,
            'date': tlabel,
            'ts': ts
        })
        if len(result) >= depth:
            break
    return result

async def summarize_story(host, event, model, pre_prompt):
    query = f'{pre_prompt}\n```json\n{json.dumps(event, indent=2)}\n```\n'
    r = await async_ask_model(host, model, query)
    return r

def load_pre_prompt():
    with open(os.path.join('prompts','news_prepper.txt'), 'r', encoding='utf-8') as f:
        pre_prompt = f.read()
    return pre_prompt

def extract_machine_json(raw_summary):
    """
    Extract the JSON block from the model response.
    Expected format contains a ```json fenced block.
    """
    try:
        if "```json" in raw_summary:
            blk = raw_summary.split('```json', 1)[-1].split('```', 1)[0]
        else:
            # Fallback: find first '{' and last '}' to be permissive
            start = raw_summary.find('{')
            end = raw_summary.rfind('}')
            blk = raw_summary[start:end+1]
        data = json.loads(blk)
        return data
    except Exception:
        return None

def extract_human_summary(raw_summary):
    """
    Pull the human summary section defensively.
    """
    try:
        if "Human Summary" in raw_summary:
            # Split on the header and stop before the next section marker if present
            part = raw_summary.split("Human Summary", 1)[-1]
            # Trim common next-section markers
            for marker in ["### 2", "Machine JSON", "```json"]:
                idx = part.find(marker)
                if idx != -1:
                    part = part[:idx]
            return part.strip().replace('\n\n','\n')
        return raw_summary.strip()
    except Exception:
        return raw_summary.strip()

async def main():
    if not URL:
        raise RuntimeError("URL environment variable not set. Put your model host URL in .env as URL=...")

    system_prompt = load_pre_prompt()
    topic = 'ICE protest'
    protest_news = check_google_rss(f'{topic}', '24h')

    condensed = []
    summarizer = 'gemma3:4b'

    for event in protest_news:
        llm = await summarize_story(URL, event, summarizer, system_prompt)
        raw_summary = getattr(llm, "message", getattr(llm, "choices", None))
        # Support different return shapes; prefer .message.content if present
        if hasattr(llm, "message"):
            raw_text = llm.message.content
        elif isinstance(llm, dict) and "message" in llm and "content" in llm["message"]:
            raw_text = llm["message"]["content"]
        else:
            # Fallback: try str
            raw_text = str(llm)

        data = extract_machine_json(raw_text)
        if not data:
            # Skip items that did not produce valid machine JSON
            continue

        human = extract_human_summary(raw_text)

        organized = {
            'raw': event,          # original feed item
            'readable': human,     # human summary bullet(s)
            'details': data        # machine JSON from the model
        }
        print(f"[{event.get('source',{}).get('name')}] {event.get('title')}: {human}\n")
        condensed.append(organized)

    print('[+] Saving Results')
    save_raw_stories(condensed, topic)

    # === NEW: feed raw stories to the tracker ===
    flat_stories = []
    for c in condensed:
        s = c['raw']
        flat_stories.append({
            "title": s.get("title",""),
            "link": s.get("link",""),
            "source": s.get("source", {}),
            "date": s.get("date",""),
            "ts": s.get("ts","")
        })

    tracker = EventTracker(cooldown_minutes=30, min_score=60)
    alerts, all_clusters = tracker.process(flat_stories)

    if alerts:
        print("\n[!] Alerts to investigate:")
        for a in alerts:
            print(f"- {a['status'].upper()} | score={a['score']} | {a['primary_title']}")
            print(f"  {a['primary_link']}")
    else:
        print("\n[=] No new alerts this run.")

    out_dir = ensure_topic_dir(topic)
    snapshot_path = os.path.join(out_dir, f"clusters_{int(time.time())}.json")
    with open(snapshot_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(all_clusters, indent=2, ensure_ascii=False))
    print(f"[+] Cluster snapshot saved to {snapshot_path}")