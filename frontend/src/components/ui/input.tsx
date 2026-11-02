// SPDX-License-Identifier: MIT
/** Text input primitive. `aria-invalid` drives the error treatment, so validation state is announced. */

import { cn, focusRing } from "./utils";

export function Input({ className, type = "text", ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      className={cn(
        "flex h-control-md w-full rounded-control border border-hairline border-border-control",
        "bg-surface-raised px-4 font-sans text-sm text-text-primary",
        "placeholder:text-text-muted",
        "disabled:cursor-not-allowed disabled:bg-surface-inset disabled:text-text-muted",
        "aria-[invalid=true]:border-danger-border aria-[invalid=true]:bg-danger-surface",
        focusRing,
        className,
      )}
      {...props}
    />
  );
}
