from flask import Flask, render_template, request, jsonify, redirect, url_for, Response
import asyncio
import json
import os
import glob
from datetime import datetime, timezone
import threading
import time
from collections import defaultdict
from queue import Queue, Empty

# Import existing modules
from main import check_google_rss, save_raw_stories, ensure_topic_dir, load_pre_prompt, summarize_story, \
    extract_machine_json, extract_human_summary
from tracker import EventTracker, load_store
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('APP_SECRET_KEY', 'change-me')

# --------------------------------------------------------------------------------------
# Search status + in-memory registries
# --------------------------------------------------------------------------------------

class SearchStatus:
    def __init__(self, topic):
        self.topic = topic
        self.status = "initialized"
        self.progress = 0.0
        self.processed_articles = 0
        self.total_articles = 0
        self.started_at = time.time()
        self.results = None
        self.error = None

    def to_dict(self):
        elapsed = time.time() - self.started_at if self.started_at else 0
        return {
            "topic": self.topic,
            "status": self.status,
            "progress": round(self.progress, 2),
            "processed_articles": self.processed_articles,
            "total_articles": self.total_articles,
            "elapsed_time": round(elapsed, 1),
            "has_results": self.results is not None,
            "error": self.error
        }

active_searches = {}   # search_id -> SearchStatus
search_results = {}    # search_id -> dict (alerts, clusters, raw)
stream_queues = {}     # search_id -> Queue for SSE

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def _run_async(coro):
    """Run an async coroutine safely inside a worker thread."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        except Exception:
            pass

# --------------------------------------------------------------------------------------
# Core worker
# --------------------------------------------------------------------------------------

def run_news_search(topic, search_id):
    status = active_searches[search_id]
    q = stream_queues.get(search_id)

    try:
        # 1) Fetch RSS
        status.status = "fetching_rss"
        status.progress = 5
        if q: q.put({"type": "status", "status": status.status, "progress": status.progress})
        protest_news = check_google_rss(topic, '24h')
        status.total_articles = len(protest_news)

        # 2) Load prompt + model info
        status.status = "loading_prompt"
        status.progress = 10
        if q: q.put({"type": "status", "status": status.status, "progress": status.progress})
        system_prompt = load_pre_prompt()
        summarizer = os.environ.get("NEWS_SUMMARIZER", "gemma3:4b")
        url = os.environ.get('URL')
        if not url:
            raise RuntimeError("URL environment variable not set. Put your model host URL in .env as URL=...")

        condensed = []

        # 3) Process articles
        status.status = "processing_articles"
        if q: q.put({"type": "status", "status": status.status, "progress": status.progress})
        for i, event in enumerate(protest_news):
            status.processed_articles = i + 1
            # 20% -> 80% over the loop
            status.progress = 20 + (60 * (i + 1) / max(1, len(protest_news)))
            try:
                llm = _run_async(summarize_story(url, event, summarizer, system_prompt))

                # Extract raw text from LLM response
                if hasattr(llm, "message"):
                    raw_text = llm.message.content
                elif isinstance(llm, dict) and "message" in llm and "content" in llm["message"]:
                    raw_text = llm["message"]["content"]
                else:
                    raw_text = str(llm)

                data = extract_machine_json(raw_text)
                if not data:
                    # Skip if no machine JSON block
                    if q:
                        q.put({
                            "type": "story",
                            "index": i + 1,
                            "total": len(protest_news),
                            "title": event.get("title"),
                            "source": (event.get("source") or {}).get("name"),
                            "readable": "Skipped (no machine JSON parsed)"
                        })
                    continue

                human = extract_human_summary(raw_text)

                organized = {
                    'raw': event,
                    'readable': human,
                    'details': data
                }
                condensed.append(organized)

                # Push live update
                if q:
                    q.put({
                        "type": "story",
                        "index": i + 1,
                        "total": len(protest_news),
                        "title": event.get("title"),
                        "source": (event.get("source") or {}).get("name"),
                        "readable": human
                    })

            except Exception as e:
                # Continue to next story
                if q:
                    q.put({
                        "type": "story",
                        "index": i + 1,
                        "total": len(protest_news),
                        "title": event.get("title"),
                        "source": (event.get("source") or {}).get("name"),
                        "readable": f"Error summarizing: {e}"
                    })
                continue

        # Save raw snapshots
        try:
            save_raw_stories(condensed, topic)
        except Exception:
            pass

        # 4) Run tracker
        status.status = "analyzing_clusters"
        status.progress = 85
        if q: q.put({"type": "status", "status": status.status, "progress": status.progress})

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

        tracker = EventTracker(cooldown_minutes=int(os.environ.get("TRACKER_COOLDOWN_MIN", "30")),
                               min_score=int(os.environ.get("TRACKER_MIN_SCORE", "60")))
        alerts, all_clusters = tracker.process(flat_stories)

        # 5) Package results
        status.status = "finalizing"
        status.progress = 95
        if q: q.put({"type": "status", "status": status.status, "progress": status.progress})

        search_results[search_id] = {
            "topic": topic,
            "run_at": _now_iso(),
            "alerts": alerts,
            "clusters": all_clusters,
            "condensed": condensed
        }

        status.results = search_results[search_id]
        status.status = "completed"
        status.progress = 100

    except Exception as e:
        status.status = "error"
        status.error = str(e)

    finally:
        # tell any streams to finish
        if q:
            q.put({"type": "done", "search_id": search_id})
            q.put(None)

# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start_search', methods=['POST'])
def start_search():
    topic = request.form.get('topic', '').strip()
    if not topic:
        return jsonify({"error": "Missing topic"}), 400

    search_id = f"{int(time.time())}-{len(active_searches) + 1}"
    active_searches[search_id] = SearchStatus(topic)
    stream_queues[search_id] = Queue()

    # Spawn worker thread
    thread = threading.Thread(target=run_news_search, args=(topic, search_id), daemon=True)
    thread.start()

    return jsonify({"search_id": search_id})

@app.route('/search_status/<search_id>')
def search_status(search_id):
    status = active_searches.get(search_id)
    if not status:
        return jsonify({"error": "Search not found"}), 404
    return jsonify(status.to_dict())

@app.route('/stream/<search_id>')
def stream(search_id):
    if search_id not in active_searches or search_id not in stream_queues:
        return Response("Not found", status=404)

    status = active_searches[search_id]
    q = stream_queues[search_id]

    def event_stream():
        init = {
            "type": "status",
            "status": status.status,
            "progress": status.progress,
            "processed": status.processed_articles,
            "total": status.total_articles
        }
        yield f"data: {json.dumps(init)}\n\n"
        while True:
            try:
                item = q.get(timeout=20)
            except Empty:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return Response(event_stream(), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route('/view_results/<search_id>')
def view_results(search_id):
    results = search_results.get(search_id)
    if not results:
        status = active_searches.get(search_id)
        if status and status.status != "completed":
            return redirect(url_for('index'))
        return render_template('error.html', message="Results not found or job failed.")
    return render_template('results.html', results=results, search_id=search_id)

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
    # Each topic has its own folder (created by ensure_topic_dir)
    topics = []
    # Look at directories in CWD
    for name in os.listdir("."):
        if not os.path.isdir(name):
            continue
        # topic folders created by ensure_topic_dir are lowercased (no spaces)
        if name.startswith(".") or name in ("__pycache__", "templates", "static"):
            continue
        has_data = glob.glob(os.path.join(name, "data_*.json"))
        has_clusters = glob.glob(os.path.join(name, "clusters_*.json"))
        if has_data or has_clusters:
            topics.append({
                "topic": name,
                "data_files": sorted([os.path.basename(p) for p in has_data], reverse=True),
                "cluster_files": sorted([os.path.basename(p) for p in has_clusters], reverse=True),
            })

    topics.sort(key=lambda t: t["topic"])
    return render_template('historical.html', topics=topics)

@app.route('/view_topic/<topic>')
def view_topic(topic):
    folder = topic
    if not os.path.isdir(folder):
        return render_template('error.html', message=f"Topic folder '{topic}' not found.")

    data_files = sorted(glob.glob(os.path.join(folder, "data_*.json")), reverse=True)
    cluster_files = sorted(glob.glob(os.path.join(folder, "clusters_*.json")), reverse=True)

    latest_data = None
    latest_clusters = None

    if data_files:
        try:
            latest_data = json.loads(open(data_files[0], "r", encoding="utf-8").read())
        except Exception:
            latest_data = None
    if cluster_files:
        try:
            latest_clusters = json.loads(open(cluster_files[0], "r", encoding="utf-8").read())
        except Exception:
            latest_clusters = None

    return render_template('topic_view.html', topic=topic, latest_data=latest_data, latest_clusters=latest_clusters)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
