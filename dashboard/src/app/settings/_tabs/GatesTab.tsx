"use client";

/**
 * GatesTab — display + edit the 8 trading gates.
 *
 * Data shape (from GET /admin/settings/):
 *   effective.gates = { gates: [{name, type: "hard"|"soft", enabled, params: {...}}, ...] }
 *
 * Save model: backend PATCH /admin/settings/gates accepts a partial-gates-patch.
 * To update one gate we read the full effective gates array, replace the
 * matching entry, and PATCH `{ gates: <new full array> }`. Keeps the per-card
 * save independent without needing a granular per-gate endpoint.
 */

import { useState } from "react";
import useSWR from "swr";
import { API } from "@/lib/api";
import { adminApi, getAdminToken } from "@/lib/admin";

// ---- types -------------------------------------------------------------

type GateType = "hard" | "soft" | string;

type ParamValue = number | boolean | string | string[] | Record<string, unknown> | unknown[] | null;

interface Gate {
  name: string;
  type: GateType;
  enabled: boolean;
  params: Record<string, ParamValue>;
}

interface SettingsResponse {
  mode: string;
  effective: {
    risk: Record<string, unknown>;
    categories: Record<string, unknown>;
    gates: { gates: Gate[] } | Gate[]; // tolerate either shape
  };
  overrides: Record<string, unknown>;
  baseline: Record<string, unknown>;
}

// ---- helpers -----------------------------------------------------------

/** Heuristic: classify a param value into a render mode. */
type ParamKind = "number" | "boolean" | "string" | "string-array" | "json";

function classify(v: ParamValue): ParamKind {
  if (typeof v === "number") return "number";
  if (typeof v === "boolean") return "boolean";
  if (typeof v === "string") return "string";
  if (Array.isArray(v) && v.every((x) => typeof x === "string")) return "string-array";
  return "json";
}

/** Read the gates array out of effective.gates (handles wrapped + bare shapes). */
function extractGates(effGates: SettingsResponse["effective"]["gates"]): Gate[] {
  if (Array.isArray(effGates)) return effGates;
  if (effGates && Array.isArray(effGates.gates)) return effGates.gates;
  return [];
}

// SWR fetcher that pulls the full settings doc via the admin endpoint
// (it requires X-Admin-Token, so we route through adminApi.get rather
// than the unauthenticated fetcher in lib/api).
const settingsFetcher = (path: string) => adminApi.get(path) as Promise<SettingsResponse>;

// ---- component ---------------------------------------------------------

export default function GatesTab() {
  const hasToken = typeof window !== "undefined" && !!getAdminToken();

  const { data, error, isLoading, mutate } = useSWR<SettingsResponse>(
    hasToken ? "/admin/settings/" : null,
    settingsFetcher,
    { refreshInterval: 15000 },
  );

  if (!hasToken) {
    return (
      <div className="card">
        <p className="text-sm text-muted">
          Admin token not set. Paste it in the kill-switch widget on the{" "}
          <a href="/" className="text-accent underline">home page</a> first.
        </p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="card">
        <p className="text-sm text-danger">failed to load settings: {String(error.message || error)}</p>
      </div>
    );
  }

  if (isLoading || !data) {
    return <div className="card"><p className="text-sm text-muted">loading gates…</p></div>;
  }

  const gates = extractGates(data.effective.gates);

  if (gates.length === 0) {
    return <div className="card"><p className="text-sm text-muted">no gates configured</p></div>;
  }

  return (
    <div className="space-y-4">
      <div className="text-sm text-muted">
        {gates.length} gates · API: <span className="font-mono">{API}</span>
      </div>
      {gates.map((g) => (
        <GateCard
          key={g.name}
          gate={g}
          allGates={gates}
          onSaved={() => mutate()}
        />
      ))}
    </div>
  );
}

// ---- gate card ---------------------------------------------------------

function GateCard({
  gate,
  allGates,
  onSaved,
}: {
  gate: Gate;
  allGates: Gate[];
  onSaved: () => void;
}) {
  // Local draft state — initialised from `gate` but edited in isolation
  // so the SWR poll doesn't clobber unsaved edits.
  const [enabled, setEnabled] = useState<boolean>(gate.enabled);
  const [params, setParams] = useState<Record<string, ParamValue>>(() => ({ ...gate.params }));
  // Per-param raw text (for number/JSON/array fields where we can't
  // round-trip through the typed state mid-edit, e.g. "1." or "[1,").
  const [raw, setRaw] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const dirty =
    enabled !== gate.enabled ||
    JSON.stringify(params) !== JSON.stringify(gate.params) ||
    Object.keys(raw).length > 0; // any in-flight raw edit counts as dirty

  function setParam(key: string, value: ParamValue) {
    setParams((p) => ({ ...p, [key]: value }));
  }

  async function save() {
    setSaving(true);
    setErr(null);
    try {
      // Resolve any pending raw edits into typed values. If anything
      // fails to parse, abort with an inline error pointing at the field.
      const resolvedParams: Record<string, ParamValue> = { ...params };
      for (const [k, text] of Object.entries(raw)) {
        const orig = gate.params[k];
        const kind = classify(orig);
        try {
          resolvedParams[k] = parseRaw(text, kind);
        } catch (e) {
          throw new Error(`param "${k}": ${(e as Error).message}`);
        }
      }

      // Build the full gates array with this one replaced.
      const nextGates: Gate[] = allGates.map((g) =>
        g.name === gate.name
          ? { ...g, enabled, params: resolvedParams }
          : g,
      );

      await adminApi.patch("/admin/settings/gates", { gates: nextGates });
      setRaw({});
      setSavedAt(Date.now());
      onSaved();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  function reset() {
    setEnabled(gate.enabled);
    setParams({ ...gate.params });
    setRaw({});
    setErr(null);
  }

  return (
    <div className="card space-y-3">
      {/* header */}
      <div className="flex items-center gap-3">
        <h3 className="text-base font-semibold">{gate.name}</h3>
        <span
          className={
            "text-xs px-2 py-0.5 rounded border " +
            (gate.type === "hard"
              ? "border-danger/40 text-danger"
              : "border-white/10 text-muted")
          }
        >
          {gate.type}
        </span>
        <label className="ml-auto flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          <span className={enabled ? "text-accent" : "text-muted"}>
            {enabled ? "enabled" : "disabled"}
          </span>
        </label>
      </div>

      {/* params */}
      {Object.keys(gate.params).length === 0 ? (
        <p className="text-xs text-muted">no params</p>
      ) : (
        <div className="space-y-2">
          {Object.entries(gate.params).map(([key, origVal]) => {
            const kind = classify(origVal);
            const current = params[key];
            const rawText = raw[key];
            return (
              <ParamRow
                key={key}
                paramKey={key}
                kind={kind}
                value={current}
                rawText={rawText}
                onChangeValue={(v) => {
                  setParam(key, v);
                  setRaw((r) => {
                    const { [key]: _drop, ...rest } = r;
                    return rest;
                  });
                }}
                onChangeRaw={(t) => setRaw((r) => ({ ...r, [key]: t }))}
              />
            );
          })}
        </div>
      )}

      {/* footer */}
      <div className="flex items-center gap-2 pt-2 border-t border-white/5">
        <button
          className="px-3 py-1 rounded bg-accent text-bg font-medium disabled:opacity-40"
          onClick={save}
          disabled={!dirty || saving}
        >
          {saving ? "saving…" : "save"}
        </button>
        <button
          className="px-3 py-1 rounded border border-white/10 text-muted hover:text-text disabled:opacity-40"
          onClick={reset}
          disabled={!dirty || saving}
        >
          reset
        </button>
        {err && <span className="text-xs text-danger">{err}</span>}
        {!err && savedAt && !dirty && (
          <span className="text-xs text-muted">
            saved {Math.max(0, Math.round((Date.now() - savedAt) / 1000))}s ago
          </span>
        )}
      </div>
    </div>
  );
}

// ---- param row ---------------------------------------------------------

function ParamRow({
  paramKey,
  kind,
  value,
  rawText,
  onChangeValue,
  onChangeRaw,
}: {
  paramKey: string;
  kind: ParamKind;
  value: ParamValue;
  rawText: string | undefined;
  onChangeValue: (v: ParamValue) => void;
  onChangeRaw: (t: string) => void;
}) {
  const label = (
    <label className="k block mb-1" htmlFor={`p-${paramKey}`}>
      {paramKey} <span className="text-muted/60 normal-case">({kind})</span>
    </label>
  );

  if (kind === "boolean") {
    return (
      <div>
        <label className="flex items-center gap-2 text-sm">
          <input
            id={`p-${paramKey}`}
            type="checkbox"
            checked={!!value}
            onChange={(e) => onChangeValue(e.target.checked)}
          />
          <span className="k">{paramKey}</span>
          <span className="text-muted/60 text-xs">(boolean)</span>
        </label>
      </div>
    );
  }

  if (kind === "number") {
    const display = rawText !== undefined ? rawText : value == null ? "" : String(value);
    return (
      <div>
        {label}
        <input
          id={`p-${paramKey}`}
          type="number"
          step="any"
          className="w-full bg-bg2 border border-white/10 rounded px-2 py-1 tabular-nums text-sm"
          value={display}
          onChange={(e) => {
            const t = e.target.value;
            onChangeRaw(t);
            // also push a parsed value when valid so dirty-check is cheap
            const n = Number(t);
            if (t !== "" && Number.isFinite(n)) onChangeValue(n);
          }}
        />
      </div>
    );
  }

  if (kind === "string") {
    return (
      <div>
        {label}
        <input
          id={`p-${paramKey}`}
          type="text"
          className="w-full bg-bg2 border border-white/10 rounded px-2 py-1 text-sm font-mono"
          value={value == null ? "" : String(value)}
          onChange={(e) => onChangeValue(e.target.value)}
        />
      </div>
    );
  }

  if (kind === "string-array") {
    const arr = Array.isArray(value) ? (value as string[]) : [];
    const display = rawText !== undefined ? rawText : arr.join(", ");
    return (
      <div>
        {label}
        <textarea
          id={`p-${paramKey}`}
          rows={2}
          className="w-full bg-bg2 border border-white/10 rounded px-2 py-1 text-sm font-mono"
          value={display}
          onChange={(e) => {
            const t = e.target.value;
            onChangeRaw(t);
            // parsed view (split + trim, drop empties)
            const parsed = t
              .split(",")
              .map((s) => s.trim())
              .filter((s) => s.length > 0);
            onChangeValue(parsed);
          }}
          placeholder="comma, separated, values"
        />
      </div>
    );
  }

  // json fallback (objects, mixed arrays, null)
  const display = rawText !== undefined ? rawText : JSON.stringify(value, null, 2);
  return (
    <div>
      {label}
      <textarea
        id={`p-${paramKey}`}
        rows={4}
        className="w-full bg-bg2 border border-white/10 rounded px-2 py-1 text-xs font-mono"
        value={display}
        onChange={(e) => {
          const t = e.target.value;
          onChangeRaw(t);
          // best-effort parse — leave raw if it's mid-edit invalid JSON
          try {
            onChangeValue(JSON.parse(t) as ParamValue);
          } catch {
            /* keep raw; save() will re-validate */
          }
        }}
      />
    </div>
  );
}

// ---- raw -> typed parser used at save time -----------------------------

function parseRaw(text: string, kind: ParamKind): ParamValue {
  if (kind === "number") {
    const n = Number(text);
    if (!Number.isFinite(n)) throw new Error(`"${text}" is not a number`);
    return n;
  }
  if (kind === "string-array") {
    return text
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
  }
  if (kind === "string") return text;
  if (kind === "boolean") return text === "true";
  // json
  try {
    return JSON.parse(text) as ParamValue;
  } catch (e) {
    throw new Error(`invalid JSON: ${(e as Error).message}`);
  }
}
