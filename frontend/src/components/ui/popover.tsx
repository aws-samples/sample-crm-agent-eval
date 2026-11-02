// SPDX-License-Identifier: MIT
/** Popover primitive over Radix; the anchor layer the combobox opens into. */

import * as PopoverPrimitive from "@radix-ui/react-popover";

import { cn } from "./utils";

export const Popover = PopoverPrimitive.Root;
export const PopoverTrigger = PopoverPrimitive.Trigger;
export const PopoverAnchor = PopoverPrimitive.Anchor;

export function PopoverContent({
  className,
  align = "start",
  sideOffset = 4,
  ...props
}: React.ComponentProps<typeof PopoverPrimitive.Content>) {
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content
        align={align}
        sideOffset={sideOffset}
        className={cn(
          "z-popover w-full rounded-surface border border-hairline border-border-default",
          "bg-surface-overlay shadow-popover",
          "animate-in fade-in-0 zoom-in-95 duration-fast motion-reduce:animate-none",
          className,
        )}
        {...props}
      />
    </PopoverPrimitive.Portal>
  );
}
