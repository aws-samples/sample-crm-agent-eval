// SPDX-License-Identifier: MIT
/**
 * Edge-anchored panel over the same Radix dialog primitive as `dialog.tsx`, used by the assistant
 * panel below the 1024px breakpoint. `side` defaults to `right`.
 */

import * as DialogPrimitive from "@radix-ui/react-dialog";
import { cva, type VariantProps } from "class-variance-authority";

import { DialogOverlay } from "./dialog";
import { cn, focusRing } from "./utils";

const sheetVariants = cva(
  cn(
    "fixed z-overlay flex flex-col gap-6 bg-surface-overlay p-6 shadow-overlay",
    "duration-normal ease-standard motion-reduce:animate-none",
  ),
  {
    variants: {
      side: {
        right:
          "inset-y-0 right-0 w-full max-w-[var(--layout-panel-width)] border-l-hairline border-border-default animate-in slide-in-from-right",
        left: "inset-y-0 left-0 w-full max-w-[var(--layout-panel-width)] border-r-hairline border-border-default animate-in slide-in-from-left",
        top: "inset-x-0 top-0 border-b-hairline border-border-default animate-in slide-in-from-top",
        bottom:
          "inset-x-0 bottom-0 border-t-hairline border-border-default animate-in slide-in-from-bottom",
      },
    },
    defaultVariants: { side: "right" },
  },
);

export const Sheet = DialogPrimitive.Root;
export const SheetTrigger = DialogPrimitive.Trigger;
export const SheetClose = DialogPrimitive.Close;
export { DialogTitle as SheetTitle, DialogDescription as SheetDescription } from "./dialog";

export type SheetContentProps = React.ComponentProps<typeof DialogPrimitive.Content> &
  VariantProps<typeof sheetVariants>;

export function SheetContent({ className, side, children, ...props }: SheetContentProps) {
  return (
    <DialogPrimitive.Portal>
      <DialogOverlay />
      <DialogPrimitive.Content
        className={cn(sheetVariants({ side }), focusRing, className)}
        {...props}
      >
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}
