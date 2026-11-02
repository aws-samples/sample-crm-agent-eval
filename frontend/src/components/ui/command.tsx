// SPDX-License-Identifier: MIT
/** Filterable command list from `cmdk`, the substrate the combobox is built on. */

import { Command as CommandPrimitive } from "cmdk";

import { cn, focusRing } from "./utils";

export function Command({ className, ...props }: React.ComponentProps<typeof CommandPrimitive>) {
  return (
    <CommandPrimitive
      className={cn("flex w-full flex-col overflow-hidden font-sans text-sm", className)}
      {...props}
    />
  );
}

export function CommandInput({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Input>) {
  return (
    <div className="border-b-hairline border-border-subtle px-4">
      <CommandPrimitive.Input
        className={cn(
          "h-control-lg w-full bg-transparent text-sm text-text-primary",
          "placeholder:text-text-muted",
          focusRing,
          className,
        )}
        {...props}
      />
    </div>
  );
}

export function CommandList({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.List>) {
  return (
    <CommandPrimitive.List
      className={cn("max-h-64 overflow-y-auto overflow-x-hidden p-2", className)}
      {...props}
    />
  );
}

/** An empty result renders a message, never a blank region. */
export function CommandEmpty({
  className,
  children = "No matches",
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Empty>) {
  return (
    <CommandPrimitive.Empty
      className={cn(
        "rounded-control bg-empty-surface px-4 py-6 text-center text-xs text-empty-text",
        className,
      )}
      {...props}
    >
      {children}
    </CommandPrimitive.Empty>
  );
}

export function CommandGroup({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Group>) {
  return (
    <CommandPrimitive.Group
      className={cn(
        "[&_[cmdk-group-heading]]:px-4 [&_[cmdk-group-heading]]:py-2",
        "[&_[cmdk-group-heading]]:text-2xs [&_[cmdk-group-heading]]:font-semibold",
        "[&_[cmdk-group-heading]]:text-text-muted",
        className,
      )}
      {...props}
    />
  );
}

export function CommandItem({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Item>) {
  return (
    <CommandPrimitive.Item
      className={cn(
        "flex cursor-default select-none items-center rounded-control px-4 py-3 outline-none",
        "text-text-primary data-[selected=true]:bg-surface-hover",
        "data-[disabled=true]:pointer-events-none data-[disabled=true]:text-text-muted",
        className,
      )}
      {...props}
    />
  );
}

export function CommandSeparator({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Separator>) {
  return (
    <CommandPrimitive.Separator
      className={cn("my-2 h-px bg-border-subtle", className)}
      {...props}
    />
  );
}
