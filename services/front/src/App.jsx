import { useEffect, useState } from "react";

const api = async (path, token, opts = {}) => {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(opts.headers || {}) },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(body.detail?.reason || body.detail || r.statusText), { status: r.status });
  return body;
};

export default function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem("token") || "");
  const [email, setEmail] = useState("alice@demo.local");
  const [password, setPassword] = useState("");
  const [accounts, setAccounts] = useState([]);
  const [txs, setTxs] = useState([]);
  const [form, setForm] = useState({ to_account: 10, amount: "12.50" });
  const [msg, setMsg] = useState(null);

  const refresh = async (t = token) => {
    if (!t) return;
    try {
      const [a, x] = await Promise.all([api("/api/accounts/me", t), api("/api/transactions?limit=20", t)]);
      setAccounts(a); setTxs(x); setMsg((m) => (m?.kind === "down" ? null : m));
    } catch (e) {
      if (e.status === 401) { setToken(""); sessionStorage.removeItem("token"); }
      else setMsg({ kind: "down", text: `Service indisponible (${e.status || "réseau"})` });
    }
  };
  useEffect(() => { refresh(); const id = setInterval(refresh, 3000); return () => clearInterval(id); }, [token]);

  const login = async (e) => {
    e.preventDefault();
    try {
      const { access_token } = await api("/auth/login", null, { method: "POST", body: JSON.stringify({ email, password }) });
      sessionStorage.setItem("token", access_token); setToken(access_token); setMsg(null);
    } catch (e) { setMsg({ kind: "err", text: e.message }); }
  };
  const pay = async (e) => {
    e.preventDefault();
    try {
      const tx = await api("/api/transactions", token, { method: "POST", body: JSON.stringify({ to_account: Number(form.to_account), amount: form.amount }) });
      setMsg({ kind: "ok", text: `Paiement #${tx.id} de ${tx.amount} € effectué` }); refresh();
    } catch (e) { setMsg({ kind: "err", text: `Paiement refusé : ${e.message}` }); }
  };
  const logout = () => { sessionStorage.removeItem("token"); setToken(""); setTxs([]); setAccounts([]); };

  const banner = msg && (
    <div className={`rounded-lg px-4 py-2 text-sm ${msg.kind === "ok" ? "bg-emerald-100 text-emerald-900" : msg.kind === "down" ? "bg-amber-100 text-amber-900" : "bg-rose-100 text-rose-900"}`}>{msg.text}</div>
  );

  if (!token) return (
    <main className="mx-auto mt-16 max-w-sm space-y-4 rounded-2xl bg-white p-8 shadow">
      <h1 className="text-2xl font-bold">AlphaPay <span className="text-xs font-medium text-slate-400">SI fictif — workshop</span></h1>
      {banner}
      <form onSubmit={login} className="space-y-3">
        <input id="email" className="w-full rounded-lg border px-3 py-2" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="email" />
        <input id="password" type="password" className="w-full rounded-lg border px-3 py-2" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="mot de passe" />
        <button className="w-full rounded-lg bg-indigo-600 px-3 py-2 font-semibold text-white hover:bg-indigo-700">Se connecter</button>
      </form>
      <p className="text-xs text-slate-500">Comptes de démo : alice / bob / carol @demo.local — mot de passe DEMO_PASSWORD</p>
    </main>
  );

  return (
    <main className="mx-auto max-w-3xl space-y-6 p-6">
      <header className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">AlphaPay</h1>
        <button onClick={logout} className="text-sm text-slate-500 hover:underline">Déconnexion</button>
      </header>
      {banner}
      <section className="grid gap-4 sm:grid-cols-2">
        {accounts.map((a) => (
          <div key={a.id} className="rounded-2xl bg-white p-5 shadow">
            <div className="text-xs uppercase tracking-wide text-slate-400">Compte #{a.id} · {a.label}</div>
            <div className="mt-1 text-3xl font-bold tabular-nums">{Number(a.balance).toFixed(2)} <span className="text-base font-medium text-slate-400">{a.currency}</span></div>
          </div>
        ))}
        <form onSubmit={pay} className="space-y-3 rounded-2xl bg-white p-5 shadow">
          <div className="font-semibold">Nouveau paiement</div>
          <label className="block text-sm">Vers le compte
            <input id="to_account" type="number" min="1" className="mt-1 w-full rounded-lg border px-3 py-2" value={form.to_account} onChange={(e) => setForm({ ...form, to_account: e.target.value })} />
          </label>
          <label className="block text-sm">Montant (€)
            <input id="amount" type="number" step="0.01" min="0.01" className="mt-1 w-full rounded-lg border px-3 py-2" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} />
          </label>
          <button className="w-full rounded-lg bg-indigo-600 px-3 py-2 font-semibold text-white hover:bg-indigo-700">Payer</button>
        </form>
      </section>
      <section className="rounded-2xl bg-white shadow">
        <div className="border-b px-5 py-3 font-semibold">Dernières transactions</div>
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase text-slate-400"><tr><th className="px-5 py-2">#</th><th>De → vers</th><th>Montant</th><th>Statut</th><th>Origine</th><th>Date</th></tr></thead>
          <tbody>
            {txs.map((t) => (
              <tr key={t.id} className="border-t">
                <td className="px-5 py-2 tabular-nums">{t.id}</td>
                <td>{t.from_account} → {t.to_account}</td>
                <td className="tabular-nums">{Number(t.amount).toFixed(2)} €</td>
                <td><span className={`rounded-full px-2 py-0.5 text-xs ${t.status === "completed" ? "bg-emerald-100 text-emerald-800" : "bg-rose-100 text-rose-800"}`}>{t.status}</span></td>
                <td className="text-slate-500">{t.origin}</td>
                <td className="text-slate-500">{new Date(t.created_at).toLocaleTimeString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}
