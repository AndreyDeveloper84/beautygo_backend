# DRF-232 — H3 Memory Hypothesis Validation (SQL Pack)

> Status: READY TO RUN · Author: Claude · Date: 2026-05-05
> Decision Day: **2026-05-13** (this pack drives the go/no-go on UserPersonalContext investment)
> Source DB: production MAX-bot Django DB (`mysite/services_app/*` tables)
> Audit reference: `docs/BOT_CODE_AUDIT_2026-04.md` §1.6 + §6
> Hypothesis ticket: `docs/HYPOTHESIS_VALIDATION_PLAN_2026-04.md` H3
> Companion: `docs/PRODUCT_AUDIT_2026-04.md` §1.9 (UserPersonalContext architecture)

## Why this pack exists

H3 = "AI который помнит → conversion lift". The bot has been running
30+ days with full telemetry on every assistant turn (`Message.action_type`
+ `action_data` + `tokens_in/out` + `latency_ms`). Decision Day asks:

1. **Is the AI flow actually used?** What share of conversations reach
   `confirm_booking`? What's the funnel drop-off shape?
2. **Do repeat clients book the same master?** That's the proxy for
   "memory pays off" before we have an actual UserPersonalContext.
3. **What does action distribution look like?** Are people stuck on
   `ask_clarification` (poor LLM grounding) or moving through
   `show_masters → show_slots → confirm_booking`?

Decision matrix:

| Signal | Meaning | Action |
|---|---|---|
| Conv rate (show_masters → confirm_booking) ≥ 25 % | LLM grounding works → memory will move the needle further | **Build full UserPersonalContext** (Phase 6 deferral revisited) |
| Conv rate 10–25 % | Mixed — some funnel friction, hard to tell if memory helps | **Ship the wire-up (DRF-230) only**, run A/B post-pilot |
| Conv rate < 10 % | LLM picks wrong masters / users don't trust recs | **Don't build memory yet** — fix grounding (better SpecialistContext, tool definitions) |

| Repeat-master pattern | Meaning | Action |
|---|---|---|
| ≥ 30 % of returning bookers re-book same master | Strong "favorite master" signal | UserPersonalContext.favorite_master is high-value |
| 15–30 % | Weak signal — some preference, not load-bearing | Defer favorite_master, keep just preferences hint |
| < 15 % | Random — clients pick by availability not loyalty | Memory promise is weaker than thought; pivot positioning |

## How to run

These are **PostgreSQL queries** against the bot's production DB. Run
each block independently — they're independent metrics, no single
"verdict" query.

```bash
# On the bot VPS (or read-replica if you have one):
psql -h <host> -U <user> -d <bot_db> -f drf_232_section_<N>.sql

# Or paste into Django dbshell:
ssh maxbot-vps
cd ~/mysite
docker compose exec -T web python manage.py dbshell
\i /path/to/section.sql
```

If running directly against Django:

```bash
docker compose exec -T web python manage.py shell -c "
from django.db import connection
with connection.cursor() as cur:
    cur.execute(open('section_1.sql').read())
    for row in cur.fetchall():
        print(row)
"
```

Save outputs as `outputs/section_<N>_<YYYY-MM-DD>.csv` and share in
the Decision Day doc.

---

## Section 1 — Action type distribution (volume, latency, cost)

**Question:** Where is the AI actually spending its turns? Is the
conversation a sequence of `ask_clarification` (poor grounding) or a
healthy `show_masters → show_slots → confirm_booking` funnel?

```sql
-- 1A. Action type distribution last 30d (raw counts + cost signal)
SELECT
    action_type,
    COUNT(*)                                                 AS turns,
    ROUND(AVG(latency_ms))                                   AS avg_latency_ms,
    ROUND(AVG(tokens_in + tokens_out))                       AS avg_tokens_total,
    SUM(tokens_in + tokens_out)                              AS total_tokens
FROM services_app_message
WHERE role = 'assistant'
  AND created_at > NOW() - INTERVAL '30 days'
GROUP BY action_type
ORDER BY turns DESC;
```

**What to look for:**

- `ask_clarification` should be ≤ 15 % of turns. Above means LLM is
  flailing — fix grounding before investing in memory.
- `show_masters` + `show_slots` + `confirm_booking` together = the
  productive funnel. Should be > 60 % combined.
- A bare row (action_type = `''`) = pure-text answers (greetings,
  off-topic redirects). Some volume here is healthy; 50 %+ means
  the bot isn't reaching tool-use stage often enough.

```sql
-- 1B. Daily trend — is the funnel improving or degrading week over week?
SELECT
    DATE_TRUNC('day', created_at) AS day,
    action_type,
    COUNT(*) AS turns
FROM services_app_message
WHERE role = 'assistant'
  AND created_at > NOW() - INTERVAL '30 days'
GROUP BY day, action_type
ORDER BY day, turns DESC;
```

Plot day × action_type as a stacked area chart. Look for an
`ask_clarification` cliff (drop on a specific date = a prompt change
helped) or a confirm_booking ramp (LLM warming up).

---

## Section 2 — Funnel conversion (show_masters → confirm_booking)

**Question:** Of the conversations that reached the recommendation
stage, how many made it to a booking confirmation?

```sql
-- 2A. Per-conversation funnel: how far did it get?
WITH per_conv AS (
    SELECT
        conversation_id,
        BOOL_OR(action_type = 'show_masters')      AS reached_masters,
        BOOL_OR(action_type = 'show_slots')        AS reached_slots,
        BOOL_OR(action_type = 'confirm_booking')   AS reached_confirm
    FROM services_app_message
    WHERE role = 'assistant'
      AND created_at > NOW() - INTERVAL '30 days'
    GROUP BY conversation_id
)
SELECT
    COUNT(*)                                                   AS total_convs,
    COUNT(*) FILTER (WHERE reached_masters)                    AS reached_masters,
    COUNT(*) FILTER (WHERE reached_slots)                      AS reached_slots,
    COUNT(*) FILTER (WHERE reached_confirm)                    AS reached_confirm,
    ROUND(100.0 * COUNT(*) FILTER (WHERE reached_masters)
          / NULLIF(COUNT(*), 0), 1)                            AS pct_reached_masters,
    ROUND(100.0 * COUNT(*) FILTER (WHERE reached_slots)
          / NULLIF(COUNT(*) FILTER (WHERE reached_masters), 0), 1)
                                                                AS pct_masters_to_slots,
    ROUND(100.0 * COUNT(*) FILTER (WHERE reached_confirm)
          / NULLIF(COUNT(*) FILTER (WHERE reached_slots), 0), 1)
                                                                AS pct_slots_to_confirm,
    ROUND(100.0 * COUNT(*) FILTER (WHERE reached_confirm)
          / NULLIF(COUNT(*) FILTER (WHERE reached_masters), 0), 1)
                                                                AS pct_masters_to_confirm
FROM per_conv;
```

The single number that drives the decision matrix above is
`pct_masters_to_confirm`. Three other numbers explain *why*:

- `pct_reached_masters` low → users abandon at intake (off-topic,
  unclear ask) or LLM doesn't recommend at all
- `pct_masters_to_slots` low → users don't like the recommendations
  (memory would help by remembering "they liked Анна")
- `pct_slots_to_confirm` low → slot UX problem (no slots fit, friction
  on the confirm card)

```sql
-- 2B. Funnel by week — is it improving?
WITH per_conv AS (
    SELECT
        conversation_id,
        DATE_TRUNC('week', MIN(created_at)) AS week,
        BOOL_OR(action_type = 'show_masters')    AS reached_masters,
        BOOL_OR(action_type = 'confirm_booking') AS reached_confirm
    FROM services_app_message
    WHERE role = 'assistant'
      AND created_at > NOW() - INTERVAL '30 days'
    GROUP BY conversation_id
)
SELECT
    week,
    COUNT(*)                                             AS convs,
    COUNT(*) FILTER (WHERE reached_masters)              AS to_masters,
    COUNT(*) FILTER (WHERE reached_confirm)              AS to_confirm,
    ROUND(100.0 * COUNT(*) FILTER (WHERE reached_confirm)
          / NULLIF(COUNT(*) FILTER (WHERE reached_masters), 0), 1)
                                                          AS conv_pct
FROM per_conv
GROUP BY week
ORDER BY week;
```

---

## Section 3 — Repeat-master pattern (favorite_master proxy)

**Question:** Do clients who book more than once tend to book the
same master? That's the empirical proxy for the
`UserPersonalContext.favorite_master` field's value.

`BookingRequest.source = 'bot_max'` is what the bot writes; `is_processed`
flips to `true` once the YClients API call succeeds (= a real booking).

```sql
-- 3A. Returning bookers + master loyalty
WITH user_bookings AS (
    SELECT
        bot_user_id,
        master_name,
        COUNT(*) AS bookings_with_master
    FROM services_app_bookingrequest
    WHERE source = 'bot_max'
      AND is_processed = TRUE
      AND created_at > NOW() - INTERVAL '90 days'  -- wider window — repeat behaviour is rare
    GROUP BY bot_user_id, master_name
),
user_totals AS (
    SELECT
        bot_user_id,
        SUM(bookings_with_master)              AS total_bookings,
        MAX(bookings_with_master)              AS top_master_bookings
    FROM user_bookings
    GROUP BY bot_user_id
)
SELECT
    COUNT(*) FILTER (WHERE total_bookings >= 2)                 AS returning_bookers,
    COUNT(*) FILTER (WHERE total_bookings >= 2
                     AND top_master_bookings >= 2)              AS loyal_to_one_master,
    ROUND(100.0 * COUNT(*) FILTER (WHERE total_bookings >= 2
                                    AND top_master_bookings >= 2)
          / NULLIF(COUNT(*) FILTER (WHERE total_bookings >= 2), 0), 1)
                                                                  AS loyalty_pct
FROM user_totals;
```

`loyalty_pct` is the answer: of users who came back at least twice,
what fraction kept the same master? Decision matrix above uses this.

```sql
-- 3B. Top loyal pairs — for the qualitative interview list
SELECT
    bot_user_id,
    master_name,
    COUNT(*) AS bookings
FROM services_app_bookingrequest
WHERE source = 'bot_max'
  AND is_processed = TRUE
  AND created_at > NOW() - INTERVAL '90 days'
GROUP BY bot_user_id, master_name
HAVING COUNT(*) >= 2
ORDER BY COUNT(*) DESC, master_name
LIMIT 20;
```

Pull these `bot_user_id`'s into the customer interview list — they're
the real "memory matters" cohort.

---

## Section 4 — LLM grounding health check

**Question:** Is the bot reaching the recommendation stage at all, or
abandoning to plain-text answers? An off-topic-heavy population means
the system prompt isn't anchoring well.

```sql
-- 4A. Conversations that NEVER produced a tool-use action
WITH per_conv AS (
    SELECT
        conversation_id,
        COUNT(*) FILTER (WHERE role = 'assistant')                              AS assistant_turns,
        COUNT(*) FILTER (WHERE role = 'assistant' AND action_type = '')          AS plain_text_turns,
        COUNT(*) FILTER (WHERE role = 'assistant' AND action_type != '')         AS tool_turns
    FROM services_app_message
    WHERE created_at > NOW() - INTERVAL '30 days'
    GROUP BY conversation_id
    HAVING COUNT(*) FILTER (WHERE role = 'assistant') > 0
)
SELECT
    COUNT(*)                                                              AS total_convs,
    COUNT(*) FILTER (WHERE tool_turns = 0)                                AS pure_text_convs,
    ROUND(100.0 * COUNT(*) FILTER (WHERE tool_turns = 0)
          / NULLIF(COUNT(*), 0), 1)                                       AS pct_pure_text,
    ROUND(AVG(assistant_turns), 1)                                        AS avg_turns_per_conv,
    ROUND(AVG(plain_text_turns::numeric / NULLIF(assistant_turns, 0)), 2) AS avg_text_share
FROM per_conv;
```

`pct_pure_text` > 30 % means the AI is acting as a chat-bot, not a
booking concierge. **Fix grounding before investing in memory.**

```sql
-- 4B. Conversation outcomes (Phase 1 Learning Roadmap field)
SELECT
    outcome,
    COUNT(*) AS convs,
    ROUND(AVG(EXTRACT(EPOCH FROM (last_message_at - created_at)) / 60), 1) AS avg_minutes
FROM services_app_conversation
WHERE created_at > NOW() - INTERVAL '30 days'
  AND outcome IS NOT NULL
  AND outcome != ''
GROUP BY outcome
ORDER BY convs DESC;
```

`outcome=success` matches the funnel-final bookings; `abandoned` and
`redirected` quantify the leak. If `success` > `abandoned` we have a
working flow.

---

## Section 5 — Memory-relevant signals (what fields would be load-bearing)

**Question:** Among UserPersonalContext fields we could build, which
ones are clients implicitly signalling through their bookings?

`BotUser.context` is the bot's pre-existing JSON bag — Phase 2.4
populates `services_viewed`, `bookings_count`, `last_followup_sent_at`,
etc. Production data here is the cheapest possible signal.

```sql
-- 5A. Distinct service categories per user
SELECT
    bot_user_id,
    COUNT(DISTINCT category_name) AS distinct_categories,
    COUNT(*) AS bookings
FROM services_app_bookingrequest
WHERE source = 'bot_max'
  AND is_processed = TRUE
  AND created_at > NOW() - INTERVAL '90 days'
GROUP BY bot_user_id
HAVING COUNT(*) >= 2
ORDER BY bookings DESC
LIMIT 30;
```

If users mostly stay in 1 category → `preferred_categories` is
high-value. If they bounce across categories → it's not.

```sql
-- 5B. Time-of-day pattern — does each user have a preferred slot?
-- Approximation: extract hour from BookingRequest.created_at (the
-- moment they BOOKED, not the slot they booked into — that's a TODO
-- if BookingRequest grows a `scheduled_at` field).
SELECT
    bot_user_id,
    EXTRACT(HOUR FROM created_at) AS booking_hour_utc,
    COUNT(*) AS occurrences
FROM services_app_bookingrequest
WHERE source = 'bot_max'
  AND is_processed = TRUE
  AND created_at > NOW() - INTERVAL '90 days'
GROUP BY bot_user_id, EXTRACT(HOUR FROM created_at)
HAVING COUNT(*) >= 2
ORDER BY bot_user_id, occurrences DESC;
```

Pattern hint, not a verdict. If many users have a single dominant
hour bucket → `preferred_time_slots` matters. If hours scatter →
saved time-slot preference is noise.

```sql
-- 5C. context bag fill rate (how much do users let the bot learn
-- about them implicitly?)
SELECT
    COUNT(*)                                                              AS total_users,
    COUNT(*) FILTER (WHERE context IS NOT NULL AND context != '{}')       AS with_any_context,
    ROUND(AVG(JSONB_OBJECT_KEYS_LENGTH(context)), 1)                      AS avg_context_keys
FROM services_app_botuser
WHERE last_seen > NOW() - INTERVAL '30 days';

-- Helper if your Postgres lacks JSONB_OBJECT_KEYS_LENGTH:
-- SELECT AVG(jsonb_array_length(jsonb_object_keys(context))) ...
-- Or:
-- SELECT ROUND(AVG((SELECT COUNT(*) FROM jsonb_object_keys(context))), 1) ...
```

Replace the helper line if the function isn't available — the count
of keys per user is what matters.

---

## Section 6 — Cost / scaling sanity check

```sql
-- 6A. Token spend trajectory (cost forecasting)
SELECT
    DATE_TRUNC('day', created_at)         AS day,
    SUM(tokens_in)                        AS in_tokens,
    SUM(tokens_out)                       AS out_tokens,
    ROUND((SUM(tokens_in) * 0.15
          + SUM(tokens_out) * 0.60) / 1000.0, 4) AS usd_estimate
FROM services_app_message
WHERE role = 'assistant'
  AND created_at > NOW() - INTERVAL '30 days'
GROUP BY day
ORDER BY day;
```

`gpt-4o-mini` pricing as of 2026-04 used above ($0.15 per 1M in,
$0.60 per 1M out). If you're paying < $5/day at current scale, the
extra prompt budget for a UserPersonalContext block is irrelevant.
If > $30/day, every extra token in the system prompt costs.

---

## How to use this on Decision Day

1. Run all six sections, save CSV outputs into `outputs/` here.
2. Fill in the verdict table:

   | Metric | Value | Threshold | Verdict |
   |---|---|---|---|
   | `pct_masters_to_confirm` (Section 2A) | __ % | ≥ 25 / 10–25 / < 10 | __ |
   | `loyalty_pct` (Section 3A) | __ % | ≥ 30 / 15–30 / < 15 | __ |
   | `pct_pure_text` (Section 4A) | __ % | < 30 / ≥ 30 | __ |

3. Decision rule:

   - **Build full UserPersonalContext** if (Section 2A ≥ 25 %) AND
     (Section 3A ≥ 30 %) AND (Section 4A < 30 %).
   - **Ship DRF-230 wire-up only** if borderline on any one signal.
   - **Don't build memory yet — fix grounding** if Section 4A ≥ 30 %
     OR Section 2A < 10 %.

4. Bring the qualitative interview list from Section 3B to the
   customer-interview side of validation (DRF-232 part 2).

## Out-of-scope follow-ups

- **Cohort cross-check (DRF-234):** the bot audience skews wellness;
  the same SQL run on Ayla pilot users (Anna ICP) post-pilot will
  validate transferability.
- **Slot booking-time vs requested-time:** Section 5B is approximate
  because `BookingRequest` doesn't carry the YClients-side scheduled
  datetime; if memory becomes Phase 6, add `scheduled_at` and rerun.
- **Personal context "cold start":** these queries measure existing
  loyalty without memory; the *uplift* from memory is an A/B that
  only Ayla pilot can answer (DRF-235).

## References

- `docs/BOT_CODE_AUDIT_2026-04.md` §1.6 (telemetry already in DB),
  §6 (raw SQL templates that this pack expands)
- `docs/HYPOTHESIS_VALIDATION_PLAN_2026-04.md` H3
- `docs/PRODUCT_AUDIT_2026-04.md` §1.9 (UserPersonalContext architecture
  with three sensitivity zones — what we'd build if memory pays off)
- Bot DB schema: `mysite/services_app/models.py`
  - `BotUser` :1099 — context JSON bag, `last_seen`, `bookings_count`
  - `BookingRequest` :1258 — `source='bot_max'`, `is_processed`,
    `master_name`, `category_name`
  - `Conversation` :1609 — `outcome` enum (success/abandoned/...)
  - `Message` :1738 — `role`, `action_type`, `tokens_in/out`,
    `latency_ms`
