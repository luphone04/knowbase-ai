import { NOT_MEASURED, formatLatency, formatScore } from "@/lib/evaluation";
import type { LatencyStats } from "@/lib/types";

/** A 0–1 metric, or "Not measured" (never shown as 0). */
export function Score({ value }: { value: number | null | undefined }) {
  const text = formatScore(value);
  if (text === null) return <span className="text-muted-foreground">{NOT_MEASURED}</span>;
  return <span className="font-medium tabular-nums">{text}</span>;
}

export function Latency({ stats }: { stats: LatencyStats | undefined }) {
  const text = formatLatency(stats);
  if (text === null) return <span className="text-muted-foreground">{NOT_MEASURED}</span>;
  return <span className="tabular-nums">{text}</span>;
}
