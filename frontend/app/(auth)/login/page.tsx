"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { ErrorAlert } from "@/components/error-alert";
import { FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { fieldErrors } from "@/lib/errors";
import { safeNextPath } from "@/lib/redirect";
import { SESSION_KEY } from "@/lib/session";
import type { LoginInput } from "@/lib/types";

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const queryClient = useQueryClient();
  // ?next=... is set when a signed-out visit or an ended session sent the user here.
  const next = useSearchParams().get("next");
  const login = useMutation({
    mutationFn: (input: LoginInput) => api.auth.login(input),
    onSuccess: (session) => {
      queryClient.setQueryData(SESSION_KEY, session);
      router.replace(safeNextPath(next));
    },
  });
  const errors = fieldErrors(login.error);

  function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    login.mutate({ email: String(form.get("email")), password: String(form.get("password")) });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h1>Sign in</h1>
        </CardTitle>
        <CardDescription>
          {next ? "Sign in to continue." : "Use the email and password you registered with."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="grid gap-4">
          {login.isError && !Object.keys(errors).length ? (
            <ErrorAlert error={login.error} title="Couldn't sign in" />
          ) : null}
          <FormField
            label="Email"
            name="email"
            type="email"
            autoComplete="email"
            required
            error={errors.email}
          />
          <FormField
            label="Password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            maxLength={128}
            error={errors.password}
          />
          <Button type="submit" disabled={login.isPending}>
            {login.isPending ? "Signing in…" : "Sign in"}
          </Button>
          <p className="text-center text-sm text-muted-foreground">
            No account?{" "}
            <Link href="/register" className="font-medium text-foreground underline-offset-4 hover:underline">
              Create one
            </Link>
          </p>
        </form>
      </CardContent>
    </Card>
  );
}
