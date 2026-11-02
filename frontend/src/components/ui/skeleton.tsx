// SPDX-License-Identifier: MIT
/**
 * Loading placeholder, with `role="status"` and an accessible label so the wait is announced.
 */

import { cn } from "./utils";

export interface SkeletonProps extends React.ComponentProps<"div"> {
  /** Announced to assistive technology while the region is loading. */
  label?: string;
}

export function Skeleton({ className, label = "Loading", ...props }: SkeletonProps) {
  return (
    <div
      role="status"
      aria-label={label}
      className={cn(
        "animate-pulse rounded-control bg-skeleton-base motion-reduce:animate-none",
        className,
      )}
      {...props}
    />
  );
}
