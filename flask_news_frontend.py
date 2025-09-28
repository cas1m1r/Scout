import os
import glob
import json
import asyncio
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from queue import Queue, Empty
from typing import Dict

from flask import Flask, render_template, request, jsonify, Response, redirect, url_for
from dotenv import load_dotenv

# Bring back your full pipeline pieces
from main import (
    check_google_rss,
    save_raw_stories,
    ensure_topic_dir,
    load_pre_prompt,
    summarize_story,
    extract_machine_json,
    extract_human_summary,
)
from tracker import EventTracker, load_store

load_dotenv()

# ---------- Flask app with explicit template/static paths ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.secret_key = os.environ.get("APP_SECRET_KEY", "change-me")

# ---------- Models / registries ----------
@dataclass
class SearchStatus:
    topic: str
    depth: int
    status: str = "initialized"
    progress: float = 0.0
    processed_articles: int = 0
    total_articles: int = 0
    started_at: float = field(default_factory=time.time)
    results: dict | None = None
    error: str | None = None

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
            "error": self.error,
        }

active_searches: Dict[str, SearchStatus] = {}
search_results: Dict[str, dict] = {}
stream_queues: Dict[str, Queue] = {}

# ---------- Async helper ----------
def _run_async(coro):
    """Run an async coroutine safely inside a worker thread."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        except Exception:
            pass

def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def _sse(event: str, payload) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"

# ---------- Worker ----------
def run_news_search(topic: str, search_id: str, depth: int = 100):
    status = active_searches[search_id]
    q = stream_queues.get(search_id)

    try:
        # 1) Fetch RSS
        status.status = "fetching_rss"
        status.progress = 5
        if q: q.put({"type": "status", "status": status.status, "progress": status.progress})
        articles = check_google_rss(topic, "24h", depth)
        status.total_articles = len(articles)

        # 2) Load LLM prompt & model
        status.status = "loading_prompt"
        status.progress = 10
        if q: q.put({"type": "status", "status": status.status, "progress": status.progress})

        system_prompt = load_pre_prompt()
        summarizer = os.environ.get("NEWS_SUMMARIZER", "gemma3:4b")
        host_url = os.environ.get("URL")
        if not host_url:
            raise RuntimeError("URL environment variable not set (point it to your LLM host).")

        condensed = []

        # 3) Per-article summarization (stream results as we go)
        status.status = "processing_articles"
        if q: q.put({"type": "status", "status": status.status, "progress": status.progress})
        total = max(1, len(articles))
        for i, event in enumerate(articles):
            status.processed_articles = i + 1
            status.progress = 20 + (60 * (i + 1) / total)
            try:
                # Ask the LLM
                llm = _run_async(summarize_story(host_url, event, summarizer, system_prompt))

                # Extract raw response text
                raw_text = None
                if hasattr(llm, "message"):
                    raw_text = llm.message.content
                elif isinstance(llm, dict) and "message" in llm and "content" in llm["message"]:
                    raw_text = llm["message"]["content"]
                else:
                    raw_text = str(llm) if llm is not None else ""

                # Parse machine JSON + human summary
                data = extract_machine_json(raw_text) or {}
                human = extract_human_summary(raw_text) or ""

                organized = {"raw": event, "readable": human, "details": data}
                condensed.append(organized)

                if q:
                    q.put({
                        "type": "story",
                        "index": i + 1,
                        "total": status.total_articles,
                        "title": event.get("title"),
                        "source": (event.get("source") or {}).get("name"),
                        "readable": human
                    })
                    q.put({"type": "progress", "processed": status.processed_articles, "total": status.total_articles})

            except Exception as e:
                if q:
                    q.put({
                        "type": "story",
                        "index": i + 1,
                        "total": status.total_articles,
                        "title": event.get("title"),
                        "source": (event.get("source") or {}).get("name"),
                        "readable": f"Error summarizing: {e}"
                    })
                continue

        # 3b) Save raw snapshot
        try:
            save_raw_stories(condensed, topic)
        except Exception:
            pass

        # 4) Run tracker/clusterer
        status.status = "analyzing_clusters"
        status.progress = 85
        if q: q.put({"type": "status", "status": status.status, "progress": status.progress})

        flat_stories = []
        for c in condensed:
            s = c["raw"]
            flat_stories.append({
                "title": s.get("title", ""),
                "link": s.get("link", ""),
                "source": s.get("source", {}),
                "date": s.get("date", ""),
                "ts": s.get("ts", ""),
            })

        tracker = EventTracker(
            cooldown_minutes=int(os.environ.get("TRACKER_COOLDOWN_MIN", "30")),
            min_score=int(os.environ.get("TRACKER_MIN_SCORE", "60")),
        )
        alerts, all_clusters = tracker.process(flat_stories)

        # 5) Package + record results
        status.status = "finalizing"
        status.progress = 95
        if q: q.put({"type": "status", "status": status.status, "progress": status.progress})

        result_payload = {
            "topic": topic,
            "run_at": _now_iso(),
            "alerts": alerts,
            "clusters": all_clusters,
            "condensed": condensed,
        }
        search_results[search_id] = result_payload
        status.results = result_payload
        status.status = "completed"
        status.progress = 100

    except Exception as e:
        status.status = "error"
        status.error = str(e)

    finally:
        if q:
            q.put({"type": "done", "search_id": search_id})
            q.put(None)

# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/start_search", methods=["POST"])
def start_search():
    topic = request.form.get("topic", "").strip()
    depth = request.form.get("depth", "100")
    try:
        depth = int(depth)
    except Exception:
        depth = 100

    if not topic:
        return jsonify({"error": "Missing topic"}), 400

    search_id = f"{int(time.time())}-{len(active_searches) + 1}"
    active_searches[search_id] = SearchStatus(topic=topic, depth=depth)
    stream_queues[search_id] = Queue()

    thread = threading.Thread(target=run_news_search, args=(topic, search_id, depth), daemon=True)
    thread.start()

    return jsonify({"search_id": search_id})

@app.route("/search_status/<search_id>")
def search_status(search_id):
    status = active_searches.get(search_id)
    if not status:
        return jsonify({"error": "Search not found"}), 404
    return jsonify(status.to_dict())

@app.route("/stream/<search_id>")
def stream(search_id):
    q = stream_queues.get(search_id)
    if not q:
        return Response(_sse("error", {"error": "invalid search id"}), mimetype="text/event-stream")

    def gen():
        # Initial hello
        yield _sse("status", {"status": "connected"})
        while True:
            try:
                msg = q.get(timeout=30)
            except Empty:
                yield _sse("ping", {})
                continue

            if msg is None:
                break

            # Ensure event name is in "type"
            evt = msg.get("type", "message")
            yield _sse(evt, msg)

    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/view_results/<search_id>")
def view_results(search_id):
    results = search_results.get(search_id)
    if not results:
        status = active_searches.get(search_id)
        if status and status.status != "completed":
            return redirect(url_for("index"))
        return render_template("error.html", message="Results not found or job failed.")
    return render_template("results.html", results=results, search_id=search_id)

@app.route("/event_store")
def event_store():
    history = load_store()
    clusters = defaultdict(list)
    for rec in history:
        cid = rec.get("cluster_id", "unknown")
        clusters[cid].append(rec)

    cluster_summaries = []
    for cid, items in clusters.items():
        items = sorted(items, key=lambda x: x.get("run_at") or "", reverse=True)
        latest = items[0]
        cluster_events = [it for it in items if it.get("alerted_at")]
        if "summary" in latest:
            summary = latest["summary"]
            summary["event_count"] = len(cluster_events)
            summary["last_alert"] = latest.get("alerted_at")
            summary["last_run"] = latest.get("run_at")
            cluster_summaries.append(summary)

    cluster_summaries.sort(key=lambda x: x.get("score", 0), reverse=True)
    return render_template("event_store.html", clusters=cluster_summaries)

@app.route("/historical_data")
def historical_data():
    topics = []
    for name in os.listdir(BASE_DIR):
        path = os.path.join(BASE_DIR, name)
        if not os.path.isdir(path):
            continue
        if name.startswith(".") or name in ("__pycache__", "templates", "static"):
            continue
        has_data = glob.glob(os.path.join(path, "data_*.json"))
        has_clusters = glob.glob(os.path.join(path, "clusters_*.json"))
        if has_data or has_clusters:
            topics.append({
                "topic": name,
                "data_files": sorted([os.path.basename(p) for p in has_data], reverse=True),
                "cluster_files": sorted([os.path.basename(p) for p in has_clusters], reverse=True),
            })
    topics.sort(key=lambda t: t["topic"])
    return render_template("historical.html", topics=topics)

@app.route("/view_topic/<topic>")
def view_topic(topic):
    folder = os.path.join(BASE_DIR, topic)
    if not os.path.isdir(folder):
        return render_template("error.html", message=f"Topic folder '{topic}' not found.")

    data_files = sorted(glob.glob(os.path.join(folder, "data_*.json")), reverse=True)
    cluster_files = sorted(glob.glob(os.path.join(folder, "clusters_*.json")), reverse=True)

    latest_data = None
    latest_clusters = None

    if data_files:
        try:
            with open(data_files[0], "r", encoding="utf-8") as f:
                latest_data = json.load(f)
        except Exception:
            latest_data = None
    if cluster_files:
        try:
            with open(cluster_files[0], "r", encoding="utf-8") as f:
                latest_clusters = json.load(f)
        except Exception:
            latest_clusters = None

    return render_template("topic_view.html", topic=topic, latest_data=latest_data, latest_clusters=latest_clusters)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
