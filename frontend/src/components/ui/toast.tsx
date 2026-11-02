// SPDX-License-Identifier: MIT
/**
 * Toast primitives over Radix, which supplies the swipe/timeout behaviour and the live region.
 *
 * `ToastViewport` must be mounted once near the app root. The `denied` variant is for a scope
 * refusal, which is not announced as an error.
 */

import * as ToastPrimitive from "@radix-ui/react-toast";
import { cva, type VariantProps } from "class-variance-authority";

import { cn, focusRing } from "./utils";

export const ToastProvider = ToastPrimitive.Provider;

export function ToastViewport({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitive.Viewport>) {
  return (
    <ToastPrimitive.Viewport
      className={cn(
        "fixed bottom-0 right-0 z-toast flex max-h-screen w-full max-w-narrow flex-col gap-4 p-6",
        className,
      )}
      {...props}
    />
  );
}

const toastVariants = cva(
  cn(
    "pointer-events-auto flex items-start justify-between gap-6",
    "rounded-surface border border-hairline p-5 shadow-overlay",
    "font-sans text-sm",
    "data-[state=open]:animate-in data-[state=open]:slide-in-from-right",
    "data-[state=closed]:animate-out data-[state=closed]:fade-out-0",
    "duration-normal ease-standard motion-reduce:animate-none",
  ),
  {
    variants: {
      variant: {
        neutral: "border-border-default bg-surface-overlay text-text-primary",
        positive: "border-positive-border bg-positive-surface text-positive-text",
        caution: "border-caution-border bg-caution-surface text-caution-text",
        danger: "border-danger-border bg-danger-surface text-danger-text",
        denied: "border-denied-border bg-denied-surface text-denied-text",
      },
    },
    defaultVariants: { variant: "neutral" },
  },
);

export type ToastProps = React.ComponentProps<typeof ToastPrimitive.Root> &
  VariantProps<typeof toastVariants>;

export function Toast({ className, variant, ...props }: ToastProps) {
  return <ToastPrimitive.Root className={cn(toastVariants({ variant }), className)} {...props} />;
}

export function ToastTitle({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitive.Title>) {
  return <ToastPrimitive.Title className={cn("font-medium", className)} {...props} />;
}

export function ToastDescription({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitive.Description>) {
  return (
    <ToastPrimitive.Description className={cn("text-xs opacity-90", className)} {...props} />
  );
}

export function ToastAction({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitive.Action>) {
  return (
    <ToastPrimitive.Action
      className={cn(
        "shrink-0 rounded-control px-4 py-2 text-xs font-medium underline-offset-4 hover:underline",
        focusRing,
        className,
      )}
      {...props}
    />
  );
}

export function ToastClose({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitive.Close>) {
  return (
    <ToastPrimitive.Close
      aria-label="Dismiss"
      className={cn("shrink-0 rounded-control px-2 opacity-70 hover:opacity-100", focusRing, className)}
      {...props}
    >
      ✕
    </ToastPrimitive.Close>
  );
}

export { toastVariants };
