# Stored outcome calibration follow-up


## Actual stored-label comparison

A read-only Supabase snapshot at 2026-10-26 21:34:21.830955+00 resolved all13 explicit session IDs. It found8 of30 selected dispatch summaries; missing summaries remain not-ingested, not failed. The query returned transaction_read_only=on and rolled back. No production row or score was rewritten.

The stored-to-v2 classifier comparison changes28 of30 task labels across11 of13 unique sessions. Every stored session label lacks a method version. The transcript classifier snapshot and later database snapshot have different observation times; this is a transition/sensitivity comparison, not ground-truth accuracy.

| Actual stored label → new classifier | Tasks |
|---|---:|
|shipped -> committed|16|
|pushed -> partial|5|
|partial -> partial|1|
|shipped -> pushed|3|
|shipped -> partial|2|
|shipped -> shipped|1|
|pushed -> committed|2|

Independent acceptance remains unknown30/30. Full explicit joins, source times, exact read-only SQL and limitations are in outcome-stored-comparison.json.

