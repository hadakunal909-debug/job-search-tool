"""
app.py — Job Match & Resume Tailor  (card-feed UI)

Run it:
    pip install -r requirements.txt
    streamlit run app.py        # opens http://localhost:8501

Two views:
  • FEED  — your scraped jobs as cards, each with a résumé↔JD match ring, an H1B
            badge, and Like / Hide / Apply actions (saved to user_jobs.json).
  • TAILOR — click "Tailor / View" on a card to open the resume editor for that job
            (match score, matched/missing keywords, optional AI tailoring, download).
"""
import os
import json
import hmac
import datetime
import requests
import streamlit as st
import core
import db   # storage layer: Supabase if configured, else local files
import auth  # password hashing for the multi-user login

st.set_page_config(page_title="Jobs — Match & Tailor", page_icon="🎯", layout="wide",
                   initial_sidebar_state="expanded")


# ============================================================
# Login — per-user accounts (admin-created). Each person has their OWN resume,
# match scores, and saved jobs. Fails CLOSED (no account => no access).
# ============================================================
def login_gate() -> bool:
    if st.session_state.get("user"):
        return True

    def attempt():
        u = (st.session_state.get("login_user") or "").strip()
        p = st.session_state.get("login_pw") or ""
        try:
            rec = db.get_user(u) if u else None
        except Exception as e:
            st.session_state["login_err"] = "Login backend error: %s" % e
            return
        if rec and auth.verify_password(p, rec.get("password_hash", "")):
            st.session_state["user"] = u
            st.session_state.pop("login_pw", None)      # don't keep the raw password
            st.session_state.pop("login_err", None)
        else:
            st.session_state["login_err"] = "bad"

    st.title("🔒 Sign in")
    st.text_input("Username", key="login_user")
    st.text_input("Password", type="password", key="login_pw", on_change=attempt)
    st.button("Sign in", type="primary", on_click=attempt)
    err = st.session_state.get("login_err")
    if err == "bad":
        st.error("😕 Wrong username or password.")
    elif err:
        st.error(err)
    st.caption("Accounts are created by the admin — there is no public sign-up.")
    return False


if not login_gate():
    st.stop()

USER = st.session_state["user"]
PAGE_SIZE = 6

# Load this user's resume once per session (drives match scoring + the Tailor view).
if "my_resume" not in st.session_state:
    _rec = db.get_user(USER) or {}
    st.session_state["my_resume"] = _rec.get("resume", "") or ""

# ============================================================
# Persistence: like / hide / applied  (PER-USER, via db.py)
# ============================================================
if "actions" not in st.session_state:                 # network read — only once per session
    st.session_state["actions"] = db.get_user_statuses(USER)
st.session_state.setdefault("selected_url", None)     # None = feed view
st.session_state.setdefault("visible", PAGE_SIZE)
st.session_state.setdefault("resume_area", "")
st.session_state.setdefault("jd_area", "")
st.session_state.setdefault("last_job_url", None)


def set_action(url, status):
    a = st.session_state.actions
    if a.get(url) == status:        # clicking the same status again clears it
        a.pop(url, None)
        db.set_user_status(USER, url, "")
    else:
        a[url] = status
        db.set_user_status(USER, url, status)


# ============================================================
# Data + scoring
# ============================================================
@st.cache_data(show_spinner=False)
def get_jobs():
    return db.load_jobs()


@st.cache_data(show_spinner=False)
def _idf():
    return core.load_idf()


def saved_resume():
    """This logged-in user's resume (edited in the 📄 My résumé view)."""
    return st.session_state.get("my_resume", "") or ""


@st.cache_data(show_spinner=False)
def jd_for(url):
    return core.fetch_jd(url)


@st.cache_data(show_spinner=False)
def score_for(url, resume_text):
    jd = jd_for(url)
    return core.match_resume(resume_text, jd)[0] if jd else 0


@st.cache_data(show_spinner=False)
def user_scores(resume_text):
    """{url: match%} for THIS resume, computed from each job's stored JD text.
    Returns {} when the resume is empty (feed then falls back to the stored score)."""
    if not (resume_text or "").strip():
        return {}
    idf = _idf()
    out = {}
    for j in get_jobs():
        u, jd = j.get("url", ""), (j.get("jd", "") or "")
        if u and jd:
            out[u] = core.skill_match(resume_text, jd, idf)[0]
    return out


def time_ago(stamp):
    try:
        then = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M")
    except Exception:
        return stamp or ""
    secs = (datetime.datetime.now() - then).total_seconds()
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    days = int(secs // 86400)
    if days < 14:
        return f"{days}d ago"
    if days < 60:
        return f"{days // 7}w ago"
    return f"{days // 30}mo ago"


ROLE_FILTERS = {
    "Coordinator": ("coordinator",),
    "Analyst": ("analyst",),
    "Project / Program Mgr": ("project manager", "program manager", "project management",
                              "program management", "scrum master", "pmo"),
    "Operations": ("operations",),
    "Associate / Jr": ("associate", "junior", "graduate", "rotational", "entry"),
}


# ============================================================
# Styling
# ============================================================
st.markdown("""
<style>
[data-testid="stToolbar"], #MainMenu, footer {visibility:hidden;}
.block-container {padding-top:1.4rem; max-width:1150px;}
.feedhdr {font-size:28px; font-weight:800; color:#10261D; margin:0 0 .1rem 0;}
.feedsub {color:#6b7a74; font-size:13px; margin-bottom:.6rem;}
.avatar {width:48px; height:48px; border-radius:12px; background:#E8F7F0; color:#0f7a52;
         font-weight:800; font-size:20px; display:flex; align-items:center; justify-content:center;
         overflow:hidden; border:1px solid #e3efe9;}
.avatar img {width:30px; height:30px; border-radius:6px;}
.jtitle {font-size:18px; font-weight:700; color:#10261D; line-height:1.25;}
.jcompany {color:#6b7a74; font-size:13px; margin-top:1px;}
.chips {display:flex; flex-wrap:wrap; gap:6px; margin:.5rem 0 .2rem 0;}
.chip {background:#F1F5F3; border-radius:14px; padding:3px 10px; font-size:12px; color:#3a4a44;}
.chip.h1b {background:#E3F9EE; color:#0c7a4e; font-weight:700;}
.chip.noh1b {background:#FBEFEF; color:#b4453c;}
.matchcard {background:#0E2A22; border-radius:14px; padding:14px 10px; display:flex;
            flex-direction:column; align-items:center; gap:7px;}
.ring {width:84px; height:84px; border-radius:50%; display:flex; align-items:center; justify-content:center;}
.hole {width:64px; height:64px; border-radius:50%; background:#0E2A22; display:flex;
       align-items:center; justify-content:center; color:#fff; font-weight:800; font-size:19px;}
.hole span {font-size:11px; margin-left:1px; font-weight:600;}
.mlabel {color:#bff3df; font-size:10.5px; font-weight:700; letter-spacing:.05em;}
/* buttons -> pill style */
div[data-testid="stButton"] > button {border-radius:20px; border:1px solid #dde5e1;
       padding:.28rem .85rem; font-size:13px;}
div[data-testid="stButton"] > button:hover {border-color:#16C47F; color:#0f7a52;}
div[data-testid="stLinkButton"] > a {border-radius:20px;}
</style>
""", unsafe_allow_html=True)


COMPANY_DOMAINS = {
    "Samsara": "samsara.com", "Stripe": "stripe.com", "Verkada": "verkada.com",
    "Brex": "brex.com", "Datadog": "datadoghq.com", "Instacart": "instacart.com",
    "SoFi": "sofi.com", "Scale AI": "scale.com", "Airbnb": "airbnb.com",
    "Databricks": "databricks.com", "Twilio": "twilio.com", "Robinhood": "robinhood.com",
    "Toast": "toasttab.com", "Checkr": "checkr.com", "Affirm": "affirm.com",
    "Flexport": "flexport.com", "MongoDB": "mongodb.com", "Okta": "okta.com",
    "Palantir": "palantir.com", "Ramp": "ramp.com", "Notion": "notion.so",
    "Vanta": "vanta.com", "Replit": "replit.com", "Cursor": "cursor.com",
    "Avery Dennison": "averydennison.com", "Experian": "experian.com",
    "Amazon": "amazon.com",
}


def avatar_html(company):
    domain = COMPANY_DOMAINS.get(company)
    if domain:
        src = "https://www.google.com/s2/favicons?domain=%s&sz=64" % domain
        return f'<div class="avatar"><img src="{src}" alt=""></div>'
    return f'<div class="avatar">{(company or "?").strip()[:1].upper()}</div>'


def badges_html(job):
    chips = []
    if job.get("location"):
        chips.append(f'<span class="chip">📍 {job["location"]}</span>')
    t = job.get("title", "").lower()
    if "intern" in t:
        chips.append('<span class="chip">🎓 Internship</span>')
    if "remote" in (job.get("location", "").lower()):
        chips.append('<span class="chip">🏠 Remote</span>')
    if job.get("sponsors_h1b") == "yes":
        chips.append('<span class="chip h1b">✅ Sponsors H1B</span>')
    elif job.get("sponsors_h1b") == "no":
        chips.append('<span class="chip noh1b">• No H1B data</span>')
    return '<div class="chips">' + "".join(chips) + "</div>"


def match_card_html(score):
    if score >= 55:
        color, label = "#16C47F", "STRONG MATCH"
    elif score >= 42:
        color, label = "#2FA8E0", "GOOD MATCH"
    else:
        color, label = "#E0913B", "FAIR MATCH"
    return f"""<div class="matchcard">
      <div class="ring" style="background:conic-gradient({color} {score * 3.6}deg, #24463c 0)">
        <div class="hole">{score}<span>%</span></div></div>
      <div class="mlabel">{label}</div></div>"""


# ============================================================
# "Update jobs" — kick off the GitHub Actions scraper on demand
# ============================================================
GITHUB_REPO = "hadakunal909-debug/job-search-tool"
SCRAPE_WORKFLOW = "scrape.yml"


def _gh_token() -> str:
    tok = os.environ.get("GH_TOKEN", "")
    if not tok:
        try:
            tok = st.secrets.get("gh_token", "")
        except Exception:
            tok = ""
    return tok


def trigger_scrape():
    """Ask GitHub to run the 'Scrape jobs' workflow now (workflow_dispatch).
    Returns (ok, message). The heavy scrape runs on GitHub's servers, not here."""
    token = _gh_token()
    if not token:
        return False, ("No GitHub token set, so the button can't start a scrape. Add a "
                       "GH_TOKEN env var (on Render) or gh_token in .streamlit/secrets.toml "
                       "(local).")
    try:
        r = requests.post(
            "https://api.github.com/repos/%s/actions/workflows/%s/dispatches"
            % (GITHUB_REPO, SCRAPE_WORKFLOW),
            headers={"Authorization": "Bearer %s" % token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json={"ref": "main"}, timeout=20)
    except Exception as e:
        return False, "Couldn't reach GitHub: %s" % e
    if r.status_code == 204:
        return True, "ok"
    if r.status_code in (401, 403):
        return False, "GitHub rejected the token (it needs Actions: read & write on this repo)."
    if r.status_code == 404:
        return False, "Repo or workflow not found (token can't see the repo, or wrong name)."
    return False, "GitHub returned %s: %s" % (r.status_code, r.text[:200])


# ============================================================
# Sidebar
# ============================================================
with st.sidebar:
    st.markdown("### 🎯 JobMatch")
    st.caption("Signed in as **%s**" % USER)
    if st.button("Log out", use_container_width=True):
        st.session_state.clear()
        st.rerun()
    nav = st.radio("Go to", ["🎯 Jobs feed", "📄 My résumé", "🏢 Sponsor careers"],
                   label_visibility="collapsed")
    if st.button("🔄 Reload jobs", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    if st.button("🛰️ Update jobs (scrape now)", use_container_width=True,
                 help="Runs your cloud scraper on GitHub and writes fresh jobs to the "
                      "database. Takes a few minutes — then click Reload jobs."):
        with st.spinner("Starting the scraper on GitHub…"):
            ok, msg = trigger_scrape()
        if ok:
            st.success("Scrape started ✓  Give it ~3–5 min, then click 🔄 Reload jobs.")
            st.markdown("[Watch progress on GitHub ↗](https://github.com/%s/actions)"
                        % GITHUB_REPO)
        else:
            st.error(msg)
    st.markdown("---")
    if core.ai_available():
        st.success("AI tailoring: enabled")
    else:
        st.info("AI tailoring is off. To enable: `pip install anthropic`, set "
                "`ANTHROPIC_API_KEY`, and restart.")
    st.caption("Match % = the share of each job's skills **your résumé** covers "
               "(set it in 📄 My résumé).")
    st.markdown("---")
    min_match = st.slider("Minimum match %", 0, 100, 45, step=5,
                          help="Hide jobs whose ATS keyword-match is below this (Recommended tab).")
    date_posted = st.selectbox("Date posted",
                               ["Any time", "Past 24 hours", "Past week", "Past month"])


jobs_all = get_jobs()
actions = st.session_state.actions

if not jobs_all and nav not in ("🏢 Sponsor careers", "📄 My résumé"):
    where = "Supabase" if db.using_supabase() else "jobs.csv"
    st.warning(f"No jobs found in {where}. Run `python scraper.py` and "
               f"`python score_jobs.py`, then click **Reload**.")
    st.stop()


# ============================================================
# TAILOR VIEW  (a single job opened from the feed)
# ============================================================
def render_tailor(job):
    if st.button("← Back to jobs"):
        st.session_state.selected_url = None
        st.rerun()

    st.markdown(f"<div class='feedhdr'>{job.get('title','')}</div>", unsafe_allow_html=True)
    meta = f"**{job.get('company','')}**"
    if job.get("location"):
        meta += f" · {job['location']}"
    st.markdown(meta)
    if job.get("sponsors_h1b") == "yes":
        st.success("✅ This company has sponsored H1B before")
    if job.get("url"):
        st.link_button("Open job posting ↗", job["url"], type="primary")
        applied = st.session_state.actions.get(job["url"]) == "applied"
        if st.button("✓ Applied" if applied else "Mark as applied"):
            set_action(job["url"], "applied"); st.rerun()

    # auto-fetch JD when the opened job changes
    if job.get("url") and st.session_state.last_job_url != job["url"]:
        with st.spinner("Fetching job description…"):
            st.session_state["jd_area"] = jd_for(job["url"])
        if not st.session_state["resume_area"]:
            st.session_state["resume_area"] = saved_resume()
        st.session_state.last_job_url = job["url"]

    left, right = st.columns(2, gap="large")
    with left:
        st.text_area("Job description (edit or paste the real JD for the best match)",
                     height=420, key="jd_area")
    with right:
        b1, b2 = st.columns(2)
        if b1.button("✨ Tailor to this JD (AI)", use_container_width=True,
                     disabled=not core.ai_available()):
            if st.session_state["jd_area"].strip():
                with st.spinner("Tailoring with Claude…"):
                    try:
                        st.session_state["resume_area"] = core.tailor_with_ai(
                            st.session_state["resume_area"], st.session_state["jd_area"])
                        st.rerun()
                    except Exception as e:
                        st.error(f"AI tailoring failed: {e}")
            else:
                st.warning("No job description to tailor against.")
        if b2.button("↩︎ Reset resume", use_container_width=True):
            st.session_state["resume_area"] = saved_resume()
            st.rerun()
        st.text_area("Your resume — edit freely (add the missing keywords where true)",
                     height=360, key="resume_area")
        d1, d2 = st.columns(2)
        try:
            d1.download_button("⬇︎ .docx",
                               core.resume_to_docx_bytes(st.session_state["resume_area"]),
                               file_name="resume_tailored.docx", use_container_width=True)
        except Exception:
            d1.caption("`pip install python-docx` for .docx")
        d2.download_button("⬇︎ .md", st.session_state["resume_area"],
                           file_name="resume_tailored.md", use_container_width=True)

    resume_now, jd_now = st.session_state["resume_area"], st.session_state["jd_area"]
    score, kw_have, kw_missing = core.skill_match(resume_now, jd_now, _idf())
    st.markdown("#### 🤖 ATS keyword match")
    st.progress(score / 100, text=f"{score}% of this job's key terms are in your resume")
    with st.expander(f"✗ Add these to your resume ({len(kw_missing)}) — JD keywords you're missing",
                     expanded=True):
        st.write(", ".join(kw_missing[:20]) if kw_missing else "—")
    with st.expander(f"✓ Keywords you already match ({len(kw_have)})"):
        st.write(", ".join(kw_have[:25]) if kw_have else "—")


# ============================================================
# FEED VIEW
# ============================================================
def render_feed():
    st.markdown("<div class='feedhdr'>🎯 Jobs</div>", unsafe_allow_html=True)

    resume = saved_resume()
    scores = user_scores(resume)              # {url: match%} for THIS user's resume
    if not resume.strip():
        st.info("📄 Add your résumé in **My résumé** (sidebar) to get match scores tailored to you.")

    hidden = {u for u, s in actions.items() if s == "hidden"}
    liked = {u for u, s in actions.items() if s == "liked"}
    applied = {u for u, s in actions.items() if s == "applied"}
    n_rec = len([j for j in jobs_all if j.get("url") not in hidden])
    st.markdown(f"<div class='feedsub'>Recommended {n_rec} · Liked {len(liked)} · "
                f"Applied {len(applied)} · Hidden {len(hidden)}</div>", unsafe_allow_html=True)

    view = st.segmented_control("View", ["Recommended", "Liked", "Applied", "Hidden"],
                                default="Recommended", key="view_ctl",
                                label_visibility="collapsed")
    c1, c2, c3, c4 = st.columns([2.2, 2.0, 1.5, 1.1])
    search = c1.text_input("Search", placeholder="🔎 Search by title or company",
                           label_visibility="collapsed")
    roles = c2.pills("Role", list(ROLE_FILTERS), selection_mode="multi",
                     label_visibility="collapsed")
    sort = c3.selectbox("Sort", ["Best match", "Newest", "Oldest", "Company A-Z"],
                        label_visibility="collapsed")
    h1b_only = c4.toggle("H1B only")

    # pick the list for the active tab
    by_status = {"Liked": liked, "Applied": applied, "Hidden": hidden}
    if view == "Recommended":
        jobs = [j for j in jobs_all if j.get("url") not in hidden]
    else:
        jobs = [j for j in jobs_all if j.get("url") in by_status[view]]

    def _score_val(j):
        s = scores.get(j.get("url", ""))
        if s is not None:
            return s
        ms = str(j.get("match_score", ""))    # fallback: stored score (job has no JD yet)
        return int(ms) if ms.isdigit() else -1

    total_before = len(jobs)
    # filters
    if search:
        s = search.lower()
        jobs = [j for j in jobs if s in (j.get("title", "") + " " + j.get("company", "")).lower()]
    if roles:
        wanted = tuple(kw for r in roles for kw in ROLE_FILTERS[r])
        jobs = [j for j in jobs if any(k in j.get("title", "").lower() for k in wanted)]
    if h1b_only:
        jobs = [j for j in jobs if j.get("sponsors_h1b") == "yes"]
    if min_match > 0 and view == "Recommended":
        jobs = [j for j in jobs if _score_val(j) >= min_match]
        st.caption(f"Showing {len(jobs)} of {total_before} jobs (match >= {min_match}%)")

    _windows = {"Past 24 hours": 1, "Past week": 7, "Past month": 30}
    if date_posted in _windows:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=_windows[date_posted])

        def _posted_dt(j):
            try:
                return datetime.datetime.strptime(j.get("found_date", ""), "%Y-%m-%d %H:%M")
            except Exception:
                return None
        jobs = [j for j in jobs if (_posted_dt(j) or datetime.datetime.min) >= cutoff]

    if sort == "Newest":
        jobs.sort(key=lambda j: j.get("found_date", ""), reverse=True)
    elif sort == "Oldest":
        jobs.sort(key=lambda j: j.get("found_date", ""))
    elif sort == "Company A-Z":
        jobs.sort(key=lambda j: (j.get("company", "").lower(), j.get("title", "").lower()))
    else:  # Best match
        jobs.sort(key=_score_val, reverse=True)

    # collapse duplicate postings (same title + company, different locations)
    _seen_tc, _dedup = set(), []
    for j in jobs:
        k = (j.get("title", "").strip().lower(), j.get("company", ""))
        if k in _seen_tc:
            continue
        _seen_tc.add(k)
        _dedup.append(j)
    jobs = _dedup

    if not jobs:
        st.info("No jobs at or above this match %. Lower 'Minimum match %' in the sidebar, "
                "or clear the search / role filters.")
        return

    visible = jobs[: st.session_state.visible]

    def card_score(job):
        s = scores.get(job.get("url", ""))
        if s is not None:
            return s
        ms = str(job.get("match_score", ""))
        return int(ms) if ms.isdigit() else score_for(job.get("url", ""), resume)

    for i, job in enumerate(visible):
        url = job.get("url", "")
        with st.container(border=True):
            a, mid, ring = st.columns([0.55, 3.3, 1.3])
            a.markdown(avatar_html(job.get("company", "")), unsafe_allow_html=True)
            with mid:
                ago = time_ago(job.get("found_date", ""))
                posted = f"🕒 Posted {ago}" if ago else ""
                st.markdown(
                    f"<div class='jcompany'>{posted}</div>"
                    f"<div class='jtitle'>{job.get('title','')}</div>"
                    f"<div class='jcompany'>{job.get('company','')}</div>"
                    + badges_html(job), unsafe_allow_html=True)
                bcols = st.columns([0.8, 0.8, 1.5, 1.6])
                if bcols[0].button("♥" if url in liked else "♡", key=f"like{i}", help="Like"):
                    set_action(url, "liked"); st.rerun()
                if bcols[1].button("🚫", key=f"hide{i}", help="Hide this job"):
                    set_action(url, "hidden"); st.rerun()
                if bcols[2].button("✎ Tailor", key=f"tailor{i}",
                                   help="Open the resume editor for this job"):
                    st.session_state.selected_url = url
                    st.session_state.last_job_url = None
                    st.rerun()
                if url:
                    bcols[3].link_button("Apply ↗", url, type="primary",
                                         use_container_width=True)
            ring.markdown(match_card_html(card_score(job)), unsafe_allow_html=True)

    if len(jobs) > st.session_state.visible:
        if st.button(f"Show more  ({len(jobs) - st.session_state.visible} left)",
                     use_container_width=True):
            st.session_state.visible += PAGE_SIZE
            st.rerun()


# ============================================================
# MY RÉSUMÉ VIEW — each user's own resume (drives their personal match scores)
# ============================================================
def render_resume():
    st.markdown("<div class='feedhdr'>📄 My résumé</div>", unsafe_allow_html=True)
    st.markdown("<div class='feedsub'>Paste your résumé as plain text. Every job's match ring "
                "is the share of that job's key skills your résumé covers — so keep this current. "
                "Only you can see it.</div>", unsafe_allow_html=True)
    txt = st.text_area("Your résumé", value=st.session_state.get("my_resume", ""),
                       height=460, key="my_resume_edit", label_visibility="collapsed")
    if st.button("💾 Save résumé", type="primary"):
        db.set_user_resume(USER, txt)
        st.session_state["my_resume"] = txt
        st.cache_data.clear()        # recompute everyone's-eye-view scores against the new résumé
        st.success("Saved ✓ Your match scores now reflect this résumé. Open the Jobs feed.")


# ============================================================
# SPONSOR CAREERS VIEW — career links for H1B sponsors (many aren't scrapeable)
# ============================================================
@st.cache_data(show_spinner=False)
def _careers_md():
    return open("careers_us.md", encoding="utf-8").read() if os.path.exists("careers_us.md") else ""


def render_careers():
    st.markdown("<div class='feedhdr'>🏢 Sponsor career links</div>", unsafe_allow_html=True)
    st.markdown("<div class='feedsub'>H1B / green-card sponsor employers (DOL data) with direct US "
                "apply links — for companies the scraper can't reach: banks, pharma, consulting, "
                "universities, Eightfold portals.</div>", unsafe_allow_html=True)
    md = _careers_md()
    if not md:
        st.info("careers_us.md is missing — run `python make_careers.py` to build it.")
        return
    q = st.text_input("Search", placeholder="🔎 Filter companies (e.g. Bloomberg, university, bank)",
                      label_visibility="collapsed")
    if q:
        ql = q.lower()
        hits = [ln for ln in md.splitlines() if ln.startswith("- ") and ql in ln.lower()]
        st.caption(f"{len(hits)} compan{'y' if len(hits) == 1 else 'ies'} match")
        st.markdown("\n".join(hits) if hits else "_No companies match._")
    else:
        st.markdown(md)


# ============================================================
# Route
# ============================================================
sel = st.session_state.selected_url
job_by_url = {j.get("url"): j for j in jobs_all}
if sel and sel in job_by_url:
    render_tailor(job_by_url[sel])
elif nav == "🏢 Sponsor careers":
    render_careers()
elif nav == "📄 My résumé":
    render_resume()
else:
    render_feed()
