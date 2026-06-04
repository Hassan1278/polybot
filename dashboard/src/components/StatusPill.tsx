"use client";

type Props = {
  /** Numeric value used to choose color via thresholds. */
  value: number | null | undefined;
  /** If value >= green, pill is green. */
  green?: number;
  /** If value >= yellow (but < green), pill is yellow. Below yellow, red. */
  yellow?: number;
  /** Reverse thresholds: lower is better (e.g. lag seconds). */
  invert?: boolean;
  /** Optional label override; defaults to formatted value. */
  label?: string;
  /** Optional className extension. */
  className?: string;
};

function pickTone(
  value: number | null | undefined,
  green: number,
  yellow: number,
  invert: boolean,
): "green" | "yellow" | "red" | "muted" {
  if (value == null || (typeof value === "number" && Number.isNaN(value))) {
    return "muted";
  }
  if (invert) {
    if (value <= green) return "green";
    if (value <= yellow) return "yellow";
    return "red";
  }
  if (value >= green) return "green";
  if (value >= yellow) return "yellow";
  return "red";
}

const TONES: Record<string, string> = {
  green: "bg-accent/15 text-accent border-accent/30",
  yellow: "bg-yellow-500/15 text-yellow-400 border-yellow-500/30",
  red: "bg-danger/15 text-danger border-danger/30",
  muted: "bg-white/5 text-muted border-white/10",
};

export default function StatusPill({
  value,
  green = 1,
  yellow = 0,
  invert = false,
  label,
  className = "",
}: Props) {
  const tone = pickTone(value, green, yellow, invert);
  const valid = value != null && !Number.isNaN(value);
  const text =
    label ??
    (!valid
      ? "—"
      : Number.isInteger(value)
      ? String(value)
      : (value as number).toFixed(2));
  return (
    <span
      className={`inline-flex items-center text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded-full border ${TONES[tone]} ${className}`}
    >
      {text}
    </span>
  );
}
