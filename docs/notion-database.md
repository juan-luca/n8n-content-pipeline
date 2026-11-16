# Notion databases

The workflows use two databases. Share both with your integration, and put their IDs in `.env`. Property names
must match.

## Content tracker (`NOTION_DATABASE_ID`)

One page per content request. The pipeline writes it; the daily digest reads it.

| Property | Type | Written when |
|---|---|---|
| Name | Title | created: `client · platform · content type` |
| Request ID | Text | created |
| Client | Text | created |
| Platform | Select | created (instagram, tiktok, facebook, linkedin, x) |
| Status | Select | every step: Drafting, In review, Scheduled, Rejected, Needs attention, Failed |
| Priority | Select | created: Urgent if it publishes within `URGENT_WITHIN_DAYS` days, otherwise Normal |
| Publish date | Date | created |
| Requested by | Text | created |
| Revision | Number | created (0) and on each review round |
| Review requested | Date | in review: when the draft was sent to Slack (the digest counts waiting time from it) |
| Caption | Text | in review: caption + hashtags, as sent to the reviewer |
| Brand checks | Text | in review |
| Post ID | Text | scheduled |
| Approved by | Text | scheduled |
| Notes | Text | rejected, needs attention or failed: the reason |

A board view grouped by Status gives the team the same picture as the local mock at `/board`.

## Clients (`NOTION_CLIENTS_DATABASE_ID`)

One page per client. Account managers keep it up to date; the pipeline only reads it. A request whose client has
no page (the match is on the exact name) is drafted from the request alone, and the reviewer is told so.

| Property | Type | Used for |
|---|---|---|
| Name | Title | matching the request's client |
| Brand voice | Text | sent to the model with every draft |
| Default tone | Select | when the request doesn't set a tone |
| Words to avoid | Text | comma-separated; added to the request's own list and checked on every draft |
| Hashtags | Text | comma-separated; the model must include them, and the brand checks verify it |
| Sign-off | Text | closing line of every caption |
| Account manager | Text | who is asked to step in when a request needs a person |
