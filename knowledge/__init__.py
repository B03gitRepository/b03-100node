"""面向 OM 与 RC 的版本化网络底图事实及工单知识模块。"""
from pathlib import Path
import sys

# 既有公开组件习惯直接将 src/ 作为顶层模块路径导入共享运行时，此处保持兼容。
_source = str(Path(__file__).resolve().parents[1] / "src")
if _source not in sys.path:
    sys.path.append(_source)
