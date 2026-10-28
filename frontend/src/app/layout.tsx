import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import { ApiProvider } from "@/components/api/ApiProvider";
import { Backdrop } from "@/components/glass/Backdrop";
import { ThemeProvider } from "@/components/theme/ThemeProvider";
import { themeInitScript } from "@/components/theme/theme-script";
import "./globals.css";

const sans = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  display: "swap",
});

const mono = JetBrains_Mono({
  variable: "--font-jetbrains-mono",
  subsets: ["latin"],
  display: "swap",
});

export const metadata: Metadata = {
  title: {
    default: "Sluice",
    template: "%s · Sluice",
  },
  description:
    "A self-planning agentic data pipeline for drifting data, measured like production.",
};

export const viewport: Viewport = {
  colorScheme: "light dark",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // The head script sets data-theme / data-reduce-transparency before
    // hydration, so those attributes legitimately differ from the server HTML.
    <html
      lang="en"
      className={`${sans.variable} ${mono.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <head>
        <script
          // biome-ignore lint/security/noDangerouslySetInnerHtml: static, build-time constant; must run before first paint to avoid a theme flash.
          dangerouslySetInnerHTML={{ __html: themeInitScript }}
        />
      </head>
      <body className="flex min-h-full flex-col">
        <Backdrop />
        <ThemeProvider>
          <ApiProvider>{children}</ApiProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
