// SPDX-License-Identifier: MIT
/** Combobox: a filterable single-select built from Popover + Command, per the shadcn composition. */

import { useState } from "react";

import { Button } from "./button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "./command";
import { Popover, PopoverContent, PopoverTrigger } from "./popover";
import { cn } from "./utils";

export interface ComboboxOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface ComboboxProps {
  options: readonly ComboboxOption[];
  value?: string;
  onValueChange: (value: string) => void;
  /** Shown on the trigger when nothing is selected. */
  placeholder?: string;
  searchPlaceholder?: string;
  emptyMessage?: string;
  disabled?: boolean;
  className?: string;
  /** Labels the trigger for assistive technology when no visible label is adjacent. */
  "aria-label"?: string;
}

export function Combobox({
  options,
  value,
  onValueChange,
  placeholder = "Select",
  searchPlaceholder = "Search",
  emptyMessage = "No matches",
  disabled = false,
  className,
  "aria-label": ariaLabel,
}: ComboboxProps) {
  const [open, setOpen] = useState(false);
  const selected = options.find((option) => option.value === value);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          role="combobox"
          aria-expanded={open}
          aria-label={ariaLabel}
          disabled={disabled}
          className={cn("w-full justify-between font-regular", className)}
        >
          <span className={cn(!selected && "text-text-muted")}>
            {selected ? selected.label : placeholder}
          </span>
          <span aria-hidden className="text-text-muted">
            ▾
          </span>
        </Button>
      </PopoverTrigger>
      <PopoverContent className="p-0">
        <Command>
          <CommandInput placeholder={searchPlaceholder} />
          <CommandList>
            <CommandEmpty>{emptyMessage}</CommandEmpty>
            <CommandGroup>
              {options.map((option) => (
                <CommandItem
                  key={option.value}
                  value={option.label}
                  disabled={option.disabled}
                  onSelect={() => {
                    onValueChange(option.value);
                    setOpen(false);
                  }}
                >
                  {option.label}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
