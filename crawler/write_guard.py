"""
쓰기 액션 안전장치 — dry-run + 사용자 승인 강제

리뷰 답글 작성·프로모션 관리 등 사장님센터에 실제 변경을 가하는 작업은
잘못되면 고객에게 그대로 노출된다. 그래서 모든 쓰기 액션은 이 계약을 따른다:

  1. dry-run 이 기본(WRITE_DRY_RUN=true). 무엇을 할지 preview() 로 보여주기만 함.
  2. 실제 실행은 명시적 승인(confirm=True)이 있어야만 _apply() 호출.
  3. 승인 없이 실행 시도하면 PermissionError.

향후 각 쓰기 작업(예: ReplyToReviewAction)은 WriteAction 을 상속해
preview()/_apply() 만 구현하면 된다. 크롤러/스케줄러는 run() 만 호출한다.
"""

import abc
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _default_dry_run():
    return os.getenv("WRITE_DRY_RUN", "true").strip().lower() not in (
        "false", "0", "no", "off")


class WriteAction(abc.ABC):
    """모든 쓰기 액션의 베이스. preview()/_apply() 를 구현해 확장한다."""

    #: 사람이 읽을 액션 이름 (로그/알림용)
    name = "write-action"

    @abc.abstractmethod
    def preview(self):
        """실행하면 무엇이 바뀌는지 사람이 읽을 문자열로 반환(부작용 없음)."""

    @abc.abstractmethod
    def _apply(self):
        """실제 변경을 수행한다. run() 을 통해서만 호출되어야 한다."""

    def run(self, confirm=False, dry_run=None):
        """액션을 실행(또는 dry-run)한다.

        Args:
            confirm: True 여야만 실제 실행. 승인 단계에 해당.
            dry_run: None 이면 .env WRITE_DRY_RUN 을 따름. True 면 미리보기만.

        Returns:
            {"applied": bool, "dry_run": bool, "preview": str, "result": ...}
        """
        dry_run = _default_dry_run() if dry_run is None else dry_run
        preview = self.preview()

        if dry_run:
            logger.info("[DRY-RUN] %s\n%s", self.name, preview)
            return {"applied": False, "dry_run": True,
                    "preview": preview, "result": None}

        if not confirm:
            raise PermissionError(
                f"쓰기 액션 '{self.name}' 은 승인(confirm=True)이 필요합니다. "
                f"미리보기:\n{preview}"
            )

        logger.info("[APPLY] %s\n%s", self.name, preview)
        result = self._apply()
        return {"applied": True, "dry_run": False,
                "preview": preview, "result": result}
