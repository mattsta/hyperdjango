"""
HyperAdmin template constants.

All inline HTML/CSS template strings used by the HyperAdmin interface.
These are self-contained Jinja2-style templates with HTMX integration.
"""

# ── Admin templates (inline, self-contained) ──────────────────────────────

_ADMIN_CSS = """
:root { --bg: #f8f9fa; --card: #fff; --primary: #2563eb; --danger: #dc2626;
        --border: #e5e7eb; --text: #111827; --muted: #6b7280; --success: #059669;
        --hover-bg: #f8fafc; --th-bg: #f1f5f9; --focus-ring: rgba(37,99,235,0.1);
        --btn-hover: #1d4ed8; --btn-danger-hover: #b91c1c; --btn-sec-hover: #d1d5db;
        --alert-success-bg: #ecfdf5; --alert-success-border: #a7f3d0;
        --alert-error-bg: #fef2f2; --alert-error-border: #fecaca;
        --toast-shadow: rgba(0,0,0,0.15); --dialog-shadow: rgba(0,0,0,0.12);
        --dialog-backdrop: rgba(0,0,0,0.4); }
[data-theme="dark"] {
        --bg: #0f172a; --card: #1e293b; --primary: #3b82f6; --danger: #ef4444;
        --border: #334155; --text: #f1f5f9; --muted: #94a3b8; --success: #10b981;
        --hover-bg: #1e293b; --th-bg: #1e293b; --focus-ring: rgba(59,130,246,0.2);
        --btn-hover: #2563eb; --btn-danger-hover: #dc2626; --btn-sec-hover: #475569;
        --alert-success-bg: #064e3b; --alert-success-border: #065f46;
        --alert-error-bg: #450a0a; --alert-error-border: #7f1d1d;
        --toast-shadow: rgba(0,0,0,0.4); --dialog-shadow: rgba(0,0,0,0.3);
        --dialog-backdrop: rgba(0,0,0,0.6); }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
        --bg: #0f172a; --card: #1e293b; --primary: #3b82f6; --danger: #ef4444;
        --border: #334155; --text: #f1f5f9; --muted: #94a3b8; --success: #10b981;
        --hover-bg: #1e293b; --th-bg: #1e293b; --focus-ring: rgba(59,130,246,0.2);
        --btn-hover: #2563eb; --btn-danger-hover: #dc2626; --btn-sec-hover: #475569;
        --alert-success-bg: #064e3b; --alert-success-border: #065f46;
        --alert-error-bg: #450a0a; --alert-error-border: #7f1d1d;
        --toast-shadow: rgba(0,0,0,0.4); --dialog-shadow: rgba(0,0,0,0.3);
        --dialog-backdrop: rgba(0,0,0,0.6); } }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
       background: var(--bg); color: var(--text); line-height: 1.6; }
.admin-layout { display: flex; min-height: 100vh; }
.sidebar { width: 220px; background: var(--card); border-right: 1px solid var(--border);
           padding: 0; flex-shrink: 0; position: fixed; top: 0; left: 0; bottom: 0;
           overflow-y: auto; z-index: 100; }
.sidebar-header { background: var(--primary); color: #fff; padding: 1rem; }
.sidebar-header h1 { font-size: 1rem; font-weight: 600; margin: 0; }
.sidebar-section { padding: 0.5rem 0; border-bottom: 1px solid var(--border); }
.sidebar-section-title { font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.08em;
                          color: var(--muted); padding: 0.5rem 1rem 0.25rem; font-weight: 600; }
.sidebar a { display: block; padding: 0.3rem 1rem; font-size: 0.8125rem; color: var(--text);
             text-decoration: none; border-left: 3px solid transparent; }
.sidebar a:hover { background: var(--hover-bg); border-left-color: var(--primary); text-decoration: none; }
.sidebar a.active { background: var(--focus-ring); border-left-color: var(--primary); font-weight: 600; }
.sidebar-footer { padding: 0.75rem 1rem; border-top: 1px solid var(--border); display: flex;
                   align-items: center; gap: 0.5rem; }
.sidebar-footer a { font-size: 0.8125rem; color: var(--muted); padding: 0; border: 0; }
.main-content { margin-left: 220px; flex: 1; padding: 1.5rem 2rem; min-width: 0; }
.container { max-width: 1100px; margin: 0; padding: 0; }
header { display: none; }
h2 { font-size: 1.5rem; margin-bottom: 1rem; }
a { color: var(--primary); text-decoration: none; }
a:hover { text-decoration: underline; }
a:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.btn:focus-visible { outline: 2px solid currentColor; outline-offset: 2px; }
table { width: 100%; border-collapse: collapse; background: var(--card);
        border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
th, td { text-align: left; padding: 0.625rem 1rem; border-bottom: 1px solid var(--border); }
th { background: var(--th-bg); font-weight: 600; font-size: 0.8125rem; text-transform: uppercase;
     letter-spacing: 0.05em; color: var(--muted); cursor: pointer; }
th a { color: var(--muted); }
th a:hover { color: var(--text); }
tr:hover td { background: var(--hover-bg); }
.btn { display: inline-block; padding: 0.5rem 1rem; border-radius: 6px; border: none;
       font-size: 0.875rem; font-weight: 500; cursor: pointer; text-decoration: none; }
.btn-primary { background: var(--primary); color: #fff; }
.btn-primary:hover { background: var(--btn-hover); text-decoration: none; }
.btn-danger { background: var(--danger); color: #fff; }
.btn-danger:hover { background: var(--btn-danger-hover); text-decoration: none; }
.btn-secondary { background: var(--border); color: var(--text); }
.btn-secondary:hover { background: var(--btn-sec-hover); text-decoration: none; }
form.inline { display: inline; }
.form-group { margin-bottom: 1rem; }
.form-group label { display: block; font-weight: 500; margin-bottom: 0.25rem; font-size: 0.875rem; }
.form-group input, .form-group select, .form-group textarea {
    width: 100%; padding: 0.5rem 0.75rem; border: 1px solid var(--border);
    border-radius: 6px; font-size: 0.875rem; font-family: inherit; }
.form-group input:focus, .form-group select:focus, .form-group textarea:focus {
    outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px var(--focus-ring); }
.form-group textarea { min-height: 120px; resize: vertical; }
.form-group input[type="checkbox"] { width: auto; margin-right: 0.5rem; }
.form-group .help { font-size: 0.75rem; color: var(--muted); margin-top: 0.125rem; }
.actions { margin-top: 1.5rem; display: flex; gap: 0.75rem; }
.alert { padding: 0.75rem 1rem; border-radius: 6px; margin-bottom: 1rem; font-size: 0.875rem; }
.alert-success { background: var(--alert-success-bg); color: var(--success); border: 1px solid var(--alert-success-border); }
.alert-error { background: var(--alert-error-bg); color: var(--danger); border: 1px solid var(--alert-error-border); }
.pagination { margin-top: 1rem; display: flex; gap: 0.25rem; align-items: center; }
.pagination a, .pagination span { padding: 0.375rem 0.75rem; border: 1px solid var(--border);
    border-radius: 4px; font-size: 0.8125rem; }
.pagination span.current { background: var(--primary); color: #fff; border-color: var(--primary); }
.meta { color: var(--muted); font-size: 0.8125rem; margin-bottom: 0.75rem; }
.card { background: var(--card); border-radius: 8px; padding: 1.5rem;
        box-shadow: 0 1px 3px rgba(0,0,0,0.06); margin-bottom: 1rem; }
.model-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 1rem; }
.model-card { background: var(--card); border-radius: 8px; padding: 1.25rem;
              box-shadow: 0 1px 3px rgba(0,0,0,0.06); border: 1px solid var(--border); }
.model-card h3 { font-size: 1.125rem; margin-bottom: 0.5rem; }
.model-card .count { color: var(--muted); font-size: 0.875rem; }
.search-bar { margin-bottom: 1rem; display: flex; gap: 0.5rem; }
.search-bar input { flex: 1; }
.toast { position: fixed; top: 1rem; right: 1rem; z-index: 1000; padding: 0.75rem 1.25rem;
         border-radius: 8px; font-size: 0.875rem; box-shadow: 0 4px 12px rgba(0,0,0,0.15);
         animation: toast-in 0.3s ease, toast-out 0.3s ease 2.7s forwards; }
.toast-success { background: #059669; color: #fff; }
.toast-error { background: var(--danger); color: #fff; }
@keyframes toast-in { from { opacity: 0; transform: translateY(-1rem); } to { opacity: 1; transform: translateY(0); } }
@keyframes toast-out { from { opacity: 1; } to { opacity: 0; } }
dialog { border: none; border-radius: 12px; padding: 0; box-shadow: 0 8px 30px rgba(0,0,0,0.12);
         max-width: 480px; width: 90%; }
dialog::backdrop { background: rgba(0,0,0,0.4); }
dialog .card { margin: 0; box-shadow: none; }
.htmx-indicator { display: none; }
.htmx-request .htmx-indicator { display: inline-block; }
.htmx-request.htmx-indicator { display: inline-block; }
.field-error { color: var(--danger); font-size: 0.75rem; margin-top: 0.125rem; }
.field-valid { color: var(--success); font-size: 0.75rem; margin-top: 0.125rem; }
.theme-toggle { cursor: pointer; background: none; border: 1px solid rgba(255,255,255,0.3);
    color: rgba(255,255,255,0.85); padding: 0.25rem 0.5rem; border-radius: 4px;
    font-size: 0.8125rem; margin-left: 0.75rem; }
.theme-toggle:hover { border-color: rgba(255,255,255,0.6); color: #fff; }
"""

_TEMPLATE_HEADER = (
    """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} — HyperAdmin</title>
<style{{ csp_nonce_attr(csp_nonce)|safe }}>"""
    + _ADMIN_CSS
    + """</style>
<script src="/static/htmx.min.js"{{ csp_nonce_attr(csp_nonce)|safe }}></script>
<script{{ csp_nonce_attr(csp_nonce)|safe }}>
function hyperFkAutocomplete(inputId,dropdownId,hiddenId){
  var inp=document.getElementById(inputId);
  var dd=document.getElementById(dropdownId);
  if(!inp||!dd)return;
  var url=inp.getAttribute('data-fk-url');
  var timer=null;
  inp.addEventListener('input',function(){
    clearTimeout(timer);
    var v=inp.value;
    timer=setTimeout(function(){
      fetch(url+'&q='+encodeURIComponent(v)).then(function(r){return r.text()}).then(function(h){
        dd.innerHTML=h;dd.style.display=h.trim()?'block':'none';
      });
    },250);
  });
  dd.addEventListener('click',function(e){
    var opt=e.target.closest('[data-pk]');
    if(opt){document.getElementById(hiddenId).value=opt.dataset.pk;inp.value=opt.textContent.trim();dd.style.display='none';}
  });
  document.addEventListener('click',function(e){
    if(!e.target.closest('#'+dropdownId)&&!e.target.closest('#'+inputId))dd.style.display='none';
  });
}
</script>
{{ extra_media|safe }}
</head><body>
<div class="admin-layout">
<aside class="sidebar" aria-label="Admin navigation">
<div class="sidebar-header"><h1>{{ admin_title }}</h1></div>
<div class="sidebar-section">
<div class="sidebar-section-title">Navigation</div>
<a href="{{ prefix }}/">Dashboard</a>
</div>
<div class="sidebar-section">
<div class="sidebar-section-title">Models</div>
{% for m in registered_models %}<a href="{{ prefix }}/{{ m.slug }}/">{{ m.name }}</a>{% endfor %}
</div>
<div class="sidebar-footer">
<a href="{{ prefix }}/logout/">Logout</a>
<button class="theme-toggle" onclick="toggleTheme()" title="Toggle dark/light mode" aria-label="Toggle dark/light mode" id="theme-btn">🌙</button>
</div>
</aside>
<main class="main-content">
<div class="container">"""
)

_THEME_JS = """<script{{ csp_nonce_attr(csp_nonce)|safe }}>
(function(){var t=localStorage.getItem('hyper-theme');if(t)document.documentElement.setAttribute('data-theme',t);
var b=document.getElementById('theme-btn');if(b)b.textContent=(t==='dark'||(!t&&matchMedia('(prefers-color-scheme:dark)').matches))?'☀️':'🌙';})();
function toggleTheme(){var r=document.documentElement,c=r.getAttribute('data-theme');
var n=c==='dark'?'light':(c==='light'?'dark':(matchMedia('(prefers-color-scheme:dark)').matches?'light':'dark'));
r.setAttribute('data-theme',n);localStorage.setItem('hyper-theme',n);
var b=document.getElementById('theme-btn');if(b)b.textContent=n==='dark'?'☀️':'🌙';}
</script>"""

_TEMPLATE_FOOTER = _THEME_JS + """</div></main></div></body></html>"""

TEMPLATE_DASHBOARD = (
    _TEMPLATE_HEADER
    + """
<h2>Dashboard</h2>
{% if has_auth_models or has_cache_dashboard %}
<div style="margin-bottom:1em;display:flex;flex-wrap:wrap;gap:0.5rem;">
{% if has_auth_models %}
  <a href="{{ prefix }}/permission-check/" style="display:inline-block;padding:6px 16px;background:#6366f1;color:#fff;border-radius:4px;text-decoration:none;font-size:0.9em">Permission Checker</a>
  <a href="{{ prefix }}/groups/tree/" style="display:inline-block;padding:6px 16px;background:#059669;color:#fff;border-radius:4px;text-decoration:none;font-size:0.9em">Role Hierarchy</a>
  <a href="{{ prefix }}/rbac-audit/" style="display:inline-block;padding:6px 16px;background:#dc2626;color:#fff;border-radius:4px;text-decoration:none;font-size:0.9em">RBAC Audit Log</a>
  <a href="{{ prefix }}/rbac-policy/" style="display:inline-block;padding:6px 16px;background:#f59e0b;color:#fff;border-radius:4px;text-decoration:none;font-size:0.9em">Policy Export/Import</a>
  <a href="{{ prefix }}/rbac-dashboard/" style="display:inline-block;padding:6px 16px;background:#8b5cf6;color:#fff;border-radius:4px;text-decoration:none;font-size:0.9em">RBAC Overview</a>
  <a href="{{ prefix }}/rate-limit-rules/" style="display:inline-block;padding:6px 16px;background:#0ea5e9;color:#fff;border-radius:4px;text-decoration:none;font-size:0.9em">Rate Limit Rules</a>
{% endif %}
{% if has_cache_dashboard %}
  <a href="{{ prefix }}/cache/" style="display:inline-block;padding:6px 16px;background:#14b8a6;color:#fff;border-radius:4px;text-decoration:none;font-size:0.9em">Cache Dashboard</a>
{% endif %}
</div>
{% endif %}
<div style="display:flex;gap:1.5rem;flex-wrap:wrap;">
<div style="flex:2;min-width:300px;">
<div class="model-grid">
{% for m in models %}
<div class="model-card">
<h3><a href="{{ prefix }}/{{ m.slug }}/">{{ m.name }}</a></h3>
<div class="count">{{ m.field_count }} fields</div>
<a href="{{ prefix }}/{{ m.slug }}/add/" class="btn btn-primary" style="margin-top:0.75rem;">Add new</a>
</div>
{% endfor %}
</div>
</div>
{% if recent_activity %}
<div style="flex:1;min-width:250px;">
<div class="card">
<h3 style="font-size:0.875rem;margin-bottom:0.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;">Recent Activity</h3>
{% for entry in recent_activity %}
<div style="padding:0.375rem 0;border-bottom:1px solid var(--border);font-size:0.8125rem;">
<span style="{% if entry.action == 'add' %}color:var(--success){% elif entry.action == 'delete' %}color:var(--danger){% else %}color:var(--primary){% endif %};">{{ entry.action }}</span>
<strong>{{ entry.object_repr }}</strong>
<span style="color:var(--muted);">({{ entry.model_name }})</span>
{% if entry.username %}<br><span style="color:var(--muted);font-size:0.75rem;">by {{ entry.username }}</span>{% endif %}
</div>
{% endfor %}
</div>
</div>
{% endif %}
</div>
"""
    + _TEMPLATE_FOOTER
)

_PARTIAL_URL = "{{ prefix }}/{{ slug }}/partial/"
_PARTIAL_PARAMS = "?q={{ search_query }}&sort={{ sort_field }}&dir={{ sort_dir }}{% for fname, fval in active_filters.items() %}{% if fval %}&filter_{{ fname }}={{ fval }}{% endif %}{% endfor %}"

# Partial template — just the result table + pagination, for HTMX swaps
TEMPLATE_LIST_PARTIAL = (
    """
<div id="result-table">
<div class="meta" aria-live="polite" aria-atomic="true">{{ total }} result{% if total != 1 %}s{% endif %}</div>
<form method="post" id="list-form">
{{ csrf_input | safe }}
{% if actions and perms.can_delete %}
<div style="display:flex;gap:0.5rem;margin-bottom:0.75rem;align-items:center;">
<select name="_action" aria-label="Bulk action" style="padding:0.375rem;border:1px solid var(--border);border-radius:4px;font-size:0.8125rem;">
<option value="">— Action —</option>
{% for act in actions %}<option value="{{ act.name }}">{{ act.label }}</option>{% endfor %}
</select>
<button type="submit" class="btn btn-secondary" style="padding:0.375rem 0.75rem;font-size:0.8125rem;" aria-label="Apply bulk action">Go</button>
</div>
{% endif %}
<table>
<tr>
{% if actions and perms.can_delete %}<th scope="col" style="width:2rem;"><input type="checkbox" aria-label="Select all rows" onclick="document.querySelectorAll('input[name=_selected]').forEach(c=>c.checked=this.checked)"></th>{% endif %}
{% for col in columns %}
<th scope="col"><a hx-get=\""""
    + _PARTIAL_URL
    + """?sort={{ col.name }}&dir={% if sort_field == col.name and sort_dir == 'asc' %}desc{% else %}asc{% endif %}&q={{ search_query }}{% for fname, fval in active_filters.items() %}{% if fval %}&filter_{{ fname }}={{ fval }}{% endif %}{% endfor %}"
       hx-target="#result-table" hx-swap="outerHTML" hx-push-url="true">
{{ col.label }}{% if sort_field == col.name %}{% if sort_dir == "asc" %} ▲{% else %} ▼{% endif %}{% endif %}</a></th>
{% endfor %}
<th scope="col">Actions</th>
</tr>
{% for row in rows %}
<tr>
{% if actions and perms.can_delete %}<td><input type="checkbox" name="_selected" value="{{ row.pk }}" aria-label="Select row {{ row.pk }}"></td>{% endif %}
{% for cell in row.cells %}
{% if cell.editable and perms.can_change %}
<td><input type="{{ cell.widget }}" name="{{ cell.field_name }}_{{ row.pk }}" value="{{ cell.value }}"
     style="padding:0.25rem 0.375rem;border:1px solid var(--border);border-radius:4px;font-size:0.8125rem;width:auto;max-width:12rem;"
     {% if cell.widget == "checkbox" %}{% if cell.raw_value %} checked{% endif %}{% endif %}></td>
{% else %}
<td>{% if cell.is_link %}<a href="{{ prefix }}/{{ slug }}/{{ row.pk }}/">{{ cell.display }}</a>{% else %}{{ cell.display }}{% endif %}</td>
{% endif %}
{% endfor %}
<td>
{% if perms.can_change %}<a href="{{ prefix }}/{{ slug }}/{{ row.pk }}/" class="btn btn-secondary" style="padding:0.25rem 0.5rem;font-size:0.75rem;">Edit</a>{% endif %}
{% if perms.can_delete %}<button class="btn btn-danger" style="padding:0.25rem 0.5rem;font-size:0.75rem;"
        hx-get="{{ prefix }}/{{ slug }}/{{ row.pk }}/confirm-delete/"
        hx-target="#delete-dialog" hx-swap="innerHTML" aria-label="Delete">Del</button>{% endif %}
</td>
</tr>
{% endfor %}
</table>
{% if list_editable and perms.can_change %}
<input type="hidden" name="_save_list_editable" value="1">
<button type="submit" class="btn btn-primary" style="margin-top:0.75rem;padding:0.375rem 1rem;font-size:0.875rem;" formaction="{{ prefix }}/{{ slug }}/save-list/">Save</button>
{% endif %}
</form>
{% if total_pages > 1 %}
<nav class="pagination" aria-label="Pagination">
{% if page > 1 %}<a hx-get=\""""
    + _PARTIAL_URL
    + """?page={{ page - 1 }}&sort={{ sort_field }}&dir={{ sort_dir }}&q={{ search_query }}{% for fname, fval in active_filters.items() %}{% if fval %}&filter_{{ fname }}={{ fval }}{% endif %}{% endfor %}"
   hx-target="#result-table" hx-swap="outerHTML" hx-push-url="true">← Prev</a>{% endif %}
{% for p in page_range %}
{% if p == page %}<span class="current" aria-current="page">{{ p }}</span>{% else %}
<a hx-get=\""""
    + _PARTIAL_URL
    + """?page={{ p }}&sort={{ sort_field }}&dir={{ sort_dir }}&q={{ search_query }}{% for fname, fval in active_filters.items() %}{% if fval %}&filter_{{ fname }}={{ fval }}{% endif %}{% endfor %}"
   hx-target="#result-table" hx-swap="outerHTML" hx-push-url="true">{{ p }}</a>{% endif %}
{% endfor %}
{% if page < total_pages %}<a hx-get=\""""
    + _PARTIAL_URL
    + """?page={{ page + 1 }}&sort={{ sort_field }}&dir={{ sort_dir }}&q={{ search_query }}{% for fname, fval in active_filters.items() %}{% if fval %}&filter_{{ fname }}={{ fval }}{% endif %}{% endfor %}"
   hx-target="#result-table" hx-swap="outerHTML" hx-push-url="true">Next →</a>{% endif %}
</nav>
{% endif %}
</div>
"""
)

TEMPLATE_LIST = (
    _TEMPLATE_HEADER
    + """
{% if message %}<div class="toast toast-success" role="status" aria-live="polite" onanimationend="if(event.animationName==='toast-out')this.remove()">{{ message }}</div>{% endif %}
{% if error_message %}<div class="toast toast-error" onanimationend="if(event.animationName==='toast-out')this.remove()">{{ error_message }}</div>{% endif %}
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;">
<h2>{{ model_name }}</h2>
{% if perms.can_add %}<a href="{{ prefix }}/{{ slug }}/add/" class="btn btn-primary">Add {{ model_name }}</a>{% endif %}
</div>
<input type="text" name="q" value="{{ search_query }}" placeholder="Search {{ model_name }}..." aria-label="Search {{ model_name }}"
       class="search-bar" style="width:100%;margin-bottom:1rem;padding:0.5rem 0.75rem;border:1px solid var(--border);border-radius:6px;font-size:0.875rem;"
       hx-get="{{ prefix }}/{{ slug }}/partial/"
       hx-trigger="input changed delay:300ms, search"
       hx-target="#result-table"
       hx-swap="outerHTML"
       hx-push-url="true"
       hx-include="[name='q']"
       name="q">
{% if date_hierarchy %}
<div style="margin-bottom:0.75rem;padding:0.5rem 0.75rem;background:var(--card-bg);border:1px solid var(--border);border-radius:6px;font-size:0.8125rem;">
{% if date_hierarchy.level == "month" or date_hierarchy.level == "day" %}
<a href="{{ prefix }}/{{ slug }}/" style="margin-right:0.5rem;">All dates</a> &raquo;
{% endif %}
{% if date_hierarchy.level == "month" %}
<strong>{{ date_hierarchy.year }}</strong>:
{% for item in date_hierarchy.items %}
<a href="{{ prefix }}/{{ slug }}/?dh_year={{ date_hierarchy.year }}&dh_month={{ item.value }}"
   style="margin:0 0.25rem;{% if date_hierarchy.active_month == item.value %}font-weight:700;{% endif %}">{{ item.label }}</a>
{% endfor %}
{% elif date_hierarchy.level == "day" %}
<a href="{{ prefix }}/{{ slug }}/?dh_year={{ date_hierarchy.year }}">{{ date_hierarchy.year }}</a> &raquo;
<strong>{{ date_hierarchy.month }}</strong>:
{% for item in date_hierarchy.items %}
<a href="{{ prefix }}/{{ slug }}/?dh_year={{ date_hierarchy.year }}&dh_month={{ date_hierarchy.month }}&dh_day={{ item.value }}"
   style="margin:0 0.25rem;{% if date_hierarchy.active_day == item.value %}font-weight:700;{% endif %}">{{ item.label }}</a>
{% endfor %}
{% else %}
{% for item in date_hierarchy.items %}
<a href="{{ prefix }}/{{ slug }}/?dh_year={{ item.value }}" style="margin:0 0.25rem;">{{ item.label }}</a>
{% endfor %}
{% endif %}
</div>
{% endif %}
<div style="display:flex;gap:1rem;">
<div style="flex:1;">
"""
    + TEMPLATE_LIST_PARTIAL
    + """
</div>
{% if filters %}
<div style="width:200px;flex-shrink:0;">
<div class="card" style="padding:1rem;">
<h3 style="font-size:0.875rem;margin-bottom:0.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;">Filters</h3>
{% for flt in filters %}
<div style="margin-bottom:0.75rem;">
<label style="font-size:0.8125rem;font-weight:600;display:block;margin-bottom:0.25rem;">{{ flt.label }}</label>
{% for opt in flt.options %}
<a hx-get="{{ prefix }}/{{ slug }}/partial/?filter_{{ flt.name }}={{ opt.value }}&q={{ search_query }}&sort={{ sort_field }}&dir={{ sort_dir }}"
   hx-target="#result-table" hx-swap="outerHTML" hx-push-url="true"
   style="display:block;font-size:0.8125rem;padding:0.125rem 0;cursor:pointer;{% if flt.active_value == opt.value %}font-weight:700;color:var(--primary);{% endif %}" role="button">{{ opt.label }}</a>
{% endfor %}
{% if flt.active_value %}<a hx-get="{{ prefix }}/{{ slug }}/partial/?q={{ search_query }}&sort={{ sort_field }}&dir={{ sort_dir }}"
   hx-target="#result-table" hx-swap="outerHTML" hx-push-url="true"
   style="display:block;font-size:0.75rem;color:var(--muted);margin-top:0.25rem;cursor:pointer;" role="button">Clear</a>{% endif %}
</div>
{% endfor %}
</div>
</div>
{% endif %}
</div>
<dialog id="delete-dialog" role="alertdialog" aria-label="Confirm deletion" onclick="if(event.target===this)this.close()"></dialog>
<script>document.body.addEventListener('htmx:afterSwap',function(e){if(e.detail.target.id==='delete-dialog')e.detail.target.showModal()})</script>
"""
    + _TEMPLATE_FOOTER
)

TEMPLATE_FORM = (
    _TEMPLATE_HEADER
    + """
<h2>{% if is_edit %}Edit{% else %}Add{% endif %} {{ model_name }}</h2>
{% if extra_links %}<div style="margin-bottom:1em">{% for link in extra_links %}<a href="{{ link.url }}" style="display:inline-block;padding:4px 12px;background:#6366f1;color:#fff;border-radius:4px;text-decoration:none;font-size:0.85em;margin-right:0.5em">{{ link.label }}</a>{% endfor %}</div>{% endif %}
{% if error %}<div class="alert alert-error" role="alert">{{ error }}</div>{% endif %}
<form method="post">
>
{{ csrf_input | safe }}
{% for group in field_groups %}
<div class="card" {% if "collapse" in group.classes %}style="margin-bottom:1rem;"{% endif %}>
{% if group.title %}<h3 style="font-size:1rem;margin-bottom:0.75rem;padding-bottom:0.5rem;border-bottom:1px solid var(--border);">{{ group.title }}</h3>{% endif %}
{% if group.description %}<p style="font-size:0.8125rem;color:var(--muted);margin-bottom:0.75rem;">{{ group.description }}</p>{% endif %}
{% for f in group.fields %}
{% if f.is_readonly %}
<div class="form-group">
<label>{{ f.label }}</label>
<div style="padding:0.5rem 0;color:var(--muted);">{{ f.value }}</div>
</div>
{% else %}
<div class="form-group">
{% if f.widget == "checkbox" %}
<label><input type="checkbox" name="{{ f.name }}" value="1" {% if f.value %}checked{% endif %}> {{ f.label }}</label>
{% elif f.widget == "textarea" %}
<label>{{ f.label }}{% if f.required %} *{% endif %}</label>
<textarea name="{{ f.name }}" {% if f.required %}required{% endif %} {% for k, v in f.attrs.items() %}{{ k }}="{{ v }}" {% endfor %}>{{ f.value }}</textarea>
{% elif f.radio_layout and f.choices %}
<label>{{ f.label }}{% if f.required %} *{% endif %}</label>
<div class="radio-group radio-{{ f.radio_layout }}" style="display:flex;{% if f.radio_layout == 'vertical' %}flex-direction:column;{% endif %}gap:0.5rem;margin-top:0.25rem;">
{% for opt_val, opt_label in f.choices %}
<label style="display:flex;align-items:center;gap:0.25rem;cursor:pointer;"><input type="radio" name="{{ f.name }}" value="{{ opt_val }}" {% if f.value == opt_val %}checked{% endif %}> {{ opt_label }}</label>
{% endfor %}
</div>
{% elif f.widget == "select" %}
<label>{{ f.label }}{% if f.required %} *{% endif %}</label>
<select name="{{ f.name }}" {% if f.required %}required{% endif %}>
<option value="">—</option>
{% for opt_val, opt_label in f.choices %}<option value="{{ opt_val }}" {% if f.value == opt_val %}selected{% endif %}>{{ opt_label }}</option>{% endfor %}
</select>
{% elif f.foreign_key %}
<label>{{ f.label }}{% if f.required %} *{% endif %}</label>
<div style="position:relative;">
<input type="hidden" name="{{ f.name }}" value="{{ f.value }}" id="fk_{{ f.name }}">
<input type="text" placeholder="Search {{ f.label }}..." autocomplete="off"
       value="{{ f.display_value }}"
       id="fk_search_{{ f.name }}"
       style="width:100%;"
       data-fk-url="{{ prefix }}/{{ slug }}/autocomplete/?field={{ f.name }}">
<div id="fk_results_{{ f.name }}" style="position:absolute;top:100%;left:0;right:0;z-index:100;background:var(--card);border:1px solid var(--border);border-radius:0 0 6px 6px;max-height:200px;overflow-y:auto;display:none;box-shadow:0 4px 12px rgba(0,0,0,0.15);"></div>
</div>
<script>hyperFkAutocomplete('fk_search_{{ f.name }}','fk_results_{{ f.name }}','fk_{{ f.name }}');</script>
{% else %}
<label>{{ f.label }}{% if f.required %} *{% endif %}</label>
<input type="{{ f.widget }}" name="{{ f.name }}" value="{{ f.value }}" {% if f.required %}required{% endif %} {% for k, v in f.attrs.items() %}{{ k }}="{{ v }}" {% endfor %}>
{% endif %}
{% if f.help %}<div class="help">{{ f.help }}</div>{% endif %}
</div>
{% endif %}
{% endfor %}
</div>
{% endfor %}
{{ inline_html|safe }}
{% if prepopulated_fields %}
<script>
(function(){
  var rules = {{ prepopulated_fields_json|safe }};
  function slugify(s){return s.toLowerCase().replace(/[^\\w\\s-]/g,'').replace(/[\\s_]+/g,'-').replace(/^-+|-+$/g,'').substring(0,200);}
  for(var target in rules){
    var sources=rules[target];
    var targetEl=document.querySelector('[name="'+target+'"]');
    if(!targetEl)continue;
    var changed=false;
    targetEl.addEventListener('input',function(){changed=true;});
    sources.forEach(function(src){
      var srcEl=document.querySelector('[name="'+src+'"]');
      if(srcEl){
        srcEl.addEventListener('input',function(){
          if(!changed)targetEl.value=slugify(srcEl.value);
        });
      }
    });
  }
})();
</script>
{% endif %}
{% if m2m_fields %}
{% for m2m_name, m2m in m2m_fields.items() %}
<div class="form-group" style="margin-top:1.5rem;">
<label style="font-weight:600;font-size:0.95em;">{{ m2m.label }}</label>
<div style="display:flex;gap:0.5rem;margin-top:0.5rem;">
<div style="flex:1;">
<div style="font-size:0.8em;color:var(--muted);margin-bottom:0.25rem;">Available</div>
<select id="m2m_avail_{{ m2m_name }}" multiple size="8" style="width:100%;border:1px solid var(--border);border-radius:6px;padding:0.25rem;background:var(--bg);color:var(--text);">
{% for opt_id, opt_label in m2m.available %}{% if opt_id not in m2m.selected %}<option value="{{ opt_id }}">{{ opt_label }}</option>{% endif %}{% endfor %}
</select>
</div>
<div style="display:flex;flex-direction:column;justify-content:center;gap:0.25rem;">
<button type="button" onclick="m2mMove('{{ m2m_name }}','add')" class="btn btn-secondary" style="padding:0.25rem 0.5rem;font-size:0.8em;" aria-label="Add selected items">→</button>
<button type="button" onclick="m2mMove('{{ m2m_name }}','remove')" class="btn btn-secondary" style="padding:0.25rem 0.5rem;font-size:0.8em;" aria-label="Remove selected items">←</button>
</div>
<div style="flex:1;">
<div style="font-size:0.8em;color:var(--muted);margin-bottom:0.25rem;">Chosen</div>
<select id="m2m_chosen_{{ m2m_name }}" multiple size="8" style="width:100%;border:1px solid var(--border);border-radius:6px;padding:0.25rem;background:var(--bg);color:var(--text);">
{% for opt_id, opt_label in m2m.available %}{% if opt_id in m2m.selected %}<option value="{{ opt_id }}">{{ opt_label }}</option>{% endif %}{% endfor %}
</select>
</div>
</div>
<div id="m2m_hidden_{{ m2m_name }}">
{% for sid in m2m.selected %}<input type="hidden" name="m2m_{{ m2m_name }}" value="{{ sid }}">{% endfor %}
</div>
</div>
{% endfor %}
<script>
function m2mMove(name, dir) {
  var avail = document.getElementById('m2m_avail_' + name);
  var chosen = document.getElementById('m2m_chosen_' + name);
  var hidden = document.getElementById('m2m_hidden_' + name);
  var source = dir === 'add' ? avail : chosen;
  var target = dir === 'add' ? chosen : avail;
  var selected = Array.from(source.selectedOptions);
  selected.forEach(function(opt) {
    source.removeChild(opt);
    target.appendChild(opt);
  });
  // Rebuild hidden inputs from chosen
  hidden.innerHTML = '';
  Array.from(chosen.options).forEach(function(opt) {
    var inp = document.createElement('input');
    inp.type = 'hidden';
    inp.name = 'm2m_' + name;
    inp.value = opt.value;
    hidden.appendChild(inp);
  });
}
// On form submit, ensure all chosen options are selected (for accessibility)
document.querySelector('form').addEventListener('submit', function() {
  document.querySelectorAll('[id^=m2m_chosen_]').forEach(function(sel) {
    Array.from(sel.options).forEach(function(opt) { opt.selected = true; });
  });
});
</script>
{% endif %}
{% if view_on_site_url %}<a href="{{ view_on_site_url }}" target="_blank" class="btn btn-secondary" style="margin-bottom:0.5rem;">View on site</a>{% endif %}
<div class="actions">
{% if not view_only %}
<button type="submit" class="btn btn-primary">{% if is_edit %}Save changes{% else %}Create{% endif %}</button>
{% if save_as and is_edit %}<button type="submit" name="_save_as" value="1" class="btn btn-secondary">Save as new</button>{% endif %}
{% endif %}
<a href="{{ prefix }}/{{ slug }}/" class="btn btn-secondary">{% if view_only %}Back{% else %}Cancel{% endif %}</a>
{% if is_edit and not view_only %}
<form class="inline" method="post" action="{{ prefix }}/{{ slug }}/{{ pk }}/delete/" onsubmit="return confirm('Delete?')" style="margin-left:auto;">
<button type="submit" class="btn btn-danger">Delete</button>
</form>
{% endif %}
</div>
</form>
"""
    + _TEMPLATE_FOOTER
)

TEMPLATE_CONFIRM_DELETE = (
    _TEMPLATE_HEADER
    + """
<h2>Delete {{ model_name }}</h2>
<div class="card">
<p>Are you sure you want to delete <strong>{{ instance_str }}</strong>?</p>
<div class="actions">
<form method="post">
>
{{ csrf_input | safe }}
<button type="submit" class="btn btn-danger">Yes, delete</button>
</form>
<a href="{{ prefix }}/{{ slug }}/{{ pk }}/" class="btn btn-secondary">Cancel</a>
</div>
</div>
"""
    + _TEMPLATE_FOOTER
)

# Dialog-based delete confirmation for HTMX (no full page)
TEMPLATE_DELETE_DIALOG = """
<div class="card" style="padding:1.5rem;">
<h3 style="margin-bottom:0.75rem;">Delete {{ model_name }}?</h3>
<p style="margin-bottom:1rem;color:var(--muted);">This will permanently delete <strong>{{ instance_str }}</strong>.</p>
<div style="display:flex;gap:0.5rem;">
<form method="post" action="{{ prefix }}/{{ slug }}/{{ pk }}/delete/"
      hx-post="{{ prefix }}/{{ slug }}/{{ pk }}/delete/" hx-target="body">
{{ csrf_input | safe }}
<button type="submit" class="btn btn-danger">Delete</button>
</form>
<button class="btn btn-secondary" onclick="this.closest('dialog').close()">Cancel</button>
</div>
</div>
"""

# Field validation response
TEMPLATE_FIELD_ERROR = """<div class="field-error" role="alert">{{ error }}</div>"""
TEMPLATE_FIELD_VALID = """<div class="field-valid" role="status">Valid</div>"""

# Object history view
TEMPLATE_HISTORY = (
    _TEMPLATE_HEADER
    + """
<h2>History: {{ object_repr }}</h2>
<p class="meta"><a href="{{ prefix }}/{{ slug }}/{{ pk }}/">← Back to edit</a></p>
{% if entries %}
<table>
<tr><th>Date</th><th>User</th><th>Action</th><th>Changes</th></tr>
{% for entry in entries %}
<tr>
<td style="white-space:nowrap;">{{ entry.timestamp }}</td>
<td>{{ entry.username }}</td>
<td>
{% if entry.action == "add" %}<span style="color:var(--success);">Created</span>
{% elif entry.action == "change" %}<span style="color:var(--primary);">Changed</span>
{% elif entry.action == "delete" %}<span style="color:var(--danger);">Deleted</span>
{% else %}{{ entry.action }}{% endif %}
</td>
<td style="font-size:0.8125rem;color:var(--muted);">{{ entry.changes }}</td>
</tr>
{% endfor %}
</table>
{% else %}
<p style="color:var(--muted);">No history recorded for this object.</p>
{% endif %}
"""
    + _TEMPLATE_FOOTER
)

# Login page
TEMPLATE_LOGIN = (
    """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login — {{ admin_title }}</title>
<style>"""
    + _ADMIN_CSS
    + """
.login-box { max-width: 400px; margin: 4rem auto; }
</style></head><body>
<header><div class="container" style="display:flex;align-items:center;">
<h1>{{ admin_title }}</h1>
</div></header>
<main>
<div class="container">
<div class="login-box">
<div class="card">
<h2 style="margin-bottom:1rem;">Log in</h2>
{% if error %}<div class="alert alert-error" role="alert">{{ error }}</div>{% endif %}
<form method="post">
{{ csrf_input | safe }}
<div class="form-group">
<label for="id_username">Username</label>
<input type="text" name="username" id="id_username" required autofocus aria-required="true">
</div>
<div class="form-group">
<label for="id_password">Password</label>
<input type="password" name="password" id="id_password" required aria-required="true">
</div>
<button type="submit" class="btn btn-primary" style="width:100%;">Log in</button>
</form>
</div>
</div>
</main></div></body></html>
"""
)

# Inline formset rendered inside the edit/add form
TEMPLATE_INLINE_SECTION = """
{% for inline in inlines %}
<div class="card" style="margin-top:1rem;" id="inline-{{ inline.slug }}">
<h3 style="font-size:1rem;margin-bottom:0.75rem;padding-bottom:0.5rem;border-bottom:1px solid var(--border);">
{{ inline.name }} ({{ inline.rows|length }} existing)
</h3>
<table style="width:100%;">
<tr>
{% for col in inline.columns %}<th style="font-size:0.75rem;">{{ col.label }}</th>{% endfor %}
{% if inline.can_delete %}<th style="width:3rem;font-size:0.75rem;">Delete</th>{% endif %}
</tr>
{% for row in inline.rows %}
<tr class="inline-row" id="inline-{{ inline.slug }}-{{ row.index }}">
<input type="hidden" name="{{ inline.prefix }}-{{ row.index }}-id" value="{{ row.pk }}">
{% for f in row.fields %}
<td>
{% if f.widget == "checkbox" %}
<input type="checkbox" name="{{ inline.prefix }}-{{ row.index }}-{{ f.name }}" value="1" {% if f.value %}checked{% endif %}>
{% elif f.widget == "select" %}
<select name="{{ inline.prefix }}-{{ row.index }}-{{ f.name }}" style="width:100%;padding:0.25rem;font-size:0.8125rem;">
<option value="">—</option>
{% for opt_val, opt_label in f.choices %}<option value="{{ opt_val }}" {% if f.value == opt_val %}selected{% endif %}>{{ opt_label }}</option>{% endfor %}
</select>
{% elif f.foreign_key %}
<div style="position:relative;">
<input type="hidden" name="{{ inline.prefix }}-{{ row.index }}-{{ f.name }}" value="{{ f.value }}" id="ifk_{{ inline.prefix }}_{{ row.index }}_{{ f.name }}">
<input type="text" placeholder="Search..." autocomplete="off" value="{{ f.display_value }}"
       style="width:100%;padding:0.25rem;font-size:0.8125rem;border:1px solid var(--border);border-radius:4px;"
       id="ifk_search_{{ inline.prefix }}_{{ row.index }}_{{ f.name }}"
       data-fk-url="{{ prefix }}/{{ inline.parent_slug }}/autocomplete/?field={{ f.name }}&fk_table={{ f.foreign_key }}">
<div id="ifk_dd_{{ inline.prefix }}_{{ row.index }}_{{ f.name }}" style="position:absolute;z-index:10;background:var(--card);border:1px solid var(--border);border-radius:0 0 6px 6px;max-height:200px;overflow-y:auto;width:100%;display:none;box-shadow:0 4px 12px rgba(0,0,0,0.15);"></div>
</div>
<script>hyperFkAutocomplete('ifk_search_{{ inline.prefix }}_{{ row.index }}_{{ f.name }}','ifk_dd_{{ inline.prefix }}_{{ row.index }}_{{ f.name }}','ifk_{{ inline.prefix }}_{{ row.index }}_{{ f.name }}');</script>
{% else %}
<input type="{{ f.widget }}" name="{{ inline.prefix }}-{{ row.index }}-{{ f.name }}" value="{{ f.value }}"
       style="width:100%;padding:0.25rem;font-size:0.8125rem;border:1px solid var(--border);border-radius:4px;"
       {% for k, v in f.attrs.items() %}{{ k }}="{{ v }}" {% endfor %}>
{% endif %}
</td>
{% endfor %}
{% if inline.can_delete %}<td><input type="checkbox" name="{{ inline.prefix }}-{{ row.index }}-DELETE" value="1" aria-label="Delete this row"></td>{% endif %}
</tr>
{% endfor %}
{% for row in inline.empty_rows %}
<tr class="inline-row" id="inline-{{ inline.slug }}-new-{{ row.index }}">
{% for f in row.fields %}
<td>
{% if f.widget == "checkbox" %}
<input type="checkbox" name="{{ inline.prefix }}-new-{{ row.index }}-{{ f.name }}" value="1">
{% elif f.widget == "select" and f.choices %}
<select name="{{ inline.prefix }}-new-{{ row.index }}-{{ f.name }}" style="width:100%;padding:0.25rem;font-size:0.8125rem;">
<option value="">—</option>
{% for opt_val, opt_label in f.choices %}<option value="{{ opt_val }}">{{ opt_label }}</option>{% endfor %}
</select>
{% elif f.foreign_key %}
<div style="position:relative;">
<input type="hidden" name="{{ inline.prefix }}-new-{{ row.index }}-{{ f.name }}" value="" id="ifk_{{ inline.prefix }}_new_{{ row.index }}_{{ f.name }}">
<input type="text" placeholder="Search..." autocomplete="off"
       style="width:100%;padding:0.25rem;font-size:0.8125rem;border:1px solid var(--border);border-radius:4px;"
       id="ifk_search_{{ inline.prefix }}_new_{{ row.index }}_{{ f.name }}"
       data-fk-url="{{ prefix }}/{{ inline.parent_slug }}/autocomplete/?field={{ f.name }}&fk_table={{ f.foreign_key }}">
<div id="ifk_dd_{{ inline.prefix }}_new_{{ row.index }}_{{ f.name }}" style="position:absolute;z-index:10;background:var(--card);border:1px solid var(--border);border-radius:0 0 6px 6px;max-height:200px;overflow-y:auto;width:100%;display:none;box-shadow:0 4px 12px rgba(0,0,0,0.15);"></div>
</div>
<script>hyperFkAutocomplete('ifk_search_{{ inline.prefix }}_new_{{ row.index }}_{{ f.name }}','ifk_dd_{{ inline.prefix }}_new_{{ row.index }}_{{ f.name }}','ifk_{{ inline.prefix }}_new_{{ row.index }}_{{ f.name }}');</script>
{% else %}
<input type="{{ f.widget }}" name="{{ inline.prefix }}-new-{{ row.index }}-{{ f.name }}" value="{{ f.value }}"
       style="width:100%;padding:0.25rem;font-size:0.8125rem;border:1px solid var(--border);border-radius:4px;"
       {% for k, v in f.attrs.items() %}{{ k }}="{{ v }}" {% endfor %}>
{% endif %}
</td>
{% endfor %}
{% if inline.can_delete %}<td></td>{% endif %}
</tr>
{% endfor %}
</table>
<input type="hidden" name="{{ inline.prefix }}-TOTAL" value="{{ inline.total }}">
<input type="hidden" name="{{ inline.prefix }}-INITIAL" value="{{ inline.initial }}">
<div style="margin-top:0.5rem;">
<button type="button" class="btn btn-secondary" style="font-size:0.75rem;padding:0.25rem 0.75rem;"
        hx-get="{{ prefix }}/{{ slug }}/inline-row/?inline={{ inline.slug }}&index={{ inline.next_index }}"
        hx-target="#inline-{{ inline.slug }} table"
        hx-swap="beforeend">+ Add {{ inline.name }}</button>
</div>
</div>
{% endfor %}
"""

# Single inline row returned by HTMX endpoint
TEMPLATE_INLINE_ROW = """
<tr class="inline-row" id="inline-{{ inline_slug }}-new-{{ index }}">
{% for f in fields %}
<td>
{% if f.widget == "checkbox" %}
<input type="checkbox" name="{{ prefix_name }}-new-{{ index }}-{{ f.name }}" value="1">
{% else %}
<input type="{{ f.widget }}" name="{{ prefix_name }}-new-{{ index }}-{{ f.name }}" value="{{ f.value }}"
       style="width:100%;padding:0.25rem;font-size:0.8125rem;border:1px solid var(--border);border-radius:4px;"
       {% for k, v in f.attrs.items() %}{{ k }}="{{ v }}" {% endfor %}>
{% endif %}
</td>
{% endfor %}
{% if can_delete %}<td></td>{% endif %}
</tr>
"""

# ── Effective Permissions View ────────────────────────────────────────────

TEMPLATE_EFFECTIVE_PERMS = (
    """
"""
    + _TEMPLATE_HEADER
    + """
<div style="margin-bottom:1em">
  <a href="{{ back_url }}">&larr; Back to user</a> |
  <a href="{{ prefix }}/permission-check/">Permission Checker</a>
</div>
<h2>{{ user_info.username }} (ID: {{ user_info.id }})</h2>
<p>
  {% if user_info.is_superuser %}<span style="background:#f59e0b;color:#000;padding:2px 8px;border-radius:3px">Superuser</span>{% endif %}
  {% if user_info.is_staff %}<span style="background:#3b82f6;color:#fff;padding:2px 8px;border-radius:3px">Staff</span>{% endif %}
  {% if not user_info.is_active %}<span style="background:#ef4444;color:#fff;padding:2px 8px;border-radius:3px">Inactive</span>{% endif %}
</p>

<h3>Groups / Roles</h3>
{% if groups %}
<table><thead><tr><th>Name</th><th>Parent ID</th><th>Priority</th></tr></thead><tbody>
{% for g in groups %}<tr><td>{{ g.name }}</td><td>{{ g.parent_id if g.parent_id else '—' }}</td><td>{{ g.priority }}</td></tr>{% endfor %}
</tbody></table>
{% else %}<p style="color:#888">No group memberships</p>{% endif %}

<h3>Direct Permissions</h3>
{% if direct_perms %}
<table><thead><tr><th>Permission</th><th>Model</th><th>Source</th></tr></thead><tbody>
{% for p in direct_perms %}<tr><td>{{ p.codename }}</td><td>{{ p.model_name }}</td><td><span style="background:#22c55e;color:#fff;padding:1px 6px;border-radius:3px">{{ p.source }}</span></td></tr>{% endfor %}
</tbody></table>
{% else %}<p style="color:#888">No direct permissions</p>{% endif %}

<h3>Inherited Permissions (via Group Hierarchy)</h3>
{% if inherited_perms %}
<table><thead><tr><th>Permission</th><th>Model</th><th>Source</th><th>Via</th></tr></thead><tbody>
{% for p in inherited_perms %}<tr><td>{{ p.codename }}</td><td>{{ p.model_name }}</td><td><span style="background:#3b82f6;color:#fff;padding:1px 6px;border-radius:3px">{{ p.source }}</span></td><td style="color:#888;font-size:0.9em">{{ p.via }}</td></tr>{% endfor %}
</tbody></table>
{% else %}<p style="color:#888">No inherited permissions</p>{% endif %}

<h3>Object-Level Permissions</h3>
{% if object_perms %}
<table><thead><tr><th>Permission</th><th>Model</th><th>Object ID</th><th>Source</th></tr></thead><tbody>
{% for p in object_perms %}<tr><td>{{ p.codename }}</td><td>{{ p.model_name }}</td><td><code>{{ p.object_id }}</code></td><td>{{ p.source }}</td></tr>{% endfor %}
</tbody></table>
{% else %}<p style="color:#888">No object-level permissions</p>{% endif %}

<h3>Conditional Rules</h3>
{% if rules %}
<table><thead><tr><th>Permission</th><th>Type</th><th>Effect</th><th>Priority</th><th>Scope</th><th>Config</th></tr></thead><tbody>
{% for r in rules %}<tr>
<td>{{ r.codename }}</td><td>{{ r.rule_type }}</td>
<td>{% if r.is_deny %}<span style="background:#ef4444;color:#fff;padding:1px 6px;border-radius:3px">DENY</span>{% else %}<span style="background:#22c55e;color:#fff;padding:1px 6px;border-radius:3px">ALLOW</span>{% endif %}</td>
<td>{{ r.priority }}</td><td>{{ r.scope }}</td>
<td style="font-size:0.85em"><code>{{ r.rule_config }}</code></td>
</tr>{% endfor %}
</tbody></table>
{% else %}<p style="color:#888">No conditional rules</p>{% endif %}

<h3>Field-Level Access</h3>
{% if field_access %}
<table><thead><tr><th>Model</th><th>Field</th><th>Access</th><th>Source</th></tr></thead><tbody>
{% for f in field_access %}<tr><td>{{ f.model_name }}</td><td>{{ f.field_name }}</td>
<td>{% if f.access == 'hidden' %}<span style="background:#ef4444;color:#fff;padding:1px 6px;border-radius:3px">Hidden</span>
{% elif f.access == 'readonly' %}<span style="background:#f59e0b;color:#000;padding:1px 6px;border-radius:3px">Read Only</span>
{% else %}<span style="background:#22c55e;color:#fff;padding:1px 6px;border-radius:3px">Writable</span>{% endif %}</td>
<td>{{ f.source }}</td></tr>{% endfor %}
</tbody></table>
{% else %}<p style="color:#888">No field-level restrictions</p>{% endif %}
"""
    + _TEMPLATE_FOOTER
)

# ── Group Hierarchy Tree View ─────────────────────────────────────────────

TEMPLATE_GROUP_TREE = (
    _TEMPLATE_HEADER
    + """
<div style="margin-bottom:1em">
  <a href="{{ prefix }}/groups/">&larr; Groups List</a> |
  <a href="{{ prefix }}/">Dashboard</a>
</div>
<h2>Group Hierarchy</h2>
<p style="color:#888;margin-bottom:1em">Role inheritance tree. Child groups inherit all permissions from their parent chain.</p>

{% if tree %}
<table>
<thead><tr><th>Group</th><th>Priority</th><th>Permissions</th><th>Members</th><th scope="col">Actions</th></tr></thead>
<tbody>
{% for g in tree %}
<tr>
<td style="padding-left:{{ g.depth * 24 + 8 }}px;">
  {% if g.depth > 0 %}<span style="color:#ccc;margin-right:4px;">{% for _ in range(g.depth) %}&nbsp;&nbsp;{% endfor %}&#x2514;</span>{% endif %}
  <strong>{{ g.name }}</strong>
  {% if g.parent_id %}<span style="color:#888;font-size:0.8em">(inherits)</span>{% else %}<span style="color:#3b82f6;font-size:0.8em">(root)</span>{% endif %}
</td>
<td>{{ g.priority }}</td>
<td><span style="background:#e0e7ff;color:#3730a3;padding:1px 8px;border-radius:10px;font-size:0.85em">{{ g.perm_count }}</span></td>
<td><span style="background:#fef3c7;color:#92400e;padding:1px 8px;border-radius:10px;font-size:0.85em">{{ g.member_count }}</span></td>
<td><a href="{{ prefix }}/groups/{{ g.id }}/" style="font-size:0.85em">Edit</a></td>
</tr>
{% endfor %}
</tbody>
</table>
{% else %}
<p style="color:#888">No groups defined yet. <a href="{{ prefix }}/groups/add/">Create one</a>.</p>
{% endif %}
"""
    + _TEMPLATE_FOOTER
)

# ── Permission Checker View ───────────────────────────────────────────────

TEMPLATE_PERM_CHECK = (
    """
"""
    + _TEMPLATE_HEADER
    + """
<div style="margin-bottom:1em">
  <a href="{{ prefix }}/">&larr; Dashboard</a>
</div>
<h2>Permission Checker</h2>
<p style="color:#888">Test whether a user has a specific permission. Shows the full decision chain.</p>

<form method="POST" style="background:#f8f9fa;padding:1em;border-radius:8px;margin-bottom:1.5em">
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:1em;margin-bottom:1em">
    <div>
      <label style="display:block;font-weight:600;margin-bottom:4px">User ID</label>
      <input type="number" name="user_id" value="{{ form_user_id if form_user_id else '' }}" required style="width:100%;padding:6px;border:1px solid #ccc;border-radius:4px">
    </div>
    <div>
      <label style="display:block;font-weight:600;margin-bottom:4px">Permission Codename</label>
      <input type="text" name="perm" value="{{ form_perm if form_perm else '' }}" placeholder="e.g. change_post" required style="width:100%;padding:6px;border:1px solid #ccc;border-radius:4px">
    </div>
    <div>
      <label style="display:block;font-weight:600;margin-bottom:4px">Model Name</label>
      <input type="text" name="model_name" value="{{ form_model if form_model else '' }}" placeholder="e.g. post" required style="width:100%;padding:6px;border:1px solid #ccc;border-radius:4px">
    </div>
  </div>
  <button type="submit" style="background:#2563eb;color:#fff;border:none;padding:8px 24px;border-radius:4px;cursor:pointer;font-size:1em">Check Permission</button>
</form>

{% if result %}
<div style="padding:1em;border-radius:8px;margin-bottom:1em;{% if result.allowed %}background:#f0fdf4;border:1px solid #22c55e{% else %}background:#fef2f2;border:1px solid #ef4444{% endif %}">
  <h3 style="margin-top:0">
    {% if result.allowed %}&#10004; ALLOWED{% else %}&#10008; DENIED{% endif %}
    <span style="font-weight:normal;font-size:0.9em;color:#666"> — {{ result.username }}</span>
  </h3>
</div>

<h3>Decision Chain</h3>
<table><thead><tr><th>#</th><th>Check</th><th>Result</th><th>Detail</th></tr></thead><tbody>
{% for step in result.steps %}
<tr>
  <td>{{ loop.index }}</td>
  <td><code>{{ step.check }}</code></td>
  <td>{% if step.result %}<span style="color:#22c55e;font-weight:bold">PASS</span>{% else %}<span style="color:#ef4444;font-weight:bold">FAIL</span>{% endif %}</td>
  <td>{{ step.detail }}</td>
</tr>
{% endfor %}
</tbody></table>
{% endif %}
"""
    + _TEMPLATE_FOOTER
)

# ── RBAC Audit Log View ──────────────────────────────────────────────────

TEMPLATE_RBAC_AUDIT = (
    _TEMPLATE_HEADER
    + """
<div style="margin-bottom:1em">
  <a href="{{ prefix }}/">&larr; Dashboard</a>
</div>
<h2>RBAC Audit Log</h2>
<p style="color:#888;margin-bottom:1em">Tracks all permission changes: grants, revocations, group membership changes, rule additions, and field access modifications.</p>

{% if entries %}
<table>
<thead><tr><th>Time</th><th>Action</th><th>Target</th><th>ID</th><th>Detail</th><th>Actor</th></tr></thead>
<tbody>
{% for e in entries %}
<tr>
<td style="font-size:0.8em;color:#888;white-space:nowrap">{{ e.timestamp }}</td>
<td>
  {% if "grant" in e.action %}<span style="background:#22c55e;color:#fff;padding:1px 8px;border-radius:3px;font-size:0.85em">{{ e.action }}</span>
  {% elif "revoke" in e.action or "remove" in e.action %}<span style="background:#ef4444;color:#fff;padding:1px 8px;border-radius:3px;font-size:0.85em">{{ e.action }}</span>
  {% else %}<span style="background:#3b82f6;color:#fff;padding:1px 8px;border-radius:3px;font-size:0.85em">{{ e.action }}</span>{% endif %}
</td>
<td>{{ e.target_type }}</td>
<td><code>{{ e.target_id }}</code></td>
<td style="font-size:0.85em"><code>{{ e.detail }}</code></td>
<td style="font-size:0.85em;color:#888">{{ e.actor_username if e.actor_username else '&#8212;' }}</td>
</tr>
{% endfor %}
</tbody>
</table>
{% else %}
<p style="color:#888">No RBAC changes recorded yet.</p>
{% endif %}
"""
    + _TEMPLATE_FOOTER
)

TEMPLATE_RBAC_EXPORT = (
    _TEMPLATE_HEADER
    + """
<div style="margin-bottom:1em">
  <a href="{{ prefix }}/">&larr; Dashboard</a>
</div>
<h2>RBAC Policy Export/Import</h2>
<p style="color:#888;margin-bottom:1em">Backup, migrate, or restore your entire RBAC policy (groups, permissions, rules, field access).</p>

<div style="display:flex;gap:2rem;flex-wrap:wrap;">
<div style="flex:1;min-width:300px;">
<div class="card" style="padding:1.5rem;">
<h3 style="margin-bottom:1rem;">Export Policy</h3>
<p style="color:#888;font-size:0.875rem;margin-bottom:1rem;">Download the complete RBAC policy as JSON. Includes groups, permissions, assignments, object perms, rules, and field access.</p>
<a href="{{ prefix }}/rbac-export/download/" class="btn btn-primary" style="display:inline-block;padding:8px 20px;background:#3b82f6;color:#fff;border-radius:4px;text-decoration:none;">Download JSON</a>
{% if export_stats %}
<div style="margin-top:1rem;font-size:0.8125rem;color:#888;">
  <strong>Current policy:</strong>
  {{ export_stats.groups }} groups,
  {{ export_stats.permissions }} permissions,
  {{ export_stats.rules }} rules,
  {{ export_stats.field_permissions }} field restrictions
</div>
{% endif %}
</div>
</div>

<div style="flex:1;min-width:300px;">
<div class="card" style="padding:1.5rem;">
<h3 style="margin-bottom:1rem;">Import Policy</h3>
<p style="color:#888;font-size:0.875rem;margin-bottom:1rem;">Upload a JSON policy file. By default merges with existing data. Check "Replace" to wipe and replace all RBAC data.</p>
<form method="post" enctype="multipart/form-data" action="{{ prefix }}/rbac-import/">
  <div style="margin-bottom:1rem;">
    <input type="file" name="policy_file" accept=".json,application/json" required style="font-size:0.875rem;">
  </div>
  <div style="margin-bottom:1rem;">
    <label style="font-size:0.875rem;cursor:pointer;">
      <input type="checkbox" name="clear_existing" value="1">
      Replace all existing RBAC data (destructive)
    </label>
  </div>
  <button type="submit" class="btn btn-primary" style="padding:8px 20px;background:#dc2626;color:#fff;border:none;border-radius:4px;cursor:pointer;">Import Policy</button>
</form>
{% if import_result %}
<div style="margin-top:1rem;padding:0.75rem;border-radius:4px;{% if import_result.errors %}background:#fef2f2;border:1px solid #fca5a5;{% else %}background:#f0fdf4;border:1px solid #86efac;{% endif %}">
  <strong>{% if import_result.errors %}Import completed with errors{% else %}Import successful{% endif %}</strong>
  <div style="font-size:0.8125rem;margin-top:0.5rem;">
  {% for section, count in import_result.imported.items() %}
    {{ section }}: {{ count }}{% if not loop.last %}, {% endif %}
  {% endfor %}
  </div>
  {% if import_result.errors %}
  <div style="font-size:0.8125rem;color:#dc2626;margin-top:0.5rem;">
  {% for err in import_result.errors %}
    <div>{{ err }}</div>
  {% endfor %}
  </div>
  {% endif %}
</div>
{% endif %}
</div>
</div>
</div>
"""
    + _TEMPLATE_FOOTER
)

TEMPLATE_RBAC_DASHBOARD = (
    _TEMPLATE_HEADER
    + """
<div style="margin-bottom:1em">
  <a href="{{ prefix }}/">&larr; Dashboard</a>
</div>
<h2>RBAC Overview</h2>

<div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-bottom:2rem;">
  <div class="card" style="flex:1;min-width:180px;padding:1.25rem;text-align:center;">
    <div style="font-size:2rem;font-weight:700;color:#3b82f6;">{{ stats.total_groups }}</div>
    <div style="font-size:0.8125rem;color:#888;">Groups/Roles</div>
  </div>
  <div class="card" style="flex:1;min-width:180px;padding:1.25rem;text-align:center;">
    <div style="font-size:2rem;font-weight:700;color:#059669;">{{ stats.total_permissions }}</div>
    <div style="font-size:0.8125rem;color:#888;">Permissions</div>
  </div>
  <div class="card" style="flex:1;min-width:180px;padding:1.25rem;text-align:center;">
    <div style="font-size:2rem;font-weight:700;color:#6366f1;">{{ stats.total_users }}</div>
    <div style="font-size:0.8125rem;color:#888;">Active Users</div>
  </div>
  <div class="card" style="flex:1;min-width:180px;padding:1.25rem;text-align:center;">
    <div style="font-size:2rem;font-weight:700;color:#dc2626;">{{ stats.total_rules }}</div>
    <div style="font-size:0.8125rem;color:#888;">Conditional Rules</div>
  </div>
</div>

{% if stats.users_per_group %}
<div class="card" style="padding:1.25rem;margin-bottom:1.5rem;">
<h3 style="font-size:0.875rem;margin-bottom:1rem;color:#888;text-transform:uppercase;letter-spacing:0.05em;">Users per Group</h3>
<table>
<thead><tr><th>Group</th><th>Members</th><th>Bar</th></tr></thead>
<tbody>
{% for row in stats.users_per_group %}
<tr>
<td>{{ row.name }}</td>
<td>{{ row.count }}</td>
<td><div style="background:#3b82f6;height:14px;border-radius:2px;width:{{ row.pct }}%"></div></td>
</tr>
{% endfor %}
</tbody>
</table>
</div>
{% endif %}

{% if stats.permission_coverage %}
<div class="card" style="padding:1.25rem;margin-bottom:1.5rem;">
<h3 style="font-size:0.875rem;margin-bottom:1rem;color:#888;text-transform:uppercase;letter-spacing:0.05em;">Permission Coverage by Model</h3>
<table>
<thead><tr><th>Model</th><th>Permissions</th><th>Assigned Groups</th><th>Assigned Users</th></tr></thead>
<tbody>
{% for row in stats.permission_coverage %}
<tr>
<td><strong>{{ row.model_name }}</strong></td>
<td>{{ row.perm_count }}</td>
<td>{{ row.group_count }}</td>
<td>{{ row.user_count }}</td>
</tr>
{% endfor %}
</tbody>
</table>
</div>
{% endif %}

{% if stats.orphaned_permissions %}
<div class="card" style="padding:1.25rem;margin-bottom:1.5rem;">
<h3 style="font-size:0.875rem;margin-bottom:1rem;color:#888;text-transform:uppercase;letter-spacing:0.05em;">Orphaned Permissions (unassigned)</h3>
<div style="display:flex;flex-wrap:wrap;gap:0.5rem;">
{% for p in stats.orphaned_permissions %}
<span style="background:#fef2f2;color:#dc2626;padding:2px 10px;border-radius:3px;font-size:0.8125rem;">{{ p.model_name }}.{{ p.codename }}</span>
{% endfor %}
</div>
</div>
{% endif %}

{% if stats.recent_changes %}
<div class="card" style="padding:1.25rem;">
<h3 style="font-size:0.875rem;margin-bottom:1rem;color:#888;text-transform:uppercase;letter-spacing:0.05em;">Recent RBAC Changes</h3>
<table>
<thead><tr><th>Time</th><th>Action</th><th>Target</th><th>Actor</th></tr></thead>
<tbody>
{% for e in stats.recent_changes %}
<tr>
<td style="font-size:0.8em;color:#888;white-space:nowrap">{{ e.timestamp }}</td>
<td>
  {% if "grant" in e.action %}<span style="background:#22c55e;color:#fff;padding:1px 8px;border-radius:3px;font-size:0.85em">{{ e.action }}</span>
  {% elif "revoke" in e.action or "remove" in e.action %}<span style="background:#ef4444;color:#fff;padding:1px 8px;border-radius:3px;font-size:0.85em">{{ e.action }}</span>
  {% else %}<span style="background:#3b82f6;color:#fff;padding:1px 8px;border-radius:3px;font-size:0.85em">{{ e.action }}</span>{% endif %}
</td>
<td>{{ e.target_type }} <code>{{ e.target_id }}</code></td>
<td style="font-size:0.85em;color:#888">{{ e.actor_username if e.actor_username else '&#8212;' }}</td>
</tr>
{% endfor %}
</tbody>
</table>
</div>
{% endif %}
"""
    + _TEMPLATE_FOOTER
)

TEMPLATE_CACHE_DASHBOARD = (
    _TEMPLATE_HEADER
    + """
<div style="margin-bottom:1em">
  <a href="{{ prefix }}/">&larr; Dashboard</a>
  <a href="{{ prefix }}/cache/json" style="float:right;font-size:0.85em;color:#888;text-decoration:none;">JSON API &rarr;</a>
</div>
<h2>Cache Dashboard</h2>
<meta http-equiv="refresh" content="5">

<div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-bottom:2rem;">
  <div class="card" style="flex:1;min-width:180px;padding:1.25rem;text-align:center;">
    <div style="font-size:2rem;font-weight:700;color:#3b82f6;">{{ stats.query_cache.hit_rate }}</div>
    <div style="font-size:0.8125rem;color:#888;">Query Cache Hit Rate</div>
  </div>
  <div class="card" style="flex:1;min-width:180px;padding:1.25rem;text-align:center;">
    <div style="font-size:2rem;font-weight:700;color:#059669;">{{ stats.query_cache.total_requests }}</div>
    <div style="font-size:0.8125rem;color:#888;">Total Requests</div>
  </div>
  <div class="card" style="flex:1;min-width:180px;padding:1.25rem;text-align:center;">
    <div style="font-size:2rem;font-weight:700;color:#dc2626;">{{ stats.query_cache.invalidations }}</div>
    <div style="font-size:0.8125rem;color:#888;">Invalidations</div>
  </div>
  <div class="card" style="flex:1;min-width:180px;padding:1.25rem;text-align:center;">
    <div style="font-size:2rem;font-weight:700;color:#6366f1;">{{ stats.query_cache.table_count }}</div>
    <div style="font-size:0.8125rem;color:#888;">Tables Tracked</div>
  </div>
</div>

<div class="card" style="padding:1.25rem;margin-bottom:1.5rem;">
<h3 style="font-size:0.875rem;margin-bottom:1rem;color:#888;text-transform:uppercase;letter-spacing:0.05em;">Query Cache Details</h3>
<table>
<thead><tr><th>Metric</th><th>Value</th></tr></thead>
<tbody>
<tr><td>Hits</td><td>{{ stats.query_cache.hits }}</td></tr>
<tr><td>Misses</td><td>{{ stats.query_cache.misses }}</td></tr>
<tr><td>Sets</td><td>{{ stats.query_cache.sets }}</td></tr>
<tr><td>Table Invalidations</td><td>{{ stats.query_cache.table_invalidations }}</td></tr>
<tr><td>Row Invalidations</td><td>{{ stats.query_cache.row_invalidations }}</td></tr>
</tbody>
</table>
</div>

{% if stats.table_versions %}
<div class="card" style="padding:1.25rem;margin-bottom:1.5rem;">
<h3 style="font-size:0.875rem;margin-bottom:1rem;color:#888;text-transform:uppercase;letter-spacing:0.05em;">Table Versions</h3>
<table>
<thead><tr><th>Table</th><th>Version</th></tr></thead>
<tbody>
{% for t in stats.table_versions %}
<tr><td><code>{{ t.name }}</code></td><td>{{ t.version }}</td></tr>
{% endfor %}
</tbody>
</table>
</div>
{% endif %}

{% if stats.two_tier %}
<div class="card" style="padding:1.25rem;margin-bottom:1.5rem;">
<h3 style="font-size:0.875rem;margin-bottom:1rem;color:#888;text-transform:uppercase;letter-spacing:0.05em;">Two-Tier Cache (L1 + L2)</h3>
<table>
<thead><tr><th>Tier</th><th>Hits</th><th>Rate</th><th>Bar</th></tr></thead>
<tbody>
<tr>
  <td>L1 (local)</td><td>{{ stats.two_tier.l1_hits }}</td>
  <td>{{ stats.two_tier.l1_hit_rate_pct }}%</td>
  <td><div style="background:#3b82f6;height:14px;border-radius:2px;width:{{ stats.two_tier.l1_hit_rate_pct }}%"></div></td>
</tr>
<tr>
  <td>L2 (shared)</td><td>{{ stats.two_tier.l2_hits }}</td>
  <td>{{ stats.two_tier.l2_hit_rate_pct }}%</td>
  <td><div style="background:#059669;height:14px;border-radius:2px;width:{{ stats.two_tier.l2_hit_rate_pct }}%"></div></td>
</tr>
<tr>
  <td>Misses</td><td>{{ stats.two_tier.misses }}</td>
  <td colspan="2"></td>
</tr>
</tbody>
</table>
<div style="margin-top:0.75rem;">
  <strong>Overall hit rate:</strong>
  <div style="background:#e5e7eb;border-radius:4px;height:20px;margin-top:4px;overflow:hidden;">
    <div style="background:#14b8a6;height:100%;width:{{ stats.two_tier.overall_hit_rate_pct }}%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:0.75rem;font-weight:600;">{{ stats.two_tier.overall_hit_rate_pct }}%</div>
  </div>
</div>
</div>
{% endif %}

{% if stats.locmem %}
<div class="card" style="padding:1.25rem;">
<h3 style="font-size:0.875rem;margin-bottom:1rem;color:#888;text-transform:uppercase;letter-spacing:0.05em;">LocMemCache</h3>
<table>
<thead><tr><th>Metric</th><th>Value</th></tr></thead>
<tbody>
<tr><td>Entries</td><td>{{ stats.locmem.entry_count }}</td></tr>
<tr><td>Max Size</td><td>{{ stats.locmem.max_size }}</td></tr>
<tr><td>Utilization</td><td>{{ stats.locmem.utilization }}</td></tr>
</tbody>
</table>
</div>
{% endif %}
"""
    + _TEMPLATE_FOOTER
)
