"use client";

/**
 * CategoriesTab — list / enable / disable / add / remove category configs.
 *
 * Reads from GET /admin/settings/ (effective.categories + overrides.categories)
 * Mutations:
 *   PATCH  /admin/settings/categories/{name}
 *   POST   /admin/settings/categories
 *   DELETE /admin/settings/categories/{name}  (soft-disable)
 *
 * Conventions: matches existing dashboard pages — uses adminApi.* helpers,
 * SWR for read with 10s refresh, debounced PATCH for numeric inputs, inline
 * error display (no thrown errors). Shows "(overridden)" badge next to
 * categories that have entries in overrides.categories.
 */

import useSWR from "swr";
import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { adminApi, getAdminToken } from "@/lib/admin";

type CategoryConfig = {
  enabled: boolean;
  top_n: number;
  min_win_rate: number;
  tags: string[];
};

type SettingsPayload = {
  mode: "paper" | "live";
  effective: {
    risk: Record<string, unknown>;
    categories: Record<string, CategoryConfig>;
    gates: Record<string, unknown>;
  };
  overrides: {
    risk?: Record<string, unknown>;
    categories?: Record<string, Partial<CategoryConfig>>;
    gates?: Record<string, unknown>;
  };
  baseline?: unknown;
};

// adminApi.get returns `unknown` — wrap it in a typed fetcher for SWR.
const swrAdminFetcher = async (path: string): Promise<SettingsPayload> => {
  return (await adminApi.get(path)) as SettingsPayload;
};

const DEBOUNCE_MS = 600;

export default function CategoriesTab() {
  // Re-render once the admin token appears so the SWR key flips on.
  const [hasToken, setHasToken] = useState<boolean>(() => !!getAdminToken());
  useEffect(() => {
    if (hasToken) return;
    const id = setInterval(() => {
      if (getAdminToken()) setHasToken(true);
    }, 1000);
    return () => clearInterval(id);
  }, [hasToken]);

  const { data, error, isLoading, mutate } = useSWR<SettingsPayload>(
    hasToken ? "/admin/settings/" : null,
    swrAdminFetcher,
    { refreshInterval: 10000 },
  );

  const [mutationError, setMutationError] = useState<string | null>(null);

  if (!hasToken) {
    return (
      <div className="card text-sm text-muted space-y-2">
        <div>Admin token not set in this tab.</div>
        <div>
          Paste it into the kill-switch widget on the{" "}
          <Link href="/" className="text-accent hover:underline">home page</Link>{" "}
          first.
        </div>
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
    return <div className="card text-sm text-muted">loading categories…</div>;
  }

  const effectiveCats = data.effective?.categories ?? {};
  const overrideCats = data.overrides?.categories ?? {};
  const rows = Object.entries(effectiveCats).sort(([a], [b]) => a.localeCompare(b));

  return (
    <div className="space-y-4">
      {mutationError && (
        <div className="card text-sm text-danger">
          mutation failed: {mutationError}{" "}
          <button
            className="ml-2 underline text-muted hover:text-text"
            onClick={() => setMutationError(null)}
          >dismiss</button>
        </div>
      )}

      <div className="card overflow-x-auto">
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="text-sm k">Categories</h2>
          <span className="text-xs text-muted">
            {rows.length} configured · {Object.keys(overrideCats).length} overridden
          </span>
        </div>
        <table className="text-sm w-full">
          <thead className="text-muted text-xs uppercase">
            <tr>
              <th className="text-left p-2">Name</th>
              <th className="text-left p-2">Enabled</th>
              <th className="text-left p-2">Tags</th>
              <th className="text-right p-2">top_n</th>
              <th className="text-right p-2">min_win_rate</th>
              <th className="text-left p-2"></th>
              <th className="text-left p-2"></th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([name, cfg]) => (
              <CategoryRow
                key={name}
                name={name}
                cfg={cfg}
                isOverridden={Object.prototype.hasOwnProperty.call(overrideCats, name)}
                onMutate={() => mutate()}
                onError={(msg) => setMutationError(msg)}
              />
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="p-6 text-center text-muted">
                  no categories configured
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <AddCategoryForm
        existingNames={new Set(Object.keys(effectiveCats))}
        onMutate={() => mutate()}
        onError={(msg) => setMutationError(msg)}
      />
    </div>
  );
}

/* ---------- row ---------- */

function CategoryRow({
  name, cfg, isOverridden, onMutate, onError,
}: {
  name: string;
  cfg: CategoryConfig;
  isOverridden: boolean;
  onMutate: () => void;
  onError: (msg: string) => void;
}) {
  // Local editable mirrors of the row. We keep them in sync with upstream
  // values across SWR refreshes UNLESS the user is actively editing — then
  // their in-progress text is preserved.
  const [topN, setTopN] = useState<string>(String(cfg.top_n));
  const [minWR, setMinWR] = useState<string>(String(cfg.min_win_rate));
  const [tags, setTags] = useState<string>((cfg.tags || []).join(", "));
  const [busy, setBusy] = useState(false);
  const [savingTags, setSavingTags] = useState(false);

  const touchedRef = useRef({ topN: false, minWR: false, tags: false });

  useEffect(() => {
    if (!touchedRef.current.topN) setTopN(String(cfg.top_n));
    if (!touchedRef.current.minWR) setMinWR(String(cfg.min_win_rate));
    if (!touchedRef.current.tags) setTags((cfg.tags || []).join(", "));
  }, [cfg.top_n, cfg.min_win_rate, cfg.tags]);

  // Debounced PATCH for the numeric fields.
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const schedulePatch = (patch: Partial<CategoryConfig>) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      setBusy(true);
      try {
        await adminApi.patch(
          `/admin/settings/categories/${encodeURIComponent(name)}`,
          patch,
        );
        for (const k of Object.keys(patch)) {
          (touchedRef.current as Record<string, boolean>)[
            k === "top_n" ? "topN" : k === "min_win_rate" ? "minWR" : k
          ] = false;
        }
        onMutate();
      } catch (e) {
        onError(String((e as Error).message ?? e));
      } finally {
        setBusy(false);
      }
    }, DEBOUNCE_MS);
  };

  const onToggle = async (next: boolean) => {
    setBusy(true);
    try {
      await adminApi.patch(
        `/admin/settings/categories/${encodeURIComponent(name)}`,
        { enabled: next },
      );
      onMutate();
    } catch (e) {
      onError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const onSaveTags = async () => {
    setSavingTags(true);
    try {
      const arr = tags
        .split(",")
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
      await adminApi.patch(
        `/admin/settings/categories/${encodeURIComponent(name)}`,
        { tags: arr },
      );
      touchedRef.current.tags = false;
      onMutate();
    } catch (e) {
      onError(String((e as Error).message ?? e));
    } finally {
      setSavingTags(false);
    }
  };

  const onDelete = async () => {
    if (!confirm(`Soft-disable category "${name}"?`)) return;
    setBusy(true);
    try {
      await adminApi.delete(
        `/admin/settings/categories/${encodeURIComponent(name)}`,
      );
      onMutate();
    } catch (e) {
      onError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <tr className="border-t border-white/5 align-middle">
      <td className="p-2">
        <span className="font-mono text-sm">{name}</span>
        {isOverridden && (
          <span className="ml-2 text-[10px] uppercase tracking-wider text-accent">
            (overridden)
          </span>
        )}
      </td>

      <td className="p-2">
        <input
          type="checkbox"
          checked={!!cfg.enabled}
          disabled={busy}
          onChange={(e) => onToggle(e.target.checked)}
          className="h-4 w-4 accent-accent cursor-pointer"
        />
      </td>

      <td className="p-2 min-w-[220px]">
        <input
          type="text"
          value={tags}
          onChange={(e) => { touchedRef.current.tags = true; setTags(e.target.value); }}
          className="w-full bg-black/40 border border-white/10 rounded px-2 py-1 text-xs font-mono"
          placeholder="tag1, tag2"
        />
      </td>

      <td className="p-2 text-right">
        <input
          type="number"
          value={topN}
          min={0}
          step={1}
          onChange={(e) => {
            touchedRef.current.topN = true;
            setTopN(e.target.value);
            const n = parseInt(e.target.value, 10);
            if (Number.isFinite(n) && n >= 0) schedulePatch({ top_n: n });
          }}
          className="w-20 bg-black/40 border border-white/10 rounded px-2 py-1 text-sm tabular-nums text-right"
        />
      </td>

      <td className="p-2 text-right">
        <input
          type="number"
          value={minWR}
          min={0}
          max={1}
          step={0.01}
          onChange={(e) => {
            touchedRef.current.minWR = true;
            setMinWR(e.target.value);
            const n = parseFloat(e.target.value);
            if (Number.isFinite(n) && n >= 0 && n <= 1) {
              schedulePatch({ min_win_rate: n });
            }
          }}
          className="w-20 bg-black/40 border border-white/10 rounded px-2 py-1 text-sm tabular-nums text-right"
        />
      </td>

      <td className="p-2">
        <button
          onClick={onSaveTags}
          disabled={savingTags}
          className="px-2 py-1 text-xs rounded border border-white/10 hover:border-accent hover:text-accent disabled:opacity-40"
          title="Save tags"
        >
          {savingTags ? "…" : "save tags"}
        </button>
      </td>

      <td className="p-2">
        <button
          onClick={onDelete}
          disabled={busy}
          className="px-2 py-1 text-xs rounded border border-white/10 text-danger hover:border-danger disabled:opacity-40"
          title="Soft-disable"
        >
          delete
        </button>
      </td>
    </tr>
  );
}

/* ---------- add form ---------- */

function AddCategoryForm({
  existingNames, onMutate, onError,
}: {
  existingNames: Set<string>;
  onMutate: () => void;
  onError: (msg: string) => void;
}) {
  const [name, setName] = useState("");
  const [tags, setTags] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [topN, setTopN] = useState<string>("30");
  const [minWR, setMinWR] = useState<string>("0.55");
  const [busy, setBusy] = useState(false);
  const [localErr, setLocalErr] = useState<string | null>(null);

  const parsedTopN = useMemo(() => parseInt(topN, 10), [topN]);
  const parsedMinWR = useMemo(() => parseFloat(minWR), [minWR]);

  const nameClean = name.trim();
  const isDuplicate = nameClean.length > 0 && existingNames.has(nameClean);
  const validTopN = Number.isFinite(parsedTopN) && parsedTopN >= 0;
  const validMinWR = Number.isFinite(parsedMinWR) && parsedMinWR >= 0 && parsedMinWR <= 1;
  const canSubmit = nameClean.length > 0 && !isDuplicate && validTopN && validMinWR && !busy;

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setLocalErr(null);
    setBusy(true);
    try {
      const tagArr = tags
        .split(",")
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
      await adminApi.post("/admin/settings/categories", {
        name: nameClean,
        tags: tagArr,
        enabled,
        top_n: parsedTopN,
        min_win_rate: parsedMinWR,
      });
      // Reset form on success.
      setName("");
      setTags("");
      setEnabled(true);
      setTopN("30");
      setMinWR("0.55");
      onMutate();
    } catch (err) {
      const msg = String((err as Error).message ?? err);
      setLocalErr(msg);
      onError(msg);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card space-y-3" onSubmit={onSubmit}>
      <h2 className="text-sm k">Add category</h2>

      <div className="grid grid-cols-1 md:grid-cols-5 gap-3">
        <label className="text-xs space-y-1">
          <div className="k">name</div>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. crypto"
            className="w-full bg-black/40 border border-white/10 rounded px-2 py-1 text-sm font-mono"
          />
          {isDuplicate && (
            <div className="text-danger text-[10px]">already exists</div>
          )}
        </label>

        <label className="text-xs space-y-1 md:col-span-2">
          <div className="k">tags (comma-separated)</div>
          <input
            type="text"
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            placeholder="bitcoin, eth, defi"
            className="w-full bg-black/40 border border-white/10 rounded px-2 py-1 text-sm font-mono"
          />
        </label>

        <label className="text-xs space-y-1">
          <div className="k">top_n</div>
          <input
            type="number"
            value={topN}
            min={0}
            step={1}
            onChange={(e) => setTopN(e.target.value)}
            className="w-full bg-black/40 border border-white/10 rounded px-2 py-1 text-sm tabular-nums"
          />
          {!validTopN && (
            <div className="text-danger text-[10px]">must be ≥ 0</div>
          )}
        </label>

        <label className="text-xs space-y-1">
          <div className="k">min_win_rate (0..1)</div>
          <input
            type="number"
            value={minWR}
            min={0}
            max={1}
            step={0.01}
            onChange={(e) => setMinWR(e.target.value)}
            className="w-full bg-black/40 border border-white/10 rounded px-2 py-1 text-sm tabular-nums"
          />
          {!validMinWR && (
            <div className="text-danger text-[10px]">0 ≤ x ≤ 1</div>
          )}
        </label>
      </div>

      <div className="flex items-center gap-4">
        <label className="text-xs flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="h-4 w-4 accent-accent"
          />
          <span className="k">enabled</span>
        </label>

        <button
          type="submit"
          disabled={!canSubmit}
          className="ml-auto px-3 py-1 rounded bg-accent text-black text-sm font-semibold disabled:opacity-40"
        >
          {busy ? "adding…" : "add category"}
        </button>
      </div>

      {localErr && (
        <div className="text-danger text-xs">{localErr}</div>
      )}
    </form>
  );
}
