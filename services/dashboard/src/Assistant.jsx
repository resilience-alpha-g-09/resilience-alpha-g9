import { useEffect, useState } from "react";

// Assistant opérateur (RAG) : consultatif. Il lit docs/ et n'a aucun droit sur la plateforme — c'est le remédiateur qui agit.
const BASE_SUGGESTIONS = [
  "Quelle est la procédure de reprise de l'API ?",
  "Que fait le remédiateur quand l'authentification sature ?",
  "Que faire si l'état passe à FAILED ?",
];

// Rendu minimal du Markdown renvoyé par le modèle (**gras**, `code`), sans injecter de HTML
function Inline({ text }) {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) return <b key={i}>{part.slice(2, -2)}</b>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={i} className="rounded bg-slate-800 px-1 font-mono text-xs">{part.slice(1, -1)}</code>;
    return part;
  });
}

export default function Assistant({ incident, stateName }) {
  const [health, setHealth] = useState(null);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [reply, setReply] = useState(null);

  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const r = await fetch(import.meta.env.BASE_URL+"assistant/health");
        const h = r.ok ? await r.json() : null;
        if (alive) setHealth(h);
      } catch { if (alive) setHealth(null); }
    };
    check(); const id = setInterval(check, 15000); return () => { alive = false; clearInterval(id); };
  }, []);

  useEffect(() => {
    if (!busy) return;
    const t0 = Date.now(); setElapsed(0);
    const id = setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 500);
    return () => clearInterval(id);
  }, [busy]);

  const ready = health?.status === "ok";
  const suggestions = incident?.reason && !["IDLE", "RECOVERED"].includes(stateName)
    ? [`Que signifie un incident « ${incident.reason} » et quelle est la procédure ?`, ...BASE_SUGGESTIONS.slice(0, 2)]
    : BASE_SUGGESTIONS;

  const ask = async (q) => {
    const text = (q ?? question).trim();
    if (text.length < 3 || busy) return;
    setQuestion(text); setBusy(true); setReply(null);
    try {
      const r = await fetch(import.meta.env.BASE_URL+"assistant/ask", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: text }),
      });
      if (r.status === 429) setReply({ error: "Trop de questions d'affilée — réessayer dans quelques secondes." });
      else if (!r.ok) setReply({ error: r.status === 422 ? "Question invalide (3 à 500 caractères)." : "Assistant indisponible." });
      else {
        const body = await r.json();
        setReply(body.error ? { error: `Index non prêt : ${body.error}` } : body);
      }
    } catch { setReply({ error: "Assistant indisponible." }); }
    setBusy(false);
  };

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-400">Assistant opérateur (RAG local)</div>
          <div className="text-xs text-slate-500">Consultatif : répond à partir des procédures du projet, n'agit pas sur la plateforme.</div>
        </div>
        <span className={`rounded-full px-3 py-1 text-xs font-semibold ${ready ? "bg-emerald-900/50 text-emerald-200" : "bg-slate-800 text-slate-400"}`}>
          {ready ? `en ligne · ${health.model}` : health ? "index en construction" : "hors ligne (profil ai)"}
        </span>
      </div>

      <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); ask(); }}>
        <input value={question} onChange={(e) => setQuestion(e.target.value)} maxLength={500} disabled={!ready || busy}
          placeholder={ready ? "Poser une question sur une procédure, un composant, un incident…" : "Assistant non démarré : make assistant-up"}
          className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm placeholder:text-slate-500 focus:border-indigo-400 focus:outline-none disabled:opacity-50" />
        <button type="submit" disabled={!ready || busy || question.trim().length < 3}
          className="rounded-lg bg-indigo-500 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-400 disabled:opacity-40">
          {busy ? `${elapsed} s…` : "Demander"}
        </button>
      </form>

      <div className="mt-2 flex flex-wrap gap-2">
        {suggestions.map((s) => (
          <button key={s} type="button" disabled={!ready || busy} onClick={() => ask(s)}
            className="rounded-full border border-slate-700 px-3 py-1 text-xs text-slate-300 hover:border-indigo-400 disabled:opacity-40">{s}</button>
        ))}
      </div>

      {reply && (
        <div className="mt-4 space-y-2 text-sm">
          {reply.error ? <div className="rounded-lg bg-amber-900/40 px-3 py-2 text-amber-200">{reply.error}</div> : (
            <>
              <div className="space-y-1 leading-relaxed text-slate-200">
                {reply.answer.split("\n").filter((l) => l.trim()).map((l, i) => <p key={i}><Inline text={l} /></p>)}
              </div>
              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
                <span>sources :</span>
                {reply.sources.map((s) => <code key={s} className="rounded bg-slate-800 px-1.5 py-0.5 font-mono">docs/{s}</code>)}
              </div>
            </>
          )}
        </div>
      )}
    </section>
  );
}
