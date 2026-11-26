"""The app's stylesheet, built from hotato.theme so the app and the HTML
reports share one palette."""

from __future__ import annotations

from hotato.theme import REPORT as _C

APP_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:%(bg)s;color:%(cream)s;
 font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 font-size:15px;line-height:1.5}
.mono,code,pre,textarea.code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
a{color:%(info)s}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{
 outline:2px solid %(ember)s;outline-offset:2px}
.skip{position:absolute;left:-999px}.skip:focus{left:8px;top:8px;background:%(card)s;padding:8px;z-index:9}
.wrap{max-width:1040px;margin:0 auto;padding:20px 16px 56px}
header.top{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;
 border-bottom:1px solid %(line)s;padding-bottom:14px;margin-bottom:8px}
.brand{display:flex;align-items:center;gap:10px;color:%(cream)s;text-decoration:none;font-weight:700;font-size:20px}
.logo{width:26px;height:26px;border-radius:8px;background:%(ember)s;flex:none}
.grow{flex:1}
nav.main{display:flex;flex-wrap:wrap;gap:4px;margin:0 0 20px;padding:0}
nav.main a{color:%(muted)s;text-decoration:none;padding:8px 12px;border-radius:9px;border:1px solid transparent}
nav.main a[aria-current=page]{color:%(cream)s;background:%(card2)s;border-color:%(line)s}
nav.main a:hover{color:%(cream)s}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-0.01em}
h2{font-size:17px;margin:0 0 10px}
.sub{color:%(muted)s;margin:0 0 18px}
.muted{color:%(muted)s}
.card{background:%(card)s;border:1px solid %(line)s;border-radius:16px;padding:16px 18px;margin-bottom:16px;min-width:0}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));margin-bottom:16px}
.stat{background:%(card)s;border:1px solid %(line)s;border-radius:14px;padding:14px 16px}
.stat .k{font-size:12px;color:%(muted)s}
.stat .v{font-size:28px;font-weight:700;line-height:1.2}
.stat .spark{color:%(ember)s;margin-top:6px;display:block;max-width:100%%}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%%;font-size:14px}
th,td{text-align:left;padding:8px 12px 8px 0;border-bottom:1px solid %(line)s;vertical-align:top}
th{color:%(muted)s;font-weight:600;font-size:12px;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.chip{display:inline-block;border-radius:999px;padding:1px 10px;font-size:12px;font-weight:600;
 background:%(card2)s;border:1px solid %(line)s;white-space:nowrap}
.chip.pass{background:%(green)s;color:%(on_accent)s;border-color:%(green)s}
.chip.fail{background:%(red)s;color:%(on_accent)s;border-color:%(red)s}
.chip.unknown{background:%(attention)s;color:%(on_accent)s;border-color:%(attention)s}
label{display:block;font-size:13px;color:%(muted)s;margin:10px 0 4px}
input,select,textarea{width:100%%;max-width:100%%;background:%(bg)s;color:%(cream)s;
 border:1px solid %(line)s;border-radius:9px;padding:9px 11px;font:inherit}
textarea.code{min-height:320px;font-size:13px;line-height:1.45}
.inline{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
.inline>div{flex:1 1 150px;min-width:0}
.inline label{margin-top:0}
button,.btn{background:%(ember)s;color:%(on_accent)s;border:0;border-radius:9px;padding:9px 16px;
 font:inherit;font-weight:650;cursor:pointer;text-decoration:none;display:inline-block}
button.secondary,.btn.secondary{background:%(card2)s;color:%(cream)s;border:1px solid %(line)s}
button.danger{background:%(red)s}
.err{border:1px solid %(red)s;background:%(red_wash)s;border-radius:10px;padding:10px 12px;margin:0 0 14px}
.ok{border:1px solid %(green)s;border-radius:10px;padding:10px 12px;margin:0 0 14px}
.secret{word-break:break-all;background:%(bg)s;border:1px dashed %(ember)s;border-radius:10px;padding:10px 12px}
pre{background:%(bg)s;border:1px solid %(line)s;border-radius:10px;padding:10px 12px;overflow-x:auto;margin:6px 0}
.turn{display:flex;gap:10px;padding:6px 0;border-bottom:1px solid %(line)s}
.turn .who{flex:0 0 64px;font-size:12px;font-weight:700}
.turn.caller .who{color:%(caller)s}.turn.agent .who{color:%(agent)s}
.turn .t{flex:0 0 52px;color:%(muted)s;font-size:12px}
.turn .txt{flex:1;min-width:0;overflow-wrap:anywhere}
.auth{max-width:420px;margin:8vh auto 0;padding:0 16px}
.conn{border-top:1px solid %(line)s;padding:10px 0}
.nm{margin:0}.fit{flex:0 0 auto!important}.di{display:inline}.stat .v.sm{font-size:17px}.h3{font-size:14px}
input.auto{width:auto}
.pager{display:flex;gap:10px;align-items:center;margin-top:12px}
details>summary{cursor:pointer;padding:6px 0}
@media (max-width:560px){.turn{flex-wrap:wrap}.turn .who,.turn .t{flex:0 0 auto}}
@media print{body{background:%(paper)s;color:%(paper_ink)s}.card,.stat{background:%(paper)s;border-color:%(paper_line)s}}
""" % _C

APP_JS = """
document.addEventListener('click', function (e) {
  var b = e.target.closest('[data-fill]');
  if (!b) return;
  var t = document.getElementById(b.getAttribute('data-fill'));
  var src = document.getElementById(b.getAttribute('data-from'));
  if (t && src) { t.value = src.textContent; t.focus(); }
});
"""
