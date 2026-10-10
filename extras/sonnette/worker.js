/* ═══════════════════════════════════════════════════════════════════════════════════════════════════
   LA « SONNETTE » DU RÉVEIL IA — un petit programme Cloudflare Workers (offre gratuite), étape 8 du README.

   Ce qu'elle fait, une fois par minute (déclencheur « cron » de Cloudflare) :

   1. PODCAST — environ 15 minutes avant ton heure de réveil, elle demande à GitHub de préparer le podcast
      (le workflow « Podcast du matin », envoyé sans notification dans Telegram). Les exécutions planifiées de
      GitHub peuvent arriver des heures en retard ; un lancement demandé par ce programme, lui, part tout de suite.
   2. SONNERIE — à l'heure exacte, elle fait sonner ton téléphone : elle appelle l'adresse « webhook » de
      l'appli MacroDroid, qui télécharge puis joue reveil.mp3 (petite sonnerie + podcast) via l'adresse secrète
      /p/<SECRET>/reveil.mp3 de ce même programme. Si le podcast du jour n'est pas prêt, cette adresse sert une
      sonnerie seule : le réveil sonne à l'heure quoi qu'il arrive.
   3. PLAN B — si le téléphone n'a pas répondu 2 minutes après la sonnerie (ou si tu n'as pas mis MacroDroid),
      elle t'écrit dans Telegram avec notification : « ⏰ 6 h 30 — bonjour ! ».
   4. JOURNAL — elle regarde (sans rien lire ni effacer) s'il y a un message en attente dans ta conversation
      Telegram, et lance alors le workflow « Journal vocal » : tes messages, tes images et tes ordres
      (« mets mon réveil à 6h ») sont traités en une ou deux minutes au lieu d'une demi-heure.

   Ce qu'elle ne fait JAMAIS : lire ou garder le contenu de tes messages, écrire dans ton dépôt, effacer un message.

   Réglages (Cloudflare → ton Worker → Settings → Variables and Secrets) :
     Secrets  : GITHUB_TOKEN        jeton « fine-grained » limité à ce dépôt (Actions : lecture/écriture, Contents : lecture)
                TELEGRAM_BOT_TOKEN  la clé de ton bot
                SECRET              un mot de passe inventé (20 caractères ou plus, lettres et chiffres) : il fait partie
                                    des adresses de ce programme
                RING_URL            (facultatif) l'adresse « webhook » de ta macro MacroDroid
     Variables: TELEGRAM_CHAT_ID    ton identifiant Telegram (le même que dans GitHub)
                REPO, BRANCH, TZ, LEAD_MIN, DOORBELL   (facultatifs, voir DEFAULTS ci-dessous)
     Stockage : un « KV namespace » relié au Worker sous le nom KV
     Cron     : * * * * *   (toutes les minutes)

   Les adresses de ce programme (toutes secrètes) :
     /p/<SECRET>/reveil.mp3        le fichier que joue le téléphone
     /status/<SECRET>              l'état en clair (prochain réveil, dernier podcast, réglages présents)
     /test/<SECRET>/ring           fait sonner le téléphone maintenant (essai)
     /test/<SECRET>/doorbell       envoie le message Telegram du plan B (essai)
     /test/<SECRET>/podcast        lance la préparation du podcast (essai du jeton GitHub)

   Le même fichier sert aux tests hors ligne : « cd extras/sonnette && node --test ».
   ═══════════════════════════════════════════════════════════════════════════════════════════════════ */

export const VERSION = "v4.0";
const MIN = 60_000;
const HOUR = 60 * MIN;

/** Réglages par défaut : on peut les remplacer par des variables Cloudflare du même nom. */
export const DEFAULTS = {
  REPO: "Terreur-L3/reveil-ia",   // « propriétaire/dépôt » GitHub
  BRANCH: "main",                 // la branche où se trouvent les workflows et memoire/reglages.json
  TZ: "Europe/Paris",
  LEAD_MIN: 15,                   // le podcast est préparé tant de minutes avant le réveil (5 à 60)
  DOORBELL: "fallback",           // fallback = Telegram seulement si le téléphone ne répond pas ; always ; never
};

/** Tous les délais. Les « écritures KV » mettent jusqu'à ~1 minute à être visibles partout : un rattrapage
 *  n'agit donc jamais avant `margeMs` après l'heure prévue, pour ne rien faire deux fois. */
export const LIMITS = {
  primaryMs: 1 * MIN,            // le passage qui tombe dans la minute prévue agit tout de suite
  margeMs: 2 * MIN,              // un rattrapage attend au moins ça
  ringCatchupMs: 20 * MIN,       // au-delà de 20 min de retard, on ne sonne plus (on prévient seulement)
  podcastCatchupMs: 10 * MIN,    // la préparation du podcast n'a plus d'intérêt pour la sonnerie passé ce délai
  freshBeforeMs: 3 * HOUR,       // un podcast fait moins de 3 h avant le réveil est réutilisé
  retryMs: 12 * MIN,             // 2e essai si le podcast n'est toujours pas prêt
  maxPodcastTries: 2,
  maxRingTries: 3,
  ringRetryMs: 90_000,
  fallbackMs: 2 * MIN,           // le téléphone a ce temps pour aller chercher le fichier, sinon plan B
  fallbackMaxMs: 15 * MIN,
  journalCooldownMs: 4 * MIN,
  journalTries: 3,
  journalStuckPauseMs: 3 * HOUR,
  alertEveryMs: 6 * HOUR,
  testEveryMs: 20_000,
  maxPodcastPerDay: 6,
  maxJournalPerDay: 80,
  ttlS: 3 * 24 * 3600,
};

const DAYS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"];
const JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"];
const MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
  "novembre", "décembre"];
const HHMM_RE = /^([01]\d|2[0-3]):([0-5]\d)$/;
const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

// ───────────────────────────────────────────────────────────────────────── 1. le temps (heure de Paris)
const formatters = new Map();
function formatter(tz) {
  let f = formatters.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-GB", {
      timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    formatters.set(tz, f);
  }
  return f;
}
const pad = (n) => String(n).padStart(2, "0");
const isoDay = (y, m, d) => `${y}-${pad(m)}-${pad(d)}`;

/** Jour de la semaine, 0 = lundi (comme Python). */
export function weekday(y, m, d) {
  return (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7;
}

/** Les éléments de l'heure locale d'un instant (en millisecondes). */
export function zoned(ms, tz) {
  const p = {};
  for (const part of formatter(tz).formatToParts(new Date(ms))) {
    if (part.type !== "literal") p[part.type] = Number(part.value);
  }
  const { year: y, month: m, day: d } = p;
  return { y, m, d, hh: p.hour % 24, mm: p.minute, ss: p.second, dow: weekday(y, m, d), day: isoDay(y, m, d) };
}

/** L'instant (en millisecondes) où il est `hh:mm` à l'heure locale ce jour-là (gère l'heure d'été). */
export function zonedToEpoch(y, m, d, hh, mm, tz) {
  const naive = Date.UTC(y, m - 1, d, hh, mm);
  let guess = naive;
  for (let i = 0; i < 4; i++) {
    const z = zoned(guess, tz);
    const offset = Date.UTC(z.y, z.m - 1, z.d, z.hh, z.mm, z.ss) - guess;
    const next = naive - offset;
    if (next === guess) return guess;
    guess = next;
  }
  return guess;
}

export function addDays(y, m, d, n) {
  const t = new Date(Date.UTC(y, m - 1, d + n));
  return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: t.getUTCDate() };
}

export const frTime = (hhmm) => `${Number(hhmm.slice(0, 2))} h${hhmm.slice(3) !== "00" ? ` ${hhmm.slice(3)}` : ""}`;
export function dayLabel(day) {
  const [y, m, d] = day.split("-").map(Number);
  return `${JOURS[weekday(y, m, d)]} ${d === 1 ? "1er" : d} ${MOIS[m - 1]}`;
}

// ───────────────────────────────────────────────────────────────────────── 2. l'horaire (comme src/reglages.py)
function truthy(v) {
  if (Array.isArray(v)) return v.length > 0;
  if (v && typeof v === "object") return Object.keys(v).length > 0;
  return Boolean(v);
}
function validIsoDate(s) {
  const m = DATE_RE.exec(s);
  if (!m) return false;
  const t = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  return t.getUTCFullYear() === +m[1] && t.getUTCMonth() === +m[2] - 1 && t.getUTCDate() === +m[3];
}

/** Réglages complets et valides, même si le fichier est absent ou abîmé (même règles que reglages.normalize). */
export function normalizeSettings(data) {
  const out = { actif: false, jours: Object.fromEntries(DAYS.map((d) => [d, null])), ponctuel: {}, maj: "" };
  const rev = data && typeof data === "object" && !Array.isArray(data) ? data.reveil : null;
  if (!rev || typeof rev !== "object" || Array.isArray(rev)) return out;
  out.actif = truthy(rev.actif);
  const jours = rev.jours;
  if (jours && typeof jours === "object" && !Array.isArray(jours)) {
    for (const d of DAYS) out.jours[d] = typeof jours[d] === "string" && HHMM_RE.test(jours[d]) ? jours[d] : null;
  }
  const ponctuel = rev.ponctuel;
  if (ponctuel && typeof ponctuel === "object" && !Array.isArray(ponctuel)) {
    for (const [k, v] of Object.entries(ponctuel)) {
      if (!validIsoDate(k)) continue;
      if (v === "off" || (typeof v === "string" && HHMM_RE.test(v))) out.ponctuel[k] = v;
    }
  }
  out.maj = rev.maj ? String(rev.maj) : "";
  return out;
}

/** L'heure (« HH:MM ») du réveil de ce jour, ou null. `day` = « AAAA-MM-JJ », `dow` = 0 pour lundi. */
export function wakeFor(settings, day, dow) {
  const exception = settings.ponctuel[day];
  if (exception !== undefined) return exception === "off" ? null : exception;
  if (!settings.actif) return null;
  return settings.jours[DAYS[dow]] ?? null;
}

/** Le prochain réveil à partir de `z` (une heure locale venant de `zoned`) : aujourd'hui s'il n'est pas passé. */
export function nextWake(settings, z) {
  for (let off = 0; off < 15; off++) {
    const t = addDays(z.y, z.m, z.d, off);
    const day = isoDay(t.y, t.m, t.d);
    const hhmm = wakeFor(settings, day, weekday(t.y, t.m, t.d));
    if (hhmm == null) continue;
    if (off > 0 || z.hh * 60 + z.mm <= Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3))) return { day, hhmm };
  }
  return null;
}

/** Les réveils (instants précis) dont l'heure tombe dans [fromMs, toMs[. */
export function instantsBetween(settings, tz, fromMs, toMs) {
  const a = zoned(fromMs, tz);
  const b = zoned(toMs, tz);
  const first = addDays(a.y, a.m, a.d, -1);
  const stop = Date.UTC(b.y, b.m - 1, b.d + 1);
  const out = [];
  for (let k = 0; k < 40; k++) {
    const t = addDays(first.y, first.m, first.d, k);
    if (Date.UTC(t.y, t.m - 1, t.d) > stop) break;
    const day = isoDay(t.y, t.m, t.d);
    const hhmm = wakeFor(settings, day, weekday(t.y, t.m, t.d));
    if (hhmm == null) continue;
    const at = zonedToEpoch(t.y, t.m, t.d, Number(hhmm.slice(0, 2)), Number(hhmm.slice(3)), tz);
    if (at >= fromMs && at < toMs) out.push({ id: `${day}T${hhmm}`, day, hhmm, at });
  }
  return out.sort((x, y) => x.at - y.at);
}

/** Le prochain réveil à venir (instant précis), jusqu'à 15 jours. */
export function nextInstant(settings, tz, nowMs) {
  const z = zoned(nowMs, tz);
  const n = nextWake(settings, z);
  if (!n) return null;
  const [y, m, d] = n.day.split("-").map(Number);
  return { id: `${n.day}T${n.hhmm}`, day: n.day, hhmm: n.hhmm, at: zonedToEpoch(y, m, d, Number(n.hhmm.slice(0, 2)), Number(n.hhmm.slice(3)), tz) };
}

// ───────────────────────────────────────────────────────────────────────── 3. la décision (fonction pure, testée à fond)
/** Le podcast déjà publié suffit-il pour ce réveil ? (fait moins de 3 h avant, avec de l'audio) */
export function podcastReady(meta, wakeAt) {
  if (!meta || meta.audio !== true) return false;
  const made = Date.parse(meta.genere_a);
  return Number.isFinite(made) && made >= wakeAt - LIMITS.freshBeforeMs;
}

/** Le podcast publié date-t-il d'aujourd'hui (heure de Paris) ? C'est lui qu'on sert à la sonnerie. */
export function isFreshToday(meta, nowMs, tz) {
  return Boolean(meta && meta.audio === true && meta.date === zoned(nowMs, tz).day);
}

/** La fenêtre des réveils à surveiller à cet instant : ceux qui viennent de sonner (pour le rattrapage) et ceux
 *  dont la préparation du podcast commence. */
export function watchWindow(now, cfg) {
  return [now - LIMITS.ringCatchupMs - 10 * MIN, now + cfg.leadMs + LIMITS.primaryMs + 1];
}

/**
 * Ce qu'il faut faire à cet instant. Ne touche à rien : retourne la liste des « effets » à exécuter.
 *   markers : { [idReveil]: { pod: {at, n}, ring: {at, ok, how, n, doorbell}, missed } }   (écrits par la sonnette)
 *   fetched : { [idReveil]: {at, file} }                                                       (écrits à chaque téléchargement)
 */
export function plan({ now, cfg, settings, markers = {}, fetched = {}, meta = null }) {
  const effects = [];
  const maj = Date.parse(settings.maj);
  const [from, to] = watchWindow(now, cfg);
  for (const w of instantsBetween(settings, cfg.tz, from, to)) {
    // Un réveil changé APRÈS son heure ne sonne pas ce jour-là (voir reglages.apply_request).
    if (Number.isFinite(maj) && maj > w.at) continue;
    const m = markers[w.id] || {};

    // 1) préparer le podcast, `lead` minutes avant
    const sinceStart = now - (w.at - cfg.leadMs);
    if (sinceStart >= 0 && now < w.at + LIMITS.podcastCatchupMs && !podcastReady(meta, w.at)) {
      const tries = m.pod?.n || 0;
      const due = tries === 0
        ? sinceStart < LIMITS.primaryMs || sinceStart >= LIMITS.margeMs
        : tries < LIMITS.maxPodcastTries && now - m.pod.at >= LIMITS.retryMs;
      if (due) effects.push({ type: "dispatch_podcast", id: w.id, retry: tries > 0 });
    }

    // 2) sonner
    const age = now - w.at;
    const ring = m.ring;
    if (age >= 0 && age < LIMITS.ringCatchupMs) {
      if (!ring) {
        if (age < LIMITS.primaryMs) effects.push({ type: "ring", id: w.id, hhmm: w.hhmm, late: false });
        else if (age >= LIMITS.margeMs) effects.push({ type: "ring", id: w.id, hhmm: w.hhmm, late: true });
      } else if (ring.ok === false && (ring.n || 1) < LIMITS.maxRingTries && now - ring.at >= LIMITS.ringRetryMs) {
        effects.push({ type: "ring", id: w.id, hhmm: w.hhmm, late: age >= LIMITS.margeMs, retry: true });
      }
    } else if (age >= LIMITS.ringCatchupMs && !ring && !m.missed) {
      effects.push({ type: "missed", id: w.id, hhmm: w.hhmm });
    }

    // 3) plan B : le téléphone n'est pas venu chercher le fichier
    if (ring && ring.ok === true && String(ring.how).includes("webhook") && !ring.doorbell && !fetched[w.id]
        && now - ring.at >= LIMITS.fallbackMs && now - ring.at < LIMITS.fallbackMaxMs && cfg.doorbell !== "never") {
      effects.push({ type: "doorbell", id: w.id, hhmm: w.hhmm, reason: "fallback" });
    }
  }
  return effects;
}

// ───────────────────────────────────────────────────────────────────────── 4. configuration
function bounded(v, def, lo, hi) {
  const n = Number.parseInt(String(v ?? ""), 10);
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : def;
}
const clean = (v) => String(v ?? "").trim();

export function readConfig(env) {
  const ringUrl = clean(env.RING_URL);
  const doorbell = clean(env.DOORBELL).toLowerCase();
  return {
    repo: clean(env.REPO) || DEFAULTS.REPO,
    branch: clean(env.BRANCH) || DEFAULTS.BRANCH,
    tz: clean(env.TZ) || DEFAULTS.TZ,
    leadMs: bounded(env.LEAD_MIN, DEFAULTS.LEAD_MIN, 5, 60) * MIN,
    chatId: clean(env.TELEGRAM_CHAT_ID),
    tgToken: clean(env.TELEGRAM_BOT_TOKEN),
    ghToken: clean(env.GITHUB_TOKEN),
    secret: clean(env.SECRET),
    ringUrl: /^https:\/\//i.test(ringUrl) ? ringUrl : "",
    doorbell: ["fallback", "always", "never"].includes(doorbell) ? doorbell : DEFAULTS.DOORBELL,
  };
}

// ───────────────────────────────────────────────────────────────────────── 5. GitHub, Telegram, MacroDroid
export class HttpError extends Error {
  constructor(service, status, description = "") {
    super(`${service} ${status}`);   // jamais d'adresse ni de clé dans les messages
    this.service = service;
    this.status = status;
    this.description = description;
  }
}

const timeout = (ms = 12_000) => AbortSignal.timeout(ms);

async function github(cfg, method, path, { body, raw = false } = {}) {
  return fetch(`https://api.github.com${path}`, {
    method,
    headers: {
      authorization: `Bearer ${cfg.ghToken}`,
      accept: raw ? "application/vnd.github.raw+json" : "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "reveil-sonnette",
      ...(body ? { "content-type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    signal: timeout(),
  });
}

async function repoFile(cfg, path, ref) {
  return github(cfg, "GET", `/repos/${cfg.repo}/contents/${path}?ref=${encodeURIComponent(ref)}`, { raw: true });
}

async function dispatchWorkflow(cfg, file, inputs) {
  const res = await github(cfg, "POST", `/repos/${cfg.repo}/actions/workflows/${file}/dispatches`, {
    body: { ref: cfg.branch, inputs },
  });
  // GitHub répond « 204 » (sans corps) ; depuis 2026 il peut répondre « 200 » avec le détail du lancement quand on le
  // demande (option return_run_details). Dans les deux cas le workflow est lancé : tout code 2xx veut dire « c'est parti ».
  if (res.status < 200 || res.status > 299) throw new HttpError("github", res.status);
}

async function workflowBusy(cfg, file) {
  try {
    const res = await github(cfg, "GET", `/repos/${cfg.repo}/actions/workflows/${file}/runs?per_page=3`);
    if (!res.ok) return false;
    const data = await res.json();
    return (data.workflow_runs || []).some((run) => run.status !== "completed");
  } catch {
    return false;
  }
}

async function telegram(cfg, method, payload) {
  const res = await fetch(`https://api.telegram.org/bot${cfg.tgToken}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
    signal: timeout(),
  });
  let data = null;
  try { data = await res.json(); } catch { /* réponse vide */ }
  if (!res.ok || !data || data.ok !== true) throw new HttpError("telegram", res.status, String(data?.description || ""));
  return data.result;
}

async function callWebhook(cfg) {
  const res = await fetch(cfg.ringUrl, { method: "GET", redirect: "follow", signal: timeout(8000) });
  if (!res.ok) throw new HttpError("webhook", res.status);
}

export function doorbellText({ hhmm, fresh, late = false }) {
  const head = `⏰ ${frTime(hhmm)}${late ? " (avec un peu de retard)" : ""} — bonjour !`;
  const body = fresh
    ? "Ton podcast du matin est dans la conversation, juste au-dessus : appuie sur le message vocal."
    : "Le podcast n'est pas encore prêt : il arrive dans la conversation dès qu'il est terminé.";
  return `${head}\n${body}`;
}

// ───────────────────────────────────────────────────────────────────────── 6. mémoire de la sonnette (KV)
async function kvGet(env, key) {
  try { return await env.KV.get(key, "json"); } catch { return null; }
}
async function kvPut(env, key, value, ttl = LIMITS.ttlS) {
  try {
    await env.KV.put(key, JSON.stringify(value), { expirationTtl: Math.max(60, ttl) });
    return true;
  } catch (err) {
    console.error("KV: écriture impossible", key.split(":")[0], err?.message || "");
    return false;
  }
}

/** Un seul message par sorte de problème et par 6 heures, sans notification (tu le vois en te réveillant). */
async function alertOnce(env, cfg, now, kind, text) {
  if (!cfg.tgToken || !cfg.chatId) return false;
  const last = await kvGet(env, `al:${kind}`);
  if (last && now - last < LIMITS.alertEveryMs) return false;
  await kvPut(env, `al:${kind}`, now, LIMITS.alertEveryMs / 1000);   // on note d'abord : jamais de tempête d'alertes
  try {
    await telegram(cfg, "sendMessage", { chat_id: cfg.chatId, text: `⚠️ Sonnette : ${text}`, disable_notification: true });
  } catch (err) {
    console.error("alerte non envoyée", err?.message || "");
  }
  return true;
}

async function bump(env, key) {
  const n = Number(await kvGet(env, key)) || 0;
  await kvPut(env, key, n + 1, 2 * 24 * 3600);
  return n + 1;
}

// ───────────────────────────────────────────────────────────────────────── 7. le passage de chaque minute
/** Le décalage du journal (memoire/reglages.json : journal_offset) : tous les messages Telegram dont le numéro est
 *  plus petit ont déjà été traités, même si Telegram les garde encore en attente jusqu'au prochain passage. */
export function journalOffset(raw) {
  const v = raw && typeof raw === "object" && !Array.isArray(raw) ? raw.journal_offset : undefined;
  return Number.isInteger(v) && v >= 0 ? v : 0;
}

/** Lit memoire/reglages.json. Retourne { settings, offset } : `settings` = l'horaire (celui en mémoire si GitHub ne
 *  répond pas), `offset` = le décalage du journal, ou null si on ne le connaît pas (GitHub muet). */
async function loadSettings(env, cfg, now) {
  let raw = null;
  try {
    const res = await repoFile(cfg, "memoire/reglages.json", cfg.branch);
    if (res.status === 404) raw = {};   // pas encore de réglages : aucun réveil
    else if (res.ok) raw = JSON.parse(await res.text());
    else throw new HttpError("github", res.status);
  } catch (err) {
    if (err instanceof HttpError && (err.status === 401 || err.status === 403)) {
      await alertOnce(env, cfg, now, "github",
        "GitHub refuse mon jeton (il a peut-être expiré). Refais un jeton (README, étape 8) et remplace GITHUB_TOKEN dans Cloudflare.");
    } else {
      console.error("réglages illisibles", err?.message || "");
    }
  }
  if (raw !== null) {
    const rev = { reveil: raw && typeof raw === "object" ? raw.reveil ?? null : null };
    const cached = await kvGet(env, "sched");
    if (JSON.stringify(cached) !== JSON.stringify(rev)) await kvPut(env, "sched", rev, 30 * 24 * 3600);
    return { settings: normalizeSettings(rev), offset: journalOffset(raw) };
  }
  const cached = await kvGet(env, "sched");   // GitHub ne répond pas : on garde le dernier horaire connu
  return { settings: cached ? normalizeSettings(cached) : null, offset: null };
}

async function loadMeta(cfg) {
  try {
    const res = await repoFile(cfg, "meta.json", "podcast");
    return res.ok ? JSON.parse(await res.text()) : null;
  } catch {
    return null;
  }
}

async function sendDoorbell(cfg, hhmm, fresh, late) {
  await telegram(cfg, "sendMessage", { chat_id: cfg.chatId, text: doorbellText({ hhmm, fresh, late }), disable_notification: false });
}

async function runEffect(env, cfg, now, effect, markers, meta) {
  const key = `w:${effect.id}`;
  const m = markers[effect.id] || {};
  const save = async () => { markers[effect.id] = m; await kvPut(env, key, m); };

  if (effect.type === "dispatch_podcast") {
    const today = zoned(now, cfg.tz).day;
    if ((Number(await kvGet(env, `cnt:${today}:pod`)) || 0) >= LIMITS.maxPodcastPerDay) {
      await alertOnce(env, cfg, now, "cap", "trop de podcasts demandés aujourd'hui : j'arrête pour ne pas vider tes minutes GitHub.");
      return "plafond";
    }
    let outcome = "ok";
    try {
      await dispatchWorkflow(cfg, "podcast.yml", { silencieux: "true", origine: "sonnette" });
    } catch (err) {
      outcome = err.message;
      await alertOnce(env, cfg, now, "dispatch",
        `je n'ai pas pu lancer le podcast (${err.message}). Vérifie le jeton GITHUB_TOKEN (Actions : lecture et écriture) dans Cloudflare.`);
    }
    m.pod = { at: now, n: (m.pod?.n || 0) + 1 };
    await save();
    await bump(env, `cnt:${today}:pod`);
    return outcome;
  }

  if (effect.type === "ring") {
    const fresh = isFreshToday(meta, now, cfg.tz);
    const via = [];
    const errors = [];
    if (cfg.ringUrl) {
      try { await callWebhook(cfg); via.push("webhook"); } catch (err) { errors.push(err.message); }
    }
    const wantBell = cfg.doorbell !== "never" && (cfg.doorbell === "always" || !via.includes("webhook"));
    if (wantBell && cfg.tgToken && cfg.chatId) {
      try { await sendDoorbell(cfg, effect.hhmm, fresh, effect.late); via.push("doorbell"); } catch (err) { errors.push(err.message); }
    }
    const ok = via.length > 0;
    m.ring = { at: now, ok, how: via.join("+"), late: Boolean(effect.late), n: (m.ring?.n || 0) + 1 };
    if (via.includes("doorbell")) m.ring.doorbell = now;
    await save();
    if (!ok) {
      await alertOnce(env, cfg, now, "ring",
        `je n'ai pas pu faire sonner ton réveil de ${frTime(effect.hhmm)} (${errors.join(", ") || "aucun moyen configuré"}). `
        + "Vérifie RING_URL et les clés dans Cloudflare.");
    }
    return ok ? via.join("+") : `échec : ${errors.join(", ")}`;
  }

  if (effect.type === "doorbell") {
    const fresh = isFreshToday(meta, now, cfg.tz);
    let outcome = "ok";
    try { await sendDoorbell(cfg, effect.hhmm, fresh, false); }
    catch (err) { outcome = err.message; }
    m.ring = { ...(m.ring || {}), doorbell: now };   // même en cas d'échec : un seul essai
    await save();
    return outcome;
  }

  if (effect.type === "missed") {
    m.missed = now;
    await save();
    await alertOnce(env, cfg, now, "missed",
      `ton réveil de ${frTime(effect.hhmm)} n'a pas sonné (je n'ai pas tourné à ce moment-là). Le podcast est dans la conversation.`);
    return "manqué";
  }
  return "inconnu";
}

async function wakePart(env, cfg, now, settings) {
  if (!cfg.ghToken) {
    await alertOnce(env, cfg, now, "config", "il manque le jeton GITHUB_TOKEN dans Cloudflare (README, étape 8).");
    return { skipped: "jeton GitHub manquant" };
  }
  if (!settings) return { skipped: "réglages inconnus" };

  const near = instantsBetween(settings, cfg.tz, ...watchWindow(now, cfg));
  if (!near.length) return { effects: [] };
  const markers = {};
  const fetched = {};
  const lastFetch = await kvGet(env, "fl");   // le dernier téléchargement de reveil.mp3 par le téléphone
  for (const w of near) {
    markers[w.id] = (await kvGet(env, `w:${w.id}`)) || {};
    const ring = markers[w.id].ring;
    // « le téléphone est venu » = il a téléchargé le fichier APRÈS la sonnerie (un essai fait avant ne compte pas)
    fetched[w.id] = ring && lastFetch && lastFetch.at >= ring.at ? lastFetch : null;
  }
  const meta = await loadMeta(cfg);
  const effects = plan({ now, cfg, settings, markers, fetched, meta });
  const done = [];
  for (const effect of effects) {
    done.push({ ...effect, resultat: await runEffect(env, cfg, now, effect, markers, meta) });
  }
  if (done.length) console.log(JSON.stringify({ sonnette: VERSION, effets: done }));
  return { effects: done };
}

async function journalPart(env, cfg, now, offset) {
  if (!cfg.tgToken || !cfg.chatId || !cfg.ghToken) return { skipped: true };
  if (offset === null || offset === undefined) return { skipped: "décalage du journal inconnu (GitHub ne répond pas)" };
  let updates;
  try {
    // Sans « offset » : on regarde seulement, on ne confirme rien (c'est le journal qui confirme).
    updates = await telegram(cfg, "getUpdates", { limit: 100, timeout: 0 });
  } catch (err) {
    if (err.status === 409 && /webhook/i.test(err.description)) {
      await alertOnce(env, cfg, now, "webhook", "ton bot a un « webhook » actif : le journal ne peut pas lire tes messages.");
    }
    return { error: err.message };
  }
  const mine = (Array.isArray(updates) ? updates : []).filter(
    (u) => u?.message && String(u.message.chat?.id) === cfg.chatId && Number(u.update_id) >= offset);
  if (!mine.length) return { pending: 0 };

  const first = mine[0].update_id;
  const tg = (await kvGet(env, "tg")) || {};
  const same = tg.u === first;
  let tries = same ? tg.n || 0 : 0;
  if (same && now - tg.at < LIMITS.journalCooldownMs) return { wait: "délai" };
  if (same && tries >= LIMITS.journalTries) {
    if (now - tg.at < LIMITS.journalStuckPauseMs) {
      await alertOnce(env, cfg, now, "journal",
        "ton dernier message n'a pas été traité après 3 essais. Regarde l'onglet Actions de GitHub (workflow « Journal vocal »).");
      return { wait: "bloqué" };
    }
    tries = 0;
  }
  if (await workflowBusy(cfg, "journal.yml")) return { wait: "en cours" };
  const today = zoned(now, cfg.tz).day;
  if ((Number(await kvGet(env, `cnt:${today}:jr`)) || 0) >= LIMITS.maxJournalPerDay) {
    await alertOnce(env, cfg, now, "cap", "trop de lancements du journal aujourd'hui : j'arrête pour ne pas vider tes minutes GitHub.");
    return { wait: "plafond" };
  }
  let outcome = "ok";
  try {
    await dispatchWorkflow(cfg, "journal.yml", { origine: "sonnette" });
  } catch (err) {
    outcome = err.message;
    await alertOnce(env, cfg, now, "dispatch",
      `je n'ai pas pu lancer le journal (${err.message}). Vérifie le jeton GITHUB_TOKEN (Actions : lecture et écriture).`);
  }
  await kvPut(env, "tg", { u: first, at: now, n: tries + 1 }, 2 * 24 * 3600);
  await bump(env, `cnt:${today}:jr`);
  console.log(JSON.stringify({ sonnette: VERSION, journal: outcome }));
  return { dispatched: outcome };
}

/** Le passage de chaque minute. `scheduledTime` = l'instant prévu (en ms) : c'est lui qui décide, pas l'horloge. */
export async function tick(env, scheduledTime = Date.now()) {
  const cfg = readConfig(env);
  const now = Math.floor(scheduledTime / MIN) * MIN;
  if (!env.KV) {
    console.error("Il manque le « KV namespace » relié sous le nom KV (README, étape 8).");
    return { error: "KV manquant" };
  }
  const loaded = cfg.ghToken ? await loadSettings(env, cfg, now) : { settings: null, offset: null };
  const [wake, journal] = await Promise.allSettled([
    wakePart(env, cfg, now, loaded.settings),
    journalPart(env, cfg, now, loaded.offset),
  ]);
  for (const r of [wake, journal]) if (r.status === "rejected") console.error("passage en erreur :", r.reason?.message || "");
  if (new Date(now).getUTCMinutes() % 15 === 0) await kvPut(env, "tick", now, 2 * 24 * 3600);   // 96 par jour
  return {
    wake: wake.status === "fulfilled" ? wake.value : { error: String(wake.reason?.message || wake.reason) },
    journal: journal.status === "fulfilled" ? journal.value : { error: String(journal.reason?.message || journal.reason) },
  };
}

// ───────────────────────────────────────────────────────────────────────── 8. les adresses (toutes secrètes)
function safeEqual(a, b) {
  const enc = new TextEncoder();
  const x = enc.encode(String(a));
  const y = enc.encode(String(b));
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

const NO_STORE = { "cache-control": "no-store", "x-robots-tag": "noindex" };
const jsonResponse = (data, status = 200) => new Response(JSON.stringify(data, null, 2), {
  status, headers: { "content-type": "application/json; charset=utf-8", ...NO_STORE },
});
const notFound = () => new Response("Not found", { status: 404, headers: { "content-type": "text/plain", ...NO_STORE } });

async function servePodcast(env, cfg, now, method) {
  if (!cfg.ghToken) return jsonResponse({ erreur: "il manque GITHUB_TOKEN dans Cloudflare" }, 503);
  const meta = await loadMeta(cfg);
  const fresh = isFreshToday(meta, now, cfg.tz);
  // Podcast du jour si on l'a ; sinon la sonnerie seule (jamais les actus d'hier : ce serait faux).
  const candidates = fresh ? ["reveil.mp3", "latest.mp3"] : ["sonnerie.mp3"];
  for (const file of candidates) {
    let res;
    try { res = await repoFile(cfg, file, "podcast"); } catch { continue; }
    if (!res.ok || !res.body) continue;
    // Noté sans rien lire d'autre : le plan B compare cette heure à celle de la sonnerie (voir wakePart).
    const last = await kvGet(env, "fl");
    if (!last || now - last.at > 10_000) await kvPut(env, "fl", { at: now, file }, 2 * 24 * 3600);
    const headers = {
      "content-type": "audio/mpeg",
      "x-reveil": fresh ? "podcast" : "sonnerie-seule",
      ...NO_STORE,
    };
    // Pas de « content-length » recopié : si GitHub compresse, la taille annoncée ne serait pas celle qu'on envoie.
    return new Response(method === "HEAD" ? null : res.body, { status: 200, headers });
  }
  return jsonResponse({ erreur: "aucun fichier audio disponible", podcast_du_jour: fresh }, 404);
}

async function statusPage(env, cfg, now) {
  const settings = cfg.ghToken ? (await loadSettings(env, cfg, now)).settings : null;
  const meta = cfg.ghToken ? await loadMeta(cfg) : null;
  const z = zoned(now, cfg.tz);
  const next = settings ? nextInstant(settings, cfg.tz, now) : null;
  const marker = next ? (await kvGet(env, `w:${next.id}`)) || {} : {};
  const tickAt = await kvGet(env, "tick");
  const lines = [];
  if (next) {
    lines.push(`Prochain réveil : ${dayLabel(next.day)} à ${frTime(next.hhmm)} (dans ${Math.max(0, Math.round((next.at - now) / MIN))} min).`);
    const start = zoned(next.at - cfg.leadMs, cfg.tz);
    lines.push(`Le podcast sera préparé vers ${frTime(`${pad(start.hh)}:${pad(start.mm)}`)}.`);
  } else {
    lines.push("Aucun réveil prévu dans les 15 prochains jours (envoie « /reveil 6h30 » au bot).");
  }
  lines.push(meta ? `Dernier podcast : ${meta.date}${isFreshToday(meta, now, cfg.tz) ? " (d'aujourd'hui)" : " (pas d'aujourd'hui)"}.` : "Aucun podcast publié pour l'instant.");
  lines.push(tickAt ? `Dernier passage de la sonnette : il y a ${Math.round((now - tickAt) / MIN)} min.` : "La sonnette n'a pas encore tourné (ajoute le cron « * * * * * »).");
  return jsonResponse({
    version: VERSION,
    maintenant: `${z.day} ${pad(z.hh)}:${pad(z.mm)} (${cfg.tz})`,
    resume: lines,
    configuration: {
      jeton_github: Boolean(cfg.ghToken),
      bot_telegram: Boolean(cfg.tgToken),
      identifiant_telegram: Boolean(cfg.chatId),
      adresse_macrodroid: Boolean(cfg.ringUrl),
      kv: Boolean(env.KV),
      depot: cfg.repo,
      avance_podcast_min: cfg.leadMs / MIN,
      plan_b: cfg.doorbell,
    },
    prochain_reveil: next ? { id: next.id, podcast_lance: Boolean(marker.pod), sonnerie: marker.ring ?? null } : null,
    dernier_podcast: meta ? { date: meta.date, genere_a: meta.genere_a, mode: meta.mode, origine: meta.origine } : null,
    dernier_passage_ms: tickAt || null,
  });
}

async function testAction(env, cfg, now, what) {
  if (!["ring", "doorbell", "podcast"].includes(what)) return notFound();
  const last = await kvGet(env, "rl:test");
  if (last && now - last < LIMITS.testEveryMs) return jsonResponse({ ok: false, erreur: "trop rapide : attends 20 secondes" }, 429);
  await kvPut(env, "rl:test", now, 120);
  try {
    if (what === "ring") {
      if (!cfg.ringUrl) return jsonResponse({ ok: false, erreur: "RING_URL n'est pas réglé dans Cloudflare" }, 400);
      await callWebhook(cfg);
      return jsonResponse({ ok: true, fait: "j'ai appelé l'adresse de MacroDroid : ton téléphone doit réagir dans quelques secondes" });
    }
    if (what === "doorbell") {
      if (!cfg.tgToken || !cfg.chatId) return jsonResponse({ ok: false, erreur: "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manque" }, 400);
      const z = zoned(now, cfg.tz);
      await sendDoorbell(cfg, `${pad(z.hh)}:${pad(z.mm)}`, true, false);
      return jsonResponse({ ok: true, fait: "message envoyé dans Telegram (avec notification)" });
    }
    if (!cfg.ghToken) return jsonResponse({ ok: false, erreur: "GITHUB_TOKEN n'est pas réglé dans Cloudflare" }, 400);
    await dispatchWorkflow(cfg, "podcast.yml", { silencieux: "true", origine: "sonnette" });
    return jsonResponse({ ok: true, fait: "podcast demandé : il arrive dans Telegram dans 3 à 5 minutes (onglet Actions de GitHub pour suivre)" });
  } catch (err) {
    return jsonResponse({ ok: false, erreur: err.message, conseil: err.status === 404 || err.status === 403 || err.status === 401 ? "vérifie les droits du jeton GitHub (Actions : lecture et écriture)" : undefined }, 502);
  }
}

export async function route(request, env, now = Date.now()) {
  const cfg = readConfig(env);
  const parts = new URL(request.url).pathname.split("/").filter(Boolean);
  if (parts.length < 2 || cfg.secret.length < 16 || !safeEqual(parts[1], cfg.secret)) return notFound();
  const [kind, , what] = parts;
  if (kind === "p" && parts.length === 3 && what === "reveil.mp3") return servePodcast(env, cfg, now, request.method);
  if (kind === "status" && parts.length === 2) return statusPage(env, cfg, now);
  if (kind === "test" && parts.length === 3) return testAction(env, cfg, now, what);
  return notFound();
}

export default {
  async fetch(request, env) {
    return route(request, env, Date.now());
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env, event.scheduledTime));
  },
};
