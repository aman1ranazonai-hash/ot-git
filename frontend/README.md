# 🖥️ Frontend

This folder documents the **frontend** (browser-facing) layer of the Kronos Trading Bot.

> **Note:** The actual source files live in `../dashboard/` to keep Flask's template resolution working without changes. This README serves as a navigation guide.

---

## What counts as "frontend"

| Path (from project root) | Description |
|---|---|
| `dashboard/templates/index.html` | Main dashboard UI (HTML) |
| `dashboard/templates/settings.html` | Settings page (HTML) |
| `dashboard/static/css/` | Stylesheet(s) |
| `dashboard/static/js/app.js` | Dashboard JavaScript (polling, charts, controls) |

---

## How it works

- **Framework:** Plain HTML + Vanilla CSS + Vanilla JS (no build step, no npm)
- **Served by:** Flask (`dashboard/app.py`) via `render_template()` and `/static/` route
- **Data:** All live data fetched via JSON APIs (`/api/status`, `/api/trades`, `/api/logs`, `/api/markets`)
- **Refresh intervals:** Stats every 5 s · Trades every 10 s · Logs every 2 s

---

## Key frontend files to edit

```
dashboard/
├── templates/
│   ├── index.html        ← Main dashboard page
│   └── settings.html     ← Settings page
└── static/
    ├── css/              ← Stylesheets
    └── js/
        └── app.js        ← All dashboard JS logic
```

---

## Adding a new page

1. Create `dashboard/templates/<page>.html`
2. Add a Flask route in `dashboard/app.py`:
   ```python
   @app.route("/page")
   def page():
       return render_template("page.html")
   ```
3. Link to it from `index.html` or `settings.html`
