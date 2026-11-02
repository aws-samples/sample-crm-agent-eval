// SPDX-License-Identifier: MIT
/** Tooltip primitive over Radix. A tooltip is supplementary — never the only carrier of meaning. */

import * as TooltipPrimitive from "@radix-ui/react-tooltip";

import { cn } from "./utils";

export const TooltipProvider = TooltipPrimitive.Provider;
export const Tooltip = TooltipPrimitive.Root;
export const TooltipTrigger = TooltipPrimitive.Trigger;

export function TooltipContent({
  className,
  sideOffset = 6,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Content>) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        sideOffset={sideOffset}
        className={cn(
          "z-popover max-w-narrow rounded-control bg-surface-inverse px-4 py-2",
          "font-sans text-2xs text-text-inverse shadow-popover",
          "animate-in fade-in-0 zoom-in-95 duration-fast motion-reduce:animate-none",
          className,
        )}
        {...props}
      />
    </TooltipPrimitive.Portal>
  );
}
