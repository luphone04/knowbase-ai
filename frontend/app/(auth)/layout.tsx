import { BookOpenText } from "lucide-react";

export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-6 bg-muted/40 px-4 py-12">
      <div className="flex items-center gap-2 text-lg font-semibold tracking-tight">
        <BookOpenText className="size-5" aria-hidden />
        Knowbase AI
      </div>
      <div className="w-full max-w-sm">{children}</div>
    </main>
  );
}
