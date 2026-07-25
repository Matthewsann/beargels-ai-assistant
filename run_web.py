"""베어글스 인스타 파이프라인 웹페이지 실행.

    python run_web.py

실행 후 브라우저에서 http://localhost:8000 접속.
구글 드라이브/크레딧 없이 로컬 폴더 + 브라우저 업로드로 동작한다.
"""

import logging
import os
import sys

# 어느 위치에서 실행하든 프로젝트 루트를 기준으로 동작하게 한다.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
os.chdir(_HERE)

import uvicorn

from sns_automation.webapp import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    app = create_app()
    print("\n  베어글스 인스타 파이프라인")
    print("  브라우저에서 열기 →  http://localhost:8000\n")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
