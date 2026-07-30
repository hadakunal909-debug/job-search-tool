// JobMatch — Tinder-style job-swipe phone app (Expo / React Native).
//
// A standalone native app that talks to your existing JobMatch backend over its token API
// (/api/app/login, /api/app/feed, /api/app/job, /api/app/action, /api/ext/tailor).
// Swipe RIGHT = apply (mark applied + open the company page + build a tailored résumé you can
// share/upload). Swipe LEFT = pass. Works in Expo Go — paste into snack.expo.dev or run locally.
//
// One thing to set: API_BASE below (your live backend). You can also override it on the login
// screen. The backend's /api/app/* + /api/ext/tailor routes must be deployed for this to work.

import React, { useState, useRef, useEffect, useCallback } from 'react';
import {
  View, Text, TextInput, TouchableOpacity, StyleSheet, Animated, PanResponder,
  Dimensions, Linking, ActivityIndicator, StatusBar, Alert, ScrollView, Modal, Platform, Share,
} from 'react-native';
// ------------------------------------------------------------------ config
const API_BASE = 'https://stemjobs1.astrochakra.co';   // <-- your live JobMatch backend

const { width: SCREEN_W } = Dimensions.get('window');
const SWIPE_THRESHOLD = SCREEN_W * 0.25;

const C = {
  bg: '#0f1117', surface: '#181b23', surface2: '#20242e', line: '#2a2f3a',
  ink: '#f2f4f8', muted: '#98a0b0', brand: '#6366f1', brandInk: '#c7d2fe',
  ok: '#22c55e', danger: '#ef4444', blue: '#3b82f6',
};

// ------------------------------------------------------------------ tiny in-memory storage
// (kept dependency-free so the app bundles anywhere; swap for expo-secure-store / AsyncStorage
// later if you want the login to persist across app restarts.)
const mem = {};
const store = {
  async get(k) { return mem[k] != null ? mem[k] : null; },
  async set(k, v) { mem[k] = v; },
  async del(k) { delete mem[k]; },
};

// ------------------------------------------------------------------ api
function makeApi(base, token) {
  const root = (base || API_BASE).replace(/\/+$/, '');
  const j = async (res) => { const d = await res.json().catch(() => ({})); return d; };
  return {
    async login(username, password) {
      const r = await fetch(root + '/api/app/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      return j(r);
    },
    async feed({ min = 0, offset = 0, limit = 20 } = {}) {
      const q = `token=${encodeURIComponent(token)}&min=${min}&sort=score&offset=${offset}&limit=${limit}`;
      const r = await fetch(root + '/api/app/feed?' + q);
      return j(r);
    },
    async job(url) {
      const r = await fetch(root + `/api/app/job?token=${encodeURIComponent(token)}&url=${encodeURIComponent(url)}`);
      return j(r);
    },
    async action(url, status) {
      const r = await fetch(root + '/api/app/action', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, url, status }),
      });
      return j(r);
    },
    async tailor(job) {
      const r = await fetch(root + '/api/ext/tailor', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, job_url: job.url, company: job.company || '', format: 'pdf' }),
      });
      return j(r);
    },
  };
}

// ------------------------------------------------------------------ demo mode (no backend needed)
const SAMPLE_JOBS = [
  { url: 'demo:1', apply_url: 'https://stripe.com/jobs', title: 'Technical Program Manager', company: 'Stripe', location: 'New York, NY', date: 'Today', score: 72, sponsors_h1b: 'yes', everify: true, sponsor_jd: 'open', exp_years: 3, have: ['project management', 'stakeholder', 'agile', 'sql'], missing: ['fintech', 'roadmap'], jd: 'Drive cross-functional programs across payments teams. Manage timelines, stakeholders, and delivery. Skills: program management, Agile, SQL, reporting.' },
  { url: 'demo:2', apply_url: 'https://ramp.com/careers', title: 'Project Manager, Operations', company: 'Ramp', location: 'Remote, US', date: 'Today', score: 66, sponsors_h1b: 'yes', sponsor_jd: 'open', exp_years: 2, have: ['operations', 'process improvement', 'excel'], missing: ['saas', 'okrs'], jd: 'Own operational projects end-to-end. Improve processes, build dashboards, coordinate vendors.' },
  { url: 'demo:3', apply_url: 'https://www.datadoghq.com/careers', title: 'Business Analyst', company: 'Datadog', location: 'Boston, MA', date: '1d ago', score: 61, sponsors_h1b: 'yes', everify: true, exp_years: 2, have: ['data analysis', 'sql', 'tableau'], missing: ['forecasting'], jd: 'Analyze business metrics, build reports in Tableau/SQL, partner with product and finance.' },
  { url: 'demo:4', apply_url: 'https://www.rockwellautomation.com/careers', title: 'Project Controls Analyst', company: 'Rockwell Automation', location: 'Milwaukee, WI', date: '3d ago', score: 51, sponsors_h1b: 'yes', sponsor_jd: 'open', exp_years: 3, have: ['scheduling', 'cost analysis', 'reporting'], missing: ['primavera', 'earned value'], jd: 'Support project controls: cost, schedule, and reporting across engineering programs.' },
  { url: 'demo:5', apply_url: 'https://www.tesla.com/careers', title: 'Project Management Intern', company: 'Tesla', location: 'Palo Alto, CA', date: 'Yesterday', score: 44, intern: true, exp_years: '', have: ['coordination', 'excel'], missing: ['manufacturing'], jd: 'Summer internship supporting PMO on cross-team initiatives and scheduling.' },
];
function demoApi() {
  const rows = SAMPLE_JOBS;
  const wait = (ms) => new Promise(r => setTimeout(r, ms));
  return {
    async login() { return { ok: true }; },
    async feed({ offset = 0 } = {}) { return { ok: true, rows: offset === 0 ? rows : [], total: rows.length, has_more: false }; },
    async job(url) { const jj = rows.find(r => r.url === url) || {}; return { ok: true, ...jj }; },
    async action() { return { ok: true }; },
    async tailor(job) {
      await wait(1200);
      return {
        ok: true, ai_used: false, notes: ['Demo résumé (sample). Deploy the backend for the real tailored PDF.'],
        file: { name: (job.company || 'Demo') + '_Resume.txt', mime: 'text/plain',
                text: 'JobMatch — demo tailored résumé\n\nSample for: ' + job.title + ' at ' + job.company + '.\n\nConnect the real backend to get the AI-tailored PDF built from your profile.' },
      };
    },
  };
}

// ================================================================== root
export default function App() {
  const [ready, setReady] = useState(false);
  const [auth, setAuth] = useState(null);      // {token, user, base}

  useEffect(() => {
    (async () => {
      const token = await store.get('jm_token');
      const user = await store.get('jm_user');
      const base = (await store.get('jm_base')) || API_BASE;
      if (token && user) setAuth({ token, user, base });
      setReady(true);
    })();
  }, []);

  const onLogin = async (a) => {
    await store.set('jm_token', a.token);
    await store.set('jm_user', a.user);
    await store.set('jm_base', a.base);
    setAuth(a);
  };
  const onLogout = async () => {
    await store.del('jm_token'); await store.del('jm_user');
    setAuth(null);
  };
  const startDemo = () => setAuth({ demo: true, user: 'demo', base: API_BASE, token: '' });

  return (
    <View style={styles.root}>
      <StatusBar barStyle="light-content" />
      {!ready ? (
        <View style={styles.center}><ActivityIndicator color={C.brand} size="large" /></View>
      ) : auth ? (
        <SwipeScreen auth={auth} onLogout={onLogout} />
      ) : (
        <LoginScreen onLogin={onLogin} onDemo={startDemo} />
      )}
    </View>
  );
}

// ================================================================== login
function LoginScreen({ onLogin, onDemo }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [base, setBase] = useState(API_BASE);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [adv, setAdv] = useState(false);

  const submit = async () => {
    if (!username || !password) { setErr('Enter your username and password.'); return; }
    setBusy(true); setErr('');
    try {
      const api = makeApi(base, '');
      const d = await api.login(username.trim(), password);
      if (d.ok && d.token) onLogin({ token: d.token, user: d.username, base });
      else setErr(d.error || 'Login failed.');
    } catch (e) { setErr('Could not reach the server. Check the address.'); }
    setBusy(false);
  };

  return (
    <ScrollView contentContainerStyle={[styles.center, { padding: 26 }]} keyboardShouldPersistTaps="handled">
      <Text style={styles.brandLogo}>Job<Text style={{ color: C.brand }}>Match</Text></Text>
      <Text style={styles.sub}>Swipe your way through jobs</Text>
      <TextInput style={styles.input} placeholder="Username" placeholderTextColor={C.muted}
        autoCapitalize="none" autoCorrect={false} value={username} onChangeText={setUsername} />
      <TextInput style={styles.input} placeholder="Password" placeholderTextColor={C.muted}
        secureTextEntry value={password} onChangeText={setPassword} onSubmitEditing={submit} />
      {adv && (
        <TextInput style={styles.input} placeholder="Server URL" placeholderTextColor={C.muted}
          autoCapitalize="none" autoCorrect={false} value={base} onChangeText={setBase} />
      )}
      {!!err && <Text style={styles.err}>{err}</Text>}
      <TouchableOpacity style={styles.primaryBtn} onPress={submit} disabled={busy}>
        {busy ? <ActivityIndicator color="#fff" /> : <Text style={styles.primaryBtnTxt}>Log in</Text>}
      </TouchableOpacity>
      <TouchableOpacity onPress={() => setAdv(v => !v)}>
        <Text style={styles.link}>{adv ? 'Hide server setting' : 'Advanced: change server'}</Text>
      </TouchableOpacity>
      {!!onDemo && (
        <TouchableOpacity style={[styles.ghostBtn, { marginTop: 22 }]} onPress={onDemo}>
          <Text style={styles.ghostBtnTxt}>Try the demo (no account)</Text>
        </TouchableOpacity>
      )}
    </ScrollView>
  );
}

// ================================================================== swipe deck
function SwipeScreen({ auth, onLogout }) {
  const api = useRef(auth.demo ? demoApi() : makeApi(auth.base, auth.token)).current;

  const [deck, setDeck] = useState([]);
  const [index, setIndex] = useState(0);
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(true);
  const [resumes, setResumes] = useState([]);     // {url,title,company,state,file,note,error}
  const [showResumes, setShowResumes] = useState(false);
  const [detail, setDetail] = useState(null);      // job detail modal data
  const [detailBusy, setDetailBusy] = useState(false);

  // refs so the (once-created) PanResponder always sees current state
  const deckRef = useRef([]); deckRef.current = deck;
  const indexRef = useRef(0); indexRef.current = index;
  const offsetRef = useRef(0);
  const hasMoreRef = useRef(true); hasMoreRef.current = hasMore;
  const loadingRef = useRef(false);
  const seen = useRef({});

  const position = useRef(new Animated.ValueXY()).current;

  const loadMore = useCallback(async (reset) => {
    if (loadingRef.current) return;
    if (!reset && !hasMoreRef.current) return;
    loadingRef.current = true; setLoading(true);
    if (reset) { offsetRef.current = 0; seen.current = {}; }
    try {
      const d = await api.feed({ min: 0, offset: offsetRef.current, limit: 20 });
      const rows = (d && d.rows) || [];
      offsetRef.current += rows.length;
      setHasMore(!!(d && d.has_more));
      const fresh = [];
      for (const jrow of rows) {
        if (!jrow || !jrow.url || seen.current[jrow.url]) continue;
        seen.current[jrow.url] = 1;
        if (jrow.status === 'hidden' || jrow.status === 'applied') continue;
        fresh.push(jrow);
      }
      if (reset) { setDeck(fresh); setIndex(0); }
      else if (fresh.length) setDeck(prev => prev.concat(fresh));
    } catch (e) { /* keep whatever we have */ }
    loadingRef.current = false; setLoading(false);
  }, [api]);

  useEffect(() => { loadMore(true); }, [loadMore]);
  // prefetch when the deck is running low
  useEffect(() => {
    if (!loadingRef.current && hasMoreRef.current && deck.length - index < 4) loadMore(false);
  }, [index, deck.length, loadMore]);

  // ---- résumé tailor queue (sequential) ----
  const tq = useRef([]); const working = useRef(false);
  const patchResume = (url, patch) =>
    setResumes(prev => prev.map(r => (r.url === url ? { ...r, ...patch } : r)));
  const pump = useCallback(async () => {
    if (working.current) return;
    const job = tq.current.shift(); if (!job) return;
    working.current = true; patchResume(job.url, { state: 'building' });
    try {
      const d = await api.tailor(job);
      if (d && d.ok && d.file) patchResume(job.url, { state: 'ready', file: d.file, note: (d.notes && d.notes[0]) || (d.ai_used ? 'AI-tailored' : 'Best-matching résumé') });
      else patchResume(job.url, { state: 'error', error: (d && d.error) || 'Tailoring failed.' });
    } catch (e) { patchResume(job.url, { state: 'error', error: 'Network error.' }); }
    working.current = false; pump();
  }, [api]);
  const enqueueTailor = (job) => {
    setResumes(prev => (prev.find(r => r.url === job.url) ? prev
      : [{ url: job.url, title: job.title, company: job.company, state: 'building' }, ...prev]));
    tq.current.push(job); pump();
  };

  const openApply = (job) => {
    const u = job.apply_url || job.url;
    if (u && /^https?:\/\//i.test(u)) Linking.openURL(u).catch(() => {});
  };

  // ---- the swipe action (kept in a ref so PanResponder never goes stale) ----
  const swipeRef = useRef(() => {});
  swipeRef.current = (dir) => {
    const job = deckRef.current[indexRef.current];
    if (!job) return;
    if (dir === 'right') { api.action(job.url, 'applied').catch(() => {}); openApply(job); enqueueTailor(job); }
    else { api.action(job.url, 'hidden').catch(() => {}); }
    setIndex(i => i + 1);
  };

  const forceSwipe = useCallback((dir) => {
    Animated.timing(position, {
      toValue: { x: dir === 'right' ? SCREEN_W * 1.4 : -SCREEN_W * 1.4, y: 0 },
      duration: 220, useNativeDriver: false,
    }).start(() => { position.setValue({ x: 0, y: 0 }); swipeRef.current(dir); });
  }, [position]);
  const reset = useCallback(() => {
    Animated.spring(position, { toValue: { x: 0, y: 0 }, friction: 6, useNativeDriver: false }).start();
  }, [position]);

  const openDetails = useCallback(async () => {
    const job = deckRef.current[indexRef.current]; if (!job) return;
    setDetail({ base: job }); setDetailBusy(true);
    try { const d = await api.job(job.url); setDetail({ base: job, data: d }); }
    catch (e) { setDetail({ base: job, data: { ok: false } }); }
    setDetailBusy(false);
  }, [api]);
  const openDetailsRef = useRef(openDetails); openDetailsRef.current = openDetails;

  const pan = useRef(
    PanResponder.create({
      onStartShouldSetPanResponder: () => true,
      onMoveShouldSetPanResponder: (e, g) => Math.abs(g.dx) > 4 || Math.abs(g.dy) > 4,
      onPanResponderMove: (e, g) => position.setValue({ x: g.dx, y: g.dy }),
      onPanResponderRelease: (e, g) => {
        if (g.dx > SWIPE_THRESHOLD) forceSwipe('right');
        else if (g.dx < -SWIPE_THRESHOLD) forceSwipe('left');
        else if (Math.abs(g.dx) < 6 && Math.abs(g.dy) < 6) { reset(); openDetailsRef.current(); }
        else reset();
      },
    })
  ).current;

  const shareResume = async (file) => {
    try {
      // Real mode serves the tailored PDF at a URL -> open it (Safari saves/shares/uploads it).
      if (file.url && /^https?:\/\//i.test(file.url)) { Linking.openURL(file.url); return; }
      // Demo / fallback: React Native's built-in share sheet (no extra native modules needed).
      await Share.share({ title: file.name || 'Résumé', message: file.text || 'Your tailored résumé is ready.' });
    } catch (e) { Alert.alert('Could not share', String(e).slice(0, 160)); }
  };

  const rotate = position.x.interpolate({ inputRange: [-SCREEN_W / 2, 0, SCREEN_W / 2], outputRange: ['-9deg', '0deg', '9deg'] });
  const likeOp = position.x.interpolate({ inputRange: [0, SWIPE_THRESHOLD], outputRange: [0, 1], extrapolate: 'clamp' });
  const nopeOp = position.x.interpolate({ inputRange: [-SWIPE_THRESHOLD, 0], outputRange: [1, 0], extrapolate: 'clamp' });

  const top = deck[index];
  const next = deck[index + 1];
  const doneForNow = !loading && !top;
  const readyCount = resumes.length;

  return (
    <View style={styles.appWrap}>
      {/* app bar */}
      <View style={styles.appBar}>
        <Text style={styles.appTitle}>Swipe</Text>
        <View style={{ flexDirection: 'row', alignItems: 'center' }}>
          {readyCount > 0 && (
            <TouchableOpacity style={styles.pill} onPress={() => setShowResumes(true)}>
              <Text style={styles.pillTxt}>Résumés</Text>
              <View style={styles.badge}><Text style={styles.badgeTxt}>{readyCount}</Text></View>
            </TouchableOpacity>
          )}
          <TouchableOpacity style={styles.iconBtn} onPress={onLogout}>
            <Text style={styles.iconTxt}>⎋</Text>
          </TouchableOpacity>
        </View>
      </View>

      {/* deck */}
      <View style={styles.stage}>
        {doneForNow && (
          <View style={styles.msg}>
            <Text style={styles.msgTxt}>You're all caught up.{'\n'}Check back after the next scrape.</Text>
            <TouchableOpacity style={styles.ghostBtn} onPress={() => loadMore(true)}>
              <Text style={styles.ghostBtnTxt}>Reload</Text>
            </TouchableOpacity>
          </View>
        )}
        {loading && !top && <ActivityIndicator color={C.brand} size="large" />}

        {next && <Card job={next} style={[styles.card, styles.cardBehind]} />}
        {top && (
          <Animated.View
            {...pan.panHandlers}
            style={[styles.card, { transform: [{ translateX: position.x }, { translateY: position.y }, { rotate }] }]}
          >
            <Animated.View style={[styles.stamp, styles.stampLike, { opacity: likeOp }]}><Text style={[styles.stampTxt, { color: C.ok }]}>APPLY</Text></Animated.View>
            <Animated.View style={[styles.stamp, styles.stampNope, { opacity: nopeOp }]}><Text style={[styles.stampTxt, { color: C.danger }]}>PASS</Text></Animated.View>
            <CardBody job={top} />
          </Animated.View>
        )}
      </View>

      {/* action bar */}
      <View style={styles.actionBar}>
        <RoundBtn label="✕" color={C.danger} onPress={() => top && forceSwipe('left')} disabled={!top} />
        <RoundBtn label="ⓘ" color={C.blue} small onPress={() => top && openDetails()} disabled={!top} />
        <RoundBtn label="✓" color={C.ok} big onPress={() => top && forceSwipe('right')} disabled={!top} />
      </View>
      <Text style={styles.hint}>Swipe right to apply · left to pass · tap a card for details</Text>

      {/* résumés modal */}
      <Modal visible={showResumes} animationType="slide" transparent onRequestClose={() => setShowResumes(false)}>
        <View style={styles.sheetWrap}>
          <View style={styles.sheet}>
            <View style={styles.sheetHead}>
              <Text style={styles.sheetTitle}>Tailored résumés</Text>
              <TouchableOpacity onPress={() => setShowResumes(false)}><Text style={styles.iconTxt}>✕</Text></TouchableOpacity>
            </View>
            <ScrollView style={{ maxHeight: 420 }}>
              {resumes.length === 0 && <Text style={styles.muted}>Swipe right to build résumés here.</Text>}
              {resumes.map(r => (
                <View key={r.url} style={styles.trRow}>
                  <Text style={styles.trTitle} numberOfLines={2}>{r.title}</Text>
                  <Text style={styles.trCo}>{r.company}</Text>
                  {r.state === 'building' && <View style={styles.rowc}><ActivityIndicator color={C.muted} /><Text style={styles.muted}>  Building…</Text></View>}
                  {r.state === 'ready' && (
                    <View style={styles.rowc}>
                      <TouchableOpacity style={styles.smallBtn} onPress={() => shareResume(r.file)}><Text style={styles.smallBtnTxt}>Share / Save</Text></TouchableOpacity>
                      <Text style={styles.mutedSm}>  {r.note}</Text>
                    </View>
                  )}
                  {r.state === 'error' && <Text style={[styles.mutedSm, { color: C.danger }]}>{r.error}</Text>}
                </View>
              ))}
            </ScrollView>
          </View>
        </View>
      </Modal>

      {/* details modal */}
      <Modal visible={!!detail} animationType="slide" transparent onRequestClose={() => setDetail(null)}>
        <View style={styles.sheetWrap}>
          <View style={styles.sheet}>
            <View style={styles.sheetHead}>
              <Text style={styles.sheetTitle} numberOfLines={1}>{detail?.base?.title}</Text>
              <TouchableOpacity onPress={() => setDetail(null)}><Text style={styles.iconTxt}>✕</Text></TouchableOpacity>
            </View>
            <Text style={styles.trCo}>{detail?.base?.company} · {detail?.base?.location || 'n/a'}</Text>
            {detailBusy ? <ActivityIndicator color={C.brand} style={{ marginTop: 20 }} /> : (
              <ScrollView style={{ maxHeight: 460, marginTop: 8 }}>
                {!!(detail?.data?.have?.length) && (
                  <>
                    <Text style={styles.secHdr}>Skills you match</Text>
                    <View style={styles.chips}>{detail.data.have.map((k, i) => <Text key={i} style={[styles.chip, styles.chipHave]}>{k}</Text>)}</View>
                  </>
                )}
                {!!(detail?.data?.missing?.length) && (
                  <>
                    <Text style={styles.secHdr}>Add these to your résumé</Text>
                    <View style={styles.chips}>{detail.data.missing.map((k, i) => <Text key={i} style={[styles.chip, styles.chipMiss]}>{k}</Text>)}</View>
                  </>
                )}
                <Text style={styles.secHdr}>Job description</Text>
                <Text style={styles.jd}>{detail?.data?.jd || 'No description stored — open Apply to read it on the company site.'}</Text>
              </ScrollView>
            )}
            <TouchableOpacity style={[styles.primaryBtn, { marginTop: 12 }]} onPress={() => detail?.base && openApply(detail.base)}>
              <Text style={styles.primaryBtnTxt}>Open apply page ↗</Text>
            </TouchableOpacity>
          </View>
        </View>
      </Modal>
    </View>
  );
}

// ------------------------------------------------------------------ card bits
function initials(c) { return (c || '?').trim().charAt(0).toUpperCase(); }
function Badges({ job }) {
  const b = [];
  if (job.intern) b.push(['Internship', C.brand]);
  if (job.sponsors_h1b === 'yes') b.push(['H1B', C.blue]);
  if (job.everify) b.push(['E-Verify', C.ok]);
  if (job.agency) b.push(['Agency', '#b45309']);
  if (job.exp_years !== '' && job.exp_years != null) b.push([job.exp_years + '+ yrs', C.muted]);
  if (job.sponsor_jd === 'open') b.push(['Sponsors', C.ok]);
  else if (job.sponsor_jd === 'blocked') b.push(['No sponsorship', C.danger]);
  return (
    <View style={styles.badges}>
      {b.map(([t, col], i) => <Text key={i} style={[styles.badge2, { color: col, borderColor: col }]}>{t}</Text>)}
    </View>
  );
}
function CardBody({ job }) {
  const score = job.score_pending ? null : (job.score || 0);
  const ringCol = score == null ? C.muted : score >= 55 ? C.brand : score >= 42 ? C.blue : C.muted;
  return (
    <>
      <View style={styles.cardTop}>
        <View style={styles.logo}><Text style={styles.logoTxt}>{initials(job.company)}</Text></View>
        <View style={[styles.scoreRing, { borderColor: ringCol }]}>
          <Text style={[styles.scoreTxt, { color: ringCol }]}>{score == null ? '—' : score + '%'}</Text>
        </View>
      </View>
      <Text style={styles.cardTitle} numberOfLines={3}>{job.title}</Text>
      <Text style={styles.cardCo}>{job.company}</Text>
      <Text style={styles.cardMeta}>{job.location || 'n/a'}{job.date ? '  ·  ' + job.date : ''}</Text>
      <Badges job={job} />
      <View style={{ flex: 1 }} />
      <Text style={styles.cardTap}>Tap for the full description</Text>
    </>
  );
}
function Card({ job, style }) { return <View style={style}><CardBody job={job} /></View>; }

function RoundBtn({ label, color, onPress, disabled, big, small }) {
  const size = big ? 70 : small ? 50 : 60;
  return (
    <TouchableOpacity onPress={onPress} disabled={disabled}
      style={[styles.round, { width: size, height: size, borderRadius: size / 2, opacity: disabled ? 0.35 : 1 }]}>
      <Text style={[styles.roundTxt, { color, fontSize: big ? 30 : 24 }]}>{label}</Text>
    </TouchableOpacity>
  );
}

// ------------------------------------------------------------------ styles
const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  appWrap: { flex: 1, paddingTop: Platform.OS === 'ios' ? 52 : 28 },
  appBar: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 16, height: 44 },
  appTitle: { color: C.ink, fontSize: 20, fontWeight: '800' },
  iconBtn: { paddingHorizontal: 8, paddingVertical: 6, marginLeft: 6 },
  iconTxt: { color: C.ink, fontSize: 20 },
  pill: { flexDirection: 'row', alignItems: 'center', backgroundColor: C.surface2, borderRadius: 999, paddingHorizontal: 12, paddingVertical: 7, borderWidth: 1, borderColor: C.line },
  pillTxt: { color: C.ink, fontWeight: '600', fontSize: 13 },
  badge: { marginLeft: 6, minWidth: 18, height: 18, borderRadius: 9, backgroundColor: C.brand, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 4 },
  badgeTxt: { color: '#fff', fontSize: 11, fontWeight: '800' },

  stage: { flex: 1, margin: 14, marginTop: 8 },
  card: {
    position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: C.surface,
    borderRadius: 22, borderWidth: 1, borderColor: C.line, padding: 22,
    shadowColor: '#000', shadowOpacity: 0.35, shadowRadius: 16, shadowOffset: { width: 0, height: 8 }, elevation: 6,
  },
  cardBehind: { transform: [{ scale: 0.955 }, { translateY: 12 }] },
  cardTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  logo: { width: 54, height: 54, borderRadius: 14, backgroundColor: C.brand, alignItems: 'center', justifyContent: 'center' },
  logoTxt: { color: '#fff', fontSize: 24, fontWeight: '800' },
  scoreRing: { width: 54, height: 54, borderRadius: 27, borderWidth: 4, alignItems: 'center', justifyContent: 'center' },
  scoreTxt: { fontSize: 14, fontWeight: '800' },
  cardTitle: { color: C.ink, fontSize: 24, fontWeight: '800', marginTop: 14, lineHeight: 29 },
  cardCo: { color: C.ink, fontSize: 16, fontWeight: '600', marginTop: 6 },
  cardMeta: { color: C.muted, fontSize: 13, marginTop: 3 },
  badges: { flexDirection: 'row', flexWrap: 'wrap', marginTop: 10 },
  badge2: { fontSize: 11, fontWeight: '700', borderWidth: 1, borderRadius: 6, paddingHorizontal: 8, paddingVertical: 3, marginRight: 6, marginBottom: 6 },
  cardTap: { color: C.muted, fontSize: 12, textAlign: 'center', opacity: 0.8 },
  stamp: { position: 'absolute', top: 26, zIndex: 5, borderWidth: 3, borderRadius: 10, paddingHorizontal: 12, paddingVertical: 4 },
  stampLike: { right: 22, transform: [{ rotate: '12deg' }], borderColor: C.ok },
  stampNope: { left: 22, transform: [{ rotate: '-12deg' }], borderColor: C.danger },
  stampTxt: { fontSize: 26, fontWeight: '900', letterSpacing: 2 },

  actionBar: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 22, paddingVertical: 6 },
  round: { alignItems: 'center', justifyContent: 'center', backgroundColor: C.surface, borderWidth: 1, borderColor: C.line },
  roundTxt: { fontWeight: '800' },
  hint: { color: C.muted, fontSize: 12, textAlign: 'center', paddingVertical: 12 },

  // login
  brandLogo: { color: C.ink, fontSize: 34, fontWeight: '900' },
  input: { width: '100%', maxWidth: 360, backgroundColor: C.surface, borderWidth: 1, borderColor: C.line, borderRadius: 12, color: C.ink, paddingHorizontal: 14, paddingVertical: 13, fontSize: 16, marginTop: 10 },
  primaryBtn: { width: '100%', maxWidth: 360, backgroundColor: C.brand, borderRadius: 12, paddingVertical: 14, alignItems: 'center', marginTop: 14 },
  primaryBtnTxt: { color: '#fff', fontSize: 16, fontWeight: '700' },
  link: { color: C.brandInk, marginTop: 16, fontSize: 13 },
  err: { color: C.danger, marginTop: 10, textAlign: 'center' },

  msg: { alignItems: 'center' },
  msgTxt: { color: C.muted, textAlign: 'center', fontSize: 15, lineHeight: 22 },
  ghostBtn: { marginTop: 14, borderWidth: 1, borderColor: C.line, borderRadius: 10, paddingHorizontal: 18, paddingVertical: 10 },
  ghostBtnTxt: { color: C.ink, fontWeight: '600' },

  // sheets
  sheetWrap: { flex: 1, justifyContent: 'flex-end', backgroundColor: 'rgba(0,0,0,0.5)' },
  sheet: { backgroundColor: C.surface, borderTopLeftRadius: 20, borderTopRightRadius: 20, padding: 18, paddingBottom: 30 },
  sheetHead: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 },
  sheetTitle: { color: C.ink, fontSize: 17, fontWeight: '800', flex: 1 },
  muted: { color: C.muted, textAlign: 'center', padding: 18 },
  mutedSm: { color: C.muted, fontSize: 12 },
  trRow: { borderWidth: 1, borderColor: C.line, borderRadius: 12, padding: 12, marginVertical: 6 },
  trTitle: { color: C.ink, fontWeight: '700', fontSize: 14 },
  trCo: { color: C.muted, fontSize: 13, marginBottom: 8 },
  rowc: { flexDirection: 'row', alignItems: 'center', flexWrap: 'wrap' },
  smallBtn: { backgroundColor: C.brand, borderRadius: 9, paddingHorizontal: 14, paddingVertical: 9 },
  smallBtnTxt: { color: '#fff', fontWeight: '700', fontSize: 13 },
  secHdr: { color: C.ink, fontWeight: '700', fontSize: 13, marginTop: 12, marginBottom: 6 },
  chips: { flexDirection: 'row', flexWrap: 'wrap' },
  chip: { fontSize: 12, borderRadius: 7, paddingHorizontal: 9, paddingVertical: 4, marginRight: 6, marginBottom: 6, overflow: 'hidden' },
  chipHave: { backgroundColor: 'rgba(34,197,94,0.15)', color: '#86efac' },
  chipMiss: { backgroundColor: 'rgba(99,102,241,0.15)', color: C.brandInk },
  jd: { color: C.muted, fontSize: 13, lineHeight: 20, marginTop: 4 },
  sub: { color: C.muted, marginTop: 4, marginBottom: 18 },
});
