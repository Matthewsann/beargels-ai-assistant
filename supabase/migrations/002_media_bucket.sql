-- 발행용 이미지 임시 호스팅 버킷
-- Supabase SQL Editor에서 실행하세요.
--
-- Buffer API는 파일을 직접 받지 않고 "공개 URL"만 받는다.
-- 드라이브에서 받은 사진을 JPEG로 변환해 이 공개 버킷에 올리고,
-- 그 URL을 Buffer에 전달한다.

insert into storage.buckets (id, name, public)
values ('sns-media', 'sns-media', true)
on conflict (id) do nothing;

-- 봇(anon 키)이 버킷에 올리고 지울 수 있게 정책 추가
drop policy if exists "sns-media anon insert" on storage.objects;
create policy "sns-media anon insert" on storage.objects
    for insert to anon with check (bucket_id = 'sns-media');

drop policy if exists "sns-media anon update" on storage.objects;
create policy "sns-media anon update" on storage.objects
    for update to anon using (bucket_id = 'sns-media');

drop policy if exists "sns-media anon delete" on storage.objects;
create policy "sns-media anon delete" on storage.objects
    for delete to anon using (bucket_id = 'sns-media');

drop policy if exists "sns-media public read" on storage.objects;
create policy "sns-media public read" on storage.objects
    for select to anon using (bucket_id = 'sns-media');
