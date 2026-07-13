import { FormEvent, useState } from "react";
import { login } from "../api";

// Equivalente React de login.html. En éxito hace un full reload a "/" para que
// el backend valide la cookie de sesión y sirva el dashboard.
export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const res = await login(username, password);
      if (res.ok) {
        window.location.href = "/";
        return;
      }
      setError(res.detail || "No se pudo iniciar sesión");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Error de red");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="flex flex-col items-center mb-6">
          <div className="w-14 h-14 rounded-2xl bg-navy-900 flex items-center justify-center text-white font-bold text-2xl shadow-card tracking-tight">
            H
          </div>
          <h1 className="text-lg font-bold text-navy-900 mt-3">Hugo</h1>
          <p className="text-xs text-navy-500">Control de calidad · catálogo B2Box</p>
        </div>

        <form
          onSubmit={onSubmit}
          className="bg-white rounded-2xl border border-navy-200 shadow-card p-6 fade-in"
        >
          <h2 className="text-base font-semibold text-navy-900 mb-4">Ingresar</h2>

          <label className="block text-sm font-medium text-navy-700 mb-1" htmlFor="username">
            Email
          </label>
          <input
            id="username"
            name="username"
            type="email"
            autoComplete="username email"
            required
            placeholder="tu@b2box.pro"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="w-full px-3 py-2 rounded-lg border border-navy-300 bg-navy-50 text-sm focus:bg-white focus:border-brand-500 outline-none transition mb-4"
          />

          <label className="block text-sm font-medium text-navy-700 mb-1" htmlFor="password">
            Contraseña
          </label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full px-3 py-2 rounded-lg border border-navy-300 bg-navy-50 text-sm focus:bg-white focus:border-brand-500 outline-none transition mb-4"
          />

          {error && (
            <div className="mb-4 p-2.5 rounded-lg text-xs bg-rose-50 border border-rose-200 text-rose-800">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={submitting}
            className="w-full bg-navy-900 hover:bg-navy-800 text-white font-semibold text-sm px-4 py-2.5 rounded-lg transition disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {submitting ? "Entrando…" : "Entrar"}
          </button>
        </form>

        <p className="text-center text-xs text-navy-400 mt-4">Acceso restringido al equipo B2Box</p>
      </div>
    </div>
  );
}
