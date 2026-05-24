export enum AuthMode {
  Clerk = "clerk",
  Local = "local",
  CfAccess = "cf_access",
}

/** True when the backend is configured to validate Cloudflare Access JWTs.
 *
 * In this mode the browser never holds an auth token: CF Access sets its own
 * httpOnly cookie at the edge and the tunnel injects ``Cf-Access-Jwt-Assertion``
 * on every request. The frontend should NOT add an ``Authorization`` header. */
export function isCfAccessMode(): boolean {
  return process.env.NEXT_PUBLIC_AUTH_MODE === AuthMode.CfAccess;
}
