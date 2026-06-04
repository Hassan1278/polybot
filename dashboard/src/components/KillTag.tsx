"use client";
import useSWR from "swr";
import { fetcher } from "@/lib/api";

type Health = { mode: string; kill_switch: string | null };

export default function KillTag() {
  const { data } = useSWR<Health>("/health", fetcher, { refreshInterval: 5000 });
  if (!data) return <span className="text-xs text-muted">…</span>;
  if (data.kill_switch) {
    return <span className="text-xs text-danger uppercase">KILLED · {data.kill_switch}</span>;
  }
  return (
    <span className="text-xs">
      <span className="text-muted">mode: </span>
      <span className={data.mode === "live" ? "text-danger" : "text-accent"}>{data.mode}</span>
    </span>
  );
}
