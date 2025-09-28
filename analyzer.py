# file: analyzer.py
import json, math, re
from datetime import datetime
from dateutil import parser as dp

def _dt(s):
    try: return dp.parse(s)
    except: return None

def load_event_store(path="event_store.jsonl"):
    rows=[]
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                rows.append(json.loads(ln))
    return rows

def analyze(rows):
    # flatten
    flat=[]
    for r in rows:
        s=r.get("summary",{}) or {}
        comp=s.get("components",{}) or {}
        flat.append({
            "cluster_id": r.get("cluster_id"),
            "run_at": _dt(r.get("run_at")),
            "score": s.get("score",0) or 0,
            "freshness": comp.get("freshness",0) or 0,
            "corroboration": comp.get("corroboration",0) or 0,
            "specificity": comp.get("specificity",0) or 0,
            "impact": comp.get("impact",0) or 0,
            "status": s.get("status","unclear"),
            "count": s.get("count",0) or 0,
            "primary_title": s.get("primary_title",""),
        })
    # group by cluster
    by = {}
    for r in sorted(flat, key=lambda x:(x["cluster_id"], x["run_at"] or datetime.min)):
        by.setdefault(r["cluster_id"], []).append(r)

    def toks(cid):
        return set(t for t in re.split(r"[-_]", cid) if t and t not in {"the","a","an","of","to","in","and"})

    out=[]
    for cid, g in by.items():
        first, last = g[0], g[-1]
        hours = max(((last["run_at"]-first["run_at"]).total_seconds()/3600) if (first["run_at"] and last["run_at"]) else 0.0, 1e-6)
        momentum = (last["score"] - first["score"]) / hours
        status_changes = sum(1 for i in range(1,len(g)) if g[i]["status"] != g[i-1]["status"])
        corroborated = (last["corroboration"] >= 10) or (max(x["count"] for x in g) >= 3)
        murky = (last["status"] in {"unclear","announced"}) and (last["corroboration"] < 10) and (last["score"] < 60)
        out.append({
            "cluster_id": cid,
            "runs": len(g),
            "first_run": first["run_at"].isoformat() if first["run_at"] else None,
            "last_run": last["run_at"].isoformat() if last["run_at"] else None,
            "last_score": last["score"],
            "freshness_latest": last["freshness"],
            "corroboration_latest": last["corroboration"],
            "max_count": max(x["count"] for x in g),
            "status_latest": last["status"],
            "status_changes": status_changes,
            "momentum_per_hr": round(momentum,2),
            "primary_title_latest": last["primary_title"],
            "corroborated_flag": corroborated,
            "murky_flag": murky,
            "tokens": list(toks(cid)),
        })
    # simple topic grouping
    used=set(); groups={}
    items=sorted(out, key=lambda r: (-r["last_score"], -r["freshness_latest"]))
    gid=0
    for r in items:
        if r["cluster_id"] in used: continue
        gid+=1
        base=r
        base_set=set(base["tokens"])
        groups[r["cluster_id"]] = f"g{gid}"
        used.add(r["cluster_id"])
        for s in items:
            if s["cluster_id"] in used: continue
            j = len(base_set & set(s["tokens"])) / max(1, len(base_set | set(s["tokens"])))
            if j >= 0.55:
                groups[s["cluster_id"]] = f"g{gid}"
                used.add(s["cluster_id"])

    # priority (0..1)
    for r in out:
        r["topic_group"] = groups.get(r["cluster_id"])
        r["priority"] = (
            (r["freshness_latest"]/40.0)*0.35 +
            (r["corroboration_latest"]/20.0)*0.25 +
            (r["last_score"]/100.0)*0.25 +
            (max(0.0, r["momentum_per_hr"])/10.0)*0.15
        )
    out.sort(key=lambda r: r["priority"], reverse=True)
    return out
