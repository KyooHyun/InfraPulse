"""캘리브레이션이 쓸 룰 집합 — DB에 붙지 않고도 운영과 같은 룰을 만든다."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Collection, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models
from app.fds_engine import DEFAULT_RULES


def default_rules(
    overrides: Optional[Dict[str, Dict[str, float]]] = None,
    exclude: Collection[str] = (),
) -> List[models.FdsRule]:
    """시드 룰을 그대로 만든다. overrides로 임계값/가중치를 바꿔 실험하고, exclude로 룰을 뺄 수 있다.

    세션에 붙이지 않은 SQLAlchemy 인스턴스라 DB가 없어도 만들어진다 —
    캘리브레이션은 MySQL이 떠 있지 않은 자리에서도 돌아야 한다.
    """
    rules: List[models.FdsRule] = []
    for spec in DEFAULT_RULES:
        if spec["condition_type"] in exclude:
            continue
        values: Dict[str, Any] = dict(spec)
        if overrides and values["condition_type"] in overrides:
            values.update(overrides[values["condition_type"]])
        rules.append(models.FdsRule(**values))
    return rules


def with_threshold(rule_type: str, threshold: float) -> List[models.FdsRule]:
    """특정 룰의 임계값만 바꾼 룰 집합."""
    return default_rules({rule_type: {"threshold": threshold}})
