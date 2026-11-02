// SPDX-License-Identifier: MIT
/**
 * Status badge, one variant per semantic state. `pending` also carries a dashed border and a hatch,
 * and `denied` its own hue, so neither state rests on colour alone.
 */

import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "./utils";

const badgeVariants = cva(
  "inline-flex items-center gap-2 rounded-pill border border-hairline px-4 py-1 font-sans text-2xs font-medium leading-snug",
  {
    variants: {
      variant: {
        neutral: "border-border-default bg-surface-inset text-text-secondary",
        accent: "border-accent-border bg-accent-surface text-accent-text",
        positive: "border-positive-border bg-positive-surface text-positive-text",
        caution: "border-caution-border bg-caution-surface text-caution-text",
        danger: "border-danger-border bg-danger-surface text-danger-text",
        denied: "border-denied-border bg-denied-surface text-denied-text",
        info: "border-info-border bg-info-surface text-info-text",
        pending:
          "border-dashed border-pending-border bg-pending-surface bg-pending-hatch text-pending-text",
      },
    },
    defaultVariants: { variant: "neutral" },
  },
);

export type BadgeProps = React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>;

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { badgeVariants };
