"""콘솔 출력 인코딩 — 한국어 Windows에서 리포트가 중간에 끊기는 것을 막는다.

한국어 Windows의 기본 콘솔 코드페이지는 cp949다. 이 스크립트들의 리포트에는
em dash(—)나 화살표(→) 같은 cp949에 없는 문자가 섞여 있어서, 그대로 print하면
UnicodeEncodeError로 분석이 **결과를 다 계산해 놓고 출력 도중에** 죽는다.
실제로 `python -m calibration.reachability`가 그렇게 죽었다.

그래서 각 CLI의 main() 첫 줄에서 stdout/stderr를 UTF-8로 바꾼다. import 시점이
아니라 main()에서 하는 이유는, pytest가 sys.stdout을 자기 캡처 객체로 바꿔 놓기
때문이다 — 패키지를 import하는 것만으로 남의 stdout을 건드리면 안 된다.
"""
from __future__ import annotations

import sys


def use_utf8_console() -> None:
    """stdout/stderr를 UTF-8로 전환한다. 불가능한 스트림이면 조용히 넘어간다."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # pytest 캡처 객체처럼 reconfigure가 없는 스트림 — 건드리지 않는다.
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # 이미 detach된 스트림 등. 출력 인코딩 때문에 분석을 죽일 이유는 없다.
            pass
