# granola-to-obsidian

Automatically syncs [Granola](https://www.granola.ai) meeting notes into an [Obsidian](https://obsidian.md) vault every day at 9pm via macOS launchd.

## What it does

- **Class meetings** → appended as sessions to `Classes/[ClassName].md`, enriched with Canvas LMS session labels and upcoming due dates
- **All other meetings** → individual files in `Meetings/[category]/YYYY-MM-DD - Title.md`, auto-categorized by topic
- **Smart updates** — if you edit notes in Granola later, only the AI block refreshes; your personal `### My Notes` section is always preserved
- **No duplicates** — each meeting is tracked by `granola_id`; re-runs are safe

## Folder taxonomy

Non-class meetings are automatically sorted into:

| Folder | What goes there |
|--------|----------------|
| `Meetings/Courses/[Course Name]/` | Past course sessions detected by title |
| `Meetings/Real Estate/` | Real estate networking, panels, strategy |
| `Meetings/Speakers & Events/` | Fireside chats, talks, investor summits |
| `Meetings/Career/` | Advising, planning, 1x1s |
| `Meetings/Personal/` | Social events, personal meetings |
| `Meetings/Work/` | Work meetings, client calls, projects |
| `Meetings/Other/` | Fallback for uncategorized |

Add keyword rules to `_TITLE_CATEGORY_RULES` in the script to extend this.

## Setup

### 1. Dependencies

```bash
pip3 install httpx
```

### 2. Granola auth

The script reads your Granola token automatically from:
```
~/Library/Application Support/Granola/supabase.json
```
No configuration needed — just be logged into Granola on your Mac.

### 3. Canvas API token

Create a `.env` file next to the script (or set the env var):
```
CANVAS_API_TOKEN=your_token_here
```
Get your token at `https://[your-canvas].instructure.com/profile/settings` → Approved Integrations → New Access Token.

### 4. Configure your vault paths

Edit the paths near the top of `granola_to_obsidian.py`:
```python
OBSIDIAN_CLASSES  = Path.home() / "Documents/Obsidian Vault/Classes"
OBSIDIAN_MEETINGS = Path.home() / "Documents/Obsidian Vault/Meetings"
CANVAS_BASE       = "https://canvas.YOUR-SCHOOL.edu/api/v1"
```

### 5. Configure class matching

Edit `CLASS_PATTERNS` to match your course titles as they appear in Granola:
```python
CLASS_PATTERNS = [
    ("hrmgt",    "HRMGT350"),   # matches any title containing "hrmgt"
    ("finance 347", "Finance347"),
    # add your courses here
]
```

The matched name must correspond to a `.md` file in your `Classes/` folder.

### 6. Schedule with launchd (macOS)

Edit `launchd/com.jacob.granola-obsidian.plist` — update the script path and username — then install:

```bash
cp launchd/com.jacob.granola-obsidian.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.jacob.granola-obsidian.plist
```

Check logs:
```bash
tail -f ~/Library/Logs/granola-obsidian.log
```

Run manually:
```bash
python3 granola_to_obsidian.py
```

## Note format

### Class files (`Classes/ClassName.md`)

```markdown
## 2026-04-16 — Session Title
<!-- granola_id: abc123 updated_at: 2026-04-16T21:00:00 -->
*Meeting title from Granola*
**Due:** Assignment name (Apr 22)

### Topic from Granola AI notes
- bullet
- bullet

---
### My Notes

*(your personal notes — never overwritten)*

---
```

### Meeting files (`Meetings/[category]/YYYY-MM-DD - Title.md`)

```markdown
---
granola_id: abc123
updated_at: 2026-04-16T21:00:00
title: "Meeting Title"
created_at: 2026-04-16
tags:
  - person/first-last
  - source/career
  - topic/career
---

# Meeting Title
**With:** Person Name

## Notes
[AI-generated notes from Granola]

---
### My Notes

*(your personal notes — never overwritten)*

---
```

## Obsidian setup tips

- Set `Home.md` as your startup note: `Settings → Options → Startup → Open specific note`
- Use `MOCs/` folder for Maps of Content linking across categories
- Tags `topic/real-estate`, `topic/career`, etc. make the tag pane useful for cross-folder filtering

## Credits

Techniques adapted from [dannymcc/Granola-to-Obsidian](https://github.com/dannymcc/Granola-to-Obsidian).
