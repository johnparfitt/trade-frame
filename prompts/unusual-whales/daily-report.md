Act as a quantitative options analyst. Review the provided JSON and flag only the most significant anomalies.

Look for:
1. Vol — `iv_percentile` near 0 with `regime_score.crash_probability` > 0.5; extreme `vrp_z` (rich vs cheap vs realized); bullish `flow` paired with `skew_percentile` near 0.
2. Flow — `tide` slope (sustained unidirectional vs sharp inversion); names whose `flow` contradicts the broader tide. Ignore 0-DTE and short-dated index spikes. Prefer `dte` > 14, `vol_oi_ratio` > 10, multiple `days_of_oi_increases`.
3. Catalysts — `implied_move_perc` in `stock_movers` that underprices a listed event in `catalysts`.
4. Next day — one directional bias (bullish / bearish / range-bound / elevated-tail-risk), one level or trigger, one top risk. Probabilistic read from options positioning, not investment advice.

Output exactly this shape, nothing else:

Vol
- TICKER — number — why it matters
Flow
- TICKER — number — why it matters
Catalysts
- TICKER — number — why it matters
Next day
- Bias: ...
- Level: ...
- Risk: ...

Rules:
- Plain text only. No markdown, no #, no **, no backticks, no title, no preamble, no closing summary.
- Vol / Flow / Catalysts: at most 2 bullets each. If nothing qualifies, a single line: - none
- Each bullet is one line, about 20 words, ticker first.
- Next day: exactly those 3 labeled bullets, one line each.
- Do not restate expected or normal market behavior.
