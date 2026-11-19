# Sing All step 5: emails and the job link — task specs (draft)

2026-11-19, draft. Nothing here is built. The differences from the HLD and the questions under "For Alan" are open.

Design: `docs/specs/sing-all-process-backend-hld.md` §1 (emails, job link), §3 (the email column), §10, §11, §16 row 5, §17.3 (Brevo, the outbox), §17.8, on branch `docs/sing-all-process-hld`. Steps 1, 2, 3 and 7 are built. Step 4 (mixes and zip) is specced in `sing-all-mixes-step4-spec.md`; step 5 doesn't need it, but its email mentions the zip once step 4 is in.

Step 5 tells the owner when a Sing All ends, by email, with a link that opens the Song on that job's card:

- **the email**: sent once when a job ends completed, partial or failed, from Brevo's transactional API, in English;
- **the job link** `https://sightsinger.app/jobs/{jobId}`: owner only; it signs in if needed and opens the Song showing that job's card;
- **the card** says an email will arrive, in place of step 3's "Your song will be here when you come back."

Most of HLD §11 is already built by step 2 (Songs: reopening a Song, `/app/song/{id}`, the workspace) and step 3 (the job card restored from the Song's history, the "Sing All ended" history entry the chat model reads). What is left is the `/jobs/{jobId}` route.

The repository is public. Tests, fixtures and commits use made-up titles, part names and lyrics only, no real job or session ids, and no real email addresses (use `example.test`).

## Differences from the HLD

| # | HLD | This spec | Why |
|---|---|---|---|
| 1 | Sent on completed, partial, needs_decision and failed, and on the decision-timeout cancel | Step 5 sends on completed, partial and failed. needs_decision and its timeout don't exist until step 6, which adds their two emails | Step 3 has no needs_decision: it ends with a refund. The outbox and template take a new status with no other change |
| 2 | Not covered: a job the owner stopped | No email when the owner stops a job (`cancelled`) | They pressed Stop and saw it end. The card shows the receipt |
| 3 | The email address: not said | The address is taken from the sign-in token at "Sing it" and saved on the job as `notifyEmail` | The job then needs no Auth admin lookup at the end, and the address is the one the user was signed in with when they paid |
| 4 | "Anyone else sees 'This job isn't yours'" | Anyone else, and a job that doesn't exist, both see "This job isn't yours. Ask the owner for a share link." | `GET /sing-all/jobs/{id}` already answers the same 404 for both, so a job id can't be tested for existence |
| 5 | "Chat: the history, with the job summary as the latest assistant message" | Unchanged from step 3: the "Sing All ended" entry stays where it was written; opening the link scrolls to that job's card | Step 3 writes the entry when the job ends, so it is already the latest message unless the user chatted after it. Moving it would rewrite history |
| 6 | Emails sent "from an outbox document" | Yes: `mailOutbox/{jobId}-{status}`, created in the same transaction that ends the job; a sender sends it and marks it sent. The sweeper retries unsent ones | A job's end is written once; a send can fail. The document id makes a second send impossible |

## Data

```text
singAllJobs/{jobId}
  NEW  notifyEmail     str           from the token at "Sing it"
  NEW  emailSentFor    [status]      as the HLD says; set by the sender

mailOutbox/{jobId}-{status}          NEW; clients can't read or write it (no rule in firestore.rules)
  kind         "sing_all_ended"
  uid, jobId, status
  to           the job's notifyEmail
  createdAt, sentAt?, attempts, lastError?
```

## The email

English only, plain and short, from a real reply-to address (Alan's choice, below).

| Status | Subject | First line |
|---|---|---|
| completed | Your song is ready: {title} | Every part is sung. |
| partial | Your song is partly ready: {title} | {done} of {takes} takes were sung. The rest were refunded. |
| failed | We couldn't sing your song: {title} | No take could be sung. Your credits were refunded. |

Then the receipt (reserved, charged, refunded, as the card shows it), then the button **Open your song** → `https://sightsinger.app/jobs/{jobId}` (from `SHARE_BASE_URL`'s host setting, renamed or reused: see "For Alan"). Once step 4 is in: "Your zip is ready to download from the song."

The wording is in `src/backend/messages/en.properties`, one key per line, so it can be reviewed without reading code.

## Who builds what

| Task | Who | Needs |
|---|---|---|
| **E1. The outbox, the Brevo sender and the email** | Codex | — |
| **E2. The job link and the card's line** | Claude | — |
| **E3. Deployment: the Brevo key, sender and reply-to** | Alan | E1 |

E2 is Claude's: it is UI. E1 touches the reconciler's end of a job but no credits code. Parent branch: `feature/sing-all-emails`, cut from `feature/share`. Steps 4 and 5 don't touch the same files except `sing_all.py`'s `finish`, where both add an action; whichever merges second resolves that one place.

## E1. [Codex] The outbox, the Brevo sender and the email

### Rule

```python
# POST /sing-all/jobs: job.notifyEmail = the token's email (absent if the token has none)

# sing_all.finish(status): the actions gain {"kind": "queue_email"} when
#   status in {"completed", "partial", "failed"} and job.notifyEmail
# wind_down (Stop) adds nothing.

queue_email(job):        # in the same transaction as the job's end
    create mailOutbox/{jobId}-{status} if it doesn't exist

send(outbox_doc):        # right after queue_email, and from the sweeper for unsent docs
    if doc.sentAt: return
    render the email from the job (title, status, receipt, link)
    Brevo POST /v3/smtp/email; on 2xx: doc.sentAt = now; job.emailSentFor += [status]
    else: doc.attempts += 1; doc.lastError = the status code and Brevo's error code only
    after 5 attempts: stop retrying and log an error (the §17.8 alert "email send failures")
```

### Change

1. `src/backend/sing_all_mail.py`: `render_ended_email(job, base_url) -> (subject, text, html)`, `queue_email`, `send_outbox(doc_id)`, `send_unsent(limit)`.
2. `sing_all.py`: the `queue_email` action from `finish`; `sing_all_runner.py` runs it and then `send_outbox`.
3. `POST /sing-all/jobs` saves `notifyEmail`.
4. The sweeper calls `send_unsent`.
5. Settings: `BREVO_TRANSACTIONAL_API_KEY_SECRET` (and version), `SING_ALL_EMAIL_FROM`, `SING_ALL_EMAIL_REPLY_TO`, `SING_ALL_EMAILS = off | on` (default `off`; `off` queues nothing). The key is read the way `waitlist._load_brevo_api_key` reads the marketing key.
6. Messages: the subjects, first lines, receipt labels and button text in `en.properties`.

### Done when

- A completed, a partial and a failed job each create one outbox document and one Brevo request (mocked HTTP) with the right subject, first line, receipt and link; `emailSentFor` holds the status.
- A stopped job, a job with no `notifyEmail`, and `SING_ALL_EMAILS=off` send nothing.
- Reconciling the same ended job again sends nothing more.
- A Brevo failure leaves the document unsent with `attempts` and `lastError`; the sweeper's next pass sends it; after 5 failures it stops.
- No email address, job id or session id appears in a log line; the outbox document holds the address and is not readable by clients.

### Checks

The step 3 check command plus `tests/test_sing_all_mail.py`. Baseline first; the known failures stay the same.

### Out of scope

The UI. needs_decision emails (step 6). Any other email (receipts for chat takes, marketing). HTML design beyond a plain, readable layout.

## E2. [Claude] The job link and the card's line

### Rule

```python
# /jobs/:jobId — outside the Song routes; sign-in required (ProtectedRoute, as /app)
r = GET /sing-all/jobs/{jobId}
    404 -> "This job isn't yours. Ask the owner for a share link."  (with a link to /app)
    200 -> navigate to /app/song/{r.sessionId}?job={jobId}, replacing the history entry
# A job whose Song is deleted answers 404 too (step 4, Alan 2026-11-19: a deleted Song can't be opened), so an old email's link shows the same message.
# /app/song/{id}?job={jobId}: the Song opens as today, then scrolls to that job's card and highlights it once.
#   If the card isn't in the history (an older Song), the Song opens as today.
```

- The card's "You can close this page. Your song will be here when you come back." becomes "You can close this page. We'll email you when it's done." while `SING_ALL_EMAILS` is on; the workspace or the job progress says so (`emailOnEnd: true`).
- `firebase.json`: rewrite `/jobs/**` to `/app.html`, before the catch-all, with the `/app/**` cache header.

### Done when

- Playwright, mocked: the owner signed in opens the link and lands on the Song with the job's card in view; signed out, the link goes through sign-in and then lands there; another user sees "This job isn't yours…"; the card's line follows `emailOnEnd`.
- One real-backend run: a job ends, its outbox document exists, and its link opens the Song.

## E3. Deployment (Alan)

Brevo: a transactional API key (or the marketing key, if Brevo allows both on one), a verified sender, and the reply-to address. `SING_ALL_EMAILS=on` in the API's env when ready.

## For Alan

1. **Sender and reply-to:** which address sends, and which receives replies?
2. **Brevo key:** a new transactional key, or the existing marketing key's secret?
3. **Link host:** reuse `SHARE_BASE_URL` (`https://sightsinger.app`) for the job link, or a separate `APP_BASE_URL` setting? One setting for both is simpler.
4. **Stopped jobs (difference 2):** no email when the owner stops. Agreed?
5. **The address (difference 3):** the one signed in at "Sing it". Agreed?
