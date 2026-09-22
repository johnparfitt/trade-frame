import { parseAnalysis } from "./parseAnalysis";

function isNone(item: string): boolean {
  return item.toLowerCase() === "none";
}

export default function ReportBody({ analysis }: { analysis: string }) {
  const sections = parseAnalysis(analysis);

  if (sections.length === 0) {
    return (
      <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-200">
        {analysis}
      </p>
    );
  }

  return (
    <div className="space-y-6">
      {sections.map((section) => (
        <section key={section.title}>
          <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">
            {section.title}
          </h3>
          <ul className="space-y-2">
            {section.items.map((item, i) => (
              <li
                key={`${section.title}-${i}`}
                className={
                  isNone(item)
                    ? "text-sm text-slate-500"
                    : "flex gap-2 text-sm leading-relaxed text-slate-200"
                }
              >
                {isNone(item) ? (
                  "None"
                ) : (
                  <>
                    <span className="mt-[0.55rem] h-1 w-1 shrink-0 rounded-full bg-slate-500" />
                    <span>{item}</span>
                  </>
                )}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
