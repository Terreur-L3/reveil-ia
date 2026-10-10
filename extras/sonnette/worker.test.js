// Tests hors ligne de la sonnette : « cd extras/sonnette && node --test ».
// Aucun accès réseau : GitHub, Telegram et MacroDroid sont simulés, et on rejoue des matinées minute par minute.
import { afterEach, beforeEach, describe, test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import worker, {
  DEFAULTS, LIMITS, addDays, dayLabel, doorbellText, frTime, instantsBetween, isFreshToday, journalOffset, nextInstant,
  nextWake, normalizeSettings, plan, podcastReady, readConfig, route, tick, wakeFor, weekday, zoned, zonedToEpoch,
} from "./worker.js";

const MIN = 60_000;
const TZ = "Europe/Paris";
const SECRET = "s3cretDeTestDe24Caracteres";
const TG_TOKEN = "123456:TOKEN-TELEGRAM-DE-TEST";
const GH_TOKEN = "github_pat_JETON_DE_TEST";
const RING_URL = "https://trigger.macrodroid.com/aaaa-bbbb-cccc-dddd-eeee/reveil-ia";
const CHAT = "8923851509";
const FIXTURES = JSON.parse(readFileSync(new URL("../../tests/data/wake_cases.json", import.meta.url), "utf8"));

// ─────────────────────────────────────────────────────────────── outils
const at = (day, hhmm, sec = 0) => {
  const [y, m, d] = day.split("-").map(Number);
  return zonedToEpoch(y, m, d, Number(hhmm.slice(0, 2)), Number(hhmm.slice(3)), TZ) + sec * 1000;
};
const WEEK = { lun: "06:30", mar: "06:30", mer: "06:30", jeu: "06:30", ven: "06:30", sam: null, dim: null };
const reglages = ({ actif = true, jours = WEEK, ponctuel = {}, maj = "" } = {}) => ({
  version: 1, reveil: { actif, jours: { ...jours }, ponctuel: { ...ponctuel }, maj },
});
const DAY = "2026-10-08";            // un jeudi
const WAKE = at(DAY, "06:30");       // 04:30 UTC (heure d'été)
const baseEnv = (extra = {}) => ({
  GITHUB_TOKEN: GH_TOKEN, TELEGRAM_BOT_TOKEN: TG_TOKEN, TELEGRAM_CHAT_ID: CHAT, SECRET, RING_URL, ...extra,
});

/** Le « KV » de Cloudflare, avec en option un retard de visibilité des écritures (cohérence « à terme »). */
class FakeKV {
  constructor(world, delayMs = 0) { this.world = world; this.delay = delayMs; this.versions = new Map(); this.puts = 0; }
  async get(key, type) {
    const list = this.versions.get(key);
    if (!list) return null;
    const horizon = this.world.clock - this.delay;
    let found = null;
    for (const v of list) if (v.t <= horizon) found = v;
    if (!found || (found.exp !== null && found.exp <= this.world.clock)) return null;
    return type === "json" ? JSON.parse(found.value) : found.value;
  }
  async put(key, value, opts = {}) {
    if (opts.expirationTtl !== undefined && opts.expirationTtl < 60) throw new Error("expirationTtl < 60");
    const list = this.versions.get(key) || [];
    list.push({ t: this.world.clock, value, exp: opts.expirationTtl ? this.world.clock + opts.expirationTtl * 1000 : null });
    this.versions.set(key, list);
    this.puts += 1;
  }
}

const bytes = (text) => new TextEncoder().encode(text);
const readText = async (res) => new TextDecoder().decode(await res.arrayBuffer());

/** GitHub + Telegram + MacroDroid simulés. */
class World {
  constructor() {
    this.clock = 0;
    this.calls = [];                 // { url, method, headers, body }
    this.settings = reglages();      // contenu de memoire/reglages.json (null = fichier absent)
    this.settingsStatus = 200;       // forcer une erreur GitHub
    this.meta = { date: "2026-10-07", genere_a: "2026-10-07T04:00:00Z", audio: true, mode: "normal", origine: "cron", reveil_mp3: true };
    this.files = { "reveil.mp3": "REVEIL=BIP+PODCAST", "sonnerie.mp3": "SONNERIE-SEULE", "latest.mp3": "PODCAST-SEUL" };
    this.dispatches = [];            // { file, inputs, at }
    this.dispatchStatus = 204;
    this.runs = [];                  // runs du workflow journal (status)
    this.updates = [];               // messages en attente chez Telegram
    this.getUpdatesError = null;     // { code, description }
    this.sent = [];                  // messages envoyés par la sonnette dans Telegram
    this.telegramSendFails = false;
    this.webhookStatus = 200;
    this.webhookCalls = [];          // instants des appels
    this.podcastDelayMin = 4;        // le workflow publie le podcast au bout de ce délai (null = il échoue)
    this.pending = [];
  }

  kv(delayMs = 0) { return new FakeKV(this, delayMs); }

  tg(method) { return this.calls.filter((c) => c.url.includes("api.telegram.org") && c.url.endsWith(`/${method}`)); }

  runPending() {
    for (const job of this.pending.filter((p) => p.at <= this.clock)) job.fn();
    this.pending = this.pending.filter((p) => p.at > this.clock);
  }

  async fetch(url, init = {}) {
    url = String(url);
    const call = { url, method: init.method || "GET", headers: Object.fromEntries(new Headers(init.headers || {})), body: init.body, at: this.clock };
    this.calls.push(call);
    const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

    if (url.startsWith(RING_URL)) {
      this.webhookCalls.push(this.clock);
      return new Response(this.webhookStatus === 200 ? "OK" : "KO", { status: this.webhookStatus });
    }
    if (url.startsWith("https://api.telegram.org/bot")) {
      const method = url.split("/").pop();
      const body = JSON.parse(init.body || "{}");
      if (!url.includes(TG_TOKEN)) return json({ ok: false, description: "Unauthorized" }, 401);
      if (method === "getUpdates") {
        if (this.getUpdatesError) return json({ ok: false, error_code: this.getUpdatesError.code, description: this.getUpdatesError.description }, this.getUpdatesError.code);
        return json({ ok: true, result: this.updates.slice(0, body.limit || 100) });
      }
      if (method === "sendMessage") {
        if (this.telegramSendFails) return json({ ok: false, description: "Bad Request" }, 400);
        this.sent.push({ ...body, at: this.clock });
        return json({ ok: true, result: { message_id: this.sent.length } });
      }
      return json({ ok: false, description: "inconnu" }, 404);
    }
    if (url.startsWith("https://api.github.com/")) {
      if (call.headers.authorization !== `Bearer ${GH_TOKEN}`) return json({ message: "Bad credentials" }, 401);
      const path = url.slice("https://api.github.com".length);
      let m;
      if ((m = path.match(/^\/repos\/[^/]+\/[^/]+\/actions\/workflows\/([\w.]+)\/dispatches$/)) && call.method === "POST") {
        const body = JSON.parse(init.body);
        if (this.dispatchStatus >= 200 && this.dispatchStatus <= 299) {          // 204 (habituel) ou 200 (avec le détail du lancement)
          this.dispatches.push({ file: m[1], inputs: body.inputs, ref: body.ref, at: this.clock });
          if (m[1] === "podcast.yml" && this.podcastDelayMin !== null) {
            const when = this.clock + this.podcastDelayMin * MIN;
            this.pending.push({ at: when, fn: () => {
              this.meta = { date: zoned(when, TZ).day, genere_a: new Date(when).toISOString(), audio: true, mode: "normal", origine: "sonnette", reveil_mp3: true };
            } });
          }
          if (this.dispatchStatus === 204) return new Response(null, { status: 204 });
          return json({ workflow_run_id: 1234567, run_url: "https://api.github.com/repos/x/y/actions/runs/1234567", html_url: "https://github.com/x/y/actions/runs/1234567" }, this.dispatchStatus);
        }
        return json({ message: "nope" }, this.dispatchStatus);
      }
      if ((m = path.match(/^\/repos\/[^/]+\/[^/]+\/actions\/workflows\/journal\.yml\/runs/))) {
        return json({ workflow_runs: this.runs.map((status) => ({ status })) });
      }
      if ((m = path.match(/^\/repos\/[^/]+\/[^/]+\/contents\/([^?]+)\?ref=(\w+)$/))) {
        const [, file, ref] = m;
        if (file === "memoire/reglages.json" && ref === "main") {
          if (this.settingsStatus !== 200) return json({ message: "erreur" }, this.settingsStatus);
          return this.settings === null ? json({ message: "Not Found" }, 404) : new Response(JSON.stringify(this.settings), { status: 200 });
        }
        if (file === "meta.json" && ref === "podcast") {
          return this.meta === null ? json({ message: "Not Found" }, 404) : new Response(JSON.stringify(this.meta), { status: 200 });
        }
        if (ref === "podcast" && this.files[file] !== undefined) {
          const data = bytes(this.files[file]);
          return new Response(data, { status: 200, headers: { "content-length": String(data.length) } });
        }
        return json({ message: "Not Found" }, 404);
      }
    }
    throw new Error(`appel réseau non prévu dans le test : ${url.split("?")[0]}`);
  }
}

let world;
let realFetch;
let logs;
const realLog = console.log;
const realError = console.error;
beforeEach(() => {
  world = new World();
  realFetch = globalThis.fetch;
  globalThis.fetch = (url, init) => world.fetch(url, init);
  logs = [];
  console.log = (...a) => logs.push(a.join(" "));
  console.error = (...a) => logs.push(a.join(" "));
});
afterEach(() => {
  globalThis.fetch = realFetch;
  console.log = realLog;
  console.error = realError;
});

/** Rejoue le temps minute par minute ; le téléphone va chercher le fichier 5 s après un appel du webhook. */
async function simulate(env, from, to, { skip = () => false, phone = true } = {}) {
  let seen = world.webhookCalls.length;
  for (let t = from; t <= to; t += MIN) {
    world.clock = t;
    world.runPending();
    if (skip(t)) continue;
    await tick(env, t);
    if (phone && world.webhookCalls.length > seen) {
      seen = world.webhookCalls.length;
      world.clock = t + 5000;
      const res = await route(new Request(`https://reveil.example/p/${SECRET}/reveil.mp3`), env, t + 5000);
      await res.arrayBuffer();
    }
  }
}
const notifying = () => world.sent.filter((m) => m.disable_notification === false);
const silent = () => world.sent.filter((m) => m.disable_notification === true);
const podcasts = () => world.dispatches.filter((d) => d.file === "podcast.yml");
const journals = () => world.dispatches.filter((d) => d.file === "journal.yml");

// ═══════════════════════════════════════════════════════════════ le temps
describe("temps (heure de Paris)", () => {
  test("heure d'été et d'hiver : des instants connus", () => {
    assert.equal(new Date(zonedToEpoch(2026, 10, 8, 6, 30, TZ)).toISOString(), "2026-10-08T04:30:00.000Z");
    assert.equal(new Date(zonedToEpoch(2026, 12, 8, 6, 30, TZ)).toISOString(), "2026-12-08T05:30:00.000Z");
  });
  test("passage à l'heure d'hiver (25 octobre 2026) et à l'heure d'été (29 mars 2026)", () => {
    assert.equal(new Date(zonedToEpoch(2026, 10, 24, 7, 0, TZ)).toISOString(), "2026-10-24T05:00:00.000Z");
    assert.equal(new Date(zonedToEpoch(2026, 10, 25, 7, 0, TZ)).toISOString(), "2026-10-25T06:00:00.000Z");
    assert.equal(new Date(zonedToEpoch(2026, 3, 28, 7, 0, TZ)).toISOString(), "2026-03-28T06:00:00.000Z");
    assert.equal(new Date(zonedToEpoch(2026, 3, 29, 7, 0, TZ)).toISOString(), "2026-03-29T05:00:00.000Z");
  });
  test("zoned et zonedToEpoch sont réciproques, jour après jour, toute l'année", () => {
    for (let k = 0; k < 366; k++) {
      const t = addDays(2026, 1, 1, k);
      for (const hhmm of ["00:00", "05:45", "06:30", "12:00", "23:59"]) {
        const ms = zonedToEpoch(t.y, t.m, t.d, Number(hhmm.slice(0, 2)), Number(hhmm.slice(3)), TZ);
        const z = zoned(ms, TZ);
        assert.deepEqual([z.y, z.m, z.d, z.hh, z.mm], [t.y, t.m, t.d, Number(hhmm.slice(0, 2)), Number(hhmm.slice(3))], `${t.y}-${t.m}-${t.d} ${hhmm}`);
      }
    }
  });
  test("minuit : l'heure n'est jamais 24", () => {
    const z = zoned(Date.UTC(2026, 9, 7, 22, 0, 0), TZ);   // 00:00 à Paris
    assert.equal(z.hh, 0);
    assert.equal(z.day, "2026-10-08");
  });
  test("jour de la semaine : lundi = 0", () => {
    assert.equal(weekday(2026, 10, 5), 0);
    assert.equal(weekday(2026, 10, 8), 3);
    assert.equal(weekday(2026, 10, 11), 6);
  });
  test("addDays passe les mois et les années", () => {
    assert.deepEqual(addDays(2026, 12, 31, 1), { y: 2027, m: 1, d: 1 });
    assert.deepEqual(addDays(2028, 2, 28, 1), { y: 2028, m: 2, d: 29 });
    assert.deepEqual(addDays(2026, 3, 1, -1), { y: 2026, m: 2, d: 28 });
  });
  test("phrases françaises", () => {
    assert.equal(frTime("06:30"), "6 h 30");
    assert.equal(frTime("07:00"), "7 h");
    assert.equal(frTime("00:05"), "0 h 05");
    assert.equal(dayLabel("2026-10-08"), "jeudi 8 octobre");
    assert.equal(dayLabel("2026-11-01"), "dimanche 1er novembre");
  });
});

// ═══════════════════════════════════════════════════════════════ l'horaire, comme reglages.py
describe("horaire : mêmes réponses que src/reglages.py (cas partagés tests/data/wake_cases.json)", () => {
  for (const c of FIXTURES.wake_for) {
    test(`wakeFor : ${c.nom}`, () => {
      const [y, m, d] = c.jour.split("-").map(Number);
      assert.equal(wakeFor(normalizeSettings(c.reglages), c.jour, weekday(y, m, d)), c.attendu);
    });
  }
  for (const c of FIXTURES.next_wake) {
    test(`nextWake : ${c.nom}`, () => {
      const [date, time] = c.maintenant.split("T");
      const [y, m, d] = date.split("-").map(Number);
      const [hh, mm] = time.split(":").map(Number);
      const got = nextWake(normalizeSettings(c.reglages), { y, m, d, hh, mm });
      assert.deepEqual(got ? { jour: got.day, heure: got.hhmm } : null, c.attendu);
    });
  }
  test("normalizeSettings ne plante jamais, quoi qu'il y ait dans le fichier", () => {
    for (const junk of [null, undefined, 42, "x", [], [1, 2], { reveil: null }, { reveil: [] }, { reveil: { jours: null, ponctuel: 3 } }]) {
      const s = normalizeSettings(junk);
      assert.equal(s.actif, false);
      assert.deepEqual(Object.keys(s.jours), ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]);
    }
  });
});

describe("instants de réveil", () => {
  const settings = normalizeSettings(reglages());
  test("un réveil dans la fenêtre", () => {
    const list = instantsBetween(settings, TZ, WAKE - 30 * MIN, WAKE + 30 * MIN);
    assert.deepEqual(list.map((w) => w.id), ["2026-10-08T06:30"]);
    assert.equal(list[0].at, WAKE);
  });
  test("fenêtre qui chevauche minuit : on retrouve le réveil de demain matin", () => {
    const list = instantsBetween(settings, TZ, at("2026-10-07", "23:00"), at("2026-10-08", "07:00"));
    assert.deepEqual(list.map((w) => w.id), ["2026-10-08T06:30"]);
  });
  test("le dernier instant de la fenêtre est exclu", () => {
    assert.equal(instantsBetween(settings, TZ, WAKE - MIN, WAKE).length, 0);
    assert.equal(instantsBetween(settings, TZ, WAKE, WAKE + MIN).length, 1);
  });
  test("jour du passage à l'heure d'hiver", () => {
    const s = normalizeSettings(reglages({ jours: { ...WEEK, dim: "07:00" } }));
    const list = instantsBetween(s, TZ, at("2026-10-25", "05:00"), at("2026-10-25", "09:00"));
    assert.equal(list.length, 1);
    assert.equal(new Date(list[0].at).toISOString(), "2026-10-25T06:00:00.000Z");
  });
  test("nextInstant donne le prochain réveil avec son instant exact", () => {
    const next = nextInstant(settings, TZ, WAKE + 60 * MIN);
    assert.equal(next.id, "2026-10-09T06:30");
    assert.equal(next.at, at("2026-10-09", "06:30"));
    assert.equal(nextInstant(normalizeSettings(reglages({ actif: false })), TZ, WAKE), null);
  });
});

// ═══════════════════════════════════════════════════════════════ la décision (fonction pure)
describe("plan : qui fait quoi, et quand", () => {
  const cfg = readConfig(baseEnv());
  const settings = normalizeSettings(reglages());
  const stale = { date: "2026-10-07", genere_a: "2026-10-07T04:00:00Z", audio: true };
  const fresh = { date: DAY, genere_a: new Date(WAKE - 10 * MIN).toISOString(), audio: true };
  const go = (now, extra = {}) => plan({ now, cfg, settings, meta: stale, ...extra });
  const types = (effects) => effects.map((e) => e.type + (e.late ? ":late" : "") + (e.retry ? ":retry" : ""));

  test("rien longtemps avant", () => {
    assert.deepEqual(go(WAKE - 16 * MIN), []);
    assert.deepEqual(go(WAKE - 2 * 3600_000), []);
  });
  test("podcast : lancé dans la minute prévue (réveil − 15 min)", () => {
    assert.deepEqual(types(go(WAKE - 15 * MIN)), ["dispatch_podcast"]);
    assert.deepEqual(types(go(WAKE - 15 * MIN + 30_000)), ["dispatch_podcast"]);
  });
  test("podcast : rien dans la minute suivante, rattrapage 2 minutes après", () => {
    assert.deepEqual(go(WAKE - 14 * MIN), []);
    assert.deepEqual(go(WAKE - 14 * MIN + 59_000), []);
    assert.deepEqual(types(go(WAKE - 13 * MIN)), ["dispatch_podcast"]);
  });
  test("podcast : une fois lancé, on ne le relance pas ; 2e essai 12 min plus tard s'il n'est pas prêt", () => {
    const markers = { "2026-10-08T06:30": { pod: { at: WAKE - 15 * MIN, n: 1 } } };
    assert.deepEqual(go(WAKE - 13 * MIN, { markers }), []);
    assert.deepEqual(go(WAKE - 4 * MIN, { markers }), []);
    assert.deepEqual(types(go(WAKE - 3 * MIN, { markers })), ["dispatch_podcast:retry"]);
    assert.deepEqual(go(WAKE - 3 * MIN, { markers, meta: fresh }), []);
  });
  test("podcast : jamais plus de 2 essais, et plus de lancement 10 min après l'heure", () => {
    const two = { "2026-10-08T06:30": { pod: { at: WAKE - 3 * MIN, n: 2 } } };
    const launches = (effects) => effects.filter((e) => e.type === "dispatch_podcast");
    assert.deepEqual(launches(go(WAKE - 1 * MIN, { markers: two })), []);
    assert.deepEqual(launches(go(WAKE + 9 * MIN, { markers: two })), []);
    assert.deepEqual(launches(go(WAKE + 9 * MIN)), [{ type: "dispatch_podcast", id: "2026-10-08T06:30", retry: false }]);
    assert.deepEqual(launches(go(WAKE + 10 * MIN)), []);
  });
  test("podcast : déjà prêt (fait moins de 3 h avant) → pas de nouveau lancement", () => {
    assert.deepEqual(go(WAKE - 15 * MIN, { meta: fresh }), []);
    const earlier = { date: DAY, genere_a: new Date(WAKE - 2.5 * 3600_000).toISOString(), audio: true };
    assert.deepEqual(go(WAKE - 15 * MIN, { meta: earlier }), []);
    const tooOld = { date: DAY, genere_a: new Date(WAKE - 3.5 * 3600_000).toISOString(), audio: true };
    assert.deepEqual(types(go(WAKE - 15 * MIN, { meta: tooOld })), ["dispatch_podcast"]);
  });
  test("podcast prêt seulement s'il y a de l'audio", () => {
    assert.equal(podcastReady({ ...fresh, audio: false }, WAKE), false);
    assert.equal(podcastReady(null, WAKE), false);
    assert.equal(podcastReady({ audio: true, genere_a: "n'importe quoi" }, WAKE), false);
    assert.equal(podcastReady(fresh, WAKE), true);
  });
  test("sonnerie : dans la minute exacte, puis plus rien", () => {
    assert.deepEqual(types(go(WAKE, { meta: fresh })), ["ring"]);
    assert.deepEqual(types(go(WAKE + 59_000, { meta: fresh })), ["ring"]);
    assert.deepEqual(go(WAKE + 60_000, { meta: fresh }), []);
    assert.deepEqual(go(WAKE + 119_000, { meta: fresh }), []);
  });
  test("sonnerie : rattrapage entre 2 et 20 minutes de retard seulement", () => {
    assert.deepEqual(types(go(WAKE + 2 * MIN, { meta: fresh })), ["ring:late"]);
    assert.deepEqual(types(go(WAKE + 19 * MIN, { meta: fresh })), ["ring:late"]);
    assert.deepEqual(types(go(WAKE + 20 * MIN, { meta: fresh })), ["missed"]);
    assert.deepEqual(go(WAKE + 31 * MIN, { meta: fresh }), []);
  });
  test("sonnerie : déjà faite → jamais deux fois", () => {
    const markers = { "2026-10-08T06:30": { ring: { at: WAKE, ok: true, how: "webhook", n: 1 } } };
    for (const dt of [0, 30_000, 2 * MIN, 5 * MIN, 19 * MIN]) {
      assert.ok(!types(go(WAKE + dt, { meta: fresh, markers, fetched: { "2026-10-08T06:30": { at: WAKE + 5000 } } })).some((t) => t.startsWith("ring")), `dt=${dt}`);
    }
  });
  test("sonnerie : un échec est retenté (au plus 3 fois, 90 s d'écart)", () => {
    const failed = (n) => ({ "2026-10-08T06:30": { ring: { at: WAKE, ok: false, how: "", n } } });
    assert.deepEqual(go(WAKE + 60_000, { meta: fresh, markers: failed(1) }), []);
    assert.deepEqual(types(go(WAKE + 90_000, { meta: fresh, markers: failed(1) })), ["ring:retry"]);
    assert.deepEqual(go(WAKE + 5 * MIN, { meta: fresh, markers: failed(3) }), []);
  });
  test("plan B : le téléphone n'est pas venu chercher le fichier", () => {
    const rang = { "2026-10-08T06:30": { ring: { at: WAKE, ok: true, how: "webhook", n: 1 } } };
    assert.deepEqual(go(WAKE + 119_000, { meta: fresh, markers: rang }), []);
    assert.deepEqual(types(go(WAKE + 2 * MIN, { meta: fresh, markers: rang })), ["doorbell"]);
    assert.deepEqual(go(WAKE + 2 * MIN, { meta: fresh, markers: rang, fetched: { "2026-10-08T06:30": { at: WAKE + 4000 } } }), []);
    assert.deepEqual(go(WAKE + 16 * MIN, { meta: fresh, markers: rang }), []);
    const already = { "2026-10-08T06:30": { ring: { at: WAKE, ok: true, how: "webhook", n: 1, doorbell: WAKE + 2 * MIN } } };
    assert.deepEqual(go(WAKE + 4 * MIN, { meta: fresh, markers: already }), []);
    const bellOnly = { "2026-10-08T06:30": { ring: { at: WAKE, ok: true, how: "doorbell", n: 1, doorbell: WAKE } } };
    assert.deepEqual(go(WAKE + 4 * MIN, { meta: fresh, markers: bellOnly }), []);
    const never = plan({ now: WAKE + 2 * MIN, cfg: readConfig(baseEnv({ DOORBELL: "never" })), settings, meta: fresh, markers: rang });
    assert.deepEqual(never, []);
  });
  test("règle « maj » : un réveil changé après son heure ne sonne pas ce jour-là", () => {
    const changedAfter = normalizeSettings(reglages({ maj: new Date(WAKE + 60_000).toISOString() }));
    for (const dt of [-15 * MIN, -3 * MIN, 0, 2 * MIN, 10 * MIN, 20 * MIN]) {
      assert.deepEqual(plan({ now: WAKE + dt, cfg, settings: changedAfter, meta: stale }), [], `dt=${dt}`);
    }
    const changedBefore = normalizeSettings(reglages({ maj: new Date(WAKE - 60_000).toISOString() }));
    assert.deepEqual(types(plan({ now: WAKE, cfg, settings: changedBefore, meta: fresh })), ["ring"]);
    const garbage = normalizeSettings(reglages({ maj: "pas une date" }));
    assert.deepEqual(types(plan({ now: WAKE, cfg, settings: garbage, meta: fresh })), ["ring"]);
  });
  test("pas de réveil, pas d'effet", () => {
    const off = normalizeSettings(reglages({ actif: false }));
    for (const dt of [-15 * MIN, 0, 5 * MIN]) assert.deepEqual(plan({ now: WAKE + dt, cfg, settings: off, meta: stale }), []);
    const skipped = normalizeSettings(reglages({ ponctuel: { [DAY]: "off" } }));
    assert.deepEqual(plan({ now: WAKE, cfg, settings: skipped, meta: fresh }), []);
  });
  test("l'avance du podcast est réglable (LEAD_MIN)", () => {
    const c30 = readConfig(baseEnv({ LEAD_MIN: "30" }));
    assert.deepEqual(plan({ now: WAKE - 30 * MIN, cfg: c30, settings, meta: stale }).map((e) => e.type), ["dispatch_podcast"]);
    assert.deepEqual(plan({ now: WAKE - 25 * MIN, cfg: c30, settings, meta: stale, markers: { "2026-10-08T06:30": { pod: { at: WAKE - 30 * MIN, n: 1 } } } }), []);
    assert.deepEqual(plan({ now: WAKE - 31 * MIN, cfg: c30, settings, meta: stale }), [], "pas avant l'heure prévue");
  });
});

describe("configuration", () => {
  test("valeurs par défaut", () => {
    const cfg = readConfig({});
    assert.equal(cfg.repo, DEFAULTS.REPO);
    assert.equal(cfg.branch, "main");
    assert.equal(cfg.tz, TZ);
    assert.equal(cfg.leadMs, 15 * MIN);
    assert.equal(cfg.doorbell, "fallback");
    assert.equal(cfg.ringUrl, "");
  });
  test("bornes et nettoyage", () => {
    assert.equal(readConfig({ LEAD_MIN: "2" }).leadMs, 5 * MIN);
    assert.equal(readConfig({ LEAD_MIN: "500" }).leadMs, 60 * MIN);
    assert.equal(readConfig({ LEAD_MIN: "abc" }).leadMs, 15 * MIN);
    assert.equal(readConfig({ DOORBELL: " ALWAYS " }).doorbell, "always");
    assert.equal(readConfig({ DOORBELL: "n'importe quoi" }).doorbell, "fallback");
    assert.equal(readConfig({ RING_URL: "http://pas-securise" }).ringUrl, "");
    assert.equal(readConfig({ RING_URL: " https://ok.example/x " }).ringUrl, "https://ok.example/x");
    assert.equal(readConfig({ TELEGRAM_CHAT_ID: 123 }).chatId, "123");
  });
  test("texte du plan B", () => {
    assert.match(doorbellText({ hhmm: "06:30", fresh: true }), /6 h 30 — bonjour/);
    assert.match(doorbellText({ hhmm: "06:30", fresh: true }), /juste au-dessus/);
    assert.match(doorbellText({ hhmm: "06:30", fresh: false }), /pas encore prêt/);
    assert.match(doorbellText({ hhmm: "06:30", fresh: true, late: true }), /un peu de retard/);
  });
  test("podcast « du jour » : date de Paris", () => {
    assert.equal(isFreshToday({ audio: true, date: DAY }, WAKE, TZ), true);
    assert.equal(isFreshToday({ audio: true, date: "2026-10-07" }, WAKE, TZ), false);
    assert.equal(isFreshToday({ audio: false, date: DAY }, WAKE, TZ), false);
    assert.equal(isFreshToday(null, WAKE, TZ), false);
  });
});

// ═══════════════════════════════════════════════════════════════ des matinées entières, minute par minute
describe("matinée complète (tick + KV + GitHub + Telegram simulés)", () => {
  test("GitHub répond « 200 » avec le détail du lancement (au lieu de « 204 ») : la matinée se déroule pareil", async () => {
    world.dispatchStatus = 200;
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "05:50"), at(DAY, "07:10"));
    assert.equal(podcasts().length, 1, "un seul lancement : pas de 2e essai pour un succès pris pour un échec");
    assert.deepEqual(world.webhookCalls, [at(DAY, "06:30")]);
    assert.equal(notifying().length, 0);
    assert.equal(silent().length, 0, "aucune alerte « je n'ai pas pu lancer »");
  });

  test("matinée normale : un seul podcast lancé, une seule sonnerie, le téléphone répond, aucun plan B", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "05:50"), at(DAY, "07:10"));

    assert.equal(podcasts().length, 1);
    assert.deepEqual(podcasts()[0].inputs, { silencieux: "true", origine: "sonnette" });
    assert.equal(podcasts()[0].at, at(DAY, "06:15"));
    assert.equal(podcasts()[0].ref, "main");
    assert.deepEqual(world.webhookCalls, [at(DAY, "06:30")]);
    assert.equal(notifying().length, 0, "pas de plan B quand le téléphone répond");
    assert.equal(silent().length, 0);
    const fetched = await env.KV.get("fl", "json");
    assert.equal(fetched.file, "reveil.mp3");
    assert.equal(fetched.at, at(DAY, "06:30", 5));
  });

  test("peu d'écritures KV (limite gratuite : 1 000 par jour)", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "05:50"), at(DAY, "07:10"));
    assert.ok(env.KV.puts <= 20, `${env.KV.puts} écritures pour la matinée`);
    await simulate(env, at(DAY, "07:11"), at(DAY, "23:59"));
    await simulate(env, at("2026-10-09", "00:00"), at("2026-10-09", "05:00"));
    assert.ok(env.KV.puts <= 130, `${env.KV.puts} écritures pour une journée entière`);
  });

  test("le téléphone ne répond pas : plan B dans Telegram, avec notification, une seule fois", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "06:00"), at(DAY, "07:10"), { phone: false });
    assert.deepEqual(world.webhookCalls, [at(DAY, "06:30")]);
    assert.equal(notifying().length, 1);
    assert.equal(notifying()[0].at, at(DAY, "06:32"));
    assert.equal(notifying()[0].chat_id, CHAT);
    assert.match(notifying()[0].text, /6 h 30 — bonjour/);
    assert.match(notifying()[0].text, /juste au-dessus/);
  });

  test("écritures KV visibles avec 60 s de retard : toujours exactement une fois", async () => {
    for (const phone of [true, false]) {
      world = new World();
      globalThis.fetch = (url, init) => world.fetch(url, init);
      const env = { ...baseEnv(), KV: world.kv(60_000) };
      await simulate(env, at(DAY, "06:00"), at(DAY, "07:10"), { phone });
      assert.equal(podcasts().length, 1, `podcast (téléphone ${phone})`);
      assert.equal(world.webhookCalls.length, 1, `sonnerie (téléphone ${phone})`);
      assert.equal(notifying().length, phone ? 0 : 1, `plan B (téléphone ${phone})`);
    }
  });

  test("écritures KV visibles avec 90 s de retard : toujours exactement une sonnerie", async () => {
    const env = { ...baseEnv(), KV: world.kv(90_000) };
    await simulate(env, at(DAY, "06:00"), at(DAY, "07:10"));
    assert.equal(podcasts().length, 1);
    assert.equal(world.webhookCalls.length, 1);
  });

  test("le podcast n'est pas prêt à l'heure : la sonnerie sonne quand même, avec le fichier de sonnerie seule", async () => {
    world.podcastDelayMin = null;            // le workflow échoue
    const env = { ...baseEnv(), KV: world.kv() };
    let served = "";
    for (let t = at(DAY, "06:10"); t <= at(DAY, "07:00"); t += MIN) {
      world.clock = t;
      world.runPending();
      const seen = world.webhookCalls.length;
      await tick(env, t);
      if (world.webhookCalls.length > seen) {
        const res = await route(new Request(`https://x.example/p/${SECRET}/reveil.mp3`), env, t + 5000);
        served = await readText(res);
        assert.equal(res.headers.get("x-reveil"), "sonnerie-seule");
      }
    }
    assert.equal(podcasts().length, 2, "un lancement + un 2e essai");
    assert.equal(podcasts()[1].at, at(DAY, "06:27"));
    assert.equal(world.webhookCalls.length, 1);
    assert.equal(served, "SONNERIE-SEULE", "jamais les actus d'hier");
  });

  test("le podcast arrive après la sonnerie : il est dans Telegram, et la sonnerie ne se rejoue pas", async () => {
    world.podcastDelayMin = 20;
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "06:10"), at(DAY, "07:10"));
    assert.equal(world.webhookCalls.length, 1);
  });

  test("la sonnette n'a pas tourné 3 minutes : rattrapage 2 minutes après l'heure, une seule fois", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "06:00"), at(DAY, "06:45"), { skip: (t) => t >= at(DAY, "06:29") && t <= at(DAY, "06:31") });
    assert.deepEqual(world.webhookCalls, [at(DAY, "06:32")]);
    assert.equal(podcasts().length, 1);
  });

  test("elle ne tourne plus du tout pendant 35 minutes : prévient sans bruit, ne sonne pas, une seule alerte", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "06:00"), at(DAY, "07:30"), { skip: (t) => t > at(DAY, "06:20") && t < at(DAY, "06:55") });
    assert.equal(world.webhookCalls.length, 0);
    assert.equal(silent().filter((m) => /n'a pas sonné/.test(m.text)).length, 1);
    assert.equal(notifying().length, 0);
  });

  test("sans adresse MacroDroid : plan B à l'heure exacte, avec notification", async () => {
    const env = { ...baseEnv({ RING_URL: "" }), KV: world.kv() };
    await simulate(env, at(DAY, "06:00"), at(DAY, "07:00"));
    assert.equal(world.webhookCalls.length, 0);
    assert.equal(notifying().length, 1);
    assert.equal(notifying()[0].at, at(DAY, "06:30"));
    assert.match(notifying()[0].text, /juste au-dessus/, "le podcast est prêt à 6 h 30");
  });

  test("MacroDroid répond en erreur : plan B tout de suite", async () => {
    world.webhookStatus = 500;
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "06:00"), at(DAY, "07:00"));
    assert.equal(world.webhookCalls.length, 1);
    assert.equal(notifying().length, 1);
    assert.equal(notifying()[0].at, at(DAY, "06:30"));
  });

  test("plan B « always » : sonnerie et message en même temps", async () => {
    const env = { ...baseEnv({ DOORBELL: "always" }), KV: world.kv() };
    await simulate(env, at(DAY, "06:00"), at(DAY, "07:00"));
    assert.equal(world.webhookCalls.length, 1);
    assert.equal(notifying().length, 1);
    assert.equal(notifying()[0].at, at(DAY, "06:30"));
  });

  test("plan B « never » : aucun message, même si le téléphone ne répond pas", async () => {
    const env = { ...baseEnv({ DOORBELL: "never" }), KV: world.kv() };
    await simulate(env, at(DAY, "06:00"), at(DAY, "07:00"), { phone: false });
    assert.equal(world.webhookCalls.length, 1);
    assert.equal(notifying().length, 0);
  });

  test("tout est en panne (MacroDroid et Telegram) : 3 essais au plus, une alerte au plus", async () => {
    world.webhookStatus = 500;
    world.telegramSendFails = true;
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "06:00"), at(DAY, "07:00"));
    assert.equal(world.webhookCalls.length, 3);
    assert.ok(world.webhookCalls[1] - world.webhookCalls[0] >= 90_000);
  });

  test("réveil changé APRÈS son heure : ne sonne pas ce jour-là", async () => {
    world.settings = reglages({ jours: { ...WEEK, jeu: "06:25" }, maj: "2026-10-08T06:40:00+02:00" });
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "06:35"), at(DAY, "07:10"));
    assert.equal(world.webhookCalls.length, 0);
    assert.equal(podcasts().length, 0);
    assert.equal(world.sent.length, 0);
  });

  test("réveil changé AVANT son heure, en retard sur le podcast : le podcast est lancé tout de suite, la sonnerie à l'heure", async () => {
    world.settings = reglages({ jours: { ...WEEK, jeu: "06:30" }, maj: "2026-10-08T06:21:00+02:00" });
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "06:23"), at(DAY, "06:40"));
    assert.equal(podcasts().length, 1);
    assert.equal(podcasts()[0].at, at(DAY, "06:23"));
    assert.deepEqual(world.webhookCalls, [at(DAY, "06:30")]);
  });

  test("« pas de réveil demain » : rien ne se passe", async () => {
    world.settings = reglages({ ponctuel: { [DAY]: "off" } });
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "05:50"), at(DAY, "07:10"));
    assert.equal(podcasts().length, 0);
    assert.equal(world.webhookCalls.length, 0);
  });

  test("réveil d'une seule fois à une autre heure (« demain 5 h 45 »)", async () => {
    world.settings = reglages({ ponctuel: { [DAY]: "05:45" }, maj: "2026-10-07T22:00:00+02:00" });
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "05:00"), at(DAY, "07:00"));
    assert.equal(podcasts()[0].at, at(DAY, "05:30"));
    assert.deepEqual(world.webhookCalls, [at(DAY, "05:45")]);
  });

  test("GitHub ne répond pas à l'heure du réveil : la sonnette utilise le dernier horaire connu", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "06:00"), at(DAY, "06:28"));
    world.settingsStatus = 502;
    await simulate(env, at(DAY, "06:29"), at(DAY, "06:40"));
    assert.deepEqual(world.webhookCalls, [at(DAY, "06:30")]);
  });

  test("changement d'heure (25 octobre 2026, dimanche) : sonne à 7 h heure d'hiver, soit 06:00 UTC", async () => {
    world.settings = reglages({ jours: { ...WEEK, dim: "07:00" } });
    world.meta = { date: "2026-10-24", genere_a: "2026-10-24T05:00:00Z", audio: true };
    const env = { ...baseEnv(), KV: world.kv() };
    const from = Date.UTC(2026, 9, 25, 5, 30);
    await simulate(env, from, Date.UTC(2026, 9, 25, 6, 30));
    assert.deepEqual(world.webhookCalls.map((t) => new Date(t).toISOString()), ["2026-10-25T06:00:00.000Z"]);
    assert.equal(podcasts().length, 1);
    assert.equal(new Date(podcasts()[0].at).toISOString(), "2026-10-25T05:45:00.000Z");
  });

  test("pas encore de fichier de réglages : calme plat, aucune alerte", async () => {
    world.settings = null;
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "05:50"), at(DAY, "07:10"));
    assert.equal(podcasts().length + world.webhookCalls.length + world.sent.length, 0);
  });

  test("jeton GitHub refusé : une seule alerte (sans bruit) pour toute la matinée", async () => {
    world.settingsStatus = 401;
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "06:00"), at(DAY, "09:00"));
    const alerts = silent().filter((m) => /refuse mon jeton/.test(m.text));
    assert.equal(alerts.length, 1);
    assert.equal(world.webhookCalls.length, 0);
  });

  test("une minute sans rien à faire coûte peu d'appels (limite gratuite : 50 par passage)", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = at(DAY, "12:00");
    await tick(env, world.clock);
    assert.ok(world.calls.length <= 3, `${world.calls.length} appels`);
  });
});

// ═══════════════════════════════════════════════════════════════ le journal
describe("journal : lancé dans la minute qui suit un message", () => {
  const msg = (id, chat = Number(CHAT)) => ({ update_id: id, message: { message_id: id, chat: { id: chat }, text: "secret : ne doit jamais être lu" } });
  const T = at(DAY, "12:00");

  test("aucun message : rien", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = T;
    await tick(env, T);
    assert.equal(journals().length, 0);
  });

  test("message d'une autre conversation : ignoré", async () => {
    world.updates = [msg(10, 999)];
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = T;
    await tick(env, T);
    assert.equal(journals().length, 0);
  });

  test("message en attente : le journal est lancé une fois, avec l'origine « sonnette »", async () => {
    world.updates = [msg(10)];
    const env = { ...baseEnv(), KV: world.kv() };
    for (let k = 0; k < 4; k++) { world.clock = T + k * MIN; await tick(env, world.clock); }
    assert.equal(journals().length, 1);
    assert.deepEqual(journals()[0].inputs, { origine: "sonnette" });
    assert.equal(journals()[0].ref, "main");
  });

  test("GitHub répond « 200 » avec le détail du lancement (au lieu de « 204 ») : c'est un succès, ni alerte ni doublon", async () => {
    world.dispatchStatus = 200;
    world.updates = [msg(10)];
    const env = { ...baseEnv(), KV: world.kv() };
    for (let k = 0; k < 4; k++) { world.clock = T + k * MIN; await tick(env, world.clock); }     // moins que le délai avant un 2e essai (4 min)
    assert.equal(journals().length, 1);
    assert.equal(world.sent.length, 0);
  });

  test("la sonnette regarde sans rien confirmer ni choisir : pas d'offset, pas de filtre, aucun message lu", async () => {
    world.updates = [msg(10)];
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = T;
    await tick(env, T);
    const peek = JSON.parse(world.tg("getUpdates")[0].body);
    assert.deepEqual(Object.keys(peek).sort(), ["limit", "timeout"]);
    assert.equal(peek.limit, 100);
    assert.equal(peek.timeout, 0);
    assert.ok(!("offset" in peek) && !("allowed_updates" in peek));
    assert.ok(!logs.join("\n").includes("secret : ne doit jamais"), "le contenu des messages n'est jamais écrit dans les journaux");
  });

  test("message déjà traité (le journal a noté son numéro) mais encore en attente chez Telegram : rien à relancer", async () => {
    world.updates = [msg(10)];
    world.settings = { ...reglages(), journal_offset: 11 };
    const env = { ...baseEnv(), KV: world.kv() };
    for (let k = 0; k < 10; k++) { world.clock = T + k * MIN; await tick(env, world.clock); }
    assert.equal(journals().length, 0);
  });

  test("un message plus récent que le décalage du journal le relance, un seul", async () => {
    world.updates = [msg(10), msg(11), msg(12)];
    world.settings = { ...reglages(), journal_offset: 11 };
    const env = { ...baseEnv(), KV: world.kv() };
    for (let k = 0; k < 3; k++) { world.clock = T + k * MIN; await tick(env, world.clock); }
    assert.equal(journals().length, 1);
  });

  test("cycle complet : un message → un seul lancement, même si Telegram garde le message en attente", async () => {
    world.updates = [msg(10)];
    const env = { ...baseEnv(), KV: world.kv() };
    for (let k = 0; k < 20; k++) {
      world.clock = T + k * MIN;
      if (k === 2) world.settings = { ...reglages(), journal_offset: 11 };   // le journal a fini et enregistré son décalage
      world.runs = k >= 1 && k < 2 ? ["in_progress"] : ["completed"];
      await tick(env, world.clock);
    }
    assert.equal(journals().length, 1);
    assert.equal(silent().length, 0);
  });

  test("GitHub ne répond pas : le journal n'est pas lancé à l'aveugle, et Telegram n'est pas interrogé", async () => {
    world.updates = [msg(10)];
    world.settingsStatus = 500;
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = T; await tick(env, T);
    assert.equal(journals().length, 0);
    assert.equal(world.tg("getUpdates").length, 0);
  });

  test("fichier de réglages absent : le décalage est 0 (tout est à traiter)", async () => {
    world.updates = [msg(10)];
    world.settings = null;
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = T; await tick(env, T);
    assert.equal(journals().length, 1);
  });

  test("décalage lu dans le fichier : entier positif seulement (comme reglages.normalize)", () => {
    assert.equal(journalOffset({ journal_offset: 41 }), 41);
    for (const bad of [null, undefined, {}, [], "12", -3, 1.5, true, { journal_offset: "12" }, { journal_offset: -1 }, { journal_offset: 2.5 }, { journal_offset: true }]) {
      assert.equal(journalOffset(bad), 0, JSON.stringify(bad));
    }
  });

  test("nouveau message plus tard : nouveau lancement", async () => {
    world.updates = [msg(10)];
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = T; await tick(env, T);
    world.updates = [msg(11)];
    world.clock = T + 10 * MIN; await tick(env, world.clock);
    assert.equal(journals().length, 2);
  });

  test("le journal tourne déjà : on attend, sans empiler", async () => {
    world.updates = [msg(10)];
    world.runs = ["in_progress"];
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = T; await tick(env, T);
    assert.equal(journals().length, 0);
    world.runs = ["completed"];
    world.clock = T + MIN; await tick(env, world.clock);
    assert.equal(journals().length, 1);
  });

  test("message jamais traité : 3 essais espacés de 4 minutes, une alerte, puis pause", async () => {
    world.updates = [msg(10)];
    const env = { ...baseEnv(), KV: world.kv() };
    for (let k = 0; k < 60; k++) { world.clock = T + k * MIN; await tick(env, world.clock); }
    assert.equal(journals().length, 3);
    assert.ok(journals()[1].at - journals()[0].at >= 4 * MIN);
    assert.equal(silent().filter((m) => /pas été traité/.test(m.text)).length, 1);
  });

  test("plafond quotidien de lancements", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    for (let k = 0; k < LIMITS.maxJournalPerDay + 5; k++) {
      world.updates = [msg(100 + k)];
      world.clock = T + k * MIN;
      await tick(env, world.clock);
    }
    assert.equal(journals().length, LIMITS.maxJournalPerDay);
    assert.equal(silent().filter((m) => /trop de lancements/.test(m.text)).length, 1);
  });

  test("webhook Telegram actif : alerte claire", async () => {
    world.getUpdatesError = { code: 409, description: "Conflict: can't use getUpdates method while webhook is active" };
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = T; await tick(env, T);
    assert.equal(silent().filter((m) => /webhook/.test(m.text)).length, 1);
  });

  test("conflit passager entre deux lectures : silence", async () => {
    world.getUpdatesError = { code: 409, description: "Conflict: terminated by other getUpdates request" };
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = T; await tick(env, T);
    assert.equal(world.sent.length, 0);
  });

  test("lancement refusé par GitHub : alerte, et pas de boucle folle", async () => {
    world.updates = [msg(10)];
    world.dispatchStatus = 403;
    const env = { ...baseEnv(), KV: world.kv() };
    for (let k = 0; k < 30; k++) { world.clock = T + k * MIN; await tick(env, world.clock); }
    assert.equal(silent().filter((m) => /pas pu lancer/.test(m.text)).length, 1);
    assert.ok(world.calls.filter((c) => c.url.includes("/dispatches")).length <= 3);
  });
});

// ═══════════════════════════════════════════════════════════════ les adresses secrètes
describe("adresses secrètes", () => {
  const url = (path) => new Request(`https://reveil.example${path}`);

  test("mauvais ou absent secret : 404 muet", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    for (const path of ["/", "/p/", `/p/mauvais/reveil.mp3`, `/status/${SECRET}x`, `/status`, `/test/${SECRET.slice(1)}/ring`, `/autre/${SECRET}`, `/p/${SECRET}/autre.mp3`, `/p/${SECRET}/reveil.mp3/extra`]) {
      const res = await route(url(path), env, WAKE);
      assert.equal(res.status, 404, path);
      assert.equal(await res.text(), "Not found");
    }
  });

  test("un secret trop court désactive toutes les adresses", async () => {
    const env = { ...baseEnv({ SECRET: "court" }), KV: world.kv() };
    assert.equal((await route(url("/status/court"), env, WAKE)).status, 404);
  });

  test("fichier du jour : le podcast avec sonnerie, et la sonnette note que le téléphone est venu", async () => {
    world.meta = { date: DAY, genere_a: new Date(WAKE - 10 * MIN).toISOString(), audio: true };
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = WAKE;
    const res = await route(url(`/p/${SECRET}/reveil.mp3`), env, WAKE + 5000);
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("content-type"), "audio/mpeg");
    assert.equal(res.headers.get("x-reveil"), "podcast");
    assert.equal(res.headers.get("cache-control"), "no-store");
    assert.equal(await readText(res), "REVEIL=BIP+PODCAST");
    assert.deepEqual(await env.KV.get("fl", "json"), { at: WAKE + 5000, file: "reveil.mp3" });
  });

  test("podcast d'hier : sonnerie seule", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    const res = await route(url(`/p/${SECRET}/reveil.mp3`), env, WAKE);
    assert.equal(await readText(res), "SONNERIE-SEULE");
    assert.equal(res.headers.get("x-reveil"), "sonnerie-seule");
  });

  test("sans fichier reveil.mp3 (sonnerie désactivée), on sert le podcast seul", async () => {
    world.meta = { date: DAY, genere_a: new Date(WAKE - 10 * MIN).toISOString(), audio: true };
    delete world.files["reveil.mp3"];
    const env = { ...baseEnv(), KV: world.kv() };
    assert.equal(await readText(await route(url(`/p/${SECRET}/reveil.mp3`), env, WAKE)), "PODCAST-SEUL");
  });

  test("aucun fichier : erreur claire, pas d'écran blanc", async () => {
    world.files = {};
    const env = { ...baseEnv(), KV: world.kv() };
    const res = await route(url(`/p/${SECRET}/reveil.mp3`), env, WAKE);
    assert.equal(res.status, 404);
    assert.match((await res.json()).erreur, /aucun fichier/);
  });

  test("un essai de téléchargement fait AVANT la sonnerie ne compte pas comme une réponse du téléphone", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = at(DAY, "06:20");
    await (await route(url(`/p/${SECRET}/reveil.mp3`), env, world.clock)).arrayBuffer();   // l'utilisateur teste son lien
    await simulate(env, at(DAY, "06:21"), at(DAY, "06:40"), { phone: false });
    assert.equal(world.webhookCalls.length, 1);
    assert.equal(notifying().length, 1, "le plan B part, car le téléphone n'a rien téléchargé après la sonnerie");
  });

  test("téléchargements en rafale : une seule écriture KV toutes les 10 secondes", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = WAKE;
    for (let k = 0; k < 6; k++) await (await route(url(`/p/${SECRET}/reveil.mp3`), env, WAKE + k * 1000)).arrayBuffer();
    assert.equal(env.KV.puts, 1);
  });

  test("état : lisible, et sans aucun secret", async () => {
    world.meta = { date: DAY, genere_a: new Date(WAKE - 10 * MIN).toISOString(), audio: true, mode: "normal", origine: "sonnette" };
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = at(DAY, "05:00");
    const res = await route(url(`/status/${SECRET}`), env, world.clock);
    const text = await res.text();
    const data = JSON.parse(text);
    assert.match(data.resume[0], /jeudi 8 octobre à 6 h 30 \(dans 90 min\)/);
    assert.match(data.resume[1], /préparé vers 6 h 15/);
    assert.equal(data.configuration.adresse_macrodroid, true);
    for (const secret of [TG_TOKEN, GH_TOKEN, SECRET, RING_URL, "trigger.macrodroid"]) assert.ok(!text.includes(secret), `fuite : ${secret}`);
  });

  test("essais : sonnerie, message, podcast ; deux essais coup sur coup → 429", async () => {
    const env = { ...baseEnv(), KV: world.kv() };
    world.clock = WAKE;
    let res = await route(url(`/test/${SECRET}/ring`), env, WAKE);
    assert.equal(res.status, 200);
    assert.equal(world.webhookCalls.length, 1);
    res = await route(url(`/test/${SECRET}/ring`), env, WAKE + 5000);
    assert.equal(res.status, 429);
    assert.equal(world.webhookCalls.length, 1);
    world.clock = WAKE + 30_000;
    res = await route(url(`/test/${SECRET}/doorbell`), env, WAKE + 30_000);
    assert.equal(res.status, 200);
    assert.equal(notifying().length, 1);
    world.clock = WAKE + 60_000;
    res = await route(url(`/test/${SECRET}/podcast`), env, WAKE + 60_000);
    assert.equal(res.status, 200);
    assert.deepEqual(podcasts()[0].inputs, { silencieux: "true", origine: "sonnette" });
    res = await route(url(`/test/${SECRET}/inconnu`), env, WAKE + 120_000);
    assert.equal(res.status, 404);
  });

  test("essai de sonnerie sans adresse MacroDroid : message d'aide", async () => {
    const env = { ...baseEnv({ RING_URL: "" }), KV: world.kv() };
    const res = await route(url(`/test/${SECRET}/ring`), env, WAKE);
    assert.equal(res.status, 400);
    assert.match((await res.json()).erreur, /RING_URL/);
  });

  test("essai de podcast refusé par GitHub : explication", async () => {
    world.dispatchStatus = 403;
    const env = { ...baseEnv(), KV: world.kv() };
    const res = await route(url(`/test/${SECRET}/podcast`), env, WAKE);
    assert.equal(res.status, 502);
    assert.match((await res.json()).conseil, /droits du jeton/);
  });
});

// ═══════════════════════════════════════════════════════════════ hygiène des secrets
describe("secrets : chaque clé ne va que chez son destinataire, jamais dans les journaux", () => {
  test("une matinée entière avec un message à traiter", async () => {
    world.updates = [{ update_id: 1, message: { chat: { id: Number(CHAT) }, text: "bonjour" } }];
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "06:00"), at(DAY, "06:40"));
    for (const call of world.calls) {
      const blob = JSON.stringify(call.headers) + String(call.body || "");
      if (call.url.startsWith("https://api.github.com/")) {
        assert.ok(!blob.includes(TG_TOKEN) && !call.url.includes(TG_TOKEN), "clé Telegram chez GitHub");
        assert.equal(call.headers.authorization, `Bearer ${GH_TOKEN}`);
      } else if (call.url.startsWith("https://api.telegram.org/")) {
        assert.ok(!blob.includes(GH_TOKEN), "jeton GitHub chez Telegram");
      } else {
        assert.ok(!blob.includes(GH_TOKEN) && !blob.includes(TG_TOKEN) && !blob.includes(SECRET), "clé envoyée à MacroDroid");
        assert.equal(call.headers.authorization, undefined);
      }
    }
    const out = logs.join("\n");
    for (const secret of [TG_TOKEN, GH_TOKEN, SECRET, RING_URL, "trigger.macrodroid"]) assert.ok(!out.includes(secret), `fuite dans les journaux : ${secret}`);
  });

  test("même en cas d'erreurs partout, rien ne fuit dans les journaux", async () => {
    world.settingsStatus = 500;
    world.webhookStatus = 500;
    world.telegramSendFails = true;
    world.dispatchStatus = 403;
    world.updates = [{ update_id: 1, message: { chat: { id: Number(CHAT) }, text: "x" } }];
    const env = { ...baseEnv(), KV: world.kv() };
    await simulate(env, at(DAY, "06:00"), at(DAY, "06:40"));
    const out = logs.join("\n");
    for (const secret of [TG_TOKEN, GH_TOKEN, SECRET, RING_URL, "trigger.macrodroid"]) assert.ok(!out.includes(secret), `fuite dans les journaux : ${secret}`);
  });
});

describe("gestionnaires Cloudflare", () => {
  test("fetch et scheduled sont bien exposés", async () => {
    assert.equal(typeof worker.fetch, "function");
    assert.equal(typeof worker.scheduled, "function");
    const env = { ...baseEnv(), KV: world.kv() };
    let waited;
    world.clock = at(DAY, "12:00");
    worker.scheduled({ scheduledTime: world.clock }, env, { waitUntil: (p) => { waited = p; } });
    const result = await waited;
    assert.ok(result.wake && result.journal);
    const res = await worker.fetch(new Request("https://x.example/"), env);
    assert.equal(res.status, 404);
  });

  test("sans KV : message clair dans les journaux, aucune exception", async () => {
    const result = await tick({ ...baseEnv() }, at(DAY, "12:00"));
    assert.equal(result.error, "KV manquant");
    assert.match(logs.join("\n"), /KV namespace/);
  });
});
