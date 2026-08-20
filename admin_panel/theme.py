CSS = """
:root {
    --bg: #0a0a0f;
    --bg-card: #14141c;
    --bg-card-hover: #191924;
    --border: #232330;
    --text: #e8e8ec;
    --text-dim: #8b8b96;
    --accent: #6366f1;
    --accent-hover: #7678f5;
    --accent-glow: rgba(99, 102, 241, 0.18);
    --green: #22c55e;
    --amber: #f59e0b;
    --red: #ef4444;
    --radius: 14px;
}

* { box-sizing: border-box; }

body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
    background: var(--bg);
    color: var(--text);
    display: flex;
    min-height: 100vh;
}

/* ---- Sidebar ---- */
.sidebar {
    width: 220px;
    flex-shrink: 0;
    background: var(--bg-card);
    border-right: 1px solid var(--border);
    padding: 24px 16px;
    position: sticky;
    top: 0;
    height: 100vh;
}
.sidebar h1 {
    font-size: 16px;
    font-weight: 700;
    margin: 0 0 28px 8px;
    color: var(--text);
}
.sidebar a {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border-radius: 10px;
    color: var(--text-dim);
    text-decoration: none;
    font-size: 14px;
    font-weight: 500;
    margin-bottom: 4px;
    transition: background 0.15s, color 0.15s;
}
.sidebar a:hover { background: var(--bg-card-hover); color: var(--text); }
.sidebar a.active { background: var(--accent-glow); color: var(--accent-hover); }

/* ---- Content ---- */
.content { flex: 1; padding: 32px 40px; max-width: 1100px; }
.content h2 { font-size: 22px; margin: 0 0 24px 0; }

/* ---- Cards / grid ---- */
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 32px; }
.card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    transition: border-color 0.15s, transform 0.15s;
}
.card:hover { border-color: var(--accent); transform: translateY(-1px); }
.card .label { font-size: 13px; color: var(--text-dim); margin-bottom: 8px; }
.card .value { font-size: 26px; font-weight: 700; letter-spacing: -0.5px; }

/* ---- Forms ---- */
.panel {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    margin-bottom: 24px;
}
input[type=text] {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 11px 14px;
    color: var(--text);
    font-size: 14px;
    margin-bottom: 14px;
}
input[type=text]:focus { outline: none; border-color: var(--accent); }
button, .btn {
    background: var(--accent);
    color: white;
    border: none;
    border-radius: 10px;
    padding: 11px 18px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.15s;
}
button:hover, .btn:hover { background: var(--accent-hover); }
.btn-ghost {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text-dim);
    padding: 6px 12px;
    font-size: 13px;
    border-radius: 8px;
}
.btn-ghost:hover { color: var(--text); border-color: var(--text-dim); background: transparent; }

/* ---- Table ---- */
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; color: var(--text-dim); font-weight: 500; padding: 10px 12px; border-bottom: 1px solid var(--border); }
td { padding: 12px; border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: none; }
code {
    background: var(--bg);
    border: 1px solid var(--border);
    padding: 3px 8px;
    border-radius: 6px;
    font-size: 13px;
    color: var(--accent-hover);
}

/* ---- Banners ---- */
.banner { border-radius: 10px; padding: 12px 16px; margin-bottom: 20px; font-size: 14px; }
.banner-ok { background: rgba(34,197,94,0.12); border: 1px solid rgba(34,197,94,0.3); color: var(--green); }
.banner-err { background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.3); color: var(--red); }

a.plain { color: var(--accent-hover); }
"""


def render_page(active: str, title: str, body: str) -> str:
    nav_items = [
        ("/", "📊", "Дашборд", "dashboard"),
        ("/admin/invite-links", "🔗", "Пригласительные ссылки", "invite-links"),
        ("/admin", "🗂️", "Таблицы", "tables"),
    ]
    nav_html = "\n".join(
        f'<a href="{href}" class="{"active" if key == active else ""}">{icon} {label}</a>'
        for href, icon, label, key in nav_items
    )
    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{title}</title>
        <style>{CSS}</style>
    </head>
    <body>
        <nav class="sidebar">
            <h1>Подписки</h1>
            {nav_html}
        </nav>
        <main class="content">
            {body}
        </main>
    </body>
    </html>
    """
