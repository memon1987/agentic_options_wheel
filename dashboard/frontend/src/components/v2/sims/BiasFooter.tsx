// FC-060 Layer 4 (PR-B): "How far to trust these numbers" — the bias footer.
//
// Extracted from `SweepResults` in FC-096 Phase E PR-2 review round 1 (F2).
// It used to be that component's LAST child, which was the right position right
// up until the console mounted below it: the page then read grid → biases →
// console, so the caveats sat in the middle of the evidence they qualify and
// the operator's last word on the screen was a table rather than "here is how
// far this can be trusted".
//
// The composition rule is now stated where it can be enforced: this component
// is rendered by the PAGE, after everything the caveats are about. `SweepResults`
// no longer renders it at all, so there is no way to get two of them, and no way
// for a future panel to be appended after it by accident.
//
// Everything here is printed BYTE FOR BYTE from the API. These are the runner's
// own words about how far its numbers can be trusted; an earlier cut stripped
// markdown out of them, which quietly edited a warning.

import type { SweepReport } from '../../../types/v2';
import { cls } from '../../../utils/format';

/** The server's prose, rendered exactly as sent. No parsing, no stripping. */
function Verbatim({ text, className }: { text: string; className?: string }) {
  return <p className={cls('whitespace-pre-wrap', className)}>{text}</p>;
}

export default function BiasFooter({ report }: { report: SweepReport }) {
  return (
    <div className="rounded-lg border border-gray-700 bg-gray-900/60 p-4" data-testid="bias-footer">
      <h3 className="text-sm font-semibold text-gray-300">How far to trust these numbers</h3>
      <Verbatim text={report.cross_scenario_caveat} className="text-xs text-gray-400 mt-3" />
      <Verbatim text={report.rejection_tally_caveat} className="text-xs text-gray-400 mt-3" />
      <ul className="mt-3 space-y-2">
        {report.known_biases.map((bias) => (
          <li key={bias.title}>
            <p className="text-xs font-semibold text-gray-300">{bias.title}</p>
            <Verbatim text={bias.detail} className="text-xs text-gray-500" />
          </li>
        ))}
      </ul>
      <p className="text-xs text-gray-500 mt-3">
        A cell is <span className="font-mono text-gray-300">insuf</span> when the window contained no
        completed cycle, <span className="font-mono text-amber-300">low-act N%</span> when a position
        was held on under {Math.round((report.min_days_in_position ?? 0.25) * 100)}% of decision days,
        and <span className="font-mono text-purple-300">unknown</span> when the stored row carries no
        state flag at all. None of the three is a small return: all are excluded from every median
        and every Δ on this page.
      </p>
    </div>
  );
}
