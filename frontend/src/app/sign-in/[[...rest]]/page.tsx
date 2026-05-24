"use client";

import { useSearchParams } from "next/navigation";
import { SignIn } from "@clerk/nextjs";

import { isLocalAuthMode } from "@/auth/localAuth";
import { isCfAccessMode } from "@/auth/mode";
import { resolveSignInRedirectUrl } from "@/auth/redirects";
import { LocalAuthLogin } from "@/components/organisms/LocalAuthLogin";

export default function SignInPage() {
  const searchParams = useSearchParams();

  if (isCfAccessMode()) {
    // CF Access enforces SSO at the edge. By the time this page renders, the
    // user is already signed in via the CF Access magic link cookie. No
    // in-app sign-in surface is needed.
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-50 p-6">
        <div className="rounded-lg border border-slate-200 bg-white px-6 py-4 text-center text-sm text-slate-700 shadow-sm">
          Signed in via Cloudflare Access.
        </div>
      </main>
    );
  }

  if (isLocalAuthMode()) {
    return <LocalAuthLogin />;
  }

  const forceRedirectUrl = resolveSignInRedirectUrl(
    searchParams.get("redirect_url"),
  );

  // Dedicated sign-in route for Cypress E2E.
  // Avoids modal/iframe auth flows and gives Cypress a stable top-level page.
  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 p-6">
      <SignIn
        routing="path"
        path="/sign-in"
        forceRedirectUrl={forceRedirectUrl}
      />
    </main>
  );
}
