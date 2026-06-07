"use client";
/**
 * RiskTab — edit risk-config overrides for the current mode (paper|live).
 *
 * Backend contract (services/api/routes/admin_settings.py):
 *   GET    /admin/settings/         → { mode, effective, overrides, baseline }
 *   PATCH  /admin/settings/risk     → partial section-grouped object → new effective
 *   DELETE /admin/settings/risk     → clears overrides for current mode
 *
 * Effective = baseline ⊕ overrides. We show effective as the form's initial
 * value and the baseline value next to each field if it differs (so the user
 * sees the override visually). PATCH body is built as a DIFF against effective
 * — only fields the user actually edited are sent.
 */

import useSWR from "swr";
import { useState, useEffect } from "react";
import Link from "next/link";
import { adminApi, getAdminToken } from "@/lib/admin";

// ───────────────────────── types ─────────────────────────

type Section = "position" | "drawdown" | "execution" | "sizing";

type RiskConfig = {
  position?: {
    max_position_usdc?: number;
    max_open_positions?: number;
    max_per_market_usdc?: number;
    max_per_category_usdc?: number;
  };
  drawdown?: {
    max_daily_loss_usdc?: number;
    max_weekly_loss_usdc?: number;
  };
  execution?: {
    cooldown_seconds_per_market?: number;
    max_orders_per_minute?: number;
    reject_if_spread_pct_above?: number;
  };
  sizing?: {
    base_usdc?: number;
    max_usdc?: number;
    anchor?: number;
    steepness?: number;
  };
};

type SettingsResponse = {
  mode: "paper" | "live";
  effective: { risk: RiskConfig; categories?: unknown; gates?: unknown };
  overrides: { risk?: RiskConfig };
  baseline:  { risk: RiskConfig };
};

// ─────────────────────── field schema ──────────────────────

type FieldKind = "int" | "float";
type FieldDef = {
  section: Section;
  key: string;
  label: string;
  kind: FieldKind;
  min?: number;
  max?: number;
  step?: number;
};

const FIELDS: FieldDef[] = [
  // position
  { section: "position", key: "max_position_usdc",     label: "Max position (USDC)",     kind: "float", min: 0.01, max: 1000,  step: 0.01 },
  { section: "position", key: "max_open_positions",    label: "Max open positions",      kind: "int",   min: 1,    max: 500,   step: 1 },
  { section: "position", key: "max_per_market_usdc",   label: "Max per market (USDC)",   kind: "float", min: 0.01, max: 1000,  step: 0.01 },
  { section: "position", key: "max_per_category_usdc", label: "Max per category (USDC)", kind: "float", min: 1,    max: 10000, step: 1 },
  // drawdown
  { section: "drawdown", key: "max_daily_loss_usdc",   label: "Max daily loss (USDC)",   kind: "float", min: 0,    max: 10000, step: 1 },
  { section: "drawdown", key: "max_weekly_loss_usdc",  label: "Max weekly loss (USDC)",  kind: "float", min: 0,    max: 10000, step: 1 },
  // execution
  { section: "execution", key: "cooldown_seconds_per_market", label: "Cooldown / market (s)",    kind: "int",   min: 0,             step: 1 },
  { section: "execution", key: "max_orders_per_minute",       label: "Max orders / minute",      kind: "int",   min: 1,   max: 10000, step: 1 },
  { section: "execution", key: "reject_if_spread_pct_above",  label: "Reject if spread % above", kind: "float", min: 0.1, max: 50,    step: 0.1 },
  // sizing
  { section: "sizing", key: "base_usdc", label: "Base USDC", kind: "float", step: 0.01 },
  { section: "sizing", key: "max_usdc",  label: "Max USDC",  kind: "float", step: 0.01 },
  { section: "sizing", key: "anchor",    label: "Anchor",    kind: "float", step: 0.01 },
  { section: "sizing", key: "steepness", label: "Steepness", kind: "float", step: 0.01 },
];

const SECTION_LABEL: Record<Section, string> = {
  position:  "Position limits",
  drawdown:  "Drawdown limits",
  execution: "Execution throttling",
  sizing:    "Sizing curve",
};

// ─────────────────────── helpers ────────────────────────

function getNested(obj: RiskConfig | undefined, section: Section, key: string): number | undefined {
  if (!obj) return undefined;
  const sec = (obj as Record<string, Record<string, unknown> | undefined>)[section];
  const v = sec?.[key];
  return typeof v === "number" ? v : undefined;
}

function fmtBaseline(n: number | undefined, kind: FieldKind): string {
  if (n === undefined) return "—";
  return kind === "int" ? String(Math.round(n)) : String(n);
}

/** Build a section-grouped diff: only fields whose numeric value differs from
 *  effective are included. Returns {} if nothing changed. */
function buildDiff(
  form: Record<string, string>,
  effective: RiskConfig,
): RiskConfig {
  const out: Record<string, Record<string, number>> = {};
  for (const f of FIELDS) {
    const raw = form[`${f.section}.${f.key}`] ?? "";
    if (raw === "") continue;
    const parsed = f.kind === "int" ? parseInt(raw, 10) : parseFloat(raw);
    if (Number.isNaN(parsed)) continue;
    const eff = getNested(effective, f.section, f.key);
    if (eff !== undefined && parsed === eff) continue;
    if (!out[f.section]) out[f.section] = {};
    out[f.section][f.key] = parsed;
  }
  return out as RiskConfig;
}

function diffCount(d: RiskConfig): number {
  let n = 0;
  for (const sec of Object.values(d)) {
    if (sec) n += Object.keys(sec).length;
  }
  return n;
}

// ─────────────────────── component ───────────────────────

export default function RiskTab() {
  const hasToken = typeof window !== "undefined" && !!getAdminToken();

  const { data, error, mutate, isLoading } = useSWR<SettingsResponse>(
    hasToken ? "/admin/settings/" : null,
    // The settings route requires X-Admin-Token for ALL methods, so we route
    // even reads through adminApi. With SWR's null key, fetching is disabled
    // when no token is present (we render the gated state below instead).
    async () => (await adminApi.get("/admin/settings/")) as SettingsResponse,
    { refreshInterval: 5000 },
  );

  const [form, setForm]       = useState<Record<string, string>>({});
  const [saving, setSaving]   = useState(false);
  const [resetting, setReset] = useState(false);
  const [errMsg, setErrMsg]   = useState<string | null>(null);
  const [toast, setToast]     = useState<string | null>(null);

  // Initialise the form from `effective.risk` once it loads (and re-init when
  // mode changes, since each mode has its own override layer).
  const effRiskKey = data ? JSON.stringify(data.effective.risk) : "";
  useEffect(() => {
    if (!data) return;
    const next: Record<string, string> = {};
    for (const f of FIELDS) {
      const v = getNested(data.effective.risk, f.section, f.key);
      next[`${f.section}.${f.key}`] = v === undefined ? "" : String(v);
    }
    setForm(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data?.mode, effRiskKey]);

  // Auto-clear toast after 3s.
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3000);
    return () => clearTimeout(t);
  }, [toast]);

  if (!hasToken) {
    return (
      <div className="card text-sm space-y-2">
        <div className="text-muted">Admin token required to view or edit risk config.</div>
        <Link href="/" className="text-accent hover:underline">
          ← back to home (kill-switch widget has the token paste field)
        </Link>
      </div>
    );
  }

  if (error) {
    return (
      <div className="card text-sm text-danger">
        failed to load settings: {String((error as Error).message ?? error)}
      </div>
    );
  }

  if (isLoading || !data) {
    return <div className="card text-sm text-muted">loading risk config…</div>;
  }

  const diff = buildDiff(form, data.effective.risk);
  const nChanges = diffCount(diff);
  const overrideCount = data.overrides.risk
    ? Object.values(data.overrides.risk).reduce(
        (n, sec) => n + (sec ? Object.keys(sec).length : 0),
        0,
      )
    : 0;

  async function onSave() {
    if (nChanges === 0) {
      setToast("nothing to save — no changes");
      return;
    }
    setErrMsg(null);
    setSaving(true);
    try {
      await adminApi.patch("/admin/settings/risk", diff);
      await mutate();
      setToast(`saved · ${nChanges} field${nChanges === 1 ? "" : "s"}`);
    } catch (e) {
      setErrMsg(String((e as Error).message ?? e));
    } finally {
      setSaving(false);
    }
  }

  async function onReset() {
    if (typeof window !== "undefined") {
      if (!window.confirm("Reset all risk overrides for this mode to defaults?")) return;
    }
    setErrMsg(null);
    setReset(true);
    try {
      await adminApi.delete("/admin/settings/risk");
      await mutate();
      setToast("overrides cleared");
    } catch (e) {
      setErrMsg(String((e as Error).message ?? e));
    } finally {
      setReset(false);
    }
  }

  const sections: Section[] = ["position", "drawdown", "execution", "sizing"];

  return (
    <div className="space-y-4">
      <header className="flex items-baseline justify-between">
        <div className="flex items-baseline gap-3">
          <h2 className="text-lg font-bold">Risk config</h2>
          <span className="k">
            {overrideCount} override{overrideCount === 1 ? "" : "s"} active · auto-refresh 5s
          </span>
        </div>
        <span
          className={
            "px-2 py-0.5 rounded text-xs font-mono uppercase " +
            (data.mode === "live"
              ? "bg-danger/20 text-danger border border-danger/40"
              : "bg-accent/20 text-accent border border-accent/40")
          }
          title="overrides apply to this mode only"
        >
          mode: {data.mode}
        </span>
      </header>

      <p className="text-muted text-xs max-w-3xl">
        Edit any field below and click <span className="text-text">Save</span> to
        persist as an override for <span className="text-text">{data.mode}</span> mode.
        The grey <span className="text-text">default: …</span> hint shows the
        baseline (config file) value when your override differs. Only changed
        fields are sent.
      </p>

      <form
        onSubmit={(e) => { e.preventDefault(); void onSave(); }}
        className="space-y-4"
      >
        {sections.map((sec) => {
          const secFields = FIELDS.filter((f) => f.section === sec);
          return (
            <section key={sec} className="card space-y-3">
              <h3 className="k">{SECTION_LABEL[sec]}</h3>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {secFields.map((f) => {
                  const formKey     = `${f.section}.${f.key}`;
                  const cur         = form[formKey] ?? "";
                  const eff         = getNested(data.effective.risk, f.section, f.key);
                  const base        = getNested(data.baseline.risk,  f.section, f.key);
                  const parsed      = cur === "" ? NaN : (f.kind === "int" ? parseInt(cur, 10) : parseFloat(cur));
                  const changed     = !Number.isNaN(parsed) && eff !== undefined && parsed !== eff;
                  const showDefault = base !== undefined && (eff === undefined || base !== eff);
                  return (
                    <label key={formKey} className="block text-sm">
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="text-muted text-xs">{f.label}</span>
                        {showDefault && (
                          <span className="text-[10px] text-muted">
                            default: <span className="tabular-nums">{fmtBaseline(base, f.kind)}</span>
                          </span>
                        )}
                      </div>
                      <input
                        type="number"
                        inputMode={f.kind === "int" ? "numeric" : "decimal"}
                        step={f.step}
                        min={f.min}
                        max={f.max}
                        value={cur}
                        onChange={(e) =>
                          setForm((s) => ({ ...s, [formKey]: e.target.value }))
                        }
                        className={
                          "mt-1 w-full bg-bg border rounded px-2 py-1 font-mono tabular-nums " +
                          (changed ? "border-accent/60" : "border-white/10")
                        }
                      />
                    </label>
                  );
                })}
              </div>
            </section>
          );
        })}

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={saving || nChanges === 0}
            className="px-4 py-1.5 rounded bg-accent text-bg font-semibold disabled:opacity-40"
          >
            {saving ? "saving…" : nChanges === 0 ? "save" : `save (${nChanges})`}
          </button>
          <button
            type="button"
            onClick={() => void onReset()}
            disabled={resetting || overrideCount === 0}
            className="px-4 py-1.5 rounded border border-danger/50 text-danger hover:bg-danger/10 disabled:opacity-40"
            title={overrideCount === 0 ? "no overrides to reset" : "DELETE /admin/settings/risk"}
          >
            {resetting ? "resetting…" : "reset to defaults"}
          </button>
          {toast && <span className="text-xs text-accent">{toast}</span>}
        </div>

        {errMsg && (
          <div className="text-sm text-danger whitespace-pre-wrap">
            error: {errMsg}
          </div>
        )}
      </form>
    </div>
  );
}
