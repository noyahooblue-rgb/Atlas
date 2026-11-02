"""生成单文件审查页 HTML。

不引用任何外部资源：样式与脚本全部内联。审查状态（已审勾选
与意见）存在浏览器 localStorage，读写包在 try/catch 里；
导出按钮把已审项与意见拼成 Markdown 并下载。
"""

import html as html_mod
import re
from pathlib import Path

from . import markdown
from .index import format_check


def anchor(number):
    return "def-" + number.replace(".", "-")


def _link_ids(text, definitions, config):
    """把转义后文本中的编号换成链接 / 未定义标记。"""
    def repl(match):
        number = match.group(0)
        definition = definitions.get(number)
        if definition is None:
            return '<span class="spec-undefined" title="未找到定义">%s</span>' % number
        tip = "定义：%s:%d\n%s" % (
            definition.path, definition.lineno,
            html_mod.escape(definition.text, quote=True),
        )
        label = number
        if definition.short_name:
            label = "%s（%s）" % (number, definition.short_name)
        return '<a class="spec-id" href="#%s" title="%s">%s</a>' % (
            anchor(number), tip, label)
    # 先按编号长度降序，避免短编号抢先匹配长编号的前缀。
    numbers = sorted(definitions.keys(), key=len, reverse=True)
    if not numbers:
        return text
    pattern = re.compile("|".join(re.escape(n) for n in numbers))
    # 未定义编号也要标记：用配置里的全部类别正则再扫一遍。
    text = pattern.sub(repl, text)
    for cat in config.categories:
        def repl_undef(match):
            number = match.group(0)
            if number in definitions:
                return match.group(0)  # 已在上一步处理，不会走到这里
            return '<span class="spec-undefined" title="未找到定义">%s</span>' % number
        # 只替换尚未被 <a>/<span> 包裹的裸编号：简单起见，跳过已含标签的段。
        # 由于上一步已替换全部已定义编号，这里对剩余裸编号做一次扫描，
        # 但要避开标签属性区：按 <...> 切分处理。
        segments = re.split(r"(<[^>]*>)", text)
        for i, seg in enumerate(segments):
            if not seg.startswith("<"):
                segments[i] = cat.regex.sub(repl_undef, seg)
        text = "".join(segments)
    return text


def generate(directory, config, check_result):
    definitions = check_result["definitions"]
    files = sorted(Path(directory).rglob("*.md"))
    sections = []
    for path in files:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
        body = markdown.render(
            lines, lambda t: _link_ids(t, definitions, config))
        defs_here = [d for d in definitions.values() if d.path == path.name]
        defs_here.sort(key=lambda d: d.lineno)
        checklist = []
        for d in defs_here:
            key = "%s:%s" % (d.path, d.number)
            checklist.append(
                '<div class="review-item" id="%s">'
                '<label><input type="checkbox" data-review="%s"> 已审 %s</label>'
                '<span class="def-text">%s（%s:%d）</span>'
                '<textarea data-comment="%s" placeholder="审查意见"></textarea></div>'
                % (anchor(d.number), html_mod.escape(key, quote=True),
                   html_mod.escape(d.number),
                   html_mod.escape(d.text), d.path, d.lineno,
                   html_mod.escape(key, quote=True)))
        sections.append(
            "<section><h1>%s</h1>%s<h2>审查清单</h2>%s</section>"
            % (html_mod.escape(path.name), body, "\n".join(checklist)))
    summary = html_mod.escape(format_check(check_result, config)).replace("\n", "<br>")
    script = """
<script>
(function(){
  var KEY='specpeek-review-v1';
  var state={};
  try{ state=JSON.parse(localStorage.getItem(KEY)||'{}')||{}; }catch(e){ state={}; }
  function save(){ try{ localStorage.setItem(KEY, JSON.stringify(state)); }catch(e){} }
  document.querySelectorAll('[data-review]').forEach(function(box){
    var k=box.getAttribute('data-review');
    if(state[k]&&state[k].checked){ box.checked=true; }
    box.addEventListener('change',function(){ state[k]=state[k]||{}; state[k].checked=box.checked; save(); });
  });
  document.querySelectorAll('[data-comment]').forEach(function(area){
    var k=area.getAttribute('data-comment');
    if(state[k]&&state[k].comment){ area.value=state[k].comment; }
    area.addEventListener('input',function(){ state[k]=state[k]||{}; state[k].comment=area.value; save(); });
  });
  document.getElementById('export-btn').addEventListener('click',function(){
    var out=['# 审查意见',''];
    document.querySelectorAll('.review-item').forEach(function(item){
      var box=item.querySelector('[data-review]');
      var area=item.querySelector('[data-comment]');
      var label=item.querySelector('label').textContent.trim();
      var def=item.querySelector('.def-text').textContent.trim();
      out.push('- ['+(box.checked?'x':' ')+'] '+label+' '+def);
      if(area.value.trim()){ out.push('  意见：'+area.value.trim()); }
    });
    var blob=new Blob([out.join('\\n')],{type:'text/markdown'});
    var a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='review-comments.md'; a.click();
  });
})();
</script>
"""
    page = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>规格审查页</title>
<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 1em;line-height:1.7}
table{border-collapse:collapse}th,td{border:1px solid #ccc;padding:4px 8px}
.spec-id{color:#0b5fff}.spec-undefined{color:#c00;border-bottom:1px dashed #c00}
.review-item{border:1px solid #ddd;padding:8px;margin:8px 0}.review-item textarea{width:100%;min-height:3em}
.def-text{display:block;color:#555;font-size:.9em}
</style></head><body>
<h1>规格审查页</h1><section><h2>追溯检查异常汇总</h2><p>__SUMMARY__</p>
<button id="export-btn" type="button">导出审查意见为 Markdown</button></section>
__SECTIONS__
__SCRIPT__
</body></html>
"""
    # 用占位替换而不用 % 格式化，避免 CSS/JS 里的 % 与 {} 冲突。
    return (page.replace("__SUMMARY__", summary)
                .replace("__SECTIONS__", "\n".join(sections))
                .replace("__SCRIPT__", script))
