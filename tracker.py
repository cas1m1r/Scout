# tracker.py
import json
import os
from collections import defaultdict
from urllib.parse import urlparse
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta

STORE_PATH = "event_store.jsonl"  # append-only history

# --- Utilities ---------------------------------------------------------------

def now_gmt():
    return datetime.now(timezone.utc)

def to_iso(dt):
    if not dt:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def parse_rfc822_or_none(s):
    try:
        return parsedate_to_datetime(s)
    except Exception:
        return None

def domain_of(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""

def tokenize(s):
    return [t for t in "".join(ch.lower() if ch.isalnum() else " " for ch in (s or "")).split() if t]

def jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))

def normalize_title(title):
    title = (title or "").strip()
    parts = title.split(" - ")
    return parts[0].strip() if parts else title

def event_key(story):
    """
    Cheap, deterministic cluster key based on salient tokens.
    Add or trim tokens to suit your monitoring topics.
    """
    title = normalize_title(story.get("title", ""))
    toks = tokenize(title)
    important = [t for t in toks if t in {
        "ice","protest","protests","protesters","troops","deployment","deploys","enforcement",
        "clash","clashes","tear","gas","facility","detention","broadview","portland","oregon",
        "chicago","daley","plaza"
    }]
    # Keep first 6 "important" tokens; fallback to first 6 generic tokens.
    key_core = "-".join(important[:6] or toks[:6]) or "event"
    return key_core

def story_timestamp(story):
    dt = parse_rfc822_or_none(story.get("date",""))
    return dt or now_gmt()

# --- Scoring -----------------------------------------------------------------

def score_story(story, within_hours=12):
    """
    Freshness 0-40, Specificity 0-30, Corroboration (cluster-level), Impact 0-10.
    Returns (score_without_corroboration, components).
    """
    title = normalize_title(story.get("title",""))
    toks = tokenize(title)
    dt = story_timestamp(story)
    age_hours = (now_gmt() - dt).total_seconds() / 3600.0

    # Freshness
    fresh = 40 if age_hours <= within_hours else 15 if age_hours <= 24 else 5

    # Specificity
    has_site = any(w in toks for w in ["facility","detention","center","plaza"])
    has_action = any(w in toks for w in ["clash","clashes","tear","gas","deploy","deployment","deploys","enforcement"])
    specificity = (15 if has_site else 0) + (15 if has_action else 0)

    # Impact
    impact = 0
    if any(w in toks for w in ["arrests","closure","closures","curfew","injuries","injury"]):
        impact = 10
    elif any(w in toks for w in ["traffic","shutdown","evacuation","evacuations"]):
        impact = 7

    base = fresh + specificity + impact
    return min(base, 100), {
        "freshness": fresh,
        "specificity": specificity,
        "corroboration": 0,  # set later at cluster level
        "impact": impact,
        "age_hours": round(age_hours, 2)
    }

# --- Event store -------------------------------------------------------------

def load_store(path=STORE_PATH):
    items = []
    if not os.path.exists(path):
        return items
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                continue
    return items

def append_store(obj, path=STORE_PATH):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=True) + "\n")

# --- Tracking model ----------------------------------------------------------

class EventTracker:
    """
    Maintains clusters, scores, and state transitions across runs.
    Emits alerts only on meaningful changes.
    """
    def __init__(self, cooldown_minutes=30, min_score=60):
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self.min_score = min_score
        self.history = load_store()
        self.last_alert = {}  # cluster_id -> datetime

        for h in self.history:
            if "cluster_id" in h and h.get("alerted_at"):
                try:
                    self.last_alert[h["cluster_id"]] = datetime.fromisoformat(
                        h["alerted_at"].replace("Z","+00:00")
                    )
                except Exception:
                    pass

    def cluster_batch(self, stories):
        clusters = defaultdict(lambda: {
            "cluster_id": None,
            "stories": [],
            "first_seen_gmt": None,
            "last_seen_gmt": None,
            "domains": set(),
        })
        for s in stories:
            cid = event_key(s)
            dt = story_timestamp(s)
            c = clusters[cid]
            c["cluster_id"] = cid
            c["stories"].append(s)
            dom = domain_of(s.get("link",""))
            if dom:
                c["domains"].add(dom)
            if c["first_seen_gmt"] is None or dt < c["first_seen_gmt"]:
                c["first_seen_gmt"] = dt
            if c["last_seen_gmt"] is None or dt > c["last_seen_gmt"]:
                c["last_seen_gmt"] = dt
        # finalize sets
        for c in clusters.values():
            c["domains"] = list(sorted(c["domains"]))
        return clusters

    def cluster_score(self, cluster):
        stories = cluster["stories"]
        if not stories:
            return 0, {"freshness":0,"specificity":0,"corroboration":0,"impact":0}
        comps = {"freshness":0,"specificity":0,"corroboration":0,"impact":0}
        scores = []
        for s in stories:
            sc, parts = score_story(s)
            scores.append(sc)
            for k in ("freshness","specificity","impact"):
                comps[k] += parts[k]
        n = max(len(stories), 1)
        for k in ("freshness","specificity","impact"):
            comps[k] = int(round(comps[k] / n))
        # corroboration: distinct domains (cap 4) * 5 => up to 20
        corroboration = min(len(cluster.get("domains", [])), 4) * 5
        comps["corroboration"] = corroboration
        final_score = min(int(round(sum(scores) / n)) + corroboration, 100)
        return final_score, comps

    def infer_status(self, cluster):
        toks = set()
        for s in cluster["stories"]:
            toks.update(tokenize(normalize_title(s.get("title",""))))
        if {"deploy","deployment","deploys","announced","announce","announces"} & toks:
            return "announced"
        if {"clash","clashes","tear","gas","enforcement"} & toks:
            return "ongoing"
        return "unclear"

    def summarize_cluster(self, cluster, score, comps):
        latest = max(cluster["stories"], key=lambda s: story_timestamp(s))
        return {
            "cluster_id": cluster["cluster_id"],
            "score": score,
            "components": comps,
            "status": self.infer_status(cluster),
            "first_seen_gmt": to_iso(cluster["first_seen_gmt"]),
            "last_seen_gmt": to_iso(cluster["last_seen_gmt"]),
            "domains": cluster["domains"],
            "primary_title": normalize_title(latest.get("title","")),
            "primary_link": latest.get("link",""),
            "count": len(cluster["stories"]),
        }

    def should_alert(self, summary):
        cid = summary["cluster_id"]
        score = summary["score"]
        status = summary["status"]

        if score < self.min_score:
            return False, "below_threshold"

        last = self.last_alert.get(cid)
        now = now_gmt()
        if last and (now - last) < self.cooldown:
            return False, "cooldown"

        if status in ("ongoing", "announced"):
            return True, "status_trigger"

        if score >= (self.min_score + 10):
            return True, "score_trigger"

        return False, "no_rule"

    def process(self, stories):
        alerts = []
        clusters = self.cluster_batch(stories)
        summaries = []

        for cid, c in clusters.items():
            score, comps = self.cluster_score(c)
            summary = self.summarize_cluster(c, score, comps)
            summaries.append(summary)

            go, reason = self.should_alert(summary)
            record = {
                "cluster_id": cid,
                "summary": summary,
                "reason": reason,
                "run_at": to_iso(now_gmt()),
                "alerted_at": to_iso(now_gmt()) if go else None
            }
            append_store(record)
            if go:
                self.last_alert[cid] = datetime.fromisoformat(record["alerted_at"].replace("Z","+00:00"))
                alerts.append(summary)

        return alerts, summaries
