import { CircleCheck, CircleX, Clock, LoaderCircle, TriangleAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { Tone } from "@/lib/status";
import { cn } from "@/lib/utils";

const TONES: Record<Tone, { className: string; icon: React.ComponentType<{ className?: string }> }> = {
  neutral: { className: "bg-muted text-muted-foreground", icon: Clock },
  info: { className: "bg-blue-50 text-blue-700", icon: LoaderCircle },
  success: { className: "bg-emerald-50 text-emerald-700", icon: CircleCheck },
  danger: { className: "bg-red-50 text-red-700", icon: CircleX },
  warning: { className: "bg-amber-50 text-amber-800", icon: TriangleAlert },
};

/** A status with an icon and text, so it never depends on colour alone. */
export function StatusBadge({ label, tone }: { label: string; tone: Tone }) {
  const { className, icon: Icon } = TONES[tone];
  return (
    <Badge variant="secondary" className={cn("gap-1 font-medium", className)}>
      <Icon className={cn("size-3", tone === "info" && "animate-spin")} aria-hidden />
      {label}
    </Badge>
  );
}
