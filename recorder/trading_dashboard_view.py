"""Dashboard view helpers, HTML assets/sections, and Markdown rendering.

This module intentionally groups the dashboard presentation layer at a middle
level so navigation stays easier than many tiny renderer files.
"""

from __future__ import annotations

import json
from datetime import datetime
from html import escape
from typing import Any

from .trading_dashboard_alerts import _telegram_alert_class, dashboard_alert_conditions


# ---------------------------------------------------------------------------
# HTML primitives
# ---------------------------------------------------------------------------

def _coerce_num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def html_text(value: Any, default: str = "-") -> str:
    if value is None or value == "":
        return escape(default)
    return escape(str(value))


def html_num(value: Any, *, digits: int = 2, default: str = "-") -> str:
    number = _coerce_num(value)
    if number is None:
        return escape(default)
    return escape(f"{number:,.{digits}f}")


def html_badge(text: Any, *, class_name: str = "") -> str:
    extra = f" {escape(class_name)}" if class_name else ""
    return f'<span class="badge{extra}">{html_text(text)}</span>'


def html_table(headers: list[str], rows: list[list[Any]], *, empty_text: str = "No rows") -> str:
    head = "".join(f"<th>{escape(item)}</th>" for item in headers)
    row_count = len(rows)
    if not rows:
        body = f'<tr><td colspan="{len(headers)}" class="muted">{escape(empty_text)}</td></tr>'
    else:
        rendered_rows = []
        for row in rows:
            rendered_rows.append("<tr>" + "".join(f"<td>{html_text(cell)}</td>" for cell in row) + "</tr>")
        body = "\n".join(rendered_rows)
    table_key = escape("|".join(headers)[:120])
    return (
        f'<div class="table-shell" data-dashboard-table="1" data-page-size="12" '
        f'data-row-count="{row_count}" data-table-key="{table_key}">'
        '<div class="table-toolbar">'
        f'<span class="table-count">{row_count} rows</span>'
        '<span class="table-page-label">page 1</span>'
        '<button type="button" class="table-page-prev" aria-label="Previous rows">Prev</button>'
        '<button type="button" class="table-page-next" aria-label="Next rows">Next</button>'
        '</div>'
        f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
        '</div>'
    )


_html_badge = html_badge
_html_num = html_num
_html_table = html_table
_html_text = html_text


# ---------------------------------------------------------------------------
# HTML static assets
# ---------------------------------------------------------------------------

def _trading_dashboard_html_style() -> str:
    return """  <style>
    :root {
      color-scheme: dark;
      --bg: #070a12;
      --panel: #101725;
      --panel2: #151e2f;
      --text: #e9eef8;
      --muted: #91a0b8;
      --line: #253149;
      --green: #28d17c;
      --yellow: #ffd166;
      --red: #ff5c75;
      --blue: #7cb7ff;
      --nav-width: clamp(190px, 12.5vw, 260px);
      --main-pad: clamp(12px, 1.15vw, 26px);
      --grid-gap: clamp(9px, .75vw, 16px);
      --card-pad: clamp(11px, .9vw, 18px);
      --card-radius: clamp(12px, .9vw, 18px);
      --body-font: clamp(12px, .78vw, 15px);
      --small-font: clamp(10px, .68vw, 12px);
      --table-font: clamp(10px, .70vw, 13px);
      --h2-font: clamp(20px, 1.55vw, 30px);
      --h3-font: clamp(14px, .92vw, 18px);
      --metric-font: clamp(20px, 1.55vw, 28px);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: radial-gradient(circle at top left, #17233a 0, var(--bg) 36rem);
      color: var(--text);
      font-family: Segoe UI, Inter, Arial, sans-serif;
      font-size: var(--body-font);
      line-height: 1.45;
    }
    body.density-compact {
      --nav-width: 178px;
      --main-pad: 10px;
      --grid-gap: 8px;
      --card-pad: 10px;
      --card-radius: 12px;
      --body-font: 12px;
      --small-font: 10px;
      --table-font: 10px;
      --h2-font: 20px;
      --h3-font: 14px;
      --metric-font: 20px;
    }
    body.density-wide {
      --nav-width: clamp(240px, 12vw, 300px);
      --main-pad: clamp(20px, 1vw, 32px);
      --grid-gap: clamp(14px, .75vw, 20px);
      --card-pad: clamp(16px, .85vw, 22px);
      --body-font: clamp(13px, .72vw, 16px);
      --table-font: clamp(11px, .62vw, 13px);
      --h2-font: clamp(26px, 1.35vw, 34px);
      --metric-font: clamp(24px, 1.35vw, 32px);
    }
    .layout { display: grid; grid-template-columns: var(--nav-width) minmax(0, 1fr); min-height: 100vh; }
    nav {
      position: sticky;
      top: 0;
      height: 100vh;
      padding: var(--main-pad) clamp(10px, .8vw, 16px);
      background: rgba(7, 10, 18, .86);
      border-right: 1px solid var(--line);
      overflow: auto;
    }
    nav h1 { font-size: var(--h3-font); margin: 0 0 14px; }
    nav a {
      display: block;
      color: var(--muted);
      text-decoration: none;
      padding: clamp(6px, .45vw, 8px) clamp(7px, .55vw, 10px);
      border-radius: 10px;
      margin: 2px 0;
    }
    nav a:hover { background: var(--panel2); color: var(--text); }
    main { padding: var(--main-pad); max-width: min(2300px, 100vw); width: 100%; min-width: 0; }
    .topbar {
      display: flex;
      align-items: stretch;
      justify-content: space-between;
      gap: var(--grid-gap);
      margin-bottom: var(--grid-gap);
      min-height: clamp(78px, 5.2vw, 96px);
    }
    .title {
      min-width: 0;
      display: flex;
      flex-direction: column;
      justify-content: center;
    }
    .title h2 { margin: 0; font-size: var(--h2-font); }
    .title p { margin: 5px 0 0; color: var(--muted); }
    .lamp {
      flex: 0 0 clamp(360px, 38vw, 620px);
      min-width: 0;
      height: clamp(78px, 5.2vw, 96px);
      padding: clamp(9px, .65vw, 14px);
      border-radius: 18px;
      background: var(--panel);
      border: 1px solid var(--line);
      text-align: center;
      display: flex;
      flex-direction: column;
      justify-content: center;
      overflow: hidden;
    }
    .lamp-status {
      flex: 0 0 auto;
      line-height: 1.2;
    }
    .lamp-reasons {
      flex: 1 1 auto;
      min-height: 0;
      max-height: 42px;
      overflow: auto;
      padding-top: 3px;
      scrollbar-width: thin;
    }
    .lamp .dot {
      width: 18px;
      height: 18px;
      display: inline-block;
      border-radius: 999px;
      margin-right: 8px;
      vertical-align: -3px;
      background: var(--muted);
      box-shadow: 0 0 22px currentColor;
    }
    body.status-green .dot { background: var(--green); color: var(--green); }
    body.status-yellow .dot { background: var(--yellow); color: var(--yellow); }
    body.status-red .dot { background: var(--red); color: var(--red); }
    .grid { display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: var(--grid-gap); }
    .card {
      background: rgba(16, 23, 37, .92);
      border: 1px solid var(--line);
      border-radius: var(--card-radius);
      padding: var(--card-pad);
      box-shadow: 0 18px 50px rgba(0,0,0,.22);
      min-width: 0;
      overflow: hidden;
    }
    .span-3 { grid-column: span 3; }
    .span-4 { grid-column: span 4; }
    .span-6 { grid-column: span 6; }
    .span-8 { grid-column: span 8; }
    .span-12 { grid-column: span 12; }
    h3 { margin: 0 0 10px; font-size: var(--h3-font); }
    .metric { font-size: var(--metric-font); font-weight: 700; margin: 3px 0; }
    .muted { color: var(--muted); }
    .badge {
      display: inline-block;
      margin: 3px 5px 3px 0;
      padding: clamp(3px, .32vw, 4px) clamp(6px, .45vw, 8px);
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--text);
      background: var(--panel2);
      font-size: var(--small-font);
    }
    .reason { color: var(--yellow); }
    .lamp-reasons .reason {
      max-width: 100%;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      vertical-align: top;
    }
    .table-shell {
      margin-top: 6px;
      border: 1px solid rgba(37, 49, 73, .7);
      border-radius: 12px;
      background: rgba(7, 10, 18, .18);
      overflow: hidden;
    }
    .table-toolbar {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      min-height: 34px;
      padding: 6px 8px;
      border-bottom: 1px solid rgba(37, 49, 73, .65);
      background: rgba(21, 30, 47, .42);
      color: var(--muted);
      font-size: var(--small-font);
    }
    .table-count { margin-right: auto; }
    .table-page-prev,
    .table-page-next {
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel2);
      color: var(--text);
      cursor: pointer;
      font-size: var(--small-font);
      padding: 3px 9px;
    }
    .table-page-prev:disabled,
    .table-page-next:disabled {
      cursor: default;
      opacity: .35;
    }
    .table-wrap {
      width: 100%;
      max-width: 100%;
      overflow-x: auto;
      overflow-y: auto;
      max-height: min(48vh, 520px);
    }
    table { width: 100%; min-width: 720px; border-collapse: collapse; font-size: var(--table-font); }
    th, td { padding: clamp(5px, .42vw, 8px) clamp(6px, .48vw, 9px); border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th {
      color: var(--muted);
      font-weight: 600;
      position: sticky;
      top: 0;
      z-index: 1;
      background: #101725;
    }
    details {
      margin: clamp(7px, .55vw, 10px) 0;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(21, 30, 47, .55);
      padding: clamp(6px, .45vw, 8px) clamp(7px, .55vw, 10px);
    }
    details summary {
      cursor: pointer;
      color: var(--text);
      font-weight: 650;
      list-style-position: inside;
    }
    details[open] summary { margin-bottom: 8px; color: var(--blue); }
    .ticker li { margin-bottom: 8px; }
    .small { font-size: var(--small-font); }
    pre { overflow: auto; max-width: 100%; max-height: min(42vh, 460px); }
    code { color: var(--blue); }
    @media (min-width: 1850px) {
      .span-3 { grid-column: span 3; }
      .span-4 { grid-column: span 4; }
      .span-6 { grid-column: span 6; }
      .span-8 { grid-column: span 8; }
    }
    @media (max-width: 1450px) {
      .span-3 { grid-column: span 6; }
      .span-4 { grid-column: span 6; }
      .span-8 { grid-column: span 12; }
    }
    @media (max-width: 1050px) {
      .layout { grid-template-columns: 1fr; }
      nav { position: relative; height: auto; }
      .span-3, .span-4, .span-6, .span-8, .span-12 { grid-column: span 12; }
      .topbar { flex-direction: column; align-items: stretch; }
      .lamp {
        flex-basis: auto;
        width: 100%;
      }
    }
    @media (max-width: 720px) {
      body {
        --main-pad: 8px;
        --grid-gap: 8px;
        --card-pad: 9px;
      }
      nav a { display: inline-block; margin-right: 4px; }
      .lamp { min-width: 0; height: 78px; }
      table { min-width: 640px; }
    }
    @media (max-height: 760px) and (min-width: 1051px) {
      body:not(.density-wide) {
        --main-pad: 10px;
        --grid-gap: 8px;
        --card-pad: 10px;
        --body-font: 12px;
        --table-font: 10px;
        --h3-font: 14px;
        --metric-font: 20px;
      }
      nav a { padding-top: 5px; padding-bottom: 5px; }
      .table-wrap { max-height: min(42vh, 360px); }
      pre { max-height: min(34vh, 320px); }
    }
  </style>
"""


def _trading_dashboard_html_script(refresh_seconds: int) -> str:
    return f"""<script>
  const refreshSeconds = {refresh_seconds};
  let remaining = refreshSeconds;
  const counter = document.getElementById("refresh-counter");
  const pageStateKey = "trading-command-center-ui-state:" + window.location.pathname;
  function readPageState() {{
    try {{
      return JSON.parse(window.localStorage.getItem(pageStateKey) || "{{}}");
    }} catch (error) {{
      return {{}};
    }}
  }}
  function writePageState(patch) {{
    try {{
      const current = readPageState();
      window.localStorage.setItem(pageStateKey, JSON.stringify(Object.assign(current, patch)));
    }} catch (error) {{
      /* Best-effort UI continuity only. */
    }}
  }}
  let scrollSaveTimer = null;
  function saveScrollPosition() {{
    if (scrollSaveTimer) window.clearTimeout(scrollSaveTimer);
    scrollSaveTimer = window.setTimeout(() => {{
      writePageState({{ scrollX: window.scrollX || 0, scrollY: window.scrollY || 0 }});
    }}, 100);
  }}
  function saveDetailsState() {{
    const detailsState = {{}};
    document.querySelectorAll("details").forEach((node, index) => {{
      const summary = node.querySelector("summary");
      const label = summary ? summary.textContent.trim() : "";
      const key = node.id || node.getAttribute("data-dashboard-section") || label || String(index);
      detailsState[key] = node.open;
    }});
    writePageState({{ details: detailsState }});
  }}
  function attachDetailsStateListeners() {{
    document.querySelectorAll("details").forEach((node) => {{
      if (node.dataset.dashboardToggleBound === "1") return;
      node.dataset.dashboardToggleBound = "1";
      node.addEventListener("toggle", saveDetailsState);
    }});
  }}
  function restoreDetailsState(state) {{
    const detailsState = state.details || {{}};
    document.querySelectorAll("details").forEach((node, index) => {{
      const summary = node.querySelector("summary");
      const label = summary ? summary.textContent.trim() : "";
      const key = node.id || node.getAttribute("data-dashboard-section") || label || String(index);
      if (Object.prototype.hasOwnProperty.call(detailsState, key)) {{
        node.open = Boolean(detailsState[key]);
      }}
    }});
    attachDetailsStateListeners();
  }}
  function restoreScrollPosition(state) {{
    if (window.location.hash) return;
    const y = Number(state.scrollY || 0);
    const x = Number(state.scrollX || 0);
    if (Number.isFinite(y) && y > 0) {{
      window.setTimeout(() => window.scrollTo(x, y), 80);
      window.setTimeout(() => window.scrollTo(x, y), 250);
    }}
  }}
  function applyResponsiveDensity() {{
    const width = window.innerWidth || document.documentElement.clientWidth || 0;
    const height = window.innerHeight || document.documentElement.clientHeight || 0;
    const compact = width < 1280 || height < 820;
    const wide = width >= 1850 && height >= 900;
    document.body.classList.toggle("density-compact", compact && !wide);
    document.body.classList.toggle("density-wide", wide);
  }}
  function applyLocalTimes() {{
    document.querySelectorAll(".local-time[data-iso]").forEach((node) => {{
      const raw = node.dataset.iso || "";
      const parsed = new Date(raw);
      if (Number.isNaN(parsed.getTime())) return;
      node.textContent = parsed.toLocaleString(undefined, {{
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }});
      node.title = raw;
    }});
  }}
  function initializeDashboardTables() {{
    document.querySelectorAll("[data-dashboard-table='1']").forEach((shell, shellIndex) => {{
      const tbody = shell.querySelector("tbody");
      const rows = tbody ? Array.from(tbody.querySelectorAll("tr")) : [];
      const pageSize = Math.max(1, Number(shell.dataset.pageSize || 12));
      const totalRows = rows.length;
      const controls = shell.querySelector(".table-toolbar");
      const label = shell.querySelector(".table-page-label");
      const count = shell.querySelector(".table-count");
      const prev = shell.querySelector(".table-page-prev");
      const next = shell.querySelector(".table-page-next");
      if (!tbody || !controls || !label || !count || !prev || !next) return;
      const hasRealRows = totalRows > 0 && !rows[0].querySelector("td[colspan]");
      const totalPages = hasRealRows ? Math.max(1, Math.ceil(totalRows / pageSize)) : 1;
      let currentPage = Math.min(Math.max(1, Number(shell.dataset.currentPage || 1)), totalPages);
      function renderPage() {{
        rows.forEach((row, index) => {{
          if (!hasRealRows || totalRows <= pageSize) {{
            row.hidden = false;
            return;
          }}
          const start = (currentPage - 1) * pageSize;
          row.hidden = index < start || index >= start + pageSize;
        }});
        const startRow = hasRealRows ? ((currentPage - 1) * pageSize + 1) : 0;
        const endRow = hasRealRows ? Math.min(totalRows, currentPage * pageSize) : 0;
        count.textContent = hasRealRows ? `${{totalRows}} rows · showing ${{startRow}}-${{endRow}}` : "0 rows";
        label.textContent = `page ${{currentPage}} / ${{totalPages}}`;
        prev.disabled = currentPage <= 1;
        next.disabled = currentPage >= totalPages;
        controls.hidden = !hasRealRows || totalRows <= pageSize;
        shell.dataset.currentPage = String(currentPage);
      }}
      if (prev.dataset.dashboardPagerBound !== "1") {{
        prev.dataset.dashboardPagerBound = "1";
        prev.addEventListener("click", () => {{
          currentPage = Math.max(1, currentPage - 1);
          renderPage();
        }});
      }}
      if (next.dataset.dashboardPagerBound !== "1") {{
        next.dataset.dashboardPagerBound = "1";
        next.addEventListener("click", () => {{
          currentPage = Math.min(totalPages, currentPage + 1);
          renderPage();
        }});
      }}
      renderPage();
    }});
  }}
  const savedPageState = readPageState();
  applyResponsiveDensity();
  applyLocalTimes();
  initializeDashboardTables();
  restoreDetailsState(savedPageState);
  restoreScrollPosition(savedPageState);
  async function refreshDashboardSections() {{
    if (refreshSeconds <= 0) return;
    if (window.location.protocol === "file:") {{
      if (counter) counter.textContent = "localhost";
      return;
    }}
    try {{
      writePageState({{ scrollX: window.scrollX || 0, scrollY: window.scrollY || 0 }});
      saveDetailsState();
      const response = await fetch(window.location.href.split("#")[0], {{
        cache: "no-store",
        headers: {{ "X-Dashboard-Partial-Refresh": "1" }}
      }});
      if (!response.ok) return;
      const text = await response.text();
      const nextDoc = new DOMParser().parseFromString(text, "text/html");
      const nextMain = nextDoc.querySelector("main");
      const currentMain = document.querySelector("main");
      if (!nextMain || !currentMain) return;
      const stateBeforeSwap = readPageState();
      currentMain.innerHTML = nextMain.innerHTML;
      document.body.className = nextDoc.body.className;
      document.title = nextDoc.title || document.title;
      applyResponsiveDensity();
      applyLocalTimes();
      initializeDashboardTables();
      const currentSnapshot = document.getElementById("dashboard-snapshot");
      const nextSnapshot = nextDoc.getElementById("dashboard-snapshot");
      if (currentSnapshot && nextSnapshot) {{
        currentSnapshot.textContent = nextSnapshot.textContent || "";
      }}
      restoreDetailsState(stateBeforeSwap);
      const x = Number(stateBeforeSwap.scrollX || 0);
      const y = Number(stateBeforeSwap.scrollY || 0);
      if (Number.isFinite(y)) {{
        window.requestAnimationFrame(() => window.scrollTo(Number.isFinite(x) ? x : 0, y));
      }}
      remaining = refreshSeconds;
    }} catch (error) {{
      /* Keep the current dashboard visible if the next artifact is mid-write or unavailable. */
    }}
  }}
  window.addEventListener("scroll", saveScrollPosition, {{ passive: true }});
  window.addEventListener("resize", applyResponsiveDensity, {{ passive: true }});
  window.addEventListener("beforeunload", () => {{
    writePageState({{ scrollX: window.scrollX || 0, scrollY: window.scrollY || 0 }});
    saveDetailsState();
  }});
  document.addEventListener("visibilitychange", () => {{
    if (document.visibilityState === "hidden") {{
      writePageState({{ scrollX: window.scrollX || 0, scrollY: window.scrollY || 0 }});
      saveDetailsState();
    }}
  }});
  if (refreshSeconds > 0 && counter) {{
    setInterval(() => {{
      remaining -= 1;
      if (remaining < 0) remaining = refreshSeconds;
      counter.textContent = remaining.toString();
    }}, 1000);
    setInterval(refreshDashboardSections, Math.max(1000, refreshSeconds * 1000));
  }}
</script>
"""


# ---------------------------------------------------------------------------
# HTML section renderers
# ---------------------------------------------------------------------------

def _local_time_text(value: Any, default: str = "-") -> str:
    if value in (None, ""):
        return default
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value or default)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _render_dashboard_status_recovery_cards(ctx: dict[str, Any]) -> str:
    opinion = ctx["opinion"]
    focus_items = ctx["focus_items"]
    watch_items = ctx["watch_items"]
    safety = ctx["safety"]
    operational_status = ctx["operational_status"]
    trading_readiness = ctx["trading_readiness"]
    raw_color = ctx["raw_color"]
    status = ctx["status"]
    status_category_rows = ctx["status_category_rows"]
    dashboard_status_timeline = ctx["dashboard_status_timeline"]
    status_timeline_rows = ctx["status_timeline_rows"]
    action_queue = ctx["action_queue"]
    action_queue_hidden = ctx["action_queue_hidden"]
    action_queue_rows = ctx["action_queue_rows"]
    option_confidence_recovery = ctx["option_confidence_recovery"]
    option_confidence_recovery_rows = ctx["option_confidence_recovery_rows"]
    revival_review_rows = ctx["revival_review_rows"]
    recovery_plan = ctx["recovery_plan"]
    recovery_plan_rows = ctx["recovery_plan_rows"]
    recovery_executor = ctx["recovery_executor"]
    recovery_executor_rows = ctx["recovery_executor_rows"]
    recovery_post_validation = ctx["recovery_post_validation"]
    return f"""      <article class="card span-8">
        <h3>AI opinion</h3>
        <p><strong>{_html_text(opinion.get("stance"))}</strong> &mdash; {_html_text(opinion.get("note_ko"))}</p>
        <p class="muted">Focus</p>
        <div>{focus_items or '<span class="muted">-</span>'}</div>
        <p class="muted">Watch items</p>
        <div>{watch_items or '<span class="muted">-</span>'}</div>
      </article>
      <article class="card span-4" id="settings">
        <h3>Safety</h3>
        <p>{_html_badge("paper only" if safety.get("paper_only_dashboard") else "check mode")} {_html_badge("read only" if safety.get("read_only_snapshot_builder") else "check writes")} {_html_badge("transmit off" if not safety.get("transmit_enabled") else "transmit enabled")}</p>
        <p class="muted small">This UI does not submit, stage, transmit, or close real orders.</p>
      </article>
      <article class="card span-8" id="status_categories">
        <h3>Status Categories</h3>
        <p>{_html_badge(f"operational {operational_status.get('color') or '-'}")} {_html_badge(f"readiness {trading_readiness.get('label') or '-'}")} {_html_badge(f"raw {raw_color}")}</p>
        <p>{_html_badge(f"dominant {status.get('dominant_reason_category') or '-'}")} {_html_badge(f"{len(status.get('reasons') or [])} reasons")} {_html_badge("trade blocking" if (status.get("trade_blocking_summary") or {}).get("trade_blocking") else "monitoring only")}</p>
        <p class="muted small">{_html_text(status.get("natural_language_ko") or "-")}</p>
        <p class="muted small">Operational watch {_html_text(operational_status.get("watch_reasons") or [])} &middot; readiness blocks {_html_text(trading_readiness.get("blocking_reasons") or [])}</p>
        <p class="muted small">Blocking {_html_text((status.get("trade_blocking_summary") or {}).get("trade_blocking_reasons") or [])} &middot; monitoring {_html_text((status.get("trade_blocking_summary") or {}).get("monitoring_only_reasons") or [])}</p>
        <p class="muted small">Alpha flow {_html_text((status.get("alpha_entry_flow") or {}).get("summary") or "-")} &middot; final {_html_text((status.get("alpha_entry_flow") or {}).get("final_state") or "-")}</p>
        <p class="muted small">RED is split into system/data/market/safety/session categories so a safety block is not mistaken for a process error.</p>
        {_html_table(["Category", "Count", "Reasons"], status_category_rows, empty_text="No status categories")}
      </article>
      <article class="card span-4" id="status_timeline">
        <h3>Status Timeline</h3>
        <p>{_html_badge(dashboard_status_timeline.get("status") or "not available")} {_html_badge(f"{dashboard_status_timeline.get('transition_count') or 0} transitions")} {_html_badge(f"{dashboard_status_timeline.get('window_row_count') or 0} rows")}</p>
        <p class="muted small">Recent dashboard state changes. This is diagnostic-only and does not alter gates.</p>
        {_html_table(["At", "From", "To", "Changed", "Reasons"], status_timeline_rows, empty_text="No recent status transitions")}
      </article>
      <article class="card span-12" id="action_queue">
        <h3>Action Queue</h3>
        <p>{_html_badge(action_queue.get("highest_severity") or "green")} {_html_badge(f"{action_queue.get('visible_action_count') or 0} visible")} {_html_badge(f"{action_queue.get('total_action_count') or 0} total")} {_html_badge(f"priority {action_queue.get('highest_priority_score') or 0}")}</p>
        <p class="muted">Read-only recovery/risk-response priorities. This card does not execute broker orders or transmit trades.</p>
        <p class="muted small">Hidden actions {_html_text(action_queue_hidden.get("hidden_action_count") or 0)} &middot; hidden promotion repair/recycle {_html_text(action_queue_hidden.get("promotion_repair_hidden_count") or 0)} &middot; ids {_html_text(action_queue_hidden.get("promotion_repair_hidden_action_ids") or [])}</p>
        {_html_table(["Priority", "Severity", "Domain", "Action", "Recommendation"], action_queue_rows, empty_text="No priority action is required")}
      </article>
      <article class="card span-12" id="option_confidence_recovery">
        <h3>Option Confidence Recovery</h3>
        <p>{_html_badge(option_confidence_recovery.get("status") or "not configured")} {_html_badge(f"repair actions {option_confidence_recovery.get('repair_action_count') or 0}")} {_html_badge(f"recycle {option_confidence_recovery.get('recycle_candidate_count') or 0}")} {_html_badge(f"near {option_confidence_recovery.get('near_return_count') or 0}")}</p>
        <p class="muted small">Root causes {_html_text(option_confidence_recovery.get("root_cause_counts") or {})} &middot; quote quality {_html_text(option_confidence_recovery.get("final_quote_quality_status") or "-")} / optional index {_html_text(option_confidence_recovery.get("final_optional_index_quote_quality_status") or "-")}</p>
        <p class="muted small">Revival queue {_html_text(option_confidence_recovery.get("revival_review_queue_count") or 0)} &middot; auto promote disabled until fresh samples, option confidence, freshness, and drawdown/regime checks pass.</p>
        {_html_table(["Step", "Why", "Command hint"], option_confidence_recovery_rows, empty_text="No option confidence recovery steps")}
        {_html_table(["Candidate", "Queue status", "Lifecycle", "New samples", "Root causes", "Required checks"], revival_review_rows, empty_text="No revival review queue rows")}
      </article>
      <article class="card span-12" id="recovery_plan">
        <h3>Recovery Plan</h3>
        <p>{_html_badge(recovery_plan.get("highest_severity") or "green")} {_html_badge(f"{recovery_plan.get('planned_step_count') or 0} dry-run steps")} {_html_badge("dry run" if recovery_plan.get("dry_run", True) else "execution mode")}</p>
        <p class="muted">Read-only dry-run plan derived from the action queue. It previews sequencing and cooldowns but does not execute subprocesses, refresh jobs, broker orders, or transmit.</p>
        {_html_table(["Order", "Severity", "Phase", "Status", "Source action", "Command preview"], recovery_plan_rows, empty_text="No recovery step is required")}
      </article>
      <article class="card span-12" id="recovery_executor">
        <h3>Recovery Executor</h3>
        <p>{_html_badge(recovery_executor.get("final_status") or "not configured")} {_html_badge("execute requested" if recovery_executor.get("execute_requested") else "dry run")} {_html_badge(f"{recovery_executor.get('executed_command_count') or 0} executed")} {_html_badge(f"{recovery_executor.get('failure_count') or 0} failures")}</p>
        <p class="muted">Allowlist-only read-only recovery executor. The dashboard loop records a dry-run executor report; actual execution requires the separate CLI with --execute.</p>
        {_html_table(["Step", "Phase", "Status", "Commands", "Executed"], recovery_executor_rows, empty_text="No executor report is available")}
      </article>
      <article class="card span-12" id="recovery_post_validation">
        <h3>Recovery Post-Validation</h3>
        <p>{_html_badge(recovery_post_validation.get("outcome") or "not configured")} {_html_badge(f"{recovery_post_validation.get('improved_metric_count') or 0} improved")} {_html_badge(f"{recovery_post_validation.get('worsened_metric_count') or 0} worsened")} {_html_badge(f"{recovery_post_validation.get('unchanged_metric_count') or 0} unchanged")}</p>
        <p class="muted">Read-only before/after validation. It compares prior dashboard/action/plan artifacts with the current ones and does not run subprocesses or broker actions.</p>
      </article>"""


def _render_dashboard_account_market_cards(ctx: dict[str, Any]) -> str:
    account = ctx["account"]
    risk = ctx["risk"]
    mark_quality = ctx["mark_quality"]
    market_temperature = ctx["market_temperature"]
    market_summary = ctx["market_summary"]
    market_data_desk = ctx["market_data_desk"]
    market_components = ctx["market_components"]
    market_temperature_diagnostics = ctx["market_temperature_diagnostics"]
    market_temperature_adjustment = ctx["market_temperature_adjustment"]
    threshold_rows = ctx["threshold_rows"]
    market_temperature_component_rows = ctx["market_temperature_component_rows"]
    market_temperature_contributor_rows = ctx["market_temperature_contributor_rows"]
    market_symbol_quarantine = ctx["market_symbol_quarantine"]
    market_symbol_quarantine_rows = ctx["market_symbol_quarantine_rows"]
    market_data_session_overlay = ctx["market_data_session_overlay"]
    market_session_monitorability = ctx["market_session_monitorability"]
    market_session_next_open = ctx["market_session_next_open"]
    market_session_buckets = ctx["market_session_buckets"]
    market_data_risk_gate = ctx["market_data_risk_gate"]
    market_data_session_transition_gate = ctx["market_data_session_transition_gate"]
    session_transition_release_governor = ctx["session_transition_release_governor"]
    plan = ctx["plan"]
    market_data_operating_auto_gate = ctx["market_data_operating_auto_gate"]
    capital_margin_state = ctx["capital_margin_state"]
    capital_aware_action_gate = ctx["capital_aware_action_gate"]
    live_read_only_capital_validation = ctx["live_read_only_capital_validation"]
    capital_pipeline_orchestrator = ctx["capital_pipeline_orchestrator"]
    strategy_allocation_engine = ctx["strategy_allocation_engine"]
    micro_option_candidates = ctx.get("micro_option_candidates") or {}
    paper_option_ticket_preview = ctx.get("paper_option_ticket_preview") or {}
    option_whatif_safety_validation = ctx.get("option_whatif_safety_validation") or {}
    limited_auto_trading_readiness = ctx.get("limited_auto_trading_readiness") or {}
    strategy_allocation_rows = ctx["strategy_allocation_rows"]
    micro_option_candidate_rows = ctx.get("micro_option_candidate_rows") or []
    paper_option_ticket_rows = ctx.get("paper_option_ticket_rows") or []
    paper_option_ticket_review_rows = ctx.get("paper_option_ticket_review_rows") or []
    option_whatif_validation_rows = ctx.get("option_whatif_validation_rows") or []
    limited_auto_readiness_block_rows = ctx.get("limited_auto_readiness_block_rows") or []
    limited_auto_readiness_step_rows = ctx.get("limited_auto_readiness_step_rows") or []
    live_capital_validation_rows = ctx["live_capital_validation_rows"]
    capital_pipeline_rows = ctx["capital_pipeline_rows"]
    capital_withdrawal_guard = (capital_margin_state.get("withdrawal_guard") or {}) if isinstance(capital_margin_state.get("withdrawal_guard"), dict) else {}
    capital_budgets = (capital_margin_state.get("budgets") or {}) if isinstance(capital_margin_state.get("budgets"), dict) else {}
    capital_micro_budget_overlay = (capital_budgets.get("micro_cash_budget_overlay") or {}) if isinstance(capital_budgets.get("micro_cash_budget_overlay"), dict) else {}
    capital_gate_capital_state = (capital_aware_action_gate.get("capital_state") or {}) if isinstance(capital_aware_action_gate.get("capital_state"), dict) else {}
    capital_gate_withdrawal_guard = (capital_gate_capital_state.get("withdrawal_guard") or {}) if isinstance(capital_gate_capital_state.get("withdrawal_guard"), dict) else {}
    live_capital_capacity = (live_read_only_capital_validation.get("account_capacity") or {}) if isinstance(live_read_only_capital_validation.get("account_capacity"), dict) else {}
    live_capital_state = (live_read_only_capital_validation.get("capital_state") or {}) if isinstance(live_read_only_capital_validation.get("capital_state"), dict) else {}
    live_broker_whatif = (live_read_only_capital_validation.get("broker_whatif") or {}) if isinstance(live_read_only_capital_validation.get("broker_whatif"), dict) else {}
    operating_defensive_mode = ctx["operating_defensive_mode"]
    operating_protective_packet = ctx["operating_protective_packet"]
    market_data_operating_plan = ctx["market_data_operating_plan"]
    operating_gate_rows = ctx["operating_gate_rows"]
    capital_gate_rows = ctx["capital_gate_rows"]
    operating_defensive_plan = ctx["operating_defensive_plan"]
    defensive_action_rows = ctx["defensive_action_rows"]
    operating_defensive_ledger = ctx["operating_defensive_ledger"]
    defensive_intent_rows = ctx["defensive_intent_rows"]
    return f"""      <article class="card span-3" id="account">
        <h3>Net liquidation</h3>
        <div class="metric">{_html_num(account.get("net_liquidation"))}</div>
        <p class="muted">Account {_html_text(account.get("selected_account"))}</p>
      </article>
      <article class="card span-3">
        <h3>Cash</h3>
        <div class="metric">{_html_num(account.get("cash"))}</div>
        <p class="muted">Buying power {_html_num(account.get("buying_power"))}</p>
      </article>
      <article class="card span-3">
        <h3>Available funds</h3>
        <div class="metric">{_html_num(account.get("available_funds"))}</div>
        <p class="muted">Excess liquidity {_html_num(account.get("excess_liquidity"))}</p>
      </article>
      <article class="card span-3">
        <h3>Margin</h3>
        <div class="metric">{_html_num(account.get("maintenance_margin"))}</div>
        <p class="muted">Initial {_html_num(account.get("initial_margin"))}</p>
      </article>

      <article class="card span-4" id="risk">
        <h3>Portfolio risk</h3>
        <p>{_html_badge(risk.get("risk_state"))} {_html_badge(risk.get("recommended_action"))} {_html_badge("new entries allowed" if risk.get("new_entries_allowed") else "entries restricted")}</p>
        <p class="muted">Breaches {_html_text(risk.get("breach_count"))} &middot; Warnings {_html_text(risk.get("warning_count"))}</p>
        <pre class="small">{_html_text(json.dumps(risk.get("metrics") or {}, ensure_ascii=False, indent=2))}</pre>
      </article>
      <article class="card span-4" id="market_data">
        <h3>Mark quality</h3>
        <p>Refresh {_html_text(mark_quality.get("refresh_status_counts") or {})}</p>
        <p>Actions {_html_text(mark_quality.get("quality_action_counts") or {})}</p>
        <p class="muted">Delayed {_html_text(mark_quality.get("delayed_row_count"))} &middot; Missing {_html_text(mark_quality.get("missing_mark_count"))} &middot; Fallback {_html_text(mark_quality.get("fallback_tick_count"))}</p>
      </article>
      <article class="card span-4">
        <h3>Market temperature</h3>
        <div class="metric">{_html_text(market_temperature.get("label") or market_summary.get("temperature_label") or "-")}</div>
        <p>Score {_html_num(market_temperature.get("score") if market_temperature.get("score") is not None else market_summary.get("temperature_score"), digits=1)} &middot; Status {_html_badge(market_data_desk.get("status") or market_summary.get("status") or "unknown")}</p>
        <p class="muted">Live {_html_text(market_summary.get("live_count"))} &middot; Delayed {_html_text(market_summary.get("delayed_count"))} &middot; Missing {_html_text(market_summary.get("missing_price_count"))} &middot; Warming {_html_text(market_summary.get("warming_up_count"))} &middot; Alerts {_html_text(market_summary.get("alert_count"))}</p>
        <p class="muted">Data {_html_badge((market_components.get("data") or {}).get("label") or "-")} Liquidity {_html_badge((market_components.get("liquidity") or {}).get("label") or "-")} Move {_html_badge((market_components.get("market_move") or {}).get("label") or "-")}</p>
        <div>{''.join(_html_badge(item) for item in (market_temperature.get("risk_flags") or market_summary.get("risk_flags") or [])[:5]) or '<span class="muted">No flags</span>'}</div>
      </article>
      <article class="card span-8" id="market_temperature_diagnostics">
        <h3>Market Temperature Diagnostics</h3>
        <p>{_html_badge(market_temperature_diagnostics.get("label") or "-")} {_html_badge(f"score {market_temperature_diagnostics.get('score') if market_temperature_diagnostics.get('score') is not None else '-'}")} {_html_badge(f"causes {len(market_temperature_diagnostics.get('root_causes') or [])}")} {_html_badge(market_temperature_adjustment.get("adjusted_interpretation") or "-")}</p>
        <p class="muted small">Raw {_html_text(market_temperature_adjustment.get("raw_status") or "-")} &middot; adjusted {_html_text(market_temperature_adjustment.get("adjusted_status") or "-")} &middot; false-positive risk {_html_text(market_temperature_adjustment.get("false_positive_risk"))} &middot; trade-blocking {_html_text(market_temperature_adjustment.get("trade_blocking_market_temperature"))}</p>
        <p class="muted small">Lifecycle {_html_text((market_temperature_adjustment.get("monitoring_only_lifecycle") or {}).get("policy") or {})} &middot; release {_html_text((market_temperature_adjustment.get("monitoring_only_lifecycle") or {}).get("release_now"))} &middot; re-escalate {_html_text((market_temperature_adjustment.get("monitoring_only_lifecycle") or {}).get("re_escalate_now"))}</p>
        <p class="muted small">Root causes {_html_text(market_temperature_diagnostics.get("root_causes") or [])} &middot; counts {_html_text(market_temperature_diagnostics.get("summary_counts") or {})}</p>
        {_html_table(["Metric", "Regular", "Non-regular", "Unit"], threshold_rows, empty_text="No session threshold table")}
        {_html_table(["Component", "Status", "Score", "Counts"], market_temperature_component_rows, empty_text="No temperature components")}
        {_html_table(["Key", "Group", "Status", "Gap %", "Tick %", "Spread %", "Alerts"], market_temperature_contributor_rows, empty_text="No temperature contributors")}
      </article>
      <article class="card span-4" id="market_symbol_quarantine">
        <h3>Symbol Anomaly Quarantine</h3>
        <div class="metric">{_html_text(market_symbol_quarantine.get("status") or "not configured")}</div>
        <p>{_html_badge(f"candidates {market_symbol_quarantine.get('candidate_count') or 0}")} {_html_badge("read only")}</p>
        <p class="muted small">These rows are not removed. They are downweighted for temperature interpretation when spread/gap/close-only quotes look suspicious.</p>
        {_html_table(["Key", "Group", "Severity", "Lifecycle", "Spread %", "Gap %", "Reasons"], market_symbol_quarantine_rows, empty_text="No anomaly quarantine candidates")}
      </article>
      <article class="card span-4" id="market_data_session">
        <h3>Market Data Session Overlay</h3>
        <div class="metric">{_html_text(market_data_session_overlay.get("session_name") or "not configured")}</div>
        <p>{_html_badge(market_data_session_overlay.get("risk_interpretation") or "-")} {_html_badge("stock overnight on" if market_data_session_overlay.get("stock_cash_overnight_monitoring_enabled") else "stock overnight unknown")} {_html_badge("options regular-only" if market_data_session_overlay.get("option_quotes_regular_session_dependent") else "options session unknown")}</p>
        <p class="muted">Monitorable now {_html_text(market_session_monitorability.get("monitorable_now") or 0)} &middot; Regular/reference dependent {_html_text(market_session_monitorability.get("monitor_if_fresh_price_present") or 0)}</p>
        <p class="muted small">Next regular open {_html_text(market_session_next_open.get("next_open_local") or "-")} &middot; buckets {_html_text(market_session_buckets)}</p>
      </article>
      <article class="card span-4">
        <h3>Market Data Risk Gate</h3>
        <div class="metric">{_html_text(market_data_risk_gate.get("gate_status") or "not configured")}</div>
        <p>{_html_badge(market_data_risk_gate.get("action") or "-")} {_html_badge("entries allowed" if market_data_risk_gate.get("new_entries_allowed") else "entries restricted")}</p>
        <p class="muted">Size {_html_text(market_data_risk_gate.get("entry_size_multiplier") if market_data_risk_gate else "-")} &middot; Hedge {_html_text(market_data_risk_gate.get("hedge_entries_allowed") if market_data_risk_gate else "-")} &middot; Bias {_html_text(market_data_risk_gate.get("option_strategy_bias") or "-")}</p>
        <p class="muted">{_html_text(market_data_risk_gate.get("reason") or "-")}</p>
        <p class="muted small">{_html_text(" | ".join(str(item) for item in (market_data_risk_gate.get("operator_summary_lines") or [])) or "No operator summary available.")}</p>
        <p class="muted small">Source {_html_text((market_data_risk_gate.get("source_freshness") or {}).get("status") or "-")} &middot; Macro {_html_text((market_data_risk_gate.get("macro_reaction_assessment") or {}).get("status") or "-")} &middot; Options {_html_text((market_data_risk_gate.get("option_structure_context") or {}).get("overall_status") or "-")} &middot; Close-proxy {_html_text((market_data_risk_gate.get("close_only_proxy_status") or {}).get("proxy_available_count") or 0)}</p>
      </article>
      <article class="card span-4" id="session_transition_gate">
        <h3>Session Transition Gate</h3>
        <div class="metric">{_html_text(market_data_session_transition_gate.get("decision") or "not configured")}</div>
        <p>
          {_html_badge(f"severity {market_data_session_transition_gate.get('severity') or '-'}")}
          {_html_badge(f"{market_data_session_transition_gate.get('previous_session') or '-'} → {market_data_session_transition_gate.get('current_session') or '-'}")}
          {_html_badge(f"transition {market_data_session_transition_gate.get('transition_type') or '-'}")}
        </p>
        <p class="muted">Rebuild before auto-release {_html_text(market_data_session_transition_gate.get("rebuild_required_before_auto_release"))} &middot; Alpha after transition {_html_text(market_data_session_transition_gate.get("ordinary_alpha_entries_allowed_after_transition"))} &middot; Size {_html_text(market_data_session_transition_gate.get("max_new_entry_size_multiplier"))}</p>
        <p class="muted small">{_html_text(market_data_session_transition_gate.get("reason") or "Requires fresh session, risk, and operating gates before ordinary paper alpha is released.")}</p>
      </article>
      <article class="card span-4" id="session_release_governor">
        <h3>Session Release Governor</h3>
        <div class="metric">{_html_text(session_transition_release_governor.get("release_status") or "not configured")}</div>
        <p>
          {_html_badge(f"alpha {session_transition_release_governor.get('ordinary_alpha_release_allowed')}")}
          {_html_badge(f"size {session_transition_release_governor.get('max_new_entry_size_multiplier')}")}
          {_html_badge(f"failed {session_transition_release_governor.get('failed_check_count') or 0}")}
        </p>
        <p class="muted">M204 plan blocks: primary {_html_text(plan.get("blocked_by_session_transition_release_governor_count") or 0)} &middot; operating {_html_text(market_data_operating_plan.get("blocked_by_session_transition_release_governor_count") or 0)}</p>
        <p class="muted small">{_html_text(session_transition_release_governor.get("reason") or "M204 blocks ordinary alpha paper execution while preserving hedge/risk-reduction paths.")}</p>
      </article>
      <article class="card span-8" id="operating_gate">
        <h3>Operating Gate</h3>
        <div class="metric">{_html_text(market_data_operating_auto_gate.get("operating_mode") or "not configured")}</div>
        <p>
          {_html_badge(f"severity {operating_defensive_mode.get('severity') or '-'}")}
          {_html_badge(f"auto-ready {market_data_operating_auto_gate.get('auto_ready_count') if market_data_operating_auto_gate else '-'}")}
          {_html_badge(f"pending {market_data_operating_plan.get('pending_paper_execution_count') if market_data_operating_plan else '-'}")}
          {_html_badge(f"protective {operating_protective_packet.get('candidate_count') if operating_protective_packet else '-'}")}
          {_html_badge("ticketing on" if operating_protective_packet.get("ticketing_allowed") else "ticketing off")}
        </p>
        <p class="muted">
          Alpha {_html_text(operating_defensive_mode.get("ordinary_alpha_entries_allowed"))}
          &middot; Hedge {_html_text(operating_defensive_mode.get("hedge_or_defensive_entries_allowed"))}
          &middot; Exit/risk-reduction {_html_text(operating_defensive_mode.get("exit_or_risk_reduction_allowed"))}
          &middot; Size {_html_text(operating_defensive_mode.get("max_new_entry_size_multiplier"))}
        </p>
        <p class="muted small">This is the M182 paper operating overlay. It blocks/filters paper intent flow only and keeps protective option candidates analysis-only unless ticketing is explicitly allowed.</p>
        {_html_table(["Symbol", "Action", "Ticket", "Status", "Block reasons", "Size"], operating_gate_rows, empty_text="No operating-gate sample rows")}
      </article>
      <article class="card span-8" id="capital_aware_gate">
        <h3>Capital-Aware Gate</h3>
        <div class="metric">{_html_text(capital_gate_capital_state.get("leverage_mode") or capital_margin_state.get("leverage_mode") or "not configured")}</div>
        <p>
          {_html_badge(f"capital {capital_margin_state.get('status') or capital_gate_capital_state.get('status') or '-'}")}
          {_html_badge(f"ready {capital_aware_action_gate.get('capital_ready_count') if capital_aware_action_gate else '-'}")}
          {_html_badge(f"resized {capital_aware_action_gate.get('capital_resized_count') if capital_aware_action_gate else '-'}")}
          {_html_badge(f"blocked {capital_aware_action_gate.get('capital_blocked_count') if capital_aware_action_gate else '-'}")}
          {_html_badge(f"capacity block {capital_aware_action_gate.get('capital_capacity_blocked_count') if capital_aware_action_gate else '-'}")}
          {_html_badge(f"action block {capital_aware_action_gate.get('capital_action_blocked_count') if capital_aware_action_gate else '-'}")}
        </p>
        <p class="muted">
          Cash budget {_html_num(capital_budgets.get("deployable_cash_after_reserve"))}
          &middot; Option premium {_html_num(capital_budgets.get("options_premium_budget"))}
          &middot; Max single {_html_num(capital_budgets.get("max_single_position_notional"))}
          &middot; New margin {_html_num(capital_budgets.get("max_new_initial_margin"))}
        </p>
        <p class="muted small">
          Budget model {_html_text(capital_budgets.get("budget_model") or "standard_fractional")}
          &middot; Micro overlay {_html_text(capital_micro_budget_overlay.get("reason") or "-")}
          &middot; General option {_html_num(capital_micro_budget_overlay.get("general_options_premium_budget"))}
          &middot; Micro option {_html_num(capital_micro_budget_overlay.get("micro_options_premium_budget_candidate"))}
        </p>
        <p class="muted small">
          Max safe withdrawal {_html_num(capital_withdrawal_guard.get("max_safe_withdrawal") if capital_withdrawal_guard else capital_gate_withdrawal_guard.get("max_safe_withdrawal"))}
          &middot; Withdrawal status {_html_text(capital_withdrawal_guard.get("status") or capital_gate_withdrawal_guard.get("status") or "-")}
          &middot; Fresh snapshot required {_html_text(capital_withdrawal_guard.get("requires_fresh_account_snapshot") if capital_withdrawal_guard else capital_gate_withdrawal_guard.get("requires_fresh_account_snapshot"))}
        </p>
        <p class="muted small">Block categories {_html_text(capital_aware_action_gate.get("capital_block_category_counts") or {})}</p>
        <p class="muted small">M239 applies account cash/margin/withdrawal budgets before paper intent ledgering. `capacity block` means the account budget cannot carry it; `action block` means an upstream source/market/action gate is not ready. It never submits or transmits broker orders.</p>
        {_html_table(["Symbol", "Action", "Ticket", "Capital status", "Category", "Qty", "Cash req", "Margin req", "Reasons"], capital_gate_rows, empty_text="No capital-aware sample rows")}
      </article>
      <article class="card span-8" id="live_capital_validation">
        <h3>Live Read-Only Capital Validation</h3>
        <div class="metric">{_html_text(live_read_only_capital_validation.get("status") or "not configured")}</div>
        <p>
          {_html_badge(f"feasible {live_read_only_capital_validation.get('current_size_feasible_count') if live_read_only_capital_validation else '-'}")}
          {_html_badge(f"resize {live_read_only_capital_validation.get('resize_required_count') if live_read_only_capital_validation else '-'}")}
          {_html_badge(f"blocked {live_read_only_capital_validation.get('blocked_count') if live_read_only_capital_validation else '-'}")}
          {_html_badge(f"trade {live_read_only_capital_validation.get('trade_executable_current_size_count') if live_read_only_capital_validation else '-'}")}
          {_html_badge(f"trade after resize {live_read_only_capital_validation.get('trade_executable_after_resize_count') if live_read_only_capital_validation else '-'}")}
          {_html_badge(f"micro blocked {live_read_only_capital_validation.get('micro_policy_blocked_count') if live_read_only_capital_validation else '-'}")}
          {_html_badge(f"what-if safe {live_broker_whatif.get('safe_to_use') if live_broker_whatif else '-'}")}
        </p>
        <p class="muted">
          Account {_html_text(live_capital_state.get("selected_account") or "-")}
          &middot; Leverage {_html_text(live_capital_state.get("leverage_mode") or "-")}
          &middot; Snapshot age {_html_text(live_capital_state.get("snapshot_age_seconds") if live_capital_state.get("snapshot_age_seconds") is not None else "-")}s
        </p>
        <p class="muted small">
          Cash {_html_num(live_capital_capacity.get("deployable_cash_after_reserve"))}
          &middot; New margin {_html_num(live_capital_capacity.get("max_new_initial_margin"))}
          &middot; Max single {_html_num(live_capital_capacity.get("max_single_position_notional"))}
          &middot; Options {_html_num(live_capital_capacity.get("options_premium_budget"))}
          &middot; Safe withdrawal {_html_num(live_capital_capacity.get("max_safe_withdrawal"))}
        </p>
        <p class="muted small">
          M246 micro policy {_html_text(live_read_only_capital_validation.get("micro_policy_operating_mode") or "-")}
          &middot; ready {_html_text(live_read_only_capital_validation.get("micro_policy_current_ready_count") if live_read_only_capital_validation else "-")}
          &middot; after-resize ready {_html_text(live_read_only_capital_validation.get("micro_policy_after_resize_ready_count") if live_read_only_capital_validation else "-")}
          &middot; reasons {_html_text(live_read_only_capital_validation.get("micro_policy_block_reason_counts") or {})}
        </p>
        <p class="muted small">
          Micro exit {_html_text((live_read_only_capital_validation.get("micro_policy_state") or {}).get("transition") or "-")}
          &middot; confirmations {_html_text((live_read_only_capital_validation.get("micro_policy_state") or {}).get("consecutive_exit_confirmations") or 0)}
          / {_html_text((live_read_only_capital_validation.get("micro_policy_state") or {}).get("exit_confirmations_required") or 0)}
          &middot; exit-ready {_html_text((live_read_only_capital_validation.get("micro_policy_state") or {}).get("exit_ready"))}
        </p>
        <p class="muted small">M245 separates capital-feasible from trade-executable. Feasible means account capacity can carry it; trade requires source, market, session, cash-only, broker, micro-policy, and capital gates to be ready.</p>
        <p class="muted small">Blocked by gate {_html_text(live_read_only_capital_validation.get("blocked_by_gate_counts") or {})}</p>
        {_html_table(["Symbol", "Action", "Ticket", "Source qty", "Max qty", "Capital status", "Execution status", "Micro", "Binding", "Reasons"], live_capital_validation_rows, empty_text="No live read-only capital validation rows")}
      </article>
      <article class="card span-8" id="capital_pipeline">
        <h3>Capital Pipeline Freshness</h3>
        <div class="metric">{_html_text(capital_pipeline_orchestrator.get("status") or "not configured")}</div>
        <p>
          {_html_badge(f"stale/missing {capital_pipeline_orchestrator.get('stale_or_missing_count') if capital_pipeline_orchestrator else '-'}")}
          {_html_badge(f"rebuild {capital_pipeline_orchestrator.get('rebuild_required_count') if capital_pipeline_orchestrator else '-'}")}
          {_html_badge(f"executed {(capital_pipeline_orchestrator.get('execution') or {}).get('executed_rebuild') if capital_pipeline_orchestrator else '-'}")}
          {_html_badge(f"age {capital_pipeline_orchestrator.get('age_seconds') if capital_pipeline_orchestrator else '-'}s")}
        </p>
        <p class="muted small">
          Rebuild steps {_html_text(capital_pipeline_orchestrator.get("rebuild_steps") or [])}
          &middot; Executed steps {_html_text((capital_pipeline_orchestrator.get("execution") or {}).get("executed_steps") or [])}
        </p>
        <p class="muted small">M244 checks whether M237 capital state, M239 capital gate, M241 allocation, M242 paper execution, and M243 live read-only validation are all based on the same fresh chain.</p>
        {_html_table(["Artifact", "Status", "Age sec", "Dependency lag", "Rebuild", "Reasons"], capital_pipeline_rows, empty_text="No capital pipeline artifact rows")}
      </article>
      <article class="card span-8" id="strategy_allocation">
        <h3>Strategy Allocation Engine</h3>
        <div class="metric">{_html_text(strategy_allocation_engine.get("mode") or "not configured")}</div>
        <p>
          {_html_badge(f"ready {strategy_allocation_engine.get('paper_trade_ready_count') if strategy_allocation_engine else '-'}")}
          {_html_badge(f"hedge-only {strategy_allocation_engine.get('hedge_only_count') if strategy_allocation_engine else '-'}")}
          {_html_badge(f"blocked {strategy_allocation_engine.get('blocked_count') if strategy_allocation_engine else '-'}")}
          {_html_badge(f"micro blocked {strategy_allocation_engine.get('micro_policy_blocked_count') if strategy_allocation_engine else '-'}")}
          {_html_badge(f"age {strategy_allocation_engine.get('age_seconds') if strategy_allocation_engine else '-'}s")}
        </p>
        <p class="muted">
          Recommendations {_html_text(strategy_allocation_engine.get("recommendation_counts") or {})}
          &middot; Expressions {_html_text(strategy_allocation_engine.get("expression_counts") or {})}
        </p>
        <p class="muted small">
          Capital {_html_text((strategy_allocation_engine.get("capital_context") or {}).get("status") or "-")}
          &middot; Micro {_html_text(strategy_allocation_engine.get("micro_policy_operating_mode") or ((strategy_allocation_engine.get("micro_policy_context") or {}).get("operating_mode") or "-"))}
          &middot; Market gate {_html_text((strategy_allocation_engine.get("market_context") or {}).get("risk_gate_action") or "-")}
          &middot; Option quotes usable {_html_text((strategy_allocation_engine.get("options_context") or {}).get("quote_usable"))}
          &middot; Safety paper-only {_html_text((strategy_allocation_engine.get("safety") or {}).get("paper_recommendation_only"))}
        </p>
        <p class="muted small">
          M241.5 ranked {_html_text((strategy_allocation_engine.get("ticker_candidate_ranking_context") or {}).get("ranked_candidate_count") or "-")}
          tickers &middot; top {_html_text((strategy_allocation_engine.get("ticker_candidate_ranking_context") or {}).get("top_symbols") or [])}
          &middot; M241.6 appended {_html_text(strategy_allocation_engine.get("m241_6_synthetic_appended_count") or 0)}
          / injectable {_html_text((strategy_allocation_engine.get("ticker_action_candidate_synthesis_context") or {}).get("injectable_candidate_count") or 0)}
          &middot; M241.7 cross-asset {_html_text(strategy_allocation_engine.get("top_cross_asset_expressions") or [])}
        </p>
        <p class="muted small">M241 chooses the safest paper/read-only expression for each candidate; M246 constrains that choice for micro cash-only accounts. It never submits or transmits broker orders.</p>
          {_html_table(["Symbol", "Source", "Direction", "Recommendation", "Expression", "X-asset", "Confidence", "M241.5 rank", "Gamma", "Micro", "Capital status", "Reasons"], strategy_allocation_rows, empty_text="No strategy allocation rows")}
      </article>
      <article class="card span-8" id="micro_option_candidates">
        <h3>Micro Cash Option Candidates</h3>
        <div class="metric">{_html_text(micro_option_candidates.get("status") or "not configured")}</div>
        <p>
          {_html_badge(f"ready {micro_option_candidates.get('ready_count') if micro_option_candidates else '-'}")}
          {_html_badge(f"stale {micro_option_candidates.get('stale_preview_count') if micro_option_candidates else '-'}")}
          {_html_badge(f"blocked {micro_option_candidates.get('blocked_count') if micro_option_candidates else '-'}")}
          {_html_badge(f"budget {_html_num(micro_option_candidates.get('option_budget'))}")}
          {_html_badge(f"max ticket {_html_num(micro_option_candidates.get('max_option_ticket_debit'))}")}
          {_html_badge(f"quote fresh {micro_option_candidates.get('quote_fresh') if micro_option_candidates else '-'}")}
        </p>
        <p class="muted">
          Mode {_html_text(micro_option_candidates.get("mode") or "-")}
          &middot; Micro {_html_text(micro_option_candidates.get("micro_policy_operating_mode") or "-")}
          &middot; Quote age {_html_text(micro_option_candidates.get("quote_age_seconds") if micro_option_candidates.get("quote_age_seconds") is not None else "-")}s
          &middot; Reasons {_html_text(micro_option_candidates.get("top_reason_counts") or {})}
        </p>
        <p class="muted small">M247 ranks stored option quotes for micro-cash long-option candidates. It is read-only/paper-only; stale, expired, or over-budget contracts are not actionable.</p>
        {_html_table(["Status", "Score", "Symbol", "Strategy", "Exp/DTE", "Strike", "Debit", "Spread", "Vol/OI", "Reasons"], micro_option_candidate_rows, empty_text="No M247 micro option candidate rows")}
      </article>
      <article class="card span-8" id="paper_option_ticket_preview">
        <h3>Paper Option Ticket Preview</h3>
        <div class="metric">{_html_text(paper_option_ticket_preview.get("status") or "not configured")}</div>
        <p>
          {_html_badge(f"tickets {paper_option_ticket_preview.get('ticket_count') if paper_option_ticket_preview else '-'}")}
          {_html_badge(f"ready {paper_option_ticket_preview.get('ready_ticket_count') if paper_option_ticket_preview else '-'}")}
          {_html_badge(f"blocked {paper_option_ticket_preview.get('blocked_ticket_count') if paper_option_ticket_preview else '-'}")}
          {_html_badge(f"single {paper_option_ticket_preview.get('single_leg_ticket_count') if paper_option_ticket_preview else '-'}")}
          {_html_badge(f"multi-leg {paper_option_ticket_preview.get('multi_leg_ticket_count') if paper_option_ticket_preview else '-'}")}
          {_html_badge(f"max ticket {_html_num(paper_option_ticket_preview.get('max_ticket_debit'))}")}
        </p>
        <p class="muted">
          Source M247 {_html_text((paper_option_ticket_preview.get("source") or {}).get("m247_status") or "-")}
          &middot; option budget {_html_num(paper_option_ticket_preview.get("option_budget"))}
          &middot; limit policy {_html_text(paper_option_ticket_preview.get("limit_price_policy") or "-")}
          &middot; fresh quote required {_html_text(paper_option_ticket_preview.get("require_fresh_quote"))}
        </p>
        <p class="muted small">M248 creates paper-only option ticket previews. Multi-leg spreads are shown here for review, but no broker order is submitted or transmitted.</p>
        <p class="muted small">Top blocks {_html_text(paper_option_ticket_preview.get("top_block_reason_counts") or {})}</p>
        {_html_table(["Ticket", "Symbol", "Strategy", "Type", "Legs", "Limit", "Debit", "Max loss", "Max ticket", "Transmit"], paper_option_ticket_rows, empty_text="No M248 paper option tickets")}
        {_html_table(["Symbol", "Strategy", "Status", "Score", "Limit", "Debit", "Reasons"], paper_option_ticket_review_rows, empty_text="No M248 candidate reviews")}
      </article>
      <article class="card span-8" id="option_whatif_safety_validation">
        <h3>Option What-If Safety Validation</h3>
        <div class="metric">{_html_text(option_whatif_safety_validation.get("status") or "not configured")}</div>
        <p>
          {_html_badge(f"passed {option_whatif_safety_validation.get('passed_count') if option_whatif_safety_validation else '-'}")}
          {_html_badge(f"pending {option_whatif_safety_validation.get('whatif_pending_count') if option_whatif_safety_validation else '-'}")}
          {_html_badge(f"blocked {option_whatif_safety_validation.get('blocked_count') if option_whatif_safety_validation else '-'}")}
          {_html_badge(f"broker required {option_whatif_safety_validation.get('require_broker_whatif') if option_whatif_safety_validation else '-'}")}
          {_html_badge(f"combo local {option_whatif_safety_validation.get('multi_leg_combo_enabled') if option_whatif_safety_validation else '-'}")}
          {_html_badge(f"broker combo {option_whatif_safety_validation.get('broker_combo_whatif_enabled') if option_whatif_safety_validation else '-'}")}
        </p>
        <p class="muted">
          Quote fresh {_html_text(option_whatif_safety_validation.get("quote_fresh"))}
          &middot; option session deferred {_html_text(option_whatif_safety_validation.get("option_session_expected_deferred"))}
          &middot; broker refresh now {_html_text(option_whatif_safety_validation.get("execute_whatif_refresh"))}
          &middot; combo scope {_html_text(option_whatif_safety_validation.get("multi_leg_combo_scope") or "-")}
        </p>
        <p class="muted small">
          Budgets {_html_text(option_whatif_safety_validation.get("budgets") or {})}
          &middot; broker source {_html_text(option_whatif_safety_validation.get("broker_whatif_source") or {})}
          &middot; combo prep {_html_text(option_whatif_safety_validation.get("broker_combo_whatif_preparation") or {})}
        </p>
        <p class="muted small">M249 validates local safety and optional broker what-if readiness. Broker combo what-if remains disabled until explicitly enabled; live routing stays locked.</p>
        {_html_table(["Ticket", "Symbol", "Strategy", "Legs", "Status", "Executable", "Local", "Broker", "Debit", "Reasons"], option_whatif_validation_rows, empty_text="No M249 option what-if validation rows")}
      </article>
      <article class="card span-8" id="limited_auto_trading_readiness">
        <h3>Limited Auto-Trading Readiness</h3>
        <div class="metric">{_html_text(limited_auto_trading_readiness.get("readiness_level") or "not configured")}</div>
        <p>
          {_html_badge(f"status {limited_auto_trading_readiness.get('status') or '-'}")}
          {_html_badge(f"hard {limited_auto_trading_readiness.get('hard_block_count') if limited_auto_trading_readiness else '-'}")}
          {_html_badge(f"trade-now {limited_auto_trading_readiness.get('trade_now_blocker_count') if limited_auto_trading_readiness else '-'}")}
          {_html_badge(f"live {limited_auto_trading_readiness.get('live_eligibility_blocker_count') if limited_auto_trading_readiness else '-'}")}
          {_html_badge(f"soft {limited_auto_trading_readiness.get('soft_warning_count') if limited_auto_trading_readiness else '-'}")}
          {_html_badge(f"deferred {limited_auto_trading_readiness.get('expected_deferred') if limited_auto_trading_readiness else '-'}")}
          {_html_badge(f"age {limited_auto_trading_readiness.get('age_seconds') if limited_auto_trading_readiness else '-'}s")}
        </p>
        <p>
          {_html_badge(f"posture {limited_auto_trading_readiness.get('gate_execution_posture') or '-'}")}
          {_html_badge(f"size x{_html_num(limited_auto_trading_readiness.get('gate_size_multiplier'))}")}
          {_html_badge(f"confidence x{_html_num(limited_auto_trading_readiness.get('gate_confidence_multiplier'))}")}
          {_html_badge(f"haircut {limited_auto_trading_readiness.get('gate_haircut_count') if limited_auto_trading_readiness else '-'}")}
          {_html_badge(f"confidence discount {limited_auto_trading_readiness.get('gate_confidence_discount_count') if limited_auto_trading_readiness else '-'}")}
          {_html_badge(f"monitoring {limited_auto_trading_readiness.get('gate_monitoring_only_count') if limited_auto_trading_readiness else '-'}")}
        </p>
        <p class="muted">
          Live routing {_html_text(limited_auto_trading_readiness.get("live_routing_status") or "locked")}
          &middot; live execution {_html_text(limited_auto_trading_readiness.get("live_execution_enabled"))}
          &middot; limited candidate {_html_text(limited_auto_trading_readiness.get("limited_auto_trade_candidate"))}
          &middot; paper execution {_html_text(limited_auto_trading_readiness.get("paper_execution_enabled"))}
          &middot; trade now {_html_text(limited_auto_trading_readiness.get("trade_now_allowed"))}
        </p>
        <p class="muted small">
          Max premium {_html_num(limited_auto_trading_readiness.get("max_allowed_premium"))}
          &middot; max single {_html_num(limited_auto_trading_readiness.get("max_allowed_single_position_notional"))}
          &middot; max new margin {_html_num(limited_auto_trading_readiness.get("max_allowed_new_initial_margin"))}
          &middot; strategies {_html_text(limited_auto_trading_readiness.get("eligible_strategy_types") or [])}
        </p>
        <p class="muted small">Gate severity meaning: hard block = true safety stop; trade-now blocker = no new entries now; haircut = reduce size; confidence discount = scorer confidence only; monitoring-only = show on dashboard without blocking. M250 does not submit, transmit, route, or mutate broker orders.</p>
        {_html_table(["Severity", "Code", "Source", "Message", "Value"], limited_auto_readiness_block_rows, empty_text="No M250 hard/soft readiness blocks")}
        {_html_table(["Type", "Step"], limited_auto_readiness_step_rows, empty_text="No M250 next steps")}
      </article>
      <article class="card span-4" id="defensive_action_plan">
        <h3>Defensive Action Plan</h3>
        <div class="metric">{_html_text(operating_defensive_plan.get("recommended_posture") or "not configured")}</div>
        <p>
          {_html_badge(f"actions {operating_defensive_plan.get('action_count') or 0}")}
          {_html_badge(f"priority {operating_defensive_plan.get('highest_priority_score') or 0}")}
          {_html_badge(f"chain {operating_defensive_plan.get('requires_option_chain_count') or 0}")}
          {_html_badge(f"what-if {operating_defensive_plan.get('requires_margin_whatif_count') or 0}")}
        </p>
        <p class="muted small">M186 turns the operating gate into paper-only defensive recommendations. It does not create tickets or broker orders.</p>
        {_html_table(["Priority", "Symbol", "Action", "Strategy", "Ticketing", "Chain", "What-if", "Reason"], defensive_action_rows, empty_text="No defensive action candidates")}
      </article>
      <article class="card span-4" id="defensive_intent_ledger">
        <h3>Defensive Intent Ledger</h3>
        <div class="metric">{_html_text(operating_defensive_ledger.get("recommended_posture") or "not configured")}</div>
        <p>
          {_html_badge(f"pending {operating_defensive_ledger.get('pending_defensive_paper_execution_count') or 0}")}
          {_html_badge(f"blocked {operating_defensive_ledger.get('blocked_count') or 0}")}
          {_html_badge(f"strict {operating_defensive_ledger.get('strict_gate_mode')}")}
          {_html_badge(f"what-if blocks {operating_defensive_ledger.get('missing_whatif_block_count') or 0}")}
        </p>
        <p class="muted small">M187 narrows M186 actions into paper-only defensive intents after what-if and option quote gates. It never transmits orders.</p>
        {_html_table(["State", "Symbol", "Strategy", "Priority", "Blocks", "Paper action", "Qty"], defensive_intent_rows, empty_text="No defensive intent rows")}
      </article>
"""


def _render_dashboard_macro_learning_cards(ctx: dict[str, Any]) -> str:
    snapshot = ctx["snapshot"]
    macro = ctx["macro"]
    macro_release_interpreter = ctx["macro_release_interpreter"]
    evidence_freshness = ctx["evidence_freshness"]
    evidence_guard = ctx["evidence_guard"]
    evidence_auto_recovery = ctx["evidence_auto_recovery"]
    source_ingest_health = ctx["source_ingest_health"]
    source_online_supervisor = ctx["source_online_supervisor"]
    m236_news_learning = ctx["m236_news_learning"]
    learning_pipeline = ctx["learning_pipeline"]
    live_scorer_learning = ctx["live_scorer_learning"]
    live_learning_llm_notes = ctx["live_learning_llm_notes"]
    m232_hot_cache = ctx["m232_hot_cache"]
    threshold_candidate_evaluator = ctx["threshold_candidate_evaluator"]
    promotion_gate_rows = ctx["promotion_gate_rows"]
    recycle_state_rows = ctx["recycle_state_rows"]
    recycle_graduation = ctx["recycle_graduation"]
    recycle_graduation_rows = ctx["recycle_graduation_rows"]
    singleton_locks = ctx["singleton_locks"]
    singleton_truth_rows = ctx["singleton_truth_rows"]
    singleton_rows = ctx["singleton_rows"]
    singleton_service_group_rows = ctx["singleton_service_group_rows"]
    runtime_version_guard = ctx["runtime_version_guard"]
    operational_runbook = ctx["operational_runbook"]
    operational_runbook_rows = ctx["operational_runbook_rows"]
    operational_runbook_command_rows = ctx["operational_runbook_command_rows"]
    safety_latency = ctx["safety_latency"]
    latency_rows = ctx["latency_rows"]
    latency_source_rows = ctx["latency_source_rows"]
    return f"""      <article class="card span-4" id="macro">
        <h3>Macro / economic</h3>
        <p>{_html_badge(macro.get("risk_bias") or "unknown")}</p>
        <p class="muted">Series {_html_text(macro.get("covered_series_count"))}/{_html_text(macro.get("series_count"))} &middot; Missing {_html_text(macro.get("missing_series_count"))}</p>
        <ul>{''.join(f'<li>{_html_text(item)}</li>' for item in (macro.get("scorer_takeaways") or [])[:5]) or '<li class="muted">No takeaways</li>'}</ul>
      </article>
      <article class="card span-4" id="macro_release_interpreter">
        <h3>Macro Releases / Calendar</h3>
        <div class="metric">{_html_text(macro_release_interpreter.get("latest_macro_surprise_label") or "not configured")}</div>
        <p class="muted small">{_html_text(macro_release_interpreter.get("latest_event_line") or "No parsed macro release yet")}</p>
        <p>
          {_html_badge(f"parsed {macro_release_interpreter.get('parsed_indicator_count') or 0}")}
          {_html_badge(f"clusters {macro_release_interpreter.get('cluster_count') or 0}")}
          {_html_badge(f"reaction {macro_release_interpreter.get('market_reaction_status') or '-'}")}
          {_html_badge(f"critical {macro_release_interpreter.get('critical_event_count') or 0}")}
        </p>
        <p class="muted small">Asset bias {_html_text(macro_release_interpreter.get("asset_bias") or {})}</p>
        <p class="muted small">Next: {_html_text((macro_release_interpreter.get("next_event") or {}).get("event_time_local") or "-")} &middot; {_html_text((macro_release_interpreter.get("next_event") or {}).get("name") or "-")}</p>
        <p class="muted small">Calendar updated {_html_text(macro_release_interpreter.get("calendar_generated_at_local") or "-")}</p>
      </article>
      <article class="card span-4" id="evidence_freshness">
        <h3>Evidence freshness</h3>
        <div class="metric">{_html_text(evidence_freshness.get("overall_status") or "not configured")}</div>
        <p>Guard {_html_badge(evidence_guard.get("action") or "-")} &middot; Stale {_html_text(len(evidence_freshness.get("stale_source_keys") or []))}</p>
        <p class="muted">Counts {_html_text(evidence_freshness.get("status_counts") or {})}</p>
      </article>
      <article class="card span-4" id="auto_recovery">
        <h3>Auto-recovery watchdog</h3>
        <div class="metric">{_html_text(evidence_auto_recovery.get("status") or "not configured")}</div>
        <p>Freshness {_html_badge(evidence_auto_recovery.get("last_freshness_status") or "-")} &middot; Last refresh {_html_badge(evidence_auto_recovery.get("last_refresh_status") or "-")} &middot; Failures {_html_text(evidence_auto_recovery.get("consecutive_failures") or 0)} &middot; Current gap resolved {_html_badge((snapshot.get("status") or {}).get("evidence_auto_recovery_gap_currently_resolved"))}</p>
        <p class="muted">Stale {_html_text(",".join(evidence_auto_recovery.get("last_stale_source_keys") or []) or "-")} &middot; Lagging {_html_text(",".join(evidence_auto_recovery.get("last_lagging_source_keys") or []) or "-")}</p>
        <p class="muted">Next eligible {_html_text(evidence_auto_recovery.get("next_refresh_eligible_at") or "-")}</p>
      </article>
      <article class="card span-4" id="source_doctor">
        <h3>Data Source Doctor</h3>
        <div class="metric">{_html_text(source_ingest_health.get("overall_status") or "not configured")}</div>
        <p>Online gaps {_html_text(len(source_ingest_health.get("online_problem_source_keys") or []))} &middot; Degraded {_html_text(len(source_ingest_health.get("degraded_source_keys") or []))}</p>
        <p class="muted">Root causes {_html_text(source_ingest_health.get("root_cause_counts") or {})}</p>
      </article>
      <article class="card span-4" id="source_supervisor">
        <h3>Source Online Supervisor</h3>
        <div class="metric">{_html_text(source_online_supervisor.get("final_status") or "not configured")}</div>
        <p>Live validation {_html_badge(source_online_supervisor.get("final_live_validation_status") or "-")} &middot; Source health {_html_badge(source_online_supervisor.get("final_source_ingest_health_status") or "-")}</p>
        <p class="muted small">{_html_text((source_online_supervisor.get("status_explanation") or {}).get("operator_summary") or "-")}</p>
        <p class="muted small">Yellow reasons {_html_text((source_online_supervisor.get("status_explanation") or {}).get("yellow_reasons") or [])}</p>
        <p class="muted small">Alpha Vantage disabled {_html_badge(str((source_online_supervisor.get("final_alpha_vantage_disabled") or {}).get("disabled", False)).lower())} &middot; {_html_text((source_online_supervisor.get("final_alpha_vantage_disabled") or {}).get("reason") or "-")}</p>
        <p class="muted">Actions {_html_text(source_online_supervisor.get("final_action_status_counts") or {})}</p>
        <p class="muted small">Latest tick local {_html_text((source_online_supervisor.get("latest_tick") or {}).get("checked_at_local") or _local_time_text((source_online_supervisor.get("latest_tick") or {}).get("checked_at")))} &middot; raw {_html_text((source_online_supervisor.get("latest_tick") or {}).get("checked_at") or "-")}</p>
        <p class="muted small">Run local {_html_text(source_online_supervisor.get("started_at_local") or _local_time_text(source_online_supervisor.get("started_at")))} → {_html_text(source_online_supervisor.get("completed_at_local") or _local_time_text(source_online_supervisor.get("completed_at") or source_online_supervisor.get("updated_at")))}</p>
      </article>
      <article class="card span-4" id="m236_news_learning">
        <h3>M236 News Learning</h3>
        <div class="metric">{_html_text(m236_news_learning.get("status") or "not configured")}</div>
        <p>
          {_html_badge(f"records {m236_news_learning.get('record_count') or 0}")}
          {_html_badge(f"ticker review {m236_news_learning.get('ticker_review_queue_count') or 0}")}
          {_html_badge(f"repeats {m236_news_learning.get('repeated_canonical_event_count') or 0}")}
        </p>
        <p class="muted small">Statuses {_html_text(m236_news_learning.get("status_counts") or {})} &middot; due {_html_text(m236_news_learning.get("due_horizon_counts") or {})}</p>
        <p class="muted small">Retention tail {_html_text(m236_news_learning.get("tail_record_count") or 0)} &middot; evaluated index {_html_text(m236_news_learning.get("evaluated_index_record_count") or 0)} &middot; retained {_html_text(m236_news_learning.get("evaluated_retained_record_count") or 0)}</p>
        <p class="muted small">Velocity {_html_text(m236_news_learning.get("learning_velocity") or {})}</p>
        <p class="muted small">Scorer directives {_html_text(m236_news_learning.get("scorer_directive_counts") or {})}</p>
        <p class="muted small">Ticker buckets {_html_text(m236_news_learning.get("ticker_confidence_buckets") or {})}</p>
        <p class="muted small">Ticker review buckets {_html_text(m236_news_learning.get("ticker_review_bucket_counts") or {})}</p>
        <p class="muted small">Macro release {_html_text((m236_news_learning.get("macro_release_integration") or {}).get("latest_macro_surprise_label") or "-")} &middot; near latest {_html_text((m236_news_learning.get("macro_release_integration") or {}).get("macro_records_near_latest_release_count") or 0)}</p>
        <p class="muted small">Role-family readiness {_html_text(m236_news_learning.get("role_family_readiness") or {})}</p>
        <p class="muted small">Top providers {_html_text([{ "family": item.get("family"), "weight": item.get("overall_weight"), "samples": item.get("sample_count") } for item in (m236_news_learning.get("top_providers") or [])[:4]])}</p>
        <p class="muted small">Updated local {_html_text(m236_news_learning.get("generated_at_local") or "-")}</p>
      </article>
      <article class="card span-4" id="learning_pipeline_health">
        <h3>Learning Pipeline Health</h3>
        <div class="metric">{_html_text(learning_pipeline.get("label") or "not configured")}</div>
        <p>
          {_html_badge(f"status {learning_pipeline.get('status') or '-'}")}
          {_html_badge(f"healthy {learning_pipeline.get('healthy')}")}
          {_html_badge(f"m225 backlog {learning_pipeline.get('m225_backlog_count') or 0}")}
          {_html_badge(f"m225 next {learning_pipeline.get('m225_next_interval_seconds') or '-'}s")}
          {_html_badge(f"m226 ready {learning_pipeline.get('m226_promotion_ready_count') or 0}")}
          {_html_badge(f"m232 {learning_pipeline.get('m232_hot_cache_status') or '-'}")}
          {_html_badge(f"m232 usable {learning_pipeline.get('m232_hot_cache_usable')}")}
          {_html_badge(f"m232 scorer read {learning_pipeline.get('m232_scorer_read_status') or '-'}")}
        </p>
        <p class="muted small">Option session {_html_text((learning_pipeline.get("option_session") or {}).get("session") or "-")} &middot; ET {_html_text((learning_pipeline.get("option_session") or {}).get("checked_at_et") or "-")}</p>
        <p class="muted small">Warnings {_html_text(learning_pipeline.get("warnings") or [])} &middot; blockers {_html_text(learning_pipeline.get("blocking_reasons") or [])}</p>
        <p class="muted small">M226 option deferred/stale {_html_text(learning_pipeline.get("m226_expected_deferred_option_count") or 0)}/{_html_text(learning_pipeline.get("m226_stale_option_count") or 0)} &middot; gate block/watch {_html_text(learning_pipeline.get("m226_gate_blocked_count") or 0)}/{_html_text(learning_pipeline.get("m226_gate_watchlist_count") or 0)}</p>
        <p class="muted small">M232 scorer read cache-age {_html_text(learning_pipeline.get("m232_age_at_last_scorer_read_seconds") if learning_pipeline.get("m232_age_at_last_scorer_read_seconds") is not None else "-")}s &middot; scorer report age {_html_text(learning_pipeline.get("m232_scorer_report_age_seconds") if learning_pipeline.get("m232_scorer_report_age_seconds") is not None else "-")}s &middot; changed {_html_text(learning_pipeline.get("m232_hot_cache_changed_keys") or [])}</p>
      </article>
      <article class="card span-4" id="live_learning">
        <h3>M223 Live Learning</h3>
        <div class="metric">{_html_text(live_scorer_learning.get("status") or "not configured")}</div>
        <p>
          {_html_badge("shadow only" if live_scorer_learning.get("shadow_only", True) else "promotion enabled")}
          {_html_badge(f"pred {live_scorer_learning.get('prediction_count') or 0}")}
          {_html_badge(f"v2 {live_scorer_learning.get('evidence_packet_v2_prediction_count') or 0}")}
          {_html_badge(f"q {round(float(live_scorer_learning.get('avg_packet_quality_score') or 0), 1)}")}
          {_html_badge(f"eval {live_scorer_learning.get('evaluated_outcome_count') or 0}")}
          {_html_badge(f"notes {live_scorer_learning.get('adjustment_note_count') or 0}")}
        </p>
        <p class="muted small">Pending / blocked outcomes {_html_text(live_scorer_learning.get("pending_outcome_count") or 0)} / {_html_text(live_scorer_learning.get("blocked_outcome_count") or 0)} &middot; candidate samples {_html_text(live_scorer_learning.get("candidate_sample_count") or 0)} &middot; hit rate {_html_text(live_scorer_learning.get("candidate_hit_rate") if live_scorer_learning.get("candidate_hit_rate") is not None else "-")}</p>
        <p class="muted small">Quality {_html_text(live_scorer_learning.get("packet_quality_counts") or {})} &middot; mapping {_html_text(live_scorer_learning.get("asset_mapping_confidence_counts") or {})} &middot; time {_html_text(live_scorer_learning.get("timestamp_alignment_counts") or {})} late {_html_text(live_scorer_learning.get("late_baseline_count") or 0)}</p>
        <p class="muted small">Packet {_html_text(live_scorer_learning.get("evidence_packet_version") or "-")} &middot; Scorer {_html_text(live_scorer_learning.get("scorer_name") or "-")} / {_html_text(live_scorer_learning.get("scorer_version") or "-")} &middot; updated {_html_text(live_scorer_learning.get("completed_at_local") or "-")}</p>
      </article>
      <article class="card span-4" id="m232_control_plane">
        <h3>M232 Control Plane</h3>
        <div class="metric">{_html_text(m232_hot_cache.get("status") or "not configured")}</div>
        <p>
          {_html_badge(f"usable {m232_hot_cache.get('usable')}")}
          {_html_badge(f"age {m232_hot_cache.get('age_seconds') if m232_hot_cache.get('age_seconds') is not None else '-'}s")}
          {_html_badge(f"tier {m232_hot_cache.get('adaptive_tier') or '-'}")}
          {_html_badge(f"next {m232_hot_cache.get('adaptive_next_interval_seconds') or '-'}s")}
          {_html_badge(f"changed {len(m232_hot_cache.get('adaptive_changed_keys') or [])}")}
          {_html_badge(f"paper scorer {m232_hot_cache.get('paper_scorer_ready')}")}
        </p>
        <p class="muted small">Conditional overlay {_html_text(m232_hot_cache.get("conditional_overlay_available"))} &middot; gate wait/watch/block {_html_text(m232_hot_cache.get("waitlist_count") or 0)}/{_html_text(m232_hot_cache.get("watchlist_count") or 0)}/{_html_text(m232_hot_cache.get("blocked_count") or 0)}</p>
        <p class="muted small">Reflex candidates/memories {_html_text(m232_hot_cache.get("reflex_candidate_count") or 0)}/{_html_text(m232_hot_cache.get("reflex_memory_count") or 0)} &middot; option memories {_html_text(m232_hot_cache.get("option_memory_count") or 0)} &middot; action paper/block {_html_text(m232_hot_cache.get("action_gate_paper_entry_candidate_count") or 0)}/{_html_text(m232_hot_cache.get("action_gate_block_or_second_pass_count") or 0)}</p>
        <p class="muted small">Option strength {_html_text(m232_hot_cache.get("option_scorer_use_strength_counts") or {})} &middot; threshold {_html_text(m232_hot_cache.get("option_threshold_candidate_context_count") or 0)} &middot; context-only {_html_text(m232_hot_cache.get("option_context_only_count") or 0)}</p>
        <p class="muted small">Source counts {_html_text(m232_hot_cache.get("source_counts") or {})} &middot; changed keys {_html_text(m232_hot_cache.get("adaptive_changed_keys") or [])}</p>
        <p class="muted small">Updated local {_html_text(m232_hot_cache.get("generated_at_local") or "-")} &middot; max age {_html_text(m232_hot_cache.get("max_consumer_age_seconds") or "-")}s &middot; full next due {_html_text(m232_hot_cache.get("full_report_next_due_in_seconds") if m232_hot_cache.get("full_report_next_due_in_seconds") is not None else "-")}s</p>
      </article>
      <article class="card span-4" id="llm_notes">
        <h3>M225 LLM Notes</h3>
        <div class="metric">{_html_text(live_learning_llm_notes.get("version") or "not configured")}</div>
        <p>
          {_html_badge("shadow only" if live_learning_llm_notes.get("shadow_only", True) else "promotion enabled")}
          {_html_badge(f"status {live_learning_llm_notes.get('status') or '-'}")}
          {_html_badge(f"backlog {live_learning_llm_notes.get('backlog_count') or 0}")}
          {_html_badge(f"tier {(live_learning_llm_notes.get('adaptive_schedule') or {}).get('tier') or '-'}")}
          {_html_badge(f"limit {(live_learning_llm_notes.get('adaptive_schedule') or {}).get('effective_limit') or '-'}")}
          {_html_badge(f"candidates {live_learning_llm_notes.get('candidate_count') or 0}")}
          {_html_badge(f"created {live_learning_llm_notes.get('created_count') or 0}")}
          {_html_badge(f"rejected {live_learning_llm_notes.get('rejected_count') or 0}")}
          {_html_badge(f"errors {live_learning_llm_notes.get('error_count') or 0}")}
          {_html_badge(f"groups {(live_learning_llm_notes.get('threshold_candidate_summary') or {}).get('candidate_group_count') or 0}")}
        </p>
        <p class="muted small">Quality {_html_text(live_learning_llm_notes.get("note_quality_counts_this_tick") or {})} &middot; providers {_html_text((live_learning_llm_notes.get("note_counts") or {}).get("provider_counts") or {})}</p>
        <p class="muted small">Adaptive next interval {_html_text((live_learning_llm_notes.get("adaptive_schedule") or {}).get("next_interval_seconds") or "-")}s &middot; cooldown {_html_text(live_learning_llm_notes.get("cooldown_reasons") or [])} &middot; next {_html_text(live_learning_llm_notes.get("next_eligible_at_local") or "-")} &middot; consecutive errors {_html_text(live_learning_llm_notes.get("consecutive_error_ticks") or 0)}</p>
        <p class="muted small">Model {_html_text(live_learning_llm_notes.get("model") or "-")} &middot; note {_html_text(live_learning_llm_notes.get("note_version") or "-")} &middot; updated {_html_text(live_learning_llm_notes.get("completed_at_local") or "-")}</p>
      </article>
      <article class="card span-4" id="threshold_candidate_evaluator">
        <h3>M226 Threshold Evaluator</h3>
        <div class="metric">{_html_text(threshold_candidate_evaluator.get("version") or "not configured")}</div>
        <p>
          {_html_badge("shadow only" if threshold_candidate_evaluator.get("shadow_only", True) else "promotion enabled")}
          {_html_badge(f"candidates {threshold_candidate_evaluator.get('candidate_count') or 0}")}
          {_html_badge(f"families {threshold_candidate_evaluator.get('semantic_family_count') or 0}")}
          {_html_badge(f"tier {(threshold_candidate_evaluator.get('adaptive_refresh') or {}).get('tier') or '-'}")}
          {_html_badge(f"ready {threshold_candidate_evaluator.get('promotion_ready_count') or 0}")}
          {_html_badge(f"trial {(threshold_candidate_evaluator.get('shadow_promotion_trial') or {}).get('active_count') or 0}")}
          {_html_badge(f"gate wait {(threshold_candidate_evaluator.get('promotion_gate') or {}).get('waitlist_count') or 0}")}
          {_html_badge(f"gate watch {(threshold_candidate_evaluator.get('promotion_gate') or {}).get('watchlist_count') or 0}")}
          {_html_badge(f"gate block {(threshold_candidate_evaluator.get('promotion_gate') or {}).get('blocked_count') or 0}")}
          {_html_badge(f"needs {threshold_candidate_evaluator.get('needs_more_samples_count') or 0}")}
          {_html_badge(f"watch {threshold_candidate_evaluator.get('watch_count') or 0}")}
          {_html_badge(f"shadow {threshold_candidate_evaluator.get('shadow_candidate_count') or 0}")}
          {_html_badge(f"reject {threshold_candidate_evaluator.get('reject_count') or 0}")}
        </p>
        <p class="muted small">Status {_html_text(threshold_candidate_evaluator.get("status_counts") or {})} &middot; blockers {_html_text(threshold_candidate_evaluator.get("blocker_counts") or {})} &middot; severity {_html_text(threshold_candidate_evaluator.get("blocker_severity_counts") or {})}</p>
        <p class="muted small">M225 focus {_html_text((threshold_candidate_evaluator.get("m225_focus_feedback") or {}).get("focus_pattern_count") or 0)} &middot; top family {_html_text((((threshold_candidate_evaluator.get("m225_focus_feedback") or {}).get("focus_patterns") or [{}])[0]).get("semantic_family_key") or "-")}</p>
        <p class="muted small">Shadow trial active {_html_text((threshold_candidate_evaluator.get("shadow_promotion_trial") or {}).get("active_count") or 0)} &middot; ready {_html_text((threshold_candidate_evaluator.get("shadow_promotion_trial") or {}).get("ready_for_real_promotion_count") or 0)} &middot; cycle new/continued/disappeared {_html_text((threshold_candidate_evaluator.get("cycle_report") or {}).get("new_family_count") or 0)}/{_html_text((threshold_candidate_evaluator.get("cycle_report") or {}).get("continued_family_count") or 0)}/{_html_text((threshold_candidate_evaluator.get("cycle_report") or {}).get("disappeared_family_count") or 0)}</p>
        <p class="muted small">Promotion Gate waitlist {_html_text((threshold_candidate_evaluator.get("promotion_gate") or {}).get("waitlist_count") or 0)} &middot; watchlist {_html_text((threshold_candidate_evaluator.get("promotion_gate") or {}).get("watchlist_count") or 0)} &middot; blocked {_html_text((threshold_candidate_evaluator.get("promotion_gate") or {}).get("blocked_count") or 0)} &middot; block reasons {_html_text((threshold_candidate_evaluator.get("promotion_gate") or {}).get("blocked_reason_counts") or {})} &middot; watch reasons {_html_text((threshold_candidate_evaluator.get("promotion_gate") or {}).get("watchlist_reason_counts") or {})}</p>
        <p class="muted small">Promotion Gate watch detail {_html_text((threshold_candidate_evaluator.get("promotion_gate") or {}).get("watchlist_detail_reason_counts") or {})} &middot; requirements {_html_text((threshold_candidate_evaluator.get("promotion_gate") or {}).get("promotion_requirement_counts") or {})}</p>
        <p class="muted small">Promotion Gate stale watch {_html_text(((threshold_candidate_evaluator.get("promotion_gate") or {}).get("watchlist_aging") or {}).get("stale_watchlist_count") or 0)} &middot; conditional {_html_text((threshold_candidate_evaluator.get("promotion_gate") or {}).get("conditional_promotion_count") or 0)} &middot; transitions {_html_text(((threshold_candidate_evaluator.get("promotion_gate") or {}).get("transition_history") or {}).get("transition_count") or 0)} &middot; backtest {_html_text(((threshold_candidate_evaluator.get("promotion_gate") or {}).get("backtest_report") or {}).get("current_review_status_counts") or {})}</p>
        <p class="muted small">Promotion Gate operational {_html_text((threshold_candidate_evaluator.get("promotion_gate") or {}).get("operational_status_counts") or {})} &middot; action requests {_html_text((threshold_candidate_evaluator.get("promotion_gate") or {}).get("action_request_count") or 0)} &middot; alerts {_html_text((threshold_candidate_evaluator.get("promotion_gate") or {}).get("alert_signal_count") or 0)}</p>
        <p class="muted small">Promotion Gate option roots {_html_text(((threshold_candidate_evaluator.get("promotion_gate") or {}).get("option_confidence_root_causes") or {}).get("root_cause_counts") or {})} &middot; recycle blockers {_html_text(((threshold_candidate_evaluator.get("promotion_gate") or {}).get("recycle_learning") or {}).get("return_blocker_counts") or {})} &middot; recycle eligible/near {_html_text(((threshold_candidate_evaluator.get("promotion_gate") or {}).get("recycle_learning") or {}).get("eligible_to_return_count") or 0)}/{_html_text(((threshold_candidate_evaluator.get("promotion_gate") or {}).get("recycle_learning") or {}).get("near_return_count") or 0)} &middot; lifecycle {_html_text(((threshold_candidate_evaluator.get("promotion_gate") or {}).get("recycle_learning") or {}).get("lifecycle_status_counts") or {})} &middot; scorer policies {_html_text(((threshold_candidate_evaluator.get("promotion_gate") or {}).get("scorer_threshold_policy_summary") or {}).get("scorer_action_counts") or {})}</p>
        <p class="muted small">Recycle graduation terminal {_html_text(recycle_graduation.get("terminal_candidate_count") or 0)} &middot; near {_html_text(recycle_graduation.get("near_graduation_count") or 0)} &middot; historical lifecycle {_html_text(recycle_graduation.get("historical_latest_lifecycle_counts") or {})}</p>
        {_html_table(["Gate", "Operational", "Family", "Detail", "Need", "Age", "Stale", "Conditional"], promotion_gate_rows, empty_text="No Promotion Gate watchlist candidates")}
        {_html_table(["Candidate", "Recycle", "Lifecycle", "New samples", "Confidence ticks", "Quarantine", "Root causes", "Scorer action", "Mult"], recycle_state_rows, empty_text="No active recycle candidates")}
        {_html_table(["Candidate", "Lifecycle", "Operational", "New samples", "Confidence", "Blocked by", "Action"], recycle_graduation_rows, empty_text="No dormant/retired or near-graduation recycle candidates")}
        <p class="muted small">Refresh changed {_html_text((threshold_candidate_evaluator.get("adaptive_refresh") or {}).get("changed_since_last_tick"))} &middot; next {_html_text((threshold_candidate_evaluator.get("adaptive_refresh") or {}).get("next_interval_seconds") or "-")}s &middot; ticks {_html_text(threshold_candidate_evaluator.get("tick_count") or 1)}</p>
        <p class="muted small">Top {_html_text(((threshold_candidate_evaluator.get("top_candidates") or [{}])[0]).get("status") or "-")} score {_html_text(((threshold_candidate_evaluator.get("top_candidates") or [{}])[0]).get("promotion_score") or "-")} &middot; updated {_html_text(threshold_candidate_evaluator.get("evaluated_at_local") or "-")}</p>
      </article>

      <article class="card span-4" id="singleton_locks">
        <h3>Loop Heartbeat</h3>
        <div class="metric">{_html_text(singleton_locks.get("overall_status") or "unknown")}</div>
        <p>
          {_html_badge(f"online {singleton_locks.get('online_count') or 0}/{singleton_locks.get('lock_count') or 0}")}
          {_html_badge(f"stale/missing {singleton_locks.get('stale_or_missing_count') or 0}")}
          {_html_badge(f"zombie output {singleton_locks.get('zombie_output_stale_count') or 0}")}
          {_html_badge(f"dup blocks {singleton_locks.get('duplicate_block_count_recent') or 0}")}
          {_html_badge(f"groups {singleton_locks.get('service_group_count') or 0}")}
        </p>
        <p>
          {_html_badge(f"proc online {(singleton_locks.get('truth_summary') or {}).get('process_online_count') or 0}")}
          {_html_badge(f"lock files {(singleton_locks.get('truth_summary') or {}).get('lock_file_count') or 0}")}
          {_html_badge(f"artifact fresh {(singleton_locks.get('truth_summary') or {}).get('artifact_fresh_count') or 0}")}
          {_html_badge(f"fresh/no proc {(singleton_locks.get('truth_summary') or {}).get('fresh_artifact_without_process_count') or 0}")}
        </p>
        <p class="muted small">Status {_html_text(singleton_locks.get("status_counts") or {})} &middot; service groups {_html_text(singleton_locks.get("service_group_status_counts") or {})} &middot; generated {_html_text(singleton_locks.get("generated_at") or "-")} &middot; PID column is lock-holder writer PID; duplicate-looking Windows wrapper processes are ignored unless duplicate blocks or stale output are reported.</p>
        <p class="muted small">{_html_text((singleton_locks.get("truth_summary") or {}).get("interpretation") or "")}</p>
        {_html_table(["Signal", "Value"], singleton_truth_rows, empty_text="No truth-summary rows")}
        {_html_table(["Service", "Group status", "Writer PID", "PID running", "Artifact age", "Artifact stale", "Interpretation"], singleton_service_group_rows, empty_text="No service-group rows")}
        {_html_table(["Loop", "Status", "Writer PID", "Age s", "Age stale", "Artifact age", "Artifact stale", "CWD"], singleton_rows, empty_text="No singleton lock rows")}
      </article>

      <article class="card span-4" id="runtime_version_guard">
        <h3>Runtime Guard</h3>
        <div class="metric">{_html_text(runtime_version_guard.get("status") or "unknown")}</div>
        <p>
          {_html_badge(f"restart {runtime_version_guard.get('restart_recommended')}")}
          {_html_badge(f"checked {len(runtime_version_guard.get('checked_components') or [])}")}
        </p>
        <p class="muted small">Options supervisor version {_html_text(((runtime_version_guard.get("options_market_structure_supervisor") or {}).get("version")) or "-")} &middot; missing fields {_html_text(((runtime_version_guard.get("options_market_structure_supervisor") or {}).get("missing_fields")) or [])}</p>
        <p class="muted small">{_html_text(((runtime_version_guard.get("options_market_structure_supervisor") or {}).get("interpretation")) or "-")}</p>
      </article>

      <article class="card span-8" id="operational_runbook">
        <h3>Operational Runbook</h3>
        <div class="metric">{_html_text(operational_runbook.get("status") or "not configured")}</div>
        <p>{_html_badge(f"checks {len(operational_runbook.get('immediate_checks') or [])}")} {_html_badge(f"repo {operational_runbook.get('repo_root') or '-'}")}</p>
        <p class="muted small">Read-only runbook. It documents start/stop/verification flow and does not submit broker orders, transmit trades, or mutate live thresholds.</p>
        {_html_table(["Type", "Item"], operational_runbook_rows, empty_text="No runbook checks")}
        {_html_table(["Phase", "Command"], operational_runbook_command_rows, empty_text="No runbook commands")}
      </article>
      <article class="card span-4" id="safety_latency">
        <h3>Safety Gate Latency</h3>
        <div class="metric">{_html_text(safety_latency.get("status") or "not configured")}</div>
        <p>{_html_badge(f"total {safety_latency.get('total_elapsed_ms') if safety_latency.get('total_elapsed_ms') is not None else '-'} ms")} {_html_badge(f"stages {safety_latency.get('stage_count') or 0}")}</p>
        <p class="muted small">Measurement only. It does not skip gates, submit broker orders, or enable transmit.</p>
        {_html_table(["Stage", "Elapsed ms"], latency_rows, empty_text="No latency rows")}
        {_html_table(["Source", "Elapsed ms", "Loaded"], latency_source_rows, empty_text="No source latency rows")}
      </article>

"""


def _render_dashboard_positions_options_cards(ctx: dict[str, Any]) -> str:
    snapshot = ctx["snapshot"]
    paper_position_rows = ctx["paper_position_rows"]
    account_position_rows = ctx["account_position_rows"]
    options_desk = ctx["options_desk"]
    option_quote_gate = ctx["option_quote_gate"]
    option_position_risk = ctx["option_position_risk"]
    option_readiness = ctx["option_readiness"]
    option_readiness_rows = ctx["option_readiness_rows"]
    option_position_rows = ctx["option_position_rows"]
    option_group_rows = ctx["option_group_rows"]
    option_plan_rows = ctx["option_plan_rows"]
    advanced_option_rows = ctx["advanced_option_rows"]
    protective_option_rows = ctx["protective_option_rows"]
    single_stock_watchlist_rows = ctx["single_stock_watchlist_rows"]
    options_market_structure_supervisor = ctx["options_market_structure_supervisor"]
    runtime_version_guard = ctx["runtime_version_guard"]
    options_market_structure = ctx["options_market_structure"]
    m220_market_move_option_overlay = ctx["m220_market_move_option_overlay"]
    m232_hot_cache = ctx["m232_hot_cache"]
    m220_move_overlay_rows = ctx["m220_move_overlay_rows"]
    index_option_coverage_rows = ctx["index_option_coverage_rows"]
    family_usage_rows = ctx["family_usage_rows"]
    family_repair_rows = ctx["family_repair_rows"]
    m220_validation_rows = ctx["m220_validation_rows"]
    budget_tuning_rows = ctx["budget_tuning_rows"]
    option_quote_quality_rows = ctx["option_quote_quality_rows"]
    options_market_structure_rows = ctx["options_market_structure_rows"]
    options_market_structure_bucket_rows = ctx["options_market_structure_bucket_rows"]
    return f"""      <article class="card span-12" id="positions">
        <h3>Paper positions</h3>
        {_html_table(["Symbol", "Status", "Qty", "Mark", "Source", "Market value", "Unrealized PnL"], paper_position_rows, empty_text="No paper positions")}
      </article>
      <article class="card span-12">
        <h3>IBKR account positions</h3>
        {_html_table(["Symbol", "Type", "CCY", "Qty", "Avg cost", "Expiry", "Right", "Strike"], account_position_rows, empty_text="No account positions")}
      </article>
      <article class="card span-12" id="options_desk">
        <h3>Options Desk</h3>
        <p>
          {_html_badge(f"readiness {options_desk.get('readiness_level') or '-'}")}
          {_html_badge(f"score {options_desk.get('readiness_score') if options_desk.get('readiness_score') is not None else '-'}")}
          {_html_badge(f"quote {option_quote_gate.get('gate_action') or '-'}")}
          {_html_badge(f"quoted {option_quote_gate.get('best_option_quoted_count') if option_quote_gate.get('best_option_quoted_count') is not None else '-'}")}
          {_html_badge(f"positions {options_desk.get('account_option_position_count') or 0}")}
          {_html_badge(f"groups {options_desk.get('position_strategy_group_count') or 0}")}
          {_html_badge(f"risk red {option_position_risk.get('red_count') or 0}")}
          {_html_badge(f"short ITM {option_position_risk.get('short_itm_count') or 0}")}
          {_html_badge(f"expiring soon {option_position_risk.get('expiring_soon_count') or 0}")}
          {_html_badge(f"protective {options_desk.get('protective_candidate_count') or 0}")}
          {_html_badge(f"advanced {options_desk.get('advanced_strategy_count') or 0}")}
          {_html_badge("new option entries allowed" if options_desk.get("new_option_entries_allowed") else "new option entries blocked")}
          {_html_badge(f"session {options_desk.get('option_session_name') or '-'}")}
        </p>
        <p class="muted small">
          Allowed structures {_html_text(options_desk.get("allowed_option_structures") or [])}
          &middot; Bias {_html_text(options_desk.get("option_strategy_bias") or "-")}
          &middot; Regular-session dependent {_html_text(options_desk.get("option_quotes_regular_session_dependent"))}
          &middot; Quote fresh {_html_text(option_quote_gate.get("fresh"))}
          &middot; Quote session {_html_text(option_quote_gate.get("option_quote_session") or "-")}
          &middot; Quote deferred {_html_text(option_quote_gate.get("option_quote_deferred"))}
          &middot; Next regular open {_html_text(options_desk.get("next_regular_open") or "-")}
          &middot; Blocks {_html_text(options_desk.get("blocked_order_paths") or [])}
        </p>
        <p class="muted small">
          Position risk alerts {_html_text(option_position_risk.get("alert_counts") or {})}
          &middot; Moneyness unavailable {_html_text(option_position_risk.get("moneyness_unavailable_count") or 0)}
          &middot; Quote assessment blocked {_html_text(option_position_risk.get("quote_assessment_blocked"))}
          &middot; Quote alert {_html_text(option_position_risk.get("quote_assessment_alert") or "-")}
        </p>
        <p class="muted small">Readiness reasons {_html_text(options_desk.get("readiness_reasons") or [])}</p>
        <p class="muted small">This card is display and readiness only. Multi-leg strategies, spreads, collars, protective puts, and option-combo candidates still require quote/chain/margin gates before any later automated order path can release.</p>
        <h4>Readiness checks</h4>
        {_html_table(["Check", "Passed", "Impact", "Value", "Reason"], option_readiness_rows, empty_text="No option readiness checks")}
        <h4>Current account option positions</h4>
        {_html_table(["Contract", "Risk", "Side", "Qty", "DTE", "ITM", "Underlying", "Source", "Expiry", "Right", "Strike", "Alerts"], option_position_rows, empty_text="No account option positions")}
        <h4>Grouped / multi-leg position view</h4>
        {_html_table(["Strategy ID", "Risk", "Structure", "Legs", "Min DTE", "Short ITM", "Net contracts", "Alerts", "Leg detail"], option_group_rows, empty_text="No option strategy groups")}
        <h4>Option plan intents</h4>
        {_html_table(["Symbol", "Action", "Qty", "State", "Ticket", "Leg detail"], option_plan_rows, empty_text="No option plan intents")}
        <h4>Protective option candidates</h4>
        {_html_table(["Symbol", "Source action", "Strategy", "Analysis", "Ticketing", "Legs", "Leg detail", "Risk", "Reason"], protective_option_rows, empty_text="No protective option candidates")}
        <h4>Advanced option strategy candidates</h4>
        {_html_table(["Source", "Symbol", "Strategy", "Legs", "Ticket/state", "Checks", "Risk", "Reason"], advanced_option_rows, empty_text="No advanced option strategy candidates")}
      </article>
      <article class="card span-12" id="adaptive_single_stock_options">
        <h3>Adaptive Single-Stock Options Watchlist</h3>
        <p>
          {_html_badge((snapshot.get("single_stock_option_watchlist") or {}).get("status") or "not configured")}
          {_html_badge(f"selected {len((snapshot.get('single_stock_option_watchlist') or {}).get('selected_symbols') or [])}")}
          {_html_badge(f"tiers {(snapshot.get('single_stock_option_watchlist') or {}).get('tier_counts') or {}}")}
          {_html_badge(f"actions {(snapshot.get('single_stock_option_watchlist') or {}).get('action_status_counts') or {}}")}
        </p>
        <p class="muted small">
          Version {_html_text((snapshot.get("single_stock_option_watchlist") or {}).get("version") or "-")}
          &middot; generated {_html_text((snapshot.get("single_stock_option_watchlist") or {}).get("generated_at") or "-")}
          &middot; age {_html_text((snapshot.get("single_stock_option_watchlist") or {}).get("age_seconds"))}s
          &middot; selected {_html_text((snapshot.get("single_stock_option_watchlist") or {}).get("selected_symbols") or [])}
        </p>
        <p class="muted small">Institutional-style adaptive queue: event, regime/theme, core, position, option-quality, and learning feedback are combined. Analysis-only and quality-blocked names should not be treated as execution-ready.</p>
        {_html_table(["Symbol", "Tier", "Score", "TTL", "Permission", "QuoteQ", "OI proxy", "Gamma", "Outcomes", "Promotion", "Themes"], single_stock_watchlist_rows, empty_text="No adaptive single-stock option watchlist rows")}
      </article>
      <article class="card span-12" id="options_market_structure">
        <h3>Options Market Structure</h3>
        <p>
          {_html_badge(f"supervisor {options_market_structure_supervisor.get('final_status') or '-'}")}
          {_html_badge(f"session {options_market_structure_supervisor.get('final_session') or '-'}")}
          {_html_badge(f"phase {options_market_structure_supervisor.get('final_session_refresh_phase') or '-'}")}
          {_html_badge(f"core fresh {options_market_structure_supervisor.get('final_quote_fresh')}")}
          {_html_badge(f"core quality {options_market_structure_supervisor.get('final_quote_quality_status') or '-'}")}
          {_html_badge(f"core latency {options_market_structure_supervisor.get('final_quote_data_latency_status') or '-'}")}
          {_html_badge(f"index fresh {options_market_structure_supervisor.get('final_optional_index_quote_fresh')}")}
          {_html_badge(f"index quality {options_market_structure_supervisor.get('final_optional_index_quote_quality_status') or '-'}")}
          {_html_badge(f"index latency {options_market_structure_supervisor.get('final_optional_index_quote_data_latency_status') or '-'}")}
          {_html_badge(f"proxy fallback {len((options_market_structure_supervisor.get('m220_family_issue_summary') or {}).get('proxy_fallback_families') or [])}")}
          {_html_badge(f"probe-backed proxy {len((options_market_structure_supervisor.get('m220_family_issue_summary') or {}).get('proxy_probe_backed_families') or [])}")}
          {_html_badge(f"unavailable {len((options_market_structure_supervisor.get('m220_family_issue_summary') or {}).get('coverage_unavailable_families') or [])}")}
          {_html_badge(f"M222 logs {len(options_market_structure_supervisor.get('final_m222_option_structure_outcome_actions') or [])}")}
          {_html_badge(f"M220 move overlay {m220_market_move_option_overlay.get('overall_status') or '-'}")}
          {_html_badge(f"mapped moves {m220_market_move_option_overlay.get('mapped_move_count') or 0}")}
          {_html_badge(options_market_structure.get("status") or "not configured")}
          {_html_badge(f"quotes {options_market_structure.get('quote_count') or 0}")}
          {_html_badge(f"symbols {options_market_structure.get('symbol_count') or 0}")}
          {_html_badge(f"pin {((options_market_structure.get('summary') or {}).get('pin_risk_symbol_count') or 0)}")}
          {_html_badge(f"proximity {((options_market_structure.get('summary') or {}).get('proximity_alert_symbol_count') or 0)}")}
          {_html_badge(f"freshness {((options_market_structure.get('session_guard') or {}).get('freshness_status') or '-')}" )}
          {_html_badge("volume fallback on" if options_market_structure.get("fallback_to_volume") else "oi only")}
        </p>
        <p class="muted small">
          Supervisor core symbols {_html_text((options_market_structure_supervisor.get("config") or {}).get("symbols") or [])}
          &middot; optional index symbols {_html_text((options_market_structure_supervisor.get("config") or {}).get("optional_index_symbols") or [])}
          &middot; action counts {_html_text(options_market_structure_supervisor.get("final_action_status_counts") or {})}
          &middot; M222 outcome logs {_html_text(len(options_market_structure_supervisor.get("final_m222_option_structure_outcome_actions") or []))} failed {_html_text(options_market_structure_supervisor.get("final_m222_failed_action_count") or 0)}
          &middot; failure reasons {_html_text(options_market_structure_supervisor.get("final_failure_reason_counts") or {})}
          &middot; artifact delta {_html_text((options_market_structure_supervisor.get("final_artifact_delta") or {}).get("outcome") or "-")} / {_html_text((options_market_structure_supervisor.get("final_optional_index_artifact_delta") or {}).get("outcome") or "-")}
          &middot; symbol status {_html_text(len(options_market_structure_supervisor.get("symbol_status") or {}))}
          &middot; isolated {_html_text(options_market_structure_supervisor.get("isolated_symbol_count") or len(options_market_structure_supervisor.get("isolated_symbols_by_action") or {}))}
          &middot; delayed {_html_text(options_market_structure_supervisor.get("final_quote_delayed_contracts") or 0)} / {_html_text(options_market_structure_supervisor.get("final_optional_index_quote_delayed_contracts") or 0)}
          &middot; core quality reasons {_html_text(options_market_structure_supervisor.get("final_quote_quality_reason_counts") or {})}
          &middot; index quality reasons {_html_text(options_market_structure_supervisor.get("final_optional_index_quote_quality_reason_counts") or {})}
          &middot; index effective quality {_html_text(options_market_structure_supervisor.get("final_optional_index_effective_quote_quality_status") or "-")}
          &middot; DTE buckets core/index {_html_text(options_market_structure_supervisor.get("final_quote_dte_bucket_counts") or {})} / {_html_text(options_market_structure_supervisor.get("final_optional_index_quote_dte_bucket_counts") or {})}
          &middot; effective max age {_html_text(options_market_structure_supervisor.get("final_effective_max_snapshot_age_minutes") or "-")}m
          &middot; failed actions {_html_text(options_market_structure_supervisor.get("failed_action_count") or 0)}
          &middot; family coverage {_html_text(options_market_structure_supervisor.get("final_index_option_coverage_status") or "-")}
          &middot; issue categories {_html_text((options_market_structure_supervisor.get("m220_family_issue_summary") or {}).get("issue_category_counts") or {})}
          &middot; family use {_html_text((options_market_structure_supervisor.get("m220_family_issue_summary") or {}).get("effective_operating_status_counts") or {})}
          &middot; scorer strength {_html_text((options_market_structure_supervisor.get("m220_family_issue_summary") or {}).get("scorer_use_strength_counts") or {})}
          &middot; proxy fallback {_html_text((options_market_structure_supervisor.get("m220_family_issue_summary") or {}).get("proxy_fallback_families") or [])}
          &middot; probe-backed proxy {_html_text((options_market_structure_supervisor.get("m220_family_issue_summary") or {}).get("proxy_probe_backed_families") or [])}
          &middot; unavailable {_html_text((options_market_structure_supervisor.get("m220_family_issue_summary") or {}).get("coverage_unavailable_families") or [])}
          &middot; quote budgets {_html_text(options_market_structure_supervisor.get("final_quote_contract_budgets") or {})}
          &middot; family allocation {_html_text(options_market_structure_supervisor.get("index_family_quote_budget_allocation") or {})}
          &middot; deferred state {_html_text((options_market_structure_supervisor.get("option_quote_deferred_operating_state") or {}).get("status") or "-")}
          &middot; alert policy {_html_text((options_market_structure_supervisor.get("option_quote_deferred_operating_state") or {}).get("alert_policy") or "-")}
        </p>
        <p class="muted small">
          M220 one-command {_html_text((options_market_structure_supervisor.get("m220_operational_runbook") or {}).get("one_command") or "-")}
          &middot; move-option overlay {_html_text(m220_market_move_option_overlay.get("overall_status") or "-")}
          &middot; status counts {_html_text(m220_market_move_option_overlay.get("status_counts") or {})}
          &middot; note {_html_text(m220_market_move_option_overlay.get("summary_note") or "-")}
        </p>
        <p class="muted small">
          Source quote snapshot {_html_text(options_market_structure.get("source_quote_snapshot_version") or "-")}
          &middot; generated {_html_text(options_market_structure.get("source_quote_generated_at") or "-")}
          &middot; age minutes {_html_text((options_market_structure.get("session_guard") or {}).get("source_quote_age_minutes") if (options_market_structure.get("session_guard") or {}).get("source_quote_age_minutes") is not None else "-")}
          &middot; max age {_html_text((options_market_structure.get("session_guard") or {}).get("max_snapshot_age_minutes") or "-")}
          &middot; guard {_html_text((options_market_structure.get("session_guard") or {}).get("interpretation") or "-")}
          &middot; regimes {_html_text((options_market_structure.get("summary") or {}).get("gamma_regime_counts") or {})}
          &middot; confidence {_html_text((options_market_structure.get("summary") or {}).get("confidence_counts") or {})}
          &middot; expiry buckets {_html_text((options_market_structure.get("summary") or {}).get("expiry_bucket_counts") or {})}
          &middot; missing OI {_html_text((options_market_structure.get("summary") or {}).get("missing_open_interest_symbols") or [])}
        </p>
        <p class="muted small">
          Option learning strength {_html_text(m232_hot_cache.get("option_scorer_use_strength_counts") or {})}
          &middot; threshold candidates {_html_text(m232_hot_cache.get("option_threshold_candidate_context_count") or 0)}
          &middot; context-only {_html_text(m232_hot_cache.get("option_context_only_count") or 0)}
          &middot; option complete rows {_html_text(m232_hot_cache.get("option_complete_rows") or 0)}
        </p>
        <p class="muted small">Read-only market-structure overlay. Use it as context for newsless moves, late-day pin risk, expiration/0DTE sessions, and moves near call/put walls; do not treat it as the sole cause of a market move.</p>
        <details open>
          <summary>M220 market move option explanation</summary>
          {_html_table(["Market", "Family", "Move", "Option", "Source", "Gamma", "Explanation", "Walls", "Proximity", "Interpretation"], m220_move_overlay_rows, empty_text="No material mapped market move needs option-structure interpretation")}
        </details>
        <details open>
          <summary>Index / proxy coverage matrix</summary>
          {_html_table(["Family", "Direct", "Mini", "Proxy", "Selected", "Budget", "Coverage", "Confidence"], index_option_coverage_rows, empty_text="No index option coverage matrix")}
        </details>
        <details open>
          <summary>Family-level option usability</summary>
          {_html_table(["Family", "Effective status", "Scorer strength", "Selected", "Direct failure non-blocking", "Severity", "Summary"], family_usage_rows, empty_text="No family-level option usability rows")}
        </details>
        <details open>
          <summary>M220 family repair plan</summary>
          {_html_table(["Family", "Mode", "Severity", "Selected", "Issue categories", "Next action", "Summary"], family_repair_rows, empty_text="No M220 family repair plan")}
        </details>
        <details open>
          <summary>M220 live validation checklist</summary>
          {_html_table(["Family", "Role", "Symbols", "Passed", "Status", "Issue categories", "Next action"], m220_validation_rows, empty_text="No M220 live validation checklist")}
        </details>
        <details>
          <summary>Family budget tuning observations</summary>
          {_html_table(["Family", "Budget", "Selected", "Usable/requested", "Candidates/chains", "Not requested", "Suggestion"], budget_tuning_rows, empty_text="No family budget tuning observations")}
        </details>
        <details open>
          <summary>Option quote quality by symbol</summary>
          {_html_table(["Symbol", "Scope", "Status", "Quality", "Quoted/requested", "Rate", "Avg spread", "Latency", "Failures", "Isolation until", "Reason"], option_quote_quality_rows, empty_text="No option quote quality rows")}
        </details>
        <details>
          <summary>Symbol gamma / wall / pin table</summary>
          {_html_table(["Symbol", "Spot", "Gamma regime", "Total GEX", "Flip", "Call wall", "Put wall", "Max pain", "Pin dist %", "Alerts", "Confidence", "Score", "Warnings"], options_market_structure_rows, empty_text="No options market-structure report")}
        </details>
        <details>
          <summary>Expiry bucket pressure</summary>
          {_html_table(["Symbol", "Bucket", "Quotes", "DTE", "Gamma regime", "Total GEX", "Call wall", "Put wall", "Max pain", "Pin", "Alerts"], options_market_structure_bucket_rows, empty_text="No expiry-bucket market-structure rows")}
        </details>
      </article>

"""


def _render_dashboard_plan_evidence_source_cards(ctx: dict[str, Any]) -> str:
    plan = ctx["plan"]
    plan_rows = ctx["plan_rows"]
    protective_candidate_rows = ctx["protective_candidate_rows"]
    news_rows = ctx["news_rows"]
    geopolitical_heat_rows = ctx.get("geopolitical_heat_rows") or []
    geopolitical_desk = (ctx.get("snapshot") or {}).get("geopolitical_desk") or {}
    regime_rows = ctx["regime_rows"]
    evidence_rows = ctx["evidence_rows"]
    auto_recovery_rows = ctx["auto_recovery_rows"]
    source_doctor_rows = ctx["source_doctor_rows"]
    source_supervisor_rows = ctx["source_supervisor_rows"]
    market_data_rows = ctx["market_data_rows"]
    market_data_session_rows = ctx["market_data_session_rows"]
    market_data_desk = ctx["market_data_desk"]
    missing_html = ctx["missing_html"]
    source_rows = ctx["source_rows"]
    return f"""      <article class="card span-12" id="plans">
        <h3>Buy / sell plan</h3>
        <p>Pending paper execution <strong>{_html_text(plan.get("pending_paper_execution_count"))}</strong> &middot; M204 release blocks <strong>{_html_text(plan.get("blocked_by_session_transition_release_governor_count") or 0)}</strong> &middot; Action counts <code>{_html_text(plan.get("action_counts") or {})}</code></p>
        <p class="muted small">M242 capital-aware execution: required {_html_text(plan.get("require_capital_aware_gate"))} &middot; source {_html_text(plan.get("source_has_capital_aware_gate"))} &middot; ready/resized/blocked {_html_text(plan.get("source_capital_ready_count"))}/{_html_text(plan.get("source_capital_resized_count"))}/{_html_text(plan.get("source_capital_blocked_count"))}</p>
        {_html_table(["Symbol", "Action", "Qty", "State", "Ticket", "Capital", "Cap scale", "Risk gate"], plan_rows, empty_text="No pending plan rows")}
      </article>
      <article class="card span-12">
        <h3>Protective option analysis packet</h3>
        <p class="muted">Candidates remain analysis-only while market-data operating mode is defensive/red. No tickets or broker orders are created by this card.</p>
        {_html_table(["Symbol", "Source action", "Analysis", "Ticketing", "Reason"], protective_candidate_rows, empty_text="No protective analysis candidates")}
      </article>

      <article class="card span-12" id="news">
        <h3>Latest one-line news</h3>
        {_html_table(["Time", "Provider", "Symbol/Region", "Headline"], news_rows, empty_text="No latest news rows")}
      </article>

      <article class="card span-12" id="geopolitical_desk">
        <h3>Geopolitical Desk</h3>
        <p>
          {_html_badge("available" if geopolitical_desk.get("available") else "not configured")}
          {_html_badge(f"hot {geopolitical_desk.get('hot_bucket_count') if geopolitical_desk.get('hot_bucket_count') is not None else 0}")}
          {_html_badge(f"buckets {geopolitical_desk.get('bucket_count') or 0}")}
        </p>
        <p class="muted small">Heat colors {_html_text(geopolitical_desk.get("heat_color_counts") or {})} &middot; generated {_html_text(_local_time_text(geopolitical_desk.get("generated_at")))}</p>
        {_html_table(["Heat", "Score", "Region", "Theme", "Signals", "Urgency", "Assets", "Action", "Latest", "Headline"], geopolitical_heat_rows, empty_text="No geopolitical heat buckets")}
      </article>

      <article class="card span-12" id="regimes">
        <h3>Standing regimes</h3>
        {_html_table(["Regime", "Status", "Family", "Region", "Bias", "Severity", "Confidence", "Effective", "Freshness", "Age d", "Last seen"], regime_rows, empty_text="No active regimes")}
      </article>
      <article class="card span-12">
        <h3>Evidence Freshness Desk</h3>
        {_html_table(["Source", "Status", "Age min", "Scorer use", "Weight", "Latest event"], evidence_rows, empty_text="Evidence freshness not configured")}
      </article>
      <article class="card span-12">
        <h3>Evidence Auto-Recovery Watchdog</h3>
        {_html_table(["Tick", "Status", "Freshness", "Stale", "Lagging", "Cooldown remaining"], auto_recovery_rows, empty_text="Evidence auto-recovery watchdog not configured")}
      </article>
      <article class="card span-12">
        <h3>Data Source Doctor</h3>
        {_html_table(["Source", "Online", "Freshness", "Health", "Age min", "Root cause", "Action"], source_doctor_rows, empty_text="Source ingest health not configured")}
      </article>
      <article class="card span-12">
        <h3>Source Online Supervisor latest actions</h3>
        {_html_table(["Action", "Source", "Status", "Priority", "Next eligible local", "Description"], source_supervisor_rows, empty_text="Source supervisor not configured")}
      </article>

      <article class="card span-12">
        <h3>Market Data Desk watchlist</h3>
        {_html_table(["Key", "Group", "Price", "Gap %", "Data", "Candidate", "Delayed", "Alerts", "Status"], market_data_rows, empty_text="No market-data rows")}
      </article>
      <article class="card span-12">
        <h3>Market Data Session monitorability</h3>
        <p class="muted">This separates overnight-monitorable stock/ETF, FX/futures/crypto-style rows from regular-session-dependent option/liquidity interpretation.</p>
        {_html_table(["Key", "Bucket", "Monitorability", "Price", "Price present", "Delayed", "Alerts", "Status"], market_data_session_rows, empty_text="No market-data session overlay rows")}
      </article>

      <article class="card span-6">
        <h3>Market-data desk summary</h3>
        <pre class="small">{_html_text(json.dumps(market_data_desk.get("summary") or {}, ensure_ascii=False, indent=2))}</pre>
      </article>
      <article class="card span-6" id="logs">
        <h3>Sources / missing notes</h3>
        {missing_html}
      </article>
      <article class="card span-12">
        <h3>Source files</h3>
        {_html_table(["Source", "Loaded", "Error", "Path"], source_rows, empty_text="No source metadata")}
      </article>
"""


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def _local_time_text(value: Any, default: str = "-") -> str:
    if value in (None, ""):
        return default
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value or default)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")




def _append_markdown_positions_options_sections(lines: list[str], ctx: dict[str, Any]) -> None:
    m220_market_move_option_overlay = ctx['m220_market_move_option_overlay']
    options_desk = ctx['options_desk']
    options_market_structure = ctx['options_market_structure']
    options_market_structure_supervisor = ctx['options_market_structure_supervisor']
    positions = ctx['positions']
    runtime_version_guard = ctx['runtime_version_guard']
    single_stock_option_watchlist = ctx['single_stock_option_watchlist']
    lines.extend(
        [
            "",
            "## Current positions",
            "",
            "| Symbol | Source | Qty | Mark | Market value | Unrealized PnL |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in positions.get("paper_positions") or []:
        lines.append(
            f"| {row.get('symbol')} | paper | {row.get('open_net_quantity')} | {row.get('latest_mark_price')} | {row.get('open_market_value')} | {row.get('unrealized_pnl')} |"
        )
    for row in positions.get("account_positions") or []:
        lines.append(
            f"| {row.get('symbol')} | account/{row.get('sec_type')} | {row.get('position')} | - | - | - |"
        )
    if not (positions.get("paper_positions") or positions.get("account_positions")):
        lines.append("| - | - | - | - | - | - |")
    readiness = options_desk.get("readiness") or {}
    quote_gate = options_desk.get("quote_gate") or {}
    option_position_risk = options_desk.get("position_risk_summary") or {}
    lines.extend(
        [
            "",
            "## Options Desk",
            "",
            f"- Readiness: `{options_desk.get('readiness_level') or '-'}` score `{options_desk.get('readiness_score') if options_desk.get('readiness_score') is not None else '-'}`",
            f"- Quote gate: `{quote_gate.get('gate_action') or '-'}` fresh `{quote_gate.get('fresh')}` quoted `{quote_gate.get('best_option_quoted_count') if quote_gate.get('best_option_quoted_count') is not None else '-'}` session `{quote_gate.get('option_quote_session') or '-'}` deferred `{quote_gate.get('option_quote_deferred')}`",
            f"- Position risk: red `{option_position_risk.get('red_count') or 0}` yellow `{option_position_risk.get('yellow_count') or 0}` green `{option_position_risk.get('green_count') or 0}` short ITM `{option_position_risk.get('short_itm_count') or 0}` expiring soon `{option_position_risk.get('expiring_soon_count') or 0}` quote assessment blocked `{option_position_risk.get('quote_assessment_blocked')}`",
            f"- Position risk alerts: `{option_position_risk.get('alert_counts') or {}}`",
            f"- Readiness reasons: `{options_desk.get('readiness_reasons') or []}`",
            f"- Account option positions: `{options_desk.get('account_option_position_count') or 0}`",
            f"- Position strategy groups: `{options_desk.get('position_strategy_group_count') or 0}`",
            f"- Protective candidates / ticketing: `{options_desk.get('protective_candidate_count') or 0}` / `{options_desk.get('protective_ticketing_allowed')}`",
            f"- Advanced strategies: `{options_desk.get('advanced_strategy_count') or 0}`",
            f"- Option session: `{options_desk.get('option_session_name') or '-'}` regular-dependent `{options_desk.get('option_quotes_regular_session_dependent')}` next open `{options_desk.get('next_regular_open') or '-'}`",
            f"- New option entries allowed: `{options_desk.get('new_option_entries_allowed')}` blocked `{options_desk.get('blocked_order_paths') or []}`",
            f"- Allowed option structures: `{options_desk.get('allowed_option_structures') or []}` bias `{options_desk.get('option_strategy_bias') or '-'}`",
            "",
            "| Readiness check | Passed | Impact | Value | Reason |",
            "| --- | --- | ---: | --- | --- |",
        ]
    )
    for check in readiness.get("checks") or []:
        lines.append(
            f"| `{check.get('name')}` | `{check.get('passed')}` | {check.get('impact')} | `{str(check.get('value'))[:80].replace('|', '/')}` | `{check.get('reason')}` |"
        )
    if not readiness.get("checks"):
        lines.append("| - | - | - | - | - |")
    lines.extend(
        [
            "",
            "| Contract | Risk | Side | Qty | DTE | ITM | Underlying | Expiry | Right | Strike | Alerts |",
            "| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- | ---: | --- |",
        ]
    )
    for row in options_desk.get("account_option_positions") or []:
        lines.append(
            f"| `{row.get('contract_label')}` | `{row.get('position_risk_level') or '-'}` | {row.get('side')} | {row.get('position')} | {row.get('dte')} | {row.get('in_the_money')} | {row.get('underlying_price')} | {row.get('expiry')} | {row.get('right')} | {row.get('strike')} | `{row.get('position_risk_alerts') or []}` |"
        )
    if not options_desk.get("account_option_positions"):
        lines.append("| - | - | - | - | - | - | - | - | - | - | - |")
    lines.extend(["", "| Strategy ID | Risk | Structure | Legs | Min DTE | Short ITM | Net contracts | Alerts |", "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |"])
    for row in options_desk.get("position_strategy_groups") or []:
        lines.append(
            f"| `{row.get('strategy_id')}` | `{row.get('risk_level') or '-'}` | `{row.get('structure')}` | {row.get('leg_count')} | {row.get('min_dte')} | {row.get('short_itm_count')} | {row.get('net_contracts')} | `{row.get('risk_alerts') or []}` |"
        )
    if not options_desk.get("position_strategy_groups"):
        lines.append("| - | - | - | - | - | - | - | - |")
    lines.extend(["", "| Source | Symbol | Strategy | Legs | Ticketing/state | Chain/what-if | Reason/blocks |", "| --- | --- | --- | ---: | --- | --- | --- |"])
    for row in options_desk.get("advanced_strategies") or []:
        chain_whatif = f"{row.get('requires_option_chain', '-')} / {row.get('requires_margin_whatif', '-')}"
        ticket_state = row.get("ticketing_allowed")
        if ticket_state is None:
            ticket_state = row.get("state")
        reason = row.get("reason") or ",".join(row.get("block_reasons") or []) or "-"
        lines.append(
            f"| {row.get('source')} | `{row.get('symbol')}` | `{row.get('strategy')}` | {row.get('leg_count')} | {ticket_state} | {chain_whatif} | {str(reason).replace('|', '/')} |"
        )
    if not options_desk.get("advanced_strategies"):
        lines.append("| - | - | - | - | - | - | - |")
    if single_stock_option_watchlist:
        lines.extend(
            [
                "",
                "## Adaptive Single-Stock Options Watchlist",
                "",
                f"- Status: `{single_stock_option_watchlist.get('status') or '-'}` version `{single_stock_option_watchlist.get('version') or '-'}` age `{single_stock_option_watchlist.get('age_seconds')}` seconds",
                f"- Selected: `{single_stock_option_watchlist.get('selected_symbols') or []}`",
                f"- Tier counts: `{single_stock_option_watchlist.get('tier_counts') or {}}` actions `{single_stock_option_watchlist.get('action_status_counts') or {}}`",
                "",
                "| Symbol | Tier | Score | TTL | Permission | QuoteQ | OI proxy | Gamma | Outcomes | Promotion | Themes |",
                "| --- | --- | ---: | ---: | --- | ---: | ---: | --- | --- | --- | --- |",
            ]
        )
        for row in single_stock_option_watchlist.get("rows") or []:
            outcomes = f"C{row.get('outcome_completed') or 0}/P{row.get('outcome_pending') or 0}/B{row.get('outcome_blocked') or 0}"
            lines.append(
                f"| `{row.get('symbol')}` | `{row.get('tier')}` | {row.get('score')} | {row.get('ttl_minutes')} | "
                f"`{row.get('trade_permission') or '-'}` | {row.get('quote_quality_score')} | {row.get('oi_proxy_confidence')} | "
                f"`{row.get('gamma_regime') or '-'}` | `{outcomes}` | `{row.get('promotion_status') or '-'}` | `{row.get('themes') or []}` |"
            )
    oms_summary = options_market_structure.get("summary") or {}
    oms_session_guard = options_market_structure.get("session_guard") or {}
    oms_supervisor_config = options_market_structure_supervisor.get("config") or {}
    lines.extend(
        [
            "",
            "## Options Market Structure",
            "",
            f"- Supervisor: `{options_market_structure_supervisor.get('final_status') or '-'}` session `{options_market_structure_supervisor.get('final_session') or '-'}` phase `{options_market_structure_supervisor.get('final_session_refresh_phase') or '-'}` priority `{options_market_structure_supervisor.get('final_session_refresh_priority') or '-'}` quote allowed `{options_market_structure_supervisor.get('final_option_quote_allowed')}` core quote fresh/usable `{options_market_structure_supervisor.get('final_quote_fresh')}` / `{options_market_structure_supervisor.get('final_quote_usable')}` optional index fresh/usable `{options_market_structure_supervisor.get('final_optional_index_quote_fresh')}` / `{options_market_structure_supervisor.get('final_optional_index_quote_usable')}`",
            f"- Supervisor quote quality: core `{options_market_structure_supervisor.get('final_quote_quality_status') or '-'}` score `{options_market_structure_supervisor.get('final_quote_quality_score')}` latency `{options_market_structure_supervisor.get('final_quote_data_latency_status') or '-'}` delayed `{options_market_structure_supervisor.get('final_quote_delayed_contracts')}` optional index `{options_market_structure_supervisor.get('final_optional_index_quote_quality_status') or '-'}` score `{options_market_structure_supervisor.get('final_optional_index_quote_quality_score')}` latency `{options_market_structure_supervisor.get('final_optional_index_quote_data_latency_status') or '-'}` delayed `{options_market_structure_supervisor.get('final_optional_index_quote_delayed_contracts')}` failures `{options_market_structure_supervisor.get('final_failure_reason_counts') or {}}`",
            f"- Runtime guard: `{runtime_version_guard.get('status') or '-'}` restart recommended `{runtime_version_guard.get('restart_recommended')}` missing `{((runtime_version_guard.get('options_market_structure_supervisor') or {}).get('missing_fields') or [])}`",
            f"- Supervisor artifact delta: core `{(options_market_structure_supervisor.get('final_artifact_delta') or {}).get('outcome') or '-'}` optional index `{(options_market_structure_supervisor.get('final_optional_index_artifact_delta') or {}).get('outcome') or '-'}` effective max age `{options_market_structure_supervisor.get('final_effective_max_snapshot_age_minutes')}` minutes",
            f"- M222 outcome learning: actions `{len(options_market_structure_supervisor.get('final_m222_option_structure_outcome_actions') or [])}` failed `{options_market_structure_supervisor.get('final_m222_failed_action_count') or 0}`",
            f"- Supervisor symbol status count: `{len(options_market_structure_supervisor.get('symbol_status') or {})}` isolated `{options_market_structure_supervisor.get('isolated_symbol_count') or len(options_market_structure_supervisor.get('isolated_symbols_by_action') or {})}` backoff notes `{len(options_market_structure_supervisor.get('symbol_failure_notes') or {})}`",
            f"- Supervisor symbols: core `{oms_supervisor_config.get('symbols') or []}` optional index `{oms_supervisor_config.get('optional_index_symbols') or []}` actions `{options_market_structure_supervisor.get('final_action_status_counts') or {}}`",
            f"- Index option family coverage: `{options_market_structure_supervisor.get('final_index_option_coverage_status') or '-'}` confidence `{options_market_structure_supervisor.get('final_index_option_confidence_counts') or {}}` quote budgets `{options_market_structure_supervisor.get('final_quote_contract_budgets') or {}}` family allocation `{options_market_structure_supervisor.get('index_family_quote_budget_allocation') or {}}`",
            f"- M220 family issues: `{(options_market_structure_supervisor.get('m220_family_issue_summary') or {}).get('issue_category_counts') or {}}` proxy fallback `{(options_market_structure_supervisor.get('m220_family_issue_summary') or {}).get('proxy_fallback_families') or []}` unavailable `{(options_market_structure_supervisor.get('m220_family_issue_summary') or {}).get('coverage_unavailable_families') or []}`",
            f"- Deferred operating state: `{(options_market_structure_supervisor.get('option_quote_deferred_operating_state') or {}).get('status') or '-'}` alert policy `{(options_market_structure_supervisor.get('option_quote_deferred_operating_state') or {}).get('alert_policy') or '-'}` next open `{(options_market_structure_supervisor.get('option_quote_deferred_operating_state') or {}).get('next_regular_open') or '-'}`",
            f"- M220 one-command runbook: `{(options_market_structure_supervisor.get('m220_operational_runbook') or {}).get('one_command') or '-'}`",
            f"- M220 market move option explanation: `{m220_market_move_option_overlay.get('overall_status') or '-'}` mapped moves `{m220_market_move_option_overlay.get('mapped_move_count') or 0}` status counts `{m220_market_move_option_overlay.get('status_counts') or {}}`",
            f"- M220 interpretation note: {m220_market_move_option_overlay.get('summary_note') or '-'}",
            f"- Status: `{options_market_structure.get('status') or '-'}` quote/symbol count `{options_market_structure.get('quote_count') or 0}` / `{options_market_structure.get('symbol_count') or 0}`",
            f"- Source quote snapshot: `{options_market_structure.get('source_quote_snapshot_version') or '-'}` at `{options_market_structure.get('source_quote_generated_at') or '-'}`",
            f"- Freshness guard: `{oms_session_guard.get('freshness_status') or '-'}` age minutes `{oms_session_guard.get('source_quote_age_minutes') if oms_session_guard.get('source_quote_age_minutes') is not None else '-'}` max `{oms_session_guard.get('max_snapshot_age_minutes')}` interpretation `{oms_session_guard.get('interpretation') or '-'}`",
            f"- Gamma regimes: `{oms_summary.get('gamma_regime_counts') or {}}` pin-risk symbols `{oms_summary.get('pin_risk_symbol_count') or 0}` proximity-alert symbols `{oms_summary.get('proximity_alert_symbol_count') or 0}` confidence `{oms_summary.get('confidence_counts') or {}}`",
            f"- Expiry buckets: `{oms_summary.get('expiry_bucket_counts') or {}}`",
            f"- Missing-OI symbols: `{oms_summary.get('missing_open_interest_symbols') or []}` volume fallback `{options_market_structure.get('fallback_to_volume')}`",
        ]
    )
    coverage_matrix = options_market_structure_supervisor.get("index_option_coverage_matrix") or {}
    if coverage_matrix.get("families"):
        lines.extend(["", "| Index family | Direct | Mini | Proxy | Selected | Budget | Coverage | Confidence |", "| --- | --- | --- | --- | --- | ---: | --- | --- |"])
        for family in coverage_matrix.get("families") or []:
            direct = family.get("direct") or {}
            mini = family.get("mini") or {}
            proxy = family.get("proxy") or {}
            lines.append(
                f"| `{family.get('label') or family.get('family_id')}` | "
                f"`{direct.get('status')}` {direct.get('ok_symbols') or direct.get('symbols') or []} | "
                f"`{mini.get('status')}` {mini.get('ok_symbols') or mini.get('symbols') or []} | "
                f"`{proxy.get('status')}` {proxy.get('ok_symbols') or proxy.get('symbols') or []} | "
                f"`{family.get('selected_source_type')}` {family.get('selected_symbols') or []} | "
                f"{family.get('family_quote_budget') if family.get('family_quote_budget') is not None else '-'} | "
                f"`{family.get('coverage_status')}` | `{family.get('confidence')}` |"
            )
        lines.append("")
    family_repair_plan = options_market_structure_supervisor.get("m220_family_repair_plan") or []
    if family_repair_plan:
        lines.extend(["", "| M220 repair family | Mode | Severity | Selected | Issue categories | Next action | Summary |", "| --- | --- | --- | --- | --- | --- | --- |"])
        for row in family_repair_plan:
            lines.append(
                f"| `{row.get('label') or row.get('family_id')}` | `{row.get('operating_mode')}` | `{row.get('severity')}` | "
                f"`{row.get('selected_source_type')}` {row.get('selected_symbols') or []} | "
                f"`{row.get('issue_category_counts') or {}}` | `{row.get('next_action')}` | {row.get('operator_summary') or '-'} |"
            )
        lines.append("")
    budget_tuning = options_market_structure_supervisor.get("index_family_budget_tuning_observations") or []
    if budget_tuning:
        lines.extend(["", "| Budget family | Budget | Selected | Usable/requested | Candidates/chains | Suggestion |", "| --- | ---: | --- | ---: | ---: | --- |"])
        for row in budget_tuning:
            lines.append(
                f"| `{row.get('label') or row.get('family_id')}` | {row.get('allocated_budget') if row.get('allocated_budget') is not None else '-'} | "
                f"`{row.get('selected_source_type')}` {row.get('selected_symbols') or []} | "
                f"{int(row.get('usable_role_symbol_count') or 0)} / {int(row.get('requested_role_symbol_count') or 0)} | "
                f"{int(row.get('candidate_contracts') or 0)} / {int(row.get('chain_rows') or 0)} | "
                f"`{row.get('suggested_budget_bias') or '-'}` |"
            )
        lines.append("")
    m220_checklist = options_market_structure_supervisor.get("m220_live_validation_checklist") or []
    if m220_checklist:
        lines.extend(["", "| M220 family | Role | Symbols | Passed | Status | Next action |", "| --- | --- | --- | --- | --- | --- |"])
        for row in m220_checklist:
            lines.append(
                f"| `{row.get('family') or row.get('family_id')}` | `{row.get('role')}` | "
                f"`{row.get('symbols') or []}` | `{row.get('passed')}` | `{row.get('status')}` | `{row.get('next_action')}` |"
            )
        lines.append("")
    m220_overlay_rows = m220_market_move_option_overlay.get("rows") or []
    if m220_overlay_rows:
        lines.extend(["", "| M220 move | Family | Option source | Gamma / status | Walls | Interpretation |", "| --- | --- | --- | --- | --- | --- |"])
        for row in m220_overlay_rows:
            walls = f"call {row.get('call_wall') if row.get('call_wall') is not None else '-'} / put {row.get('put_wall') if row.get('put_wall') is not None else '-'} / pain {row.get('max_pain') if row.get('max_pain') is not None else '-'}"
            move = f"{row.get('market_key')} {row.get('move_direction')} {row.get('move_magnitude_pct')}%"
            source = f"{row.get('matched_option_symbol') or '-'} via {row.get('selected_source_type') or '-'}"
            gamma = f"{row.get('gamma_regime') or '-'} / {row.get('explanation_status') or '-'}"
            lines.append(
                f"| `{move}` | `{row.get('family_label') or row.get('family_id')}` | `{source}` | `{gamma}` | {walls} | {str(row.get('interpretation') or '-').replace('|', '/')} |"
            )
        lines.append("")
    lines.extend(
        [
            "| Symbol | Spot | Gamma regime | Total GEX | Flip | Call wall | Put wall | Max pain | Pin dist % | Alerts | Confidence | Score | Warnings |",
            "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | --- |",
        ]
    )
    for row in options_market_structure.get("symbols") or []:
        lines.append(
            f"| `{row.get('symbol')}` | {row.get('spot') if row.get('spot') is not None else '-'} | `{row.get('gamma_regime') or '-'}` | "
            f"{row.get('total_gamma_exposure') if row.get('total_gamma_exposure') is not None else '-'} | {row.get('gamma_flip') if row.get('gamma_flip') is not None else '-'} | "
            f"{row.get('call_wall') if row.get('call_wall') is not None else '-'} | {row.get('put_wall') if row.get('put_wall') is not None else '-'} | "
            f"{row.get('max_pain') if row.get('max_pain') is not None else '-'} | {row.get('distance_to_pin_pct') if row.get('distance_to_pin_pct') is not None else '-'} | "
            f"`{row.get('proximity_alerts') or []}` | `{row.get('confidence') or '-'}` | {row.get('confidence_score') if row.get('confidence_score') is not None else '-'} | `{row.get('warnings') or []}` |"
        )
    if not options_market_structure.get("symbols"):
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | - | - |")
    lines.extend(["", "| Symbol | Bucket | Quotes | DTE | Gamma regime | Total GEX | Call wall | Put wall | Max pain | Pin | Alerts |", "| --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |"])
    oms_bucket_rows = 0
    for row in options_market_structure.get("symbols") or []:
        for bucket in row.get("expiry_buckets") or []:
            oms_bucket_rows += 1
            dte_range = "-"
            if bucket.get("min_dte") is not None or bucket.get("max_dte") is not None:
                dte_range = f"{bucket.get('min_dte')}-{bucket.get('max_dte')}"
            lines.append(
                f"| `{row.get('symbol')}` | `{bucket.get('bucket')}` | {bucket.get('quote_count')} | {dte_range} | `{bucket.get('gamma_regime') or '-'}` | "
                f"{bucket.get('total_gamma_exposure') if bucket.get('total_gamma_exposure') is not None else '-'} | {bucket.get('call_wall') if bucket.get('call_wall') is not None else '-'} | "
                f"{bucket.get('put_wall') if bucket.get('put_wall') is not None else '-'} | {bucket.get('max_pain') if bucket.get('max_pain') is not None else '-'} | {bucket.get('pin_risk')} | `{bucket.get('proximity_alerts') or []}` |"
            )
    if oms_bucket_rows == 0:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - |")


def _append_markdown_plan_operating_sections(lines: list[str], ctx: dict[str, Any]) -> None:
    market_data_operating_auto_gate = ctx['market_data_operating_auto_gate']
    market_data_operating_plan = ctx['market_data_operating_plan']
    operating_defensive_ledger = ctx['operating_defensive_ledger']
    operating_defensive_mode = ctx['operating_defensive_mode']
    operating_defensive_plan = ctx['operating_defensive_plan']
    operating_protective_packet = ctx['operating_protective_packet']
    plan = ctx['plan']
    lines.extend(
        [
            "",
            "## Buy / sell plan",
            "",
            f"- Intent count: `{plan.get('intent_count')}`",
            f"- Pending paper execution: `{plan.get('pending_paper_execution_count')}`",
            f"- Capital-aware execution: required `{plan.get('require_capital_aware_gate')}` source_has `{plan.get('source_has_capital_aware_gate')}` applied `{plan.get('capital_aware_gate_applied')}` ready/resized/blocked `{plan.get('source_capital_ready_count')}`/`{plan.get('source_capital_resized_count')}`/`{plan.get('source_capital_blocked_count')}`",
            f"- Action counts: `{plan.get('action_counts') or {}}`",
            "",
            "| Symbol | Action | Qty | State | Ticket |",
            "| --- | --- | ---: | --- | --- |",
        ]
    )
    for row in plan.get("intents") or []:
        lines.append(f"| {row.get('symbol')} | {row.get('action')} | {row.get('quantity')} | {row.get('state')} | {row.get('ticket_type')} |")
    if not plan.get("intents"):
        lines.append("| - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Operating gate",
            "",
            f"- Operating mode: `{market_data_operating_auto_gate.get('operating_mode') or '-'}`",
            f"- Defensive severity: `{operating_defensive_mode.get('severity') or '-'}`",
            f"- Source preview items / auto-ready after operating gate: `{market_data_operating_auto_gate.get('source_preview_item_count')}` / `{market_data_operating_auto_gate.get('auto_ready_count')}`",
            f"- Operating ledger pending paper execution: `{market_data_operating_plan.get('pending_paper_execution_count')}`",
            f"- Ordinary alpha allowed: `{operating_defensive_mode.get('ordinary_alpha_entries_allowed')}`",
            f"- Hedge/defensive entries allowed: `{operating_defensive_mode.get('hedge_or_defensive_entries_allowed')}`",
            f"- Exit/risk-reduction allowed: `{operating_defensive_mode.get('exit_or_risk_reduction_allowed')}`",
            f"- Max new-entry size multiplier: `{operating_defensive_mode.get('max_new_entry_size_multiplier')}`",
            f"- Protective analysis candidates / ticketing: `{operating_protective_packet.get('candidate_count')}` / `{operating_protective_packet.get('ticketing_allowed')}`",
            f"- Auto-gate status counts: `{market_data_operating_auto_gate.get('status_counts') or {}}`",
            f"- Block reason counts: `{market_data_operating_auto_gate.get('block_reason_counts') or {}}`",
            "",
            "| Symbol | Action | Ticket | Status | Block reasons | Size mult |",
            "| --- | --- | --- | --- | --- | ---: |",
        ]
    )
    for row in market_data_operating_auto_gate.get("sample_auto_execution_items") or []:
        lines.append(
            f"| {row.get('symbol')} | {row.get('action')} | {row.get('ticket_type')} | {row.get('status')} | "
            f"{', '.join(row.get('block_reasons') or []) or '-'} | {row.get('entry_size_multiplier')} |"
        )
    if not market_data_operating_auto_gate.get("sample_auto_execution_items"):
        lines.append("| - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "| Protective symbol | Source action | Analysis status | Ticketing | Reason |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in operating_protective_packet.get("sample_candidates") or []:
        lines.append(
            f"| {row.get('symbol')} | {row.get('source_action')} | {row.get('analysis_status')} | "
            f"{row.get('ticketing_allowed')} | {row.get('reason') or '-'} |"
        )
    if not operating_protective_packet.get("sample_candidates"):
        lines.append("| - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Defensive action plan",
            "",
            f"- Version: `{operating_defensive_plan.get('version') or '-'}`",
            f"- Generated at: `{operating_defensive_plan.get('generated_at') or '-'}` age `{operating_defensive_plan.get('age_seconds')}` seconds",
            f"- Posture: `{operating_defensive_plan.get('recommended_posture') or '-'}`",
            f"- Actions: `{operating_defensive_plan.get('action_count') or 0}`",
            f"- Action type counts: `{operating_defensive_plan.get('action_type_counts') or {}}`",
            f"- Strategy counts: `{operating_defensive_plan.get('strategy_counts') or {}}`",
            "",
            "| Priority | Symbol | Action | Strategy | Ticketing | Chain | What-if | Reason |",
            "| ---: | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for item in operating_defensive_plan.get("top_actions") or []:
        lines.append(
            f"| {item.get('priority_score')} | `{item.get('symbol')}` | `{item.get('action_type')}` | `{item.get('strategy')}` | "
            f"{item.get('ticketing_allowed')} | {item.get('requires_option_chain')} | {item.get('requires_margin_whatif')} | {str(item.get('reason') or '-').replace('|', '/')} |"
        )
    if not operating_defensive_plan.get("top_actions"):
        lines.append("| - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Defensive intent ledger",
            "",
            f"- Version: `{operating_defensive_ledger.get('version') or '-'}`",
            f"- Generated at: `{operating_defensive_ledger.get('generated_at') or '-'}` age `{operating_defensive_ledger.get('age_seconds')}` seconds",
            f"- Posture: `{operating_defensive_ledger.get('recommended_posture') or '-'}`",
            f"- Candidates / pending / blocked: `{operating_defensive_ledger.get('candidate_count') or 0}` / `{operating_defensive_ledger.get('pending_defensive_paper_execution_count') or 0}` / `{operating_defensive_ledger.get('blocked_count') or 0}`",
            f"- Strict gate mode: `{operating_defensive_ledger.get('strict_gate_mode')}`",
            f"- State counts: `{operating_defensive_ledger.get('state_counts') or {}}`",
            "",
            "| State | Symbol | Strategy | Priority | Blocks | Paper action | Qty |",
            "| --- | --- | --- | ---: | --- | --- | ---: |",
        ]
    )
    for item in operating_defensive_ledger.get("defensive_intents") or []:
        lines.append(
            f"| `{item.get('state')}` | `{item.get('symbol')}` | `{item.get('strategy')}` | {item.get('priority_score')} | "
            f"{', '.join(item.get('block_reasons') or []) or '-'} | `{item.get('paper_action') or '-'}` | {item.get('quantity') if item.get('quantity') is not None else '-'} |"
        )
    if not operating_defensive_ledger.get("defensive_intents"):
        lines.append("| - | - | - | - | - | - | - |")


def _append_markdown_news_evidence_learning_sections(lines: list[str], ctx: dict[str, Any]) -> None:
    evidence_auto_recovery = ctx['evidence_auto_recovery']
    evidence_freshness = ctx['evidence_freshness']
    learning_pipeline = ctx['learning_pipeline']
    live_learning_llm_notes = ctx['live_learning_llm_notes']
    live_scorer_learning = ctx['live_scorer_learning']
    m232_hot_cache = ctx['m232_hot_cache']
    m236_news_learning = ctx['m236_news_learning']
    operational_runbook = ctx['operational_runbook']
    safety_latency = ctx['safety_latency']
    snapshot = ctx['snapshot']
    source_ingest_health = ctx['source_ingest_health']
    source_online_supervisor = ctx['source_online_supervisor']
    threshold_candidate_evaluator = ctx['threshold_candidate_evaluator']
    lines.extend(
        [
            "",
            "## Latest one-line news",
            "",
        ]
    )
    for item in snapshot.get("news_ticker") or []:
        lines.append(f"- `{item.get('published_at') or item.get('provider_published_at') or item.get('received_at') or '-'}` [{item.get('provider') or '-'}] {item.get('headline') or '-'}")
    if not snapshot.get("news_ticker"):
        lines.append("- none")
    geopolitical_desk = snapshot.get("geopolitical_desk") or {}
    lines.extend(
        [
            "",
            "## Geopolitical Desk",
            "",
            f"- Available: `{geopolitical_desk.get('available')}` buckets `{geopolitical_desk.get('bucket_count')}` hot `{geopolitical_desk.get('hot_bucket_count')}`",
            f"- Heat colors: `{geopolitical_desk.get('heat_color_counts') or {}}`",
            "",
            "| Heat | Score | Region | Theme | Signals | Urgency | Assets | Action | Latest |",
            "| --- | ---: | --- | --- | ---: | --- | --- | --- | --- |",
        ]
    )
    for item in (geopolitical_desk.get("top_buckets") or [])[:12]:
        lines.append(
            f"| `{item.get('heat_color')}` | {item.get('heat_score')} | `{item.get('region')}` | `{item.get('theme')}` | "
            f"{item.get('signal_count')} | `{item.get('urgency_counts')}` | `{item.get('top_assets')}` | "
            f"`{item.get('desk_action')}` | `{item.get('latest_event_at') or '-'}` |"
        )
    if not geopolitical_desk.get("top_buckets"):
        lines.append("| - | - | - | - | - | - | - | - | - |")
    lines.extend(["", "## Standing regimes", ""])
    for item in snapshot.get("standing_regimes") or []:
        lines.append(
            f"- `{item.get('status') or '-'}` {item.get('regime_name') or item.get('regime_key')} "
            f"family `{item.get('family') or '-'}` severity `{item.get('severity_score')}` confidence `{item.get('confidence')}` "
            f"effective `{item.get('effective_weight')}` freshness `{item.get('freshness_status') or '-'}` age_days `{item.get('age_days')}` "
            f"bias `{item.get('directional_bias') or '-'}`"
        )
    if not snapshot.get("standing_regimes"):
        lines.append("- none")
    lines.extend(["", "## Evidence freshness", ""])
    if evidence_freshness:
        lines.extend(
            [
                f"- Overall status: `{evidence_freshness.get('overall_status')}`",
                f"- Status counts: `{evidence_freshness.get('status_counts') or {}}`",
                f"- Scorer guard: `{(evidence_freshness.get('scorer_guard') or {}).get('action')}`",
                "",
                "| Source | Status | Age min | Scorer use | Weight |",
                "| --- | --- | ---: | --- | ---: |",
            ]
        )
        for item in evidence_freshness.get("sources") or []:
            lines.append(
                f"| {item.get('label') or item.get('source_key')} | {item.get('status')} | {item.get('age_minutes')} "
                f"| {item.get('scorer_usability')} | {item.get('scorer_weight')} |"
            )
    else:
        lines.append("- not configured")
    lines.extend(["", "## Evidence auto-recovery", ""])
    if evidence_auto_recovery:
        lines.extend(
            [
                f"- Status: `{evidence_auto_recovery.get('status')}`",
                f"- Last refresh: `{evidence_auto_recovery.get('last_refresh_status')}`",
                f"- Freshness after refresh: `{evidence_auto_recovery.get('last_freshness_status') or '-'}`",
                f"- Stale / lagging sources: `{evidence_auto_recovery.get('last_stale_source_keys') or []}` / `{evidence_auto_recovery.get('last_lagging_source_keys') or []}`",
                f"- Consecutive failures: `{evidence_auto_recovery.get('consecutive_failures')}`",
                f"- Next refresh eligible at: `{evidence_auto_recovery.get('next_refresh_eligible_at') or '-'}`",
                f"- Last queue counts: `{evidence_auto_recovery.get('last_refresh_queue_status_counts') or {}}`",
            ]
        )
    else:
        lines.append("- not configured")
    lines.extend(["", "## Data Source Doctor", ""])
    if source_ingest_health:
        lines.extend(
            [
                f"- Overall status: `{source_ingest_health.get('overall_status')}`",
                f"- Health counts: `{source_ingest_health.get('health_counts') or {}}`",
                f"- Online problem sources: `{source_ingest_health.get('online_problem_source_keys') or []}`",
                "",
                "| Source | Online | Freshness | Health | Root cause | Action |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in source_ingest_health.get("sources") or []:
            lines.append(
                f"| {item.get('label') or item.get('source_key')} | {item.get('online_required')} | {item.get('freshness_status')} "
                f"| {item.get('health_status')} | `{item.get('root_cause')}` | `{item.get('recommended_action')}` |"
            )
    else:
        lines.append("- not configured")
    lines.extend(["", "## Source Online Supervisor", ""])
    if source_online_supervisor:
        latest_tick = source_online_supervisor.get("latest_tick") or {}
        lines.extend(
            [
                f"- Final status: `{source_online_supervisor.get('final_status')}`",
                f"- Final live validation: `{source_online_supervisor.get('final_live_validation_status')}`",
                f"- Final source ingest health: `{source_online_supervisor.get('final_source_ingest_health_status')}`",
                f"- Alpha Vantage disabled: `{(source_online_supervisor.get('final_alpha_vantage_disabled') or {}).get('disabled', False)}` reason `{(source_online_supervisor.get('final_alpha_vantage_disabled') or {}).get('reason') or '-'}`",
                f"- Action status counts: `{source_online_supervisor.get('final_action_status_counts') or {}}`",
                f"- Provider cooldown actions: `{[item.get('action_id') for item in source_online_supervisor.get('provider_cooldown_actions') or []]}`",
                f"- Failed actions: `{[item.get('action_id') for item in source_online_supervisor.get('failed_actions') or []]}`",
                f"- Latest tick: `{latest_tick.get('tick_index')}` checked local `{latest_tick.get('checked_at_local') or _local_time_text(latest_tick.get('checked_at'))}` raw `{latest_tick.get('checked_at') or '-'}`",
                "",
                "| Action | Source | Status | Next eligible local | Description |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for action in latest_tick.get("actions") or []:
            details = action.get("details") or {}
            lines.append(
                f"| `{action.get('action_id')}` | {action.get('source_key')} | {action.get('status')} | `{details.get('next_eligible_at_local') or _local_time_text(details.get('next_eligible_at'))}` | {action.get('description')} |"
            )
    else:
        lines.append("- not configured")
    lines.extend(["", "## M236 News Source-Quality Learning", ""])
    if m236_news_learning:
        lines.extend(
            [
                f"- Status: `{m236_news_learning.get('status')}` version `{m236_news_learning.get('version')}`",
                f"- Records: `{m236_news_learning.get('record_count')}` status `{m236_news_learning.get('status_counts') or {}}`",
                f"- Retention: tail `{m236_news_learning.get('tail_record_count')}` evaluated-index `{m236_news_learning.get('evaluated_index_record_count')}` retained `{m236_news_learning.get('evaluated_retained_record_count')}`",
                f"- Learning velocity: `{m236_news_learning.get('learning_velocity') or {}}`",
                f"- Due horizons: `{m236_news_learning.get('due_horizon_counts') or {}}`",
                f"- Ticker review queue: `{m236_news_learning.get('ticker_review_queue_count')}` buckets `{m236_news_learning.get('ticker_confidence_buckets') or {}}`",
                f"- Repeated canonical events: `{m236_news_learning.get('repeated_canonical_event_count')}`",
                f"- Scorer directives: `{m236_news_learning.get('scorer_directive_counts') or {}}`",
                f"- Macro release integration: `{m236_news_learning.get('macro_release_integration') or {}}`",
                f"- Role-family readiness: `{m236_news_learning.get('role_family_readiness') or {}}`",
                "",
                "| Provider | Samples | Evaluated | Hit rate | Weight | Status |",
                "| --- | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for provider in m236_news_learning.get("top_providers") or []:
            lines.append(
                f"| `{provider.get('family')}` | {provider.get('sample_count')} | {provider.get('evaluated_horizon_count')} | "
                f"{provider.get('hit_rate')} | {provider.get('overall_weight')} | `{provider.get('learning_status')}` |"
            )
    else:
        lines.append("- not configured")
    lines.extend(["", "## Learning Pipeline Health", ""])
    if learning_pipeline:
        lines.extend(
            [
                f"- Status: `{learning_pipeline.get('status')}` / `{learning_pipeline.get('label')}`",
                f"- Healthy: `{learning_pipeline.get('healthy')}`",
                f"- Blocking reasons: `{learning_pipeline.get('blocking_reasons') or []}`",
                f"- Warnings: `{learning_pipeline.get('warnings') or []}`",
                f"- Option session: `{((learning_pipeline.get('option_session') or {}).get('session') or '-')}` ET `{((learning_pipeline.get('option_session') or {}).get('checked_at_et') or '-')}`",
                f"- Predictions / notes / M225 backlog: `{learning_pipeline.get('prediction_count')}` / `{learning_pipeline.get('adjustment_note_count')}` / `{learning_pipeline.get('m225_backlog_count')}`",
                f"- M225 next / M226 ready / gate block-watch: `{learning_pipeline.get('m225_next_interval_seconds')}` / `{learning_pipeline.get('m226_promotion_ready_count')}` / `{learning_pipeline.get('m226_gate_blocked_count')}`-`{learning_pipeline.get('m226_gate_watchlist_count')}`",
                f"- M232 hot-cache: `{learning_pipeline.get('m232_hot_cache_status') or '-'}` usable `{learning_pipeline.get('m232_hot_cache_usable')}` age `{learning_pipeline.get('m232_hot_cache_age_seconds')}`s tier `{learning_pipeline.get('m232_hot_cache_adaptive_tier') or '-'}` next `{learning_pipeline.get('m232_hot_cache_next_interval_seconds') or '-'}`s",
                f"- M232 scorer read: `{learning_pipeline.get('m232_scorer_read_status') or '-'}` usable `{learning_pipeline.get('m232_scorer_read_usable')}` cache-age-at-read `{learning_pipeline.get('m232_age_at_last_scorer_read_seconds')}`s scorer-report-age `{learning_pipeline.get('m232_scorer_report_age_seconds')}`s changed `{learning_pipeline.get('m232_hot_cache_changed_keys') or []}`",
            ]
        )
    else:
        lines.append("- not configured")
    lines.extend(["", "## M223 Live Scorer Learning", ""])
    if live_scorer_learning:
        lines.extend(
            [
                f"- Status: `{live_scorer_learning.get('status')}`",
                f"- Shadow only: `{live_scorer_learning.get('shadow_only')}` / promotion `{live_scorer_learning.get('promotion_mode')}`",
                f"- Evidence packet: `{live_scorer_learning.get('evidence_packet_version') or '-'}`",
                f"- Scorer: `{live_scorer_learning.get('scorer_name')}` / `{live_scorer_learning.get('scorer_version')}`",
                f"- Predictions: `{live_scorer_learning.get('prediction_count')}` / v2 `{live_scorer_learning.get('evidence_packet_v2_prediction_count')}`",
                f"- Packet quality counts: `{live_scorer_learning.get('packet_quality_counts') or {}}`",
                f"- Asset mapping confidence: `{live_scorer_learning.get('asset_mapping_confidence_counts') or {}}`",
                f"- Timestamp alignment: `{live_scorer_learning.get('timestamp_alignment_counts') or {}}` / late `{live_scorer_learning.get('late_baseline_count')}`",
                f"- Pending / blocked / evaluated outcomes: `{live_scorer_learning.get('pending_outcome_count')}` / `{live_scorer_learning.get('blocked_outcome_count')}` / `{live_scorer_learning.get('evaluated_outcome_count')}`",
                f"- Adjustment notes: `{live_scorer_learning.get('adjustment_note_count')}`",
                f"- Candidate sample / hit rate / avg error: `{live_scorer_learning.get('candidate_sample_count')}` / `{live_scorer_learning.get('candidate_hit_rate')}` / `{live_scorer_learning.get('candidate_avg_absolute_error')}`",
                f"- Updated local: `{live_scorer_learning.get('completed_at_local') or '-'}`",
            ]
        )
    else:
        lines.append("- not configured")
    lines.extend(["", "## M232 Live Learning Control Plane", ""])
    if m232_hot_cache:
        lines.extend(
            [
                f"- Version: `{m232_hot_cache.get('version')}`",
                f"- Status / usable / age: `{m232_hot_cache.get('status')}` / `{m232_hot_cache.get('usable')}` / `{m232_hot_cache.get('age_seconds')}`s",
                f"- Adaptive tier / next interval / changed: `{m232_hot_cache.get('adaptive_tier') or '-'}` / `{m232_hot_cache.get('adaptive_next_interval_seconds') or '-'}`s / `{m232_hot_cache.get('adaptive_changed_keys') or []}`",
                f"- Full report latest / next due: `{m232_hot_cache.get('full_report_latest_generated_at') or '-'}` / `{m232_hot_cache.get('full_report_next_due_in_seconds') or '-'}`s",
                f"- Source counts: `{m232_hot_cache.get('source_counts') or {}}`",
                f"- Paper scorer ready / conditional overlay: `{m232_hot_cache.get('paper_scorer_ready')}` / `{m232_hot_cache.get('conditional_overlay_available')}`",
                f"- Gate wait/watch/block: `{m232_hot_cache.get('waitlist_count')}` / `{m232_hot_cache.get('watchlist_count')}` / `{m232_hot_cache.get('blocked_count')}`",
                f"- Reflex candidates / memories: `{m232_hot_cache.get('reflex_candidate_count')}` / `{m232_hot_cache.get('reflex_memory_count')}`",
                f"- Option memories / complete rows: `{m232_hot_cache.get('option_memory_count')}` / `{m232_hot_cache.get('option_complete_rows')}`",
                f"- Action-gate paper/block memories: `{m232_hot_cache.get('action_gate_paper_entry_candidate_count')}` / `{m232_hot_cache.get('action_gate_block_or_second_pass_count')}`",
            ]
        )
    else:
        lines.append("- not configured")
    lines.extend(["", "## M225 LLM Adjustment Notes", ""])
    if live_learning_llm_notes:
        lines.extend(
            [
                f"- Version: `{live_learning_llm_notes.get('version')}`",
                f"- Model: `{live_learning_llm_notes.get('model')}`",
                f"- Status: `{live_learning_llm_notes.get('status') or '-'}`",
                f"- Backlog / adaptive tier / limit / next interval: `{live_learning_llm_notes.get('backlog_count')}` / `{(live_learning_llm_notes.get('adaptive_schedule') or {}).get('tier') or '-'}` / `{(live_learning_llm_notes.get('adaptive_schedule') or {}).get('effective_limit')}` / `{(live_learning_llm_notes.get('adaptive_schedule') or {}).get('next_interval_seconds')}`",
                f"- Candidates / created / rejected / errors: `{live_learning_llm_notes.get('candidate_count')}` / `{live_learning_llm_notes.get('created_count')}` / `{live_learning_llm_notes.get('rejected_count')}` / `{live_learning_llm_notes.get('error_count')}`",
                f"- Quality this tick: `{live_learning_llm_notes.get('note_quality_counts_this_tick') or {}}`",
                f"- Provider counts: `{(live_learning_llm_notes.get('note_counts') or {}).get('provider_counts') or {}}`",
                f"- Threshold candidate groups: `{(live_learning_llm_notes.get('threshold_candidate_summary') or {}).get('candidate_group_count')}`",
                f"- Cooldown / next eligible: `{live_learning_llm_notes.get('cooldown_reasons') or []}` / `{live_learning_llm_notes.get('next_eligible_at_local') or '-'}`",
                f"- Shadow only: `{live_learning_llm_notes.get('shadow_only')}`",
                f"- Updated local: `{live_learning_llm_notes.get('completed_at_local') or '-'}`",
            ]
        )
    else:
        lines.append("- not configured")
    lines.extend(["", "## M226 Threshold Candidate Evaluator", ""])
    if threshold_candidate_evaluator:
        top = threshold_candidate_evaluator.get("top_candidates") or []
        best = top[0] if top else {}
        lines.extend(
            [
                f"- Version: `{threshold_candidate_evaluator.get('version')}`",
                f"- Raw / families: `{threshold_candidate_evaluator.get('raw_candidate_count')}` / `{threshold_candidate_evaluator.get('semantic_family_count')}`",
                f"- Candidates / ready / needs / watch / shadow / reject: `{threshold_candidate_evaluator.get('candidate_count')}` / `{threshold_candidate_evaluator.get('promotion_ready_count')}` / `{threshold_candidate_evaluator.get('needs_more_samples_count')}` / `{threshold_candidate_evaluator.get('watch_count')}` / `{threshold_candidate_evaluator.get('shadow_candidate_count')}` / `{threshold_candidate_evaluator.get('reject_count')}`",
                f"- Adaptive tier / changed / next interval: `{(threshold_candidate_evaluator.get('adaptive_refresh') or {}).get('tier') or '-'}` / `{(threshold_candidate_evaluator.get('adaptive_refresh') or {}).get('changed_since_last_tick')}` / `{(threshold_candidate_evaluator.get('adaptive_refresh') or {}).get('next_interval_seconds')}`",
                f"- Status counts: `{threshold_candidate_evaluator.get('status_counts') or {}}`",
                f"- Blocker counts: `{threshold_candidate_evaluator.get('blocker_counts') or {}}`",
                f"- Blocker severity counts: `{threshold_candidate_evaluator.get('blocker_severity_counts') or {}}`",
                f"- M225 focus count: `{(threshold_candidate_evaluator.get('m225_focus_feedback') or {}).get('focus_pattern_count')}`",
                f"- Shadow trial active / ready: `{(threshold_candidate_evaluator.get('shadow_promotion_trial') or {}).get('active_count')}` / `{(threshold_candidate_evaluator.get('shadow_promotion_trial') or {}).get('ready_for_real_promotion_count')}`",
                f"- Promotion gate waitlist / watchlist / blocked: `{(threshold_candidate_evaluator.get('promotion_gate') or {}).get('waitlist_count')}` / `{(threshold_candidate_evaluator.get('promotion_gate') or {}).get('watchlist_count')}` / `{(threshold_candidate_evaluator.get('promotion_gate') or {}).get('blocked_count')}`",
                f"- Promotion gate blocked reasons: `{(threshold_candidate_evaluator.get('promotion_gate') or {}).get('blocked_reason_counts') or {}}`",
                f"- Promotion gate watchlist reasons: `{(threshold_candidate_evaluator.get('promotion_gate') or {}).get('watchlist_reason_counts') or {}}`",
                f"- Promotion gate watchlist detail reasons: `{(threshold_candidate_evaluator.get('promotion_gate') or {}).get('watchlist_detail_reason_counts') or {}}`",
                f"- Promotion gate stale watchlist / conditional / transitions: `{((threshold_candidate_evaluator.get('promotion_gate') or {}).get('watchlist_aging') or {}).get('stale_watchlist_count')}` / `{(threshold_candidate_evaluator.get('promotion_gate') or {}).get('conditional_promotion_count')}` / `{((threshold_candidate_evaluator.get('promotion_gate') or {}).get('transition_history') or {}).get('transition_count')}`",
                f"- Promotion gate operational / actions / alerts: `{(threshold_candidate_evaluator.get('promotion_gate') or {}).get('operational_status_counts') or {}}` / `{(threshold_candidate_evaluator.get('promotion_gate') or {}).get('action_request_count')}` / `{(threshold_candidate_evaluator.get('promotion_gate') or {}).get('alert_signal_count')}`",
                f"- Promotion gate option roots / recycle blockers / scorer policies: `{((threshold_candidate_evaluator.get('promotion_gate') or {}).get('option_confidence_root_causes') or {}).get('root_cause_counts') or {}}` / `{((threshold_candidate_evaluator.get('promotion_gate') or {}).get('recycle_learning') or {}).get('return_blocker_counts') or {}}` / `{((threshold_candidate_evaluator.get('promotion_gate') or {}).get('scorer_threshold_policy_summary') or {}).get('scorer_action_counts') or {}}`",
                f"- Promotion gate recycle count / eligible / near / lifecycle: `{((threshold_candidate_evaluator.get('promotion_gate') or {}).get('recycle_learning') or {}).get('recycle_candidate_count')}` / `{((threshold_candidate_evaluator.get('promotion_gate') or {}).get('recycle_learning') or {}).get('eligible_to_return_count')}` / `{((threshold_candidate_evaluator.get('promotion_gate') or {}).get('recycle_learning') or {}).get('near_return_count')}` / `{((threshold_candidate_evaluator.get('promotion_gate') or {}).get('recycle_learning') or {}).get('lifecycle_status_counts') or {}}`",
                f"- Cycle new / continued / disappeared families: `{(threshold_candidate_evaluator.get('cycle_report') or {}).get('new_family_count')}` / `{(threshold_candidate_evaluator.get('cycle_report') or {}).get('continued_family_count')}` / `{(threshold_candidate_evaluator.get('cycle_report') or {}).get('disappeared_family_count')}`",
                f"- Best candidate: `{best.get('status') or '-'}` score `{best.get('promotion_score') or '-'}` samples `{best.get('sample_count') or '-'}`",
                f"- Shadow only: `{threshold_candidate_evaluator.get('shadow_only')}`",
                f"- Updated local: `{threshold_candidate_evaluator.get('evaluated_at_local') or '-'}`",
            ]
        )
    else:
        lines.append("- not configured")
    lines.extend(["", "## Loop Heartbeat", ""])
    singleton_locks = snapshot.get("singleton_locks") or {}
    if singleton_locks:
        truth = singleton_locks.get("truth_summary") or {}
        lines.extend(
            [
                f"- Overall: `{singleton_locks.get('overall_status')}` online `{singleton_locks.get('online_count')}` / `{singleton_locks.get('lock_count')}` stale/missing `{singleton_locks.get('stale_or_missing_count')}` zombie-output `{singleton_locks.get('zombie_output_stale_count') or 0}`",
                f"- Truth: process online `{truth.get('process_online_count')}` lock files `{truth.get('lock_file_count')}` artifact fresh/stale `{truth.get('artifact_fresh_count')}`/`{truth.get('artifact_stale_count')}` fresh artifact without process `{truth.get('fresh_artifact_without_process_count')}` `{truth.get('fresh_artifact_without_process_services') or []}`",
                f"- Duplicate blocks recent: `{singleton_locks.get('duplicate_block_count_recent')}`",
                f"- Status counts: `{singleton_locks.get('status_counts') or {}}`",
                "- PID note: table PID is the singleton lock-holder writer PID; duplicate-looking Windows wrapper processes are ignored unless duplicate blocks or stale output are reported.",
            ]
        )
        for row in (singleton_locks.get("locks") or [])[:5]:
            lines.append(
                f"- `{row.get('name')}` status `{row.get('status')}` writer_pid `{row.get('writer_pid') or row.get('pid')}` age `{row.get('age_seconds')}` stale `{row.get('stale_by_age')}` artifact_age `{row.get('artifact_age_seconds')}` artifact_stale `{row.get('artifact_stale')}`"
            )
    else:
        lines.append("- not configured")
    lines.extend(["", "## Operational Runbook", ""])
    if operational_runbook:
        lines.extend(
            [
                f"- Status: `{operational_runbook.get('status')}`",
                f"- Repo: `{operational_runbook.get('repo_root')}`",
                f"- Safety: `{operational_runbook.get('safety') or {}}`",
                "",
                "### Immediate checks",
                "",
            ]
        )
        for item in operational_runbook.get("immediate_checks") or []:
            lines.append(f"- {item}")
        lines.extend(["", "### Verification commands", ""])
        for item in operational_runbook.get("verification_commands") or []:
            lines.append(f"- `{item}`")
    else:
        lines.append("- not configured")
    lines.extend(["", "## Safety Gate Latency", ""])
    if safety_latency:
        lines.extend(
            [
                f"- Status: `{safety_latency.get('status')}` total_ms `{safety_latency.get('total_elapsed_ms')}` stages `{safety_latency.get('stage_count')}`",
                f"- Safety: `{safety_latency.get('safety') or {}}`",
                "",
                "| Stage | Elapsed ms |",
                "| --- | ---: |",
            ]
        )
        for item in safety_latency.get("top_stages") or []:
            lines.append(f"| `{item.get('stage')}` | {item.get('elapsed_ms')} |")
        lines.extend(["", "| Source | Elapsed ms | Loaded |", "| --- | ---: | --- |"])
        for item in safety_latency.get("slowest_sources") or []:
            lines.append(f"| `{item.get('source')}` | {item.get('elapsed_ms')} | `{item.get('loaded')}` |")
    else:
        lines.append("- not configured")


def _append_markdown_market_diagnostics_sections(lines: list[str], ctx: dict[str, Any]) -> None:
    market_data_session_overlay = ctx['market_data_session_overlay']
    market_session_buckets = ctx['market_session_buckets']
    market_session_monitorability = ctx['market_session_monitorability']
    market_symbol_quarantine = ctx['market_symbol_quarantine']
    market_temperature_adjustment = ctx['market_temperature_adjustment']
    market_temperature_diagnostics = ctx['market_temperature_diagnostics']
    snapshot = ctx['snapshot']
    lines.extend(["", "## Market Data Temperature Diagnostics", ""])
    if market_temperature_diagnostics:
        lines.extend(
            [
                f"- Status: `{market_temperature_diagnostics.get('status')}` label `{market_temperature_diagnostics.get('label')}` score `{market_temperature_diagnostics.get('score')}`",
                f"- Session adjustment: `{market_temperature_adjustment.get('adjusted_interpretation')}` adjusted `{market_temperature_adjustment.get('adjusted_status')}` false-positive `{market_temperature_adjustment.get('false_positive_risk')}` trade-blocking `{market_temperature_adjustment.get('trade_blocking_market_temperature')}`",
                f"- Monitoring lifecycle: `{market_temperature_adjustment.get('monitoring_only_lifecycle') or {}}`",
                f"- Symbol anomaly quarantine: `{market_symbol_quarantine.get('status')}` candidates `{market_symbol_quarantine.get('candidate_count') or 0}` keys `{market_symbol_quarantine.get('candidate_keys') or []}`",
                f"- Root causes: `{market_temperature_diagnostics.get('root_causes') or []}`",
                f"- Summary counts: `{market_temperature_diagnostics.get('summary_counts') or {}}`",
                "",
                "| Threshold | Regular | Non-regular | Unit |",
                "| --- | ---: | ---: | --- |",
            ]
        )
        for item in ((market_temperature_adjustment.get("thresholds") or {}).get("table") or []):
            lines.append(f"| `{item.get('metric')}` | {item.get('regular')} | {item.get('non_regular')} | `{item.get('unit')}` |")
        lines.extend(
            [
                "",
                "| Component | Status | Score | Counts |",
                "| --- | --- | ---: | --- |",
            ]
        )
        for item in market_temperature_diagnostics.get("component_rows") or []:
            lines.append(f"| `{item.get('component')}` | `{item.get('status')}` | {item.get('score') if item.get('score') is not None else '-'} | `{item.get('counts') or {}}` |")
        lines.extend(["", "| Key | Group | Status | Gap % | Tick % | Alerts |", "| --- | --- | --- | ---: | ---: | --- |"])
        for item in (market_temperature_diagnostics.get("top_contributors") or [])[:8]:
            lines.append(
                f"| `{item.get('key')}` | `{item.get('group')}` | `{item.get('status')}` | {item.get('gap_pct') if item.get('gap_pct') is not None else '-'} | {item.get('tick_move_pct') if item.get('tick_move_pct') is not None else '-'} | `{item.get('alerts') or []}` |"
            )
    else:
        lines.append("- not configured")
    lines.extend(["", "## Market Data Session Overlay", ""])
    if market_data_session_overlay:
        next_open = market_data_session_overlay.get("next_regular_open") or {}
        lines.extend(
            [
                f"- US session: `{market_data_session_overlay.get('session_name') or '-'}` regular `{market_data_session_overlay.get('is_regular_session')}` extended/overnight `{market_data_session_overlay.get('is_extended_or_overnight')}`",
                f"- Interpretation: `{market_data_session_overlay.get('risk_interpretation') or '-'}`",
                f"- Stock/ETF cash overnight monitoring: `{market_data_session_overlay.get('stock_cash_overnight_monitoring_enabled')}`",
                f"- Listed option quote/bid-ask regular-session dependent: `{market_data_session_overlay.get('option_quotes_regular_session_dependent')}`",
                f"- Monitorability counts: `{market_session_monitorability}`",
                f"- Session bucket counts: `{market_session_buckets}`",
                f"- Next regular open: `{next_open.get('next_open_local') or '-'}` wait_hours `{next_open.get('wait_hours') if next_open else '-'}`",
                f"- Monitorable alert instruments: `{market_data_session_overlay.get('monitorable_alert_instruments') or []}`",
                f"- Regular-dependent alert instruments: `{market_data_session_overlay.get('regular_dependent_alert_instruments') or []}`",
                "",
                "| Key | Bucket | Monitorability | Price | Alerts | Status |",
                "| --- | --- | --- | ---: | --- | --- |",
            ]
        )
        for row in market_data_session_overlay.get("rows") or []:
            lines.append(
                f"| `{row.get('key')}` | `{row.get('session_bucket')}` | `{row.get('monitorability')}` | {row.get('price') if row.get('price') is not None else '-'} | "
                f"{', '.join(row.get('alerts') or []) or '-'} | `{row.get('status') or '-'}` |"
            )
        if not market_data_session_overlay.get("rows"):
            lines.append("| - | - | - | - | - | - |")
    else:
        lines.append("- not configured")
    lines.extend(["", "## Menus", ""])
    for item in snapshot.get("menus") or []:
        lines.append(f"- `{item.get('id')}` {item.get('label')}: {item.get('description')}")
    if snapshot.get("missing_sources"):
        lines.extend(["", "## Missing / stale source notes", ""])
        for item in snapshot.get("missing_sources") or []:
            lines.append(f"- {item}")


def trading_dashboard_markdown(snapshot: dict[str, Any]) -> str:
    status = snapshot.get("status") or {}
    account = snapshot.get("account") or {}
    risk = snapshot.get("risk") or {}
    positions = snapshot.get("positions") or {}
    options_desk = snapshot.get("options_desk") or {}
    options_market_structure = snapshot.get("options_market_structure") or {}
    options_market_structure_supervisor = snapshot.get("options_market_structure_supervisor") or {}
    single_stock_option_watchlist = snapshot.get("single_stock_option_watchlist") or {}
    runtime_version_guard = snapshot.get("runtime_version_guard") or {}
    m220_market_move_option_overlay = snapshot.get("m220_market_move_option_structure_overlay") or {}
    plan = snapshot.get("buy_sell_plan") or {}
    mark_quality = ((snapshot.get("market_data") or {}).get("mark_refresh_quality") or {}).get("quality") or {}
    market_data_desk = ((snapshot.get("market_data") or {}).get("market_data_desk") or {})
    market_data_session_overlay = ((snapshot.get("market_data") or {}).get("market_data_session_overlay") or {})
    market_data_risk_gate = ((snapshot.get("market_data") or {}).get("market_data_risk_gate") or {})
    market_data_operating_auto_gate = ((snapshot.get("market_data") or {}).get("market_data_operating_auto_gate") or {})
    capital_margin_state = snapshot.get("capital_margin_state") or ((snapshot.get("market_data") or {}).get("capital_margin_state") or {})
    capital_aware_action_gate = snapshot.get("capital_aware_action_gate") or ((snapshot.get("market_data") or {}).get("capital_aware_action_gate") or {})
    live_read_only_capital_validation = snapshot.get("live_read_only_capital_validation") or ((snapshot.get("market_data") or {}).get("live_read_only_capital_validation") or {})
    capital_pipeline_orchestrator = snapshot.get("capital_pipeline_orchestrator") or ((snapshot.get("market_data") or {}).get("capital_pipeline_orchestrator") or {})
    strategy_allocation_engine = snapshot.get("strategy_allocation_engine") or ((snapshot.get("market_data") or {}).get("strategy_allocation_engine") or {})
    micro_option_candidates = snapshot.get("micro_option_candidates") or ((snapshot.get("market_data") or {}).get("micro_option_candidates") or {})
    market_data_session_transition_gate = ((snapshot.get("market_data") or {}).get("market_data_session_transition_gate") or {})
    session_transition_release_governor = ((snapshot.get("market_data") or {}).get("session_transition_release_governor") or {})
    market_data_operating_plan = ((snapshot.get("market_data") or {}).get("market_data_operating_execution_intent_ledger") or {})
    operating_defensive_plan = ((snapshot.get("market_data") or {}).get("operating_defensive_action_plan") or {})
    operating_defensive_ledger = ((snapshot.get("market_data") or {}).get("operating_defensive_intent_ledger") or {})
    operating_defensive_mode = market_data_operating_auto_gate.get("defensive_mode") or {}
    operating_protective_packet = market_data_operating_auto_gate.get("protective_analysis_packet") or {}
    capital_budgets = capital_margin_state.get("budgets") or {}
    capital_micro_budget_overlay = capital_budgets.get("micro_cash_budget_overlay") or {}
    capital_withdrawal_guard = capital_margin_state.get("withdrawal_guard") or {}
    capital_gate_capital_state = capital_aware_action_gate.get("capital_state") or {}
    market_summary = market_data_desk.get("summary") or {}
    market_temperature_diagnostics = market_data_desk.get("temperature_diagnostics") or {}
    market_temperature_adjustment = market_data_desk.get("temperature_session_adjustment") or {}
    market_symbol_quarantine = market_data_desk.get("symbol_anomaly_quarantine") or {}
    market_session_summary = market_data_session_overlay.get("summary") or {}
    market_session_monitorability = market_data_session_overlay.get("monitorability_counts") or market_session_summary.get("monitorability_counts") or {}
    market_session_buckets = market_data_session_overlay.get("session_bucket_counts") or market_session_summary.get("session_bucket_counts") or {}
    evidence_freshness = snapshot.get("evidence_freshness") or {}
    evidence_auto_recovery = snapshot.get("evidence_auto_recovery") or {}
    source_ingest_health = snapshot.get("source_ingest_health") or {}
    source_online_supervisor = snapshot.get("source_online_supervisor") or {}
    m236_news_learning = snapshot.get("m236_news_learning") or {}
    macro_release_interpreter = snapshot.get("macro_release_interpreter") or {}
    learning_pipeline = snapshot.get("learning_pipeline") or {}
    live_scorer_learning = snapshot.get("live_scorer_learning") or {}
    live_learning_llm_notes = snapshot.get("live_learning_llm_notes") or {}
    threshold_candidate_evaluator = snapshot.get("threshold_candidate_evaluator") or {}
    m232_hot_cache = snapshot.get("m232_hot_cache") or {}
    singleton_locks = snapshot.get("singleton_locks") or {}
    promotion_gate = threshold_candidate_evaluator.get("promotion_gate") or {}
    action_queue = snapshot.get("action_queue") or {}
    action_queue_hidden = action_queue.get("hidden_action_summary") or {}
    recovery_plan = snapshot.get("recovery_plan") or {}
    recovery_executor = snapshot.get("recovery_executor") or {}
    recovery_post_validation = snapshot.get("recovery_post_validation") or {}
    operational_runbook = snapshot.get("operational_runbook") or {}
    option_confidence_recovery = snapshot.get("option_confidence_recovery_plan") or {}
    safety_latency = snapshot.get("safety_gate_latency_budget") or {}
    dashboard_status_timeline = snapshot.get("dashboard_status_timeline") or {}
    opinion = snapshot.get("ai_opinion") or {}
    try:
        alerts = dashboard_alert_conditions(snapshot)
    except Exception:
        alerts = []
    alert_class_counts = {
        name: sum(1 for alert in alerts if _telegram_alert_class(alert) == name)
        for name in ("critical", "warning", "info")
    }
    lines = [
        "# Trading Command Center Snapshot",
        "",
        f"- Generated at: `{snapshot.get('generated_at')}`",
        f"- Mode: `{snapshot.get('mode')}`",
        f"- Status: display `{status.get('display_color') or status.get('color')}` / raw `{status.get('raw_color') or status.get('color')}` / label `{status.get('display_label') or '-'}` / reasons `{', '.join(status.get('reasons') or [])}`",
        f"- Operational/readiness: `{(((status.get('operational_readiness_overlay') or {}).get('operational_status') or {}).get('color') or '-')}` / `{(((status.get('operational_readiness_overlay') or {}).get('trading_readiness') or {}).get('label') or '-')}`",
        f"- Status categories: `{status.get('reason_categories') or []}` dominant `{status.get('dominant_reason_category') or '-'}`",
        f"- Trade-blocking split: `{(status.get('trade_blocking_summary') or {}).get('trade_blocking')}` blocking `{(status.get('trade_blocking_summary') or {}).get('trade_blocking_reasons') or []}` monitoring `{(status.get('trade_blocking_summary') or {}).get('monitoring_only_reasons') or []}`",
        f"- Status timeline: `{dashboard_status_timeline.get('status') or '-'}` transitions `{dashboard_status_timeline.get('transition_count') or 0}` rows `{dashboard_status_timeline.get('window_row_count') or 0}`",
        f"- Explanation: {status.get('natural_language_ko') or '-'}",
        f"- AI opinion: `{opinion.get('stance')}` - {opinion.get('note_ko') or ''}",
        "- Safety: read-only dashboard snapshot; no broker order submission or transmit.",
        "",
        "## Account / Margin",
        "",
        f"- Selected account: `{account.get('selected_account') or '-'}`",
        f"- Net liquidation: `{account.get('net_liquidation')}`",
        f"- Cash: `{account.get('cash')}`",
        f"- Buying power: `{account.get('buying_power')}`",
        f"- Available funds: `{account.get('available_funds')}`",
        f"- Initial / maintenance margin: `{account.get('initial_margin')}` / `{account.get('maintenance_margin')}`",
        "",
        "## Risk and mark quality",
        "",
        f"- Risk state: `{risk.get('risk_state')}` / action `{risk.get('recommended_action')}`",
        f"- Risk metrics: `{risk.get('metrics') or {}}`",
        f"- Mark refresh status counts: `{mark_quality.get('refresh_status_counts') or {}}`",
        f"- Quality actions: `{mark_quality.get('quality_action_counts') or {}}`",
        f"- Delayed / missing / fallback ticks: `{mark_quality.get('delayed_row_count')}` / `{mark_quality.get('missing_mark_count')}` / `{mark_quality.get('fallback_tick_count')}`",
        f"- Market temperature: `{market_summary.get('temperature_label')}` score `{market_summary.get('temperature_score')}` status `{market_data_desk.get('status') or market_summary.get('status')}`",
        f"- Market temperature diagnostics: causes `{market_temperature_diagnostics.get('root_causes') or []}` counts `{market_temperature_diagnostics.get('summary_counts') or {}}`",
        f"- Market temperature adjustment: `{market_temperature_adjustment.get('adjusted_interpretation') or '-'}` false-positive `{market_temperature_adjustment.get('false_positive_risk')}` trade-blocking `{market_temperature_adjustment.get('trade_blocking_market_temperature')}`",
        f"- Market temperature lifecycle: `{(market_temperature_adjustment.get('monitoring_only_lifecycle') or {}).get('policy') or {}}` thresholds `{(market_temperature_adjustment.get('thresholds') or {}).get('values') or {}}`",
        f"- Symbol anomaly quarantine: `{market_symbol_quarantine.get('status') or '-'}` candidates `{market_symbol_quarantine.get('candidate_count') or 0}` keys `{market_symbol_quarantine.get('candidate_keys') or []}`",
        f"- Market data live/delayed/missing/warming/alerts: `{market_summary.get('live_count')}` / `{market_summary.get('delayed_count')}` / `{market_summary.get('missing_price_count')}` / `{market_summary.get('warming_up_count')}` / `{market_summary.get('alert_count')}`",
        f"- Market session overlay: `{market_data_session_overlay.get('session_name') or '-'}` interpretation `{market_data_session_overlay.get('risk_interpretation') or '-'}` monitorable `{market_session_monitorability.get('monitorable_now') or 0}` regular-dependent `{market_session_monitorability.get('monitor_if_fresh_price_present') or 0}` stock overnight `{market_data_session_overlay.get('stock_cash_overnight_monitoring_enabled')}` option regular-only `{market_data_session_overlay.get('option_quotes_regular_session_dependent')}`",
        f"- Market data risk gate: `{market_data_risk_gate.get('gate_status') or '-'}` action `{market_data_risk_gate.get('action') or '-'}` size `{market_data_risk_gate.get('entry_size_multiplier') if market_data_risk_gate else '-'}`",
        f"- Operating auto-gate: `{market_data_operating_auto_gate.get('operating_mode') or '-'}` severity `{operating_defensive_mode.get('severity') or '-'}` auto-ready `{market_data_operating_auto_gate.get('auto_ready_count')}` protective candidates `{operating_protective_packet.get('candidate_count')}` ticketing `{operating_protective_packet.get('ticketing_allowed')}`",
        f"- Capital/margin state: `{capital_margin_state.get('status') or capital_gate_capital_state.get('status') or '-'}` leverage `{capital_margin_state.get('leverage_mode') or capital_gate_capital_state.get('leverage_mode') or '-'}` model `{capital_budgets.get('budget_model') or '-'}` cash budget `{capital_budgets.get('deployable_cash_after_reserve')}` options budget `{capital_budgets.get('options_premium_budget')}` micro reason `{capital_micro_budget_overlay.get('reason') or '-'}` max withdrawal `{capital_withdrawal_guard.get('max_safe_withdrawal')}`",
        f"- Capital-aware gate: ready `{capital_aware_action_gate.get('capital_ready_count')}` resized `{capital_aware_action_gate.get('capital_resized_count')}` blocked `{capital_aware_action_gate.get('capital_blocked_count')}` reasons `{capital_aware_action_gate.get('block_reason_counts') or {}}`",
        f"- Live read-only capital validation: status `{live_read_only_capital_validation.get('status')}` capital feasible/resize/blocked `{live_read_only_capital_validation.get('current_size_feasible_count')}`/`{live_read_only_capital_validation.get('resize_required_count')}`/`{live_read_only_capital_validation.get('blocked_count')}` trade current/resize/not `{live_read_only_capital_validation.get('trade_executable_current_size_count')}`/`{live_read_only_capital_validation.get('trade_executable_after_resize_count')}`/`{live_read_only_capital_validation.get('not_trade_executable_count')}` micro `{live_read_only_capital_validation.get('micro_policy_operating_mode')}` blocked `{live_read_only_capital_validation.get('micro_policy_blocked_count')}` blocked-by `{live_read_only_capital_validation.get('blocked_by_gate_counts') or {}}`",
        f"- Capital pipeline freshness: status `{capital_pipeline_orchestrator.get('status')}` stale/missing `{capital_pipeline_orchestrator.get('stale_or_missing_count')}` rebuild `{capital_pipeline_orchestrator.get('rebuild_required_count')}` steps `{capital_pipeline_orchestrator.get('rebuild_steps') or []}`",
        f"- Strategy allocation: ready `{strategy_allocation_engine.get('paper_trade_ready_count')}` hedge-only `{strategy_allocation_engine.get('hedge_only_count')}` blocked `{strategy_allocation_engine.get('blocked_count')}` micro `{strategy_allocation_engine.get('micro_policy_operating_mode')}` micro-blocked `{strategy_allocation_engine.get('micro_policy_blocked_count')}` cross-asset `{strategy_allocation_engine.get('top_cross_asset_expressions') or []}` expressions `{strategy_allocation_engine.get('expression_counts') or {}}`",
        f"- Micro option candidates: status `{micro_option_candidates.get('status') or '-'}` ready/stale/blocked `{micro_option_candidates.get('ready_count')}`/`{micro_option_candidates.get('stale_preview_count')}`/`{micro_option_candidates.get('blocked_count')}` budget `{micro_option_candidates.get('option_budget')}` quote fresh `{micro_option_candidates.get('quote_fresh')}` reasons `{micro_option_candidates.get('top_reason_counts') or {}}`",
        f"- Session transition gate: `{market_data_session_transition_gate.get('decision') or '-'}` severity `{market_data_session_transition_gate.get('severity') or '-'}` transition `{market_data_session_transition_gate.get('transition_type') or '-'}` rebuild `{market_data_session_transition_gate.get('rebuild_required_before_auto_release')}`",
        f"- Session release governor: `{session_transition_release_governor.get('release_status') or '-'}` alpha `{session_transition_release_governor.get('ordinary_alpha_release_allowed')}` size `{session_transition_release_governor.get('max_new_entry_size_multiplier')}` failed `{session_transition_release_governor.get('failed_check_count') or 0}`",
        f"- Release-governed plan blocks: primary `{plan.get('blocked_by_session_transition_release_governor_count') or 0}` operating `{market_data_operating_plan.get('blocked_by_session_transition_release_governor_count') or 0}`",
        f"- Defensive action plan: `{operating_defensive_plan.get('recommended_posture') or '-'}` actions `{operating_defensive_plan.get('action_count') or 0}` highest `{operating_defensive_plan.get('highest_priority_score') or 0}` chain/what-if `{operating_defensive_plan.get('requires_option_chain_count') or 0}` / `{operating_defensive_plan.get('requires_margin_whatif_count') or 0}`",
        f"- Defensive intent ledger: `{operating_defensive_ledger.get('recommended_posture') or '-'}` pending `{operating_defensive_ledger.get('pending_defensive_paper_execution_count') or 0}` blocked `{operating_defensive_ledger.get('blocked_count') or 0}` strict `{operating_defensive_ledger.get('strict_gate_mode')}`",
        f"- Evidence freshness: `{evidence_freshness.get('overall_status') or '-'}` stale `{len(evidence_freshness.get('stale_source_keys') or [])}` guard `{(evidence_freshness.get('scorer_guard') or {}).get('action') or '-'}`",
        f"- Evidence auto-recovery: `{evidence_auto_recovery.get('status') or '-'}` freshness `{evidence_auto_recovery.get('last_freshness_status') or '-'}` failures `{evidence_auto_recovery.get('consecutive_failures')}` current_gap_resolved `{status.get('evidence_auto_recovery_gap_currently_resolved')}` next `{evidence_auto_recovery.get('next_refresh_eligible_at') or '-'}`",
        f"- Source ingest health: `{source_ingest_health.get('overall_status') or '-'}` online problems `{source_ingest_health.get('online_problem_source_keys') or []}`",
        f"- Source supervisor: `{source_online_supervisor.get('final_status') or '-'}` actions `{source_online_supervisor.get('final_action_status_counts') or {}}` online problems `{source_online_supervisor.get('final_online_problem_source_keys') or []}`",
        f"- Action queue: `{action_queue.get('highest_severity') or '-'}` actions `{action_queue.get('visible_action_count') or 0}` / `{action_queue.get('total_action_count') or 0}` priority `{action_queue.get('highest_priority_score') or 0}`",
        f"- Option confidence recovery: `{option_confidence_recovery.get('status') or '-'}` roots `{option_confidence_recovery.get('root_cause_counts') or {}}` steps `{len(option_confidence_recovery.get('steps') or [])}`",
        f"- Safety gate latency: `{safety_latency.get('status') or '-'}` total_ms `{safety_latency.get('total_elapsed_ms')}` stages `{safety_latency.get('stage_count')}`",
        f"- Recovery plan: `{recovery_plan.get('highest_severity') or '-'}` steps `{recovery_plan.get('planned_step_count') or 0}` dry_run `{recovery_plan.get('dry_run', True)}`",
        f"- Recovery executor: `{recovery_executor.get('final_status') or '-'}` execute `{recovery_executor.get('execute_requested', False)}` commands `{recovery_executor.get('executed_command_count') or 0}` failures `{recovery_executor.get('failure_count') or 0}`",
        f"- Recovery post-validation: `{recovery_post_validation.get('outcome') or '-'}` improved `{recovery_post_validation.get('improved_metric_count') or 0}` worsened `{recovery_post_validation.get('worsened_metric_count') or 0}`",
        f"- Alert classes: `{alert_class_counts}`",
        f"- Operational runbook: `{operational_runbook.get('status') or '-'}` checks `{len(operational_runbook.get('immediate_checks') or [])}`",
        "",
        "## Action queue",
        "",
        f"- Hidden actions: `{action_queue_hidden.get('hidden_action_count') or 0}`; hidden promotion repair/recycle actions `{action_queue_hidden.get('promotion_repair_hidden_count') or 0}` ids `{action_queue_hidden.get('promotion_repair_hidden_action_ids') or []}`",
        "",
        "| Priority | Severity | Domain | Action | Recommendation |",
        "| ---: | --- | --- | --- | --- |",
    ]
    for item in action_queue.get("top_actions") or []:
        lines.append(
            f"| {item.get('priority_score')} | {item.get('severity')} | {item.get('domain')} | `{item.get('action_id')}` | {str(item.get('recommendation') or '-').replace('|', '/')} |"
        )
    if not action_queue.get("top_actions"):
        lines.append("| - | green | - | `none` | No priority action is required. |")
    lines.extend(
        [
            "",
            "## Status reason categories",
            "",
            "| Category | Count | Reasons |",
            "| --- | ---: | --- |",
        ]
    )
    for item in status.get("reason_categories") or []:
        lines.append(f"| `{item.get('label') or item.get('category')}` | {item.get('count')} | `{item.get('reasons') or []}` |")
    if not status.get("reason_categories"):
        lines.append("| `-` | 0 | `[]` |")
    lines.extend(
        [
            "",
            "## Status timeline",
            "",
            f"- Source: `{dashboard_status_timeline.get('source_path') or '-'}`",
            f"- Latest: `{dashboard_status_timeline.get('latest_status') or '-'}` transitions `{dashboard_status_timeline.get('transition_count') or 0}`",
            "",
            "| At | From | To | Changed | Reasons |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for item in dashboard_status_timeline.get("transitions") or []:
        lines.append(
            f"| `{item.get('at')}` | `{item.get('from_status')}` | `{item.get('to_status')}` | `{item.get('changed_fields') or []}` | `{item.get('to_reasons') or []}` |"
        )
    if not dashboard_status_timeline.get("transitions"):
        lines.append("| - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Option confidence recovery",
            "",
            f"- Status: `{option_confidence_recovery.get('status') or '-'}`",
            f"- Root causes: `{option_confidence_recovery.get('root_cause_counts') or {}}`",
            f"- Repair actions: `{option_confidence_recovery.get('repair_action_count') or 0}`",
            f"- Revival queue: `{option_confidence_recovery.get('revival_review_queue_count') or 0}` auto-promote `False`",
            "",
            "| Step | Why | Command hint |",
            "| --- | --- | --- |",
        ]
    )
    for item in option_confidence_recovery.get("steps") or []:
        lines.append(f"| `{item.get('step')}` | {str(item.get('why') or '-').replace('|', '/')} | `{item.get('command_hint') or '-'}` |")
    if not option_confidence_recovery.get("steps"):
        lines.append("| - | - | - |")
    lines.extend(["", "| Revival queue | Status | Lifecycle | New samples | Root causes | Required checks |", "| --- | --- | --- | ---: | --- | --- |"])
    for item in option_confidence_recovery.get("revival_review_queue") or []:
        lines.append(
            f"| `{item.get('candidate_key')}` | `{item.get('queue_status')}` | `{item.get('lifecycle_status')}` | {item.get('new_sample_count') if item.get('new_sample_count') is not None else '-'} | `{item.get('root_causes') or []}` | `{item.get('required_checks') or []}` |"
        )
    if not option_confidence_recovery.get("revival_review_queue"):
        lines.append("| - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Recovery plan",
            "",
            "| Order | Severity | Phase | Step | Status |",
            "| ---: | --- | --- | --- | --- |",
        ]
    )
    for item in recovery_plan.get("top_steps") or []:
        lines.append(
            f"| {item.get('execution_order')} | {item.get('severity')} | {item.get('phase')} | `{item.get('step_id')}` | {item.get('status')} |"
        )
    if not recovery_plan.get("top_steps"):
        lines.append("| - | green | - | `none` | no_action_required |")
    lines.extend(
        [
            "",
            "## Recovery executor",
            "",
            f"- Final status: `{recovery_executor.get('final_status') or '-'}`",
            f"- Execute requested: `{recovery_executor.get('execute_requested', False)}`",
            f"- Selected steps: `{recovery_executor.get('selected_step_count') or 0}`",
            f"- Executed commands / failures: `{recovery_executor.get('executed_command_count') or 0}` / `{recovery_executor.get('failure_count') or 0}`",
            "",
            "| Step | Status | Commands |",
            "| --- | --- | ---: |",
        ]
    )
    for item in recovery_executor.get("top_step_results") or []:
        lines.append(f"| `{item.get('step_id')}` | {item.get('status')} | {item.get('command_count')} |")
    if not recovery_executor.get("top_step_results"):
        lines.append("| `none` | no_executor_report | 0 |")
    lines.extend(
        [
            "",
            "## Recovery post-validation",
            "",
            f"- Outcome: `{recovery_post_validation.get('outcome') or '-'}`",
            f"- Improved / worsened / unchanged metrics: `{recovery_post_validation.get('improved_metric_count') or 0}` / `{recovery_post_validation.get('worsened_metric_count') or 0}` / `{recovery_post_validation.get('unchanged_metric_count') or 0}`",
            f"- Executor failures / executed commands: `{recovery_post_validation.get('executor_failure_count') or 0}` / `{recovery_post_validation.get('executor_executed_command_count') or 0}`",
        ]
    )
    _append_markdown_positions_options_sections(lines, locals())
    _append_markdown_plan_operating_sections(lines, locals())
    _append_markdown_news_evidence_learning_sections(lines, locals())
    _append_markdown_market_diagnostics_sections(lines, locals())
    return "\n".join(lines) + "\n"
