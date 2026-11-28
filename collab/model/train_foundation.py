"""基于已采样并核验通过的历史工单，训练可选的底图策略选择器。"""
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root), str(root / "src")]
    from foundation_training import main
    main()
