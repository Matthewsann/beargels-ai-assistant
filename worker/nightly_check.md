# 새벽 자동 점검 — 매일 03:30 (집 PC)

무엇을 하는가: 어제 하루 직원 웹앱과 집 PC 일꾼에서 난 **오류를 읽고, 원인을
고치고, 커밋·배포까지** 한다. 사장님이 아침에 결과만 확인하면 된다.

## 실행되는 지시문

아래 내용이 매일 03:30 에 Claude Code 로 실행된다.

---

베어글스 리뷰 답글 서비스의 새벽 자동 점검이다. 순서대로 하라.

1. `git pull` 로 최신 코드를 받는다.

2. 아래로 처리 안 된 오류를 읽는다.
   ```
   python -c "from database import supabase_client as db; import json; print(json.dumps(db.get_errors(only_unfixed=True, limit=100), ensure_ascii=False, indent=1))"
   ```

3. 오류가 없으면 아무것도 고치지 말고 "이상 없음"만 남기고 끝낸다.
   **없는 문제를 만들어내지 말 것.**

4. 오류가 있으면 같은 원인끼리 묶고, 원인별로 판단한다.
   - **코드 버그** → 고친다. 고칠 때마다 재현 조건을 확인하고, 가능하면
     `tests/` 에 회귀 테스트를 추가한다.
   - **환경 문제**(집 PC Chrome 꺼짐 = `CDP attach 실패`, 배민·쿠팡 로그인
     만료, 네트워크 일시 오류) → **코드를 고치지 말 것.** 사장님이 해야 할
     일이므로 보고에만 적는다.
   - **AI 크레딧 부족** → 코드 문제가 아니다. 보고에만 적는다.

5. 고친 것이 있으면:
   - `python -m py_compile` 로 문법 확인, 관련 테스트 실행
   - 커밋 메시지에 **어떤 오류를 왜 그렇게 고쳤는지** 적는다
   - `git push origin integration` 과 `git push origin integration:main` 둘 다
   - 배포 반영은 사장님이 PythonAnywhere 에서 `git pull` + Reload 해야 한다는
     것을 보고에 적는다(웹앱은 자동 반영되지 않는다)

6. 처리한 오류는 아래로 표시해 다음 날 또 보지 않게 한다.
   ```
   python -c "from database import supabase_client as db; db.mark_error_fixed(<id>, '<처리 내용>')"
   ```
   환경 문제로 코드를 안 고친 건도 `mark_error_fixed` 로 사유를 적어 닫는다.

7. **답글 공부(매일):** 어제 직원이 AI 초안을 고친 쌍을 읽는다.
   ```
   python -c "from database import supabase_client as db; import json; print(json.dumps(db.get_edit_pairs(days=1), ensure_ascii=False, indent=1))"
   ```
   - 고친 쌍이 없으면 이 단계는 건너뛴다.
   - 고친 쌍이 있으면 **반복되는 패턴만** 뽑아 `reference/reply_lessons.md` 를
     갱신한다(규칙 최대 15개 유지, 파일 머리의 운영 원칙을 따른다).
     직원이 거의 안 고친 좋은 최종본이 있으면 '모범 예시'에 1~2개 추가한다.
   - ⚠️ 이 파일 밖(생성 코드·페르소나)은 공부를 이유로 고치지 않는다.
     lessons 파일은 다음 생성 프롬프트에 자동 반영된다.
   - 유형별 수정률도 함께 확인해 점검기록에 적는다:
   ```
   python -c "from database import supabase_client as db; print(db.edit_rate_by_kind())"
   ```

8. 마지막에 `worker/점검기록.md` 맨 위에 그날 결과를 6줄 이내로 덧붙인다.
   (날짜 / 오류 건수 / 고친 것 / 배운 규칙 / 유형별 수정률 / 사장님이 해야 할 것)

## 지켜야 할 선

- **실고객에게 나가는 답글 문구는 새벽에 바꾸지 않는다.** 톤 변경은 사장님
  확인을 거친다. 오류 수정만 한다.
- 리뷰·답글 **데이터를 지우지 않는다.** 스키마 변경(DDL)도 하지 않는다.
- 확신이 없으면 고치지 말고 보고에만 적는다. 새벽에 잘못 고치면 아침에
  직원들이 못 쓴다.
