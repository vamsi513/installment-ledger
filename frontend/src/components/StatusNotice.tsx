/** Shared loading, error and empty states, so every view reports the same way. */

import type { JSX } from "react";

import { ApiError, describeError } from "../api/errors";

export function Loading({ label }: { label: string }): JSX.Element {
  return (
    <p className="notice notice-loading" role="status">
      {label}
    </p>
  );
}

export function Empty({ label }: { label: string }): JSX.Element {
  return <p className="notice notice-empty">{label}</p>;
}

/**
 * Show a failure in full: the service's own message, its stable code, and the
 * per field detail when it sent any. A duplicate request is called out
 * separately, because it means the opposite of a validation failure: the
 * service kept the first attempt and refused to repeat it.
 */
export function ErrorNotice({
  error,
  title,
}: {
  error: unknown;
  title?: string;
}): JSX.Element {
  const apiError = error instanceof ApiError ? error : null;
  const fields = apiError?.validationFields ?? [];

  return (
    <div className="notice notice-error" role="alert">
      {title !== undefined && <p className="notice-title">{title}</p>}
      <p className="notice-message">{describeError(error)}</p>

      {apiError !== null && (
        <p className="notice-meta">
          Error code <code>{apiError.code}</code>, HTTP {apiError.status}
        </p>
      )}

      {apiError?.isDuplicateRequest === true && (
        <p className="notice-duplicate">
          The service had already seen this idempotency key, so nothing was posted twice. Reload the
          ledger to see the attempt that was kept.
        </p>
      )}

      {fields.length > 0 && (
        <ul className="notice-fields">
          {fields.map((field, index) => (
            <li key={`${field.location.join(".")}-${String(index)}`}>
              <code>{field.location.join(".")}</code>: {field.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
