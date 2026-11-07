# Setting up timelens: instructions for an AI agent

The person you're helping is setting up **timelens**, a time tracker for their Mac. Once a minute it shows the
front window to a small AI model, together with a short paragraph about the person and a one-line description of
each of their projects, so the model can tell their work apart. Your job is to write that paragraph and those
descriptions for them. A setup page is open in their browser and fills itself in when you save the file.

The person may have never used you for anything like this, so keep them oriented.

## 1. Say what you're doing (first message, before any tool use)

Two or three plain sentences, for example:

> I'll help set up timelens, the time tracker you're installing. I'm going to look through your project folders on
> this Mac and write a short description of each one, so timelens can tell your projects apart. Nothing leaves your
> Mac, and you'll review everything on the setup page before it's saved.

## 2. Look around

- Project folders: check `~/code`, `~/Developer`, `~/Projects`, `~/src` and `~/Documents/GitHub`, plus any folder
  the person names. Keep the ones that exist.
- For each subfolder, read the first lines of its README, its git remote (`git -C <folder> remote get-url origin`)
  and its package name if there is one. Don't open other files.
- If you already know this person from earlier sessions, use what you know.

## 3. Ask, briefly (one message, only if needed)

Ask at most three questions, numbered, each with your best guess so they can just say "yes":

> 1. `acme-site` and `acme-api` look like work for a client called Acme. Right?
> 2. `apps` has no README or remote. Should timelens ignore it?
> 3. Besides coding, is most of your laptop time in the browser (email, docs, CRM)?

If they skip a question, go with your guess and say so in your last message. Don't ask a second round.

## 4. Write the file

Save one JSON file to the path given in the prompt (normally `~/Library/Application Support/timelens/agent-context.json`):

```json
{
  "about": "Two to four sentences about the person's work, written in the third person.",
  "dirs": ["~/code"],
  "known": {
    "acme-site": "Client: Acme's marketing website.",
    "pebble-api": "Personal: the API behind a side project called Pebble."
  },
  "ignore": ["apps"]
}
```

- `known`: one entry per folder you're confident about. Start with `Client:`, `Work:` or `Personal:` and keep it
  under 15 words. Name the client or employer when you know it.
- `ignore`: folders the person wants left out, or that are clearly not projects (old copies, scratch folders).
- `about`: what helps tell their activities apart: their role, who they work for, which tools they use for what,
  and habits that would confuse someone watching their screen (for example, using a coding agent to write
  documents). Leave out anything that doesn't help with that, such as where they live, their health, family,
  finances or the names of other people.

Never include secrets, keys, email addresses or phone numbers. If you can't write files, print the JSON in one
code block and ask the person to paste it into the setup page under "Agent can't write files?".

## 5. Finish (one short message)

> Done. Go back to the timelens setup page: your projects are filled in there for you to check.

Add one line for any guess you made. Nothing else.
