"""Read-only inventory. No app database, credentials or skill body is exported."""
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import platform
import re
import stat
import time
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def entry_type(path):
    path = pathlib.Path(path)
    try:
        s = path.lstat()
    except FileNotFoundError:
        return {"type": "missing"}
    d = {"type": "directory" if stat.S_ISDIR(s.st_mode) else "file"}
    if getattr(s, "st_file_attributes", 0) & 0x400:
        tag = getattr(s, "st_reparse_tag", 0)
        d.update(type="junction" if tag == 0xA0000003 else "symlink" if tag == 0xA000000C else "reparse",
                 reparse_tag=hex(tag), target=os.readlink(path))
    d["resolved"] = os.path.realpath(path)
    return d


def manifest(path):
    base = pathlib.Path(path)
    out = []
    for directory, dirs, files in os.walk(base, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
        for name in list(dirs):
            p = pathlib.Path(directory) / name
            kind = entry_type(p)
            if kind["type"] != "directory":
                out.append({"path": p.relative_to(base).as_posix(), **kind})
                dirs.remove(name)
            else:
                out.append({"path": p.relative_to(base).as_posix(), "type": "directory"})
        for name in files:
            p = pathlib.Path(directory) / name
            kind = entry_type(p)
            if kind["type"] != "file":
                out.append({"path": p.relative_to(base).as_posix(), **kind})
            else:
                raw = p.read_bytes()
                out.append({"path": p.relative_to(base).as_posix(), "type": "file", "bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest()})
    return sorted(out, key=lambda x: x["path"])


def digest(items):
    return hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def metadata(path):
    try:
        text = (pathlib.Path(path) / "SKILL.md").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return {}
    front = text.split("---", 2)[1] if text.startswith("---") else ""
    def field(key):
        m = re.search(r"^" + key + r":\s*(.+)$", front, re.M)
        return m.group(1).strip().strip("'\"") if m else None
    return {"name": field("name"), "description_chars": len(field("description") or ""),
            "body_chars": len(text), "skill_md_sha256": hashlib.sha256((pathlib.Path(path) / "SKILL.md").read_bytes()).hexdigest()}


def collect():
    start = time.perf_counter()
    if (ROOT/'skillshare.yaml').is_file():
        current=yaml.safe_load((ROOT/'skillshare.yaml').read_text(encoding='utf-8-sig'))
        managed={n for t in current['targets'].values() for n in t.get('skills',t).get('include',[])}
        roots={'hub':current['source'],**{k:t.get('skills',t)['path'] for k,t in current['targets'].items()},**current.get('x-observed-unmanaged-roots',{})}
        preserved=set(current.get('x-preserved-sources',{}))
    else:
        cfg=json.loads((ROOT/'skills-map.json').read_text(encoding='utf-8-sig'))
        managed={s['name'] for s in cfg['skills']};roots={'hub':cfg['hub'],**cfg['platform_roots']};preserved=set()
    result = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(), "os": platform.platform(),
              "python": platform.python_version(), "username": os.environ.get("USERNAME"),
              "expanduser": os.path.expanduser("~"), "userprofile": os.environ.get("USERPROFILE"),
              "git_exists": (ROOT / ".git").exists(), "configured_skills": len(managed), "roots": {}}
    for label, root in roots.items():
        rd = {"path": root, **entry_type(root), "entries": []}
        if os.path.isdir(root):
            for p in sorted(pathlib.Path(root).iterdir(), key=lambda p: p.name.lower()):
                e = {"name": p.name, **entry_type(p)}
                e["metadata"] = metadata(p)
                if p.name in managed:
                    e["ownership"] = "existing-user-managed"
                elif p.name in preserved:
                    e['ownership']='preserved-external-owner'
                elif p.name == ".system" or p.name.startswith(("chatcut-", "lark-", "figma-", "vercel-")):
                    e["ownership"] = "protected-system-or-installer"
                else:
                    e["ownership"] = "unknown-preserve"
                if e["metadata"] and (p.name in managed or label=='hub'):
                    try:
                        e["manifest"] = manifest(p)
                        e["tree_sha256"] = digest(e["manifest"])
                    except OSError as ex:
                        e["error"] = str(ex)
                rd["entries"].append(e)
        result["roots"][label] = rd
    result["root_legacy_skills"] = [{"path": str(p), **entry_type(p), "metadata": metadata(p), "manifest": manifest(p)}
                                      for p in ROOT.iterdir() if p.is_dir() and (p / "SKILL.md").is_file()]
    result["elapsed_seconds"] = round(time.perf_counter() - start, 6)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = collect()
    pathlib.Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": args.out, "configured": result["configured_skills"], "git": result["git_exists"],
        "elapsed_seconds": result["elapsed_seconds"], "roots": {k: {"total": len(v["entries"]),
        "managed": sum(e.get('ownership')=='existing-user-managed' for e in v['entries']),
        "types": {t: sum(e["type"] == t for e in v["entries"]) for t in sorted({e["type"] for e in v["entries"]})}} for k, v in result["roots"].items()}}, ensure_ascii=False))
