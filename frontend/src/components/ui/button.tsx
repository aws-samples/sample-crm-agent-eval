// SPDX-License-Identifier: MIT
/**
 * Button primitive. `asChild` renders the variant onto a child element (a router `Link`, say)
 * instead of a `<button>`, so navigation keeps its correct semantics.
 */

import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn, focusRing } from "./utils";

const buttonVariants = cva(
  cn(
    "inline-flex shrink-0 items-center justify-center gap-3 whitespace-nowrap rounded-control",
    "font-sans text-sm font-medium leading-snug",
    "transition-colors duration-fast ease-standard",
    "disabled:pointer-events-none disabled:opacity-50 aria-disabled:pointer-events-none aria-disabled:opacity-50",
    focusRing,
  ),
  {
    variants: {
      variant: {
        primary:
          "bg-accent-emphasis text-text-on-accent hover:bg-accent-emphasis-hover active:bg-accent-emphasis-active",
        secondary:
          "border border-hairline border-border-control bg-surface-raised text-text-primary hover:bg-surface-hover",
        ghost: "text-text-secondary hover:bg-surface-hover hover:text-text-primary",
        link: "text-text-link underline-offset-4 hover:underline",
        /** For a destructive commit. A scope denial is not destructive — it uses no button variant. */
        danger:
          "bg-danger-emphasis text-text-on-accent hover:bg-danger-emphasis-hover",
      },
      size: {
        sm: "h-control-sm px-4 text-xs",
        md: "h-control-md px-5",
        lg: "h-control-lg px-6 text-md",
        icon: "h-control-md w-[var(--control-height-md)] px-0",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export type ButtonProps = React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & { asChild?: boolean };

export function Button({ className, variant, size, asChild = false, ...props }: ButtonProps) {
  const Component = asChild ? Slot : "button";
  return (
    <Component className={cn(buttonVariants({ variant, size }), className)} {...props} />
  );
}

export { buttonVariants };
