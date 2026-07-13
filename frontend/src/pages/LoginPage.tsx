import { FormEvent, useState } from "react";
import { login } from "../api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

// Login alineado al design system B2BOX (mismo look que Pro/App). En éxito hace
// un full reload a "/" para que el backend valide la cookie y sirva el dashboard.
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
          <div className="w-14 h-14 rounded-2xl bg-primary flex items-center justify-center text-primary-foreground font-bold text-2xl shadow-sm tracking-tight">
            H
          </div>
          <h1 className="text-lg font-bold text-foreground mt-3">Hugo</h1>
          <p className="text-xs text-muted-foreground">Control de calidad · catálogo B2Box</p>
        </div>

        <Card className="p-6 shadow-sm animate-fade-in">
          <form onSubmit={onSubmit}>
            <h2 className="text-base font-semibold text-foreground mb-4">Ingresar</h2>

            <label className="block text-sm font-medium text-foreground mb-1" htmlFor="username">
              Email
            </label>
            <Input
              id="username"
              name="username"
              type="email"
              autoComplete="username email"
              required
              placeholder="tu@b2box.pro"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="mb-4"
            />

            <label className="block text-sm font-medium text-foreground mb-1" htmlFor="password">
              Contraseña
            </label>
            <Input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mb-4"
            />

            {error && (
              <div className="mb-4 p-2.5 rounded-md text-xs bg-destructive/10 border border-destructive/40 text-destructive">
                {error}
              </div>
            )}

            <Button type="submit" size="lg" disabled={submitting} className="w-full">
              {submitting ? "Entrando…" : "Entrar"}
            </Button>
          </form>
        </Card>

        <p className="text-center text-xs text-muted-foreground mt-4">
          Acceso restringido al equipo B2Box
        </p>
      </div>
    </div>
  );
}
