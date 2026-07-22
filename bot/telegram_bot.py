"""
텔레그램 봇

AI 비서가 생성한 리포트를 텔레그램으로 전송하고,
사용자 명령(/report, /reviews 등)에 응답한다.
"""

# TODO: .env 에서 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 불러오기 (python-dotenv)
# TODO: python-telegram-bot 으로 Application 초기화
# TODO: /start, /report, /reviews 등 명령 핸들러 등록
# TODO: 리포트 메시지 전송 함수 구현 (assistant.beargels 연동)
# TODO: 부정 리뷰 발생 시 즉시 알림 전송 로직
# TODO: 봇 폴링/웹훅 실행 로직


def send_message(text):
    """지정된 채팅방으로 메시지를 전송한다."""
    # TODO: 구현
    raise NotImplementedError


def main():
    """텔레그램 봇을 실행한다."""
    # TODO: 구현
    raise NotImplementedError


if __name__ == "__main__":
    main()
