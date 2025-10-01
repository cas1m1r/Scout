import os
import time
import json
import threading
from dataclasses import dataclass, field
from typing import Dict, Set
from queue import Queue, Empty
from datetime import datetime, timezone
from hashlib import md5

from flask import Flask, render_template, request, jsonify, Response, redirect, url_for

from main import (
    check_google_rss,
    save_raw_stories,
    load_pre_prompt,
    summarize_story,
    extract_machine_json,
    extract_human_summary,
)

# --- app setup ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.secret_key = os.environ.get('APP_SECRET_KEY', 'change-me')


@dataclass
class SearchStatus:
    topic: str
    depth: int
    status: str = "initialized"
    total: int = 0
    processed: int = 0
    progress: float = 0.0
    started_at: float = field(default_factory=time.time)
    error: str | None = None
    cycle: int = 0
    new_this_cycle: int = 0
    polling_interval: int = 300  # seconds between cycles
    should_stop: bool = False

    def to_dict(self):
        elapsed = time.time() - self.started_at
        return {
            "topic": self.topic,
            "depth": self.depth,
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "progress": round(self.progress, 2),
            "elapsed": int(elapsed),
            "error": self.error,
            "cycle": self.cycle,
            "new_this_cycle": self.new_this_cycle,
            "polling_interval": self.polling_interval,
        }


# in-memory registries
active_searches: Dict[str, SearchStatus] = {}
stream_queues: Dict[str, Queue] = {}
search_results: Dict[str, dict] = {}
seen_stories: Dict[str, Set[str]] = {}  # search_id -> set of story fingerprints


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_async(coro):
    """Run an async coroutine in a dedicated loop inside a worker thread."""
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        except Exception:
            pass


def _sse(event: str, payload) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def story_fingerprint(item):
    """Create unique fingerprint for deduplication."""
    # Normalize URL (remove query params for Google News links)
    link = item.get('link', '')
    if 'news.google.com' in link:
        # Extract actual URL if possible
        link = link.split('&url=')[-1].split('&')[0]
    
    title = (item.get('title', '') or '').lower().strip()
    source = (item.get('source', {}) or {}).get('name', '').lower().strip()
    
    # Create fingerprint from normalized data
    fp_string = f"{link}|{title}|{source}"
    return md5(fp_string.encode()).hexdigest()


# ---------------- core worker ----------------
def run_continuous_search(topic: str, search_id: str, depth: int = 100, interval: int = 300):
    """Continuously poll for news, only processing new stories."""
    q = stream_queues[search_id]
    status = active_searches[search_id]
    system_prompt = load_pre_prompt()
    summarizer = os.environ.get("NEWS_SUMMARIZER", "gemma3:4b")
    url = os.environ.get("URL")
    
    if not url:
        status.status = "error"
        status.error = "URL environment variable not set"
        q.put({"type": "status", "status": status.status, "error": status.error, "progress": 100})
        q.put({"type": "done"})
        return

    seen = seen_stories.setdefault(search_id, set())
    all_condensed = []

    try:
        while not status.should_stop:
            status.cycle += 1
            status.status = f"fetching_cycle_{status.cycle}"
            status.new_this_cycle = 0
            status.processed = 0
            
            q.put({
                "type": "cycle_start",
                "cycle": status.cycle,
                "message": f"Starting cycle {status.cycle}..."
            })

            # Fetch latest items
            items = check_google_rss(topic, '24h', depth)
            
            # Filter out already-seen stories
            new_items = []
            for item in items:
                fp = story_fingerprint(item)
                if fp not in seen:
                    new_items.append(item)
                    seen.add(fp)
            
            status.total = len(new_items)
            status.new_this_cycle = len(new_items)
            
            q.put({
                "type": "cycle_info",
                "cycle": status.cycle,
                "total_fetched": len(items),
                "new_stories": len(new_items),
                "already_seen": len(items) - len(new_items)
            })

            if status.total == 0:
                q.put({
                    "type": "no_new_stories",
                    "cycle": status.cycle,
                    "message": f"No new stories in cycle {status.cycle}. Waiting {interval}s..."
                })
            else:
                status.status = f"processing_cycle_{status.cycle}"
                
                # Process only new stories
                for idx, item in enumerate(new_items, start=1):
                    if status.should_stop:
                        break
                        
                    status.processed = idx
                    
                    # Stream headline
                    q.put({
                        "type": "headline",
                        "idx": idx,
                        "total": status.total,
                        "cycle": status.cycle,
                        "title": item.get("title"),
                        "source": (item.get("source") or {}).get("name"),
                        "link": item.get("link"),
                        "date": item.get("date"),
                    })

                    # Summarize
                    try:
                        llm = _run_async(summarize_story(url, item, summarizer, system_prompt))
                        if hasattr(llm, "message"):
                            raw_text = llm.message.content
                        elif isinstance(llm, dict) and "message" in llm and "content" in llm["message"]:
                            raw_text = llm["message"]["content"]
                        else:
                            raw_text = str(llm)

                        data = extract_machine_json(raw_text) or {}
                        human = extract_human_summary(raw_text) or ""

                        condensed_item = {"raw": item, "readable": human, "details": data}
                        all_condensed.append(condensed_item)

                        q.put({
                            "type": "summary",
                            "idx": idx,
                            "cycle": status.cycle,
                            "readable": human,
                            "details": data,
                            "source": (item.get("source") or {}).get("name"),
                        })

                    except Exception as e:
                        q.put({
                            "type": "summary",
                            "idx": idx,
                            "cycle": status.cycle,
                            "readable": f"Error summarizing: {e}",
                            "details": {},
                            "source": (item.get("source") or {}).get("name"),
                        })

                    # Progress within cycle
                    status.progress = 5 + (90.0 * idx / max(1, status.total))
                    q.put({
                        "type": "progress",
                        "cycle": status.cycle,
                        "processed": status.processed,
                        "total": status.total,
                        "progress": round(status.progress, 2),
                    })

            # Save snapshot after each cycle
            try:
                if all_condensed:
                    save_raw_stories(all_condensed, topic)
            except Exception:
                pass

            search_results[search_id] = {
                "topic": topic,
                "run_at": _now_iso(),
                "condensed": all_condensed,
                "total_cycles": status.cycle,
                "total_stories": len(all_condensed),
            }

            q.put({
                "type": "cycle_complete",
                "cycle": status.cycle,
                "new_stories": status.new_this_cycle,
                "total_unique": len(seen),
            })

            # Wait for next cycle or stop signal
            if not status.should_stop:
                status.status = "waiting"
                q.put({
                    "type": "waiting",
                    "cycle": status.cycle,
                    "wait_seconds": interval,
                    "message": f"Waiting {interval}s until next cycle..."
                })
                
                # Wait in small increments so we can check should_stop
                for _ in range(interval):
                    if status.should_stop:
                        break
                    time.sleep(1)

        # Stopped by user
        status.status = "stopped"
        q.put({
            "type": "stopped",
            "message": f"Monitoring stopped after {status.cycle} cycles",
            "total_stories": len(all_condensed)
        })

    except Exception as e:
        status.status = "error"
        status.error = str(e)
        q.put({"type": "error", "error": str(e)})
    finally:
        status.progress = 100
        q.put({"type": "done"})


# ---------------- routes ----------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/start_search', methods=['POST'])
def start_search():
    topic = request.form.get('topic', '').strip()
    depth = request.form.get('depth', '100')
    interval = request.form.get('interval', '300')  # seconds between cycles
    
    try:
        depth = int(depth)
    except Exception:
        depth = 100
        
    try:
        interval = int(interval)
    except Exception:
        interval = 300
    
    if not topic:
        return jsonify({"error": "Missing topic"}), 400

    search_id = f"{int(time.time())}-{len(active_searches)+1}"
    active_searches[search_id] = SearchStatus(
        topic=topic, 
        depth=depth,
        polling_interval=interval
    )
    stream_queues[search_id] = Queue()
    seen_stories[search_id] = set()

    thread = threading.Thread(
        target=run_continuous_search, 
        args=(topic, search_id, depth, interval), 
        daemon=True
    )
    thread.start()

    return jsonify({"search_id": search_id})


@app.route('/stop_search/<search_id>', methods=['POST'])
def stop_search(search_id):
    """Stop a continuous search."""
    status = active_searches.get(search_id)
    if not status:
        return jsonify({"error": "Search not found"}), 404
    
    status.should_stop = True
    return jsonify({"message": "Stop signal sent", "search_id": search_id})


@app.route('/stream/<search_id>')
def stream(search_id):
    q = stream_queues.get(search_id)
    if not q:
        return Response(_sse("error", {"error": "invalid search id"}), mimetype='text/event-stream')

    def gen():
        yield _sse("status", {"status": "connected", "progress": 0})
        while True:
            try:
                msg = q.get(timeout=30)
            except Empty:
                yield _sse("ping", {})
                continue

            if msg.get("type") == "done":
                yield _sse("done", msg)
                break
            else:
                yield _sse(msg.get("type"), msg)

    return Response(gen(), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route('/view_results/<search_id>')
def view_results(search_id):
    res = search_results.get(search_id)
    if not res:
        return redirect(url_for('index'))
    return render_template('results.html', results=res, topic=res.get("topic", ""))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
