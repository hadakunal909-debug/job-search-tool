"""
store.py — the brain's local knowledge base (KB). No DB, no AI. Pure JSON files under data/.

The KB grows over time and is what makes this a *learning* tool:
  profile.json     name/email
  resumes.json     [ {id,name,tags,content,created_at} ]            many résumés
  stories.json     [ {id,title,text,tags,skills,created_at,uses} ]  many STAR experiences
  lessons.json     [ {id,text,triggers,boost_story_ids,boost_terms,weight,source,created_at} ]
  companies.json   { domain: {name,about,values,looking_for,keywords,pages,fetched_at} }
  model.json       self-training model: {df:{term:docs}, n:int, assoc:{term:{story_id:w}}}
  history.json     [ {id,company,title,jd_terms,resume_id,story_ids,created_at} ]

Everything is defensive: a missing/corrupt file reads as empty, never raises.
"""
import os
import json
import uuid
import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")

_FILES = {
    "profile": "profile.json", "resumes": "resumes.json", "stories": "stories.json",
    "lessons": "lessons.json", "companies": "companies.json", "model": "model.json",
    "history": "history.json",
}


def _now():
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _path(key):
    return os.path.join(DATA_DIR, _FILES[key])


def _load(key, default):
    try:
        with open(_path(key), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _dump(key, obj):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass
    p = _path(key)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _new_id():
    return uuid.uuid4().hex


def _upsert(lst, rec, newest_first=False):
    rec = dict(rec)
    if not rec.get("id"):
        rec["id"] = _new_id()
    if not rec.get("created_at"):
        rec["created_at"] = _now()
    for i, r in enumerate(lst):
        if r.get("id") == rec["id"]:
            lst[i] = {**r, **rec}
            return rec, lst
    (lst.insert(0, rec) if newest_first else lst.append(rec))
    return rec, lst


# ---------------- profile ----------------
def get_profile():
    p = _load("profile", {})
    return p if isinstance(p, dict) else {}


def save_profile(fields):
    p = get_profile()
    for k in ("name", "email"):
        if k in fields:
            p[k] = fields[k]
    p["updated_at"] = _now()
    _dump("profile", p)
    return p


# ---------------- résumés (many) ----------------
def list_resumes():
    v = _load("resumes", [])
    return v if isinstance(v, list) else []


def get_resume(rid):
    return next((r for r in list_resumes() if r.get("id") == rid), None)


def save_resume(rec):
    rec, lst = _upsert(list_resumes(), rec)
    _dump("resumes", lst)
    return rec["id"]


def delete_resume(rid):
    _dump("resumes", [r for r in list_resumes() if r.get("id") != rid])


# ---------------- stories (many) ----------------
def list_stories():
    v = _load("stories", [])
    return v if isinstance(v, list) else []


def get_story(sid):
    return next((s for s in list_stories() if s.get("id") == sid), None)


def save_story(rec):
    rec, lst = _upsert(list_stories(), rec)
    _dump("stories", lst)
    return rec["id"]


def delete_story(sid):
    _dump("stories", [s for s in list_stories() if s.get("id") != sid])


def bump_story_uses(ids):
    lst = list_stories()
    idset = set(ids or [])
    for s in lst:
        if s.get("id") in idset:
            s["uses"] = int(s.get("uses", 0)) + 1
    _dump("stories", lst)


# ---------------- lessons (learned rules) ----------------
def list_lessons():
    v = _load("lessons", [])
    return v if isinstance(v, list) else []


def save_lesson(rec):
    rec, lst = _upsert(list_lessons(), rec, newest_first=True)
    _dump("lessons", lst)
    return rec["id"]


def delete_lesson(lid):
    _dump("lessons", [l for l in list_lessons() if l.get("id") != lid])


# ---------------- companies (accumulated research) ----------------
def list_companies():
    c = _load("companies", {})
    return c if isinstance(c, dict) else {}


def get_company(domain):
    if not domain:
        return None
    return list_companies().get(domain)


def put_company(domain, rec):
    if not domain:
        return
    c = list_companies()
    rec = dict(rec)
    rec["fetched_at"] = _now()
    c[domain] = rec
    _dump("companies", c)


# ---------------- self-training model ----------------
def load_model():
    m = _load("model", {})
    if not isinstance(m, dict):
        m = {}
    m.setdefault("df", {})        # term -> number of docs containing it
    m.setdefault("n", 0)          # number of docs seen
    m.setdefault("assoc", {})     # term -> {story_id: learned weight}
    return m


def save_model(m):
    _dump("model", m)


# ---------------- history (training signal) ----------------
def list_history():
    v = _load("history", [])
    return v if isinstance(v, list) else []


def add_event(rec):
    rec, lst = _upsert(list_history(), rec, newest_first=True)
    _dump("history", lst[:200])   # keep the last 200
    return rec["id"]
