"""점수 계산의 구성 요소가 한 곳에만 정의되어 있는지 고정한다.

철회한 ULB 비교표의 원인은 평가 스크립트가 엔진 로직을 따로 구현한 것이었다 —
룰은 45점(실제 30점), α는 스크립트마다 0.4/0.6, IF 원점수 정규화도 제각각이었다.
같은 일이 다시 생기면 이 테스트가 실패한다.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("app", "scripts", "calibration", "evaluation")

# (무엇을, 어디에만 있어야 하는가, 찾는 패턴)
SINGLE_SOURCES = [
    ("앙상블 α 정의", "app/ml/ensemble.py", re.compile(r"^\s*\w*ALPHA\w*\s*=", re.M)),
    ("IsolationForest 생성", "app/ml/isolation_forest.py", re.compile(r"\bIsolationForest\(")),
    ("IF 원점수 호출(정규화 전)", "app/ml/isolation_forest.py",
     re.compile(r"\.(score_samples|decision_function)\(")),
    ("룰 기본 임계값·가중치", "app/fds_engine.py", re.compile(r"\"weight\"\s*:\s*\d")),
]


def _python_files():
    for top in SCANNED:
        yield from (ROOT / top).rglob("*.py")


def test_scoring_components_are_defined_once():
    violations = []
    for what, owner, pattern in SINGLE_SOURCES:
        for path in _python_files():
            rel = path.relative_to(ROOT).as_posix()
            if rel == owner:
                continue
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                line = path.read_text(encoding="utf-8").count("\n", 0, match.start()) + 1
                violations.append(f"{what}: {rel}:{line} (정의 위치는 {owner})")
    assert not violations, "\n".join(violations)


def test_owners_still_define_them():
    """패턴이 낡아서 아무것도 못 잡는 상태가 되지 않도록, 정의 위치에서는 잡혀야 한다."""
    for what, owner, pattern in SINGLE_SOURCES:
        assert pattern.search((ROOT / owner).read_text(encoding="utf-8")), f"{what} 패턴이 {owner}에서 안 잡힌다"


def test_ml_features_use_only_pre_transaction_values():
    """ML 피처는 이체 실행 전에 알 수 있는 값만 받는다.

    예전 피처 9개 중 4개가 이체 후 잔액(balance_*_after, error_*)이었다 — 판정 시점에 없는
    정보라 운영에서 이체를 보류시킬 수 없었고, PaySim에서는 성능을 부풀렸다.
    """
    import inspect
    from app.ml.features import FEATURE_NAMES, feature_values

    params = inspect.signature(feature_values).parameters
    assert not [p for p in params if "after" in p], "feature_values가 거래 후 값을 받는다"
    assert not [f for f in FEATURE_NAMES if "after" in f or f.startswith("error_")]
