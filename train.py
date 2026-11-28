"""执行单步有界在线 PPO 更新，或在新进程中复核权重与采样回放。"""
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from packet_training import main
    main()
