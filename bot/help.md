# Kimbaku Scheduler Bot

## Commands
- `/upcoming` — list upcoming events (free, no AI)
- `/summary` — event counts and payment totals (free, no AI)
- `/gcal` — fetch upcoming Google Calendar events for the next 30 days (via Claude Haiku)
- `/reset` — clear conversation context
- `/help` — this message


## Typing a Question
Simple queries run locally for free — no AI needed:
> "upcoming", "summary", "teachers", "cities", "timezones"

Complex queries (adding events, calendar sync, trip planning) will ask you to pick a model.

## Model Tags
Add a tag to the start of any message to skip the model picker:

| Tag | Model | Cost |
|-----|-------|------|
| `!ollama` or `!o` or `!local` | Local Ollama | Free |
| `!claude` or `!c` or `!haiku` | Claude Haiku | ~$0.001 |
| `!sonnet` or `!s` | Claude Sonnet | ~$0.01 |

**Examples:**
```
!o what events do I have in Berlin?
!claude add event: Rope Lab London, July 10-12, teacher Esinem
!sonnet plan my travel for the next 3 events including flights
```

## Adding Events
**From a URL:**
```
!claude ingest https://example.com/event-page
```
**From text** (paste flyer, email, message):
```
!claude Tying with Style, March 11 7pm-10pm, BightBound Studio, $40
```
**Manually via CLI:**
```bash
python scripts/events.py add --name "Event" --city Berlin --start 2026-09-01 --teacher "Name"
```

## Event Status Flow
`discovered` → `interested` → `registered` → `attended`
`discovered` → `skipped` or `cancelled`

## Payments
```
!claude add deposit of £200 for event 3
!claude payment summary
```
## Local intent (free) — these phrases never hit any API:    
- "upcoming", "next event", "coming up" → runs events.py list --upcoming
- "summary", "spending", "payments" → runs events.py summary
- "teachers" → runs events.py teachers
- "cities" → runs events.py cities
- "timezone" → runs events.py timezones

5-min cache — /upcoming and /summary results are cached; repeated calls within 5 minutes return instantly with no subprocess

max_turns=15 — caps Claude agent loops so a runaway tool-use chain can't burn money

Per-model sessions — Haiku and Sonnet each keep their own conversation context; switching models doesn't bleed context between them

Model selection — for anything complex with no tag, you get a 3-button keyboard: Ollama / Haiku / Sonnet

Tags — prefix any message to skip the picker:
!o add this event: Rope Lab Berlin Aug 3
!sonnet plan my travel for the next 3 events including flights and hotels
!claude what do I owe for the London workshop
