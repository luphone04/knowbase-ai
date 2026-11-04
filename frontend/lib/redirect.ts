export const HOME_PATH = "/dashboard";

/** A same-origin path to return to after sign-in; anything else goes home (no open redirect). */
export function safeNextPath(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return HOME_PATH;
  }
  return value;
}

export function loginPath(next: string): string {
  return next === HOME_PATH ? "/login" : `/login?next=${encodeURIComponent(next)}`;
}
