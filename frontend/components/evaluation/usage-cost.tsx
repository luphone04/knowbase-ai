import { CollapsibleCard } from "@/components/collapsible-card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { UNAVAILABLE, formatTokens, ratesLabel, usageCost, usageIssueText } from "@/lib/evaluation";
import type { EvaluationRun, UsageGroup } from "@/lib/types";

const NOT_REPORTED = "Not reported";
const KIND_LABEL = { embedding: "Query embeddings", generation: "Answer generation" } as const;

/** Gemini tokens the run used and what they would cost at paid-tier prices (an estimate). */
export function UsageCost({ run, defaultOpen = true }: { run: EvaluationRun; defaultOpen?: boolean }) {
  const usage = run.summary.usage;
  return (
    <CollapsibleCard
      title="API Usage & Estimated Cost"
      defaultOpen={defaultOpen}
      data-testid="usage-cost"
      summary={usage ? usageSummaryLine(usage.total) : "Not measured for this run."}
      description={
        <>
          <strong className="font-semibold text-foreground">
            Estimated cost at standard paid-tier API pricing.
          </strong>{" "}
          This is not what Google billed: on the free tier these calls are free of charge, and token
          counts don&apos;t show an invoice. It is what the tokens Gemini reported would cost at the
          published paid-tier rates.
        </>
      }
      contentClassName="grid gap-5 text-sm"
    >
      {usage ? (
        <>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3" aria-label="Usage totals">
            <Total label="Estimated total API cost" value={<Cost group={usage.total} strong />} />
            <Total label="Total input tokens" value={<Tokens value={usage.total.input_tokens} />} />
            <Total label="Total output tokens" value={<Tokens value={usage.total.output_tokens} />} />
            <Total label="Total thinking tokens" value={<Tokens value={usage.total.thinking_tokens} />} />
            <Total label="Total reported tokens" value={<Tokens value={usage.total.total_tokens} />} />
            <Total label="Total Gemini requests" value={<span className="tabular-nums">{usage.total.requests}</span>} />
          </dl>

          <section className="grid gap-2">
            <h3 className="font-semibold">Cost breakdown</h3>
            <div className="overflow-x-auto">
              <Table aria-label="Cost breakdown">
                <TableHeader>
                  <TableRow>
                    <TableHead>Operation</TableHead>
                    <TableHead className="text-right">Requests</TableHead>
                    <TableHead className="text-right">Tokens</TableHead>
                    <TableHead className="text-right">Estimated cost</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  <BreakdownRow id="embedding" label={KIND_LABEL.embedding} group={usage.embedding} tokens={usage.embedding.input_tokens} />
                  <BreakdownRow id="generation" label={KIND_LABEL.generation} group={usage.generation} tokens={usage.generation.total_tokens} />
                  <BreakdownRow id="total" label="Total" group={usage.total} tokens={usage.total.total_tokens} strong />
                </TableBody>
              </Table>
            </div>
            <p className="text-xs text-muted-foreground">
              Tokens: input tokens for embeddings; input + output + thinking for answers (as Gemini
              reports them). Thinking tokens are billed at the output rate, once.
            </p>
          </section>

          <section className="grid gap-2">
            <h3 className="font-semibold">Model breakdown</h3>
            {usage.models.length ? (
              <div className="overflow-x-auto">
                <Table aria-label="Model breakdown">
                  <TableHeader>
                    <TableRow>
                      <TableHead>Model</TableHead>
                      <TableHead className="text-right">Requests</TableHead>
                      <TableHead className="text-right">Input</TableHead>
                      <TableHead className="text-right">Output</TableHead>
                      <TableHead className="text-right">Thinking</TableHead>
                      <TableHead>Price per 1M tokens</TableHead>
                      <TableHead className="text-right">Estimated cost</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {usage.models.map((m) => (
                      <TableRow key={`${m.kind}-${m.model}`} data-testid={`model-${m.model}`}>
                        <TableCell className="min-w-48">
                          <span className="font-mono text-xs">{m.model}</span>
                          <span className="block text-xs text-muted-foreground">{KIND_LABEL[m.kind]}</span>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {m.requests}
                          {m.unmeasured_attempts ? (
                            <span className="block text-xs text-muted-foreground">
                              +{m.unmeasured_attempts} retried attempt(s)
                            </span>
                          ) : null}
                        </TableCell>
                        <TableCell className="text-right">
                          <Tokens value={m.input_tokens} />
                          {m.cached_input_tokens ? (
                            <span className="block text-xs text-muted-foreground">
                              {formatTokens(m.cached_input_tokens)} cached
                            </span>
                          ) : null}
                        </TableCell>
                        <TableCell className="text-right">
                          {m.kind === "embedding" ? "—" : <Tokens value={m.output_tokens} />}
                        </TableCell>
                        <TableCell className="text-right">
                          {m.kind === "embedding" ? "—" : <Tokens value={m.thinking_tokens} />}
                        </TableCell>
                        <TableCell className="text-xs">{ratesLabel(m.rates)}</TableCell>
                        <TableCell className="text-right">
                          <Cost group={m} />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <p className="text-muted-foreground">No Gemini calls were recorded yet.</p>
            )}
          </section>

          <section className="grid gap-2" aria-label="Pricing information">
            <h3 className="font-semibold">Pricing information</h3>
            {usage.pricing ? (
              <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
                <Total label="Pricing tier" value={usage.pricing.tier_label} />
                <Total label="Prices verified on" value={usage.pricing.verified_on} />
                <Total
                  label="Source"
                  value={
                    <a
                      href={usage.pricing.source_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="break-all underline underline-offset-2"
                    >
                      {usage.pricing.source_url}
                    </a>
                  }
                />
                <Total
                  label="Rates (USD per 1M tokens)"
                  value={
                    <ul className="grid gap-0.5">
                      {Object.entries(usage.pricing.models).map(([model, rates]) => (
                        <li key={model}>
                          <span className="font-mono text-xs">{model}</span>: {ratesLabel(rates)}
                        </li>
                      ))}
                    </ul>
                  }
                />
              </dl>
            ) : (
              <p className="text-muted-foreground">No prices were recorded with this run.</p>
            )}
            <p className="text-xs text-muted-foreground">
              Prices are fixed when a run starts, so later price updates never change this run&apos;s
              estimate.
            </p>
          </section>

          <ul className="grid list-disc gap-1 pl-4 text-xs text-muted-foreground">
            {usage.issues.map((issue, i) => (
              <li key={i} className="text-amber-800 dark:text-amber-300">
                {usageIssueText(issue)}
              </li>
            ))}
            <li>
              Covers the Gemini calls made during this evaluation run (one query embedding per
              question, one answer per question and answer mode). It does not include document
              embeddings performed earlier, during ingestion.
            </li>
            <li>
              Requests are logical calls. A request that succeeded after retries also made failed
              attempts, which Gemini reports no token counts for; they are listed as retried
              attempts and not priced.
            </li>
            {run.summary.retry_reservations && run.summary.retry_reservations.count > 0 ? (
              <li>
                Calls made by interrupted attempts that never saved results can&apos;t be measured
                and are not included; budget was reserved for {run.summary.retry_reservations.count}{" "}
                retried question(s). Retry reservations are budget, not usage.
              </li>
            ) : null}
          </ul>
        </>
      ) : (
        <p className="text-muted-foreground">
          Not measured: this run&apos;s summary was saved before token usage was recorded.
        </p>
      )}
    </CollapsibleCard>
  );
}

/** "≥ $0.0123 estimated at paid-tier API pricing (incomplete) · 78 Gemini requests". */
function usageSummaryLine(total: UsageGroup): string {
  const { text, incomplete } = usageCost(total);
  const cost = text === UNAVAILABLE ? "Estimated cost unavailable" : `${text} estimated at paid-tier API pricing`;
  return `${cost}${incomplete ? " (incomplete)" : ""} · ${total.requests} Gemini requests`;
}

function Total({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="font-medium break-words">{value}</dd>
    </div>
  );
}

function Tokens({ value }: { value: number | null }) {
  const text = formatTokens(value);
  if (text === null) return <span className="font-normal text-muted-foreground">{NOT_REPORTED}</span>;
  return <span className="tabular-nums">{text}</span>;
}

function Cost({ group, strong }: { group: UsageGroup; strong?: boolean }) {
  const { text, incomplete } = usageCost(group);
  if (text === UNAVAILABLE) return <span className="font-normal text-muted-foreground">{UNAVAILABLE}</span>;
  return (
    <span className={strong ? "text-lg font-semibold tabular-nums" : "tabular-nums"}>
      {text}
      {incomplete ? (
        <Badge variant="outline" className="ml-1.5 align-middle font-normal">
          incomplete
        </Badge>
      ) : null}
    </span>
  );
}

function BreakdownRow({
  id,
  label,
  group,
  tokens,
  strong,
}: {
  id: string;
  label: string;
  group: UsageGroup;
  tokens: number | null;
  strong?: boolean;
}) {
  return (
    <TableRow data-testid={`breakdown-${id}`} className={strong ? "font-medium" : undefined}>
      <TableCell>{label}</TableCell>
      <TableCell className="text-right tabular-nums">
        {group.requests}
        {group.requests_without_usage ? (
          <span className="block text-xs font-normal text-muted-foreground">
            {group.requests_without_usage} without usage
          </span>
        ) : null}
        {group.unmeasured_attempts ? (
          <span className="block text-xs font-normal text-muted-foreground">
            +{group.unmeasured_attempts} retried attempt(s), not measured
          </span>
        ) : null}
      </TableCell>
      <TableCell className="text-right">
        <Tokens value={tokens} />
      </TableCell>
      <TableCell className="text-right">
        <Cost group={group} />
      </TableCell>
    </TableRow>
  );
}
