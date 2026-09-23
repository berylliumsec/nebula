import { forwardRef, useId, type ForwardRefExoticComponent, type RefAttributes, type SVGProps } from "react";

export type IconWeight = "regular" | "light";
export type LucideProps = Omit<SVGProps<SVGSVGElement>, "ref"> & {
  size?: number | string;
  absoluteStrokeWidth?: boolean;
  weight?: IconWeight;
};
export type LucideIcon = ForwardRefExoticComponent<LucideProps & RefAttributes<SVGSVGElement>>;

const kebab = (name: string) => name.replace(/([a-z0-9])([A-Z])/g, "$1-$2").toLowerCase();

export function createIcon(name: string, regular: string, light: string): LucideIcon {
  const Icon = forwardRef<SVGSVGElement, LucideProps>(function NebulaIcon({
    size = 24,
    color = "currentColor",
    strokeWidth,
    absoluteStrokeWidth: _absoluteStrokeWidth,
    weight,
    className,
    children,
    ...props
  }, ref) {
    const instanceId = useId().replaceAll(":", "");
    const numericSize = typeof size === "number" ? size : Number.parseFloat(size);
    const selectedWeight = weight ?? (strokeWidth !== undefined ? (Number(strokeWidth) <= 1.5 ? "light" : "regular") : (numericSize >= 24 ? "light" : "regular"));
    const body = (selectedWeight === "light" ? light : regular).replaceAll("__NEBULA_ICON_ID__", `nebula-${instanceId}-`);
    return <svg
      ref={ref}
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 256 256"
      fill="currentColor"
      color={color}
      role={props.role ?? (props["aria-label"] ? "img" : undefined)}
      className={["lucide", `lucide-${kebab(name)}`, className].filter(Boolean).join(" ")}
      data-icon-family="phosphor"
      data-icon-weight={selectedWeight}
      {...props}
    ><g dangerouslySetInnerHTML={{ __html: body }} />{children}</svg>;
  });
  Icon.displayName = name;
  return Icon;
}
