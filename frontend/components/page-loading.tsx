import { LoaderCircle } from "lucide-react";

export function PageLoading({ label = "Loading…" }: { label?: string }) {
  return (
    <div role="status" className="flex flex-1 items-center justify-center gap-2 p-8 text-sm text-muted-foreground">
      <LoaderCircle className="size-4 animate-spin" aria-hidden />
      {label}
    </div>
  );
}
