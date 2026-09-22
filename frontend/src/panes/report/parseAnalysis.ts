export interface ReportSection {
  title: string;
  items: string[];
}

const HEADINGS: { pattern: RegExp; title: string }[] = [
  { pattern: /^vol(atility)?(\s+surface)?/i, title: "Vol" },
  { pattern: /^(order\s*flow|flow|tide)/i, title: "Flow" },
  { pattern: /^catalyst/i, title: "Catalysts" },
  { pattern: /^next[-\s]?day/i, title: "Next day" },
];

function stripDecor(line: string): string {
  return line
    .replace(/^#+\s*/, "")
    .replace(/\*\*/g, "")
    .replace(/`/g, "")
    .trim();
}

function headingTitle(line: string): string | null {
  const text = stripDecor(line).replace(/^\d+\.\s*/, "");
  if (!text) return null;
  for (const heading of HEADINGS) {
    if (heading.pattern.test(text)) return heading.title;
  }
  return null;
}

function bulletText(line: string): string | null {
  const match = line.trim().match(/^[-*]\s+(.*)$/);
  if (!match) return null;
  const text = stripDecor(match[1]);
  return text || null;
}

export function parseAnalysis(analysis: string): ReportSection[] {
  const sections: ReportSection[] = [];
  let current: ReportSection | null = null;

  for (const raw of analysis.split(/\r?\n/)) {
    const trimmed = raw.trim();
    if (!trimmed) continue;

    const title = headingTitle(trimmed);
    if (title && !/^[-*]/.test(trimmed)) {
      current = { title, items: [] };
      sections.push(current);
      continue;
    }

    const item = bulletText(trimmed);
    if (item) {
      if (!current) {
        current = { title: "Notes", items: [] };
        sections.push(current);
      }
      current.items.push(item);
    }
  }

  return sections.filter((section) => section.items.length > 0);
}
