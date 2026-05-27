import { describe, expect, it } from "vitest";

import { resolveBookmarkEvaluationCard } from "./caio-bookmark-card";

describe("caio bookmark evaluation cards", () => {
  it("routes viable=false bookmark evaluations to a skip card", () => {
    const card = resolveBookmarkEvaluationCard({
      eventType: "think_loop.policy_decision",
      source: "bookmark-evaluator",
      producerId: "caio",
      payloads: [
        {
          action_type: "bookmark_evaluation",
          viable: false,
          source_url: "https://example.com/post",
          inferred_project: null,
          estimated_complexity: "low",
          reasoning: "Not actionable enough to implement.",
          pr_url: "https://github.com/example/repo/pull/1",
          branch: "feat/example",
          tests: "npm test",
        },
      ],
      decisionKind: null,
    });

    expect(card?.kind).toBe("skip");
    if (card?.kind !== "skip") throw new Error("Expected a skip card");
    expect(card.details).toEqual({
      sourceUrl: "https://example.com/post",
      inferredProject: null,
      estimatedComplexity: "low",
      discardReason: "Not actionable enough to implement.",
    });
  });

  it("routes manually rejected bookmark evaluations to a skip card", () => {
    const card = resolveBookmarkEvaluationCard({
      eventType: "think_loop.proposal",
      source: "think-loop",
      producerId: "caio",
      payloads: [
        {
          kind: "bookmark_evaluation",
          viable: true,
          url: "https://example.com/bookmark",
          project_inferred: "caio-cockpit",
          complexity_estimate: 3,
          reasoning: "Pedro discarded this item manually.",
        },
      ],
      decisionKind: "reject",
    });

    expect(card?.kind).toBe("skip");
    if (card?.kind !== "skip") throw new Error("Expected a skip card");
    expect(card.details).toMatchObject({
      sourceUrl: "https://example.com/bookmark",
      inferredProject: "caio-cockpit",
      estimatedComplexity: "3",
      discardReason: "Pedro discarded this item manually.",
    });
  });

  it("keeps viable bookmark evaluations on the implementation card path", () => {
    const card = resolveBookmarkEvaluationCard({
      eventType: "think_loop.proposal",
      source: "bookmark-evaluator",
      producerId: "caio",
      payloads: [
        {
          action_type: "bookmark_evaluation",
          viable: true,
          source_url: "https://example.com/build-this",
          reasoning: "Clear implementation candidate.",
        },
      ],
      decisionKind: null,
    });

    expect(card).toEqual({ kind: "implementation" });
  });
});
