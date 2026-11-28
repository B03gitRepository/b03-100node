"""启动单次分组仿真工单，或离线核验历史运行归档。"""
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from cli import main
    main()
