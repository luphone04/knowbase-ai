import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type FormFieldProps = React.ComponentProps<typeof Input> & {
  label: string;
  name: string;
  hint?: string;
  error?: string;
};

/** A labelled input with an optional hint and a validation message tied to it for screen readers. */
export function FormField({ label, name, hint, error, ...input }: FormFieldProps) {
  const describedBy = error ? `${name}-error` : hint ? `${name}-hint` : undefined;
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={name}>{label}</Label>
      <Input
        id={name}
        name={name}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        {...input}
      />
      {error ? (
        <p id={`${name}-error`} className="text-sm text-destructive">
          {error}
        </p>
      ) : hint ? (
        <p id={`${name}-hint`} className="text-sm text-muted-foreground">
          {hint}
        </p>
      ) : null}
    </div>
  );
}
