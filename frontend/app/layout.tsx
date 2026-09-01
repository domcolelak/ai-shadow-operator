import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Shadow Operator",
  description:
    "Discovers repeated work from browser sessions you explicitly recorded, and turns approved ones into safe automations.",
};

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/candidates", label: "Discovered workflows" },
  { href: "/workflows", label: "Automations" },
  { href: "/sessions", label: "Recordings" },
  { href: "/consent", label: "What is recorded" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <aside className="sidebar">
            <div className="brand">
              <span className="brand-mark" aria-hidden />
              <span>Shadow Operator</span>
            </div>
            <nav>
              {NAV.map((item) => (
                <Link key={item.href} href={item.href}>
                  {item.label}
                </Link>
              ))}
            </nav>
            <p className="sidebar-note">
              Recording only happens on sessions you start, on origins you allowlist.
              Passwords are never captured in any form, and no automation performs an
              externally visible action without approval.
            </p>
          </aside>
          <main className="content">{children}</main>
        </div>
      </body>
    </html>
  );
}
