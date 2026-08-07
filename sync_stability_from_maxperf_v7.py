#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import argparse, copy, html, re, sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

ULTIMATE_MARKERS = (
    "kg.apc.jmeter.threads.UltimateThreadGroup",
    "UltimateThreadGroup",
    "Ultimate Thread Group",
)

IGNORED_TYPES = {"TestAction", "ConstantThroughputTimer"}
IGNORED_NAMES = {"Flow Control Action", "Constant Throughput Timer"}

@dataclass
class GroupRef:
    key: str
    name: str
    enabled: bool
    element: ET.Element
    subtree: ET.Element
    parent: ET.Element
    index: int

@dataclass
class Change:
    uc: str
    group: str
    kind: str
    path: str
    detail: str = ""

def tag(el): return el.tag.rsplit("}", 1)[-1]
def is_hash(el): return tag(el) == "hashTree"
def enabled(el): return el.attrib.get("enabled", "true").lower() != "false"
def etype(el): return el.attrib.get("testclass") or tag(el)
def ename(el): return el.attrib.get("testname", "").strip() or "(без имени)"

def is_ultimate(el):
    s = " ".join((tag(el), el.attrib.get("testclass",""), el.attrib.get("guiclass",""))).lower()
    return any(x.lower() in s for x in ULTIMATE_MARKERS)

def ignored(el):
    return etype(el) in IGNORED_TYPES or ename(el) in IGNORED_NAMES

def uc_key(name):
    nums = re.findall(r"(?i)UC[\s_-]*(\d+)", name)
    if nums:
        out=[]; seen=set()
        for n in nums:
            k=f"UC{int(n)}"
            if k not in seen:
                seen.add(k); out.append(k)
        return "+".join(out)
    return re.sub(r"\s+"," ",name.casefold()).strip() or "(без имени)"

def norm_key(v):
    m = re.fullmatch(r"(?i)(?:UC)?[\s_-]*(\d+)", v.strip())
    return f"UC{int(m.group(1))}" if m else uc_key(v)

def parse(path):
    try:
        return ET.parse(path)
    except Exception as e:
        raise RuntimeError(f"Не удалось прочитать {path}: {e}") from e

def groups(root):
    out=[]; idx=0
    for parent in root.iter():
        children=list(parent)
        for i,ch in enumerate(children):
            if not is_ultimate(ch): continue
            sub = children[i+1] if i+1 < len(children) and is_hash(children[i+1]) else ET.Element("hashTree")
            out.append(GroupRef(uc_key(ename(ch)), ename(ch), enabled(ch), ch, sub, parent, idx))
            idx += 1
    return out

def pairs(tree):
    children=list(tree); i=0
    while i < len(children):
        el=children[i]
        if is_hash(el):
            i += 1; continue
        sub = children[i+1] if i+1 < len(children) and is_hash(children[i+1]) else None
        i += 2 if sub is not None else 1
        yield el, sub

def ident(el): return f'{etype(el)} "{ename(el)}"'

def signature(tree):
    out=set()
    def walk(t,parent=""):
        cnt=Counter()
        for el,sub in pairs(t):
            if not enabled(el) or ignored(el): continue
            base=ident(el); cnt[base]+=1
            seg=base if cnt[base]==1 else f"{base} [#{cnt[base]}]"
            p=f"{parent} / {seg}" if parent else seg
            out.add(p)
            if sub is not None: walk(sub,p)
    walk(tree)
    return out

def similarity(a,b):
    sa,sb=signature(a.subtree),signature(b.subtree)
    if not sa and not sb: return 1.0
    if not sa or not sb: return 0.0
    return len(sa & sb)/len(sa | sb)

def choose(maxg,cands,used):
    av=[g for g in cands if g.index not in used]
    if not av: return None
    av.sort(key=lambda g:(-similarity(maxg,g),abs(maxg.index-g.index)))
    used.add(av[0].index)
    return av[0]

def find_template(st_groups,key):
    m=[g for g in st_groups if g.key==key]
    if not m: raise RuntimeError(f"Шаблон {key} не найден в Stability")
    m.sort(key=lambda g:(not g.enabled,g.index))
    return m[0]

def clone_header(template,maxg):
    el=copy.deepcopy(template.element)
    el.attrib["testname"]=maxg.name
    el.attrib["enabled"]="true"
    return el

def active_tree(src):
    dst=ET.Element(src.tag,src.attrib)
    for el,sub in pairs(src):
        if not enabled(el) or ignored(el): continue
        dst.append(copy.deepcopy(el))
        dst.append(active_tree(sub) if sub is not None else ET.Element("hashTree"))
    return dst

def xml_eq(a,b):
    ac,bc=copy.deepcopy(a),copy.deepcopy(b)
    def clean(x):
        x.tail=None
        for c in x: clean(c)
    clean(ac); clean(bc)
    return ET.tostring(ac,encoding="unicode")==ET.tostring(bc,encoding="unicode")

def sync_tree(max_tree, st_tree, uc, group_name, changed, manual, parent=""):
    maxp=list(pairs(max_tree)); stp=list(pairs(st_tree))
    buckets=defaultdict(list)
    for e,s in stp:
        if not ignored(e): buckets[ident(e)].append((e,s))

    used=set(); cnt=Counter()
    for me,ms in maxp:
        if not enabled(me) or ignored(me): continue
        base=ident(me); cnt[base]+=1
        disp=base if cnt[base]==1 else f"{base} [#{cnt[base]}]"
        path=f"{parent} / {disp}" if parent else disp

        cand=next((x for x in buckets.get(base,[]) if id(x[0]) not in used),None)
        if cand is None:
            st_tree.append(copy.deepcopy(me))
            st_tree.append(active_tree(ms) if ms is not None else ET.Element("hashTree"))
            changed.append(Change(uc,group_name,"added",path,"Добавлен из MaxPerf"))
            continue

        se,ss=cand; used.add(id(se))

        if not enabled(se):
            i=list(st_tree).index(se)
            st_tree.remove(se); se=copy.deepcopy(me); st_tree.insert(i,se)
            changed.append(Change(uc,group_name,"enabled",path,"В Stability был отключён; взята активная версия MaxPerf"))

        if not xml_eq(se,me):
            i=list(st_tree).index(se)
            st_tree.remove(se); se=copy.deepcopy(me); st_tree.insert(i,se)
            changed.append(Change(uc,group_name,"updated",path,"Настройки, path/body/headers/script обновлены по MaxPerf"))

        if ms is not None:
            if ss is None:
                i=list(st_tree).index(se); ss=ET.Element("hashTree"); st_tree.insert(i+1,ss)
            sync_tree(ms,ss,uc,group_name,changed,manual,path)

    max_counts=Counter(ident(e) for e,_ in maxp if enabled(e) and not ignored(e))
    seen=Counter()
    for se,_ in stp:
        if ignored(se) or not enabled(se): continue
        b=ident(se); seen[b]+=1
        if seen[b] > max_counts.get(b,0):
            p=f"{parent} / {b}" if parent else b
            manual.append(Change(uc,group_name,"extra",p,"Лишний активный элемент Stability не удалён автоматически"))

def remaining(max_tree,st_tree,uc,group_name,out,parent=""):
    maxp=[x for x in pairs(max_tree) if enabled(x[0]) and not ignored(x[0])]
    stp=[x for x in pairs(st_tree) if not ignored(x[0])]
    buckets=defaultdict(list)
    for e,s in stp: buckets[ident(e)].append((e,s))
    used=set(); cnt=Counter()

    for me,ms in maxp:
        b=ident(me); cnt[b]+=1
        disp=b if cnt[b]==1 else f"{b} [#{cnt[b]}]"
        p=f"{parent} / {disp}" if parent else disp
        cand=next((x for x in buckets.get(b,[]) if id(x[0]) not in used),None)
        if cand is None:
            out.append(Change(uc,group_name,"remaining",p,"Элемент не найден после синхронизации")); continue
        se,ss=cand; used.add(id(se))
        if not enabled(se):
            out.append(Change(uc,group_name,"remaining",p,"Элемент остаётся отключён"))
        elif not xml_eq(se,me):
            out.append(Change(uc,group_name,"remaining",p,"Настройки всё ещё отличаются"))
        if ms is not None:
            if ss is None: out.append(Change(uc,group_name,"remaining",p,"Нет дочернего hashTree"))
            else: remaining(ms,ss,uc,group_name,out,p)

def report_html(maxperf,stability,outjmx,changes,manual,rem,added,report):
    def esc(x): return html.escape(str(x),quote=True)
    def render(items,css,empty):
        if not items: return f'<div class="empty">{esc(empty)}</div>'
        g=defaultdict(list)
        for x in items: g[(x.uc,x.group)].append(x)
        cards=[]
        for (uc,name),vals in sorted(g.items()):
            rows="".join(
                f'<div class="row"><span class="tag">{esc(v.kind)}</span><code>{esc(v.path)}</code><div class="detail">{esc(v.detail)}</div></div>'
                for v in vals
            )
            cards.append(f'<article class="card {css}"><div class="uc">{esc(uc)}</div><h3>{esc(name)}</h3>{rows}</article>')
        return "".join(cards)

    auto=added+changes
    doc=f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Sync report</title>
<style>
:root{{--bg:#f5f6f8;--p:#fff;--t:#27323a;--m:#6d7880;--l:#dde3e7;--b:#536f80;--gb:#eef5f0;--g:#4f6f5d;--ab:#faf4e8;--a:#82602f;--rb:#f9eeee;--r:#885050}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);font-family:Segoe UI,Arial;color:var(--t)}}.w{{width:min(1180px,calc(100% - 32px));margin:28px auto}}.hero,.sec,.sum{{background:var(--p);border:1px solid var(--l);border-radius:14px}}.hero,.sec{{padding:22px;margin-bottom:16px}}h1{{margin:0 0 12px}}.meta,.note,.detail{{color:var(--m);font-size:13px}}.sum{{display:grid;grid-template-columns:repeat(3,1fr);margin-bottom:16px;overflow:hidden}}.metric{{padding:18px;border-right:1px solid var(--l)}}.metric:last-child{{border:0}}.num{{font-size:25px;font-weight:700;color:var(--b)}}.card{{border:1px solid var(--l);border-left:4px solid var(--b);border-radius:10px;padding:15px;margin:10px 0}}.card.auto{{border-left-color:#6b8b78}}.card.manual{{border-left-color:#b18a58}}.card.remaining{{border-left-color:#ad7070}}.uc{{font-size:12px;font-weight:700;color:var(--b)}}h3{{margin:3px 0 10px}}.row{{background:#f8f9fa;border-radius:7px;padding:9px;margin:6px 0}}.tag{{font-size:11px;font-weight:650;padding:3px 7px;border-radius:999px;background:#eef3f6;color:var(--b);margin-right:8px}}code{{font:12px Consolas,monospace;overflow-wrap:anywhere}}.detail{{margin-top:5px}}.empty{{background:var(--gb);color:var(--g);padding:13px;border-radius:8px}}.manual .tag{{background:var(--ab);color:var(--a)}}.remaining .tag{{background:var(--rb);color:var(--r)}}
</style></head><body><main class="w">
<section class="hero"><h1>Синхронизация MaxPerf → Stability</h1><div class="meta"><b>MaxPerf:</b> {esc(maxperf)}<br><b>Stability:</b> {esc(stability)}<br><b>Новый JMX:</b> {esc(outjmx)}</div></section>
<section class="sum"><div class="metric"><div class="num">{len(auto)}</div><div>Автоматически изменено</div></div><div class="metric"><div class="num">{len(manual)}</div><div>Ручная проверка</div></div><div class="metric"><div class="num">{len(rem)}</div><div>Осталось отличий</div></div></section>
<section class="sec"><h2>Что уже изменено</h2><div class="note">Имя, enabled/disabled и профиль существующей Ultimate Thread Group не менялись.</div>{render(auto,"auto","Автоматических изменений нет.")}</section>
<section class="sec"><h2>Что проверить вручную</h2>{render(manual,"manual","Ручная проверка не требуется.")}</section>
<section class="sec"><h2>Что ещё отличается после синхронизации</h2>{render(rem,"remaining","По синхронизируемой активной части различий не осталось.")}</section>
</main></body></html>"""
    report.write_text(doc,encoding="utf-8")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("maxperf",type=Path); ap.add_argument("stability",type=Path)
    ap.add_argument("--template",required=True)
    ap.add_argument("--only",action="append",default=[])
    ap.add_argument("-o","--output",type=Path,default=Path("Stability_updated.jmx"))
    ap.add_argument("--report",type=Path,default=Path("sync_report.html"))
    a=ap.parse_args()

    if not a.maxperf.is_file() or not a.stability.is_file():
        print("Ошибка: проверь пути к JMX",file=sys.stderr); return 2

    try:
        mt,st=parse(a.maxperf),parse(a.stability)
        mg,sg=groups(mt.getroot()),groups(st.getroot())
        template=find_template(sg,norm_key(a.template))
        by=defaultdict(list)
        for g in sg: by[g.key].append(g)
        only={norm_key(x) for x in a.only}
        targets=[g for g in mg if g.enabled and (not only or g.key in only)]

        used=set(); changes=[]; manual=[]; rem=[]; added=[]
        for m in targets:
            s=choose(m,by.get(m.key,[]),used)
            if s is None:
                nh=clone_header(template,m); nt=active_tree(m.subtree)
                template.parent.append(nh); template.parent.append(nt)
                s=GroupRef(m.key,m.name,True,nh,nt,template.parent,10**9+len(added))
                added.append(Change(m.key,m.name,"group_added",m.name,"Добавлена с Stability-профилем из шаблона"))
            else:
                sync_tree(m.subtree,s.subtree,m.key,s.name,changes,manual)
            remaining(m.subtree,s.subtree,m.key,s.name,rem)

        st.write(a.output,encoding="UTF-8",xml_declaration=True)
        report_html(a.maxperf,a.stability,a.output,changes,manual,rem,added,a.report)

        print("Синхронизация завершена")
        print("Новый JMX:",a.output.resolve())
        print("HTML:",a.report.resolve())
        print("Автоизменений:",len(changes)+len(added))
        print("Ручная проверка:",len(manual))
        print("Осталось отличий:",len(rem))
        print("Исходный Stability не изменён.")
        return 0 if not rem else 1
    except RuntimeError as e:
        print("Ошибка:",e,file=sys.stderr); return 2

if __name__=="__main__":
    sys.exit(main())
