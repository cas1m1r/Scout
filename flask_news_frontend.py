
import os
import json
import glob
import time
import asyncio
import threading
from datetime import datetime, timezone
from collections import defaultdict
from queue import Queue, Empty

from flask import Flask, render_template, request, jsonify, redirect, url_for, Response

# Local imports
from main import (
    check_google_rss, save_raw_stories, ensure_topic_dir, load_pre_prompt,
    summarize_story, extract_machine_json, extract_human_summary
)
from tracker import EventTracker, load_store
from dotenv import load_dotenv
# in flask_news_frontend.py
from analyzer import load_event_store, analyze


load_dotenv()

# ---- Flask app paths (relative-safe) ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.secret_key = os.environ.get('APP_SECRET_KEY', 'change-me')

# ---- In-memory registries ----
class SearchStatus:
    def __init__(self, topic, depth):
        self.topic = topic
        self.depth = depth
        self.status = "initialized"
        self.progress = 0.0
        self.processed = 0
        self.total = 0
        self.started_at = time.time()
        self.error = None

    def to_dict(self):
        elapsed = time.time() - self.started_at if self.started_at else 0
        return {
            "topic": self.topic,
            "depth": self.depth,
            "status": self.status,
            "progress": round(self.progress, 2),
            "processed": self.processed,
            "total": self.total,
            "elapsed": round(elapsed, 1),
            "error": self.error
        }

active_searches = {}   # search_id -> SearchStatus
stream_queues  = {}    # search_id -> Queue (SSE)

# ---- helpers ----
def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def _sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"

# ---- routes ----
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start_search', methods=['POST'])
def start_search():
    topic = request.form.get('topic', '').strip()
    depth = request.form.get('depth', '100')
    try:
        depth = int(depth)
    except Exception:
        depth = 100
    if not topic:
        return jsonify({"error": "Missing topic"}), 400

    search_id = f"{int(time.time())}-{len(active_searches)+1}"
    active_searches[search_id] = SearchStatus(topic, depth)
    stream_queues[search_id] = Queue()

    thread = threading.Thread(target=_run_search_worker, args=(search_id,), daemon=True)
    thread.start()
    return jsonify({"search_id": search_id})

def _run_search_worker(search_id: str):
    """Thread target. Spins an asyncio loop for concurrent LLM calls."""
    status = active_searches[search_id]
    q = stream_queues[search_id]

    async def runner():
        try:
            # 1) Fetch RSS
            status.status = "fetching"
            status.progress = 5
            q.put({"type": "status", "status": status.status, "progress": status.progress})
            articles = check_google_rss(status.topic, '24h', status.depth)
            status.total = len(articles)
            if status.total == 0:
                status.status = "finished"
                status.progress = 100
                q.put({"type":"status", "status": status.status, "progress": status.progress})
                q.put({"type":"done"})
                return

            # 2) Load prompt + model info
            status.status = "loading_prompt"
            status.progress = 10
            q.put({"type": "status", "status": status.status, "progress": status.progress})
            system_prompt = load_pre_prompt()
            summarizer = os.environ.get("NEWS_SUMMARIZER", "gemma3:4b")
            url = os.environ.get("URL")
            if not url:
                raise RuntimeError("URL env var missing; set URL=http://... in .env")

            # 3) Stream HEADLINES immediately (stubs)
            status.status = "streaming_headlines"
            status.progress = 15
            q.put({"type": "status", "status": status.status, "progress": status.progress})
            for idx, item in enumerate(articles, start=1):
                q.put({
                    "type":"headline",
                    "idx": idx,
                    "total": status.total,
                    "title": item.get("title"),
                    "source": (item.get("source") or {}).get("name"),
                    "link": item.get("link"),
                    "date": item.get("date"),
                })

            # 4) Summarize concurrently with bounded parallelism
            status.status = "summarizing"
            status.progress = 20
            q.put({"type": "status", "status": status.status, "progress": status.progress})

            concurrency = int(os.environ.get("LLM_CONCURRENCY", "4"))
            sem = asyncio.Semaphore(concurrency)

            condensed = [None] * status.total  # preserve order

            async def do_one(i, event):
                nonlocal condensed
                async with sem:
                    try:
                        llm = await summarize_story(url, event, summarizer, system_prompt)
                        if hasattr(llm, "message"):
                            raw_text = llm.message.content
                        elif isinstance(llm, dict) and "message" in llm and "content" in llm["message"]:
                            raw_text = llm["message"]["content"]
                        else:
                            raw_text = str(llm)
                        machine = extract_machine_json(raw_text) or {}
                        human   = extract_human_summary(raw_text) or ""
                        condensed[i-1] = {"raw": event, "readable": human, "details": machine}
                        # emit summary incrementally
                        q.put({"type":"summary","idx": i, "readable": human})
                    except Exception as e:
                        condensed[i-1] = {"raw": event, "readable": f"Error summarizing: {e}", "details": {}}
                        q.put({"type":"summary","idx": i, "readable": f"Error summarizing: {e}"})
                    finally:
                        status.processed += 1
                        # progress maps 20->85 during summarization
                        status.progress = 20 + (65 * status.processed / max(1, status.total))
                        q.put({"type":"progress","processed": status.processed, "total": status.total})
                        q.put({"type":"status","status": status.status, "progress": status.progress})

            await asyncio.gather(*(do_one(i, art) for i, art in enumerate(articles, start=1)))

            # 5) Save raw snapshots
            try:
                save_raw_stories(condensed, status.topic)
            except Exception:
                pass

            # 6) Tracker/alerts (post-processing)
            status.status = "analyzing"
            status.progress = 90
            q.put({"type":"status","status": status.status, "progress": status.progress})

            flat = []
            for c in condensed:
                s = c["raw"]
                flat.append({
                    "title": s.get("title",""),
                    "link": s.get("link",""),
                    "source": s.get("source", {}),
                    "date": s.get("date",""),
                    "ts": s.get("ts","")
                })

            tracker = EventTracker(
                cooldown_minutes=int(os.environ.get("TRACKER_COOLDOWN_MIN","30")),
                min_score=int(os.environ.get("TRACKER_MIN_SCORE","60"))
            )
            alerts, all_clusters = tracker.process(flat)

            # 7) Done
            status.status = "finished"
            status.progress = 100
            q.put({"type":"status","status": status.status, "progress": status.progress})
            q.put({"type":"done"})

        except Exception as e:
            status.status = "error"
            status.error = str(e)
            q.put({"type":"status","status": status.status, "error": status.error})
            q.put({"type":"done"})

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner())
    finally:
        try:
            loop.close()
        except Exception:
            pass

@app.route('/second_stage')
def second_stage():
    try:
        rows = load_event_store(os.path.join(app.root_path, 'event_store.jsonl'))
        results = analyze(rows)
        return render_template('results.html', second_stage=results)  # or a new template
    except Exception as e:
        return render_template('error.html', message=f"Second-stage analysis failed: {e}")

@app.route('/second_stage.json')
def second_stage_json():
    rows = load_event_store(os.path.join(app.root_path, 'event_store.jsonl'))
    return jsonify(analyze(rows))


@app.route('/stream/<search_id>')
def stream(search_id):
    q = stream_queues.get(search_id)
    if not q:
        return Response(_sse("error", {"error":"invalid search id"}), mimetype='text/event-stream')

    def gen():
        yield _sse("status", {"status": "connected"})
        while True:
            try:
                msg = q.get(timeout=30)
            except Empty:
                yield _sse("ping", {})
                continue
            if msg is None or msg.get("type") == "done":
                yield _sse("done", {})
                break
            yield _sse(msg.get("type","message"), msg)

    return Response(gen(), mimetype='text/event-stream',
                    headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})

# --- Optional historical/event views (kept) ---
@app.route('/event_store')
def event_store():
    history = load_store()
    clusters = defaultdict(list)
    for rec in history:
        cid = rec.get('cluster_id', 'unknown')
        clusters[cid].append(rec)

    cluster_summaries = []
    for cid, items in clusters.items():
        items = sorted(items, key=lambda x: x.get('run_at') or '', reverse=True)
        latest = items[0]
        cluster_events = [it for it in items if it.get('alerted_at')]
        if 'summary' in latest:
            summary = latest['summary']
            summary['event_count'] = len(cluster_events)
            summary['last_alert'] = latest.get('alerted_at')
            summary['last_run'] = latest.get('run_at')
            cluster_summaries.append(summary)

    cluster_summaries.sort(key=lambda x: x.get('score', 0), reverse=True)
    return render_template('event_store.html', clusters=cluster_summaries)

@app.route('/historical_data')
def historical_data():
    topics = []
    for name in os.listdir(BASE_DIR):
        full = os.path.join(BASE_DIR, name)
        if not os.path.isdir(full):
            continue
        if name.startswith('.') or name in ('templates','static','__pycache__'):
            continue
        data_files = sorted(glob.glob(os.path.join(full, 'data_*.json')), reverse=True)
        if data_files:
            topics.append({"topic": name, "latest": os.path.basename(data_files[0])})
    topics.sort(key=lambda t: t['topic'])
    return render_template('historical.html', topics=topics)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
