# Filling in timelens context (instructions for a coding agent)

You're helping someone set up **timelens**, a time tracker on their Mac. Every minute it shows their front window to a
small decision model, along with a short paragraph about them and a one-line description of each of their projects.
Your job is to write that paragraph and those descriptions, so they don't have to.

The setup page is open in their browser and checks for your output every few seconds.

## What to produce

Write one JSON file to the path given in the prompt (normally `~/Library/Application Support/timelens/agent-context.json`):

```json
{
  "about": "2 to 4 sentences: what the person does, who they work for, which tools and apps they use for what.",
  "dirs": ["~/code"],
  "known": {
    "acme-site": "Client: Acme's marketing website.",
    "pebble-api": "Personal: an API for a side project called Pebble.",
    "dotfiles": "Personal: machine configuration."
  }
}
```

- `dirs`: folders whose subfolders are the person's projects. Check `~/code`, `~/Developer`, `~/Projects`,
  `~/src` and `~/Documents/GitHub`, and any folder the person names. Keep only folders that exist.
- `known`: one entry per subfolder of those folders, keyed by the exact folder name. Start each description with
  `Client:`, `Work:` or `Personal:`, then say what it is in under 15 words. Name the client or employer when you know it.
- `about`: plain sentences in the third person, as if introducing them to a colleague. Include habits that would
  confuse someone watching their screen, such as using a coding agent to write documents, or one browser profile per client.

## How to find out

1. List the project folders. For each one, read the README's first lines, the git remote URL
   (`git -C <folder> remote get-url origin`) and the package name if there is one.
2. If you have memory or notes about this person from earlier sessions, use them.
3. Ask the person, in one short message, about anything you can't tell: which projects are for clients, the client
   names, and what they mostly use their laptop for. Wait for the answer before writing the file.

## Rules

- Stay on this Mac. Don't send anything anywhere, and don't open other files than the ones above.
- Never put secrets, keys, email addresses, phone numbers or other people's personal details in the file.
- Write the file only once, when it's complete. Then tell the person to look at the setup page: it fills itself in,
  and they can edit everything before saving.
- If you can't write files (a chat-only assistant, for example), print the JSON in a single code block instead and
  tell the person to paste it into the setup page under "Agent can't write files?".
