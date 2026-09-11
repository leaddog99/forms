#!/bin/bash
cd "${1:-.}"   # working dir for matches.nq/progress.txt (default: cwd)
BASE="https://data.dws.informatik.uni-mannheim.de/structureddata/2024-12/quads/classspecific/Recipe"
PAT='banana[ -]?bread|banana[ -]?loaf|banana[ -]?nut[ -]?bread|bananenbrot|pan de pl[aá]tano|pan de banan[ao]|pain aux? bananes?|cake à la banane|バナナブレッド|banana bread'
: > matches.nq; : > progress.txt
for i in $(seq 0 20); do
  f="part_$i.gz"; s=$(date +%s)
  n=$(curl -s --retry 3 --max-time 1800 "$BASE/$f" | gzip -dc 2>/dev/null | grep -i -E "$PAT" | tee -a matches.nq | wc -l)
  echo "$f matched=$n secs=$(( $(date +%s) - s ))" >> progress.txt
done
echo "DONE total_match_lines=$(wc -l < matches.nq)" >> progress.txt
