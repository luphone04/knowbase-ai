import { CircleAlert } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { describeError } from "@/lib/errors";

export function ErrorAlert({
  error,
  title = "Something went wrong",
  children,
}: {
  error: unknown;
  title?: string;
  children?: React.ReactNode;
}) {
  return (
    <Alert variant="destructive" role="alert">
      <CircleAlert />
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription>
        <p>{describeError(error)}</p>
        {children}
      </AlertDescription>
    </Alert>
  );
}
