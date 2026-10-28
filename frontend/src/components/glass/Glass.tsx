import type { ComponentPropsWithoutRef, ElementType, ReactNode } from "react";

export type GlassProps<T extends ElementType = "div"> = {
  /** Element or component to render. Defaults to `div`. */
  as?: T;
  className?: string;
  children?: ReactNode;
} & Omit<ComponentPropsWithoutRef<T>, "as" | "className" | "children">;

/**
 * Liquid-glass surface: translucent frosted panel with a 1px gradient
 * highlight border. Styling lives in the `.glass` class in globals.css so it
 * falls back to a solid surface under reduced transparency. Server component.
 */
export function Glass<T extends ElementType = "div">({
  as,
  className,
  children,
  ...rest
}: GlassProps<T>) {
  const Component: ElementType = as ?? "div";
  return (
    <Component className={className ? `glass ${className}` : "glass"} {...rest}>
      {children}
    </Component>
  );
}
