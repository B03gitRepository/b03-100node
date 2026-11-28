"""兼容仅将 src/ 加入导入路径的调用方，转发工单知识端口函数。"""
from pathlib import Path
import sys

_project_root = str(Path(__file__).resolve().parents[1])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from knowledge.work_order import (BINDING_CONTRACT, accepts_model, accepts_rules, check_om_advice,
                                  configured, diagnosis_ids, diagnosis_query, lookup, validate_binding_edition)
from knowledge.replay import verify, verify_work_order_queries
