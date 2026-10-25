# Privacy Policy

**GSIV Blue Tracker** · Last updated: 2026-10-25

GSIV Blue Tracker (the "Bot") is a read-only Discord bot. It archives posts by GemStone IV game staff in the GemStone IV Discord server and publishes them in a searchable web archive (the "Archive"). It is operated by Nisugi (the "Operator"). It is not affiliated with Simutronics or Discord.

## What we collect

The Bot only reads channels that the server's administrators have granted it. Within those channels it stores:

- **Posts by tracked authors.** Tracked authors are members holding staff roles chosen by the server's administrators, or members listed individually in the Bot's configuration. For each post: message content, attachment names and links, embeds, the channel, timestamps, whether it is pinned, and the ID of the message it replies to.
- **The message a tracked post replies to.** When a staff member replies to a message, that one message is stored with the same details, so the reply makes sense in context. This can include a message by any member.
- **Author details for stored messages:** Discord user ID, username, display name, and avatar link. The same details are stored for anyone @mentioned in a stored message, so the mention can be shown by name.
- **Server role names and colors,** so role mentions can be shown by name.
- **Edits of stored messages:** the earlier content and the time of the edit.
- **Channel details:** IDs, names, topics, and the channel and category structure.

The Bot does **not** store any other messages. It doesn't read direct messages, use the server member list, track presence or activity, or collect voice data. It does not use cookies or analytics. The Archive's pages load avatars, attachments and custom emoji images from Discord's servers, as Discord itself does. Our web server may keep standard access logs (IP address, time, page requested) for security and troubleshooting.

## How we use it

The data is used only to run the Archive: a public website where people can search and read staff posts, see what each post replied to, and follow a link back to the original message on Discord. It is not sold, shared with third parties, used for advertising, or used to train AI models.

## Who can see it

The Archive is public. Posts in channels the Operator has marked hidden are stored but not shown.

**Deleted messages are deleted.** When a stored message is deleted on Discord, the Bot removes it from the Archive, together with its edit history, attachments and embeds.

## Storage and retention

Data is stored in a PostgreSQL database on a server operated by the Operator. It is kept for as long as the Archive runs, unless the message is deleted on Discord or removed under the next section. Backups are kept for a limited time and older ones are overwritten, so removed data disappears from backups as they rotate.

## Your choices

- **Removal requests.** If you want a message of yours removed from the Archive (for example, a message a staff member replied to), contact us with a link to the message. We will remove it and its history within 30 days.
- **Server administrators** control which channels the Bot can see, and can remove the Bot at any time. When the Bot is removed, it stops collecting immediately.

## Contact

Open an issue at <https://github.com/Nisugi/gsiv-blue-tracker/issues>, or contact Nisugi on the GemStone IV Discord server.

## Changes

We will post any changes on this page and update the date above.
