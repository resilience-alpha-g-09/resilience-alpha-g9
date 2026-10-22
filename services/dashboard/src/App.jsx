import { useEffect, useRef, useState } from "react";
import Assistant from "./Assistant.jsx";

// Une seule page pour tout voir pendant l'incident : état et reprise (remédiateur), métriques (Grafana, panneaux choisis),
// accès au back-office (LED, dernières identifications). Servie sous /supervision/ derrière l'edge — même origine que Grafana et le back-office.
const B = import.meta.env.BASE_URL;                 // "/supervision/"
const GRAFANA = "/grafana";                          // via l'edge (origine unique)
const STATES = {
  0: { name: "IDLE", color: "bg-emerald-500", phase: "AVANT" },
  1: { name: "DETECTED", color: "bg-amber-500", phase: "PENDANT" },
  2: { name: "MITIGATING", color: "bg-rose-500", phase: "PENDANT" },
  3: { name: "VERIFYING", color: "bg-yellow-400", phase: "PENDANT" },
  4: { name: "RECOVERED", color: "bg-emerald-500", phase: "APRÈS" },
  5: { name: "FAILED", color: "bg-rose-700", phase: "ÉCHEC" },
};
const LED = {
  off: { label: "éteinte", cls: "bg-slate-600" },
  green_blink: { label: "verte clignotante — visage reconnu, mot de passe attendu", cls: "bg-emerald-400 animate-pulse" },
  green_solid: { label: "verte fixe — accès ouvert", cls: "bg-emerald-400" },
  red_blink: { label: "rouge ×10 — mot de passe refusé", cls: "bg-rose-500 animate-pulse" },
  red_solid: { label: "rouge fixe — visage non reconnu", cls: "bg-rose-500" },
};
const fmt = (v, d = 2, suffix = "") => (v == null || Number.isNaN(v) ? "—" : `${Number(v).toFixed(d)}${suffix}`);
const pct = (v) => (v == null ? "—" : `${Math.round(v * 100)} %`);
const hhmmss = (ts) => new Date(ts).toLocaleTimeString();

function Spark({ data, color = "#818cf8", min, max, h = 36 }) {
  const w = 160;
  const pts = data.filter((v) => v != null);
  if (pts.length < 2) return <svg width={w} height={h} />;
  const lo = min ?? Math.min(...pts), hi = max ?? Math.max(...pts), span = hi - lo || 1;
  const d = data.map((v, i) => `${(i / (data.length - 1)) * w},${v == null ? h : h - ((v - lo) / span) * (h - 4) - 2}`).join(" ");
  return <svg width={w} height={h} className="overflow-visible"><polyline fill="none" stroke={color} strokeWidth="2" points={d} /></svg>;
}

function Tile({ label, value, sub, spark, tone = "" }) {
  return (
    <div className={`rounded-xl border border-slate-800 bg-slate-900 p-4 ${tone}`}>
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-1 flex items-end justify-between gap-2"><div className="text-2xl font-bold tabular-nums">{value}</div>{spark}</div>
      {sub && <div className="mt-1 text-xs text-slate-400">{sub}</div>}
    </div>
  );
}

function GrafanaPanel({ id, title }) {
  const src = `${GRAFANA}/d-solo/ra-plateforme/?orgId=1&panelId=${id}&refresh=5s&from=now-15m&to=now&theme=dark`;
  return (
    <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900">
      <div className="flex items-center justify-between px-3 py-1.5 text-xs uppercase tracking-wide text-slate-400">
        <span>{title}</span><a className="normal-case text-indigo-300 hover:underline" href={`${GRAFANA}/d/ra-plateforme/?orgId=1&viewPanel=${id}`} target="_blank" rel="noreferrer">Grafana ⤢</a>
      </div>
      <iframe title={title} src={src} className="block h-56 w-full border-0 bg-slate-950" loading="lazy" />
    </div>
  );
}

const RESULT_TONE = { recognized: "bg-emerald-900/50 text-emerald-200", success: "bg-emerald-900/50 text-emerald-200", password_ok: "bg-emerald-900/50 text-emerald-200",
  camera_started: "bg-slate-800 text-slate-300", locked: "bg-rose-900/50 text-rose-200", rejected: "bg-rose-900/50 text-rose-200", camera_unavailable: "bg-amber-900/50 text-amber-200" };
const STEP_LABEL = { identify: "identifiant", face: "caméra", password: "mot de passe", bootstrap: "amorçage" };
const RESULT_LABEL = { recognized: "visage reconnu", rejected: "refusé", success: "connexion", camera_started: "caméra activée", locked: "bloqué", camera_unavailable: "caméra indisponible", password_ok: "mot de passe OK" };

export default function App() {
  const [state, setState] = useState(null);
  const [events, setEvents] = useState([]);
  const [summary, setSummary] = useState(null);
  const [led, setLed] = useState(null);
  const [access, setAccess] = useState(null);
  const hist = useRef({ tx: [], p95: [], err: [], score: [] });
  const [, force] = useState(0);

  useEffect(() => {
    let alive = true;
    const j = (p) => fetch(B + p).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    const poll = async () => {
      const [s, e, m, l, a] = await Promise.all([j("bff/state"), j("bff/events?limit=40"), j("bff/summary"), j("bo/led-state"), j("bo/access-log")]);
      if (!alive) return;
      if (s) setState(s); else setState((prev) => (prev ? { ...prev, unreachable: true } : prev));
      if (e) setEvents(e);
      if (m) {
        setSummary(m);
        const push = (k, v) => { hist.current[k] = [...hist.current[k].slice(-89), v]; };
        push("tx", m.tx_rate); push("p95", m.api_p95); push("err", m.api_error_ratio); push("score", m.anomaly_score);
      }
      setLed(l); setAccess(a);
      force((n) => n + 1);
    };
    poll(); const id = setInterval(poll, 2000); return () => { alive = false; clearInterval(id); };
  }, []);

  const st = STATES[state?.state ?? 0];
  const inc = state?.current_incident || state?.last_incident;
  const services = state?.services || {};
  const errTone = summary?.api_error_ratio > 0.2 ? "ring-1 ring-rose-500" : "";
  const ledInfo = LED[led?.state] || { label: led ? led.state : "back-office injoignable", cls: "bg-slate-700" };
  const lastAccess = access?.attempts?.[0];

  return (
    <main className="mx-auto max-w-7xl space-y-5 p-5">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-xs uppercase tracking-widest text-slate-400">PROJECT RESILIENCE-ALPHA · consortium G9</div>
          <h1 className="text-2xl font-bold">Supervision &amp; reprise autonome</h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-full border border-slate-700 px-3 py-1 text-xs font-semibold tracking-wide text-slate-300">{st.phase}</span>
          <span className={`flex items-center gap-2 rounded-full px-4 py-1.5 text-sm font-bold text-slate-950 ${st.color}`}><span className="h-2 w-2 animate-pulse rounded-full bg-slate-950/60" />{st.name}</span>
          <span className="flex items-center gap-2 rounded-full border border-slate-700 px-3 py-1.5 text-xs font-semibold text-slate-200" title={ledInfo.label}>
            <span className={`h-3 w-3 rounded-full ${ledInfo.cls}`} /> LED {led?.state || "—"}
          </span>
          <nav className="flex gap-1 text-xs">
            <a className="rounded-md border border-slate-700 px-2.5 py-1.5 font-semibold text-slate-200 hover:border-indigo-400" href="/" target="_blank" rel="noreferrer">Client ⤢</a>
            <a className="rounded-md border border-slate-700 px-2.5 py-1.5 font-semibold text-slate-200 hover:border-indigo-400" href="/admin/" target="_blank" rel="noreferrer">Back-office ⤢</a>
            <a className="rounded-md border border-slate-700 px-2.5 py-1.5 font-semibold text-slate-200 hover:border-indigo-400" href={`${GRAFANA}/d/ra-plateforme/?orgId=1`} target="_blank" rel="noreferrer">Grafana ⤢</a>
          </nav>
        </div>
      </header>

      {state?.unreachable && <div className="rounded-lg bg-amber-900/40 px-4 py-2 text-sm text-amber-200">Remédiateur injoignable — données figées</div>}

      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {Object.entries(services).map(([name, h]) => (
          <Tile key={name} label={`service ${name}`} value={h.ok == null ? "…" : h.ok ? "UP" : "DOWN"}
            sub={h.ok ? "health 200" : `${h.consecutive_failures} échec(s) consécutif(s)`} tone={h.ok === false ? "ring-1 ring-rose-500" : ""} />
        ))}
        <Tile label="paiements / s" value={fmt(summary?.tx_rate, 1)} spark={<Spark data={hist.current.tx} min={0} />} />
        <Tile label="latence p95 api" value={fmt(summary?.api_p95 == null ? null : summary.api_p95 * 1000, 0, " ms")} spark={<Spark data={hist.current.p95} min={0} color="#fbbf24" />} />
        <Tile label="erreurs 5xx" value={pct(summary?.api_error_ratio)} spark={<Spark data={hist.current.err} min={0} max={1} color="#f43f5e" />} tone={errTone} />
        <Tile label="succès paiements simulés" value={pct(summary?.sim_success_ratio)} />
        <Tile label="score anomalie (ML)" value={fmt(summary?.anomaly_score, 3)} sub={summary?.anomaly_threshold == null ? "pas de modèle chargé" : `seuil ${fmt(summary.anomaly_threshold, 3)}`}
          spark={<Spark data={hist.current.score} color="#a78bfa" />} tone={summary?.anomaly_threshold != null && summary?.anomaly_score < summary?.anomaly_threshold ? "ring-1 ring-violet-500" : ""} />
        <Tile label="sources bloquées / étranglées" value={state ? `${state.blocked_ips?.length ?? 0} / ${state.throttled_ips?.length ?? 0}` : "—"}
          sub={[...(state?.blocked_ips || []).map((ip) => `${ip} bloquée`), ...(state?.throttled_ips || []).map((ip) => `${ip} étranglée (NAT)`)].join(", ") || "aucune"} />
      </section>

      <section className="grid gap-3 lg:grid-cols-3">
        <GrafanaPanel id={11} title="Latence p95 — api / auth" />
        <GrafanaPanel id={12} title="Taux d'erreur API" />
        <GrafanaPanel id={15} title="Score d'anomalie vs seuil" />
      </section>

      <section className="grid gap-4 lg:grid-cols-5">
        <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 lg:col-span-2">
          <div className="mb-2 text-xs uppercase tracking-wide text-slate-400">{state?.current_incident ? "Incident en cours" : "Dernier incident"}</div>
          {!inc ? <div className="text-slate-500">Aucun incident depuis le démarrage.</div> : (
            <div className="space-y-2 text-sm">
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                <span>source <b>{inc.source}</b></span><span>raison <b>{inc.reason}</b></span>
                {inc.suspect_ip && <span>IP suspecte <b>{inc.suspect_ip}</b></span>}
                {inc.score != null && <span>score <b>{fmt(inc.score, 3)}</b></span>}
              </div>
              <div className="text-xs text-slate-400">début {hhmmss(inc.started_at)}{inc.ended_at && ` · fin ${hhmmss(inc.ended_at)}`}</div>
              <ol className="space-y-1">
                {inc.actions.map((a, i) => (
                  <li key={i} className="flex gap-2"><span className="text-slate-500">{i + 1}.</span><span className="font-mono text-xs">{a.action} {a.target}</span><span className="ml-auto text-xs text-slate-400">{a.result}</span></li>
                ))}
              </ol>
              {inc.outcome && (
                <div className={`mt-2 rounded-lg px-3 py-2 font-semibold ${inc.outcome === "recovered" ? "bg-emerald-900/50 text-emerald-200" : "bg-rose-900/50 text-rose-200"}`}>
                  {inc.outcome === "recovered" ? "✅ Service rétabli — transaction de vérification OK" : "❌ Reprise échouée"} · MTTR {fmt(inc.mttr_seconds, 1, " s")}
                </div>
              )}
            </div>
          )}
        </div>
        <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 lg:col-span-3">
          <div className="mb-2 text-xs uppercase tracking-wide text-slate-400">Timeline des événements</div>
          <ul className="max-h-72 space-y-1 overflow-y-auto font-mono text-xs">
            {events.map((e, i) => (
              <li key={i} className={`flex gap-3 ${e.level === "error" ? "text-rose-300" : e.level === "warn" ? "text-amber-300" : "text-slate-300"}`}>
                <span className="shrink-0 text-slate-500">{hhmmss(e.ts)}</span>
                <span className="shrink-0 font-semibold">{e.msg}</span>
                <span className="truncate text-slate-400">{Object.entries(e).filter(([k]) => !["ts", "level", "msg"].includes(k)).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ")}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900 p-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div className="text-xs uppercase tracking-wide text-slate-400">Accès au back-office — identifiant → caméra → mot de passe</div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className={`flex items-center gap-2 rounded-full px-3 py-1 font-semibold ${ledInfo.cls.includes("rose") ? "bg-rose-900/50 text-rose-200" : ledInfo.cls.includes("emerald") ? "bg-emerald-900/50 text-emerald-200" : "bg-slate-800 text-slate-300"}`}>
              <span className={`h-2.5 w-2.5 rounded-full ${ledInfo.cls}`} /> LED {ledInfo.label}
            </span>
            {access?.locked?.length > 0 && <span className="rounded-full bg-rose-900/50 px-3 py-1 font-semibold text-rose-200">bloqué : {access.locked.map((l) => `${l.login} → ${hhmmss(l.locked_until)}`).join(" · ")}</span>}
            {access && <span className="text-slate-400">{access.counts?.enrolled ?? 0} visage(s) enrôlé(s) · {access.counts?.active ?? 0} opérateur(s) actif(s)</span>}
          </div>
        </div>
        {!access ? <div className="text-sm text-slate-500">Back-office injoignable.</div> : access.attempts.length === 0 ? <div className="text-sm text-slate-500">Aucune tentative d'accès pour le moment.</div> : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-slate-500"><tr><th className="py-1 pr-3">Quand</th><th className="pr-3">Identifiant</th><th className="pr-3">Étape</th><th className="pr-3">Résultat</th><th>Détail</th></tr></thead>
            <tbody>
              {access.attempts.map((a) => (
                <tr key={a.id} className="border-t border-slate-800">
                  <td className="py-1 pr-3 font-mono text-xs text-slate-400">{hhmmss(a.created_at)}</td>
                  <td className="pr-3 font-semibold">{a.login}</td>
                  <td className="pr-3 text-slate-300">{STEP_LABEL[a.step] || a.step}</td>
                  <td className="pr-3"><span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${RESULT_TONE[a.result] || "bg-slate-800 text-slate-300"}`}>{RESULT_LABEL[a.result] || a.result}</span></td>
                  <td className="text-xs text-slate-500">{a.detail || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {lastAccess && <div className="mt-2 text-xs text-slate-500">Dernier événement : {hhmmss(lastAccess.created_at)} — {lastAccess.login}, {STEP_LABEL[lastAccess.step] || lastAccess.step} → {RESULT_LABEL[lastAccess.result] || lastAccess.result}</div>}
      </section>

      <Assistant incident={inc} stateName={st.name} />
    </main>
  );
}
