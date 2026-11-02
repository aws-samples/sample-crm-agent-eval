// SPDX-License-Identifier: MIT
/** Select primitive over Radix, which supplies typeahead and the listbox keyboard contract. */

import * as SelectPrimitive from "@radix-ui/react-select";

import { cn, focusRing } from "./utils";

export const Select = SelectPrimitive.Root;
export const SelectGroup = SelectPrimitive.Group;
export const SelectValue = SelectPrimitive.Value;

export function SelectTrigger({
  className,
  children,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Trigger>) {
  return (
    <SelectPrimitive.Trigger
      className={cn(
        "flex h-control-md w-full items-center justify-between gap-4 rounded-control",
        "border border-hairline border-border-control bg-surface-raised px-4",
        "font-sans text-sm text-text-primary",
        "data-[placeholder]:text-text-muted",
        "disabled:cursor-not-allowed disabled:bg-surface-inset disabled:text-text-muted",
        focusRing,
        className,
      )}
      {...props}
    >
      {children}
      <SelectPrimitive.Icon aria-hidden className="text-text-muted">
        ▾
      </SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  );
}

export function SelectContent({
  className,
  position = "popper",
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Content>) {
  return (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Content
        position={position}
        className={cn(
          "z-popover overflow-hidden rounded-surface border border-hairline border-border-default",
          "bg-surface-overlay shadow-popover",
          "animate-in fade-in-0 zoom-in-95 duration-fast motion-reduce:animate-none",
          className,
        )}
        {...props}
      >
        <SelectPrimitive.Viewport className="p-2">{props.children}</SelectPrimitive.Viewport>
      </SelectPrimitive.Content>
    </SelectPrimitive.Portal>
  );
}

export function SelectLabel({
  className,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Label>) {
  return (
    <SelectPrimitive.Label
      className={cn("px-4 py-2 font-sans text-2xs font-semibold text-text-muted", className)}
      {...props}
    />
  );
}

export function SelectItem({
  className,
  children,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Item>) {
  return (
    <SelectPrimitive.Item
      className={cn(
        "relative flex cursor-default select-none items-center rounded-control px-4 py-3",
        "font-sans text-sm text-text-primary outline-none",
        "data-[highlighted]:bg-surface-hover",
        "data-[state=checked]:bg-surface-selected data-[state=checked]:font-medium",
        "data-[disabled]:pointer-events-none data-[disabled]:text-text-muted",
        className,
      )}
      {...props}
    >
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    </SelectPrimitive.Item>
  );
}

export function SelectSeparator({
  className,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Separator>) {
  return (
    <SelectPrimitive.Separator
      className={cn("my-2 h-px bg-border-subtle", className)}
      {...props}
    />
  );
}
