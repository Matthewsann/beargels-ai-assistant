-- 플랫폼용 짧은 메뉴 소개글(한/영) — Supabase SQL Editor에서 실행하세요.
--
-- 기존 description 은 정본용 긴 설명이다(3~4줄 + 추천 크림치즈까지).
-- 네이버 스마트플레이스·배민·쿠팡·토스 키오스크에 넣을 때는 한두 줄짜리가
-- 필요한데, 그때마다 직원이 새로 쓰다 보니 채널마다 문구가 달라졌다.
-- 여기 한 번 적어두고 각 채널에 복사해 쓴다(2026-08-17).
--
-- 영문은 토스 키오스크의 '키오스크 상품설명 🇺🇸' 칸에 그대로 들어간다.

alter table menu_items add column if not exists intro_ko text;
alter table menu_items add column if not exists intro_en text;
