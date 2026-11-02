// SPDX-License-Identifier: MIT
/**
 * Modal dialog over Radix, which supplies the focus trap and Escape handling.
 *
 * `DialogTitle` is mandatory for Radix's labelling contract; omitting it logs an accessibility
 * warning and leaves the dialog unnamed.
 */

import * as DialogPrimitive from "@radix-ui/react-dialog";

import { cn, focusRing } from "./utils";

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

export function DialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      className={cn(
        "fixed inset-0 z-overlay bg-overlay-scrim",
        "animate-in fade-in-0 duration-fast motion-reduce:animate-none",
        className,
      )}
      {...props}
    />
  );
}

export function DialogContent({
  className,
  children,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content>) {
  return (
    <DialogPrimitive.Portal>
      <DialogOverlay />
      <DialogPrimitive.Content
        /**
         * Radix sets no `aria-modal`, and its `aria-hidden` sweep does not reach a page's `main`.
         * A caller may override it via `props`, which spreads after.
         */
        aria-modal="true"
        className={cn(
          "fixed left-1/2 top-1/2 z-overlay w-full max-w-narrow -translate-x-1/2 -translate-y-1/2",
          "rounded-overlay border border-hairline border-border-default bg-surface-overlay p-8",
          "shadow-overlay",
          "animate-in fade-in-0 zoom-in-95 duration-normal ease-entrance motion-reduce:animate-none",
          focusRing,
          className,
        )}
        {...props}
      >
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

export function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("flex flex-col gap-2 pb-6", className)} {...props} />;
}

export function DialogFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div className={cn("flex flex-wrap justify-end gap-4 pt-8", className)} {...props} />
  );
}

export function DialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      className={cn(
        "font-sans text-lg font-semibold leading-tight tracking-tight text-text-primary",
        className,
      )}
      {...props}
    />
  );
}

export function DialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      className={cn("max-w-prose font-sans text-sm text-text-secondary", className)}
      {...props}
    />
  );
}
