"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { ErrorAlert } from "@/components/error-alert";
import { FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { fieldErrors } from "@/lib/errors";
import { HOME_PATH } from "@/lib/redirect";
import { SESSION_KEY } from "@/lib/session";
import type { RegisterInput } from "@/lib/types";

// The backend's rules (backend/app/api/v1/schemas.py); it checks them again.
const PASSWORD_MIN = 12;
const PASSWORD_MAX = 128;
const WORKSPACE_MAX = 100;

export default function RegisterPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const register = useMutation({
    mutationFn: (input: RegisterInput) => api.auth.register(input),
    onSuccess: (session) => {
      queryClient.setQueryData(SESSION_KEY, session);
      router.replace(HOME_PATH);
    },
  });
  const errors = fieldErrors(register.error);

  function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    register.mutate({
      email: String(form.get("email")),
      password: String(form.get("password")),
      workspace_name: String(form.get("workspace_name")).trim(),
    });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h1>Create an account</h1>
        </CardTitle>
        <CardDescription>You get your own workspace for documents and conversations.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="grid gap-4">
          {register.isError && !Object.keys(errors).length ? (
            <ErrorAlert error={register.error} title="Couldn't create the account" />
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
            autoComplete="new-password"
            required
            minLength={PASSWORD_MIN}
            maxLength={PASSWORD_MAX}
            hint={`At least ${PASSWORD_MIN} characters.`}
            error={errors.password}
          />
          <FormField
            label="Workspace name"
            name="workspace_name"
            autoComplete="organization"
            required
            maxLength={WORKSPACE_MAX}
            pattern=".*\S.*"
            title="Enter a workspace name."
            error={errors.workspace_name}
          />
          <Button type="submit" disabled={register.isPending}>
            {register.isPending ? "Creating account…" : "Create account"}
          </Button>
          <p className="text-center text-sm text-muted-foreground">
            Already registered?{" "}
            <Link href="/login" className="font-medium text-foreground underline-offset-4 hover:underline">
              Sign in
            </Link>
          </p>
        </form>
      </CardContent>
    </Card>
  );
}
