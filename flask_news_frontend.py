import os
import time
import json
import threading
from dataclasses import dataclass, field
from typing import Dict
from queue import Queue, Empty
from datetime import datetime, timezone

from flask import Flask, render_template, request, jsonify, Response, redirect, url_for

# ---- project imports (as in your working copy) ----
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
        }


# in-memory registries
active_searches: Dict[str, SearchStatus] = {}
stream_queues: Dict[str, Queue] = {}
search_results: Dict[str, dict] = {}


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


# ---------------- core worker ----------------
def run_news_search(topic: str, search_id: str, depth: int = 100):
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

    try:
        status.status = "fetching_rss"
        status.progress = 3
        q.put({"type": "status", "status": status.status, "progress": status.progress})

        items = check_google_rss(topic, '24h', depth)
        status.total = len(items)

        status.status = "processing_articles"
        q.put({"type": "status", "status": status.status, "progress": status.progress})
        if status.total == 0:
            q.put({"type": "done"})
            return

        condensed = []

        for idx, item in enumerate(items, start=1):
            status.processed = idx
            # stream headline immediately
            q.put({
                "type": "headline",
                "idx": idx,
                "total": status.total,
                "title": item.get("title"),
                "source": (item.get("source") or {}).get("name"),
                "link": item.get("link"),
                "date": item.get("date"),
            })

            # summarize
            try:
                llm = _run_async(summarize_story(url, item, summarizer, system_prompt))
                # tolerate both llama_utils object and raw dict/text
                if hasattr(llm, "message"):
                    raw_text = llm.message.content
                elif isinstance(llm, dict) and "message" in llm and "content" in llm["message"]:
                    raw_text = llm["message"]["content"]
                else:
                    raw_text = str(llm)

                data = extract_machine_json(raw_text) or {}
                human = extract_human_summary(raw_text) or ""

                condensed.append({"raw": item, "readable": human, "details": data})

                # stream summary (include details for consensus UI)
                q.put({
                    "type": "summary",
                    "idx": idx,
                    "readable": human,
                    "details": data,
                    "source": (item.get("source") or {}).get("name"),
                })

            except Exception as e:
                q.put({
                    "type": "summary",
                    "idx": idx,
                    "readable": f"Error summarizing: {e}",
                    "details": {},
                    "source": (item.get("source") or {}).get("name"),
                })

            # progress
            status.progress = 5 + (90.0 * idx / max(1, status.total))
            q.put({
                "type": "progress",
                "processed": status.processed,
                "total": status.total,
                "progress": round(status.progress, 2),
            })

        # snapshot
        try:
            save_raw_stories(condensed, topic)
        except Exception:
            pass

        search_results[search_id] = {
            "topic": topic,
            "run_at": _now_iso(),
            "condensed": condensed,
        }

        status.status = "completed"
        status.progress = 100
        q.put({"type": "status", "status": status.status, "progress": status.progress})

    finally:
        q.put({"type": "done"})


# ---------------- routes ----------------
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
    active_searches[search_id] = SearchStatus(topic=topic, depth=depth)
    stream_queues[search_id] = Queue()

    thread = threading.Thread(target=run_news_search, args=(topic, search_id, depth), daemon=True)
    thread.start()

    return jsonify({"search_id": search_id})


@app.route('/stream/<search_id>')
def stream(search_id):
    q = stream_queues.get(search_id)
    if not q:
        return Response(_sse("error", {"error": "invalid search id"}), mimetype='text/event-stream')

    def gen():
        # initial ping
        yield _sse("status", {"status": "connected", "progress": 0})
        while True:
            try:
                msg = q.get(timeout=30)
            except Empty:
                yield _sse("ping", {})
                continue

            if msg.get("type") == "done":
                yield _sse("done", {})
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
