"""B03 工单采样与封存结果评价的统一对外入口。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from episode_contract import evaluate, main, sample

if __name__ == "__main__":
    main()
