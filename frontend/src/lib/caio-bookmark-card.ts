type CaioDecisionKind = "approve" | "reject";

type ResolveBookmarkEvaluationCardInput = {
  eventType: string;
  source: string;
  producerId: string;
  payloads: Array<Record<string, unknown> | null | undefined>;
  decisionKind: CaioDecisionKind | null;
};

export type BookmarkSkipDetails = {
  sourceUrl: string | null;
  inferredProject: string | null;
  estimatedComplexity: string | null;
  discardReason: string | null;
};

export type BookmarkEvaluationCard =
  | { kind: "implementation" }
  | { kind: "skip"; details: BookmarkSkipDetails };

const BOOKMARK_HINT_FIELDS = [
  "kind",
  "type",
  "action_type",
  "task_type",
  "workflow",
  "source_type",
  "object_type",
];

const SOURCE_URL_PATHS = [
  ["source_url"],
  ["sourceUrl"],
  ["bookmark_url"],
  ["bookmarkUrl"],
  ["url"],
  ["link"],
  ["href"],
  ["bookmark", "url"],
  ["source", "url"],
  ["item", "url"],
];

const PROJECT_PATHS = [
  ["inferred_project"],
  ["project_inferred"],
  ["inferredProject"],
  ["projectInferred"],
  ["project"],
  ["project_name"],
  ["projectName"],
];

const COMPLEXITY_PATHS = [
  ["estimated_complexity"],
  ["complexity_estimate"],
  ["estimatedComplexity"],
  ["complexityEstimate"],
  ["complexity"],
  ["implementation_complexity"],
];

const REASON_PATHS = [
  ["reasoning"],
  ["reason"],
  ["discard_reason"],
  ["discardReason"],
  ["skip_reason"],
  ["skipReason"],
  ["decision", "reason"],
  ["evaluation", "reasoning"],
];

const VIABLE_PATHS = [
  ["viable"],
  ["is_viable"],
  ["isViable"],
  ["evaluation", "viable"],
];

const DISCARD_FLAG_PATHS = [
  ["discarded"],
  ["manually_discarded"],
  ["manual_discarded"],
  ["discarded_manually"],
  ["manualDiscarded"],
  ["discardedManually"],
  ["skipped"],
  ["skip"],
];

const DISCARD_STATUS_PATHS = [
  ["status"],
  ["outcome"],
  ["action"],
  ["decision"],
  ["evaluation", "status"],
  ["evaluation", "outcome"],
];

const DISCARD_STATUS_VALUES = new Set([
  "discard",
  "discarded",
  "manual_discard",
  "manual_discarded",
  "manually_discarded",
  "reject",
  "rejected",
  "skip",
  "skipped",
]);

function getPath(payload: Record<string, unknown>, path: string[]): unknown {
  let current: unknown = payload;
  for (const segment of path) {
    if (!current || typeof current !== "object" || Array.isArray(current)) {
      return undefined;
    }
    current = (current as Record<string, unknown>)[segment];
  }
  return current;
}

function valueToText(value: unknown): string | null {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed ? trimmed : null;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }
  if (typeof value === "boolean") {
    return String(value);
  }
  if (value && typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return null;
    }
  }
  return null;
}

function firstText(
  payloads: Record<string, unknown>[],
  paths: string[][],
): string | null {
  for (const payload of payloads) {
    for (const path of paths) {
      const text = valueToText(getPath(payload, path));
      if (text) return text;
    }
  }
  return null;
}

function firstUrl(payloads: Record<string, unknown>[]): string | null {
  const value = firstText(payloads, SOURCE_URL_PATHS);
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? parsed.toString()
      : null;
  } catch {
    return null;
  }
}

function hasBookmarkHint(
  input: ResolveBookmarkEvaluationCardInput,
  payloads: Record<string, unknown>[],
): boolean {
  const identifiers = [input.eventType, input.source, input.producerId];
  for (const value of identifiers) {
    if (value.toLowerCase().includes("bookmark")) return true;
  }

  for (const payload of payloads) {
    for (const field of BOOKMARK_HINT_FIELDS) {
      const value = valueToText(payload[field]);
      if (value?.toLowerCase().includes("bookmark")) return true;
    }
    if (firstText([payload], [["bookmark_url"], ["bookmarkUrl"]])) return true;
    if (getPath(payload, ["bookmark", "url"])) return true;
  }
  return false;
}

function hasEvaluationShape(payloads: Record<string, unknown>[]): boolean {
  return payloads.some((payload) => {
    const hasViability = VIABLE_PATHS.some(
      (path) => typeof getPath(payload, path) === "boolean",
    );
    const hasReasoning = firstText([payload], REASON_PATHS) !== null;
    const hasComplexity = firstText([payload], COMPLEXITY_PATHS) !== null;
    return (
      hasViability &&
      firstUrl([payload]) !== null &&
      (hasReasoning || hasComplexity)
    );
  });
}

function hasViableFalse(payloads: Record<string, unknown>[]): boolean {
  return payloads.some((payload) =>
    VIABLE_PATHS.some((path) => getPath(payload, path) === false),
  );
}

function hasManualDiscard(payloads: Record<string, unknown>[]): boolean {
  return payloads.some((payload) => {
    const flagDiscarded = DISCARD_FLAG_PATHS.some(
      (path) => getPath(payload, path) === true,
    );
    if (flagDiscarded) return true;

    return DISCARD_STATUS_PATHS.some((path) => {
      const value = valueToText(getPath(payload, path));
      return value ? DISCARD_STATUS_VALUES.has(value.toLowerCase()) : false;
    });
  });
}

function skipDetails(payloads: Record<string, unknown>[]): BookmarkSkipDetails {
  return {
    sourceUrl: firstUrl(payloads),
    inferredProject: firstText(payloads, PROJECT_PATHS),
    estimatedComplexity: firstText(payloads, COMPLEXITY_PATHS),
    discardReason: firstText(payloads, REASON_PATHS),
  };
}

export function resolveBookmarkEvaluationCard(
  input: ResolveBookmarkEvaluationCardInput,
): BookmarkEvaluationCard | null {
  const payloads = input.payloads.filter(
    (payload): payload is Record<string, unknown> =>
      Boolean(payload) &&
      typeof payload === "object" &&
      !Array.isArray(payload),
  );
  if (!payloads.length) return null;

  const isBookmarkEvaluation =
    hasBookmarkHint(input, payloads) || hasEvaluationShape(payloads);
  if (!isBookmarkEvaluation) return null;

  const shouldSkip =
    hasViableFalse(payloads) ||
    hasManualDiscard(payloads) ||
    input.decisionKind === "reject";

  return shouldSkip
    ? { kind: "skip", details: skipDetails(payloads) }
    : { kind: "implementation" };
}
