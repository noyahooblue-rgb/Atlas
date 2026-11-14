"""snapshot / verify：文件指纹清单与复核。

清单为 JSON，记录每个文件的相对路径、所在基目录、大小、多种 sha256
（原始 / 归一行尾 / 去 BOM / 去末尾空白 / 全归一化）、逐行 sha256、
行尾类型统计、是否有 BOM。verify 用这些指纹把「内容变了」细分为：
只差行尾、只差 BOM、只差文件末尾空行或空白、以上组合、其他实质
变化，并用逐行指纹给出首个差异所在的行号（只报行号，不输出内容）。

被检查的文件只以二进制方式读取，工具自身绝不写入它们。
"""

import glob
import hashlib
import json
import os
import re
from datetime import datetime

from .errors import PrecheckError
from .util import has_glob_chars, match_any, to_posix

BOM = b"\xef\xbb\xbf"
MANIFEST_KIND = "precheck-snapshot"
MANIFEST_VERSION = 1


# ---------------------------------------------------------------------------
# 字节变换与指纹
# ---------------------------------------------------------------------------

def normalize_eol(data):
    """把 CRLF / CR 统一成 LF。"""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def strip_bom(data):
    if data.startswith(BOM):
        return data[len(BOM):]
    return data


def strip_trailing_ws(data):
    """去掉文件末尾的空白（含末尾空行）。"""
    return data.rstrip(b" \t\r\n\x0b\x0c")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def eol_stats(data):
    crlf = data.count(b"\r\n")
    rest = data.replace(b"\r\n", b"")
    return {"crlf": crlf, "lf": rest.count(b"\n"), "cr": rest.count(b"\r")}


def line_hashes(data):
    """逐行指纹：先归一行尾再按 LF 切分，逐行 sha256。

    行尾差异因此不会污染行号定位；BOM 属于第一行的一部分。
    """
    return [_sha256(line) for line in normalize_eol(data).split(b"\n")]


def fingerprint_bytes(data):
    """对一段字节算出全套指纹，snapshot 与 verify 共用。"""
    eol = normalize_eol(data)
    nobom = strip_bom(data)
    lines = line_hashes(data)
    return {
        "size": len(data),
        "sha256": _sha256(data),
        "sha256_eol": _sha256(eol),
        "sha256_nobom": _sha256(nobom),
        "sha256_trail": _sha256(strip_trailing_ws(data)),
        "sha256_eol_nobom": _sha256(normalize_eol(nobom)),
        "sha256_norm": _sha256(strip_trailing_ws(normalize_eol(nobom))),
        "line_endings": eol_stats(data),
        "bom": data.startswith(BOM),
        "line_count": len(lines),
        "line_hashes": lines,
    }


def read_file_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise PrecheckError("无法读取文件 %s：%s" % (path, exc))


def build_entry(abs_path, base_abs):
    data = read_file_bytes(abs_path)
    entry = {"path": to_posix(os.path.relpath(abs_path, base_abs)),
             "base": base_abs}
    entry.update(fingerprint_bytes(data))
    return entry


# ---------------------------------------------------------------------------
# 文件收集（文件 / 目录 / 通配，支持排除）
# ---------------------------------------------------------------------------

def _glob_base(spec):
    """通配模式中第一个含通配符的路径段之前的部分，作为相对路径基目录。"""
    parts = re.split(r"[\\/]", spec)
    kept = []
    for part in parts:
        if has_glob_chars(part):
            break
        kept.append(part)
    if not kept:
        return "."
    base = os.sep.join(kept)
    return base if base else os.sep


def _iter_spec_files(spec):
    """展开一个输入参数，返回 (kind, base_abs, [abs_path,...], pattern_abs)。"""
    if has_glob_chars(spec):
        base_abs = os.path.abspath(_glob_base(spec))
        matches = [m for m in glob.glob(spec, recursive=True)
                   if os.path.isfile(m)]
        return "glob", base_abs, sorted(matches), os.path.abspath(spec)
    if os.path.isdir(spec):
        base_abs = os.path.abspath(spec)
        found = []
        for dirpath, dirnames, filenames in os.walk(base_abs):
            dirnames.sort()
            for name in sorted(filenames):
                found.append(os.path.join(dirpath, name))
        return "dir", base_abs, found, None
    if os.path.isfile(spec):
        abs_path = os.path.abspath(spec)
        return "file", os.path.dirname(abs_path), [abs_path], None
    raise PrecheckError("路径不存在或不是文件/目录：%s" % spec)


def collect(paths, excludes):
    """按参数收集文件，返回 (entries, sources)。同路径重复只收一次。"""
    entries = []
    sources = []
    seen = set()
    for spec in paths:
        kind, base_abs, files, pattern_abs = _iter_spec_files(spec)
        sources.append({"kind": kind, "base": base_abs, "pattern": pattern_abs,
                        "spec": spec})
        for abs_path in files:
            abs_path = os.path.abspath(abs_path)
            key = os.path.normcase(abs_path)
            if key in seen:
                continue
            rel = to_posix(os.path.relpath(abs_path, base_abs))
            if match_any(rel, excludes):
                continue
            seen.add(key)
            entries.append(build_entry(abs_path, base_abs))
    entries.sort(key=lambda e: (e["base"], e["path"]))
    return entries, sources


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

def run_snapshot(paths, output, label, excludes, as_json, out):
    entries, sources = collect(paths, excludes)
    if not entries:
        raise PrecheckError("没有匹配到任何文件，未生成清单")
    manifest = {
        "kind": MANIFEST_KIND,
        "version": MANIFEST_VERSION,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "label": label,
        "sources": sources,
        "excludes": list(excludes or []),
        "files": entries,
    }
    out_path = output or "precheck-snapshot.json"
    try:
        # errors="surrogateescape"：文件名是非法 UTF-8 字节（例如 GBK 字节
        # 文件名）时也不崩，清单仍可被本工具以同样方式读回。
        with open(out_path, "w", encoding="utf-8",
                  errors="surrogateescape", newline="\n") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError as exc:
        raise PrecheckError("无法写入清单 %s：%s" % (out_path, exc))
    total_bytes = sum(e["size"] for e in entries)
    if as_json:
        out(json.dumps({"manifest": out_path, "files": len(entries),
                        "total_bytes": total_bytes},
                       ensure_ascii=False))
    else:
        out("已写入清单：%s" % out_path)
        out("文件数：%d，总大小：%d 字节" % (len(entries), total_bytes))
        if label:
            out("说明：%s" % label)
    return 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

DIFF_KINDS = {
    "eol": "只差行尾（CRLF/LF）",
    "bom": "只差 BOM",
    "trailing": "只差文件末尾空行或空白",
    "eol_bom": "仅行尾与 BOM 差异（无实质内容变化）",
    "cosmetic": "仅末尾空白与行尾/BOM 的组合差异（无实质内容变化）",
    "content": "实质内容变化",
}


def classify_change(old_entry, cur):
    """按归一化指纹逐级比对，返回差异类别代码。"""
    if cur["sha256_eol"] == old_entry["sha256_eol"]:
        return "eol"
    if cur["sha256_nobom"] == old_entry["sha256_nobom"]:
        return "bom"
    if cur["sha256_trail"] == old_entry["sha256_trail"]:
        return "trailing"
    if cur["sha256_eol_nobom"] == old_entry["sha256_eol_nobom"]:
        return "eol_bom"
    if cur["sha256_norm"] == old_entry["sha256_norm"]:
        return "cosmetic"
    return "content"


def first_diff_line(old_hashes, new_hashes):
    """逐行指纹比对，返回首个不同行的 1 起行号；完全一致返回 None。"""
    common = min(len(old_hashes), len(new_hashes))
    for idx in range(common):
        if old_hashes[idx] != new_hashes[idx]:
            return idx + 1
    if len(old_hashes) != len(new_hashes):
        return common + 1
    return None


def _resolve(entry, base_override):
    base = base_override or entry["base"]
    return os.path.join(base, *entry["path"].split("/"))


def _find_extras(manifest, base_override, manifest_keys):
    """按清单记录的来源重新收集，找出未在清单中的文件（多出）。

    指定 --base 时，只对目录型来源在新基目录下做多出检查；
    文件/通配型来源不做多出断言（见 README 已知限制）。
    """
    sources = manifest.get("sources") or []
    excludes = manifest.get("excludes") or []
    extras = set()
    if base_override:
        if any(s.get("kind") == "dir" for s in sources):
            try:
                entries, _ = collect([base_override], excludes)
            except PrecheckError:
                entries = []
            for entry in entries:
                key = os.path.normcase(os.path.abspath(
                    _resolve(entry, base_override)))
                if key not in manifest_keys:
                    extras.add(entry["path"])
        return sorted(extras)
    specs = []
    for src in sources:
        if src.get("kind") == "dir":
            specs.append(src["base"])
        elif src.get("kind") == "glob" and src.get("pattern"):
            specs.append(src["pattern"])
    if not specs:
        return []
    try:
        entries, _ = collect(specs, excludes)
    except PrecheckError:
        return []
    for entry in entries:
        key = os.path.normcase(os.path.abspath(_resolve(entry, None)))
        if key not in manifest_keys:
            extras.add(entry["path"])
    return sorted(extras)


def run_verify(manifest_path, base_override, as_json, out):
    try:
        with open(manifest_path, "r", encoding="utf-8",
                  errors="surrogateescape") as fh:
            manifest = json.load(fh)
    except OSError as exc:
        raise PrecheckError("无法读取清单 %s：%s" % (manifest_path, exc))
    except ValueError as exc:
        raise PrecheckError("清单 %s 不是合法 JSON：%s" % (manifest_path, exc))
    if not isinstance(manifest, dict) or "files" not in manifest:
        raise PrecheckError("清单 %s 格式不对（缺少 files）" % manifest_path)

    results = []
    manifest_keys = set()
    for entry in manifest["files"]:
        fpath = _resolve(entry, base_override)
        manifest_keys.add(os.path.normcase(os.path.abspath(fpath)))
        if not os.path.isfile(fpath):
            results.append({"path": entry["path"], "status": "missing",
                            "detail": "文件缺失"})
            continue
        data = read_file_bytes(fpath)
        cur = fingerprint_bytes(data)
        if cur["sha256"] == entry["sha256"]:
            results.append({"path": entry["path"], "status": "ok",
                            "detail": "一致"})
            continue
        kind = classify_change(entry, cur)
        item = {"path": entry["path"], "status": "changed",
                "detail": DIFF_KINDS[kind], "diff_kind": kind}
        if kind == "content":
            line = first_diff_line(entry.get("line_hashes") or [],
                                   cur["line_hashes"])
            item["first_diff_line"] = line
        results.append(item)

    for extra in _find_extras(manifest, base_override, manifest_keys):
        results.append({"path": extra, "status": "extra",
                        "detail": "清单中没有此文件（多出）"})

    counts = {"ok": 0, "changed": 0, "missing": 0, "extra": 0}
    for item in results:
        counts[item["status"]] += 1
    all_ok = counts["changed"] == 0 and counts["missing"] == 0 \
        and counts["extra"] == 0

    if as_json:
        out(json.dumps({"manifest": manifest_path, "ok": all_ok,
                        "summary": counts, "results": results},
                       ensure_ascii=False))
    else:
        for item in results:
            if item["status"] == "ok":
                out("一致    %s" % item["path"])
            elif item["status"] == "changed":
                text = "变了    %s（%s）" % (item["path"], item["detail"])
                if item.get("first_diff_line"):
                    text += "，首个差异在第 %d 行" % item["first_diff_line"]
                out(text)
            elif item["status"] == "missing":
                out("缺失    %s" % item["path"])
            else:
                out("多出    %s" % item["path"])
        out("汇总：一致 %d，变了 %d，缺失 %d，多出 %d" % (
            counts["ok"], counts["changed"], counts["missing"],
            counts["extra"]))
    return 0 if all_ok else 1
