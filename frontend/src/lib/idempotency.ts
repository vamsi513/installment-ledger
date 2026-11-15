/**
 * Idempotency keys for write requests.
 *
 * The service requires an `Idempotency-Key` on every write and stores the
 * response against it, so a retry of the same attempt returns the original
 * response rather than applying a second payment. A key therefore has to
 * identify one attempt: a fresh key per submit, and the same key for every
 * retry of that submit.
 */

/** A new key, one per submitted attempt. */
export function newIdempotencyKey(): string {
  // Typed as possibly absent on purpose: the DOM types declare `crypto` as
  // always present, but it is missing in an insecure context.
  const cryptoApi = globalThis.crypto as Crypto | undefined;
  if (cryptoApi !== undefined && typeof cryptoApi.randomUUID === "function") {
    return cryptoApi.randomUUID();
  }

  // Fallback for a context without `crypto.randomUUID`. Collisions here would
  // only ever make a retry look like a new attempt within one browser tab, and
  // the service still refuses a key reused with a different body.
  const random = Math.random().toString(36).slice(2);
  return `ui-${String(Date.now())}-${random}`;
}
