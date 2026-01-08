# Browser Automation Debug Notes

## Task
Configure Supabase SMTP settings via browser automation (MCP browser-tools)

## What Happened

### Successfully navigated to:
1. `https://supabase.com/dashboard` - Dashboard loaded, showed project list
2. Found and clicked on `hdr-boost` project
3. Navigated to Authentication > Email > SMTP Settings tab
4. URL: `https://supabase.com/dashboard/project/dlxtgcaypaqklzldigiy/auth/smtp`

### The Problem
The "Enable custom SMTP" toggle switch was **not detectable** in the accessibility tree.

**Screenshot showed:**
- Toggle switch visible on the right side of "Enable custom SMTP" row
- "Save changes" button visible
- All text elements visible

**Accessibility tree showed:**
- Sidebar menu items (Project Overview, Table Editor, etc.)
- Navigation elements
- Text content like "Enable custom SMTP", "Emails will be sent..."
- BUT: No `switch`, `toggle`, `checkbox`, or clickable button element for the toggle

### Searches attempted:
- `browser_find("Enable custom SMTP")` - Found text span, no clickable ancestor for toggle
- `browser_find("switch")` - 0 matches
- `browser_find("Save changes")` - Found span, no button ancestor detected

### Clicks attempted:
1. Clicked on the "Enable custom SMTP" text span (backend_node_id=3441) - No effect
2. Clicked on "Save changes" span (backend_node_id=3422) - No effect

## Hypothesis
Supabase uses a custom toggle component (likely Radix UI or similar) that:
1. Doesn't expose proper ARIA roles to the accessibility tree
2. The actual clickable element might be a sibling/child that's not being picked up
3. Could be a `<button>` with `role="switch"` that's not rendering in the a11y tree

## Potential Fixes

### 1. Use coordinate-based clicking
The toggle was visually at approximately:
- x=703-715 (right side of the card)
- y=268 (same row as "Enable custom SMTP")

Could try: `browser_click` with raw x,y coordinates if the tool supports it

### 2. JavaScript injection
If MCP browser-tools supports `evaluate`, could try:
```javascript
document.querySelector('[role="switch"]')?.click()
// or
document.querySelector('button[aria-checked]')?.click()
```

### 3. Keyboard navigation
Tab through the page to focus the toggle, then press Space/Enter

### 4. Check Supabase's actual DOM
Inspect the real DOM to see what element the toggle actually is:
- Is it a `<button role="switch">`?
- Is it a custom `<div>` with click handler?
- Does it have a unique `data-*` attribute?

## SMTP Values to Configure (for when it works)
| Field | Value |
|-------|-------|
| Host | `smtp.resend.com` |
| Port | `465` |
| Username | `resend` |
| Password | `re_MUrwkKpp_HgZVSRfMQUn4XFLEY3kcZQRT` |
| Sender email | `noreply@<domain>` |
| Sender name | `HDR Boost` |

## Related Screenshots
Screenshots were saved to temp folders during the session but are ephemeral.
