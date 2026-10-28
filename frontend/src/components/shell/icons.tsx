import type { ReactNode, SVGProps } from "react";

type IconProps = Omit<SVGProps<SVGSVGElement>, "children"> & { size?: number };

function Icon({
  size = 18,
  children,
  ...rest
}: IconProps & { children: ReactNode }) {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...rest}
    >
      {children}
    </svg>
  );
}

export function PlusIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 5v14M5 12h14" />
    </Icon>
  );
}

export function MenuIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 7h16M4 12h16M4 17h10" />
    </Icon>
  );
}

export function PanelIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="3" y="4" width="18" height="16" rx="3" />
      <path d="M14 4v16" />
    </Icon>
  );
}

export function ChartIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />
    </Icon>
  );
}

/** The Sluice mark: a gate over two water lines. Decorative. */
export function SluiceMark({ size = 26 }: { size?: number }) {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      viewBox="0 0 32 32"
      width={size}
      height={size}
    >
      <defs>
        <linearGradient id="sluice-mark" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#6aa8ff" />
          <stop offset="1" stopColor="#3a6dff" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill="url(#sluice-mark)" />
      <path
        d="M9 8v8M16 8v8M23 8v8M7 8h18"
        stroke="#fff"
        strokeWidth="2.2"
        strokeLinecap="round"
      />
      <path
        d="M7 20.5c2.2-1.6 4.4-1.6 6.6 0s4.4 1.6 6.6 0 3.6-1.3 4.8-.6M7 25c2.2-1.6 4.4-1.6 6.6 0s4.4 1.6 6.6 0 3.6-1.3 4.8-.6"
        fill="none"
        stroke="#fff"
        strokeOpacity=".9"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}
