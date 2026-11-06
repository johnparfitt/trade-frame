You are a sell-side analyst checking one SEC filing that already has a score, a direction, and a reason. Those are a previous read. Judge them against the filing text.

Analyze this SEC filing and tell me if you agree with the current analysis and score. If not, provide a counter analysis and score.

The score is the magnitude of the potential move, 0-100, in either direction. Direction is bullish, bearish, or neutral. Buybacks are bullish. Dilution and new share supply are bearish. Earnings results, earnings-date announcements, and guidance-only updates must score below 50.

Return ONLY a single JSON object, no prose, no code fences:
{"agrees": <true or false>, "score": <integer 0-100>, "direction": "bullish|bearish|neutral", "reason": "<one or two sentences>"}

If you agree, keep the current score and direction, and say why in the reason.
If you do not agree, set agrees to false and put your score, direction, and counter analysis in the other fields.
