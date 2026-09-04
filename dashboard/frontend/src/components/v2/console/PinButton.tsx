// FC-096 Phase E PR-4 — pin the question the tweak bar just built.
//
// A pin is a standing commitment: the Saturday battery re-measures it for ever,
// and the WINDOW it carries becomes a SHAPE — `(end - start)` and
// `(end - holdout_start)` are stored and re-anchored to the last settled
// session every week (`routers/v2.py:1080`). So the dates in the spec are the
// record of what was asked, not what will run, and the button says so before
// the operator commits rather than after.
//
// `DELETE` is deliberately NOT offered (plan §PR-4): un-pinning is a decision
// about the battery's standing cost, made against the pin LIST, not a step in
// reading one cell.

import { useState } from 'react';
import type { SweepSpec } from '../../../types/v2';
import { pinSpec } from '../../../hooks/useSweeps';
import type { PinOutcome } from '../../../hooks/useSweeps';

export interface PinButtonProps {
  /** The built tweak spec, or `null` while the bar has nothing submittable. */
  spec: SweepSpec | null;
  /** The arm the spec declares — used only for the default note. */
  armName: string | null;
}

export default function PinButton({ spec, armName }: PinButtonProps) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<PinOutcome | null>(null);

  const pin = async () => {
    if (!spec || busy) return;
    setBusy(true);
    setOutcome(null);
    const result = await pinSpec(spec, note.trim() || (armName ?? ''));
    setBusy(false);
    setOutcome(result);
    if (result.kind === 'created') setOpen(false);
  };

  return (
    <span data-testid="pin-button" className="inline-flex flex-col gap-1">
      <span className="inline-flex items-center gap-2">
        <button
          type="button"
          data-testid="pin-open"
          disabled={!spec}
          onClick={() => setOpen((prev) => !prev)}
          className={`rounded border px-2 py-1 text-xs ${
            spec
              ? 'border-gray-600 text-gray-200 hover:bg-gray-700'
              : 'border-gray-700 text-gray-500 cursor-not-allowed'
          }`}
        >
          Pin to the weekly battery
        </button>
        {open && spec && (
          <>
            <input
              data-testid="pin-note"
              aria-label="pin note"
              value={note}
              disabled={busy}
              placeholder="note (optional)"
              onChange={(e) => setNote(e.target.value)}
              className="rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-100"
            />
            <button
              type="button"
              data-testid="pin-confirm"
              disabled={busy}
              onClick={pin}
              className="rounded bg-blue-800 px-2 py-1 text-xs text-white hover:bg-blue-700 disabled:bg-gray-700"
            >
              {busy ? 'Pinning…' : 'Confirm pin'}
            </button>
          </>
        )}
      </span>

      {open && spec && !outcome && (
        <span data-testid="pin-rolling-note" className="text-[11px] text-gray-500">
          The window becomes a SHAPE: its length and its holdout length are re-anchored to the last
          settled session every Saturday, so this pin measures the same question over a window that
          MOVES. The dates above are kept as the record of what you asked for.
        </span>
      )}

      {outcome && (
        <span
          data-testid="pin-outcome"
          data-outcome={outcome.kind}
          className={`text-[11px] ${
            outcome.kind === 'created' ? 'text-green-400' : 'text-amber-400'
          }`}
        >
          {outcome.kind === 'created' ? (
            <>
              Pinned as <span className="font-mono">{outcome.pinId}</span>
              {outcome.windowDays !== null && (
                <>
                  {' '}
                  — a rolling {outcome.windowDays}-day window
                  {outcome.holdoutDays !== null && ` with a ${outcome.holdoutDays}-day holdout`}.
                </>
              )}
            </>
          ) : (
            // Verbatim, every time. The 409 NAMES the pin that already asks
            // this question (or the cap and the list to look at), and that name
            // is the only actionable half of the refusal.
            <span data-testid="pin-detail" className="whitespace-pre-wrap">
              {outcome.detail}
            </span>
          )}
        </span>
      )}
    </span>
  );
}
