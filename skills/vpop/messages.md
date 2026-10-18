# Message tables

vPop keeps the synced messages in SQLite, one set of tables per source. `run_sql` reads them
(one `SELECT` or `WITH` statement per call). Every `timestamp` is local time as text,
`YYYY-MM-DD HH:MM:SS`: it sorts and compares as a string and works with `date()` and
`strftime()`. Every `direction` is `incoming` or `outgoing`, and outgoing messages were
written by the user ("me").

## Table `messages`

One row per text message (SMS, MMS and RCS from an Android phone).

- `timestamp`: when it was sent or received.
- `direction`: `incoming` or `outgoing`.
- `was_sent`: 0 for an outgoing message that never went out (failed or still queued).
- `thread_key`: the conversation, and the column to group by for anything "per person". A
  single E.164 number (`+1` then ten digits) for a 1:1 thread, a sorted comma-separated
  list of numbers for a group, or an `...@rcs.google.com` id for an RCS group. The tools
  name it `text:<thread_key>`.
- `sender`: the number of whoever sent an incoming message; always empty for outgoing ones,
  so never group outgoing messages by sender.
- `contact_name`: the contact name(s) the phone showed for the thread. It can be empty or
  `(Unknown)`, and a group lists several names.
- `body`: the message text. Reactions look like `❤️ to “...”` or `Liked “...”`.
- `attachments`: content types of attached files (e.g. `image/jpeg`), comma-separated.
- `epoch_ms`, `id`, `rcs_message_id`, `from_mms`: bookkeeping, rarely useful.

## Table `threads`

One row per text conversation.

- `thread_key`: as in `messages`.
- `label`: its latest contact name.
- `message_count`, `first`, `last`: how many messages, and the first and last timestamps.

## Table `whatsapp_messages`

One row per WhatsApp message.

- `timestamp`: when it was sent or received.
- `direction`: `incoming` or `outgoing`.
- `chat_key`: the chat, and the column to group by for anything "per person". An E.164
  number (`+` then digits) for a 1:1 chat, the same key as that person's text
  `thread_key`, or `...@g.us` for a group. A `...@lid` key is a person WhatsApp hides the
  number of. The tools name it `whatsapp:<chat_key>`.
- `sender`: who sent an incoming message (a number, or `...@lid`); empty for outgoing ones.
- `body`: the message text, or a media caption.
- `attachments`: the content type of attached media (e.g. `image/jpeg`).
- `epoch_ms`, `id`, `key_id`: bookkeeping, rarely useful.

## Table `whatsapp_chats`

One row per WhatsApp chat.

- `chat_key`: as in `whatsapp_messages`.
- `label`: the group's subject, or the person's name from their text thread.
- `subject`: the group's subject; empty for a 1:1 chat.
- `is_group`: 1 for a group.
- `message_count`, `first`, `last`: how many messages, and the first and last timestamps.

## Table `imports`

Bookkeeping: which backup files a sync has imported, so the next one skips them.

- `source`: the source that imported it, e.g. `android_messages` or `whatsapp`.
- `name`, `size`, `mtime_ns`: the file.
- `messages`: how many new messages it added.
- `imported_at`: when, as a local timestamp.

## Examples

Who I texted most in 2025:

```sql
SELECT m.thread_key, t.label, COUNT(*) AS n
FROM messages m JOIN threads t USING (thread_key)
WHERE m.timestamp >= '2025-01-01' AND m.timestamp < '2026-01-01'
GROUP BY m.thread_key ORDER BY n DESC LIMIT 10
```

Messages per month in one thread:

```sql
SELECT strftime('%Y-%m', timestamp) AS month, COUNT(*)
FROM messages WHERE thread_key = '+12405551234'
GROUP BY month ORDER BY month
```

Everyone I talk to most, text and WhatsApp together (a 1:1 chat has the same key in both):

```sql
SELECT who, COUNT(*) AS n FROM (
  SELECT thread_key AS who FROM messages WHERE thread_key NOT LIKE '%,%'
  UNION ALL
  SELECT chat_key FROM whatsapp_messages WHERE chat_key LIKE '+%'
) GROUP BY who ORDER BY n DESC LIMIT 10
```
