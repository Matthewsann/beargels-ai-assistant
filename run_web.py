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

# .env를 webapp 임포트(모듈 로드 시 경로 결정) 전에 읽는다 — PIPELINE_FINAL_DIR 등 반영.
from dotenv import load_dotenv

load_dotenv()

import uvicorn

from sns_automation.webapp import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _lan_ip() -> str | None:
    """같은 와이파이의 폰에서 접속할 때 쓸 PC의 내부 IP."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def main() -> None:
    app = create_app()
    host = os.getenv("PIPELINE_HOST", "0.0.0.0")
    port = int(os.getenv("PIPELINE_PORT", "8000"))
    print("\n  베어글스 인스타 파이프라인")
    print("  이 PC에서 열기   →  http://localhost:%d" % port)
    ip = _lan_ip()
    if ip and host != "127.0.0.1":
        print("  폰에서 열기(같은 와이파이) →  http://%s:%d" % (ip, port))
    code = os.getenv("PIPELINE_ACCESS_CODE", "").strip()
    if code:
        print("  접속 코드 설정됨 — 처음 접속 시 주소 뒤에 ?code=%s 붙이기" % code)
    print()
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
