"""LG Sitemap - <country> 패턴의 그룹에 sitemap_url을 자동 시드한다 (1회 실행).

규칙:
  - 그룹명이 "LG Sitemap - <code>" → https://www.lg.com/<code>/sitemap.xml
  - "LG Sitemap - global" 또는 "Global" → https://www.lg.com/sitemap.xml
  - sitemap_url이 이미 있으면 건드리지 않음 (idempotent)

사용:
  python3 seed_sitemap_urls.py             # dry-run (변경 없이 결과만 표시)
  python3 seed_sitemap_urls.py --apply     # 실제 저장
"""
import asyncio
import re
import sys

import audit_store

_PATTERN = re.compile(r"^LG\s+Sitemap\s*-\s*([\w-]+)\s*$", re.IGNORECASE)


def _build_url(country_code: str) -> str:
    code = country_code.lower()
    if code == "global":
        return "https://www.lg.com/sitemap.xml"
    return f"https://www.lg.com/{code}/sitemap.xml"


async def main(apply: bool) -> None:
    data = audit_store.load()
    groups = data.get("groups", [])

    planned = []
    for g in groups:
        if g.get("sitemap_url"):
            continue
        m = _PATTERN.match(g.get("name", "") or "")
        if not m:
            continue
        url = _build_url(m.group(1))
        planned.append((g, url))

    print(f"대상 그룹 {len(planned)}개 (전체 {len(groups)}개 중)")
    for g, url in planned[:10]:
        print(f"  {g.get('name'):30s} → {url}")
    if len(planned) > 10:
        print(f"  ... 외 {len(planned) - 10}개")

    if not apply:
        print("\n[dry-run] --apply 옵션을 붙여서 실제 저장하세요.")
        return

    for g, url in planned:
        g["sitemap_url"] = url
    await audit_store.save(data)
    print(f"\n[저장 완료] {len(planned)}개 그룹에 sitemap_url 추가됨.")


if __name__ == "__main__":
    apply_flag = "--apply" in sys.argv
    asyncio.run(main(apply_flag))
