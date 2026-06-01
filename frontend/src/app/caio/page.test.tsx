import type React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BookmarkSkipFields } from "./page";

vi.mock("next/navigation", () => ({
  usePathname: () => "/caio",
  useRouter: () => ({
    replace: vi.fn(),
  }),
}));

vi.mock("next/link", () => {
  type LinkProps = React.PropsWithChildren<{
    href: string | { pathname?: string };
  }> &
    Omit<React.AnchorHTMLAttributes<HTMLAnchorElement>, "href">;

  return {
    default: ({ href, children, ...props }: LinkProps) => (
      <a href={typeof href === "string" ? href : "#"} {...props}>
        {children}
      </a>
    ),
  };
});

vi.mock("@/auth/clerk", () => ({
  SignedIn: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  SignedOut: () => null,
  useAuth: () => ({ isSignedIn: true }),
}));

vi.mock("@/api/generated/default/default", () => ({
  useHealthzHealthzGet: () => ({ data: null, isError: false }),
}));

vi.mock("@/api/generated/users/users", () => ({
  useGetMeApiV1UsersMeGet: () => ({ data: null }),
}));

vi.mock("@/lib/use-organization-membership", () => ({
  useOrganizationMembership: () => ({ isAdmin: true }),
}));

describe("BookmarkSkipFields", () => {
  it("shows the complete long source URL with wrapping and title fallback", () => {
    const sourceUrl =
      "https://example.com/bookmarks/" +
      "very-long-path-segment-".repeat(12) +
      "?utm_source=" +
      "long-query-value-".repeat(10);

    render(
      <BookmarkSkipFields
        details={{
          sourceUrl,
          inferredProject: "caio-cockpit",
          estimatedComplexity: "low",
          discardReason: "Already handled.",
        }}
      />,
    );

    const sourceLink = screen.getByRole("link", { name: sourceUrl });

    expect(sourceLink).toHaveTextContent(sourceUrl);
    expect(sourceLink).toHaveAttribute("href", sourceUrl);
    expect(sourceLink).toHaveAttribute("title", sourceUrl);
    expect(sourceLink).toHaveClass("break-all");
  });
});
