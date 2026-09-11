import re, sys, collections
from urllib.parse import urlsplit
NAME_PREDS = ("<http://schema.org/name>", "<https://schema.org/name>", "<http://schema.org/headline>", "<https://schema.org/headline>")
TERMS = [("banana bread (en)", re.compile(r"banana[ -]?(nut[ -]?)?bread|banana[ -]?loaf", re.I)),
         ("Bananenbrot (de)", re.compile(r"bananenbrot", re.I)),
         ("pan de plátano/banana (es)", re.compile(r"pan de pl[aá]tano|pan de banan[ao]", re.I)),
         ("pain aux bananes / cake à la banane (fr)", re.compile(r"pain aux? bananes?|cake à la banane", re.I)),
         ("バナナブレッド (ja)", re.compile(r"バナナブレッド"))]
MULTI = {"co.uk","com.au","co.nz","com.br","co.za","com.mx","co.jp","org.uk","com.ar","co.in","com.tr"}
def pld(host):
    p = host.lower().split(":")[0].split(".")
    if len(p) >= 3 and ".".join(p[-2:]) in MULTI: return ".".join(p[-3:])
    return ".".join(p[-2:]) if len(p) >= 2 else host
pages = collections.defaultdict(set); hosts = collections.defaultdict(set); plds = collections.defaultdict(set)
all_pages=set(); all_hosts=set(); all_plds=set(); per_pld=collections.Counter(); n_lines=0; n_name=0
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    n_lines += 1
    parts = line.rstrip(" .\n").split(" ", 2)
    if len(parts) < 3 or parts[1] not in NAME_PREDS: continue
    rest = parts[2]
    m = re.search(r'\s<([^>]+)>\s*$', rest)
    if not m: continue
    url = m.group(1); obj = rest[:m.start()]
    n_name += 1
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    for label, rx in TERMS:
        if rx.search(obj):
            pages[label].add(url); hosts[label].add(host); plds[label].add(pld(host))
            all_pages.add(url); all_hosts.add(host); all_plds.add(pld(host)); per_pld[pld(host)] += 1
            break
print(f"match lines scanned: {n_lines:,}  (name/headline lines: {n_name:,})")
print(f"\nALL TERMS: recipe pages {len(all_pages):,} | hosts {len(all_hosts):,} | pay-level domains {len(all_plds):,}")
for label,_ in TERMS:
    print(f"  {label:42} pages {len(pages[label]):>7,} | hosts {len(hosts[label]):>6,} | PLDs {len(plds[label]):>6,}")
one = sum(1 for d,c in per_pld.items() if c == 1)
print(f"\nPLDs with exactly ONE banana-bread recipe page: {one:,} of {len(all_plds):,} ({100*one/max(1,len(all_plds)):.0f}%)")
print("Top publishers by banana-bread pages:")
for d,c in per_pld.most_common(15): print(f"  {d:35} {c:>5}")
