"""PythonAnywhere(및 일반 WSGI 서버)용 진입점.

PythonAnywhere 웹 탭의 WSGI 설정 파일에서 이 파일을 가리키게 한다:

    import sys
    path = '/home/<사용자명>/beargels-ai-assistant/service'
    if path not in sys.path:
        sys.path.insert(0, path)
    from wsgi import application            # noqa

설정값(SUPABASE_URL / SUPABASE_SERVICE_KEY / SERVICE_PATH)은 같은 폴더의
.env 파일에서 읽는다. 그 파일은 git 에 올리지 않는다(비밀값).
"""
from app import app as application  # noqa: F401
