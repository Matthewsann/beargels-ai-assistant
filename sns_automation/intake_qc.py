"""입고 검수 — 올린 소재가 쓸 만한지 그 자리에서 판정한다.

왜 필요한가(설계 검토 2026-09-04):
    "사람이 찍는 영상·사진이 별로일 수 있다"가 이 파이프라인의 가장 큰 위험이다.
    사장님 해법은 'AI 가 사진을 생성해서 대체'였지만, 제품 실물 생성은 허위표현·
    네이버 원본성·유료 원칙에 걸린다(사장님 확정 2026-09-04: 보정 O, 생성 X).
    → 현실적인 해법은 **다시 찍을 수 있을 때 알려주는 것**이다. 매장에 있을 때
      "이 클립은 흔들려서 못 써요, 단면 컷이 빠졌어요"를 폰으로 보면 3분이면
      다시 찍는다. 하루 지나 편집 단계에서 알면 그날 촬영은 통째로 버린다.

무엇을 보나(전부 집 PC 로컬 · AI 비용 0):
    · 길이   — 1.5초 미만은 릴스 한 샷으로 못 쓴다
    · 흔들림 — 프레임 선명도(엣지 분산). 손떨림·초점 나감을 잡는다
    · 밝기   — 너무 어둡거나 날아간 화면
    · 방향   — 가로로 찍은 영상(9:16 릴스에서 좌우가 잘린다)
    · 소리   — 무음 클립(ASMR 샷이면 치명적)
    · 빠진 샷 — 촬영가이드의 샷 목록 대비 개수

    py -m sns_automation.intake_qc "<주제 폴더>"     한 폴더 검수해 보기

판정은 규칙이다. 임계값은 실제 촬영본으로 잰 값(2026-09-04)에서 왔고,
아래 상수 한 곳에서만 고친다.
"""

from __future__ import annotations

import io
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}

#: 임계값 — 매장 실제 촬영본으로 잰 값(2026-09-04, px=480 프레임 기준).
#:   실촬영 클립 선명도 분포 21.5~43.3(전부 쓸 만함), 밝기 90~161
#:   같은 프레임을 흐리게 만들어 본 값: 살짝(r1)=18.6, 눈에 띄게(r1.5)=13.1,
#:   확실히 못 씀(r2)=9.7 → 아래 두 선은 그 사이에 둔다.
MIN_SECONDS = 1.5           # 이보다 짧으면 한 샷으로 못 쓴다
SHARP_BAD = 10.0            # 이하면 흔들림·초점 나감 (다시 찍어야)
SHARP_WEAK = 16.0           # 이하면 '아슬아슬' (경고만)
DARK = 50.0                 # 평균 밝기(0~255) 이하면 어둡다 (실촬영 최저 90)
BRIGHT = 226.0              # 이상이면 날아갔다
FRAMES = 3                  # 클립당 볼 프레임 수
FRAME_PX = 480              # 선명도 비교의 기준 해상도 — 바꾸면 임계값도 다시


class Verdict:
    """판정 등급 — 화면 문구와 1:1."""
    OK = "ok"
    WARN = "warn"
    BAD = "bad"


# ── 한 장·한 클립 보기(순수 계산) ─────────────────────────────

def _register_heif() -> None:
    """아이폰 HEIC 사진도 열 수 있게 한다(blog_media 와 같은 규칙)."""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:  # noqa: BLE001 — 없으면 HEIC 만 건너뛴다
        pass


def frame_stats(jpeg: bytes) -> dict:
    """프레임 한 장의 선명도·밝기. PIL 만 쓴다(numpy 불필요)."""
    from PIL import Image, ImageFilter, ImageStat
    with Image.open(io.BytesIO(jpeg)) as im:
        landscape = im.width > im.height       # 리사이즈 전 원본 비율로 판단
        g = im.convert("L")
        if max(g.size) != FRAME_PX:
            ratio = FRAME_PX / max(g.size)
            g = g.resize((max(1, int(g.width * ratio)), max(1, int(g.height * ratio))))
        bright = ImageStat.Stat(g).mean[0]
        edges = g.filter(ImageFilter.FIND_EDGES)
        # 가장자리 1px 은 필터 특성상 항상 밝게 나와 잘라낸다
        edges = edges.crop((1, 1, max(2, edges.width - 1), max(2, edges.height - 1)))
        sharp = ImageStat.Stat(edges).stddev[0]
    return {"sharp": round(sharp, 2), "bright": round(bright, 1),
            "landscape": landscape}


def judge(stats: dict) -> tuple[str, str]:
    """수치 → (등급, 사람 말). 규칙 한 곳."""
    sharp, bright = stats.get("sharp", 0), stats.get("bright", 128)
    secs = stats.get("seconds")
    if secs is not None and secs < MIN_SECONDS:
        return Verdict.BAD, f"너무 짧아요({secs:.1f}초) — 한 샷은 2초는 넘어야 해요"
    # 밝기를 먼저 본다 — 어두우면 선명도도 같이 떨어져서(실측: 밝기 30%로
    # 낮추면 선명도 28.7→8.9) '흔들렸어요'라는 엉뚱한 안내가 나간다.
    if bright <= DARK:
        return Verdict.BAD, "너무 어두워요 — 창가로 옮기거나 조명을 켜주세요"
    if bright >= BRIGHT:
        return Verdict.BAD, "화면이 날아갔어요(너무 밝음) — 역광을 피해주세요"
    if sharp <= SHARP_BAD:
        return Verdict.BAD, "흔들렸거나 초점이 안 맞았어요 — 다시 찍는 게 좋아요"
    # 경고는 하나만 골라 말한다(사장님이 조치할 순서대로): 방향 → 소리 → 흐릿함.
    # 흐릿함을 먼저 보면 '조금 흐릿해요'가 가로 촬영 경고를 가려서, 좌우가 잘릴
    # 영상을 그대로 쓰게 된다(2026-09-04 검토).
    if stats.get("landscape"):
        return Verdict.WARN, "가로로 찍혔어요 — 릴스에서 좌우가 잘려요(세로로 다시)"
    if stats.get("silent"):
        return Verdict.WARN, "소리가 없어요 — 굽는·자르는 소리가 핵심이면 다시"
    if sharp <= SHARP_WEAK:
        return Verdict.WARN, "조금 흐릿해요 — 쓸 수는 있지만 한 컷 더 있으면 좋아요"
    return Verdict.OK, ""


def check_video(path: str) -> dict:
    """영상 하나 검수. ffmpeg 로 프레임 3장을 뽑아 본다."""
    from . import video_editor as ve
    name = os.path.basename(path)
    try:
        secs = ve.probe_seconds(path)
    except Exception as e:  # noqa: BLE001 — 못 읽는 파일은 판정 보류
        logger.debug("길이 확인 실패(%s): %s", name, e)
        return {"file": name, "kind": "video", "grade": Verdict.WARN,
                "why": "파일을 열지 못했어요 — 동기화 중일 수 있어요"}
    out = {"file": name, "kind": "video", "seconds": round(secs, 1)}
    if secs and secs < MIN_SECONDS:
        out.update(grade=Verdict.BAD,
                   why=f"너무 짧아요({secs:.1f}초) — 한 샷은 2초는 넘어야 해요")
        return out
    frames = []
    try:
        frames = ve.sample_frames(path, secs, every=max(1.0, secs / FRAMES),
                                  max_frames=FRAMES, px=FRAME_PX)
    except Exception as e:  # noqa: BLE001
        logger.debug("프레임 추출 실패(%s): %s", name, e)
    if not frames:
        out.update(grade=Verdict.WARN, why="화면을 확인하지 못했어요")
        return out
    stats = [frame_stats(j) for _t, j in frames]
    # 가장 선명한 프레임으로 판단한다 — 클립 안에 좋은 2초가 있으면 쓸 수 있다.
    best = max(stats, key=lambda s: s["sharp"])
    best = {**best, "seconds": secs}
    try:
        best["silent"] = not ve._has_audio(ve.ffmpeg_exe(), path)
    except Exception:  # noqa: BLE001
        pass
    grade, why = judge(best)
    out.update(sharp=best["sharp"], bright=best["bright"],
               landscape=best.get("landscape"), silent=best.get("silent"),
               grade=grade, why=why)
    return out


def check_image(path: str) -> dict:
    """사진 하나 검수. 아이폰 HEIC 도 연다.

    사진은 가로여도 괜찮다 — 블로그 대표사진은 가로가 오히려 낫다. 방향 경고는
    릴스로 쓰는 영상에만 붙인다.
    """
    from PIL import Image
    _register_heif()
    name = os.path.basename(path)
    try:
        with Image.open(path) as im:
            im.draft("L", (FRAME_PX, FRAME_PX))       # HEIC·대형 JPEG 빠르게
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=80)
        stats = frame_stats(buf.getvalue())
    except Exception as e:  # noqa: BLE001 — HEIC 지원 없음 등
        logger.debug("사진 확인 실패(%s): %s", name, e)
        return {"file": name, "kind": "image", "grade": Verdict.WARN,
                "why": "사진을 열지 못했어요"}
    stats.pop("landscape", None)          # 사진은 가로여도 정상
    grade, why = judge(stats)
    return {"file": name, "kind": "image", "grade": grade, "why": why, **stats}


# ── 폴더 한 번에 ─────────────────────────────────────────────

#: 촬영가이드의 샷 줄 — auto_make.start_shoot 이 쓰는 '1. 무엇 (3초)' 과
#: 사람이 손으로 적는 불릿(- · * 📹) 둘 다 읽는다. 한쪽만 읽으면 우리가 만든
#: 가이드를 우리가 못 읽는다.
_SHOT_LINE = re.compile(r"^\s*(?:\d{1,2}[.)]|[-·*]|📹)\s*(?P<what>\S.*)$")


def wanted_shots(folder: str) -> list[str]:
    """촬영가이드에 적힌 샷 목록 — 몇 개를 찍어야 했는지."""
    from . import source_watch
    shots = []
    for line in source_watch.guide_text(folder).splitlines():
        m = _SHOT_LINE.match(line)
        if not m:
            continue
        what = m.group("what").strip()
        if len(what) >= 2:
            shots.append(what[:80])
    return shots


#: 한 번에 볼 파일 수 — 영상과 사진에 **따로** 준다. 하나로 묶어 자르면
#: 클립이 많은 폴더에서 사진이 한 장도 안 보이는데(영상이 먼저 온다), 블로그
#: 대표사진이 바로 그 사진이다(2026-09-04 검토에서 발견).
VIDEO_LIMIT = 12
IMAGE_LIMIT = 6


def check_folder(folder: str, *, limit: int | None = None) -> dict:
    """주제 폴더 하나를 검수한다 → 사장님 폰에 보낼 결과.

    반환: {ok, usable, bad[], warn[], missing[], files, checked_at}
    """
    from . import source_watch
    media = source_watch.scan_media(folder)
    v_lim = limit if limit is not None else VIDEO_LIMIT
    i_lim = limit if limit is not None else IMAGE_LIMIT
    rows: list[dict] = []
    for name in (media["videos"][:v_lim] + media["images"][:i_lim]):
        p = os.path.join(folder, name)
        ext = os.path.splitext(name)[1].lower()
        rows.append(check_video(p) if ext in VIDEO_EXT else check_image(p))
    bad = [r for r in rows if r.get("grade") == Verdict.BAD]
    warn = [r for r in rows if r.get("grade") == Verdict.WARN]
    ok = [r for r in rows if r.get("grade") == Verdict.OK]

    want = wanted_shots(folder)
    missing: list[str] = []
    usable = len(ok) + len(warn)
    if want and usable < len(want):
        # ⚠️ **어느 샷이 빠졌는지는 알 수 없다** — 파일에는 이름이 없다.
        #    계획 개수보다 쓸 수 있는 파일이 적다는 것만 안다. 그래서 화면에도
        #    '안 찍은 샷'이라고 단정하지 않고 '더 필요한 컷'으로 말한다.
        missing = want[usable:]
    return {
        "folder": os.path.basename(folder.rstrip("/\\")),
        "checked_at": int(time.time()),
        "files": len(rows), "ok": len(ok), "usable": usable,
        "bad": [{"file": r["file"], "why": r.get("why", "")} for r in bad],
        "warn": [{"file": r["file"], "why": r.get("why", "")} for r in warn],
        "missing": missing,
        "rows": rows,
    }


def summary_line(result: dict) -> str:
    """폰에서 한 줄로 읽을 결과."""
    n, ok = result.get("files", 0), result.get("ok", 0)
    warn = result.get("warn") or []
    bad, missing = result.get("bad") or [], result.get("missing") or []
    if not n:
        return "아직 파일이 없어요."
    parts = [f"{n}개 중 {ok}개 바로 쓸 수 있어요"]
    if warn:
        # ok=0 인데 '다시 찍을 건 없어요'라고 하면 앞뒤가 안 맞는다 — 아슬아슬한
        # 것도 세어 말한다(2026-09-04 검토).
        parts[-1] += f", {len(warn)}개는 아슬아슬하지만 쓸 수 있어요"
    if bad:
        parts.append(f"못 쓰는 {len(bad)}개: " +
                     ", ".join(f"{b['file']}({b['why'].split('—')[0].strip()})"
                               for b in bad[:3]))
    if missing:
        parts.append(f"컷이 {len(missing)}개 모자라요 — 예: {missing[0][:40]}")
    if not bad and not missing and not warn:
        parts.append("다시 찍을 건 없어요 👍")
    return " · ".join(parts)


def main() -> int:
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from . import source_watch
    args = " ".join(sys.argv[1:]).strip()
    root = source_watch.source_root()
    if not root:
        print("소재 창고를 찾지 못했어요(.env REEL_SOURCE_DIR).")
        return 1
    folder = os.path.join(root, args) if args else None
    if not folder or not os.path.isdir(folder):
        print("주제 폴더를 지정하세요. 지금 있는 폴더:")
        for t in source_watch.list_topics(root):
            print("  -", t["topic"])
        return 1
    res = check_folder(folder)
    for r in res["rows"]:
        print(f"  [{r['grade']}] {r['file']} — sharp={r.get('sharp')} "
              f"bright={r.get('bright')} {r.get('why', '')}")
    print("\n→", summary_line(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
